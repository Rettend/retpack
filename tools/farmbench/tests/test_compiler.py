"""Independent NBT checks, not just an encoder/decoder agreeing with itself."""

from copy import deepcopy
import base64
import gzip
import hashlib
import io
from importlib import resources
import json
from pathlib import Path
import shutil
import subprocess

import mcblueprint
import nbtlib
import pytest

from farmbench.blueprint import COUNTER_COLORS, blueprint_hash, load_blueprint, validate_blueprint
from farmbench.compiler import _read_nbt, compile_blueprint


EXAMPLE = Path(__file__).parents[1] / "examples" / "bamboo_micro_v1.json"


def _nbt(path: str | Path) -> nbtlib.File:
    return _read_nbt(Path(path).read_bytes())


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


@pytest.fixture
def populated_blueprint(blueprint):
    data = deepcopy(blueprint)
    data["blocks"][0]["nbt"] = '''{
        TransferCooldown:4, Items:[{Slot:0b,id:"minecraft:bamboo",count:7,
        components:{"minecraft:custom_data":{sample:12s,labels:[farm,lab]}}}],
        custom_data:{bytes:[B;1b,-2b],ints:[I;1,-2,3],longs:[L;4L,-5L]},xy:9
    }'''
    data["blocks"].append({"pos": [-4, -1, -6], "state": {"Name": "minecraft:chest"},
                           "role": "test", "nbt": '{Items:[{Slot:3b,id:"minecraft:diamond",count:2}],CustomName:"Lab chest"}'})
    data["entities"] = [
        {"id": "minecraft:hopper_minecart", "pos": [-3.5, -0.75, -5.25],
         "rotation": [45.125, -12.5], "nbt": '{Enabled:1b,TransferCooldown:8,Tags:[farm,collection],Items:[{Slot:2b,id:"minecraft:bamboo",count:13,components:{"minecraft:custom_data":{batch:17L}}}],custom_data:{ratio:0.125f}}'},
        {"id": "minecraft:oak_boat", "pos": [-2.25, 0.25, -4.5], "rotation": [180, 0],
         "nbt": '{Invulnerable:1b,CustomName:"Boat"}', "passengers": [
             {"id": "minecraft:pig", "pos": [-2.25, 0.5, -4.5], "rotation": [90, 0],
              "nbt": "{Age:-200,Health:20f,Tags:[rider]}", "passengers": [
                  {"id": "minecraft:chicken", "pos": [-2.25, 1.25, -4.5], "nbt": "{EggLayTime:6000}"},
              ]},
         ]},
        {"id": "minecraft:arrow", "pos": [-1.125, 0.25, -3.875], "rotation": [15, 30],
         "nbt": '{Motion:[0.0d,0.0d,0.0d],NoGravity:1b,damage:2.0d,Tags:[projectile]}'},
        {"id": "minecraft:item_frame", "pos": [-3.875, -1.75, -5.5],
         "nbt": '{block_pos:[I;-3,-2,-6],Facing:3b,Item:{id:"minecraft:map",count:1},Fixed:1b}'},
        {"id": "minecraft:armor_stand", "pos": [-0.5, 0.5, -3.5],
         "nbt": '{NoGravity:1b,Pose:{Head:[10.0f,20.0f,30.0f]},Tags:[decoration]}'},
        {"id": "minecraft:iron_golem", "pos": [-1.5, 0.5, -5.5], "role": "test", "passengers": [
            {"id": "minecraft:pig", "pos": [-1.5, 1.25, -5.5], "nbt": "{NoAI:1b}"},
        ]},
    ]
    return validate_blueprint(data)


def _entity_map(entities):
    result = {}
    for tag in entities:
        result.setdefault(str(tag["id"]), []).append(tag)
        children = _entity_map(tag.get("Passengers", []))
        for name, tags in children.items():
            result.setdefault(name, []).extend(tags)
    return result


