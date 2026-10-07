"""Fixture-only job preparation and checks on actual recorded trial statistics."""

from copy import deepcopy
import gzip
import hashlib
import io
import json
from pathlib import Path
import zipfile

import nbtlib
import pytest

from farmbench.blueprint import blueprint_hash, load_blueprint, validate_blueprint
from farmbench.jobs import JOB_FORMAT, RESULT_FORMAT, companion_installed, prepare_job, summarize_results


EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "bamboo_micro_v1.json"


@pytest.fixture
def instance(tmp_path):
    root = tmp_path / "fixture-instance"
    root.mkdir()
    (root / "saves").mkdir()
    (root / "saves" / "do-not-touch.dat").write_bytes(b"fixture world")
    return root


@pytest.fixture
def blueprint():
    return load_blueprint(EXAMPLE)


def job_folder(instance, job_id="bamboo-ab"):
    return instance / "config" / "farmbench" / "jobs" / job_id


def cells(path):
    root = nbtlib.File.from_fileobj(io.BytesIO(gzip.decompress(path.read_bytes())))
    return root, {
        tuple(int(part) for part in entry["pos"]): str(root["palette"][int(entry["state"])]["Name"])
        for entry in root["blocks"]
    }


def test_prepare_job_rebinds_arbitrary_source_counter_and_preserves_sources(instance, blueprint, tmp_path):
    blueprint["blocks"][1]["state"] = {"Name": "minecraft:magenta_wool"}
    blueprint["ports"][0]["counter"] = "magenta"
    blueprint["benchmark"]["counter"] = "magenta"
    # Another test-wool block is not a declared output sink and must survive.
    blueprint["blocks"].append({"pos": [-1, 0, -1], "state": {"Name": "minecraft:black_wool"}, "role": "test"})
    source = tmp_path / "source.json"
    original = ("\ufeff" + json.dumps(blueprint, separators=(",", ":"))).encode("utf-8")
    source.write_bytes(original)
    before = deepcopy(blueprint)
    job = prepare_job([source, blueprint], "bamboo-ab", instance)
    folder = job_folder(instance)
    assert json.loads((folder / "job.json").read_text(encoding="utf-8")) == job
    assert job["format"] == JOB_FORMAT
    assert job["origin"] is None and job["mode"] == "place" and job["repeats"] == 3
    assert job["conditions"] == blueprint["conditions"]
    assert job["run_ticks"] == 72000
    assert [design["counter"] for design in job["designs"]] == ["lime", "blue"]
    assert [design["offset"] for design in job["designs"]] == [[0, 0, 0], [19, 0, 0]]
    assert source.read_bytes() == original and blueprint == before
    assert (folder / "builds" / "a" / "source.json").read_bytes() == original
    assert json.loads((folder / "builds" / "b" / "source.json").read_text(encoding="utf-8")) == before
    for design in job["designs"]:
        assert design["blueprint_sha256"] == blueprint_hash(blueprint)
        assert design["instantiated_blueprint_sha256"] != design["blueprint_sha256"]
        assert design["ports"] == [{"position": [1, 0, 1], "direction": "east"}]
        assert design["entity_count"] == 0
        assert design["structure"] == f"{design['id']}.nbt"
        structure = folder / design["structure"]
        assert hashlib.sha256(structure.read_bytes()).hexdigest() == design["artifact_sha256"]
        nbt, states = cells(structure)
        assert int(nbt["DataVersion"]) == 4903
        assert list(nbt["size"]) == [3, 5, 4]
        assert states[(2, 0, 1)] == f"minecraft:{design['counter']}_wool"
        assert states[(0, 0, 0)] == "minecraft:black_wool"
        assert len(states) == 60
        build_dir = folder / "builds" / design["id"]
        build = json.loads((build_dir / "build.json").read_text(encoding="utf-8"))
        assert build["blueprint_sha256"] == design["instantiated_blueprint_sha256"]
        assert build["files"]["nbt"]["sha256"] == design["artifact_sha256"]
        for entry in build["files"].values():
            assert not Path(entry["path"]).is_absolute()
            assert (build_dir / entry["path"]).is_file()
            if "sha256" in entry:
                assert hashlib.sha256((build_dir / entry["path"]).read_bytes()).hexdigest() == entry["sha256"]
    assert (instance / "saves" / "do-not-touch.dat").read_bytes() == b"fixture world"
    assert not (instance / "schematics").exists()
    assert [path.name for path in folder.parent.iterdir()] == ["bamboo-ab"]


