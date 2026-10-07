# farmbench

Turn a farm blueprint into a Litematica schematic, follow a benchmark in Minecraft, and save the measured output. The included example is the 19-block bamboo cell tested in Retpack.

Requires [uv](https://docs.astral.sh/uv/getting-started/installation/) and Python 3.11 or newer. Run these commands from the repository root. `uv` installs the locked Python dependencies in the tool's own environment.

## Build the example

```powershell
uv run --project tools/farmbench farmbench validate tools/farmbench/examples/bamboo_micro_v1.json
uv run --project tools/farmbench farmbench build tools/farmbench/examples/bamboo_micro_v1.json --out dist/farmbench/bamboo-lab
```

The output directory contains a `.litematic`, a vanilla `.nbt`, the resolved blueprint JSON, a block-count material list, placement instructions and a build manifest with checksums. Use a new directory for each revision so previous builds remain available.

Copy the `.litematic` into your game's `schematics` folder. In a Creative world with commands enabled, load it through **M → Load Schematics** and create a placement. Follow the generated `placement.md` to paste it and check the result.

Place the **entire bounding box above ground or in cleared space**. Litematica's default paste mode fills air; existing ground can leave the bottom hopper and counter wool missing. Verify the real blocks before measuring production.

## Plan and measure a run

Use the game directory that has both Litematica and Carpet installed:

```powershell
uv run --project tools/farmbench farmbench plan tools/farmbench/examples/bamboo_micro_v1.json --build dist/farmbench/bamboo-lab/build.json --instance "$env:APPDATA\retpack\worldgen-test" --out dist/farmbench/bamboo-run-01
```

Open `dist/farmbench/bamboo-run-01/benchmark.md` and follow its steps in order. It defines placement checks, warm-up, counter reset, a fixed number of simulation ticks, and collection drainage. Wait for each sprint to finish before issuing the next command.

The plan captures installed Minecraft, Fabric and mod versions/checksums, selected saved settings, and evidence of disabled modules from the latest log. Check the actual world conditions against the plan: installed files and saved options cannot establish the live world's settings or prove that a schematic was pasted correctly.

After completing the plan, read the **raw bamboo count** from `/counter lime`. For example, if the counter reports 18 bamboo:

```powershell
uv run --project tools/farmbench farmbench record dist/farmbench/bamboo-run-01/plan.json --items 18 --completed --placement-verified --notes "No visible collection failures" --out dist/farmbench/bamboo-run-01/result.json
```

Use your measured count, not `18`. Only supply `--completed` and `--placement-verified` when those statements are true. Other runs remain useful diagnostic records, but are marked unsuitable for comparison. Keep observations and failures in `--notes`.

Rates use **simulated ticks at 20 ticks/second**, not elapsed computer time or Carpet's rounded chat estimate. Random growth varies between runs; compare several runs with the same conditions. Delivered item counts alone cannot measure an item-loss percentage.

## Export for survival

```powershell
uv run --project tools/farmbench farmbench build tools/farmbench/examples/bamboo_micro_v1.json --variant survival --out dist/farmbench/bamboo-survival
```

The lab blueprint routes the hopper into lime wool, which Carpet uses to count and destroy items. The survival export replaces that test sink with the port's specified storage block and removes other test equipment. Its material list describes placed blocks, not crafting ingredients or iron cost.

## Edit a blueprint

Start with `examples/bamboo_micro_v1.json`. Each block has a logical position, a Minecraft block state and a `farm` or `test` role. The bounding box explicitly records its minimum coordinate, so negative positions are unambiguous. The export maps that minimum to schematic position `[0, 0, 0]`; `build.json` records the translation.

Ports describe the output hopper, its direction, counter colour and survival storage replacement. Conditions and benchmark timing travel with the design. Empty cells inside the bounds are clearance space; clear them in the world before testing.

Validation checks the supported version's block names/properties, positions, duplicate blocks, bounds and hopper-to-counter connections. The first compiler accepts bounding boxes up to 1,000,000 cells, including air. Game testing establishes whether the mechanism actually works. Blueprint content hashes identify the validated design used for a result; build hashes identify the exported file.

The bundled vanilla 26.2 registry contains 1,196 blocks and 1,537 items, derived from a [pinned mcmeta summary](https://github.com/misode/mcmeta/tree/711a353b47d84e6cb592a1b72f682e5f44759284). Validation works offline. The first benchmark protocol is for natural-growth bamboo farms with one counter output; block entities with custom contents and other farm-specific test protocols are future additions.

## Development

```powershell
uv run --project tools/farmbench pytest tools/farmbench/tests
```
