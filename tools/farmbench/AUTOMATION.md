# Automated lab jobs

The Fabric lab companion runs a job inside your existing Minecraft world. Farmbench prepares the job files; the companion places or checks the designs, assigns separate counter colours, advances the simulation and saves measured results.

Use a Creative lab world with commands enabled. The benchmark protocol measures natural-growth bamboo; the blueprint format can represent other builds and entity types independently of that protocol.

## Install the companion

Build the mod using the [companion build instructions](../farmbench-mod/README.md). With Minecraft closed, run this from the repository root:

```powershell
pwsh -File scripts/install-farmbench.ps1 -Jar dist/farmbench-mod/farmbench-0.1.0.jar -Destination "$env:APPDATA\retpack\worldgen-test"
```

Use your actual instance path if different. Restart Minecraft to load the mod. The installer backs up a previous companion and updates Retpack's installed-file checksums. Reinstall the locally built companion after a standard pack update removes it.

## Prepare a job

From the repository root:

```powershell
uv run --project tools/farmbench farmbench job blueprints/bamboo_micro_2cell_v1.json blueprints/bamboo_micro_2cell_front_tray_v2.json --id bamboo-ab-01 --repeats 3
```

The selected Legacy Launcher instance is the default destination. Use `--instance <folder>` for another instance. Each job has its own folder under `config/farmbench/jobs/`; use a new job ID for a changed design or test setup.

The two source blueprints can both declare lime wool. Job preparation assigns distinct counter colours to the compiled test copies and retains both the source and instantiated blueprint hashes. You don't need to edit the source colours.

Other loaded farms must not feed those colours. The runner reports conflicting hopper positions; remove their counter connection or use a fresh Creative test world.

## Start in Minecraft

Stand beside an empty area and run:

```mcfunction
/farmbench start bamboo-ab-01
```

Automatic placement uses separated test bays starting above your position. The full placement areas must be clear; an existing build is not silently overwritten. The companion loads the test chunks, places the structures and their entities, checks the setup, then runs the paired trials.

Both farms are measured during the same simulation window. The runner warms up, drains, resets the assigned counters, runs the measurement, drains again and collects raw item counts. Drain checks inspect hopper and hopper-minecart inventories; items still in transit stop the run with a request for a longer drain. Other container types and stranded dropped items are not covered by that check. The runner records actual elapsed game ticks and stops on errors rather than reporting an interrupted run as complete.

```mcfunction
/farmbench status
/farmbench stop
```

`stop` cancels the active run. Results and failures are written under `config/farmbench/results/`. Completed test builds remain available for inspection. Use `/farmbench cleanup` to remove the automatically placed job's test bays and owned entities when you are finished. Cleanup tracks placements in the current world session; run it before closing the world.

Stay near every bay in the same dimension, with the planned simulation distance selected. Trials use the same placed farms continuously; they are not rebuilt between repeats. For a fresh placement, clean up the previous bays and start again.

## Use a build you placed yourself

Prepare an `existing` job with explicit placement coordinates. Coordinates are the schematic's minimum exported corner, not a plant or output hopper. The companion checks the actual builds and configures their declared wool outputs instead of pasting another copy.

For example, with schematic corners at `[100,70,-20]` and `[120,70,-20]`:

```powershell
uv run --project tools/farmbench farmbench job blueprints/bamboo_micro_2cell_v1.json blueprints/bamboo_micro_2cell_front_tray_v2.json --id bamboo-existing --mode existing --origin 100 70 -20 --at 100 70 -20 --at 120 70 -20
```

Supply one `--at` per design. Existing-mode cleanup does not remove your pre-existing builds.

## Read the results

```powershell
uv run --project tools/farmbench farmbench results "$env:APPDATA\retpack\worldgen-test\config\farmbench\results\<result-file>.json"
```

The summary reports per-design trial rates, means and sample variation, and paired differences where available. Incomplete or failed jobs remain diagnostic records. Runtime measurements, placement checks and operator statements are distinct evidence; successful counters alone do not prove a farm collects every produced item.

A paired test gives the farms equal time, not identical random growth events.
