from copy import deepcopy
import hashlib
from importlib import resources
import json
from pathlib import Path

import pytest

from farmbench.blueprint import (
    BlueprintError,
    DATA_VERSION,
    blueprint_hash,
    load_blueprint,
    validate_blueprint,
)


EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "bamboo_micro_v1.json"


@pytest.fixture
def blueprint():
    return json.loads(EXAMPLE.read_text(encoding="utf-8"))


def test_example_preserves_negative_logical_coordinates_and_bottom_hopper():
    result = load_blueprint(EXAMPLE)
    assert result["bounds"] == {"min": [-1, 0, -1], "size": [3, 5, 4]}
    assert result["data_version"] == 4903
    assert len(result["blocks"]) == 19
    blocks = {tuple(block["pos"]): block for block in result["blocks"]}
    assert blocks[(-1, 2, 0)]["state"] == {"Name": "minecraft:glass"}
    assert blocks[(0, 2, -1)]["state"] == {"Name": "minecraft:glass"}
    assert blocks[(0, 0, 0)]["state"]["Name"] == "minecraft:hopper"
    assert blocks[(1, 0, 0)]["role"] == "test"
    assert result["ports"][0]["survival_output"]["Name"] == "minecraft:barrel"


def test_defaults_are_resolved_without_mutating_input(blueprint):
    del blueprint["data_version"]
    del blueprint["conditions"]
    del blueprint["notes"]
    for key in ("warmup_ticks", "run_ticks", "drain_ticks"):
        del blueprint["benchmark"][key]
    del blueprint["blocks"][0]["role"]
    del blueprint["blocks"][0]["state"]["Properties"]["enabled"]
    blueprint["blocks"][5]["state"] = {"Name": "minecraft:observer"}
    blueprint["ports"][0]["survival_output"] = {"Name": "minecraft:barrel"}
    original = deepcopy(blueprint)
    result = validate_blueprint(blueprint)
    assert blueprint == original
    assert result["data_version"] == DATA_VERSION
    assert result["conditions"] == {
        "random_tick_speed": 3, "simulation_distance": 12,
        "difficulty": "normal", "dimension": "minecraft:overworld",
    }
    assert result["benchmark"] == {
        "counter": "lime", "item": "minecraft:bamboo",
        "warmup_ticks": 1200, "run_ticks": 72000, "drain_ticks": 200,
    }
    assert result["notes"] == []
    assert result["blocks"][0]["role"] == "farm"
    assert result["blocks"][0]["state"]["Properties"]["enabled"] == "true"
    # An observer's actual vanilla default differs from the example's facing.
    assert result["blocks"][5]["state"]["Properties"] == {"facing": "south", "powered": "false"}
    assert result["ports"][0]["survival_output"]["Properties"] == {"facing": "north", "open": "false"}
    result["blocks"][0]["pos"][0] = 50
    assert blueprint == original


def set_field(data, path, value):
    target = data
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value


