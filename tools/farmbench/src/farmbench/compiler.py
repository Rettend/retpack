"""Compile validated blueprints without touching a Minecraft instance or world."""

from __future__ import annotations

from collections import Counter
import gzip
import hashlib
import io
import json
from pathlib import Path
from typing import Iterator

import mcblueprint
import nbtlib

from .blueprint import blueprint_hash, validate_blueprint


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


def _positions(size: tuple) -> Iterator[tuple[int, int, int]]:
    # Litematica's packed order: x fastest, then z, then y.
    for y in range(size[1]):
        for z in range(size[2]):
            for x in range(size[0]):
                yield x, y, z


def _block_count(blocks: dict) -> int:
    return sum(state.name not in _AIR_NAMES for state in blocks.values())


def _read_nbt(data: bytes) -> nbtlib.File:
    return nbtlib.File.from_fileobj(io.BytesIO(gzip.decompress(data)))


def _write_nbt(root: nbtlib.File) -> bytes:
    buffer = io.BytesIO()
    root.write(buffer)
    # Stable hashes: no gzip filename or wall-clock timestamp.
    return gzip.compress(buffer.getvalue(), mtime=0)


def _vanilla_bytes(model: mcblueprint.Blueprint, data_version: int) -> bytes:
    # mcblueprint 0.1.0 writes only non-air cells and does not supply DataVersion.
    vanilla = mcblueprint.Blueprint(
        model.regions,
        mcblueprint.Metadata(extras={"DataVersion": data_version}),
    )
    root = _read_nbt(mcblueprint.save_bytes(vanilla, "vanilla"))
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


def _verify_roundtrip(data: bytes, fmt: str, size: tuple, blocks: dict) -> None:
    decoded = mcblueprint.load_bytes(data, fmt)
    _require(len(decoded.regions) == 1, f"{fmt} region count")
    region = decoded.region()
    _require(region.position == _ORIGIN, f"{fmt} origin")
    _require(region.size == size, f"{fmt} dimensions")
    _require(dict(region.items()) == blocks, f"{fmt} roundtrip block states")


def _verify_litematic(data: bytes, size: tuple, blocks: dict, data_version: int) -> None:
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
    _verify_roundtrip(data, "litematic", size, blocks)


def _verify_vanilla(data: bytes, size: tuple, blocks: dict, data_version: int) -> None:
    root = _read_nbt(data)
    _require(
        isinstance(root.get("DataVersion"), nbtlib.Int)
        and int(root["DataVersion"]) == data_version,
        "vanilla DataVersion",
    )
    _require(tuple(int(value) for value in root["size"]) == size, "vanilla dimensions")
    palette = [_tag_state(entry) for entry in root["palette"]]
    seen = set()
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
    _require(len(seen) == size[0] * size[1] * size[2], "explicit vanilla clearance air")
    _verify_roundtrip(data, "vanilla", size, blocks)


def _json_bytes(value: dict) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")


def _materials(blocks: dict, variant: str, source_hash: str) -> dict:
    states = Counter(state for state in blocks.values() if state.name not in _AIR_NAMES)
    counts = Counter(state.name for state in blocks.values() if state.name not in _AIR_NAMES)
    return {
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
    block_count = _block_count(blocks)
    region = mcblueprint.Region("main", _ORIGIN, size)
    for position, state in blocks.items():
        region.set(*position, state)
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
    litematic_root = _read_nbt(mcblueprint.save_bytes(model, "litematic"))
    # The library regards cave_air/void_air as blocks; Minecraft regards all
    # three air states as air. Preserve their states but not material counts.
    litematic_root["Metadata"]["TotalBlocks"] = nbtlib.Int(block_count)
    litematic = _write_nbt(litematic_root)
    vanilla = _vanilla_bytes(model, blueprint["data_version"])
    _verify_litematic(litematic, size, blocks, blueprint["data_version"])
    _verify_vanilla(vanilla, size, blocks, blueprint["data_version"])

    payloads = {
        "blueprint": ("blueprint.json", _json_bytes(blueprint)),
        "litematic": (f"{name}-{variant}.litematic", litematic),
        "nbt": (f"{name}-{variant}.nbt", vanilla),
        "materials": ("materials.json", _json_bytes(_materials(blocks, variant, source_hash))),
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
        "artifact": {**files["litematic"], "format": "litematic"},
        "files": files,
        "verification": {"nbt_versions": True, "geometry_and_states": True, "mcblueprint_roundtrip": True},
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
