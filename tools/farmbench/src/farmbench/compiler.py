"""Compile validated blueprints without touching a Minecraft instance or world."""

from __future__ import annotations

from collections import Counter
from copy import deepcopy
import gzip
import hashlib
import io
import json
import math
import struct
from pathlib import Path
from typing import Iterator

import mcblueprint
import nbtlib

from .blueprint import _entity_anchor, _modified_utf8, _parse_snbt, _registry, blueprint_hash, validate_blueprint


_DIRECTIONS = {
    "down": (0, -1, 0),
    "up": (0, 1, 0),
    "north": (0, 0, -1),
    "south": (0, 0, 1),
    "west": (-1, 0, 0),
    "east": (1, 0, 0),
}
_ORIGIN = (0, 0, 0)
_AIR_NAMES = {"minecraft:air", "minecraft:cave_air", "minecraft:void_air"}


def _state(value: dict) -> mcblueprint.BlockState:
    return mcblueprint.BlockState(value["Name"], value.get("Properties", {}))


def _state_dict(value: mcblueprint.BlockState) -> dict:
    result = {"Name": value.name}
    if value.properties:
        result["Properties"] = dict(sorted(value.properties.items()))
    return result


def _sink(port: dict) -> tuple[int, int, int]:
    direction = _DIRECTIONS[port["direction"]]
    return tuple(port["position"][axis] + direction[axis] for axis in range(3))


def _export_blocks(blueprint: dict, variant: str) -> dict:
    origin = blueprint["bounds"]["min"]
    replacements = (
        {_sink(port): port["survival_output"] for port in blueprint["ports"]}
        if variant == "survival"
        else {}
    )
    blocks = {}
    for block in blueprint["blocks"]:
        logical = tuple(block["pos"])
        if logical in replacements:
            state = _state(replacements[logical])
        elif variant == "survival" and block["role"] == "test":
            continue
        else:
            state = _state(block["state"])
        if state != mcblueprint.AIR:
            exported = tuple(logical[axis] - origin[axis] for axis in range(3))
            blocks[exported] = state
    return blocks


def _export_block_entities(blueprint: dict, variant: str) -> dict:
    origin = blueprint["bounds"]["min"]
    replacements = {_sink(port) for port in blueprint["ports"]} if variant == "survival" else set()
    tiles = {}
    for block in blueprint["blocks"]:
        if "nbt" not in block or (variant == "survival" and
                                   (block["role"] == "test" or tuple(block["pos"]) in replacements)):
            continue
        position = tuple(part - start for part, start in zip(block["pos"], origin))
        tag = _parse_snbt(block["nbt"], "block.nbt")
        tag.update({axis: nbtlib.Int(part) for axis, part in zip("xyz", position)})
        tiles[position] = tag
    return tiles


def _export_entities(blueprint: dict, variant: str) -> list[nbtlib.Compound]:
    origin = blueprint["bounds"]["min"]

    def export(entity: dict) -> nbtlib.Compound:
        tag = _parse_snbt(entity["nbt"], "entity.nbt") if "nbt" in entity else nbtlib.Compound()
        anchor = _entity_anchor(tag, "entity.nbt")
        if anchor is not None:
            anchor = [part - start for part, start in zip(anchor, origin)]
            for key in ("TileX", "TileY", "TileZ"):
                tag.pop(key, None)
            tag["block_pos"] = nbtlib.IntArray(anchor)
        tag["id"] = nbtlib.String(entity["id"])
        tag["Pos"] = nbtlib.List[nbtlib.Double]([part - start for part, start in zip(entity["pos"], origin)])
        tag["Rotation"] = nbtlib.List[nbtlib.Float](entity["rotation"])
        if "passengers" in entity:
            tag["Passengers"] = nbtlib.List[nbtlib.Compound]([export(value) for value in entity["passengers"]])
        return tag

    # Top-level order is not semantic, but passenger order can control seats.
    entities = sorted(blueprint.get("entities", []), key=lambda entity: json.dumps(entity, sort_keys=True, separators=(",", ":")))
    return [export(entity) for entity in entities if variant != "survival" or entity["role"] != "test"]