def test_counter_capacity_is_16_unique_designs(instance, blueprint):
    job = prepare_job([blueprint] * 16, "all-counters", instance)
    colors = [design["counter"] for design in job["designs"]]
    assert colors[:3] == ["lime", "blue", "white"]
    assert len(set(colors)) == 16
    with pytest.raises(ValueError, match="1–16"):
        prepare_job([blueprint] * 17, "too-many", instance)
    assert not job_folder(instance, "too-many").exists()


def test_public_api_defaults_to_legacy_selected_fixture(instance, blueprint, tmp_path, monkeypatch):
    appdata = tmp_path / "fixture-appdata"
    properties = appdata / ".tlauncher/legacy/Minecraft/tl.properties"
    properties.parent.mkdir(parents=True)
    properties.write_text(f"minecraft.gamedir={instance.as_posix()}\n", encoding="utf-8")
    monkeypatch.setenv("APPDATA", str(appdata))
    prepare_job([blueprint], "selected")
    assert (job_folder(instance, "selected") / "job.json").is_file()


@pytest.mark.parametrize("section,key,value", [
    ("conditions", "random_tick_speed", 4),
    ("conditions", "simulation_distance", 8),
    ("conditions", "difficulty", "hard"),
    ("benchmark", "warmup_ticks", 1),
    ("benchmark", "run_ticks", 100),
    ("benchmark", "drain_ticks", 100),
])
def test_conditions_and_all_phase_timings_must_agree(instance, blueprint, section, key, value):
    other = deepcopy(blueprint)
    other[section][key] = value
    with pytest.raises(ValueError, match="same"):
        prepare_job([blueprint, other], "mismatch", instance)
    assert not (instance / "config").exists()


def test_multiple_outputs_are_explicitly_rejected(instance, blueprint):
    blueprint["blocks"].extend([
        {"pos": [0, 1, 1], "state": {"Name": "minecraft:hopper", "Properties": {"facing": "east"}}, "role": "farm"},
        {"pos": [1, 1, 1], "state": {"Name": "minecraft:lime_wool"}, "role": "test"},
    ])
    port = deepcopy(blueprint["ports"][0])
    port.update(name="second", position=[0, 1, 1])
    blueprint["ports"].append(port)
    with pytest.raises(ValueError, match="exactly one output"):
        prepare_job([blueprint], "multi", instance)


@pytest.mark.parametrize("color", ["blue", "lime", "black"])
@pytest.mark.parametrize("enabled", ["true", "false"])
def test_jobs_reject_undeclared_hopper_wool_routes_before_writes(instance, blueprint, monkeypatch, color, enabled):
    blueprint["blocks"].extend([
        {"pos": [-1, 1, 1], "state": {"Name": "minecraft:hopper", "Properties": {"facing": "east", "enabled": enabled}}, "role": "farm"},
        {"pos": [0, 1, 1], "state": {"Name": f"minecraft:{color}_wool"}, "role": "test"},
    ])
    # General blueprints still permit additional geometry; job safety owns this check.
    validate_blueprint(blueprint)
    before = deepcopy(blueprint)

    def unexpected_instance(_):
        raise AssertionError("Undeclared counter routes must fail before resolving an instance")

    monkeypatch.setattr("farmbench.jobs.resolve_instance", unexpected_instance)
    with pytest.raises(ValueError, match=r"undeclared hopper-to-wool output at logical \[-1, 1, 1\]") as error:
        prepare_job([blueprint, load_blueprint(EXAMPLE)], "hidden-route", instance)
    assert f"minecraft:{color}_wool at [0, 1, 1]" in str(error.value)
    assert blueprint == before
    assert not (instance / "config").exists()


