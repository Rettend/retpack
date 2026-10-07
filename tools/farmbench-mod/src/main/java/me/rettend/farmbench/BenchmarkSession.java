package me.rettend.farmbench;

import com.google.gson.JsonArray;
import com.google.gson.JsonObject;
import java.nio.file.Path;
import java.time.Instant;
import java.util.ArrayDeque;
import java.util.Deque;
import java.util.HashSet;
import java.util.Set;
import java.util.UUID;
import java.util.function.Consumer;
import net.fabricmc.loader.api.FabricLoader;
import net.minecraft.SharedConstants;
import net.minecraft.core.BlockPos;
import net.minecraft.core.registries.BuiltInRegistries;
import net.minecraft.resources.Identifier;
import net.minecraft.server.MinecraftServer;
import net.minecraft.server.level.ServerLevel;
import net.minecraft.server.level.ServerPlayer;
import net.minecraft.world.level.ChunkPos;
import net.minecraft.world.level.storage.LevelResource;

/** All world access and transitions run on the server thread; no waits or sleeps. */
final class BenchmarkSession {
    private enum Advancement { HOLD, STEP, SPRINT, CALIBRATE_SPRINT }
    private record Operation(String name, int ticks, int randomTickSpeed, Advancement advancement,
                             Consumer<JsonObject> completed) {}

    private final MinecraftServer server;
    private final ServerLevel level;
    private final UUID playerId;
    private final JsonObject job;
    private final JsonObject conditions;
    private final String runId = Instant.now().toString().replaceAll("[-:.]", "") + "-" + UUID.randomUUID().toString().substring(0, 8);
    private final PlacementService placement;
    private final CounterService counters;
    private final ServerSettings settings;
    private final ResultStore store;
    private final JsonObject result = new JsonObject();
    private final JsonObject calibration = new JsonObject();
    private final JsonArray calibrationPhases = new JsonArray();
    private final JsonArray trials = new JsonArray();
    private final JsonArray errors = new JsonArray();
    private final JsonArray phases = new JsonArray();
    private final Deque<Operation> queue = new ArrayDeque<>();
    private final Set<ChunkPos> bayChunks = new HashSet<>();
    private final Consumer<String> notify;
    private Operation active;
    private PhaseGuard guard;
    private long phaseStart;
    private int holdLoops;
    private int sprintOverhead = -1;
    private int expectedRandomTicks;
    private boolean settling = true;
    private boolean prepared;
    private boolean finished;
    private long checks;
    private double maximumPlayerDistance;
    private JsonObject trial;
    private long measuredTicks;

    BenchmarkSession(MinecraftServer server, ServerPlayer player, JsonObject job, Path config, Consumer<String> notify) {
        this.server = server;
        this.level = player.level();
        this.playerId = player.getUUID();
        this.job = job.deepCopy();
        this.conditions = job.getAsJsonObject("conditions");
        this.notify = notify;
        validateEnvironment(player);
        BlockPos origin;
        if (job.get("origin").isJsonNull()) origin = player.blockPosition().offset(0, 2, 0);
        else {
            int[] xyz = JobLoader.vector(job.get("origin"), "origin", -30_000_000, 30_000_000);
            origin = new BlockPos(xyz[0], xyz[1], xyz[2]);
        }
        for (var element : job.getAsJsonArray("designs")) {
            JsonObject design = element.getAsJsonObject();
            int[] offset = JobLoader.vector(design.get("offset"), "offset", -30_000_000, 30_000_000);
            int[] size = JobLoader.vector(design.get("size"), "size", 1, 512);
            BlockPos min = origin.offset(offset[0], offset[1], offset[2]);
            BlockPos max = min.offset(size[0] - 1, size[1] - 1, size[2] - 1);
            for (int x = Math.floorDiv(min.getX(), 16); x <= Math.floorDiv(max.getX(), 16); x++)
                for (int z = Math.floorDiv(min.getZ(), 16); z <= Math.floorDiv(max.getZ(), 16); z++) bayChunks.add(new ChunkPos(x, z));
        }
        assertPlayer(false);
        settings = new ServerSettings(server, level.getGameRules());
        placement = new PlacementService(server, level, this.job, origin, runId);
        counters = new CounterService(server, level, this.job, origin);
        store = new ResultStore(config, JobLoader.text(job, "id"), runId);
        initializeResult(origin);
    }