def _assert_typed_equal(actual, expected):
    """Independent recursive comparison, including tags, arrays and list types."""
    assert actual.tag_id == expected.tag_id
    if isinstance(expected, nbtlib.Compound):
        assert actual.keys() == expected.keys()
        for key in expected:
            _assert_typed_equal(actual[key], expected[key])
    elif isinstance(expected, nbtlib.List):
        assert actual.subtype is expected.subtype
        assert len(actual) == len(expected)
        for left, right in zip(actual, expected, strict=True):
            _assert_typed_equal(left, right)
    elif isinstance(expected, (nbtlib.ByteArray, nbtlib.IntArray, nbtlib.LongArray)):
        assert list(actual) == list(expected)
    else:
        assert actual == expected


def test_entities_have_distinct_native_formats_and_independently_verified_payloads(tmp_path, populated_blueprint):
    original = deepcopy(populated_blueprint)
    build = compile_blueprint(populated_blueprint, tmp_path / "populated")
    assert populated_blueprint == original
    lite = _nbt(build["files"]["litematic"]["path"])["Regions"]["main"]["Entities"]
    vanilla = _nbt(build["files"]["nbt"]["path"])["entities"]
    assert lite.subtype is vanilla.subtype is nbtlib.Compound
    assert len(lite) == len(vanilla) == build["entity_root_count"] == 6
    assert build["entity_count"] == 9  # Includes the three nested passengers.
    by_id = _entity_map(lite)
    assert sum(map(len, by_id.values())) == 9
    source_roots = {entity["id"]: entity for entity in populated_blueprint["entities"]}
    for raw, wrapped in zip(lite, vanilla, strict=True):
        assert "id" in raw and "Pos" in raw and "Rotation" in raw
        assert not {"pos", "blockPos", "nbt"} & raw.keys()
        assert wrapped.keys() == {"pos", "blockPos", "nbt"}
        assert wrapped["pos"].subtype is nbtlib.Double
        assert wrapped["blockPos"].subtype is nbtlib.Int
        _assert_typed_equal(wrapped["nbt"], raw)
        _assert_typed_equal(wrapped["pos"], raw["Pos"])
        source = source_roots[str(raw["id"])]
        assert list(raw["Pos"]) == [part + delta for part, delta in zip(source["pos"], [4, 2, 6])]
        assert raw["Pos"].subtype is nbtlib.Double
        assert raw["Rotation"].subtype is nbtlib.Float
        assert list(raw["Rotation"]) == source["rotation"]
        assert "UUID" not in raw
    boat = by_id["minecraft:oak_boat"][0]
    pig = boat["Passengers"][0]
    chicken = pig["Passengers"][0]
    assert boat["Passengers"].subtype is pig["Passengers"].subtype is nbtlib.Compound
    assert list(pig["Pos"]) == [1.75, 2.5, 1.5]
    assert list(chicken["Pos"]) == [1.75, 3.25, 1.5]
    assert pig["Pos"].subtype is chicken["Pos"].subtype is nbtlib.Double
    assert pig["Rotation"].subtype is chicken["Rotation"].subtype is nbtlib.Float
    assert isinstance(pig["Age"], nbtlib.Int) and isinstance(pig["Health"], nbtlib.Float)
    assert list(pig["Tags"]) == ["rider"]
    cart = by_id["minecraft:hopper_minecart"][0]
    item = cart["Items"][0]
    assert isinstance(item, nbtlib.Compound) and isinstance(item["Slot"], nbtlib.Byte)
    assert str(item["id"]) == "minecraft:bamboo" and int(item["count"]) == 13
    assert isinstance(item["components"]["minecraft:custom_data"]["batch"], nbtlib.Long)
    assert isinstance(cart["custom_data"]["ratio"], nbtlib.Float)
    assert list(cart["Tags"]) == ["farm", "collection"]
    frame = by_id["minecraft:item_frame"][0]
    assert isinstance(frame["block_pos"], nbtlib.IntArray)
    assert list(frame["block_pos"]) == [1, 0, 0]
    frame_wrapper = next(entry for entry in vanilla if entry["nbt"]["id"] == "minecraft:item_frame")
    assert list(frame_wrapper["blockPos"]) == [1, 0, 0]  # Hanging anchor, not floor(Pos).
    assert list(frame_wrapper["pos"]) == [0.125, 0.25, 0.5]
    for key, value in build["verification"].items():
        assert value is True