def test_jobs_allow_undeclared_hoppers_without_wool_output(instance, blueprint):
    blueprint["blocks"].extend([
        {"pos": [-1, 1, 1], "state": {"Name": "minecraft:hopper", "Properties": {"facing": "east"}}, "role": "farm"},
        {"pos": [0, 1, 1], "state": {"Name": "minecraft:barrel"}, "role": "farm"},
    ])
    job = prepare_job([blueprint], "ordinary-output", instance)
    assert job["designs"][0]["counter"] == "lime"


def test_existing_uses_explicit_base_and_absolute_design_origins(instance, blueprint):
    with pytest.raises(ValueError, match="explicit --origin"):
        prepare_job([blueprint], "missing-origin", instance, mode="existing")
    job = prepare_job([blueprint, blueprint], "existing", instance, mode="existing", origin=[100, 70, -20], at=[[100, 70, -20], [120, 71, 10]], repeats=5)
    assert job["origin"] == [100, 70, -20] and job["mode"] == "existing"
    assert job["repeats"] == 5
    assert [design["offset"] for design in job["designs"]] == [[0, 0, 0], [20, 1, 30]]
    assert (instance / "saves" / "do-not-touch.dat").read_bytes() == b"fixture world"


@pytest.mark.parametrize("kwargs,match", [
    ({"at": [[0, 0, 0], [20, 0, 0]]}, "requires an explicit"),
    ({"origin": [0, 0, 0], "at": [[0, 0, 0]]}, "exactly one --at"),
    ({"origin": [0, 0, 0], "at": [[0, 0, 0], [1, 0, 0]]}, "overlapping"),
    ({"origin": [2**31 - 1, 0, 0]}, "32-bit"),
    ({"origin": [True, 0, 0]}, "32-bit"),
    ({"repeats": 0}, "integer >= 1"),
    ({"repeats": True}, "integer >= 1"),
    ({"mode": "paste"}, "Mode"),
])
def test_invalid_layout_and_options_create_no_job(instance, blueprint, kwargs, match):
    with pytest.raises(ValueError, match=match):
        prepare_job([blueprint, blueprint], "bad", instance, **kwargs)
    assert not (instance / "config").exists()


@pytest.mark.parametrize("job_id", ["../escape", "Bad", "a/b", "con", "nul", "x" * 65])
def test_job_id_cannot_escape_or_use_nonportable_filenames(instance, blueprint, job_id):
    with pytest.raises(ValueError, match="Job id"):
        prepare_job([blueprint], job_id, instance)
    assert not (instance / "config").exists()


@pytest.mark.parametrize("existing", ["empty", "nonempty", "file"])
def test_existing_job_is_never_changed(instance, blueprint, existing):
    folder = job_folder(instance)
    folder.parent.mkdir(parents=True)
    if existing == "file":
        folder.write_bytes(b"user job")
    else:
        folder.mkdir()
        if existing == "nonempty":
            (folder / "job.json").write_bytes(b"user job")
    before = {path.relative_to(instance): path.read_bytes() for path in instance.rglob("*") if path.is_file()}
    with pytest.raises(FileExistsError, match="Job already exists"):
        prepare_job([blueprint], "bamboo-ab", instance)
    after = {path.relative_to(instance): path.read_bytes() for path in instance.rglob("*") if path.is_file()}
    assert after == before
    assert not list(folder.parent.glob(".bamboo-ab-*"))
    if existing == "empty":
        assert folder.is_dir() and list(folder.iterdir()) == []


