from copy import deepcopy
import hashlib
from importlib import resources
import json
from pathlib import Path

import nbtlib
import pytest

from farmbench.blueprint import (
    BlueprintError,
    COUNTER_COLORS,
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


def test_absent_extensions_preserve_existing_v1_hash(blueprint):
    result = validate_blueprint(blueprint)
    assert "entities" not in result
    assert all("nbt" not in block for block in result["blocks"])
    assert blueprint_hash(blueprint) == "953bae37866f32a12e953981358fbb5a69b498aa9aab4bb3a8f3c7c55cddc715"


def test_all_pinned_entity_ids_are_accepted_without_a_type_whitelist(blueprint):
    registry = json.loads(resources.files("farmbench").joinpath("data", "vanilla_26_2.json").read_text("utf-8"))
    names = registry["entity_types"].split()
    assert len(names) == len(set(names)) == registry["entity_count"] == 158
    assert len(set(registry["block_entity_types"].split())) == registry["block_entity_count"] == 49
    assert {"sulfur_cube", "mannequin", "zombie_nautilus", "player", "fishing_bobber"} <= set(names)
    blueprint["entities"] = [{"id": f"minecraft:{name}", "pos": [-0.5, 1.25, 0.5]} for name in names]
    result = validate_blueprint(blueprint)
    assert len(result["entities"]) == 158
    assert {entity["id"] for entity in result["entities"]} == {f"minecraft:{name}" for name in names}
    assert all(entity["rotation"] == [0.0, 0.0] and entity["role"] == "farm" for entity in result["entities"])


def test_entity_defaults_typed_snbt_and_passengers_are_not_mutating(blueprint):
    blueprint["entities"] = [{
        "id": "minecraft:oak_boat", "pos": [-0.75, 1.25, 0.625], "role": "test",
        "rotation": [90, -12.5],
        "nbt": '{Invulnerable:1b,CustomName:"Lab boat",data:{bytes:[B;1b,-2b],longs:[L;4L],weight:1.25f}}',
        "passengers": [{"id": "minecraft:pig", "pos": [-0.5, 1.5, 0.5], "passengers": [
            {"id": "minecraft:chicken", "pos": [-0.5, 2.0, 0.5], "nbt": "{Age:-100,Tags:[rider]}"},
        ]}],
    }]
    original = deepcopy(blueprint)
    result = validate_blueprint(blueprint)
    assert blueprint == original
    entity = result["entities"][0]
    assert entity["rotation"] == [90.0, -12.5]
    assert entity["passengers"][0]["role"] == "test"
    assert entity["passengers"][0]["passengers"][0]["role"] == "test"
    tag = nbtlib.parse_nbt(entity["nbt"])
    assert isinstance(tag, nbtlib.Compound)
    assert isinstance(tag["Invulnerable"], nbtlib.Byte)
    assert isinstance(tag["data"]["bytes"], nbtlib.ByteArray)
    assert isinstance(tag["data"]["longs"], nbtlib.LongArray)
    assert isinstance(tag["data"]["weight"], nbtlib.Float)
    assert validate_blueprint(result) == result


@pytest.mark.parametrize(("key", "value", "message"), [
    ("id", "minecraft:boat", "unknown vanilla"),
    ("id", "modded:minecart", "unknown vanilla"),
    ("id", "pig", "unknown vanilla"),
    ("pos", [2, 1, 0], "outside bounds"),
    ("pos", [-1.01, 1, 0], "outside bounds"),
    ("pos", [0, 5, 0], "outside bounds"),
    ("pos", [0, 1], "3-number"),
    ("pos", [0, True, 0], "finite number"),
    ("pos", [0, "1", 0], "finite number"),
    ("pos", [0, float("inf"), 0], "finite number"),
    ("pos", [0, float("nan"), 0], "finite number"),
    ("rotation", [0], "2-number"),
    ("rotation", [0, False], "finite number"),
    ("rotation", [0, float("inf")], "finite number"),
    ("rotation", [1e100, 0], "finite NBT Float"),
    ("role", "decorative", "role"),
    ("state", {"Name": "minecraft:pig"}, "unknown field"),
    ("nbt", {}, "string"),
    ("passengers", {}, "array of entities"),
])
def test_entity_schema_rejects_invalid_or_ambiguous_fields(blueprint, key, value, message):
    blueprint["entities"] = [{"id": "minecraft:pig", "pos": [0, 1, 0], key: value}]
    with pytest.raises(BlueprintError, match=message):
        validate_blueprint(blueprint)


@pytest.mark.parametrize("literal", [
    "[]", "1b", "{", "{} trailing", "{Items:[1b,2]}",
    "{foo:1,foo:2}", "{data:{foo:1,foo:2}}", "{foo:128b}",
    "{foo:1e999d}", "{foo:1e999f}", "{foo:1e100f}",
])
def test_entity_snbt_is_a_strict_finite_compound(blueprint, literal):
    blueprint["entities"] = [{"id": "minecraft:marker", "pos": [0, 1, 0], "nbt": literal}]
    with pytest.raises(BlueprintError):
        validate_blueprint(blueprint)


@pytest.mark.parametrize("key", ["id", "Pos", "Rotation", "Passengers", "pos", "blockPos", "nbt"])
def test_entity_snbt_cannot_shadow_authoritative_schema_fields(blueprint, key):
    blueprint["entities"] = [{"id": "minecraft:marker", "pos": [0, 1, 0], "nbt": f"{{{key}:0}}"}]
    with pytest.raises(BlueprintError, match="compiler-managed"):
        validate_blueprint(blueprint)


@pytest.mark.parametrize("literal", [
    "{UUID:[I;1,2,3,4]}", "{UUIDMost:1L,UUIDLeast:2L}", "{Owner:[I;1,2,3,4]}",
    "{Leash:{UUID:[I;1,2,3,4]}}", "{Trusted:[[I;1,2,3,4]]}",
    '{Brain:{memories:{"minecraft:angry_at":{value:[I;1,2,3,4]}}}}',
    "{data:{uuid:[I;1,2,3,4]}}", "{owner:[I;1,2,3,4]}",
])
def test_fresh_identity_rejects_saved_identities_and_references_not_strip(blueprint, literal):
    blueprint["entities"] = [{"id": "minecraft:marker", "pos": [0, 1, 0], "nbt": literal}]
    with pytest.raises(BlueprintError, match="fresh identity"):
        validate_blueprint(blueprint)


def test_passenger_validation_is_recursive_and_role_consistent(blueprint):
    blueprint["entities"] = [{"id": "minecraft:minecart", "pos": [0, 1, 0], "passengers": [
        {"id": "minecraft:pig", "pos": [0, 1, 0], "passengers": [
            {"id": "minecraft:fake", "pos": [0, 1, 0]},
        ]},
    ]}]
    with pytest.raises(BlueprintError, match=r"passengers\[0\].passengers\[0\].id"):
        validate_blueprint(blueprint)
    blueprint["entities"][0]["passengers"] = [{"id": "minecraft:pig", "pos": [0, 1, 0], "role": "test"}]
    with pytest.raises(BlueprintError, match="share their parent's role"):
        validate_blueprint(blueprint)


_WOODS = (
    "acacia", "bamboo", "birch", "cherry", "crimson", "dark_oak", "jungle",
    "mangrove", "oak", "pale_oak", "spruce", "warped",
)
_COPPER_PREFIXES = (
    "", "exposed_", "weathered_", "oxidized_", "waxed_", "waxed_exposed_",
    "waxed_weathered_", "waxed_oxidized_",
)
# Independently enumerate the complete 26.2 block families, rather than derive
# expected aliases from the function being tested. Beds are no longer entities.
_BLOCK_ENTITY_FAMILIES = {
    name: [name] for name in (
        "barrel beacon bell blast_furnace brewing_stand calibrated_sculk_sensor "
        "chiseled_bookshelf comparator conduit crafter creaking_heart daylight_detector "
        "decorated_pot dispenser dropper enchanting_table end_gateway end_portal "
        "ender_chest furnace hopper jigsaw jukebox lectern potent_sulfur sculk_catalyst "
        "sculk_sensor sculk_shrieker smoker structure_block test_block test_instance_block "
        "trapped_chest trial_spawner vault"
    ).split()
} | {
    "banner": [f"{color}{wall}_banner" for color in COUNTER_COLORS for wall in ("", "_wall")],
    "beehive": ["bee_nest", "beehive"],
    "brushable_block": ["suspicious_sand", "suspicious_gravel"],
    "campfire": ["campfire", "soul_campfire"],
    "chest": ["chest", *(f"{prefix}copper_chest" for prefix in _COPPER_PREFIXES)],
    "command_block": ["command_block", "chain_command_block", "repeating_command_block"],
    "copper_golem_statue": [f"{prefix}copper_golem_statue" for prefix in _COPPER_PREFIXES],
    "hanging_sign": [f"{wood}{wall}_hanging_sign" for wood in _WOODS for wall in ("", "_wall")],
    "mob_spawner": ["spawner"],
    "piston": ["moving_piston"],
    "shelf": [f"{wood}_shelf" for wood in _WOODS],
    "shulker_box": ["shulker_box", *(f"{color}_shulker_box" for color in COUNTER_COLORS)],
    "sign": [f"{wood}{wall}_sign" for wood in _WOODS for wall in ("", "_wall")],
    "skull": [
        *(f"{kind}{wall}_head" for kind in ("creeper", "dragon", "piglin", "player", "zombie")
          for wall in ("", "_wall")),
        *(f"{kind}{wall}_skull" for kind in ("skeleton", "wither_skeleton") for wall in ("", "_wall")),
    ],
}


@pytest.mark.parametrize(("block", "tile"), [
    (block, tile) for tile, blocks in _BLOCK_ENTITY_FAMILIES.items() for block in blocks
])
def test_block_entity_ids_match_block_families(blueprint, block, tile):
    blueprint["blocks"][2]["state"] = {"Name": f"minecraft:{block}"}
    blueprint["blocks"][2]["nbt"] = "{data:{foo:12s}}"
    result = validate_blueprint(blueprint)
    tag = nbtlib.parse_nbt(result["blocks"][2]["nbt"])
    assert str(tag["id"]) == f"minecraft:{tile}"
    assert isinstance(tag["data"]["foo"], nbtlib.Short)


def test_complete_block_entity_families_have_no_omissions_or_false_positives():
    from farmbench.blueprint import _block_entity_id, _entity_registry, _registry

    blocks, _ = _registry()
    _, tiles = _entity_registry()
    expected = {f"minecraft:{block}": f"minecraft:{tile}"
                for tile, family in _BLOCK_ENTITY_FAMILIES.items() for block in family}
    assert len(_BLOCK_ENTITY_FAMILIES) == 49
    assert len(expected) == sum(map(len, _BLOCK_ENTITY_FAMILIES.values())) == 186
    assert {f"minecraft:{name}" for name in _BLOCK_ENTITY_FAMILIES} == tiles
    assert expected.keys() <= blocks.keys()
    assert {name: _block_entity_id(name) for name in blocks if _block_entity_id(name) is not None} == expected


@pytest.mark.parametrize("color", COUNTER_COLORS)
@pytest.mark.parametrize("part", ["head", "foot"])
def test_all_colored_bed_parts_are_blocks_without_block_entities_in_26_2(blueprint, color, part):
    from farmbench.blueprint import _block_entity_id, _entity_registry

    name = f"minecraft:{color}_bed"
    blueprint["blocks"][2]["state"] = {"Name": name, "Properties": {"part": part}}
    result = validate_blueprint(blueprint)
    assert result["blocks"][2]["state"]["Name"] == name
    assert result["blocks"][2]["state"]["Properties"]["part"] == part
    assert "minecraft:bed" not in _entity_registry()[1]
    assert _block_entity_id(name) is None
    blueprint["blocks"][2]["nbt"] = '{id:"minecraft:bed"}'
    with pytest.raises(BlueprintError, match="has no block entity"):
        validate_blueprint(blueprint)


@pytest.mark.parametrize("state", ["stone", "air", "piston", "piston_head", "sticky_piston"])
def test_non_block_entities_cannot_receive_tile_data(blueprint, state):
    blueprint["blocks"][2]["state"] = {"Name": f"minecraft:{state}"}
    blueprint["blocks"][2]["nbt"] = "{}"
    with pytest.raises(BlueprintError, match="has no block entity"):
        validate_blueprint(blueprint)


@pytest.mark.parametrize("literal", ["{id:1}", "{id:'minecraft:chest'}", "{id:'modded:hopper'}"])
def test_block_entity_id_cannot_conflict_with_state(blueprint, literal):
    blueprint["blocks"][0]["nbt"] = literal
    with pytest.raises(BlueprintError, match="block state requires"):
        validate_blueprint(blueprint)


@pytest.mark.parametrize("key", ["x", "y", "z", "Pos", "Rotation", "pos", "blockPos"])
def test_block_entity_coordinates_are_compiler_managed(blueprint, key):
    blueprint["blocks"][0]["nbt"] = f"{{{key}:0}}"
    with pytest.raises(BlueprintError, match="compiler-managed"):
        validate_blueprint(blueprint)


def test_hash_resolves_snbt_key_order_and_entity_numeric_defaults(blueprint):
    blueprint["entities"] = [
        {"id": "minecraft:pig", "pos": [0, 1, 0], "nbt": "{Health:20f,Age:0}"},
        {"id": "minecraft:armor_stand", "pos": [-0.5, 2, 0.5]},
    ]
    blueprint["blocks"][0]["nbt"] = "{Items:[]}"
    equivalent = deepcopy(blueprint)
    equivalent["entities"][0].update(nbt="{Age:0, Health:20.0f}", rotation=[0, 0], role="farm")
    equivalent["entities"][0]["pos"] = [0.0, 1.0, 0.0]
    equivalent["entities"].reverse()
    equivalent["blocks"][0]["nbt"] = '{id:"minecraft:hopper",Items:[]}'
    assert blueprint_hash(equivalent) == blueprint_hash(blueprint)
    equivalent["blocks"][0]["nbt"] = '{id:"minecraft:hopper",Items:[],TransferCooldown:1}'
    assert blueprint_hash(equivalent) != blueprint_hash(blueprint)


@pytest.mark.parametrize("literal", [
    "{TileX:0}", "{TileX:0,TileY:0,TileZ:0b}", "{block_pos:[0d,1d,0d]}",
    "{block_pos:[I;0,1]}", "{block_pos:{x:0,y:1,z:0,extra:1}}",
    "{block_pos:[I;0,1,0],TileX:0,TileY:1,TileZ:0}", "{block_pos:[I;2,1,0]}",
])
def test_hanging_anchors_are_unambiguous_integer_coordinates_inside_bounds(blueprint, literal):
    blueprint["entities"] = [{"id": "minecraft:item_frame", "pos": [0, 1, 0], "nbt": literal}]
    with pytest.raises(BlueprintError):
        validate_blueprint(blueprint)


def test_deep_passengers_are_preserved_and_excessive_recursion_is_rejected(blueprint):
    entity = {"id": "minecraft:marker", "pos": [0, 1, 0], "nbt": "{data:{depth:0}}"}
    blueprint["entities"] = [entity]
    for index in range(1, 33):
        entity["passengers"] = [{"id": "minecraft:marker", "pos": [0, 1, 0], "nbt": f"{{data:{{depth:{index}}}}}"}]
        entity = entity["passengers"][0]
    result = validate_blueprint(blueprint)
    entity = result["entities"][0]
    for index in range(33):
        assert int(nbtlib.parse_nbt(entity["nbt"])["data"]["depth"]) == index
        if index < 32:
            entity = entity["passengers"][0]
    entity = blueprint["entities"][0]
    for _ in range(100):
        entity["passengers"] = [{"id": "minecraft:marker", "pos": [0, 1, 0]}]
        entity = entity["passengers"][0]
    with pytest.raises(BlueprintError, match="at most 64 levels"):
        validate_blueprint(blueprint)


def test_block_snbt_rejects_nested_persistent_entity_uuid(blueprint):
    blueprint["blocks"][2]["state"] = {"Name": "minecraft:spawner"}
    blueprint["blocks"][2]["nbt"] = '{SpawnData:{entity:{id:"minecraft:pig",UUID:[I;1,2,3,4]}}}'
    with pytest.raises(BlueprintError, match="fresh identity"):
        validate_blueprint(blueprint)


def test_every_block_entity_type_has_a_block_family_mapping():
    from farmbench.blueprint import _block_entity_id, _entity_registry, _registry

    blocks, _ = _registry()
    _, types = _entity_registry()
    assert {_block_entity_id(name) for name in blocks} - {None} == types


@pytest.mark.parametrize("anchor", [
    "TileX:0,TileY:1,TileZ:0", "block_pos:{x:0,y:1,z:0}",
    "block_pos:[0,1,0]", "block_pos:[I;0,1,0]",
])
def test_anchor_aliases_resolve_to_native_26_2_snbt_and_identical_hashes(blueprint, anchor):
    blueprint["entities"] = [{"id": "minecraft:item_frame", "pos": [0, 1, 0], "nbt": "{" + anchor + "}"}]
    result = validate_blueprint(blueprint)
    tag = nbtlib.parse_nbt(result["entities"][0]["nbt"])
    assert isinstance(tag["block_pos"], nbtlib.IntArray)
    assert list(tag["block_pos"]) == [0, 1, 0]
    assert not {"TileX", "TileY", "TileZ"} & tag.keys()
    equivalent = deepcopy(blueprint)
    equivalent["entities"][0]["nbt"] = "{block_pos:[I;0,1,0]}"
    assert blueprint_hash(blueprint) == blueprint_hash(equivalent)
    assert validate_blueprint(result) == result


@pytest.mark.parametrize("value", ["😀" * 10922 + "abc", "\x00" * 32767 + "a"], ids=["emoji", "nul"])
@pytest.mark.parametrize("as_key", [False, True])
def test_nbt_modified_utf8_string_limit_accepts_exactly_65535_encoded_bytes(blueprint, value, as_key):
    from farmbench.blueprint import _modified_utf8

    assert len(_modified_utf8(value)) == 65535
    payload = nbtlib.Compound({value: nbtlib.String("ok")} if as_key else {"text": nbtlib.String(value)})
    blueprint["entities"] = [{"id": "minecraft:marker", "pos": [0, 1, 0],
                              "nbt": nbtlib.serialize_tag(nbtlib.Compound({"data": payload}))}]
    result = validate_blueprint(blueprint)
    actual = nbtlib.parse_nbt(result["entities"][0]["nbt"])["data"]
    assert (value in actual) if as_key else (actual["text"] == value)


@pytest.mark.parametrize("value", ["😀" * 10923, "\x00" * 32768], ids=["emoji", "nul"])
@pytest.mark.parametrize("as_key", [False, True])
def test_nbt_modified_utf8_limit_rejects_values_that_ordinary_utf8_would_allow(blueprint, value, as_key):
    assert len(value.encode("utf-8")) <= 65535
    payload = nbtlib.Compound({value: nbtlib.String("ok")} if as_key else {"text": nbtlib.String(value)})
    blueprint["entities"] = [{"id": "minecraft:marker", "pos": [0, 1, 0],
                              "nbt": nbtlib.serialize_tag(nbtlib.Compound({"data": payload}))}]
    with pytest.raises(BlueprintError, match="65535 modified UTF-8 bytes"):
        validate_blueprint(blueprint)