def _vanilla_entities(entities: list) -> list[nbtlib.Compound]:
    # mcblueprint 0.1.0 preserves format-native raw entities. It does not convert
    # region.Entities compounds into vanilla StructureTemplate wrappers.
    return [nbtlib.Compound({
        "pos": deepcopy(entity["Pos"]),
        "blockPos": nbtlib.List[nbtlib.Int](_entity_anchor(entity, "exported entity") or
                                          [math.floor(float(part)) for part in entity["Pos"]]),
        "nbt": deepcopy(entity),
    }) for entity in entities]


def _walk_entities(entities: list) -> Iterator[nbtlib.Compound]:
    for entity in entities:
        yield entity
        yield from _walk_entities(entity.get("Passengers", []))


def _positions(size: tuple) -> Iterator[tuple[int, int, int]]:
    # Litematica's packed order: x fastest, then z, then y.
    for y in range(size[1]):
        for z in range(size[2]):
            for x in range(size[0]):
                yield x, y, z


def _block_count(blocks: dict) -> int:
    return sum(state.name not in _AIR_NAMES for state in blocks.values())


def _read_library_nbt(data: bytes) -> nbtlib.File:
    # mcblueprint/nbtlib use ordinary UTF-8 internally. Never use this reader on
    # Java artifacts: nbtlib would replace invalid UTF-8 and silently lose data.
    return nbtlib.File.from_fileobj(io.BytesIO(gzip.decompress(data)))


def _decode_modified_utf8(data: bytes) -> str:
    if any(byte >= 0xF0 for byte in data):
        raise ValueError("NBT string contains ordinary four-byte UTF-8, not Java modified UTF-8")
    text = data.replace(b"\xc0\x80", b"\x00").decode("utf-8", "surrogatepass")
    return text.encode("utf-16-be", "surrogatepass").decode("utf-16-be")


def _read_nbt(data: bytes) -> nbtlib.File:
    """Read Java NBT without nbtlib's lossy ordinary-UTF-8 string decoder."""
    source = io.BytesIO(gzip.decompress(data))

    def read(count: int) -> bytes:
        result = source.read(count)
        if len(result) != count:
            raise ValueError("Truncated Java NBT")
        return result

    def string() -> str:
        return _decode_modified_utf8(read(struct.unpack(">H", read(2))[0]))

    def payload(tag_id: int):
        if tag_id == nbtlib.String.tag_id:
            return nbtlib.String(string())
        if tag_id == nbtlib.Compound.tag_id:
            result = nbtlib.Compound()
            while child_id := read(1)[0]:
                key = string()
                if key in result:
                    raise ValueError("Duplicate Java NBT compound key")
                result[key] = payload(child_id)
            return result
        if tag_id == nbtlib.List.tag_id:
            child_id = read(1)[0]
            count = struct.unpack(">i", read(4))[0]
            if count < 0 or (child_id == 0 and count):
                raise ValueError("Invalid Java NBT list")
            return nbtlib.List[nbtlib.Compound.get_tag(child_id)](payload(child_id) for _ in range(count))
        if tag_id not in nbtlib.Compound.all_tags or tag_id == 0:
            raise ValueError(f"Invalid Java NBT tag ID: {tag_id}")
        return nbtlib.Compound.get_tag(tag_id).parse(source)

    if read(1)[0] != nbtlib.Compound.tag_id:
        raise ValueError("Java NBT root must be a compound")
    name = string()
    root = nbtlib.File(payload(nbtlib.Compound.tag_id), root_name=name)
    if source.read(1):
        raise ValueError("Trailing Java NBT data")
    return root