def test_raced_empty_job_folder_is_preserved_by_exclusive_reservation(instance, blueprint, monkeypatch):
    folder = job_folder(instance)
    original_mkdir = Path.mkdir
    raced = []

    def race_at_reservation(path, *args, **kwargs):
        if path == folder:
            assert not raced
            original_mkdir(path)
            raced.append(path.stat())
        return original_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", race_at_reservation)
    with pytest.raises(FileExistsError, match="Job already exists"):
        prepare_job([blueprint], "bamboo-ab", instance)
    assert len(raced) == 1
    assert folder.is_dir() and list(folder.iterdir()) == []
    assert (folder.stat().st_dev, folder.stat().st_ino) == (raced[0].st_dev, raced[0].st_ino)
    assert [path.name for path in folder.parent.iterdir()] == ["bamboo-ab"]


def test_job_manifest_is_published_only_after_all_artifacts(instance, blueprint, monkeypatch):
    folder = job_folder(instance)
    original_rename = Path.rename
    moved = []

    def observe_publication(path, destination):
        destination = Path(destination)
        if destination.parent == folder:
            assert folder.is_dir()
            assert not (folder / "job.json").exists()
            if destination.name == "job.json":
                assert {entry.name for entry in folder.iterdir()} == {"a.nbt", "b.nbt", "builds"}
                assert (folder / "builds/a/build.json").is_file()
                assert (folder / "builds/b/build.json").is_file()
            moved.append(destination.name)
        return original_rename(path, destination)

    monkeypatch.setattr(Path, "rename", observe_publication)
    prepare_job([blueprint, blueprint], "bamboo-ab", instance)
    assert set(moved) == {"a.nbt", "b.nbt", "builds", "job.json"}
    assert moved[-1] == "job.json"


@pytest.mark.parametrize("failed_entry", ["a.nbt", "builds", "job.json"])
def test_publication_failure_rolls_back_only_its_reservation(instance, blueprint, monkeypatch, failed_entry):
    folder = job_folder(instance)
    unrelated = job_folder(instance, "user-job")
    unrelated.mkdir(parents=True)
    (unrelated / "job.json").write_bytes(b"preserve user job")
    original_rename = Path.rename
    attempted = []

    def fail_publication(path, destination):
        destination = Path(destination)
        if destination.parent == folder:
            attempted.append(destination.name)
            assert not (folder / "job.json").exists()
            if destination.name == failed_entry:
                raise OSError("fixture publication failed")
        return original_rename(path, destination)

    monkeypatch.setattr(Path, "rename", fail_publication)
    with pytest.raises(OSError, match="fixture publication failed"):
        prepare_job([blueprint], "bamboo-ab", instance)
    assert failed_entry in attempted
    assert not folder.exists()
    assert not (folder / "job.json").exists()
    assert (unrelated / "job.json").read_bytes() == b"preserve user job"
    assert [path.name for path in folder.parent.iterdir()] == ["user-job"]


def test_publication_rollback_does_not_delete_a_replaced_reservation(instance, blueprint, monkeypatch):
    folder = job_folder(instance)
    displaced = folder.with_name("displaced-reservation")
    original_rename = Path.rename

    def replace_reservation(path, destination):
        destination = Path(destination)
        if destination.parent == folder:
            original_rename(folder, displaced)
            folder.mkdir()
            (folder / "user-file").write_bytes(b"replacement owned by someone else")
            raise OSError("fixture reservation replaced")
        return original_rename(path, destination)

    monkeypatch.setattr(Path, "rename", replace_reservation)
    with pytest.raises(OSError, match="fixture reservation replaced"):
        prepare_job([blueprint], "bamboo-ab", instance)
    assert (folder / "user-file").read_bytes() == b"replacement owned by someone else"
    assert not (folder / "job.json").exists()
    assert displaced.is_dir() and list(displaced.iterdir()) == []
    assert not list(folder.parent.glob(".bamboo-ab-*"))


