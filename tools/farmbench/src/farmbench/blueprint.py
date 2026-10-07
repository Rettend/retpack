"""Validate v1 semantic blueprints without contacting Minecraft or the network.

The bundled registry covers vanilla Java 26.2 block states, items, entity IDs
and block entity IDs. Typed payloads are preserved, not simulated or interpreted
as a guarantee that a particular entity can be spawned by Minecraft.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import struct
from functools import lru_cache
from importlib import resources
from os import PathLike
from pathlib import Path
from typing import Any

import nbtlib
from nbtlib.literal.parser import Parser, tokenize

FORMAT = "retpack-farm-blueprint-v1"
MINECRAFT_VERSION = "26.2"
DATA_VERSION = 4903
COUNTER_COLORS = (
    "white", "orange", "magenta", "light_blue", "yellow", "lime", "pink", "gray",
    "light_gray", "cyan", "purple", "blue", "brown", "green", "red", "black",
)
DIRECTIONS = {
    "north": (0, 0, -1),
    "south": (0, 0, 1),
    "west": (-1, 0, 0),
    "east": (1, 0, 0),
    "down": (0, -1, 0),
}
_INT_MIN = -(2**31)
_INT_MAX = 2**31 - 1
_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}\Z")
_CONDITIONS = {
    "random_tick_speed": 3,
    "simulation_distance": 12,
    "difficulty": "normal",
    "dimension": "minecraft:overworld",
}
_TICKS = {"warmup_ticks": 1200, "run_ticks": 72000, "drain_ticks": 200}
_CONTAINERS = {
    "minecraft:barrel", "minecraft:chest", "minecraft:trapped_chest",
    "minecraft:hopper", "minecraft:dispenser", "minecraft:dropper",
    "minecraft:shulker_box",
    *(f"minecraft:{color}_shulker_box" for color in COUNTER_COLORS),
    *(f"minecraft:{prefix}copper_chest" for prefix in (
        "", "exposed_", "oxidized_", "weathered_", "waxed_", "waxed_exposed_",
        "waxed_oxidized_", "waxed_weathered_",
    )),
}
# Fresh identity contract: entities receive new UUIDs at placement. Passenger
# trees are the supported relationship mechanism. Persistent identities and
# world-entity references are refused, never silently removed. This applies
# recursively to SNBT (including entities stored in spawners/beehives).
_ENTITY_REFERENCES = {
    "UUID", "UUIDMost", "UUIDLeast", "Owner", "OwnerUUID", "OwnerUUIDMost",
    "OwnerUUIDLeast", "LoveCause", "HurtBy", "AngryAt", "Target", "TargetUUID",
    "ConversionPlayer", "Trusted", "Thrower", "Attach", "Leash", "leash",
    "RootVehicle", "vehicle", "root_vehicle", "owner", "owner_uuid", "uuid",
    "trusted", "angry_at", "hurt_by", "love_cause", "conversion_player",
    "minecraft:angry_at", "minecraft:liked_player",
}
_ENTITY_FIELDS = {"id", "Pos", "Rotation", "Passengers", "pos", "blockPos", "nbt"}
_TILE_FIELDS = {"x", "y", "z", "Pos", "Rotation", "pos", "blockPos", "nbt"}


class BlueprintError(ValueError):
    """A blueprint is malformed or violates a semantic v1 requirement."""


def _object(value: Any, where: str, allowed: set[str], required: set[str]) -> dict:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise BlueprintError(f"{where}: expected a JSON object with string keys")
    missing = required - value.keys()
    if missing:
        raise BlueprintError(f"{where}: missing required field(s): {', '.join(sorted(missing))}")
    unknown = value.keys() - allowed
    if unknown:
        raise BlueprintError(f"{where}: unknown field(s): {', '.join(sorted(unknown))}")
    return value


def _string(value: Any, where: str, *, empty: bool = False) -> str:
    if not isinstance(value, str) or (not empty and not value):
        raise BlueprintError(f"{where}: expected {'a' if empty else 'a nonempty'} string")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise BlueprintError(f"{where}: string contains invalid Unicode") from exc
    return value


def _name(value: Any, where: str) -> str:
    name = _string(value, where)
    if not _NAME.fullmatch(name):
        raise BlueprintError(
            f"{where}: use 1–64 lowercase letters, digits, underscores or hyphens; "
            "start with a letter or digit"
        )
    return name


def _integer(value: Any, where: str, minimum: int = _INT_MIN, maximum: int = _INT_MAX) -> int:
    # bool is an int subclass in Python, but never an integer in the JSON schema.
    if type(value) is not int or not minimum <= value <= maximum:
        raise BlueprintError(f"{where}: expected an integer from {minimum} to {maximum}")
    return value


def _vector(value: Any, where: str, *, size: bool = False) -> list[int]:
    if not isinstance(value, list) or len(value) != 3:
        raise BlueprintError(f"{where}: expected a three-integer [x, y, z] array")
    return [_integer(part, f"{where}[{axis}]", 1 if size else _INT_MIN)
            for axis, part in enumerate(value)]


def _choice(value: Any, where: str, choices: Any) -> str:
    result = _string(value, where)
    if result not in choices:
        raise BlueprintError(f"{where}: expected one of {', '.join(sorted(choices))}")
    return result


@lru_cache(maxsize=1)
def _registry() -> tuple[dict[str, tuple[dict, dict]], frozenset[str]]:
    data = json.loads(
        resources.files("farmbench").joinpath("data", "vanilla_26_2.json").read_text("utf-8")
    )
    blocks = {}
    for names, properties, defaults in data["block_groups"]:
        for name in names.split():
            key = f"minecraft:{name}"
            if key in blocks:
                raise RuntimeError(f"Duplicate block in bundled registry: {key}")
            blocks[key] = (properties, defaults)
    without_items = {f"minecraft:{name}" for name in data["blocks_without_items"].split()}
    extra_items = {f"minecraft:{name}" for name in data["extra_items"].split()}
    items = frozenset((blocks.keys() - without_items) | extra_items)
    if (data["minecraft"] != MINECRAFT_VERSION or data["data_version"] != DATA_VERSION
            or len(blocks) != data["block_count"] or len(items) != data["item_count"]):
        raise RuntimeError("Bundled vanilla 26.2 registry metadata does not match its contents")
    return blocks, items


@lru_cache(maxsize=1)
def _entity_registry() -> tuple[frozenset[str], frozenset[str]]:
    data = json.loads(
        resources.files("farmbench").joinpath("data", "vanilla_26_2.json").read_text("utf-8")
    )
    entities = frozenset(f"minecraft:{name}" for name in data["entity_types"].split())
    tiles = frozenset(f"minecraft:{name}" for name in data["block_entity_types"].split())
    if len(entities) != data["entity_count"] or len(tiles) != data["block_entity_count"]:
        raise RuntimeError("Bundled vanilla entity registry metadata does not match its contents")
    return entities, tiles


def _block_entity_id(name: str) -> str | None:
    """Match the vanilla block families to the pinned block entity registry."""
    _, types = _entity_registry()
    short = name.removeprefix("minecraft:")
    if short == "piston":
        return None  # Only moving_piston has the minecraft:piston block entity.
    if short.endswith("_bed"):
        return None  # Minecraft 26.2 removed minecraft:bed from the block entity registry.
    aliases = {
        "bee_nest": "beehive", "soul_campfire": "campfire", "spawner": "mob_spawner",
        "moving_piston": "piston", "suspicious_sand": "brushable_block",
        "suspicious_gravel": "brushable_block", "chain_command_block": "command_block",
        "repeating_command_block": "command_block",
    }
    target = aliases.get(short, short)
    for suffix, family in (
        ("_hanging_sign", "hanging_sign"), ("_sign", "sign"),
        ("_banner", "banner"), ("_shulker_box", "shulker_box"),
        ("_skull", "skull"), ("_shelf", "shelf"),
        ("copper_chest", "chest"), ("copper_golem_statue", "copper_golem_statue"),
    ):
        if short.endswith(suffix):
            target = family
            break
    if short in {f"{prefix}{wall}_head" for prefix in ("creeper", "dragon", "piglin", "player", "zombie")
                 for wall in ("", "_wall")}:
        target = "skull"
    result = f"minecraft:{target}"
    return result if result in types else None


class _SnbtParser(Parser):
    def parse_number(self):
        tag = super().parse_number()
        if isinstance(tag, nbtlib.String):
            raise self.error("Invalid or out-of-range NBT numeric literal; quote it to use a string")
        return tag

    def parse_compound(self):
        # nbtlib's default parser silently replaces duplicate compound keys.
        result = nbtlib.Compound()
        for token in self.collect_tokens_until("CLOSE_COMPOUND"):
            if token.type not in ("NUMBER", "STRING", "QUOTED_STRING"):
                raise self.error("Expected compound key")
            key = self.unquote_string(token.value) if token.type == "QUOTED_STRING" else token.value
            if key in result:
                raise self.error(f"Duplicate compound key {key!r}")
            if self.next().current_token.type != "COLON":
                raise self.error("Expected colon")
            self.next()
            result[key] = self.parse()
        return result


def _modified_utf8(value: str) -> bytes:
    """Java DataOutput.writeUTF encodes UTF-16 units, not Unicode code points."""
    units = value.encode("utf-16-be")
    encoded = bytearray()
    for index in range(0, len(units), 2):
        unit = (units[index] << 8) | units[index + 1]
        if 0 < unit < 0x80:
            encoded.append(unit)
        elif unit < 0x800:
            encoded.extend((0xC0 | (unit >> 6), 0x80 | (unit & 0x3F)))
        else:
            encoded.extend((0xE0 | (unit >> 12), 0x80 | ((unit >> 6) & 0x3F), 0x80 | (unit & 0x3F)))
    return bytes(encoded)


def _nbt_string(value: str, where: str) -> str:
    value = _string(value, where, empty=True)
    if len(_modified_utf8(value)) > 65535:
        raise BlueprintError(f"{where}: NBT strings and keys may be at most 65535 modified UTF-8 bytes")
    return value


def _canonical_tag(tag, where: str):
    if isinstance(tag, nbtlib.Compound):
        for key in tag:
            _nbt_string(key, where)
        return nbtlib.Compound({key: _canonical_tag(tag[key], f"{where}.{key}")
                               for key in sorted(tag)})
    if isinstance(tag, nbtlib.List):
        return type(tag)([_canonical_tag(value, f"{where}[{index}]")
                          for index, value in enumerate(tag)])
    if isinstance(tag, (nbtlib.Float, nbtlib.Double)):
        if not math.isfinite(float(tag)):
            raise BlueprintError(f"{where}: NBT numbers must be finite")
        if isinstance(tag, nbtlib.Float):
            try:
                return nbtlib.Float(struct.unpack(">f", struct.pack(">f", float(tag)))[0])
            except OverflowError as exc:
                raise BlueprintError(f"{where}: NBT Float is outside its finite range") from exc
    if isinstance(tag, nbtlib.String):
        _nbt_string(str(tag), where)
    return tag


def _parse_snbt(value: Any, where: str) -> nbtlib.Compound:
    """Parse one strict compound, preserving numeric, array and list tag types."""
    literal = _string(value, where)
    try:
        parser = _SnbtParser(tokenize(literal))
        tag = parser.parse()
        if literal[parser.token_span[1]:].strip():
            raise parser.error("Expected end of SNBT")
        if not isinstance(tag, nbtlib.Compound):
            raise BlueprintError(f"{where}: SNBT payload must be a compound")
        return _canonical_tag(tag, where)
    except BlueprintError:
        raise
    except (ValueError, TypeError, OverflowError, RecursionError) as exc:
        raise BlueprintError(f"{where}: invalid SNBT compound: {exc}") from exc


def _fresh_identity(tag, where: str) -> None:
    if isinstance(tag, nbtlib.Compound):
        for key, value in tag.items():
            if key in _ENTITY_REFERENCES:
                raise BlueprintError(
                    f"{where}.{key}: fresh identity forbids saved UUIDs and world-entity "
                    "references; use explicit passengers for riding relationships"
                )
            _fresh_identity(value, f"{where}.{key}")
    elif isinstance(tag, nbtlib.List):
        for index, value in enumerate(tag):
            _fresh_identity(value, f"{where}[{index}]")


def _payload(value: Any, where: str, reserved: set[str]) -> nbtlib.Compound:
    tag = _parse_snbt(value, where)
    collision = tag.keys() & reserved
    if collision:
        raise BlueprintError(
            f"{where}: compiler-managed field(s) {', '.join(sorted(collision))}; "
            "use the blueprint's position, rotation and passengers fields instead"
        )
    _fresh_identity(tag, where)
    return tag


def _snbt(tag: nbtlib.Compound) -> str:
    return nbtlib.serialize_tag(_canonical_tag(tag, "nbt"), compact=True)


def _entity_anchor(tag: nbtlib.Compound, where: str) -> list[int] | None:
    """Hanging-entity anchors are logical coordinates, distinct from Pos."""
    legacy = {"TileX", "TileY", "TileZ"} & tag.keys()
    if legacy and "block_pos" in tag:
        raise BlueprintError(f"{where}: ambiguous TileX/TileY/TileZ and block_pos anchors")
    if legacy:
        if legacy != {"TileX", "TileY", "TileZ"} or any(
                not isinstance(tag[key], nbtlib.Int) for key in ("TileX", "TileY", "TileZ")):
            raise BlueprintError(f"{where}: TileX/TileY/TileZ must all be NBT Int tags")
        return [int(tag[key]) for key in ("TileX", "TileY", "TileZ")]
    if "block_pos" in tag:
        anchor = tag["block_pos"]
        if isinstance(anchor, nbtlib.IntArray) and len(anchor) == 3:
            return [int(part) for part in anchor]
        if (isinstance(anchor, nbtlib.List) and anchor.subtype is nbtlib.Int and len(anchor) == 3):
            return [int(part) for part in anchor]
        if (isinstance(anchor, nbtlib.Compound) and anchor.keys() == {"x", "y", "z"}
                and all(isinstance(anchor[axis], nbtlib.Int) for axis in "xyz")):
            return [int(anchor[axis]) for axis in "xyz"]
        raise BlueprintError(f"{where}.block_pos: expected three NBT Int coordinates")
    return None


def _entity_vector(value: Any, where: str, length: int = 3) -> list[float]:
    if not isinstance(value, list) or len(value) != length:
        raise BlueprintError(f"{where}: expected a {length}-number array")
    result = []
    for index, part in enumerate(value):
        try:
            number = float(part)
        except (TypeError, ValueError, OverflowError):
            number = math.nan
        if type(part) not in (int, float) or not math.isfinite(number):
            raise BlueprintError(f"{where}[{index}]: expected a finite number (not a boolean)")
        if length == 2:
            try:
                number = struct.unpack(">f", struct.pack(">f", number))[0]
            except OverflowError as exc:
                raise BlueprintError(f"{where}[{index}]: rotation must fit a finite NBT Float") from exc
        result.append(0.0 if number == 0 else number)
    return result


def _entities(value: Any, bounds: dict, where: str = "entities", depth: int = 0,
              parent_role: str | None = None) -> list[dict]:
    if not isinstance(value, list):
        raise BlueprintError(f"{where}: expected an array of entities")
    if depth >= 64 and value:
        raise BlueprintError(f"{where}: passenger trees may be at most 64 levels deep")
    types, _ = _entity_registry()
    result = []
    for index, given in enumerate(value):
        entry = f"{where}[{index}]"
        given = _object(given, entry, {"id", "pos", "rotation", "role", "nbt", "passengers"},
                        {"id", "pos"})
        entity_id = _string(given["id"], f"{entry}.id")
        if entity_id not in types:
            raise BlueprintError(f"{entry}.id: unknown vanilla {MINECRAFT_VERSION} entity {entity_id!r}")
        position = _entity_vector(given["pos"], f"{entry}.pos")
        _inside(position, bounds, f"{entry}.pos")
        entity = {
            "id": entity_id, "pos": position,
            "rotation": _entity_vector(given.get("rotation", [0, 0]), f"{entry}.rotation", 2),
            "role": _choice(given.get("role", parent_role or "farm"), f"{entry}.role", ("farm", "test")),
        }
        if "nbt" in given:
            tag = _payload(given["nbt"], f"{entry}.nbt", _ENTITY_FIELDS)
            anchor = _entity_anchor(tag, f"{entry}.nbt")
            if anchor is not None:
                _inside(anchor, bounds, f"{entry}.nbt hanging anchor")
                for key in ("TileX", "TileY", "TileZ"):
                    tag.pop(key, None)
                tag["block_pos"] = nbtlib.IntArray(anchor)
            entity["nbt"] = _snbt(tag)
        if "passengers" in given:
            entity["passengers"] = _entities(given["passengers"], bounds, f"{entry}.passengers",
                                              depth + 1, entity["role"])
            if any(passenger["role"] != entity["role"] for passenger in entity["passengers"]):
                raise BlueprintError(f"{entry}.passengers: passengers must share their parent's role")
        result.append(entity)
    return result


def _state(value: Any, where: str) -> dict:
    state = _object(value, where, {"Name", "Properties"}, {"Name"})
    name = _string(state["Name"], f"{where}.Name")
    blocks, _ = _registry()
    if name not in blocks:
        raise BlueprintError(f"{where}.Name: unknown vanilla {MINECRAFT_VERSION} block {name!r}")
    domains, defaults = blocks[name]
    given = _object(state.get("Properties", {}), f"{where}.Properties", set(domains), set())
    properties = dict(defaults)
    for key, value in given.items():
        properties[key] = _choice(value, f"{where}.Properties.{key}", domains[key])
    result = {"Name": name}
    if properties:
        result["Properties"] = properties
    return result


def _inside(position: list[int] | list[float], bounds: dict, where: str) -> None:
    if any(not start <= part < start + size
           for part, start, size in zip(position, bounds["min"], bounds["size"])):
        raise BlueprintError(
            f"{where}: position {position} is outside bounds "
            f"(min={bounds['min']}, size={bounds['size']}; upper edge is exclusive)"
        )


def _bounds(value: Any) -> dict:
    given = _object(value, "bounds", {"min", "size"}, {"min", "size"})
    bounds = {
        "min": _vector(given["min"], "bounds.min"),
        "size": _vector(given["size"], "bounds.size", size=True),
    }
    for axis, (start, size) in enumerate(zip(bounds["min"], bounds["size"])):
        _integer(start + size - 1, f"bounds maximum on axis {axis}")
    return bounds


def _blocks(value: Any, bounds: dict) -> tuple[list[dict], dict[tuple[int, ...], dict]]:
    if not isinstance(value, list) or not value:
        raise BlueprintError("blocks: expected a nonempty array")
    blocks = []
    positions = {}
    for index, given in enumerate(value):
        where = f"blocks[{index}]"
        given = _object(given, where, {"pos", "state", "role", "nbt"}, {"pos", "state"})
        position = _vector(given["pos"], f"{where}.pos")
        _inside(position, bounds, f"{where}.pos")
        key = tuple(position)
        if key in positions:
            raise BlueprintError(f"{where}.pos: duplicate block position {position}")
        block = {
            "pos": position,
            "state": _state(given["state"], f"{where}.state"),
            "role": _choice(given.get("role", "farm"), f"{where}.role", ("farm", "test")),
        }
        if "nbt" in given:
            tag = _payload(given["nbt"], f"{where}.nbt", _TILE_FIELDS)
            expected = _block_entity_id(block["state"]["Name"])
            if expected is None:
                raise BlueprintError(f"{where}.nbt: state {block['state']['Name']} has no block entity")
            if "id" in tag and (not isinstance(tag["id"], nbtlib.String) or str(tag["id"]) != expected):
                raise BlueprintError(f"{where}.nbt.id: block state requires block entity {expected!r}")
            tag["id"] = nbtlib.String(expected)
            block["nbt"] = _snbt(tag)
        blocks.append(block)
        positions[key] = block
    return blocks, positions


def _ports(value: Any, bounds: dict, positions: dict) -> list[dict]:
    if not isinstance(value, list) or not value:
        raise BlueprintError("ports: expected a nonempty array of hopper output ports")
    fields = {"name", "kind", "position", "direction", "counter", "survival_output"}
    ports = []
    names = set()
    hoppers = set()
    sinks = set()
    for index, given in enumerate(value):
        where = f"ports[{index}]"
        given = _object(given, where, fields, fields)
        name = _name(given["name"], f"{where}.name")
        if name in names:
            raise BlueprintError(f"{where}.name: duplicate port name {name!r}")
        names.add(name)
        kind = _choice(given["kind"], f"{where}.kind", ("hopper",))
        position = _vector(given["position"], f"{where}.position")
        _inside(position, bounds, f"{where}.position")
        direction = _choice(given["direction"], f"{where}.direction", DIRECTIONS)
        counter = _choice(given["counter"], f"{where}.counter", COUNTER_COLORS)
        key = tuple(position)
        if key in hoppers:
            raise BlueprintError(f"{where}.position: hopper is already used by another port")
        hoppers.add(key)
        hopper = positions.get(key)
        if hopper is None or hopper["state"]["Name"] != "minecraft:hopper":
            raise BlueprintError(f"{where}.position: output hopper is missing at {position}")
        if hopper["role"] != "farm":
            raise BlueprintError(f"{where}.position: output hopper must have role 'farm'")
        if hopper["state"]["Properties"]["facing"] != direction:
            raise BlueprintError(f"{where}.direction: does not match the output hopper facing")
        if hopper["state"]["Properties"]["enabled"] != "true":
            raise BlueprintError(f"{where}.position: output hopper must be enabled")
        sink_position = [part + delta for part, delta in zip(position, DIRECTIONS[direction])]
        _inside(sink_position, bounds, f"{where} counter wool")
        sink_key = tuple(sink_position)
        if sink_key in sinks:
            raise BlueprintError(f"{where}: counter wool is already used by another port")
        sinks.add(sink_key)
        sink = positions.get(sink_key)
        if sink is None or sink["state"]["Name"] != f"minecraft:{counter}_wool":
            raise BlueprintError(
                f"{where}: expected {counter} counter wool adjacent to the hopper "
                f"in direction {direction} at {sink_position}"
            )
        if sink["role"] != "test":
            raise BlueprintError(f"{where}: counter wool must have role 'test'")
        survival = _state(given["survival_output"], f"{where}.survival_output")
        if survival["Name"] not in _CONTAINERS:
            raise BlueprintError(
                f"{where}.survival_output: use a hopper-compatible container "
                "(barrel, single chest, shulker box, hopper, dispenser or dropper)"
            )
        if survival["Properties"].get("type", "single") != "single":
            raise BlueprintError(f"{where}.survival_output: chest type must be 'single'")
        ports.append({
            "name": name, "kind": kind, "position": position, "direction": direction,
            "counter": counter, "survival_output": survival,
        })
    return ports


def _conditions(value: Any) -> dict:
    given = _object(value, "conditions", set(_CONDITIONS), set())
    conditions = _CONDITIONS | given
    return {
        "random_tick_speed": _integer(conditions["random_tick_speed"],
                                      "conditions.random_tick_speed", 0),
        "simulation_distance": _integer(conditions["simulation_distance"],
                                        "conditions.simulation_distance", 2, 32),
        "difficulty": _choice(conditions["difficulty"], "conditions.difficulty",
                              ("peaceful", "easy", "normal", "hard")),
        "dimension": _choice(conditions["dimension"], "conditions.dimension",
                             ("minecraft:overworld", "minecraft:the_nether", "minecraft:the_end")),
    }


def _benchmark(value: Any, ports: list[dict]) -> dict:
    given = _object(value, "benchmark", {"counter", "item", *_TICKS}, {"counter", "item"})
    counter = _choice(given["counter"], "benchmark.counter", COUNTER_COLORS)
    if not any(port["counter"] == counter for port in ports):
        raise BlueprintError("benchmark.counter: no output port uses this counter")
    item = _string(given["item"], "benchmark.item")
    _, items = _registry()
    if item not in items or item == "minecraft:air":
        raise BlueprintError(f"benchmark.item: unknown or empty vanilla {MINECRAFT_VERSION} item {item!r}")
    return {
        "counter": counter,
        "item": item,
        **{key: _integer(given.get(key, default), f"benchmark.{key}",
                         1 if key == "run_ticks" else 0) for key, default in _TICKS.items()},
    }


def validate_blueprint(data: Any) -> dict:
    """Return a new validated dictionary with all defaults resolved.

    Coordinates stay logical, including negative values; ``bounds.min`` is
    explicit and the upper edge is exclusive. Missing block properties use the
    actual 26.2 defaults. Block role defaults to ``farm``; counter wool must be
    explicitly marked ``test``. Conditions and tick defaults match the example.
    Unknown fields, unsupported versions, and v0 input are rejected, not ignored.
    Optional ``entities`` entries have ``id``, finite fractional ``pos``, optional
    ``rotation`` ([yaw, pitch], default [0, 0]), ``role`` (farm/test), compound
    ``nbt`` (SNBT), and recursive ``passengers`` (same schema and parent role).
    Passenger roles default to the parent role; roots default to farm.
    Optional block ``nbt`` is a compound SNBT payload; its id is inferred from
    the block state or validated if supplied. Compiler-managed positions and
    identity/reference fields are rejected. Hanging anchors in SNBT are logical
    coordinates; other custom data is opaque and preserved without translation.
    """
    if isinstance(data, dict) and data.get("format") == "retpack-farm-blueprint-v0":
        raise BlueprintError(
            "format: v0 is not accepted; deliberately migrate to v1 with explicit bounds.min, "
            "blocks/state/role, counter and survival_output (see examples/bamboo_micro_v1.json)"
        )
    fields = {"format", "name", "minecraft", "data_version", "bounds", "blocks", "ports", "entities",
              "conditions", "benchmark", "notes"}
    given = _object(data, "blueprint", fields,
                    {"format", "name", "minecraft", "bounds", "blocks", "ports", "benchmark"})
    _choice(given["format"], "format", (FORMAT,))
    name = _name(given["name"], "name")
    minecraft = _choice(given["minecraft"], "minecraft", (MINECRAFT_VERSION,))
    data_version = _integer(given.get("data_version", DATA_VERSION), "data_version", 1)
    if data_version != DATA_VERSION:
        raise BlueprintError(f"data_version: Minecraft {minecraft} requires {DATA_VERSION}")
    bounds = _bounds(given["bounds"])
    blocks, positions = _blocks(given["blocks"], bounds)
    ports = _ports(given["ports"], bounds, positions)
    notes = given.get("notes", [])
    if not isinstance(notes, list):
        raise BlueprintError("notes: expected an array of strings")
    result = {
        "format": FORMAT,
        "name": name,
        "minecraft": minecraft,
        "data_version": data_version,
        "bounds": bounds,
        "blocks": blocks,
        "ports": ports,
        "conditions": _conditions(given.get("conditions", {})),
        "benchmark": _benchmark(given["benchmark"], ports),
        "notes": [_string(note, f"notes[{index}]", empty=True)
                  for index, note in enumerate(notes)],
    }
    # Do not insert absent optional fields: pre-extension v1 hashes stay stable.
    if "entities" in given:
        result["entities"] = _entities(given["entities"], bounds)
    return result


def _unique_keys(pairs: list[tuple[str, Any]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise BlueprintError(f"JSON: duplicate object key {key!r}")
        result[key] = value
    return result


def _invalid_constant(value: str) -> None:
    raise BlueprintError(f"JSON: {value} is not a valid JSON number")


def load_blueprint(path: str | PathLike[str]) -> dict:
    """Read strict UTF-8 JSON (an optional BOM is allowed) and validate it."""
    source = Path(path)
    try:
        text = source.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError) as exc:
        raise BlueprintError(f"Cannot read blueprint {source}: {exc}") from exc
    try:
        data = json.loads(text, object_pairs_hook=_unique_keys, parse_constant=_invalid_constant)
    except BlueprintError as exc:
        raise BlueprintError(f"{source}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise BlueprintError(f"{source}:{exc.lineno}:{exc.colno}: invalid JSON: {exc.msg}") from exc
    except ValueError as exc:
        # Python can reject an excessively long integer before validation runs.
        raise BlueprintError(f"{source}: invalid JSON: {exc}") from exc
    except RecursionError as exc:
        raise BlueprintError(f"{source}: JSON is nested too deeply") from exc
    return validate_blueprint(data)


def blueprint_hash(data: Any) -> str:
    """SHA256 of default-resolved, key-sorted UTF-8 JSON without whitespace.

    Block, port and root entity order is not semantic, so those lists are sorted.
    Passenger and note order is retained. Invalid input never
    receives a hash; equivalent implicit and explicit defaults hash identically.
    """
    canonical = validate_blueprint(data)
    canonical["blocks"].sort(key=lambda block: tuple(block["pos"]))
    canonical["ports"].sort(key=lambda port: port["name"])
    if "entities" in canonical:
        canonical["entities"].sort(key=lambda entity: json.dumps(entity, sort_keys=True, separators=(",", ":")))
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False, allow_nan=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
