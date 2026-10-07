# Companion protocol

Target: Minecraft 26.2 / Fabric 0.19.5 / Java 25 / Carpet 26.2+v260616. A local Fabric mod, no network service. Python prepares files; one in-game command starts a job in a Creative lab world with commands enabled.

Package `me.rettend.farmbench`. Fabric mod ID `farmbench`. Java module at `tools/farmbench-mod/`. Job folders: `<instance>/config/farmbench/jobs/<job-id>/job.json` and vanilla `.nbt` structures. Results: `<instance>/config/farmbench/results/<job-id>-<run-id>.json`. Commands `/farmbench start <job-id>`, `/farmbench stop`, `/farmbench status`, `/farmbench cleanup`. Cleanup is explicit, limited to placed job bays/tagged entities; never a global entity kill.

Job v1:

```json
{
  "format": "retpack-farm-job-v1", "id": "bamboo-ab", "minecraft": "26.2", "data_version": 4903,
  "mode": "place", "origin": null,
  "repeats": 3, "warmup_ticks": 1200, "run_ticks": 72000, "drain_ticks": 200,
  "conditions": {"random_tick_speed": 3, "simulation_distance": 12, "difficulty": "normal", "dimension": "minecraft:overworld"},
  "designs": [
    {"id": "a", "name": "bamboo", "blueprint_sha256": "...", "instantiated_blueprint_sha256": "...", "artifact_sha256": "...", "structure": "a.nbt", "size": [3,5,4], "offset": [0,0,0], "counter": "lime", "item": "minecraft:bamboo", "entity_count": 0,
     "ports": [{"position": [1,0,1], "direction": "east"}]}
  ]
}
```

All coordinates in job design data and NBT are normalized export coordinates, not logical source coordinates. Default origin: player block position plus [0,2,0]. Explicit origin is absolute. CLI assigns separated x offsets and unique colours (lime, blue, remaining wool colours), recolours derived lab blueprint sinks before compilation. Source hash and instantiated hash remain separate. Structure SHA256 checked before loading.

Mode `place`: all target boxes must be empty before mutation (air/fluid distinction: only air qualifies). Place blocks, block entities and entities from supplied versioned NBT, verify blocks/states initially, entity counts/types/poses as possible. Default rejects overlapping boxes and unrelated existing entities in boxes. Chunk tickets keep bays loaded while job active. Mode `existing`: explicit origin required; inspect expected structures, allow replacement of wool at declared sinks to assigned colours; do not spawn/paste anything else. Record verification scope honestly, including dynamic blocks/entities limitations.

`PlacementService` exposes:
`PlacementService(MinecraftServer server, ServerLevel level, JsonObject job, BlockPos origin, String runId)`;
`void prepare()` (preflight all designs, then place/verify); `void verify()`; `void cleanup()`; `void release()` (release tickets without deleting world builds); `JsonObject report()`; `BlockPos origin()`.
The runner reports placement errors and aborts. Existing-mode cleanup must not delete pre-existing builds. Group-specific entity tags and tracked UUIDs distinguish placed entities from unrelated entities.

The runner uses server-thread Fabric tick events and world game time for phases. It calibrates frozen advancement, then verifies elapsed ticks for each phase. It restores the original tick state, growth rule, difficulty and hopperCounters setting on completion or cancellation. A hard process crash leaves an incomplete checkpoint and cannot guarantee restoration. Counter readings come directly from Carpet. Each repeat warms up, drains, resets assigned counters, measures the designs concurrently, drains again and records per-design counts. Rates exclude growth-disabled drainage. Results record mod versions, blueprint/artifact identities and verification scope.

Blueprint entity and block-entity fields are documented in [ENTITIES.md](ENTITIES.md). Entity counts include passengers. Structures use the vanilla `{pos, blockPos, nbt}` representation. Absent optional entity fields preserve existing v1 hashes.

`farmbench job BLUEPRINT [BLUEPRINT ...] --id NAME [--instance PATH] [--origin X Y Z] [--mode place|existing] [--repeats N]` defaults to Legacy's selected instance. Optional repeated `--at X Y Z` arguments specify each design's absolute origin. Preparation validates matching conditions/timing, assigns counters and offsets, then publishes the structures and job without replacing an existing job. `job.json` is published last.

`farmbench results PATH` summarizes result format `retpack-farm-job-result-v1`: status `completed`, `failed` or `cancelled`; embedded `job`; origin; environment; verification; calibration; errors; timestamps; and `trials: [{index, measured_ticks, designs: [{id, counter, item, items, items_per_hour}]}]`. Trial indices run from 1 through `job.repeats`. Rates are recomputed from counts and measured ticks. Missing, off-plan or failed trials are diagnostic; intact paired samples provide means, sample standard deviations and B-minus-A differences.

Build output goes under ignored `dist/farmbench-mod/`. Job preparation does not launch Minecraft or edit worlds; the companion executes jobs only after the in-game start command.