def test_block_entity_nbt_and_tile_coordinates_translate_in_both_formats(tmp_path, populated_blueprint):
    build = compile_blueprint(populated_blueprint, tmp_path / "tiles")
    lite = _nbt(build["files"]["litematic"]["path"])["Regions"]["main"]["TileEntities"]
    vanilla = _nbt(build["files"]["nbt"]["path"])
    assert build["block_entity_count"] == len(lite) == 2
    tiles = {tuple(int(tag[axis]) for axis in "xyz"): tag for tag in lite}
    vanilla_tiles = {tuple(int(part) for part in entry["pos"]): entry["nbt"]
                     for entry in vanilla["blocks"] if "nbt" in entry}
    assert tiles.keys() == vanilla_tiles.keys() == {(1, 1, 1), (0, 1, 0)}
    for position, tag in tiles.items():
        assert all(isinstance(tag[axis], nbtlib.Int) for axis in "xyz")
        assert list(tag[axis] for axis in "xyz") == list(position)
        _assert_typed_equal(vanilla_tiles[position], tag)
    hopper = tiles[(1, 1, 1)]
    assert str(hopper["id"]) == "minecraft:hopper"
    item = hopper["Items"][0]
    assert isinstance(item["Slot"], nbtlib.Byte)
    assert str(item["id"]) == "minecraft:bamboo" and int(item["count"]) == 7
    assert isinstance(item["components"]["minecraft:custom_data"]["sample"], nbtlib.Short)
    assert hopper["custom_data"]["bytes"].tag_id == nbtlib.ByteArray.tag_id
    assert hopper["custom_data"]["ints"].tag_id == nbtlib.IntArray.tag_id
    assert hopper["custom_data"]["longs"].tag_id == nbtlib.LongArray.tag_id
    assert int(hopper["xy"]) == 9  # Only exact x/y/z coordinate keys may be stripped by a decoder.
    for block in populated_blueprint["blocks"]:
        if "nbt" not in block:
            continue
        position = tuple(part + delta for part, delta in zip(block["pos"], [4, 2, 6]))
        expected = nbtlib.parse_nbt(block["nbt"])
        expected.update({axis: nbtlib.Int(part) for axis, part in zip("xyz", position)})
        _assert_typed_equal(tiles[position], expected)


def test_entity_materials_are_separate_and_projectiles_have_no_fake_placement_items(tmp_path, populated_blueprint):
    build = compile_blueprint(populated_blueprint, tmp_path / "materials")
    materials = json.loads(Path(build["files"]["materials"]["path"]).read_text("utf-8"))
    assert materials["total_blocks"] == build["block_count"] == 8
    entities = materials["entities"]
    assert entities["total_entities"] == 9
    assert entities["counts"]["minecraft:pig"] == 2
    assert entities["placement_items"]["minecraft:hopper_minecart"] == 1
    assert entities["placement_items"]["minecraft:oak_boat"] == 1
    assert entities["placement_items"]["minecraft:armor_stand"] == 1
    assert entities["placement_items"]["minecraft:pig_spawn_egg"] == 2
    arrow = next(entry for entry in entities["types"] if entry["id"] == "minecraft:arrow")
    assert arrow == {"id": "minecraft:arrow", "count": 1, "placement_item": None, "no_item": True}
    assert "minecraft:arrow" not in entities["placement_items"]
    assert "minecraft:hopper_minecart" not in materials["counts"]


def test_survival_omits_entire_test_passenger_trees_and_test_tiles(tmp_path, populated_blueprint):
    build = compile_blueprint(populated_blueprint, tmp_path / "survival", "survival")
    root = _nbt(build["files"]["litematic"]["path"])["Regions"]["main"]
    assert build["entity_count"] == 7
    assert build["entity_root_count"] == 5
    assert build["block_entity_count"] == len(root["TileEntities"]) == 1
    entities = _entity_map(root["Entities"])
    assert "minecraft:iron_golem" not in entities
    assert len(entities["minecraft:pig"]) == 1
    assert root["TileEntities"][0]["id"] == "minecraft:hopper"
    vanilla = _nbt(build["files"]["nbt"]["path"])
    assert len(vanilla["entities"]) == 5
    assert sum("nbt" in block for block in vanilla["blocks"]) == 1