@pytest.mark.parametrize(("path", "value", "message"), [
    (("blocks", 2, "state", "Name"), "minecraft:made_up_block", "unknown vanilla"),
    (("blocks", 2, "state", "Name"), "modded:mud", "unknown vanilla"),
    (("blocks", 2, "state", "Name"), "mud", "unknown vanilla"),
    (("blocks", 2, "state", "Properties"), {"facing": "east"}, "unknown field"),
    (("blocks", 4, "state", "Properties", "facing"), "sideways", "Properties.facing"),
    (("blocks", 4, "state", "Properties", "extended"), False, "string"),
    (("blocks", 7, "state", "Properties", "power"), "16", "Properties.power"),
    (("blocks", 7, "state", "Properties", "power"), 0, "string"),
    (("blocks", 3, "state", "Properties", "leaves"), "huge", "Properties.leaves"),
    (("blocks", 3, "state", "Properties", "age"), "2", "Properties.age"),
    (("blocks", 3, "state", "Properties"), None, "JSON object"),
    (("blocks", 3, "state", "NBT"), {}, "unknown field"),
    (("blocks", 3, "role"), "decoration", "role"),
    (("blocks", 8, "pos"), [-2, 2, 0], "outside bounds"),
    (("blocks", 8, "pos"), [2, 2, 0], "outside bounds"),
    (("blocks", 8, "pos"), [-1, 5, 0], "outside bounds"),
    (("blocks", 8, "pos"), [-1, 2, 3], "outside bounds"),
    (("blocks", 8, "pos"), [-1, -1, 0], "outside bounds"),
    (("bounds", "size"), [3, 0, 4], "integer"),
    (("bounds", "min"), [0, 0, 0], "outside bounds"),
    (("bounds", "min"), [-(2**31) - 1, 0, 0], "integer"),
    (("bounds", "min"), [2**31 - 1, 0, 0], "bounds maximum"),
    (("bounds", "size"), [3, 5], "three-integer"),
    (("blocks", 0, "pos"), [0, 0, "0"], "integer"),
    (("blocks", 0, "pos"), [0, 0, 0.0], "integer"),
    (("minecraft",), "1.21.8", "minecraft"),
    (("data_version",), 4440, "requires 4903"),
    (("name",), "../farm", "lowercase"),
    (("conditions", "difficulty"), "expert", "difficulty"),
    (("conditions", "dimension"), "minecraft:moon", "dimension"),
    (("conditions", "simulation_distance"), 33, "integer"),
    (("conditions", "random_tick_speed"), -1, "integer"),
    (("benchmark", "run_ticks"), 0, "integer"),
    (("benchmark", "warmup_ticks"), -1, "integer"),
    (("benchmark", "drain_ticks"), 1.5, "integer"),
    (("benchmark", "run_ticks"), 2**31, "integer"),
    (("benchmark", "counter"), "green", "no output port"),
    (("benchmark", "item"), "minecraft:fake_item", "unknown or empty"),
    (("benchmark", "item"), "minecraft:redstone_wire", "unknown or empty"),
    (("benchmark", "item"), "minecraft:air", "unknown or empty"),
    (("notes",), "Keep nearby", "array of strings"),
    (("notes", 0), 5, "string"),
    (("notes", 0), "\ud800", "invalid Unicode"),
    (("conditions", "randomTickSpeed"), 3, "unknown field"),
    (("benchmark", "commands"), [], "unknown field"),
    (("typo",), True, "unknown field"),
])
def test_invalid_fields_are_rejected(blueprint, path, value, message):
    set_field(blueprint, path, value)
    with pytest.raises(BlueprintError, match=message):
        validate_blueprint(blueprint)


@pytest.mark.parametrize("path", [
    ("data_version",), ("bounds", "min", 0), ("bounds", "size", 0),
    ("blocks", 0, "pos", 0), ("ports", 0, "position", 0),
    ("conditions", "random_tick_speed"), ("conditions", "simulation_distance"),
    ("benchmark", "warmup_ticks"), ("benchmark", "run_ticks"), ("benchmark", "drain_ticks"),
])
@pytest.mark.parametrize("value", [True, False])
def test_booleans_are_not_integers(blueprint, path, value):
    set_field(blueprint, path, value)
    with pytest.raises(BlueprintError, match="integer"):
        validate_blueprint(blueprint)


@pytest.mark.parametrize("path", [
    ("bounds", "min"), ("bounds", "size"), ("blocks", 0, "state"),
    ("ports", 0, "counter"), ("ports", 0, "survival_output"), ("benchmark", "item"),
])
def test_required_semantic_fields_cannot_be_omitted(blueprint, path):
    target = blueprint
    for key in path[:-1]:
        target = target[key]
    del target[path[-1]]
    with pytest.raises(BlueprintError, match="missing required field"):
        validate_blueprint(blueprint)


def test_duplicate_blocks_rejected_even_if_the_state_matches(blueprint):
    blueprint["blocks"].append(deepcopy(blueprint["blocks"][8]))
    with pytest.raises(BlueprintError, match="duplicate block position"):
        validate_blueprint(blueprint)


def test_missing_bottom_output_hopper_is_rejected(blueprint):
    del blueprint["blocks"][0]
    with pytest.raises(BlueprintError, match="output hopper is missing"):
        validate_blueprint(blueprint)


