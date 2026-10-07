"""Prepare immutable companion jobs and summarize recorded simulation-tick trials."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import itertools
import json
import math
from pathlib import Path
import re
import shutil
import stat
import statistics
import tempfile
import zipfile

from .blueprint import COUNTER_COLORS, DIRECTIONS, blueprint_hash, validate_blueprint
from .compiler import compile_blueprint
from .environment import resolve_instance


JOB_FORMAT = "retpack-farm-job-v1"
RESULT_FORMAT = "retpack-farm-job-result-v1"
SUMMARY_FORMAT = "retpack-farm-job-summary-v1"
_COUNTERS = ("lime", "blue", *(color for color in COUNTER_COLORS if color not in {"lime", "blue"}))
_JOB_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}\Z")
_SHA256 = re.compile(r"[a-f0-9]{64}\Z")
_IDENTITIES = ("blueprint_sha256", "instantiated_blueprint_sha256", "artifact_sha256")
_RESERVED_NAMES = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}
_TIMING = ("warmup_ticks", "run_ticks", "drain_ticks")
_GAP = 16
_WORLD_LIMIT = 30_000_000
_WOOL = frozenset(f"minecraft:{color}_wool" for color in COUNTER_COLORS)


def _integer(value: object, where: str, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ValueError(f"{where}: expected an integer >= {minimum}")
    return value


def _vector(value: object, where: str) -> list[int]:
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise ValueError(f"{where}: expected three integers [x, y, z]")
    if any(type(part) is not int or not -(2**31) <= part < 2**31 for part in value):
        raise ValueError(f"{where}: coordinates must be signed 32-bit integers")
    return list(value)


def _text(value: object, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{where}: expected a nonempty string")
    return value


def _unique_keys(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"JSON: duplicate object key {key!r}")
        result[key] = value
    return result


def _invalid_constant(value: str) -> None:
    raise ValueError(f"JSON: {value} is not a finite JSON number")


def _finite_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"JSON: {value} is not a finite JSON number")
    return number


def _read_object(payload: bytes) -> dict:
    value = json.loads(payload.decode("utf-8-sig"), object_pairs_hook=_unique_keys, parse_constant=_invalid_constant, parse_float=_finite_float)
    if not isinstance(value, dict):
        raise ValueError("Expected a JSON object")
    return value


def _write_json(path: Path, value: dict) -> None:
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, indent=2, ensure_ascii=False, allow_nan=False)
        stream.write("\n")


def _entity_count(entities: list[dict]) -> int:
    return sum(1 + _entity_count(entity.get("passengers", [])) for entity in entities)


def _check_counter_routes(blueprint: dict) -> None:
    positions = {tuple(block["pos"]): block for block in blueprint["blocks"]}
    declared = {tuple(port["position"]) for port in blueprint["ports"]}
    for position, block in positions.items():
        if block["state"]["Name"] != "minecraft:hopper" or position in declared:
            continue
        direction = block["state"]["Properties"]["facing"]
        sink_position = tuple(part + delta for part, delta in zip(position, DIRECTIONS[direction], strict=True))
        sink = positions.get(sink_position)
        if sink is not None and sink["state"]["Name"] in _WOOL:
            raise ValueError(
                f"{blueprint['name']}: undeclared hopper-to-wool output at logical {block['pos']}, "
                f"facing {direction} into {sink['state']['Name']} at {list(sink_position)}; "
                "remove this extra counter route before preparing a job"
            )


def _publish_job(stage: Path, target: Path) -> None:
    # mkdir is the cross-platform, atomic no-replace reservation. Renaming the
    # whole staging directory can overwrite a raced empty directory on POSIX.
    try:
        target.mkdir(exist_ok=False)
    except FileExistsError as exc:
        raise FileExistsError(f"Job already exists; choose a new --id: {target}") from exc
    reservation = target.lstat()
    try:
        for entry in sorted(stage.iterdir(), key=lambda path: path.name):
            if entry.name != "job.json":
                entry.rename(target / entry.name)
        # A folder is not runnable until all its artifacts are in place.
        (stage / "job.json").rename(target / "job.json")
    except BaseException as exc:
        try:
            current = target.lstat()
            if (stat.S_ISDIR(current.st_mode)
                    and (current.st_dev, current.st_ino) == (reservation.st_dev, reservation.st_ino)):
                shutil.rmtree(target)
        except FileNotFoundError:
            pass
        except OSError as cleanup_error:
            exc.add_note(f"Cannot remove the incomplete job reservation {target}: {cleanup_error}")
        raise


def _layout(blueprints: list[dict], origin: list[int] | None, at: object) -> list[list[int]]:
    if at is not None:
        if origin is None:
            raise ValueError("--at requires an explicit --origin")
        if not isinstance(at, (list, tuple)) or len(at) != len(blueprints):
            raise ValueError("Supply exactly one --at X Y Z per blueprint, in blueprint order")
        offsets = [
            _vector([part - origin[axis] for axis, part in enumerate(_vector(position, "--at"))], "offset")
            for position in at
        ]
    else:
        offsets = []
        x = 0
        for blueprint in blueprints:
            offsets.append(_vector([x, 0, 0], "offset"))
            x += blueprint["bounds"]["size"][0] + _GAP
    for index, (blueprint, offset) in enumerate(zip(blueprints, offsets, strict=True)):
        corner = offset if origin is None else [origin[axis] + offset[axis] for axis in range(3)]
        _vector(corner, f"design {index + 1} origin")
        maximum = _vector([corner[axis] + blueprint["bounds"]["size"][axis] - 1 for axis in range(3)], f"design {index + 1} maximum")
        if any(abs(part) > _WORLD_LIMIT for part in (*offset, *corner, *maximum)):
            raise ValueError("Companion coordinates and offsets must be within -30000000 to 30000000")
        for earlier in range(index):
            if all(
                offset[axis] < offsets[earlier][axis] + blueprints[earlier]["bounds"]["size"][axis]
                and offsets[earlier][axis] < offset[axis] + blueprint["bounds"]["size"][axis]
                for axis in range(3)
            ):
                raise ValueError(f"Designs {earlier + 1} and {index + 1} have overlapping bounding boxes")
    return offsets


def prepare_job(
    blueprints: list[str | Path | dict],
    job_id: str,
    instance: str | Path | None = None,
    *,
    origin: list[int] | tuple[int, int, int] | None = None,
    mode: str = "place",
    repeats: int = 3,
    at: list[list[int]] | None = None,
) -> dict:
    """Install a new job and return its v1 JSON object; never launch Minecraft.

    Paths preserve the original source JSON bytes. Dictionaries are copied and
    preserved as source JSON. Derived lab blueprints receive unique counters;
    source hashes never change. The first version supports natural-growth
    bamboo, with one matching output per design and at most 16 designs.
    Defaults place bays along x with a
    16-block gap. ``at`` contains absolute exported origins in blueprint order
    and requires ``origin``; offsets are calculated from that common origin.
    Existing mode always requires an explicit origin. Files are compiled in a
    fresh staging folder, then installed at config/farmbench/jobs/<job_id>.
    Existing job folders, even empty ones, are refused.
    """
    if not isinstance(job_id, str) or not _JOB_ID.fullmatch(job_id) or job_id in _RESERVED_NAMES:
        raise ValueError("Job id: use 1–64 lowercase letters, digits, underscores or hyphens; avoid reserved filenames")
    if mode not in {"place", "existing"}:
        raise ValueError("Mode must be place or existing")
    repeats = _integer(repeats, "repeats", 1)
    if repeats > 100:
        raise ValueError("The companion supports at most 100 repeats per job")
    origin = None if origin is None else _vector(origin, "origin")
    if mode == "existing" and origin is None:
        raise ValueError("Existing mode requires an explicit --origin X Y Z")
    if not isinstance(blueprints, (list, tuple)) or not 1 <= len(blueprints) <= len(_COUNTERS):
        raise ValueError("Supply 1–16 blueprints; each design needs a distinct Carpet counter")
    sources = []
    validated = []
    for source in blueprints:
        if isinstance(source, dict):
            payload = (json.dumps(source, indent=2, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
        else:
            payload = Path(source).read_bytes()
        data = validate_blueprint(_read_object(payload))
        if data["benchmark"]["item"] != "minecraft:bamboo":
            raise ValueError(f"{data['name']}: companion jobs currently support the natural-growth bamboo protocol only")
        if len(data["ports"]) != 1 or data["ports"][0]["counter"] != data["benchmark"]["counter"]:
            raise ValueError(f"{data['name']}: jobs require exactly one output port matching benchmark.counter")
        _check_counter_routes(data)
        if validated:
            first = validated[0]
            if data["minecraft"] != first["minecraft"] or data["data_version"] != first["data_version"]:
                raise ValueError("All designs must use the same Minecraft version and DataVersion")
            if data["conditions"] != first["conditions"]:
                raise ValueError("All designs must have the same conditions")
            if any(data["benchmark"][key] != first["benchmark"][key] for key in _TIMING):
                raise ValueError("All designs must have the same warmup, run and drain timing")
        sources.append(payload)
        validated.append(data)
    cells = 0
    for data in validated:
        size = data["bounds"]["size"]
        if any(part > 512 for part in size):
            raise ValueError("The companion supports bay dimensions up to 512 blocks per axis")
        cells += math.prod(size)
        if cells > 1_000_000:
            raise ValueError("The companion supports at most 1,000,000 total bay cells per job")
        for key, minimum, maximum in (("warmup_ticks", 0, 10_000_000), ("run_ticks", 1, 10_000_000), ("drain_ticks", 1, 1_000_000)):
            if not minimum <= data["benchmark"][key] <= maximum:
                raise ValueError(f"Companion {key} must be between {minimum} and {maximum}")
        if not 1 <= data["conditions"]["random_tick_speed"] <= 4096:
            raise ValueError("Companion random_tick_speed must be between 1 and 4096")
        if _entity_count(data.get("entities", [])) > 10_000:
            raise ValueError("The companion supports at most 10,000 entities per design, including passengers")
    offsets = _layout(validated, origin, at)
    root = resolve_instance(instance) / "config" / "farmbench" / "jobs"
    target = root / job_id
    if target.exists() or target.is_symlink():
        raise FileExistsError(f"Job already exists; choose a new --id: {target}")
    root.mkdir(parents=True, exist_ok=True)
    first = validated[0]
    job = {
        "format": JOB_FORMAT, "id": job_id, "minecraft": first["minecraft"], "data_version": first["data_version"],
        "mode": mode, "origin": origin, "repeats": repeats,
        **{key: first["benchmark"][key] for key in _TIMING},
        "conditions": deepcopy(first["conditions"]), "designs": [],
    }
    with tempfile.TemporaryDirectory(prefix=f".{job_id}-", dir=root) as temporary:
        stage = Path(temporary) / "job"
        stage.mkdir()
        for index, source in enumerate(validated):
            design_id = chr(ord("a") + index)
            counter = _COUNTERS[index]
            derived = deepcopy(source)
            port = derived["ports"][0]
            sink = [part + delta for part, delta in zip(port["position"], DIRECTIONS[port["direction"]], strict=True)]
            for block in derived["blocks"]:
                if block["pos"] == sink:
                    block["state"] = {"Name": f"minecraft:{counter}_wool"}
            port["counter"] = counter
            derived["benchmark"]["counter"] = counter
            derived = validate_blueprint(derived)
            build_dir = stage / "builds" / design_id
            build = compile_blueprint(derived, build_dir, variant="lab")
            instantiated_hash = blueprint_hash(derived)
            if build["blueprint_sha256"] != instantiated_hash:
                raise ValueError("Compiler build identity differs from instantiated blueprint")
            nbt = build["files"]["nbt"]
            structure_path = Path(nbt["path"])
            if not structure_path.is_absolute():
                structure_path = build_dir / structure_path
            structure = structure_path.read_bytes()
            digest = hashlib.sha256(structure).hexdigest()
            if digest != nbt["sha256"]:
                raise ValueError("Compiled structure checksum differs from build metadata")
            (stage / f"{design_id}.nbt").write_bytes(structure)
            (build_dir / "source.json").write_bytes(sources[index])
            # Compiler paths reference staging. Keep the saved build portable,
            # relative to its own directory, after installation or archiving.
            for entry in build["files"].values():
                entry["path"] = Path(entry["path"]).name
            build["artifact"]["path"] = Path(build["artifact"]["path"]).name
            (build_dir / "build.json").write_text(json.dumps(build, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")
            logical_origin = source["bounds"]["min"]
            job["designs"].append({
                "id": design_id, "name": source["name"], "blueprint_sha256": blueprint_hash(source),
                "instantiated_blueprint_sha256": instantiated_hash, "artifact_sha256": digest,
                "structure": f"{design_id}.nbt", "size": list(source["bounds"]["size"]), "offset": offsets[index],
                "counter": counter, "item": source["benchmark"]["item"],
                "entity_count": _integer(build.get("entity_count", _entity_count(derived.get("entities", []))), "entity_count"),
                "ports": [{"position": [port["position"][axis] - logical_origin[axis] for axis in range(3)], "direction": port["direction"]}],
            })
        _write_json(stage / "job.json", job)
        _publish_job(stage, target)
    return job


def companion_installed(instance: str | Path) -> bool:
    """Check installed JAR metadata only, not whether a mod is currently loaded."""
    for jar in (Path(instance) / "mods").glob("*.jar"):
        try:
            with zipfile.ZipFile(jar) as archive:
                metadata = json.loads(archive.read("fabric.mod.json"))
            if isinstance(metadata, dict) and metadata.get("id") == "farmbench":
                return True
        except (OSError, KeyError, ValueError, RuntimeError, zipfile.BadZipFile):
            continue
    return False


def _stats(values: list[float]) -> dict:
    return {
        "n": len(values),
        "mean_items_per_hour": statistics.mean(values) if values else None,
        "sample_sd_items_per_hour": statistics.stdev(values) if len(values) > 1 else None,
    }


def summarize_results(result: str | Path | dict) -> dict:
    """Return descriptive statistics from actual companion trials, never a CI.

    Rates are recalculated from raw counts and measured growth-enabled ticks.
    Missing, partial or off-plan trials remain visible but are excluded from
    aggregate comparisons. Failed/cancelled jobs retain descriptive statistics
    for their intact trials, labelled diagnostic rather than comparable.
    """
    if not isinstance(result, dict):
        result = _read_object(Path(result).read_bytes())
    else:
        result = _read_object(json.dumps(result, allow_nan=False).encode("utf-8"))
    if result.get("format") != RESULT_FORMAT:
        raise ValueError("Unsupported companion result format")
    status = result.get("status")
    if not isinstance(status, str) or status not in {"completed", "failed", "cancelled", "incomplete"}:
        raise ValueError("Result status must be completed, failed, cancelled or incomplete")
    job = result.get("job")
    if not isinstance(job, dict) or job.get("format") != JOB_FORMAT:
        raise ValueError("Result must embed a v1 companion job")
    job_id = _text(job.get("id"), "job.id")
    requested = _integer(job.get("repeats"), "job.repeats", 1)
    planned_ticks = _integer(job.get("run_ticks"), "job.run_ticks", 1)
    designs = job.get("designs")
    if not isinstance(designs, list) or not designs:
        raise ValueError("job.designs must be a nonempty array")
    expected = {}
    counters = set()
    for design in designs:
        if not isinstance(design, dict):
            raise ValueError("job.designs entries must be objects")
        design_id = _text(design.get("id"), "design.id")
        counter = design.get("counter")
        if not isinstance(counter, str) or design_id in expected or counter in counters or counter not in COUNTER_COLORS:
            raise ValueError("Job design ids and valid counters must be distinct")
        _text(design.get("item"), "design.item")
        for key in _IDENTITIES:
            if key in design and (not isinstance(design[key], str) or not _SHA256.fullmatch(design[key])):
                raise ValueError(f"design.{key} must be a lowercase SHA256 digest")
        expected[design_id] = design
        counters.add(counter)
    trials = result.get("trials", [])
    errors = result.get("errors", [])
    if not isinstance(trials, list) or not isinstance(errors, list):
        raise ValueError("Result trials and errors must be arrays")
    flags = []
    if status != "completed":
        flags.append(f"status_{status}")
    if "incomplete" in result:
        if type(result["incomplete"]) is not bool:
            raise ValueError("Result incomplete must be a boolean")
        if result["incomplete"]:
            flags.append("runner_marked_incomplete")
    if "comparable" in result:
        if type(result["comparable"]) is not bool:
            raise ValueError("Result comparable must be a boolean")
        if not result["comparable"]:
            flags.append("runner_marked_noncomparable")
    if errors:
        flags.append("reported_errors")
    if len(trials) != requested:
        flags.append("repeat_count_differs_from_job")
    rows = []
    usable = []
    indices = set()
    for trial in trials:
        if not isinstance(trial, dict):
            raise ValueError("Trial entries must be objects")
        index = _integer(trial.get("index"), "trial.index")
        if index in indices:
            raise ValueError("Duplicate trial index")
        indices.add(index)
        ticks = trial.get("measured_ticks")
        if ticks is not None:
            ticks = _integer(ticks, "trial.measured_ticks")
        trial_flags = []
        if not 1 <= index <= requested:
            trial_flags.append("trial_index_outside_job")
        if ticks != planned_ticks:
            trial_flags.append("measured_ticks_differ_from_job")
        if "completed" in trial and type(trial["completed"]) is not bool:
            raise ValueError("trial.completed must be a boolean")
        if "status" in trial and not isinstance(trial["status"], str):
            raise ValueError("trial.status must be a string")
        if trial.get("completed") is False or trial.get("status", "completed") != "completed":
            trial_flags.append("incomplete_trial")
        if "paired" in trial:
            if type(trial["paired"]) is not bool:
                raise ValueError("trial.paired must be a boolean")
            if not trial["paired"]:
                trial_flags.append("unpaired_trial")
        if "errors" in trial:
            if not isinstance(trial["errors"], list):
                raise ValueError("trial.errors must be an array")
            if trial["errors"]:
                trial_flags.append("trial_errors")
        readings = trial.get("designs", [])
        if not isinstance(readings, list):
            raise ValueError("trial.designs must be an array")
        recorded = {}
        for reading in readings:
            if not isinstance(reading, dict):
                raise ValueError("Trial design readings must be objects")
            design_id = reading.get("id")
            if not isinstance(design_id, str) or design_id not in expected or design_id in recorded:
                raise ValueError("Trial contains an unknown or duplicate design id")
            design = expected[design_id]
            if reading.get("counter") != design["counter"] or reading.get("item") != design["item"]:
                raise ValueError("Trial counter or item differs from embedded job")
            for key in _IDENTITIES:
                if key in reading and reading[key] != design.get(key):
                    raise ValueError(f"Trial {key} differs from embedded job")
            items = _integer(reading.get("items"), "trial.design.items")
            rate = items * 72000 / ticks if ticks else None
            supplied = reading.get("items_per_hour")
            if supplied is not None:
                if type(supplied) not in (int, float) or not math.isfinite(supplied) or supplied < 0:
                    raise ValueError("items_per_hour must be a finite nonnegative number")
                if rate is None or not math.isclose(supplied, rate, rel_tol=1e-9, abs_tol=1e-9):
                    raise ValueError("Reported items_per_hour differs from raw items and measured_ticks")
            recorded[design_id] = {**deepcopy(reading), "id": design_id, "counter": design["counter"], "item": design["item"], "items": items, "items_per_hour": rate}
        if set(recorded) != set(expected):
            trial_flags.append("missing_design_readings")
        row = {**deepcopy(trial), "index": index, "measured_ticks": ticks, "designs": list(recorded.values()), "included_in_statistics": not trial_flags, "flags": trial_flags}
        rows.append(row)
        if trial_flags:
            flags.append("incomplete_or_off_plan_trials")
        else:
            usable.append(recorded)
    if len(indices) != requested or any(not 1 <= index <= requested for index in indices):
        flags.append("trial_indices_differ_from_job")
    if len(usable) != requested:
        flags.append("incomplete_repeats")
    summaries = []
    for design_id, design in expected.items():
        summaries.append({
            "id": design_id, "name": design.get("name", design_id), "counter": design["counter"], "item": design["item"],
            **_stats([trial[design_id]["items_per_hour"] for trial in usable]),
        })
    pairs = []
    warnings = ["Descriptive statistics only. Small samples do not establish a precise effect or confidence interval."]
    for a, b in itertools.combinations(expected, 2):
        if expected[a]["item"] != expected[b]["item"]:
            warnings.append(f"No paired difference for {b} - {a}: target items differ.")
            continue
        pairs.append({
            "a": a, "b": b, "direction": "b-a", "item": expected[a]["item"],
            **_stats([trial[b]["items_per_hour"] - trial[a]["items_per_hour"] for trial in usable]),
        })
    flags = list(dict.fromkeys(flags))
    summary = {
        **deepcopy(result),
        "format": SUMMARY_FORMAT, "job_id": job_id, "status": status, "comparable": not flags,
        "rate_kind": "comparable" if not flags else "diagnostic_only", "flags": flags,
        "requested_repeats": requested, "recorded_trials": len(rows), "included_trials": len(usable),
        "rate_denominator": "measured_growth_enabled_run_ticks", "nominal_ticks_per_second": 20,
        "trials": rows, "designs": summaries, "paired_differences": pairs, "errors": errors, "warnings": warnings,
    }
    return summary