def test_populated_exports_are_deterministic_and_native_same_format_roundtrips_preserve_nbt(tmp_path, populated_blueprint):
    first = compile_blueprint(populated_blueprint, tmp_path / "first")
    second = compile_blueprint(populated_blueprint, tmp_path / "second")
    for key in ("blueprint", "litematic", "nbt", "materials", "placement"):
        assert first["files"][key]["sha256"] == second["files"][key]["sha256"]
    for key, fmt in (("litematic", "litematic"), ("nbt", "vanilla")):
        original = _nbt(first["files"][key]["path"])
        model = mcblueprint.load(first["files"][key]["path"])
        decoded = nbtlib.File.from_fileobj(io.BytesIO(gzip.decompress(mcblueprint.save_bytes(model, fmt))))
        if fmt == "litematic":
            _assert_typed_equal(decoded["Regions"]["main"]["Entities"], original["Regions"]["main"]["Entities"])
            _assert_typed_equal(decoded["Regions"]["main"]["TileEntities"], original["Regions"]["main"]["TileEntities"])
        else:
            _assert_typed_equal(decoded["entities"], original["entities"])
            before = {tuple(entry["pos"]): entry["nbt"] for entry in original["blocks"] if "nbt" in entry}
            after = {tuple(entry["pos"]): entry["nbt"] for entry in decoded["blocks"] if "nbt" in entry}
            assert before.keys() == after.keys()
            for pos in before:
                _assert_typed_equal(after[pos], before[pos])


@pytest.mark.parametrize("fmt", ["litematic", "vanilla"])
@pytest.mark.parametrize("corruption", ["entity_id", "entity_pos", "rotation_type", "inventory", "passenger", "tile_inventory", "tile_coordinate", "tile_type", "drop_entity", "drop_tile"])
def test_independent_verification_rejects_corrupted_entity_and_tile_nbt_before_writing(tmp_path, populated_blueprint, monkeypatch, fmt, corruption):
    real_save = mcblueprint.save_bytes

    def corrupt_save(model, actual_format):
        encoded = real_save(model, actual_format)
        if actual_format != fmt:
            return encoded
        root = nbtlib.File.from_fileobj(io.BytesIO(gzip.decompress(encoded)))
        if fmt == "litematic":
            region = root["Regions"]["main"]
            entities = region["Entities"]
            raw = list(entities)
            tiles = region["TileEntities"]
        else:
            entities = root["entities"]
            raw = [entry["nbt"] for entry in entities]
            tiles = [entry["nbt"] for entry in root["blocks"] if "nbt" in entry]
        cart = next(tag for tag in raw if tag["id"] == "minecraft:hopper_minecart")
        boat = next(tag for tag in raw if tag["id"] == "minecraft:oak_boat")
        hopper = next(tag for tag in tiles if tag["id"] == "minecraft:hopper")
        if corruption == "entity_id":
            cart["id"] = nbtlib.String("minecraft:minecart")
        elif corruption == "entity_pos":
            cart["Pos"][0] = nbtlib.Double(0.75)
        elif corruption == "rotation_type":
            cart["Rotation"] = nbtlib.List[nbtlib.Double]([float(part) for part in cart["Rotation"]])
        elif corruption == "inventory":
            cart["Items"][0]["count"] = nbtlib.Byte(13)  # Equal value, wrong tag type.
        elif corruption == "passenger":
            boat["Passengers"][0]["Passengers"][0]["EggLayTime"] = nbtlib.Int(1)
        elif corruption == "tile_inventory":
            hopper["Items"][0]["id"] = nbtlib.String("minecraft:stone")
        elif corruption == "tile_coordinate":
            hopper["x"] = nbtlib.Int(2)
        elif corruption == "tile_type":
            hopper["id"] = nbtlib.String("minecraft:chest")
        elif corruption == "drop_entity":
            entities.pop()
        elif fmt == "litematic":
            region["TileEntities"].pop()
        else:
            next(entry for entry in root["blocks"] if "nbt" in entry).pop("nbt")
        buffer = io.BytesIO()
        root.write(buffer)
        return gzip.compress(buffer.getvalue(), mtime=0)

    monkeypatch.setattr(mcblueprint, "save_bytes", corrupt_save)
    output = tmp_path / "broken"
    with pytest.raises(ValueError, match="Export verification failed"):
        compile_blueprint(populated_blueprint, output)
    assert not output.exists()