@pytest.mark.parametrize(("path", "value", "message"), [
    (("blocks", 0, "state"), {"Name": "minecraft:stone"}, "output hopper is missing"),
    (("blocks", 0, "role"), "test", "must have role 'farm'"),
    (("ports", 0, "direction"), "west", "does not match"),
    (("ports", 0, "direction"), "up", "direction"),
    (("blocks", 0, "state", "Properties", "enabled"), "false", "must be enabled"),
    (("ports", 0, "position"), [5, 0, 0], "outside bounds"),
    (("ports", 0, "counter"), "gold", "counter"),
    (("ports", 0, "kind"), "chest", "kind"),
    (("blocks", 1, "state", "Name"), "minecraft:green_wool", "expected lime counter wool"),
    (("blocks", 1, "state", "Name"), "minecraft:lime_carpet", "expected lime counter wool"),
    (("blocks", 1, "role"), "farm", "must have role 'test'"),
    (("blocks", 1, "pos"), [1, 0, 1], "expected lime counter wool"),
    (("ports", 0, "survival_output"), {"Name": "minecraft:stone"}, "hopper-compatible"),
    (("ports", 0, "survival_output"), {"Name": "minecraft:ender_chest"}, "hopper-compatible"),
    (("ports", 0, "survival_output"), {"Name": "minecraft:chest", "Properties": {"type": "left"}}, "must be 'single'"),
    (("ports", 0, "survival_output", "Properties", "facing"), "sideways", "Properties.facing"),
])
def test_output_ports_are_checked_against_actual_geometry(blueprint, path, value, message):
    set_field(blueprint, path, value)
    with pytest.raises(BlueprintError, match=message):
        validate_blueprint(blueprint)


def test_missing_adjacent_counter_wool_is_rejected(blueprint):
    del blueprint["blocks"][1]
    with pytest.raises(BlueprintError, match="expected lime counter wool"):
        validate_blueprint(blueprint)


def test_counter_wool_must_be_inside_explicit_bounds(blueprint):
    # Retain only the port hopper so the error is about the target, not a block.
    blueprint["blocks"] = blueprint["blocks"][:1]
    blueprint["bounds"] = {"min": [0, 0, 0], "size": [1, 1, 1]}
    with pytest.raises(BlueprintError, match="counter wool: position .* outside bounds"):
        validate_blueprint(blueprint)


@pytest.mark.parametrize("same_name", [True, False])
def test_duplicate_ports_are_rejected(blueprint, same_name):
    port = deepcopy(blueprint["ports"][0])
    if not same_name:
        port["name"] = "second_output"
    blueprint["ports"].append(port)
    with pytest.raises(BlueprintError, match="duplicate port name" if same_name else "already used"):
        validate_blueprint(blueprint)


def test_ports_cannot_share_a_sink_replacement(blueprint):
    blueprint["blocks"].append({
        "pos": [1, 0, -1], "state": {"Name": "minecraft:hopper", "Properties": {"facing": "south"}},
    })
    port = deepcopy(blueprint["ports"][0])
    port.update(name="second_output", position=[1, 0, -1], direction="south")
    blueprint["ports"].append(port)
    with pytest.raises(BlueprintError, match="counter wool is already used"):
        validate_blueprint(blueprint)


def test_downward_port_and_all_negative_origin_are_valid(blueprint):
    blueprint["bounds"] = {"min": [-5, -6, -7], "size": [1, 2, 1]}
    blueprint["blocks"] = [
        {"pos": [-5, -5, -7], "state": {"Name": "minecraft:hopper"}},
        {"pos": [-5, -6, -7], "state": {"Name": "minecraft:lime_wool"}, "role": "test"},
    ]
    blueprint["ports"][0].update(position=[-5, -5, -7], direction="down")
    result = validate_blueprint(blueprint)
    assert result["blocks"][0]["state"]["Properties"]["facing"] == "down"
    assert result["bounds"]["min"] == [-5, -6, -7]


@pytest.mark.parametrize("container", ["barrel", "chest", "trapped_chest", "lime_shulker_box", "hopper", "dropper", "waxed_oxidized_copper_chest"])
def test_survival_containers_have_valid_resolved_states(blueprint, container):
    blueprint["ports"][0]["survival_output"] = {"Name": f"minecraft:{container}"}
    result = validate_blueprint(blueprint)
    assert result["ports"][0]["survival_output"]["Properties"]