    void start() {
        // A checkpoint starts failed/incomplete, so a process crash cannot produce a success.
        checkpoint();
        settings.prepare(conditions);
        expectedRandomTicks = 0;
        counters.assertExclusive();
        queue.add(new Operation("frozen_hold", 3, 0, Advancement.HOLD, phase -> calibrationPhases.add(phase)));
        queue.add(new Operation("frozen_step", 4, 0, Advancement.STEP, phase -> calibrationPhases.add(phase)));
        queue.add(new Operation("step_settle", 2, 0, Advancement.HOLD, phase -> calibrationPhases.add(phase)));
        queue.add(new Operation("frozen_sprint_probe", 7, 0, Advancement.CALIBRATE_SPRINT, phase -> {
            sprintOverhead = (int) (phase.get("actual_ticks").getAsLong() - 7);
            if (sprintOverhead < 0 || sprintOverhead > 1) throw new IllegalStateException("Unsupported frozen-sprint advancement");
            calibration.addProperty("sprint_startup_ticks", sprintOverhead);
            calibrationPhases.add(phase);
        }));
        queue.add(new Operation("frozen_sprint_confirmation", 8, 0, Advancement.SPRINT, phase -> {
            calibrationPhases.add(phase);
            calibration.addProperty("status", "passed");
            placement.prepare();
            placement.verify();
            counters.registerTransportCarts(placement.report());
            counters.verifyPorts();
            counters.assertExclusive();
            // Ticket status promotion is processed by subsequent server loops, even frozen.
            queue.add(new Operation("placement_ticket_settle", 10, 0, Advancement.HOLD, ignored -> {
                assertPlayer(true);
                prepared = true;
                enqueueTrials();
                notify.accept("Calibration passed. Running paired trials for " + JobLoader.text(job, "id") + ".");
            }));
        }));
        checkpoint();
    }

    void beforeTick() {
        if (finished) return;
        checkRuntimeInvariants();
        if (prepared && ++checks % 20 == 0) counters.assertExclusive();
    }

    private void checkRuntimeInvariants() {
        settings.verify(expectedRandomTicks, conditions);
        assertPlayer(prepared);
        counters.assertIntegrity();
        if (prepared) counters.verifyPorts();
        if (active != null) {
            var tick = server.tickRateManager();
            if (active.advancement == Advancement.HOLD && (!tick.isFrozen() || tick.isSteppingForward() || tick.isSprinting()))
                throw new IllegalStateException("Frozen hold interrupted by a tick-state change");
            if (active.advancement == Advancement.STEP && !tick.isFrozen()) throw new IllegalStateException("Frozen step was unfrozen");
            if ((active.advancement == Advancement.SPRINT || active.advancement == Advancement.CALIBRATE_SPRINT)
                && tick.isSteppingForward()) throw new IllegalStateException("Sprint interrupted by a tick step");
        }
    }

    void afterTick() {
        if (finished) return;
        atEndBoundary(this::checkRuntimeInvariants, this::advanceAfterTick);
    }

    static void atEndBoundary(Runnable invariants, Runnable transition) {
        invariants.run();
        transition.run();
    }

    private void advanceAfterTick() {
        if (settling) {
            if (!server.tickRateManager().isFrozen()) throw new IllegalStateException("Initial freeze was interrupted");
            if (server.tickRateManager().runsNormally()) return;
            settling = false;
            nextOperation();
            return;
        }
        if (active == null) return;
        var tick = server.tickRateManager();
        boolean complete = switch (active.advancement) {
            case HOLD -> ++holdLoops >= active.ticks;
            case STEP -> !tick.isSteppingForward();
            case SPRINT, CALIBRATE_SPRINT -> !tick.isSprinting();
        };
        long actual = guard.observe(level.getGameTime(), System.nanoTime(), complete && active.advancement != Advancement.CALIBRATE_SPRINT);
        if (!complete) return;
        if (!tick.isFrozen()) throw new IllegalStateException("Phase did not return to frozen state");
        JsonObject reading = new JsonObject();
        reading.addProperty("name", active.name);
        reading.addProperty("advancement", active.advancement.name().toLowerCase(java.util.Locale.ROOT));
        reading.addProperty("requested_ticks", active.advancement == Advancement.HOLD ? 0
            : active.advancement == Advancement.CALIBRATE_SPRINT ? active.ticks
            : active.advancement == Advancement.SPRINT ? PhaseGuard.sprintRequest(active.ticks, sprintOverhead) : active.ticks);
        reading.addProperty("target_ticks", active.advancement == Advancement.HOLD ? 0
            : active.advancement == Advancement.CALIBRATE_SPRINT ? active.ticks + 1 : active.ticks);
        if (active.advancement == Advancement.CALIBRATE_SPRINT) {
            reading.addProperty("accepted_min_ticks", active.ticks);
            reading.addProperty("accepted_max_ticks", active.ticks + 1);
        }
        if (active.advancement == Advancement.HOLD) reading.addProperty("frozen_server_loops", holdLoops);
        reading.addProperty("actual_ticks", actual);
        reading.addProperty("start_game_time", phaseStart);
        reading.addProperty("end_game_time", level.getGameTime());
        reading.addProperty("random_tick_speed", active.randomTickSpeed);
        reading.addProperty("finished_frozen", true);
        phases.add(reading);
        active.completed.accept(reading);
        active = null;
        checkpoint();
        nextOperation();
    }