def test_every_pinned_entity_type_can_be_exported_to_both_native_formats(tmp_path, blueprint):
    registry = json.loads(resources.files("farmbench").joinpath("data", "vanilla_26_2.json").read_text("utf-8"))
    blueprint["entities"] = [{"id": f"minecraft:{name}", "pos": [-3.5, -1.25, -5.75]}
                             for name in registry["entity_types"].split()]
    build = compile_blueprint(blueprint, tmp_path / "all_types")
    assert build["entity_count"] == build["entity_root_count"] == 158
    lite = _nbt(build["files"]["litematic"]["path"])["Regions"]["main"]["Entities"]
    vanilla = _nbt(build["files"]["nbt"]["path"])["entities"]
    assert {str(tag["id"]) for tag in lite} == {str(tag["nbt"]["id"]) for tag in vanilla} == {
        f"minecraft:{name}" for name in registry["entity_types"].split()
    }
    assert all(list(tag["Pos"]) == [0.5, 0.75, 0.25] for tag in lite)
    assert all(list(tag["blockPos"]) == [0, 0, 0] for tag in vanilla)
    materials = json.loads(Path(build["files"]["materials"]["path"]).read_text("utf-8"))["entities"]
    assert materials["total_entities"] == 158
    by_type = {entry["id"]: entry for entry in materials["types"]}
    assert by_type["minecraft:marker"]["no_item"] is True
    assert by_type["minecraft:wind_charge"]["no_item"] is True
    assert by_type["minecraft:acacia_chest_boat"]["placement_item"] == "minecraft:acacia_chest_boat"
    assert by_type["minecraft:sulfur_cube"]["placement_item"] == "minecraft:sulfur_cube_spawn_egg"


@pytest.mark.parametrize("anchor", [
    "TileX:-3,TileY:-1,TileZ:-5",
    "block_pos:[I;-3,-1,-5]",
    "block_pos:[-3,-1,-5]",
    "block_pos:{x:-3,y:-1,z:-5}",
])
def test_hanging_anchor_inputs_normalize_to_native_26_2_int_arrays(tmp_path, blueprint, anchor):
    blueprint["entities"] = [{"id": "minecraft:painting", "pos": [-3.75, -1.75, -5.75],
                              "nbt": "{" + anchor + ",variant:'minecraft:kebab'}"}]
    build = compile_blueprint(blueprint, tmp_path / "anchor")
    lite = _nbt(build["files"]["litematic"]["path"])["Regions"]["main"]["Entities"][0]
    wrapped = _nbt(build["files"]["nbt"]["path"])["entities"][0]
    assert list(wrapped["blockPos"]) == [1, 1, 1]
    assert list(wrapped["pos"]) == [0.25, 0.25, 0.25]
    assert not {"TileX", "TileY", "TileZ"} & lite.keys()
    assert isinstance(lite["block_pos"], nbtlib.IntArray)
    assert list(lite["block_pos"]) == [1, 1, 1]
    assert isinstance(wrapped["nbt"]["block_pos"], nbtlib.IntArray)
    _assert_typed_equal(wrapped["nbt"], lite)


def test_deep_passenger_positions_and_payloads_survive_both_exports(tmp_path, blueprint):
    entity = {"id": "minecraft:marker", "pos": [-3.75, -1.75, -5.5], "nbt": "{data:{depth:0}}"}
    blueprint["entities"] = [entity]
    for depth in range(1, 33):
        entity["passengers"] = [{"id": "minecraft:marker", "pos": [-3.75, -1.75, -5.5],
                                  "nbt": f"{{data:{{depth:{depth}}}}}"}]
        entity = entity["passengers"][0]
    build = compile_blueprint(blueprint, tmp_path / "deep_passengers")
    assert build["entity_count"] == 33 and build["entity_root_count"] == 1
    raw = _nbt(build["files"]["litematic"]["path"])["Regions"]["main"]["Entities"][0]
    wrapped = _nbt(build["files"]["nbt"]["path"])["entities"][0]
    _assert_typed_equal(raw, wrapped["nbt"])
    for depth in range(33):
        assert raw["Pos"].subtype is nbtlib.Double
        assert list(raw["Pos"]) == [0.25, 0.25, 0.5]
        assert raw["Rotation"].subtype is nbtlib.Float
        assert int(raw["data"]["depth"]) == depth
        assert "UUID" not in raw
        if depth < 32:
            assert raw["Passengers"].subtype is nbtlib.Compound
            raw = raw["Passengers"][0]