def test_registry_is_complete_and_all_defaults_are_legal():
    registry = json.loads(resources.files("farmbench").joinpath("data", "vanilla_26_2.json").read_text("utf-8"))
    names = []
    for group, properties, defaults in registry["block_groups"]:
        names.extend(group.split())
        assert properties.keys() == defaults.keys()
        for key, default in defaults.items():
            assert isinstance(default, str)
            assert default in properties[key]
    assert len(names) == len(set(names)) == registry["block_count"] == 1196
    without_items = set(registry["blocks_without_items"].split())
    extra_items = set(registry["extra_items"].split())
    assert without_items <= set(names)
    assert not extra_items & set(names)
    assert len((set(names) - without_items) | extra_items) == registry["item_count"] == 1537
    assert registry["source"]["commit"] == "711a353b47d84e6cb592a1b72f682e5f44759284"


def test_registry_supports_blocks_outside_the_bamboo_example(blueprint):
    blueprint["blocks"][2]["state"] = {
        "Name": "minecraft:oak_stairs", "Properties": {"facing": "west", "shape": "outer_left"},
    }
    blueprint["benchmark"]["item"] = "minecraft:iron_ingot"
    result = validate_blueprint(blueprint)
    assert result["blocks"][2]["state"]["Properties"] == {
        "facing": "west", "half": "bottom", "shape": "outer_left", "waterlogged": "false",
    }


def test_hash_is_canonical_default_resolved_and_semantic(blueprint):
    original = deepcopy(blueprint)
    first = blueprint_hash(blueprint)
    assert len(first) == 64
    assert all(char in "0123456789abcdef" for char in first)
    assert blueprint == original
    equivalent = json.loads(json.dumps(blueprint, sort_keys=True))
    equivalent["blocks"].reverse()
    del equivalent["data_version"]
    del equivalent["blocks"][-1]["state"]["Properties"]["enabled"]
    equivalent["blocks"][0]["state"]["Properties"] = {}
    assert blueprint_hash(equivalent) == first
    canonical = validate_blueprint(blueprint)
    canonical["blocks"].sort(key=lambda block: tuple(block["pos"]))
    canonical["ports"].sort(key=lambda port: port["name"])
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    assert first == hashlib.sha256(encoded).hexdigest()
    blueprint["benchmark"]["run_ticks"] += 1
    assert blueprint_hash(blueprint) != first


def test_invalid_blueprint_does_not_get_a_hash(blueprint):
    blueprint["blocks"].append(deepcopy(blueprint["blocks"][0]))
    with pytest.raises(BlueprintError, match="duplicate"):
        blueprint_hash(blueprint)


def test_v0_is_not_silently_accepted(blueprint):
    blueprint["format"] = "retpack-farm-blueprint-v0"
    with pytest.raises(BlueprintError, match="deliberately migrate to v1"):
        validate_blueprint(blueprint)


@pytest.mark.parametrize("value", [None, [], True, 42, "farm", {"format": "retpack-farm-blueprint-v1"}])
def test_invalid_top_level_objects_raise_blueprint_error(value):
    with pytest.raises(BlueprintError):
        validate_blueprint(value)


@pytest.mark.parametrize("text", [
    '{"format": "one", "format": "two"}',
    '{"state": {"Name": "minecraft:mud", "Name": "minecraft:stone"}}',
    '{"notes": [NaN]}', '{"notes": [Infinity]}', '{"notes": [-Infinity]}',
    '{"format": ',
])
def test_strict_json_loading_rejects_ambiguous_or_non_json_input(tmp_path, text):
    path = tmp_path / "bad.json"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(BlueprintError):
        load_blueprint(path)


def test_load_accepts_windows_utf8_bom(tmp_path, blueprint):
    path = tmp_path / "farm.json"
    path.write_text(json.dumps(blueprint), encoding="utf-8-sig")
    assert load_blueprint(path) == validate_blueprint(blueprint)


def test_extremely_long_json_integer_is_a_blueprint_error(tmp_path):
    path = tmp_path / "long_integer.json"
    path.write_text('{"data_version": ' + "9" * 5000 + "}", encoding="utf-8")
    with pytest.raises(BlueprintError):
        load_blueprint(path)


def test_file_errors_are_blueprint_errors(tmp_path):
    with pytest.raises(BlueprintError, match="Cannot read blueprint"):
        load_blueprint(tmp_path / "missing.json")
    bad = tmp_path / "bad_utf8.json"
    bad.write_bytes(b"\xff")
    with pytest.raises(BlueprintError, match="Cannot read blueprint"):
        load_blueprint(bad)