    private void nextOperation() {
        if (queue.isEmpty()) { finish("completed", null); return; }
        Operation operation = queue.removeFirst();
        var tick = server.tickRateManager();
        if (!tick.isFrozen() || tick.isSprinting() || tick.isSteppingForward()) throw new IllegalStateException("Cannot start phase outside settled freeze");
        // A last step still has runsNormally=true until the following server-loop tick.
        if (tick.runsNormally() && operation.advancement != Advancement.HOLD) {
            queue.addFirst(operation);
            operation = new Operation("phase_settle", 1, 0, Advancement.HOLD, ignored -> {});
        }
        active = operation;
        settings.randomTicks(operation.randomTickSpeed);
        expectedRandomTicks = operation.randomTickSpeed;
        settings.verify(expectedRandomTicks, conditions);
        assertPlayer(prepared);
        counters.assertExclusive();
        phaseStart = level.getGameTime();
        long expected = switch (operation.advancement) {
            case HOLD -> 0;
            case CALIBRATE_SPRINT -> operation.ticks + 1L;
            default -> operation.ticks;
        };
        guard = new PhaseGuard(phaseStart, expected, System.nanoTime());
        holdLoops = 0;
        switch (operation.advancement) {
            case HOLD -> { }
            case STEP -> {
                if (!tick.stepGameIfPaused(operation.ticks)) throw new IllegalStateException("Server rejected frozen step");
            }
            case CALIBRATE_SPRINT -> tick.requestGameToSprint(operation.ticks);
            case SPRINT -> tick.requestGameToSprint(PhaseGuard.sprintRequest(operation.ticks, sprintOverhead));
        }
        result.addProperty("phase", operation.name);
        checkpoint();
    }

    private void enqueueTrials() {
        for (PhasePlan.Stage stage : PhasePlan.paired(JobLoader.integer(job, "repeats", 1, 100),
            JobLoader.integer(job, "warmup_ticks", 0, 10_000_000), JobLoader.integer(job, "run_ticks", 1, 10_000_000),
            JobLoader.integer(job, "drain_ticks", 1, 1_000_000), JobLoader.integer(conditions, "random_tick_speed", 1, 4096))) {
            Advancement advancement = stage.ticks() == 0 ? Advancement.HOLD : stage.ticks() == 1 ? Advancement.STEP : Advancement.SPRINT;
            queue.add(new Operation("trial_" + stage.trial() + "_" + stage.kind().name().toLowerCase(java.util.Locale.ROOT),
                stage.ticks(), stage.randomTickSpeed(), advancement, reading -> trialStageFinished(stage, reading)));
        }
    }