@pytest.mark.parametrize("item", ["minecraft:cactus", "minecraft:stone"])
def test_jobs_reject_non_bamboo_protocol_before_resolving_instance(instance, blueprint, monkeypatch, item):
    blueprint["benchmark"]["item"] = item
    assert validate_blueprint(blueprint)["benchmark"]["item"] == item

    def unexpected_instance(_):
        raise AssertionError("Unsupported protocols must fail before resolving an instance")

    monkeypatch.setattr("farmbench.jobs.resolve_instance", unexpected_instance)
    with pytest.raises(ValueError, match="natural-growth bamboo protocol only"):
        prepare_job([blueprint], "unsupported", instance)
    assert not (instance / "config").exists()


def test_compile_failure_cleans_staging_and_does_not_publish(instance, blueprint, monkeypatch):
    import farmbench.jobs as jobs
    original = jobs.compile_blueprint
    calls = []

    def fail_second(data, output, variant):
        calls.append(output)
        if len(calls) == 2:
            raise ValueError("fixture compilation failed")
        return original(data, output, variant)

    monkeypatch.setattr(jobs, "compile_blueprint", fail_second)
    with pytest.raises(ValueError, match="fixture compilation failed"):
        prepare_job([blueprint, blueprint], "bamboo-ab", instance)
    assert not job_folder(instance).exists()
    assert list(job_folder(instance).parent.iterdir()) == []
    assert len(calls) == 2


def test_job_rejects_structure_tampering_before_install(instance, blueprint, monkeypatch):
    import farmbench.jobs as jobs
    original = jobs.compile_blueprint

    def tamper(data, output, variant):
        build = original(data, output, variant)
        Path(build["files"]["nbt"]["path"]).write_bytes(b"tampered structure")
        return build

    monkeypatch.setattr(jobs, "compile_blueprint", tamper)
    with pytest.raises(ValueError, match="structure checksum"):
        prepare_job([blueprint], "tampered", instance)
    assert not job_folder(instance, "tampered").exists()
    assert list(job_folder(instance, "tampered").parent.iterdir()) == []


@pytest.mark.parametrize("use_manifest", [True, False])
def test_jobs_include_passengers_in_entity_count(instance, blueprint, monkeypatch, use_manifest):
    import farmbench.jobs as jobs
    blueprint["entities"] = [{"id": "minecraft:minecart", "pos": [0.5, 1.1, 1.5], "passengers": [
        {"id": "minecraft:armor_stand", "pos": [0.5, 1.3, 1.5], "nbt": "{Invisible:1b}"},
    ]}]
    original = jobs.compile_blueprint

    def compile_without_count(data, output, variant):
        build = original(data, output, variant)
        build.pop("entity_count")
        return build

    if not use_manifest:
        monkeypatch.setattr(jobs, "compile_blueprint", compile_without_count)
    job = prepare_job([blueprint], "entities", instance)
    assert job["designs"][0]["entity_count"] == 2
    root, _ = cells(job_folder(instance, "entities") / "a.nbt")
    assert len(root["entities"]) == 1
    assert len(root["entities"][0]["nbt"]["Passengers"]) == 1


@pytest.mark.parametrize("section,key,value", [
    ("benchmark", "warmup_ticks", 10_000_001),
    ("benchmark", "run_ticks", 10_000_001),
    ("benchmark", "drain_ticks", 0),
    ("benchmark", "drain_ticks", 1_000_001),
    ("conditions", "random_tick_speed", 0),
    ("conditions", "random_tick_speed", 4097),
])
def test_prepare_rejects_values_outside_companion_limits(instance, blueprint, section, key, value):
    blueprint[section][key] = value
    with pytest.raises(ValueError, match="Companion"):
        prepare_job([blueprint], "unsupported", instance)
    assert not (instance / "config").exists()


