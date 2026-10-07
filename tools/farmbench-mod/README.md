# Farmbench companion

Fabric companion for Minecraft **26.2**, Fabric Loader **0.19.5**, Fabric API **0.161.0+26.2**, Carpet **26.2+v260616**, and Java **25**. It runs local natural-growth bamboo jobs prepared by [farmbench](../farmbench/AUTOMATION.md). No network service is used.

Build with a Java 25 JDK selected through `JAVA_HOME`:

```powershell
.\gradlew.bat build --console=plain
```

On Linux/macOS, use `sh gradlew build` (requires `curl` and `sha256sum` or `shasum`). The launchers download the ignored wrapper jar from a pinned Fabric template commit and verify its SHA256 before use. The Gradle distribution is also checksum-pinned. No binary needs to be committed.

Tests run as part of the build. The runnable mod is `dist/farmbench-mod/farmbench-0.1.0.jar` at the repository root; build intermediates and reports are also under `dist/farmbench-mod/`. The wrapper uses Gradle 9.7.1 and Loom 1.18.3, from the Fabric 26.2 template and published Maven release. Carpet is pinned by the repository's Modrinth release ID, `bGrLxJ8v`.

Validate a real Python-generated job without launching Minecraft or changing any instance files:

```powershell
.\gradlew.bat validateJob "-Pjob=C:\path\to\job.json" --console=plain
```

This checks the Java job schema and every structure SHA256. It does not claim to verify structure NBT contents, placement, or runtime measurements.

With the game closed, install from the repository root:

```powershell
.\scripts\install-farmbench.ps1 -Jar dist/farmbench-mod/farmbench-0.1.0.jar -Destination "$env:APPDATA\retpack\worldgen-test"
```

The shared instance can contain both survival and Creative lab worlds. Run benchmarks only in a backed-up Creative lab world, never in a survival world.

Commands require command permission and starting requires a Creative player:

- `/farmbench start <job-id>` reads `config/farmbench/jobs/<job-id>/job.json`, checks artifact hashes, calibrates frozen advancement, and starts the job.
- `/farmbench status` shows the current phase or last result.
- `/farmbench stop` cancels the job and restores settings.
- `/farmbench cleanup` removes placements tracked in the current server session. It does not remove existing-mode builds or unrelated entities. After restarting the world, earlier placements are not tracked by this command.

Stay in the same dimension and near every bay. Every bay chunk centre must remain within 127 horizontal blocks of the initiating player, and all bay chunks must be loaded and entity-ticking. The runner rejects wider layouts rather than relying on tickets alone. Set the job's simulation distance before starting.

Each replicate runs all designs concurrently, warms up, drains with random ticks disabled, resets only assigned counter colours, measures growth, then drains again with random ticks disabled. Farms and entities continue across replicates; they are not rebuilt. Rates use measured growth ticks, excluding drainage. Other items in a counter fail the job. Collision checks inspect block-ticking chunks in all dimensions before phases and every 20 server loops; this does not continuously inspect unloaded chunks.

Results are saved to `config/farmbench/results/<job-id>-<run-id>.json`. Checkpoints remain failed/incomplete until every phase and restoration succeeds. A cancelled job or server stop never produces a completed comparison. A process crash leaves the last incomplete checkpoint; it cannot guarantee restoration of settings. Keep a backup of the Creative lab world.

The runner records actual world game-time deltas, raw Carpet item counts, calibration, mod versions, identities, placement verification, and limitations. It does not establish lighting, item-loss completeness, intended farm mechanics, or statistical convergence. Disabling random ticks does not disable scheduled ticks or entity-based production, so this runner rejects non-bamboo benchmark items.

Compilation and logic tests are not an in-game validation. First validate short runs, cancellation, restoration, placement, counters, and player-range failures in the lab before trusting long measurements.
