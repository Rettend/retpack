package me.rettend.farmbench;

import static org.junit.jupiter.api.Assertions.*;
import com.google.gson.JsonArray;
import com.google.gson.JsonObject;
import com.google.gson.JsonParser;
import java.nio.file.Files;
import java.nio.file.Path;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

class JobLoaderTest {
    @TempDir Path config;

    static JsonObject validJob() {
        JsonObject job = JsonParser.parseString("""
            {"format":"retpack-farm-job-v1","id":"test","minecraft":"26.2","data_version":4903,
             "mode":"place","origin":null,"repeats":3,"warmup_ticks":1200,"run_ticks":72000,"drain_ticks":200,
             "conditions":{"random_tick_speed":3,"simulation_distance":12,"difficulty":"normal","dimension":"minecraft:overworld"},
             "designs":[{"id":"a","name":"bamboo","structure":"a.nbt","size":[3,5,4],"offset":[0,0,0],
                          "counter":"lime","item":"minecraft:bamboo","entity_count":0,
                          "ports":[{"position":[1,0,1],"direction":"east"}]}]}
            """).getAsJsonObject();
        JsonObject design = job.getAsJsonArray("designs").get(0).getAsJsonObject();
        for (String field : new String[]{"blueprint_sha256", "instantiated_blueprint_sha256", "artifact_sha256"})
            design.addProperty(field, "a".repeat(64));
        return job;
    }

    @Test void acceptsProtocolAndChecksStructureBytesBeforeReturning() throws Exception {
        JsonObject job = validJob();
        Path folder = Files.createDirectories(config.resolve("farmbench/jobs/test"));
        Path structure = folder.resolve("a.nbt");
        Files.writeString(structure, "arbitrary artifact bytes");
        job.getAsJsonArray("designs").get(0).getAsJsonObject().addProperty("artifact_sha256", JobLoader.sha256(structure));
        Files.writeString(folder.resolve("job.json"), job.toString());
        assertEquals(job, new JobLoader(config).load("test"));
        assertEquals(job, JobLoader.loadFile(folder.resolve("job.json")));
        Files.writeString(structure, "changed artifact");
        assertTrue(assertThrows(IllegalArgumentException.class, () -> new JobLoader(config).load("test")).getMessage().contains("SHA256"));
    }

    @Test void rejectsCounterSharingTraversalAndExistingDefaultOrigin() {
        JsonObject job = validJob();
        JsonObject second = job.getAsJsonArray("designs").get(0).deepCopy().getAsJsonObject();
        second.addProperty("id", "b");
        job.getAsJsonArray("designs").add(second);
        assertThrows(IllegalArgumentException.class, () -> JobLoader.validate(job, "test"));
        job.getAsJsonArray("designs").remove(1);
        job.getAsJsonArray("designs").get(0).getAsJsonObject().addProperty("structure", "../a.nbt");
        assertThrows(IllegalArgumentException.class, () -> JobLoader.validate(job, "test"));
        assertThrows(IllegalArgumentException.class, () -> new JobLoader(config).load("../test"));
        JsonObject existing = validJob(); existing.addProperty("mode", "existing");
        assertThrows(IllegalArgumentException.class, () -> JobLoader.validate(existing, "test"));
    }

    @Test void rejectsFractionalStringAndOutOfRangeNumbers() {
        for (String value : new String[]{"1.5", "\"3\"", "2147483648", "-1", "null", "true"}) {
            JsonObject job = validJob(); job.add("repeats", JsonParser.parseString(value));
            assertThrows(IllegalArgumentException.class, () -> JobLoader.validate(job, "test"), value);
        }
    }

    @Test void rejectsOutOfBoundsPortsAndCellExplosion() {
        JsonObject job = validJob();
        job.getAsJsonArray("designs").get(0).getAsJsonObject().add("size", JsonParser.parseString("[512,512,512]"));
        assertThrows(IllegalArgumentException.class, () -> JobLoader.validate(job, "test"));
        JsonObject ports = validJob();
        ports.getAsJsonArray("designs").get(0).getAsJsonObject().getAsJsonArray("ports").get(0).getAsJsonObject()
            .add("position", JsonParser.parseString("[3,0,0]"));
        assertThrows(IllegalArgumentException.class, () -> JobLoader.validate(ports, "test"));
    }

    @Test void rejectsNonStrictAndTrailingJson() throws Exception {
        Path folder = Files.createDirectories(config.resolve("farmbench/jobs/test"));
        Files.writeString(folder.resolve("job.json"), validJob() + " {} ");
        assertThrows(RuntimeException.class, () -> new JobLoader(config).load("test"));
        Files.writeString(folder.resolve("job.json"), "{id:'test'}");
        assertThrows(RuntimeException.class, () -> new JobLoader(config).load("test"));
        Files.writeString(folder.resolve("job.json"), "{\"id\":\"test\",\"id\":\"test\"}");
        assertTrue(assertThrows(RuntimeException.class, () -> new JobLoader(config).load("test")).getMessage().contains("Duplicate"));
    }
}