def test_prepare_rejects_too_many_repeats_or_total_cells_before_compile(instance, blueprint):
    with pytest.raises(ValueError, match="100 repeats"):
        prepare_job([blueprint], "many", instance, repeats=101)
    blueprint["bounds"]["size"] = [101, 101, 100]
    with pytest.raises(ValueError, match="1,000,000 total"):
        prepare_job([blueprint], "huge", instance)
    blueprint["bounds"]["size"] = [513, 5, 4]
    with pytest.raises(ValueError, match="512"):
        prepare_job([blueprint], "long", instance)
    assert not (instance / "config").exists()


def test_companion_detection_uses_metadata_not_filename_and_is_nonfatal(instance):
    assert not companion_installed(instance)
    mods = instance / "mods"
    mods.mkdir()
    (mods / "farmbench-misleading.jar").write_bytes(b"broken archive")
    assert not companion_installed(instance)
    with zipfile.ZipFile(mods / "unusual-name.jar", "w") as archive:
        archive.writestr("fabric.mod.json", json.dumps({"id": "farmbench", "version": "fixture"}))
    assert companion_installed(instance)


@pytest.fixture
def result():
    return {
        "format": RESULT_FORMAT, "status": "completed",
        "job": {"format": JOB_FORMAT, "id": "bamboo-ab", "repeats": 3, "run_ticks": 36000, "designs": [
            {"id": "a", "name": "first", "counter": "lime", "item": "minecraft:bamboo"},
            {"id": "b", "name": "second", "counter": "blue", "item": "minecraft:bamboo"},
        ]},
        "trials": [{"index": index, "measured_ticks": 36000, "designs": [
            {"id": "a", "counter": "lime", "item": "minecraft:bamboo", "items": a, "items_per_hour": a * 2},
            {"id": "b", "counter": "blue", "item": "minecraft:bamboo", "items": b, "items_per_hour": b * 2},
        ]} for index, (a, b) in enumerate(((10, 12), (20, 24), (30, 36)), 1)],
        "origin": [0, 64, 0], "environment": {"source": "runtime"},
        "verification": {"scope": "fixture"}, "calibration": {"measured_ticks": 20},
        "errors": [], "timestamps": {"started": "fixture-start", "finished": "fixture-end"},
    }


def test_summary_has_trial_rates_sample_sd_and_paired_b_minus_a(result, tmp_path):
    before = deepcopy(result)
    path = tmp_path / "result.json"
    path.write_text(json.dumps(result), encoding="utf-8")
    summary = summarize_results(path)
    assert result == before
    assert summary["comparable"] and summary["rate_kind"] == "comparable"
    assert summary["requested_repeats"] == summary["recorded_trials"] == summary["included_trials"] == 3
    assert [row["designs"][0]["items_per_hour"] for row in summary["trials"]] == [20, 40, 60]
    assert summary["designs"][0]["mean_items_per_hour"] == 40
    assert summary["designs"][0]["sample_sd_items_per_hour"] == 20
    assert summary["designs"][1]["mean_items_per_hour"] == 48
    assert summary["designs"][1]["sample_sd_items_per_hour"] == 24
    assert summary["paired_differences"] == [{"a": "a", "b": "b", "direction": "b-a", "item": "minecraft:bamboo", "n": 3, "mean_items_per_hour": 8, "sample_sd_items_per_hour": 4}]
    for key in ("origin", "environment", "verification", "calibration", "errors", "timestamps"):
        assert summary[key] == result[key]
    assert "Small samples" in summary["warnings"][0]
    assert "confidence_interval" not in json.dumps(summary)


@pytest.mark.parametrize("status", ["failed", "cancelled", "incomplete"])
def test_incomplete_jobs_never_become_comparable_from_remaining_good_trials(result, status):
    result["status"] = status
    result["trials"] = result["trials"][:1]
    summary = summarize_results(result)
    assert not summary["comparable"] and summary["rate_kind"] == "diagnostic_only"
    assert summary["included_trials"] == 1
    assert summary["designs"][0]["n"] == 1
    assert summary["designs"][0]["mean_items_per_hour"] == 20
    assert summary["designs"][0]["sample_sd_items_per_hour"] is None
    assert summary["paired_differences"][0]["sample_sd_items_per_hour"] is None
    assert f"status_{status}" in summary["flags"] and "incomplete_repeats" in summary["flags"]


