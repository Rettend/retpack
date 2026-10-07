package me.rettend.farmbench;

import java.util.ArrayList;
import java.util.List;

final class PhasePlan {
    enum Kind { WARMUP, PRE_DRAIN, MEASURE, POST_DRAIN }
    record Stage(int trial, Kind kind, int ticks, int randomTickSpeed) {}

    static List<Stage> paired(int repeats, int warmup, int run, int drain, int growthSpeed) {
        if (repeats < 1 || warmup < 0 || run < 1 || drain < 1 || growthSpeed < 1)
            throw new IllegalArgumentException("Invalid paired-run timing");
        List<Stage> stages = new ArrayList<>();
        for (int i = 1; i <= repeats; i++) {
            stages.add(new Stage(i, Kind.WARMUP, warmup, growthSpeed));
            stages.add(new Stage(i, Kind.PRE_DRAIN, drain, 0));
            stages.add(new Stage(i, Kind.MEASURE, run, growthSpeed));
            stages.add(new Stage(i, Kind.POST_DRAIN, drain, 0));
        }
        return List.copyOf(stages);
    }
}
