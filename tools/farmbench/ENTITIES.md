# Entities and block-entity data

Blueprints support the vanilla Minecraft 26.2 entity registry, with typed NBT for entity-specific state. This applies to mobs, vehicles, displays, projectiles and other entity types. Minecraft still decides whether a type and its supplied state can be instantiated; registry membership does not make player snapshots or other non-spawnable records placeable.

## Add an entity

Add an optional `entities` array alongside `blocks`:

```json
"entities": [
  {
    "id": "minecraft:armor_stand",
    "pos": [0.5, 2.0, 0.5],
    "rotation": [90.0, 0.0],
    "role": "farm",
    "nbt": "{}"
  }
]
```

Positions use the blueprint's logical coordinates and may be fractional. They must be inside its bounds. Rotation is `[yaw, pitch]` in degrees and defaults to `[0, 0]`. The export translates positions by the same bounds minimum as blocks.

`nbt` is a compound SNBT string, Minecraft's text representation of typed NBT. Use the field names and types expected by Minecraft 26.2. Byte, Float and Double suffixes retain their types during export; arbitrary payload fields are preserved, not interpreted as proof that the game accepts them.

The compiler owns entity type, position and rotation. Do not repeat `id`, `Pos`, `Rotation` or `Passengers` inside `nbt`. A `passengers` array uses the same entity schema recursively; passenger positions are logical blueprint coordinates too. Passenger roles inherit from their parent and cannot differ from it.

`role` defaults to `farm`. Entities marked `test`, including their passengers, are removed from survival exports.

## Identity and location

Placed entities receive fresh identities. Saved UUIDs and known references to existing world entities are rejected with an error rather than silently discarded. Passenger trees preserve riding relationships. External owners, leashes and other cross-entity UUID relationships are not currently remapped.

Hanging-entity anchors stored as `TileX`, `TileY`, `TileZ` or typed `block_pos` translate with the blueprint and normalize to Minecraft 26.2's `block_pos` integer array. Other positions buried in custom NBT remain as supplied; the compiler does not infer the meaning of every entity-specific field.

## Add block-entity state

A block that has a block entity can include `nbt`:

```json
{
  "pos": [0, 0, 0],
  "state": {
    "Name": "minecraft:barrel",
    "Properties": {"facing": "up", "open": "false"}
  },
  "role": "farm",
  "nbt": "{}"
}
```

Put container contents, sign text and other block-entity state in the compound using the game's current NBT fields. The block-entity type is inferred from the block; an explicit `id` must match. Coordinates are supplied by the compiler.

## Export and placement

Both `.litematic` and vanilla `.nbt` exports include entities and block entities in their respective formats. Build verification checks entity counts, poses and typed payloads independently, including passenger trees. Materials report entities separately from blocks; some entity types have no direct placement item, and creative spawn eggs are not survival crafting costs.

For manual Litematica pastes, enable entity placement and inspect the actual entities. Litematica's block verifier does not verify them, repeated pastes can create duplicates, and command-based pasting may lose entity-specific NBT. Automated companion placement uses the vanilla structure data directly and reports its runtime verification scope in the result.