def _write_nbt(root: nbtlib.File) -> bytes:
    """Write Java modified UTF-8 for every string, including compound keys."""
    buffer = io.BytesIO()

    def string(value: str) -> None:
        encoded = _modified_utf8(value)
        if len(encoded) > 65535:
            raise ValueError("NBT string exceeds 65535 modified UTF-8 bytes")
        buffer.write(struct.pack(">H", len(encoded)))
        buffer.write(encoded)

    def payload(tag) -> None:
        if isinstance(tag, nbtlib.Compound):
            for key, value in tag.items():
                buffer.write(bytes([value.tag_id]))
                string(key)
                payload(value)
            buffer.write(b"\x00")
        elif isinstance(tag, nbtlib.List):
            buffer.write(struct.pack(">Bi", tag.subtype.tag_id, len(tag)))
            for value in tag:
                payload(value)
        elif isinstance(tag, nbtlib.String):
            string(str(tag))
        else:
            tag.write(buffer)

    buffer.write(b"\x0a")
    string(root.root_name)
    payload(root)
    # Stable hashes: no gzip filename or wall-clock timestamp.
    return gzip.compress(buffer.getvalue(), mtime=0)


def _vanilla_bytes(model: mcblueprint.Blueprint, data_version: int) -> bytes:
    # mcblueprint 0.1.0 writes only non-air cells and does not supply DataVersion.
    regions = [region.clone() for region in model.regions]
    for region in regions:
        region.entities = _vanilla_entities(region.entities)
    vanilla = mcblueprint.Blueprint(
        regions,
        mcblueprint.Metadata(extras={"DataVersion": data_version}),
    )
    root = _read_library_nbt(mcblueprint.save_bytes(vanilla, "vanilla"))
    air_index = len(root["palette"])
    root["palette"].append(nbtlib.Compound({"Name": nbtlib.String("minecraft:air")}))
    region = model.region()
    for position in _positions(region.size):
        if region.get(*position) == mcblueprint.AIR:
            root["blocks"].append(
                nbtlib.Compound(
                    {
                        "pos": nbtlib.List[nbtlib.Int](position),
                        "state": nbtlib.Int(air_index),
                    }
                )
            )
    return _write_nbt(root)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(f"Export verification failed: {message}")


def _tag_state(entry: nbtlib.Compound) -> mcblueprint.BlockState:
    return mcblueprint.BlockState(
        str(entry["Name"]),
        {str(key): str(value) for key, value in entry.get("Properties", {}).items()},
    )


