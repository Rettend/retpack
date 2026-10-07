package me.rettend.farmbench;

import static org.junit.jupiter.api.Assertions.*;
import java.util.List;
import org.junit.jupiter.api.Test;

class PhasePlanTest {
    @Test void eachReplicateHasPairedWarmupDrainMeasureDrainInOrder() {
        var plan = PhasePlan.paired(3, 1200, 72000, 200, 3);
        assertEquals(12, plan.size());
        for (int trial = 1; trial <= 3; trial++) {
            var stages = plan.subList((trial - 1) * 4, trial * 4);
            assertEquals(List.of(PhasePlan.Kind.WARMUP, PhasePlan.Kind.PRE_DRAIN, PhasePlan.Kind.MEASURE, PhasePlan.Kind.POST_DRAIN),
                stages.stream().map(PhasePlan.Stage::kind).toList());
            assertEquals(List.of(1200, 200, 72000, 200), stages.stream().map(PhasePlan.Stage::ticks).toList());
            assertEquals(List.of(3, 0, 3, 0), stages.stream().map(PhasePlan.Stage::randomTickSpeed).toList());
            int index = trial;
            assertTrue(stages.stream().allMatch(stage -> stage.trial() == index));
        }
    }

    @Test void supportsShortCalibrationSmokeTimingsAndNoWarmup() {
        var stages = PhasePlan.paired(1, 0, 1, 1, 3);
        assertEquals(0, stages.getFirst().ticks());
        assertEquals(1, stages.get(2).ticks());
        assertThrows(IllegalArgumentException.class, () -> PhasePlan.paired(1, 0, 0, 1, 3));
        assertThrows(IllegalArgumentException.class, () -> PhasePlan.paired(1, 0, 1, 0, 3));
    }
}
