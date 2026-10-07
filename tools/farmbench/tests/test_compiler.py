"""Independent NBT checks, not just an encoder/decoder agreeing with itself."""

from copy import deepcopy
import gzip
import hashlib
import io
import json
from pathlib import Path

import mcblueprint
import nbtlib
import pytest

from farmbench.blueprint import blueprint_hash, load_blueprint, validate_blueprint
from farmbench.compiler import compile_blueprint


EXAMPLE = Path(__file__).parents[1] / "examples" / "bamboo_micro_v1.json"


def _nbt(path: str | Path) -> nbtlib.File:
    return nbtlib.File.from_fileobj(io.BytesIO(gzip.decompress(Path(path).read_bytes())))


def _state(entry: dict) -> dict:
    result = {"Name": str(entry["Name"])}
    if entry.get("Properties"):
        result["Properties"] = {str(key): str(value) for key, value in entry["Properties"].items()}
    return result


def _decode_litematic(path: str | Path) -> tuple[nbtlib.File, dict]:
    """Decode using a little-endian integer bitstream, no mcblueprint helpers."""
    root = _nbt(path)
    region = root["Regions"]["main"]
    sx, sy, sz = (int(region["Size"][axis]) for axis in "xyz")
    palette = [_state(entry) for entry in region["BlockStatePalette"]]
    bits = max(2, (len(palette) - 1).bit_length())
    words = region["BlockStates"]
    assert len(words) == (sx * sy * sz * bits + 63) // 64
    stream = int.from_bytes(b"".join(int(word).to_bytes(8, "little", signed=True) for word in words), "little")
    cells = {}
    for index in range(sx * sy * sz):
        position = (index % sx, index // (sx * sz), (index // sx) % sz)
        palette_index = (stream >> (index * bits)) & ((1 << bits) - 1)
        assert palette_index < len(palette)
        cells[position] = palette[palette_index]
    return root, cells


def _decode_vanilla(path: str | Path) -> tuple[nbtlib.File, dict]:
    root = _nbt(path)
    palette = [_state(entry) for entry in root["palette"]]
    cells = {}
    for entry in root["blocks"]:
        position = tuple(int(value) for value in entry["pos"])
        assert position not in cells
        cells[position] = palette[int(entry["state"])]
    return root, cells


@pytest.fixture
def blueprint() -> dict:
    return validate_blueprint({
        "format": "retpack-farm-blueprint-v1",
        "name": "negative_origin",
        "minecraft": "26.2",
        "data_version": 4903,
        "bounds": {"min": [-4, -2, -6], "size": [5, 4, 4]},
        "blocks": [
            {"pos": [-3, -1, -5], "state": {"Name": "minecraft:hopper", "Properties": {"enabled": "true", "facing": "east"}}, "role": "farm"},
            {"pos": [-2, -1, -5], "state": {"Name": "minecraft:lime_wool"}, "role": "test"},
            {"pos": [-3, 0, -5], "state": {"Name": "minecraft:observer", "Properties": {"facing": "south", "powered": "false"}}, "role": "farm"},
            {"pos": [-3, 1, -5], "state": {"Name": "minecraft:piston", "Properties": {"facing": "west", "extended": "false"}}, "role": "farm"},
            {"pos": [-4, -2, -6], "state": {"Name": "minecraft:stone"}, "role": "farm"},
            {"pos": [0, 1, -3], "state": {"Name": "minecraft:stone"}, "role": "farm"},
            {"pos": [-1, -1, -5], "state": {"Name": "minecraft:gold_block"}, "role": "test"},
        ],
        "ports": [{
            "name": "items_out",
            "kind": "hopper",
            "position": [-3, -1, -5],
            "direction": "east",
            "counter": "lime",
            "survival_output": {"Name": "minecraft:barrel", "Properties": {"facing": "up", "open": "false"}},
        }],
        "conditions": {"random_tick_speed": 3, "simulation_distance": 12, "difficulty": "normal", "dimension": "minecraft:overworld"},
        "benchmark": {"counter": "lime", "item": "minecraft:bamboo", "warmup_ticks": 1200, "run_ticks": 72000, "drain_ticks": 200},
        "notes": ["Keep the player nearby and the plant lit."],
    })


def test_full_bounds_translation_versions_and_block_states(tmp_path: Path, blueprint: dict) -> None:
    original = deepcopy(blueprint)
    build = compile_blueprint(blueprint, tmp_path / "lab")
    assert blueprint == original
    assert build["translation"] == {"logical_origin": [-4, -2, -6], "exported_origin": [0, 0, 0], "offset": [4, 2, 6]}
    assert build["dimensions"] == [5, 4, 4]
    assert build["bounds"] == blueprint["bounds"]
    assert build["block_count"] == 7
    root, cells = _decode_litematic(build["artifact"]["path"])
    assert isinstance(root["MinecraftDataVersion"], nbtlib.Int)
    assert int(root["MinecraftDataVersion"]) == 4903
    assert "DataVersion" not in root  # Litematica reads MinecraftDataVersion at the root.
    assert int(root["Version"]) == 6
    assert int(root["SubVersion"]) == 1
    assert dict(root["Regions"]["main"]["Position"]) == {"x": 0, "y": 0, "z": 0}
    assert int(root["Metadata"]["TotalVolume"]) == 80
    assert int(root["Metadata"]["TotalBlocks"]) == 7
    assert len(cells) == 80
    assert cells[(0, 0, 0)] == {"Name": "minecraft:stone"}
    assert cells[(4, 3, 3)] == {"Name": "minecraft:stone"}
    assert cells[(4, 0, 0)] == {"Name": "minecraft:air"}
    for block in blueprint["blocks"]:
        exported = tuple(block["pos"][axis] + build["translation"]["offset"][axis] for axis in range(3))
        assert cells[exported] == block["state"]
    vanilla, vanilla_cells = _decode_vanilla(build["files"]["nbt"]["path"])
    assert isinstance(vanilla["DataVersion"], nbtlib.Int)
    assert int(vanilla["DataVersion"]) == 4903
    assert "MinecraftDataVersion" not in vanilla
    assert list(vanilla["size"]) == [5, 4, 4]
    assert len(vanilla["blocks"]) == 80  # Air clearance is explicit, not dropped by the sparse writer.
    assert vanilla_cells == cells
    for key in ("litematic", "nbt"):
        loaded = mcblueprint.load(build["files"][key]["path"])
        assert loaded.region().size == (5, 4, 4)
        assert loaded.region().position == (0, 0, 0)
        for position, state in cells.items():
            actual = loaded.get_block(*position)
            assert actual.name == state["Name"]
            assert dict(actual.properties) == state.get("Properties", {})


def test_survival_replaces_only_sink_and_removes_other_test_blocks(tmp_path: Path, blueprint: dict) -> None:
    lab = compile_blueprint(blueprint, tmp_path / "lab", "lab")
    survival = compile_blueprint(blueprint, tmp_path / "survival", "survival")
    _, lab_cells = _decode_litematic(lab["artifact"]["path"])
    _, survival_cells = _decode_litematic(survival["artifact"]["path"])
    assert survival["translation"] == lab["translation"]
    assert lab_cells[(2, 1, 1)] == {"Name": "minecraft:lime_wool"}
    assert survival_cells[(2, 1, 1)] == {"Name": "minecraft:barrel", "Properties": {"facing": "up", "open": "false"}}
    assert lab_cells[(3, 1, 1)] == {"Name": "minecraft:gold_block"}
    assert survival_cells[(3, 1, 1)] == {"Name": "minecraft:air"}
    assert {position for position in lab_cells if lab_cells[position] != survival_cells[position]} == {(2, 1, 1), (3, 1, 1)}
    assert survival["block_count"] == 6
    _, vanilla_cells = _decode_vanilla(survival["files"]["nbt"]["path"])
    assert vanilla_cells == survival_cells


def test_explicit_air_states_are_preserved_but_not_materials(tmp_path: Path, blueprint: dict) -> None:
    data = deepcopy(blueprint)
    for index, name in enumerate(("air", "cave_air", "void_air")):
        data["blocks"].append({"pos": [-4 + index, -2, -5], "state": {"Name": f"minecraft:{name}"}, "role": "farm"})
    data = validate_blueprint(data)
    build = compile_blueprint(data, tmp_path / "air")
    root, cells = _decode_litematic(build["artifact"]["path"])
    assert build["block_count"] == 7
    assert int(root["Metadata"]["TotalBlocks"]) == 7
    for index, name in enumerate(("air", "cave_air", "void_air")):
        assert cells[(index, 0, 1)] == {"Name": f"minecraft:{name}"}
    assert _decode_vanilla(build["files"]["nbt"]["path"])[1] == cells
    materials = json.loads(Path(build["files"]["materials"]["path"]).read_text(encoding="utf-8"))
    assert materials["total_blocks"] == 7
    assert not {"minecraft:air", "minecraft:cave_air", "minecraft:void_air"} & materials["counts"].keys()


@pytest.mark.parametrize("variant", ["lab", "survival"])
def test_example_has_nineteen_blocks(tmp_path: Path, variant: str) -> None:
    blueprint = load_blueprint(EXAMPLE)
    build = compile_blueprint(blueprint, tmp_path / variant, variant)
    assert build["block_count"] == 19
    root, cells = _decode_litematic(build["artifact"]["path"])
    assert int(root["Metadata"]["TotalBlocks"]) == 19
    assert sum(state["Name"] != "minecraft:air" for state in cells.values()) == 19
    materials = json.loads(Path(build["files"]["materials"]["path"]).read_text(encoding="utf-8"))
    assert materials["total_blocks"] == 19
    assert sum(materials["counts"].values()) == 19
    if variant == "lab":
        assert materials["counts"]["minecraft:lime_wool"] == 1
        assert "minecraft:barrel" not in materials["counts"]
    else:
        assert materials["counts"]["minecraft:barrel"] == 1
        assert "minecraft:lime_wool" not in materials["counts"]


def test_manifest_checksums_materials_and_placement(tmp_path: Path, blueprint: dict) -> None:
    build = compile_blueprint(blueprint, tmp_path / "new" / "lab")
    assert build["format"] == "retpack-farm-build-v1"
    assert build["minecraft"] == "26.2"
    assert build["data_version"] == 4903
    assert build["variant"] == "lab"
    assert build["blueprint_sha256"] == blueprint_hash(blueprint)
    assert build["artifact"] == {**build["files"]["litematic"], "format": "litematic"}
    assert json.loads(Path(build["files"]["build"]["path"]).read_text(encoding="utf-8")) == build
    assert set(path.name for path in (tmp_path / "new" / "lab").iterdir()) == {
        "negative_origin-lab.litematic", "negative_origin-lab.nbt", "materials.json", "placement.md", "build.json", "blueprint.json",
    }
    for key, file in build["files"].items():
        path = Path(file["path"])
        assert path.is_absolute() and path.is_file()
        if key != "build":
            assert hashlib.sha256(path.read_bytes()).hexdigest() == file["sha256"]
    materials = json.loads(Path(build["files"]["materials"]["path"]).read_text(encoding="utf-8"))
    assert materials["kind"] == "block-counts"
    assert materials["counts"]["minecraft:stone"] == 2
    assert "minecraft:air" not in materials["counts"]
    assert sum(materials["counts"].values()) == build["block_count"]
    assert sum(entry["count"] for entry in materials["states"]) == build["block_count"]
    assert "recipe" in materials["note"]
    assert not any("iron" in key or "cost" in key for key in materials)
    placement = Path(build["files"]["placement"]["path"]).read_text(encoding="utf-8")
    for text in ("Load Schematics", "Schematic Placements", "executeOperation", "unbound", "stick", "only air", "entire bounding box", "bottom layer", "all layers", "[-4, -2, -6]", "[4, 2, 6]"):
        assert text in placement


def test_repeated_builds_have_identical_artifact_hashes(tmp_path: Path, blueprint: dict) -> None:
    first = compile_blueprint(blueprint, tmp_path / "first")
    second = compile_blueprint(blueprint, tmp_path / "second")
    for key in ("litematic", "nbt", "materials", "placement"):
        assert first["files"][key]["sha256"] == second["files"][key]["sha256"]


def test_resolved_source_is_saved_for_reproducing_a_build(tmp_path: Path, blueprint: dict) -> None:
    build = compile_blueprint(blueprint, tmp_path / "lab")
    saved = json.loads(Path(build["files"]["blueprint"]["path"]).read_text(encoding="utf-8"))
    assert blueprint_hash(saved) == build["blueprint_sha256"]


def test_oversized_clearance_is_rejected_before_creating_output(tmp_path: Path, blueprint: dict) -> None:
    blueprint["bounds"]["size"] = [1000, 1000, 1000]
    with pytest.raises(ValueError, match="1,000,000"):
        compile_blueprint(blueprint, tmp_path / "huge")
    assert not (tmp_path / "huge").exists()


@pytest.mark.parametrize("existing", ["empty_directory", "nonempty_directory", "file"])
def test_refuses_any_existing_output_without_changing_it(tmp_path: Path, blueprint: dict, existing: str) -> None:
    output = tmp_path / "output"
    if existing == "file":
        output.write_bytes(b"preserve")
        sentinel = output
    else:
        output.mkdir()
        sentinel = output / "keep.txt"
        if existing == "nonempty_directory":
            sentinel.write_bytes(b"preserve")
    with pytest.raises(FileExistsError, match="fresh directory"):
        compile_blueprint(blueprint, output)
    if existing != "empty_directory":
        assert sentinel.read_bytes() == b"preserve"
    else:
        assert list(output.iterdir()) == []


def test_invalid_variant_does_not_create_output(tmp_path: Path, blueprint: dict) -> None:
    output = tmp_path / "invalid"
    with pytest.raises(ValueError, match="variant"):
        compile_blueprint(blueprint, output, "creative")
    assert not output.exists()


def test_unsafe_name_cannot_escape_output(tmp_path: Path, blueprint: dict) -> None:
    blueprint["name"] = "../escape"
    with pytest.raises(ValueError, match="name:"):
        compile_blueprint(blueprint, tmp_path / "output")
    assert not (tmp_path / "output").exists()


@pytest.mark.parametrize("distinct_states", [3, 5, 17, 33])
def test_packed_states_cross_long_boundaries(tmp_path: Path, blueprint: dict, distinct_states: int) -> None:
    """3, 5 and 6 bits exercise split indices and signed Java longs."""
    data = deepcopy(blueprint)
    data["bounds"] = {"min": [-8, -3, -9], "size": [8, 3, 7]}
    # Keep a real hopper port, and give every other occupied cell varied legal
    # note-block states. Air adds another palette entry beyond these states.
    data["blocks"] = [
        {"pos": [-8, -3, -9], "state": {"Name": "minecraft:hopper", "Properties": {"facing": "east", "enabled": "true"}}, "role": "farm"},
        {"pos": [-7, -3, -9], "state": {"Name": "minecraft:lime_wool"}, "role": "test"},
    ]
    data["ports"][0]["position"] = [-8, -3, -9]
    for index in range(2, 8 * 3 * 7 - 1):
        state_id = (index - 2) % (distinct_states - 2)
        position = [index % 8 - 8, index // (8 * 7) - 3, (index // 8) % 7 - 9]
        data["blocks"].append({
            "pos": position,
            "state": {"Name": "minecraft:note_block", "Properties": {"instrument": "harp", "note": str(state_id % 25), "powered": "true" if state_id >= 25 else "false"}},
            "role": "farm",
        })
    data = validate_blueprint(data)
    build = compile_blueprint(data, tmp_path / "packed")
    root, cells = _decode_litematic(build["artifact"]["path"])
    palette_size = len(root["Regions"]["main"]["BlockStatePalette"])
    assert palette_size == distinct_states + 1
    bits = max(2, (palette_size - 1).bit_length())
    if bits > 2:
        assert any((index * bits) % 64 + bits > 64 for index in range(len(cells)))
        assert any(int(word) < 0 for word in root["Regions"]["main"]["BlockStates"])
    for block in data["blocks"]:
        position = tuple(block["pos"][axis] - data["bounds"]["min"][axis] for axis in range(3))
        assert cells[position] == block["state"]
    assert cells[(7, 2, 6)] == {"Name": "minecraft:air"}
    assert _decode_vanilla(build["files"]["nbt"]["path"])[1] == cells


@pytest.mark.parametrize("corruption", ["version", "state", "dimensions"])
def test_export_verification_catches_library_regression_before_writing(tmp_path: Path, blueprint: dict, monkeypatch: pytest.MonkeyPatch, corruption: str) -> None:
    real_save = mcblueprint.save_bytes

    def broken_save(model: mcblueprint.Blueprint, fmt: str) -> bytes:
        encoded = real_save(model, fmt)
        if fmt != "litematic":
            return encoded
        root = nbtlib.File.from_fileobj(io.BytesIO(gzip.decompress(encoded)))
        if corruption == "version":
            root["MinecraftDataVersion"] = nbtlib.Int(3465)
        elif corruption == "state":
            root["Regions"]["main"]["BlockStatePalette"][1]["Name"] = nbtlib.String("minecraft:dirt")
        else:
            root["Regions"]["main"]["Size"]["x"] = nbtlib.Int(6)
        buffer = io.BytesIO()
        root.write(buffer)
        return gzip.compress(buffer.getvalue(), mtime=0)

    monkeypatch.setattr(mcblueprint, "save_bytes", broken_save)
    output = tmp_path / "broken"
    with pytest.raises(ValueError, match="Export verification failed"):
        compile_blueprint(blueprint, output)
    assert not output.exists()


@pytest.mark.parametrize("corruption", ["version", "state", "dimensions"])
def test_vanilla_verification_catches_regression(tmp_path: Path, blueprint: dict, monkeypatch: pytest.MonkeyPatch, corruption: str) -> None:
    real_save = mcblueprint.save_bytes

    def broken_save(model: mcblueprint.Blueprint, fmt: str) -> bytes:
        encoded = real_save(model, fmt)
        if fmt != "vanilla":
            return encoded
        root = nbtlib.File.from_fileobj(io.BytesIO(gzip.decompress(encoded)))
        if corruption == "version":
            root["DataVersion"] = nbtlib.Int(3465)
        elif corruption == "state":
            root["palette"][0]["Name"] = nbtlib.String("minecraft:dirt")
        else:
            root["size"][0] = nbtlib.Int(6)
        buffer = io.BytesIO()
        root.write(buffer)
        return gzip.compress(buffer.getvalue(), mtime=0)

    monkeypatch.setattr(mcblueprint, "save_bytes", broken_save)
    output = tmp_path / "broken_vanilla"
    with pytest.raises(ValueError, match="Export verification failed"):
        compile_blueprint(blueprint, output)
    assert not output.exists()
