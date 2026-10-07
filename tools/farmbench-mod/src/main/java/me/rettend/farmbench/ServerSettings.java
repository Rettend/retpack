package me.rettend.farmbench;

import carpet.CarpetServer;
import carpet.CarpetSettings;
import com.google.gson.JsonObject;
import java.util.ArrayList;
import java.util.List;
import net.minecraft.server.MinecraftServer;
import net.minecraft.world.Difficulty;
import net.minecraft.world.level.gamerules.GameRules;

final class ServerSettings {
    private final MinecraftServer server;
    private final GameRules rules;
    private final int randomTickSpeed;
    private final Difficulty difficulty;
    private final boolean hopperCounters;
    private final boolean frozen;
    private final float tickRate;
    private boolean restored;

    ServerSettings(MinecraftServer server, GameRules rules) {
        this.server = server;
        this.rules = rules;
        var tick = server.tickRateManager();
        if (tick.isSprinting() || tick.isSteppingForward()) throw new IllegalStateException("Finish the current tick sprint/step before starting a job");
        randomTickSpeed = rules.get(GameRules.RANDOM_TICK_SPEED);
        difficulty = server.getWorldData().getDifficulty();
        hopperCounters = CarpetSettings.hopperCounters;
        frozen = tick.isFrozen();
        tickRate = tick.tickrate();
    }

    void prepare(JsonObject conditions) {
        Difficulty requested = Difficulty.valueOf(JobLoader.text(conditions, "difficulty").toUpperCase(java.util.Locale.ROOT));
        if (difficulty != requested && (server.isHardcore() || server.getWorldData().isDifficultyLocked()))
            throw new IllegalStateException("Unlock difficulty or select a matching lab-world difficulty");
        server.tickRateManager().setFrozen(true);
        randomTicks(0);
        server.setDifficulty(requested, true);
        setCounters(true);
    }

    void randomTicks(int value) { rules.set(GameRules.RANDOM_TICK_SPEED, value, server); }

    void verify(int expectedRandomTicks, JsonObject conditions) {
        if (rules.get(GameRules.RANDOM_TICK_SPEED) != expectedRandomTicks) throw new IllegalStateException("random_tick_speed changed during the job");
        if (!CarpetSettings.hopperCounters) throw new IllegalStateException("hopperCounters changed during the job");
        if (!server.getWorldData().getDifficulty().getSerializedName().equals(JobLoader.text(conditions, "difficulty")))
            throw new IllegalStateException("Difficulty changed during the job");
        if (Float.compare(server.tickRateManager().tickrate(), tickRate) != 0) throw new IllegalStateException("Tick rate changed during the job");
        if (server.getPlayerList().getSimulationDistance() != JobLoader.integer(conditions, "simulation_distance", 2, 32))
            throw new IllegalStateException("Simulation distance does not match job; set it before starting");
    }

    List<String> restore() {
        if (restored) return List.of();
        restored = true;
        List<String> failures = new ArrayList<>();
        attempt(failures, "tick sprint/step", () -> {
            var tick = server.tickRateManager();
            tick.stopSprinting();
            // 26.2 stopSprinting() ignores the last-tick state: remaining=0, scheduled>0.
            // Re-arm and stop synchronously, without running any extra world tick.
            if (tick.isSprinting()) { tick.requestGameToSprint(1); tick.stopSprinting(); }
            tick.stopStepping();
            if (tick.isSprinting() || tick.isSteppingForward()) throw new IllegalStateException("Tick advancement did not stop");
        });
        attempt(failures, "random_tick_speed", () -> randomTicks(randomTickSpeed));
        attempt(failures, "difficulty", () -> server.setDifficulty(difficulty, true));
        attempt(failures, "hopperCounters", () -> setCounters(hopperCounters));
        attempt(failures, "tick rate", () -> server.tickRateManager().setTickRate(tickRate));
        attempt(failures, "frozen state", () -> server.tickRateManager().setFrozen(frozen));
        return failures;
    }

    JsonObject report() {
        JsonObject original = new JsonObject();
        original.addProperty("random_tick_speed", randomTickSpeed);
        original.addProperty("difficulty", difficulty.getSerializedName());
        original.addProperty("hopperCounters", hopperCounters);
        original.addProperty("frozen", frozen);
        original.addProperty("tick_rate", tickRate);
        original.addProperty("sprinting", false);
        original.addProperty("step_ticks", 0);
        return original;
    }

    private void setCounters(boolean value) {
        try {
            CarpetServer.settingsManager.getCarpetRule("hopperCounters").set(server.createCommandSourceStack(), Boolean.toString(value));
            if (CarpetSettings.hopperCounters != value) throw new IllegalStateException("Carpet rejected hopperCounters change");
        } catch (carpet.api.settings.InvalidRuleValueException e) { throw new IllegalStateException("Cannot set hopperCounters: " + e.getMessage(), e); }
    }

    private static void attempt(List<String> failures, String label, Runnable operation) {
        try { operation.run(); } catch (RuntimeException e) { failures.add("Could not restore " + label + ": " + e.getMessage()); }
    }
}
