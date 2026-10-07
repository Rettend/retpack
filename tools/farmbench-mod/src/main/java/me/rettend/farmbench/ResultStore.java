package me.rettend.farmbench;

import com.google.gson.Gson;
import com.google.gson.GsonBuilder;
import com.google.gson.JsonObject;
import java.io.IOException;
import java.nio.file.AtomicMoveNotSupportedException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardCopyOption;

final class ResultStore {
    private static final Gson JSON = new GsonBuilder().setPrettyPrinting().disableHtmlEscaping().create();
    private final Path destination;

    ResultStore(Path config, String jobId, String runId) {
        destination = config.resolve("farmbench/results").resolve(jobId + "-" + runId + ".json");
        if (Files.exists(destination)) throw new IllegalStateException("Result already exists: " + destination);
    }

    void write(JsonObject result) {
        Path temporary = destination.resolveSibling(destination.getFileName() + ".tmp");
        try {
            Files.createDirectories(destination.getParent());
            Files.writeString(temporary, JSON.toJson(result) + "\n");
            try { Files.move(temporary, destination, StandardCopyOption.ATOMIC_MOVE, StandardCopyOption.REPLACE_EXISTING); }
            catch (AtomicMoveNotSupportedException e) { Files.move(temporary, destination, StandardCopyOption.REPLACE_EXISTING); }
        } catch (IOException e) { throw new IllegalStateException("Cannot save result " + destination + ": " + e.getMessage(), e); }
    }

    Path path() { return destination; }
}
