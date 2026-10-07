package me.rettend.farmbench;

import java.nio.file.Path;

/** Standalone Python-to-Java artifact contract check, without bootstrapping Minecraft. */
public final class JobValidator {
    private JobValidator() {}

    public static void main(String[] args) {
        if (args.length != 1) throw new IllegalArgumentException("Usage: validateJob -Pjob=/path/to/job.json");
        var job = JobLoader.loadFile(Path.of(args[0]));
        int entities = 0;
        for (var element : job.getAsJsonArray("designs"))
            entities += JobLoader.integer(element.getAsJsonObject(), "entity_count", 0, 10_000);
        System.out.println("Validated " + JobLoader.text(job, "id") + ": " + job.getAsJsonArray("designs").size()
            + " designs, " + entities + " declared entities; job schema and all structure SHA256 values passed.");
        System.out.println("Offline file validation only. Placement, NBT contents, game ticks, counters and world behavior require runtime checks.");
    }
}
