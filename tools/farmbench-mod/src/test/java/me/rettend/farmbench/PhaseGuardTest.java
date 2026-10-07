package me.rettend.farmbench;

import static org.junit.jupiter.api.Assertions.*;
import org.junit.jupiter.api.Test;

class PhaseGuardTest {
    @Test void completionUsesActualWorldTimeNotServerLoops() {
        PhaseGuard phase = new PhaseGuard(300, 8, 0);
        for (int loop = 0; loop < 100; loop++) assertEquals(0, phase.observe(300, loop * 1_000_000L, false));
        assertEquals(3, phase.observe(303, 101_000_000L, false));
        assertEquals(8, phase.observe(308, 102_000_000L, true));
    }

    @Test void frozenHoldCannotGrow() {
        PhaseGuard hold = new PhaseGuard(50, 0, 0);
        assertEquals(0, hold.observe(50, 5_000_000_000L, true));
        assertThrows(IllegalStateException.class, () -> hold.observe(51, 6_000_000_000L, true));
    }

    @Test void rejectsEarlyFinishOvershootAndClockRollback() {
        assertThrows(IllegalStateException.class, () -> new PhaseGuard(0, 8, 0).observe(7, 1, true));
        assertThrows(IllegalStateException.class, () -> new PhaseGuard(0, 8, 0).observe(9, 1, false));
        PhaseGuard phase = new PhaseGuard(10, 8, 0);
        phase.observe(12, 1, false);
        assertThrows(IllegalStateException.class, () -> phase.observe(11, 2, false));
    }

    @Test void detectsStallAndDeadlineWithoutCompletingMeasurement() {
        assertThrows(IllegalStateException.class, () -> new PhaseGuard(0, 8, 0).observe(0, 30_000_000_001L, false));
        assertThrows(IllegalStateException.class, () -> new PhaseGuard(0, 8, 0).observe(1, 30_000_000_001L, false));
        assertThrows(IllegalStateException.class, () -> new PhaseGuard(0, 8, 0).observe(8, 60_000_000_001L, true));
    }

    @Test void rateUsesMeasurementTicksExcludingDrainAndAvoidsIntegerOverflow() {
        assertEquals(100, PhaseGuard.itemsPerHour(100, 72000));
        assertEquals(36000, PhaseGuard.itemsPerHour(100, 200));
        assertEquals(9_000_000_000L, PhaseGuard.itemsPerHour(9_000_000_000L, 72000));
        assertThrows(IllegalArgumentException.class, () -> PhaseGuard.itemsPerHour(0, 0));
    }

    @Test void measuredCalibrationControlsSprintRequests() {
        assertEquals(71999, PhaseGuard.sprintRequest(72000, 1));
        assertEquals(72000, PhaseGuard.sprintRequest(72000, 0));
        assertThrows(IllegalArgumentException.class, () -> PhaseGuard.sprintRequest(1, 1));
        assertThrows(IllegalArgumentException.class, () -> PhaseGuard.sprintRequest(10, 2));
    }

    @Test void modelsInstalledVanillaFrozenSprintStartupAndNaturalFinish() {
        // Mirrors inspected 26.2 ordering: sprint decrement before tick(), simulation after tick().
        boolean runsNormally = false, frozen = false;
        int remaining = 7;
        long gameTime = 100;
        PhaseGuard guard = new PhaseGuard(gameTime, 8, 0);
        boolean sprinting = true;
        int loops = 0;
        while (sprinting) {
            if (runsNormally) {
                if (remaining > 0) remaining--;
                else { sprinting = false; frozen = true; }
            }
            runsNormally = !frozen;
            if (runsNormally) gameTime++;
            guard.observe(gameTime, ++loops, !sprinting);
        }
        assertEquals(108, gameTime);
        assertEquals(9, loops); // Final server iteration is frozen, not a simulation tick.
        assertFalse(runsNormally);
        assertTrue(frozen);
    }
}
