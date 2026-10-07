package me.rettend.farmbench;

/** Pure world-time invariant checks shared by calibration and all measured phases. */
final class PhaseGuard {
    private final long startTick;
    private final long expectedTicks;
    private final long deadlineNanos;
    private long previousTick;
    private long lastProgressNanos;

    PhaseGuard(long startTick, long expectedTicks, long nowNanos) {
        if (expectedTicks < 0) throw new IllegalArgumentException("Negative phase duration");
        this.startTick = this.previousTick = startTick;
        this.expectedTicks = expectedTicks;
        this.lastProgressNanos = nowNanos;
        // Deadlines are safety checks only. They never determine phase completion.
        this.deadlineNanos = nowNanos + Math.max(60_000_000_000L, expectedTicks * 500_000_000L);
    }

    long observe(long gameTime, long nowNanos, boolean advancementFinished) {
        if (gameTime < previousTick) throw new IllegalStateException("World game time moved backwards");
        long elapsed = gameTime - startTick;
        if (elapsed > expectedTicks) throw new IllegalStateException("World advanced " + elapsed + " ticks; expected " + expectedTicks);
        if (nowNanos > deadlineNanos || (previousTick - startTick < expectedTicks && nowNanos - lastProgressNanos > 30_000_000_000L))
            throw new IllegalStateException("Phase timed out or world stopped advancing");
        if (gameTime != previousTick) lastProgressNanos = nowNanos;
        previousTick = gameTime;
        if (advancementFinished && elapsed != expectedTicks)
            throw new IllegalStateException("World advanced " + elapsed + " ticks; expected " + expectedTicks);
        return elapsed;
    }

    static int sprintRequest(int worldTicks, int calibratedOverhead) {
        if (calibratedOverhead < 0 || calibratedOverhead > 1) throw new IllegalArgumentException("Unsupported sprint overhead");
        int requested = worldTicks - calibratedOverhead;
        if (requested <= 0) throw new IllegalArgumentException("Use a frozen step for this duration");
        return requested;
    }

    static double itemsPerHour(long items, long measuredTicks) {
        if (items < 0 || measuredTicks <= 0) throw new IllegalArgumentException("Invalid measurement");
        return items * (72000.0 / measuredTicks);
    }
}
