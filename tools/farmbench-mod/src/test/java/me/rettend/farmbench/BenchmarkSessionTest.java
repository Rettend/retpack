package me.rettend.farmbench;

import static org.junit.jupiter.api.Assertions.*;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.atomic.AtomicBoolean;
import org.junit.jupiter.api.Test;

class BenchmarkSessionTest {
    @Test void pendingPreDrainItemsPreventCounterReset() {
        var check = new CounterService.DrainCheck("pre_drain", 200, List.of("a", "b"));
        check.inventory("a", "hopper_block", 0, 64, 0, null, 5, slot -> 0);
        check.inventory("b", "hopper_minecart", 20.5, 64.5, 0.5, "cart-b", 5, slot -> slot == 2 ? 1 : 0);
        AtomicBoolean counterReset = new AtomicBoolean();
        assertThrows(IllegalStateException.class, () -> BenchmarkSession.afterDrainVerification(check.finish(), () -> counterReset.set(true)));
        assertFalse(counterReset.get());
    }

    @Test void pendingPostDrainItemsPreventReadAndCompletedTrial() {
        var check = new CounterService.DrainCheck("post_drain", 900, List.of("a"));
        check.inventory("a", "hopper_block", 0, 64, 0, null, 5, slot -> slot == 0 ? 1 : 0);
        AtomicBoolean counterRead = new AtomicBoolean(), completed = new AtomicBoolean();
        assertThrows(IllegalStateException.class, () -> BenchmarkSession.afterDrainVerification(check.finish(), () -> {
            counterRead.set(true);
            completed.set(true);
        }));
        assertFalse(counterRead.get());
        assertFalse(completed.get());
    }

    @Test void verifiedEmptyDrainAllowsOnlyThenResetOrRecord() {
        var check = new CounterService.DrainCheck("post_drain", 900, List.of("a"));
        check.inventory("a", "hopper_minecart", 0.5, 64.5, 0.5, "cart-a", 5, slot -> 0);
        AtomicBoolean recorded = new AtomicBoolean();
        BenchmarkSession.afterDrainVerification(check.finish(), () -> recorded.set(true));
        assertTrue(recorded.get());
    }

    @Test void oneTickMeasurementCannotHideMidTickRuleChangeByStartingDrain() {
        int[] expectedRandomTicks = {3}, actualRandomTicks = {3};
        Runnable invariants = () -> {
            if (actualRandomTicks[0] != expectedRandomTicks[0]) throw new IllegalStateException("random_tick_speed changed during the job");
        };
        invariants.run(); // START_SERVER_TICK was valid.
        actualRandomTicks[0] = 0; // A tick function changes the rule before the only world tick.
        AtomicBoolean measured = new AtomicBoolean();
        assertThrows(IllegalStateException.class, () -> BenchmarkSession.atEndBoundary(invariants, () -> {
            measured.set(true);
            expectedRandomTicks[0] = 0; // The old END path hid the change by starting drainage.
        }));
        assertFalse(measured.get());
        assertEquals(3, expectedRandomTicks[0]);
    }

    @Test void finalDrainTickCannotResetCounterAfterStartThenProduceCompletedResult() {
        long expectedCounterStart = 100;
        long[] actualCounterStart = {100};
        Runnable invariants = () -> {
            if (actualCounterStart[0] != expectedCounterStart) throw new IllegalStateException("Counter was reset outside the runner");
        };
        invariants.run(); // START callback passed.
        actualCounterStart[0] = 101; // Tick function resets assigned colour during the last drain tick.
        AtomicBoolean read = new AtomicBoolean(), completed = new AtomicBoolean();
        assertThrows(IllegalStateException.class, () -> BenchmarkSession.atEndBoundary(invariants, () -> {
            read.set(true);
            completed.set(true); // No next START callback exists after the final phase.
        }));
        assertFalse(read.get());
        assertFalse(completed.get());
    }

    @Test void successfulEndBoundaryChecksBeforeAnyReadOrTransition() {
        List<String> order = new ArrayList<>();
        BenchmarkSession.atEndBoundary(() -> order.add("invariants"), () -> {
            order.add("read");
            order.add("next_phase");
        });
        assertEquals(List.of("invariants", "read", "next_phase"), order);
    }
}