def test_all_bed_colors_export_as_blocks_without_removed_bed_entities(tmp_path, blueprint):
    blueprint["bounds"]["size"] = [16, 4, 4]
    for index, color in enumerate(COUNTER_COLORS):
        for part, z in (("foot", -6), ("head", -5)):
            blueprint["blocks"].append({
                "pos": [-4 + index, -2, z],
                "state": {"Name": f"minecraft:{color}_bed", "Properties": {"facing": "south", "part": part}},
            })
    # The original clearance-corner stone occupies the first bed's foot cell.
    blueprint["blocks"] = [block for block in blueprint["blocks"]
                           if block["pos"] != [-4, -2, -6] or block["state"]["Name"].endswith("_bed")]
    build = compile_blueprint(blueprint, tmp_path / "beds")
    lite, cells = _decode_litematic(build["files"]["litematic"]["path"])
    vanilla, vanilla_cells = _decode_vanilla(build["files"]["nbt"]["path"])
    assert cells == vanilla_cells
    assert build["block_entity_count"] == 0
    assert len(lite["Regions"]["main"]["TileEntities"]) == 0
    assert not any("nbt" in entry for entry in vanilla["blocks"])
    for index, color in enumerate(COUNTER_COLORS):
        for part, z in (("foot", 0), ("head", 1)):
            assert cells[(index, 0, z)]["Name"] == f"minecraft:{color}_bed"
            assert cells[(index, 0, z)]["Properties"]["part"] == part


def test_arbitrary_entity_world_anchors_remain_opaque_in_both_exports(tmp_path, blueprint):
    payload = nbtlib.parse_nbt('''{
        HomePos:{X:-100,Y:64,Z:200},
        Brain:{memories:{"minecraft:home":{value:{dimension:"minecraft:overworld",pos:[I;-100,64,200]}}}},
        custom_data:{anchor:[-3.5d,-1.25d,-5.75d]}
    }''')
    blueprint["entities"] = [{"id": "minecraft:villager", "pos": [-3.5, -1.25, -5.75],
                              "nbt": nbtlib.serialize_tag(payload)}]
    build = compile_blueprint(blueprint, tmp_path / "opaque_anchors")
    raw = _nbt(build["files"]["litematic"]["path"])["Regions"]["main"]["Entities"][0]
    wrapped = _nbt(build["files"]["nbt"]["path"])["entities"][0]
    assert list(raw["Pos"]) == [0.5, 0.75, 0.25]
    assert list(wrapped["pos"]) == [0.5, 0.75, 0.25]
    for key, value in payload.items():
        _assert_typed_equal(raw[key], value)
        _assert_typed_equal(wrapped["nbt"][key], value)