def test_no_trials_means_no_fabricated_zero_samples(result):
    result["status"] = "failed"
    result["errors"] = ["placement failed"]
    result["trials"] = []
    summary = summarize_results(result)
    assert not summary["comparable"]
    assert summary["recorded_trials"] == summary["included_trials"] == 0
    assert summary["designs"][0]["n"] == 0
    assert summary["designs"][0]["mean_items_per_hour"] is None
    assert summary["paired_differences"][0]["mean_items_per_hour"] is None


@pytest.mark.parametrize("ticks", [None, 0, 18000])
def test_off_plan_or_unknown_ticks_are_visible_but_not_pooled(result, ticks):
    trial = result["trials"][0]
    trial["measured_ticks"] = ticks
    for reading in trial["designs"]:
        reading.pop("items_per_hour")
    summary = summarize_results(result)
    assert not summary["comparable"] and summary["included_trials"] == 2
    row = summary["trials"][0]
    assert not row["included_in_statistics"]
    assert row["designs"][0]["items_per_hour"] == (40 if ticks else None)
    assert summary["designs"][0]["mean_items_per_hour"] == 50


def test_missing_design_does_not_make_an_unpaired_trial_into_a_pair(result):
    result["trials"][1]["designs"].pop()
    summary = summarize_results(result)
    assert not summary["comparable"]
    assert summary["paired_differences"][0]["n"] == 2
    assert summary["paired_differences"][0]["mean_items_per_hour"] == 8
    assert "missing_design_readings" in summary["trials"][1]["flags"]


def test_explicit_incomplete_trial_is_excluded_even_with_planned_tick_count(result):
    result["trials"][0]["completed"] = False
    summary = summarize_results(result)
    assert summary["included_trials"] == 2 and not summary["comparable"]
    assert "incomplete_trial" in summary["trials"][0]["flags"]


def test_runner_diagnostic_flag_and_trial_errors_are_respected(result):
    result["comparable"] = False
    result["trials"][0]["errors"] = ["incomplete drainage"]
    summary = summarize_results(result)
    assert not summary["comparable"] and summary["included_trials"] == 2
    assert "runner_marked_noncomparable" in summary["flags"]
    assert "trial_errors" in summary["trials"][0]["flags"]


def test_raw_trial_metadata_is_preserved_in_summary(result):
    result["run_id"] = "fixture-run"
    result["started_at"] = "fixture-start"
    result["finished_at"] = "fixture-end"
    result["trials"][0]["phase_ticks"] = {"warmup": 1200, "drain": 200}
    result["trials"][0]["designs"][0]["raw_counter"] = {"minecraft:bamboo": 10}
    summary = summarize_results(result)
    assert summary["trials"][0]["phase_ticks"] == result["trials"][0]["phase_ticks"]
    assert summary["trials"][0]["designs"][0]["raw_counter"] == {"minecraft:bamboo": 10}
    assert summary["job"] == result["job"]
    assert summary["run_id"] == "fixture-run"
    assert summary["started_at"] == "fixture-start" and summary["finished_at"] == "fixture-end"


def test_root_incomplete_and_unpaired_trial_flags_are_honored(result):
    result["incomplete"] = True
    result["trials"][0]["paired"] = False
    summary = summarize_results(result)
    assert not summary["comparable"] and summary["included_trials"] == 2
    assert "runner_marked_incomplete" in summary["flags"]
    assert "unpaired_trial" in summary["trials"][0]["flags"]


def test_result_reading_identity_must_match_embedded_job(result):
    result["job"]["designs"][0]["blueprint_sha256"] = "a" * 64
    result["trials"][0]["designs"][0]["blueprint_sha256"] = "b" * 64
    with pytest.raises(ValueError, match="blueprint_sha256 differs"):
        summarize_results(result)


