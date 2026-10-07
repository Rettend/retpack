# Farm test commands

For natural-growth bamboo farms in Minecraft 26.2 with Carpet. Use a Creative test world with commands enabled. These defaults match farmbench: 1,200 warm-up ticks, 72,000 measured growth ticks, and 200 ticks for each drain.

**Run commands individually. Wait for each sprint to finish before the next command.** Keep the world frozen between phases; a sprint started frozen returns to frozen when it finishes.

## Set up the farms

- For one farm, use **lime wool** and skip every blue-counter command below.
- For side-by-side testing, use **A → lime wool** and **B → blue wool**. Give each its own output hopper. No other farm may feed either colour.
- In B's JSON, change the sink block to `minecraft:blue_wool`, `ports[].counter` to `blue`, and `benchmark.counter` to `blue`, then rebuild. Changing only the world would leave the recorded blueprint describing a different test.
- Keep both farms close to the player and clear of each other's redstone and dropped items. Use the same lighting and simulation distance (the examples use **12**).
- Verify the real blocks with Litematica, especially the bottom hoppers, counter wool and clearance. Start with empty collection inventories and no old dropped items.

Generate a plan for each blueprint/build using the README's `farmbench plan` command. Their timing and conditions must match. Run the **shared sequence below once**, rather than running each plan's commands separately.

For faster simulation, turn off shaders and WorldGen and lower the FPS cap while testing. Keep simulation distance and growth settings unchanged between comparisons.

## Check tick accounting once

Farmbench currently compensates for one startup tick when sprinting from a settled freeze. This was inferred from the installed game code; verify it in your running setup before relying on exact rates, and repeat after relevant game/mod updates.

```mcfunction
/tick freeze
/tick query
/gamerule random_tick_speed 0
/time query gametime
```

Read `/time query gametime` again after a short wait; it should be unchanged while frozen. Then run `/tick step 1`, wait for the step to finish, and read gametime again: it should have advanced by one.

Now note a fresh starting gametime and run:

```mcfunction
/tick sprint 99
```

Wait for completion, then:

```mcfunction
/tick query
/time query gametime
```

Subtract the starting gametime from the final reading. The current convention expects a difference of **100**, with the world still frozen. Repeat once. If the difference is different or inconsistent, keep the readings and correct the timing convention before comparing results. Do this before warm-up, not during a measured run. Preparation below restores growth to 3.

## 1. Prepare and warm up

Note the world's original difficulty, growth rule and tick state if you want to restore them afterward. Stay near both farms for the whole test.

```mcfunction
/tick freeze
/tick query
/carpet hopperCounters true
/difficulty normal
/gamerule random_tick_speed 3
/tick sprint 1199
```

Wait for completion. This advances 1,200 ticks under the calibrated convention.

## 2. Drain warm-up output

```mcfunction
/gamerule random_tick_speed 0
/tick sprint 199
```

Wait for completion. Check that pending harvests have finished and the transport inventories have emptied. If items are still in transit, use a longer `drain_ticks` in both blueprints and generate new plans. Inspect drops stranded above bamboo separately: delivered counts alone cannot tell you the loss percentage.

## 3. Reset both counters and measure

```mcfunction
/counter lime reset
/counter blue reset
/counter lime
/counter blue
/gamerule random_tick_speed 3
/tick sprint 71999
```

Confirm the counters are empty before starting the sprint. Wait for the sprint completion message. Don't add bamboo, move away, unfreeze, or send `/tick freeze` during it; freezing during a sprint cancels the remainder.

## 4. Drain and read the results

```mcfunction
/gamerule random_tick_speed 0
/tick sprint 199
```

Wait for completion, then:

```mcfunction
/tick query
/counter lime
/counter blue
```

Check that transport finished, then record the **raw bamboo counts**, not Carpet's displayed hourly estimates. For 72,000 measured growth ticks, the count equals items per simulated hour. The growth-disabled drain is excluded from that denominator.

## 5. Save the results

Save one result against each farm's own plan. Use the same trial label in both filenames or notes so the pair stays together. For example, after assigning your actual paths and counts in PowerShell:

```powershell
uv run --project tools/farmbench farmbench record $planA --items $countA --completed --placement-verified --notes "Paired trial 01, lime, bay A" --out $resultA
uv run --project tools/farmbench farmbench record $planB --items $countB --completed --placement-verified --notes "Paired trial 01, blue, bay B" --out $resultB
```

`--completed` means every phase and transport check finished; `--placement-verified` means you checked the actual build. Omit those flags when they aren't true, and record the problem in notes. In v1, `comparable: true` reflects these operator statements and the recorded conditions; it is not automatic verification of the running world.

The current CLI stores each result separately. The trial label is the manual link between A and B; paired-run orchestration and statistics are not implemented yet.

## Repeat or finish

For another trial, repeat from preparation with new result filenames. Keep geometry and settings fixed. Random growth is independent between the farms: running them together gives equal time, not identical growth events. Start with several paired one-hour trials, then use longer matching plans when you need to distinguish small differences. Compare total output alongside plant count, materials and bounding volume.

To return to normal growth and ticking after recording:

```mcfunction
/gamerule random_tick_speed 3
/tick unfreeze
```

Restore any other settings you changed. Counter wool continues to destroy items while `hopperCounters` is enabled; a barrel is the normal survival output.

## If something goes wrong

- **`/counter` disappeared after reopening:** run `/carpet hopperCounters true` again. Its “Change permanently?” link saves the rule for this test world.
- **No bamboo grows:** check `/gamerule random_tick_speed`; draining leaves it at `0`. Also check whether the world is frozen.
- **A crash or interrupted sprint:** restart the whole trial. Do not combine counts from before and after a restart.
- **Bottom blocks are missing:** the default paste fills air and can skip existing ground. Clear the full box or move it above ground, then paste and verify again.
- **Drops remain on a stalk:** inspect collection with growth disabled and the world unfrozen. Reset the setup before measuring again; clearing a jam by hand during a trial changes the experiment.