def _packed_indices(words: list, bits: int, volume: int) -> Iterator[int]:
    """Decode the continuous bit stream independently of mcblueprint's codec."""
    _require(len(words) == (volume * bits + 63) // 64, "packed array length")
    source = iter(words)
    reservoir = 0
    available = 0
    mask = (1 << bits) - 1
    for _ in range(volume):
        if available < bits:
            reservoir |= (int(next(source)) & ((1 << 64) - 1)) << available
            available += 64
        yield reservoir & mask
        reservoir >>= bits
        available -= bits


def _nbt_signature(tag):
    """Include tag IDs and list element types; numeric equality alone is unsafe."""
    if isinstance(tag, nbtlib.Compound):
        return tag.tag_id, tuple((key, _nbt_signature(value)) for key, value in sorted(tag.items()))
    if isinstance(tag, nbtlib.List):
        return tag.tag_id, tag.subtype.tag_id, tuple(_nbt_signature(value) for value in tag)
    if isinstance(tag, (nbtlib.ByteArray, nbtlib.IntArray, nbtlib.LongArray)):
        return tag.tag_id, tuple(int(value) for value in tag)
    return tag.tag_id, tag


def _verify_payload(actual, expected, where: str) -> None:
    _require(isinstance(actual, nbtlib.Compound), f"{where} compound")
    _require(_nbt_signature(actual) == _nbt_signature(expected), f"{where} typed NBT")


def _verify_entities(actual, expected: list, where: str, *, vanilla: bool = False) -> None:
    _require(isinstance(actual, nbtlib.List) and actual.subtype is nbtlib.Compound,
             f"{where} compound list")
    _require(len(actual) == len(expected), f"{where} root entity count")
    wrappers = _vanilla_entities(expected) if vanilla else expected
    for index, (entry, wanted) in enumerate(zip(actual, wrappers, strict=True)):
        _verify_payload(entry, wanted, f"{where}[{index}]")
    # Recursion in the signature verifies passengers, their poses and all custom
    # payloads, not just the root count/type reported by a library roundtrip.


def _verify_roundtrip(data: bytes, fmt: str, size: tuple, blocks: dict,
                      entities: list | None = None, tiles: dict | None = None) -> None:
    # Adapt strings only for the pinned library's semantic check. The actual
    # artifact is independently read above using Java's modified UTF-8 rules.
    normal = io.BytesIO()
    _read_nbt(data).write(normal)
    decoded = mcblueprint.load_bytes(gzip.compress(normal.getvalue(), mtime=0), fmt)
    _require(len(decoded.regions) == 1, f"{fmt} region count")
    region = decoded.region()
    _require(region.position == _ORIGIN, f"{fmt} origin")
    _require(region.size == size, f"{fmt} dimensions")
    _require(dict(region.items()) == blocks, f"{fmt} roundtrip block states")
    expected_tiles = tiles or {}
    actual_tiles = dict(region.tile_entities())
    _require(actual_tiles.keys() == expected_tiles.keys(), f"{fmt} roundtrip block entity coordinates")
    for position, expected in expected_tiles.items():
        payload = actual_tiles[position].nbt
        if fmt == "litematic":
            expected = nbtlib.Compound({key: value for key, value in expected.items() if key not in {"x", "y", "z"}})
        _verify_payload(payload, expected, f"{fmt} roundtrip block entity at {position}")
    raw = region.entities if fmt == "litematic" else decoded.entities
    _verify_entities(nbtlib.List[nbtlib.Compound](raw), entities or [], f"{fmt} roundtrip entities", vanilla=fmt == "vanilla")


def _verify_litematic(data: bytes, size: tuple, blocks: dict, data_version: int,
                      entities: list | None = None, tiles: dict | None = None) -> None:
    root = _read_nbt(data)
    _require(
        isinstance(root.get("MinecraftDataVersion"), nbtlib.Int)
        and int(root["MinecraftDataVersion"]) == data_version,
        "Litematic MinecraftDataVersion",
    )
    _require(int(root["Version"]) == 6, "Litematic format version")
    _require(len(root["Regions"]) == 1, "Litematic region count")
    region = next(iter(root["Regions"].values()))
    _require(tuple(int(region["Size"][axis]) for axis in "xyz") == size, "region size")
    _require(
        tuple(int(region["Position"][axis]) for axis in "xyz") == _ORIGIN,
        "region position",
    )
    palette = [_tag_state(entry) for entry in region["BlockStatePalette"]]
    _require(bool(palette) and palette[0] == mcblueprint.AIR, "air palette entry")
    volume = size[0] * size[1] * size[2]
    indices = _packed_indices(region["BlockStates"], max(2, (len(palette) - 1).bit_length()), volume)
    for position, index in zip(_positions(size), indices, strict=True):
        _require(index < len(palette), f"palette index at {position}")
        _require(palette[index] == blocks.get(position, mcblueprint.AIR), f"state at {position}")
    metadata = root["Metadata"]
    _require(int(metadata["TotalBlocks"]) == _block_count(blocks), "total block count")
    _require(int(metadata["TotalVolume"]) == volume, "clearance volume")
    _require(
        tuple(int(metadata["EnclosingSize"][axis]) for axis in "xyz") == size,
        "enclosing size",
    )
    expected_tiles = tiles or {}
    actual_tiles = region.get("TileEntities")
    _require(isinstance(actual_tiles, nbtlib.List) and actual_tiles.subtype is nbtlib.Compound,
             "Litematic block entity compound list")
    _require(len(actual_tiles) == len(expected_tiles), "Litematic block entity count")
    seen = set()
    for entry in actual_tiles:
        _require(all(isinstance(entry.get(axis), nbtlib.Int) for axis in "xyz"), "Litematic tile coordinate types")
        position = tuple(int(entry[axis]) for axis in "xyz")
        _require(position not in seen and position in expected_tiles, "Litematic block entity coordinate")
        seen.add(position)
        _verify_payload(entry, expected_tiles[position], f"Litematic block entity at {position}")
    _verify_entities(region.get("Entities"), entities or [], "Litematic entities")
    _verify_roundtrip(data, "litematic", size, blocks, entities, tiles)


def _verify_vanilla(data: bytes, size: tuple, blocks: dict, data_version: int,
                    entities: list | None = None, tiles: dict | None = None) -> None:
    root = _read_nbt(data)
    _require(
        isinstance(root.get("DataVersion"), nbtlib.Int)
        and int(root["DataVersion"]) == data_version,
        "vanilla DataVersion",
    )
    _require(tuple(int(value) for value in root["size"]) == size, "vanilla dimensions")
    palette = [_tag_state(entry) for entry in root["palette"]]
    seen = set()
    expected_tiles = tiles or {}
    seen_tiles = set()
    for entry in root["blocks"]:
        position = tuple(int(value) for value in entry["pos"])
        _require(position not in seen, "duplicate vanilla coordinate")
        _require(
            len(position) == 3 and all(0 <= position[axis] < size[axis] for axis in range(3)),
            "vanilla coordinate bounds",
        )
        seen.add(position)
        index = int(entry["state"])
        _require(0 <= index < len(palette), "vanilla palette index")
        _require(palette[index] == blocks.get(position, mcblueprint.AIR), f"vanilla state at {position}")
        if "nbt" in entry:
            _require(position in expected_tiles, "unexpected vanilla block entity")
            seen_tiles.add(position)
            _verify_payload(entry["nbt"], expected_tiles[position], f"vanilla block entity at {position}")
    _require(len(seen) == size[0] * size[1] * size[2], "explicit vanilla clearance air")
    _require(seen_tiles == expected_tiles.keys(), "vanilla block entity coordinates")
    _verify_entities(root.get("entities"), entities or [], "vanilla entities", vanilla=True)
    _verify_roundtrip(data, "vanilla", size, blocks, entities, tiles)


def _json_bytes(value: dict) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")


def _entity_materials(entities: list) -> dict:
    counts = Counter(str(entity["id"]) for entity in _walk_entities(entities))
    _, items = _registry()
    direct = {
        "armor_stand", "minecart", "chest_minecart", "hopper_minecart", "furnace_minecart",
        "tnt_minecart", "command_block_minecart", "item_frame", "glow_item_frame", "painting",
        "end_crystal", "mannequin",
    }
    placement_items = Counter()
    entries = []
    for entity_id, count in sorted(counts.items()):
        short = entity_id.removeprefix("minecraft:")
        candidate = entity_id if short in direct or short.endswith(("_boat", "_raft")) else None
        if candidate not in items:
            candidate = f"{entity_id}_spawn_egg" if f"{entity_id}_spawn_egg" in items else None
        if candidate is not None:
            placement_items[candidate] += count
        entries.append({"id": entity_id, "count": count, "placement_item": candidate,
                        "no_item": candidate is None})
    return {"total_entities": sum(counts.values()), "counts": dict(sorted(counts.items())),
            "placement_items": dict(sorted(placement_items.items())), "types": entries,
            "note": "Entity counts include passengers, separate from blocks. Placement items include creative spawn eggs; no_item means no direct placement item, not a crafting cost."}


def _materials(blocks: dict, variant: str, source_hash: str, entities: list | None = None) -> dict:
    states = Counter(state for state in blocks.values() if state.name not in _AIR_NAMES)
    counts = Counter(state.name for state in blocks.values() if state.name not in _AIR_NAMES)
    result = {
        "format": "retpack-farm-materials-v1",
        "kind": "block-counts",
        "blueprint_sha256": source_hash,
        "variant": variant,
        "total_blocks": _block_count(blocks),
        "counts": dict(sorted(counts.items())),
        "states": [
            {"state": _state_dict(state), "count": count}
            for state, count in sorted(states.items(), key=lambda item: str(item[0]))
        ],
        "note": "Placed block counts, excluding air; not recipe ingredients or crafting costs.",
    }
    if entities:
        result["entities"] = _entity_materials(entities)
    return result


def _placement(blueprint: dict, variant: str, filename: str, block_count: int) -> str:
    origin = blueprint["bounds"]["min"]
    size = blueprint["bounds"]["size"]
    offset = [-value for value in origin]
    lines = [
        f"# Place {blueprint['name']} ({variant})",
        "",
        f"Minecraft {blueprint['minecraft']}, data version {blueprint['data_version']}. "
        f"{block_count} non-air blocks in a {size[0]} × {size[1]} × {size[2]} bounding box.",
        "",
        f"Exported `[0, 0, 0]` is logical `{origin}`. Exported coordinates are logical "
        f"coordinates plus `{offset}`. Place the exported origin at the world's minimum "
        "corner of the chosen bounding box; do not apply this offset again. Do not rotate "
        "or mirror the benchmark build.",
        "",
        "1. Use a separate test world, not a live survival world. Clear the entire bounding "
        "box, including the bottom layer and empty clearance cells.",
        f"2. Copy `{filename}` into that test instance's `schematics` folder. Press **M**, "
        "open **Load Schematics**, load the file, then use **Schematic Placements** to "
        "set the origin. This compiler never edits an instance or world.",
        "3. For a creative paste, obtain the required command permissions. In the tested "
        "pack the tool is a **stick**, and **executeOperation** is unbound. Open "
        "**M → Configuration Menu → Hotkeys** and bind **executeOperation**. Select "
        "**Paste schematic** mode, hold the stick, and press that binding.",
        "4. Default paste replaces **only air**. Existing blocks can prevent placement; "
        "schematic air does not clear collisions. Explicit air in the export is not a "
        "promise that Litematica will clear the world. Recheck the whole box.",
        "5. Run the **Schematic Verifier** for **all layers**. Inspect the bottom layer "
        "(`exported y = 0`) for missing blocks, then check extra blocks, wrong states, "
        "hopper directions, and the output. A visible projection is not proof of placed "
        "blocks. Do not start a benchmark until placement is verified.",
        "",
    ]
    if variant == "lab":
        lines.append("The lab build retains test wool for the hopper counter. Confirm its color and port before running the benchmark.")
    else:
        lines = lines[:6] + [
            "1. Copy the `.litematic` into your instance's `schematics` folder and load it through **M → Load Schematics**.",
            "2. Position the hologram with **M → Schematic Placements**. Check that its full bounds fit the chosen building site.",
            "3. Open **M+L** for the material list and build against the hologram. It is a guide, not placed blocks.",
            "4. Use **M+V** to check missing blocks, extra blocks and wrong states. Inspect the bottom hopper and its barrel output.",
            "",
        ]
        lines.append("The survival build replaces each output test-wool sink with its survival output (normally a barrel), and omits other test blocks. It has no test-wool sink; do not use it for the wool-counter benchmark.")
    if blueprint["ports"]:
        lines.extend(["", "Outputs:"])
        for port in blueprint["ports"]:
            exported = [port["position"][axis] - origin[axis] for axis in range(3)]
            sink = [value - origin[axis] for axis, value in enumerate(_sink(port))]
            lines.append(
                f"- `{port['name']}`: hopper at exported `{exported}`, facing "
                f"`{port['direction']}`; sink at `{sink}`."
            )
    if blueprint.get("notes"):
        lines.extend(["", "Blueprint notes:"])
        lines.extend(f"- {note}" for note in blueprint["notes"])
    return "\n".join(lines) + "\n"


def compile_blueprint(blueprint: dict, output_dir: str | Path, variant: str = "lab") -> dict:
    """Emit both formats in a new directory and return the persisted build manifest.

    Input is validated and defaults resolved by blueprint.py. ``artifact`` identifies the
    primary Litematic for benchmark consumers; ``files`` contains absolute paths
    and SHA256 hashes, except ``files.build`` (a manifest cannot hash itself).
    ``translation.offset`` is added to logical coordinates; exported zero is
    ``translation.logical_origin``. Existing output directories are refused,
    even when empty. No game instance, save, or world is modified.
    """
    if variant not in ("lab", "survival"):
        raise ValueError("variant must be 'lab' or 'survival'")
    blueprint = validate_blueprint(blueprint)
    name = blueprint["name"]
    output = Path(output_dir).resolve()
    if output.exists():
        raise FileExistsError(f"Output directory already exists; choose a fresh directory: {output}")
    size = tuple(blueprint["bounds"]["size"])
    if size[0] * size[1] * size[2] > 1_000_000:
        raise ValueError("The first compiler supports bounding boxes up to 1,000,000 cells, including air")
    origin = list(blueprint["bounds"]["min"])
    source_hash = blueprint_hash(blueprint)
    blocks = _export_blocks(blueprint, variant)
    tiles = _export_block_entities(blueprint, variant)
    entities = _export_entities(blueprint, variant)
    block_count = _block_count(blocks)
    region = mcblueprint.Region("main", _ORIGIN, size)
    for position, state in blocks.items():
        region.set(*position, state)
    for position, tag in sorted(tiles.items()):
        region.set_tile_entity(*position, tag)
    region.entities = entities
    metadata = mcblueprint.Metadata(
        name=f"{name}-{variant}",
        author="retpack-farmbench",
        description=f"{variant} build; blueprint SHA256 {source_hash}",
        time_created=0,
        time_modified=0,
        # The library default is 3465. Litematica uses this root key, not DataVersion.
        extras={"MinecraftDataVersion": blueprint["data_version"]},
    )
    model = mcblueprint.Blueprint([region], metadata)
    litematic_root = _read_library_nbt(mcblueprint.save_bytes(model, "litematic"))
    # The library regards cave_air/void_air as blocks; Minecraft regards all
    # three air states as air. Preserve their states but not material counts.
    litematic_root["Metadata"]["TotalBlocks"] = nbtlib.Int(block_count)
    litematic = _write_nbt(litematic_root)
    vanilla = _vanilla_bytes(model, blueprint["data_version"])
    _verify_litematic(litematic, size, blocks, blueprint["data_version"], entities, tiles)
    _verify_vanilla(vanilla, size, blocks, blueprint["data_version"], entities, tiles)

    payloads = {
        "blueprint": ("blueprint.json", _json_bytes(blueprint)),
        "litematic": (f"{name}-{variant}.litematic", litematic),
        "nbt": (f"{name}-{variant}.nbt", vanilla),
        "materials": ("materials.json", _json_bytes(_materials(blocks, variant, source_hash, entities))),
        "placement": ("placement.md", _placement(blueprint, variant, f"{name}-{variant}.litematic", block_count).encode("utf-8")),
    }
    files = {
        key: {"path": str(output / filename), "sha256": hashlib.sha256(data).hexdigest()}
        for key, (filename, data) in payloads.items()
    }
    files["build"] = {"path": str(output / "build.json")}
    manifest = {
        "format": "retpack-farm-build-v1",
        "name": name,
        "blueprint_sha256": source_hash,
        "variant": variant,
        "minecraft": blueprint["minecraft"],
        "data_version": blueprint["data_version"],
        "bounds": {"min": origin, "size": list(size)},
        "translation": {"logical_origin": origin, "exported_origin": list(_ORIGIN), "offset": [-value for value in origin]},
        "dimensions": list(size),
        "block_count": block_count,
        "block_entity_count": len(tiles),
        "entity_count": sum(1 for _ in _walk_entities(entities)),
        "entity_root_count": len(entities),
        "entity_identity": "fresh; saved UUIDs and world-entity references are rejected; passenger trees are preserved",
        "artifact": {**files["litematic"], "format": "litematic"},
        "files": files,
        "verification": {"nbt_versions": True, "geometry_and_states": True, "mcblueprint_roundtrip": True,
                         "entity_nbt_and_poses": True, "block_entity_nbt_and_positions": True},
    }
    # Verify in memory first, then create the directory exclusively. build.json
    # is written last so incomplete I/O cannot look like a completed compilation.
    output.mkdir(parents=True, exist_ok=False)
    for filename, data in payloads.values():
        with (output / filename).open("xb") as handle:
            handle.write(data)
    with (output / "build.json").open("xb") as handle:
        handle.write(_json_bytes(manifest))
    return manifest