    private void trialStageFinished(PhasePlan.Stage stage, JsonObject reading) {
        switch (stage.kind()) {
            case WARMUP -> {
                trial = new JsonObject();
                trial.addProperty("index", stage.trial());
                trial.addProperty("paired", true);
                trial.addProperty("status", "incomplete");
                trial.add("phases", new JsonArray());
                trials.add(trial);
            }
            case PRE_DRAIN -> {
                JsonObject drain = counters.inspectDrain("trial_" + stage.trial() + "_pre_drain");
                trial.add("pre_drain_verification", drain);
                afterDrainVerification(drain, () -> {
                    counters.reset();
                    trial.addProperty("counter_reset_game_time", level.getGameTime());
                });
            }
            case MEASURE -> {
                measuredTicks = reading.get("actual_ticks").getAsLong();
                trial.addProperty("measured_ticks", measuredTicks);
                trial.addProperty("measurement_start_game_time", reading.get("start_game_time").getAsLong());
                trial.addProperty("measurement_end_game_time", reading.get("end_game_time").getAsLong());
                trial.add("before_drain", counters.read(measuredTicks));
            }
            case POST_DRAIN -> {
                placement.verify();
                JsonObject drain = counters.inspectDrain("trial_" + stage.trial() + "_post_drain");
                trial.add("post_drain_verification", drain);
                afterDrainVerification(drain, () -> {
                    trial.add("designs", counters.read(measuredTicks));
                    trial.addProperty("post_drain_ticks", reading.get("actual_ticks").getAsLong());
                    trial.addProperty("post_drain_growth_disabled", true);
                    trial.addProperty("status", "completed");
                });
            }
        }
        trial.getAsJsonArray("phases").add(reading.deepCopy());
    }

    static void afterDrainVerification(JsonObject drain, Runnable action) {
        CounterService.requireDrained(drain);
        action.run();
    }

    void finish(String status, String reason) {
        if (finished) return;
        finished = true;
        if (reason != null) errors.add(reason);
        JsonArray restoreFailures = new JsonArray();
        for (String failure : settings.restore()) { errors.add(failure); restoreFailures.add(failure); }
        try { placement.release(); } catch (RuntimeException e) { errors.add("Cannot release bay tickets: " + e.getMessage()); }
        if (status.equals("completed") && !errors.isEmpty()) status = "failed";
        result.addProperty("status", status);
        result.addProperty("incomplete", !status.equals("completed"));
        if (status.equals("completed")) result.remove("incomplete_reason");
        result.addProperty("finished_at", Instant.now().toString());
        result.addProperty("phase", active == null ? "finished" : active.name);
        result.add("errors", errors);
        result.add("restore_errors", restoreFailures);
        result.addProperty("settings_restored", restoreFailures.isEmpty());
        updateVerification();
        try { store.write(result); }
        catch (RuntimeException e) {
            FarmbenchMod.LOGGER.error("Failed to save farmbench result", e);
            errors.add(e.getMessage());
            result.addProperty("status", "failed");
            result.addProperty("incomplete", true);
            status = "failed";
            notify.accept(e.getMessage());
        }
        notify.accept("Farmbench " + status + ". Result: " + store.path());
    }

    void checkTimeout() {
        if (!finished && guard != null) guard.observe(level.getGameTime(), System.nanoTime(), false);
    }

    private void checkpoint() {
        result.addProperty("updated_at", Instant.now().toString());
        updateVerification();
        store.write(result);
    }

    private void updateVerification() {
        JsonObject verification = new JsonObject();
        verification.add("placement", placement.report());
        verification.add("counters", counters.report());
        verification.addProperty("initiating_player_proximity", "Every server tick: same dimension, Creative, all bay chunk centres strictly within 127 horizontal blocks.");
        verification.addProperty("bay_ticking", "Every server tick after placement: each bay chunk loaded and entity-ticking.");
        verification.addProperty("runtime_condition_checks", checks);
        verification.addProperty("maximum_player_chunk_distance", maximumPlayerDistance);
        verification.addProperty("operator_confirmation", "Not collected. Farm lighting, intended mechanism, scheduled-growth suppression, item-loss completeness and statistical convergence are not established by counter counts.");
        verification.addProperty("repeat_initial_state", "Persistent builds/entities continue across replicates; each replicate warms up and drains. Replicates are paired windows, not independently rebuilt farms.");
        result.add("verification", verification);
    }

    private void validateEnvironment(ServerPlayer player) {
        if (!player.isCreative()) throw new IllegalArgumentException("Start jobs as a Creative player in a lab world");
        if (!level.dimension().identifier().toString().equals(JobLoader.text(conditions, "dimension")))
            throw new IllegalArgumentException("Player must be in the job dimension");
        if (server.getPlayerList().getSimulationDistance() != JobLoader.integer(conditions, "simulation_distance", 2, 32))
            throw new IllegalArgumentException("Set simulation distance to the job value before starting");
        if (SharedConstants.getCurrentVersion().dataVersion().version() != JobLoader.integer(job, "data_version", 1, Integer.MAX_VALUE))
            throw new IllegalArgumentException("Job data version does not match this Minecraft runtime");
        for (var element : job.getAsJsonArray("designs")) {
            String item = JobLoader.text(element.getAsJsonObject(), "item");
            if (BuiltInRegistries.ITEM.getOptional(Identifier.parse(item)).isEmpty()) throw new IllegalArgumentException("Unknown item " + item);
            if (!item.equals("minecraft:bamboo")) throw new IllegalArgumentException("This runner currently supports the natural-growth bamboo protocol only");
        }
    }