@pytest.fixture
def java_nbt_strings(tmp_path):
    java = shutil.which("java")
    if java is None:
        pytest.skip("Java is needed for the independent DataInputStream.readUTF fixture")
    source = tmp_path / "ReadNbtStrings.java"
    source.write_text('''
import java.io.*;
import java.nio.charset.StandardCharsets;
import java.util.*;
import java.util.zip.GZIPInputStream;
class ReadNbtStrings {
    static final List<String> strings = new ArrayList<>();
    static String string(DataInputStream in) throws IOException {
        String value = in.readUTF(); strings.add(value); return value;
    }
    static void array(DataInputStream in, int width) throws IOException {
        int count = in.readInt();
        if (count < 0) throw new IOException("negative array length");
        in.skipNBytes((long) count * width);
    }
    static void payload(DataInputStream in, int tag) throws IOException {
        switch (tag) {
            case 1 -> in.skipNBytes(1);
            case 2 -> in.skipNBytes(2);
            case 3, 5 -> in.skipNBytes(4);
            case 4, 6 -> in.skipNBytes(8);
            case 7 -> array(in, 1);
            case 8 -> string(in);
            case 9 -> {
                int subtype = in.readUnsignedByte(), count = in.readInt();
                if (count < 0 || (subtype == 0 && count != 0)) throw new IOException("invalid list");
                for (int i = 0; i < count; i++) payload(in, subtype);
            }
            case 10 -> {
                int child;
                while ((child = in.readUnsignedByte()) != 0) { string(in); payload(in, child); }
            }
            case 11 -> array(in, 4);
            case 12 -> array(in, 8);
            default -> throw new IOException("invalid tag " + tag);
        }
    }
    public static void main(String[] args) throws Exception {
        for (String path : args) {
            try (var in = new DataInputStream(new GZIPInputStream(new FileInputStream(path)))) {
                if (in.readUnsignedByte() != 10) throw new IOException("invalid root");
                string(in); payload(in, 10);
                if (in.read() != -1) throw new IOException("trailing data");
            }
        }
        for (String value : strings)
            System.out.println(Base64.getEncoder().encodeToString(value.getBytes(StandardCharsets.UTF_8)));
    }
}
''', encoding="utf-8")

    def run(paths):
        return subprocess.run([java, str(source), *(str(path) for path in paths)],
                              capture_output=True, text=True, timeout=60)

    return run


def test_java_read_utf_reads_every_string_in_both_artifacts_without_python_codec_agreement(tmp_path, blueprint, java_nbt_strings):
    value = "ASCII\x00é漢😀𐐷"
    payload = nbtlib.Compound({"data": nbtlib.Compound({"😀\x00key": nbtlib.String(value)})})
    blueprint["entities"] = [{"id": "minecraft:marker", "pos": [-3.5, -1.25, -5.75],
                              "nbt": nbtlib.serialize_tag(payload)}]
    blueprint["blocks"][0]["nbt"] = nbtlib.serialize_tag(nbtlib.Compound({"custom_data": payload}))
    build = compile_blueprint(blueprint, tmp_path / "unicode")
    paths = [build["files"][key]["path"] for key in ("litematic", "nbt")]
    result = java_nbt_strings(paths)
    assert result.returncode == 0, result.stderr
    strings = [base64.b64decode(line).decode("utf-8") for line in result.stdout.splitlines()]
    assert strings.count(value) == 4  # Entity and block entity in each native format.
    assert strings.count("😀\x00key") == 4
    for path in paths:
        binary = gzip.decompress(Path(path).read_bytes())
        assert b"\xed\xa0\xbd\xed\xb8\x80" in binary  # Java's six-byte surrogate-pair encoding.
        assert b"\xc0\x80" in binary  # Java's two-byte NUL encoding.
        assert b"\xf0\x9f\x98\x80" not in binary
        root = _nbt(path)
        raw = root["entities"][0]["nbt"] if "entities" in root else root["Regions"]["main"]["Entities"][0]
        assert raw["data"]["😀\x00key"] == value
    repeated = compile_blueprint(blueprint, tmp_path / "unicode_again")
    for key in ("litematic", "nbt"):
        assert repeated["files"][key]["sha256"] == build["files"][key]["sha256"]


def test_java_fixture_rejects_the_original_four_byte_emoji_regression(tmp_path, java_nbt_strings):
    root = nbtlib.File({"data": nbtlib.Compound({"text": nbtlib.String("😀")})})
    buffer = io.BytesIO()
    root.write(buffer)  # Deliberately use nbtlib's incompatible ordinary UTF-8.
    assert b"\x00\x04\xf0\x9f\x98\x80" in buffer.getvalue()
    broken = tmp_path / "ordinary_utf8.nbt"
    broken.write_bytes(gzip.compress(buffer.getvalue(), mtime=0))
    result = java_nbt_strings([broken])
    assert result.returncode != 0
    assert "UTFDataFormatException" in result.stderr
    with pytest.raises(ValueError, match="modified UTF-8"):
        _read_nbt(broken.read_bytes())
