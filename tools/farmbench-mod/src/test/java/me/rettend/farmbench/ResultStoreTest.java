package me.rettend.farmbench;

import static org.junit.jupiter.api.Assertions.*;
import com.google.gson.JsonObject;
import com.google.gson.JsonParser;
import java.nio.file.Files;
import java.nio.file.Path;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

class ResultStoreTest {
    @TempDir Path config;

    @Test void checkpointsRemainFailedUntilExplicitCompletionAndDoNotReuseRuns() throws Exception {
        ResultStore store = new ResultStore(config, "test", "run");
        JsonObject result = new JsonObject();
        result.addProperty("status", "failed");
        result.addProperty("incomplete", true);
        store.write(result);
        assertEquals("failed", JsonParser.parseString(Files.readString(store.path())).getAsJsonObject().get("status").getAsString());
        assertThrows(IllegalStateException.class, () -> new ResultStore(config, "test", "run"));
        result.addProperty("status", "completed");
        result.addProperty("incomplete", false);
        store.write(result);
        assertEquals("completed", JsonParser.parseString(Files.readString(store.path())).getAsJsonObject().get("status").getAsString());
        assertFalse(Files.exists(store.path().resolveSibling("test-run.json.tmp")));
    }
}