    private void assertPlayer(boolean checkTicking) {
        ServerPlayer player = server.getPlayerList().getPlayer(playerId);
        if (player == null || player.level() != level || !player.isCreative())
            throw new IllegalStateException("Initiating player left the lab dimension or Creative mode");
        for (ChunkPos chunk : bayChunks) {
            double dx = player.getX() - chunk.getMiddleBlockX(), dz = player.getZ() - chunk.getMiddleBlockZ();
            double distance = Math.hypot(dx, dz);
            maximumPlayerDistance = Math.max(maximumPlayerDistance, distance);
            if (distance >= 127) throw new IllegalStateException("A bay is outside the initiating player's 127-block ticking safety radius; move near all bays or reduce layout width");
            if (checkTicking && (level.getChunkSource().getChunkNow(chunk.x(), chunk.z()) == null
                || !level.isPositionEntityTicking(chunk.getMiddleBlockPosition(level.getMinY()))))
                throw new IllegalStateException("Bay chunk is not loaded/entity-ticking: " + chunk);
        }
    }

    private void initializeResult(BlockPos origin) {
        result.addProperty("format", "retpack-farm-job-result-v1");
        result.addProperty("status", "failed");
        result.addProperty("incomplete", true);
        result.addProperty("incomplete_reason", "Job has not completed; process interruption leaves a failed checkpoint and requires a new run.");
        result.addProperty("run_id", runId);
        result.addProperty("started_at", Instant.now().toString());
        result.addProperty("phase", "initial_freeze");
        result.add("job", job.deepCopy());
        JsonArray xyz = new JsonArray(); xyz.add(origin.getX()); xyz.add(origin.getY()); xyz.add(origin.getZ()); result.add("origin", xyz);
        JsonObject environment = new JsonObject();
        environment.addProperty("minecraft", server.getServerVersion());
        environment.addProperty("data_version", SharedConstants.getCurrentVersion().dataVersion().version());
        environment.addProperty("java", System.getProperty("java.version"));
        environment.addProperty("dedicated", server.isDedicatedServer());
        environment.addProperty("dimension", level.dimension().identifier().toString());
        environment.addProperty("world_path", server.getWorldPath(LevelResource.ROOT).toAbsolutePath().normalize().toString());
        environment.addProperty("initiating_player", playerId.toString());
        environment.addProperty("protocol", "natural_growth_bamboo");
        environment.addProperty("drain_growth_control", "random_tick_speed=0; scheduled block ticks and entities continue");
        environment.add("conditions", conditions.deepCopy());
        JsonArray mods = new JsonArray();
        FabricLoader.getInstance().getAllMods().stream().sorted(java.util.Comparator.comparing(mod -> mod.getMetadata().getId())).forEach(mod -> {
            JsonObject metadata = new JsonObject();
            metadata.addProperty("id", mod.getMetadata().getId());
            metadata.addProperty("version", mod.getMetadata().getVersion().getFriendlyString());
            mods.add(metadata);
        });
        environment.add("mods", mods);
        result.add("environment", environment);
        result.add("original_settings", settings.report());
        calibration.addProperty("status", "incomplete");
        calibration.addProperty("clock", "ServerLevel.getGameTime, not server-loop tick count or wall time");
        calibration.add("phases", calibrationPhases);
        result.add("calibration", calibration);
        result.add("trials", trials);
        result.add("phases", phases);
        result.add("errors", errors);
    }

    boolean finished() { return finished; }
    PlacementService placement() { return placement; }
    String status() {
        return JobLoader.text(job, "id") + " " + runId + ": " + (finished ? result.get("status").getAsString() : active == null ? "settling freeze" : active.name)
            + "; " + trials.size() + "/" + JobLoader.integer(job, "repeats", 1, 100) + " trials recorded";
    }
}