def test_unlike_target_items_are_not_subtracted(result):
    result["job"]["designs"][1]["item"] = "minecraft:cactus"
    for trial in result["trials"]:
        trial["designs"][1]["item"] = "minecraft:cactus"
    summary = summarize_results(result)
    assert summary["designs"][1]["mean_items_per_hour"] == 48
    assert summary["paired_differences"] == []
    assert any("target items differ" in warning for warning in summary["warnings"])


@pytest.mark.parametrize("field,value,match", [
    ("items", -1, "integer"), ("items", True, "integer"),
    ("items_per_hour", 999, "differs"), ("items_per_hour", "20", "finite"),
    ("counter", "red", "counter or item"), ("item", "minecraft:stone", "counter or item"),
    ("id", "other", "unknown or duplicate"),
])
def test_invalid_or_inconsistent_readings_are_rejected(result, field, value, match):
    result["trials"][0]["designs"][0][field] = value
    with pytest.raises(ValueError, match=match):
        summarize_results(result)


def test_duplicate_trial_indices_are_rejected(result):
    result["trials"][1]["index"] = result["trials"][0]["index"]
    with pytest.raises(ValueError, match="Duplicate trial"):
        summarize_results(result)


@pytest.mark.parametrize("index", [0, 4, 999])
def test_off_plan_trial_indices_are_visible_but_excluded_from_statistics(result, index):
    result["completed"] = True
    result["trials"][2]["index"] = index
    summary = summarize_results(result)
    assert not summary["comparable"] and summary["rate_kind"] == "diagnostic_only"
    assert summary["recorded_trials"] == 3 and summary["included_trials"] == 2
    assert "trial_indices_differ_from_job" in summary["flags"]
    assert "incomplete_repeats" in summary["flags"]
    assert summary["designs"][0]["n"] == 2
    assert summary["designs"][0]["mean_items_per_hour"] == 30
    assert summary["paired_differences"][0]["n"] == 2
    assert summary["paired_differences"][0]["mean_items_per_hour"] == 6
    row = summary["trials"][2]
    assert row["index"] == index and row["designs"][0]["items_per_hour"] == 60
    assert not row["included_in_statistics"] and "trial_index_outside_job" in row["flags"]


def test_missing_planned_trial_index_is_flagged(result):
    result["trials"].pop(1)
    summary = summarize_results(result)
    assert not summary["comparable"] and summary["included_trials"] == 2
    assert "trial_indices_differ_from_job" in summary["flags"]
    assert [row["index"] for row in summary["trials"]] == [1, 3]


def test_extra_off_plan_trial_does_not_contaminate_complete_planned_samples(result):
    extra = deepcopy(result["trials"][0])
    extra["index"] = 999
    result["trials"].append(extra)
    summary = summarize_results(result)
    assert not summary["comparable"] and summary["included_trials"] == 3
    assert "trial_indices_differ_from_job" in summary["flags"]
    assert summary["designs"][0]["n"] == 3 and summary["designs"][0]["mean_items_per_hour"] == 40
    assert not summary["trials"][3]["included_in_statistics"]


def test_all_planned_indices_may_be_supplied_out_of_order(result):
    result["trials"].reverse()
    summary = summarize_results(result)
    assert summary["comparable"] and summary["included_trials"] == 3
    assert "trial_indices_differ_from_job" not in summary["flags"]
    assert summary["designs"][0]["mean_items_per_hour"] == 40


@pytest.mark.parametrize("payload", ['{"format": "x", "format": "y"}', '{"format": NaN}', '{"format": 1e999}'])
def test_result_json_is_strict(tmp_path, payload):
    path = tmp_path / "bad.json"
    path.write_text(payload, encoding="utf-8")
    with pytest.raises(ValueError, match="JSON"):
        summarize_results(path)
