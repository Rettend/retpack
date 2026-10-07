package me.rettend.farmbench;

import com.google.gson.JsonArray;
import com.google.gson.JsonElement;
import com.google.gson.JsonObject;
import com.google.gson.JsonNull;
import com.google.gson.JsonPrimitive;
import com.google.gson.Strictness;
import com.google.gson.stream.JsonReader;
import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.util.HashSet;
import java.util.HexFormat;
import java.util.Set;

/** Validates all identities and artifacts before any world mutation. */
public final class JobLoader {
    public static final String FORMAT = "retpack-farm-job-v1";
    private static final String ID = "[a-z0-9][a-z0-9_-]{0,63}";
    private static final Set<String> COLOURS = Set.of("white", "orange", "magenta", "light_blue",
        "yellow", "lime", "pink", "gray", "light_gray", "cyan", "purple", "blue", "brown", "green", "red", "black");
    private final Path jobs;

    public JobLoader(Path configDirectory) { jobs = configDirectory.resolve("farmbench/jobs"); }

    public JsonObject load(String id) {
        if (!id.matches(ID)) throw new IllegalArgumentException("Invalid job ID");
        Path folder = jobs.resolve(id);
        try {
            Path realJobs = jobs.toRealPath();
            Path realFolder = folder.toRealPath();
            if (!realFolder.getParent().equals(realJobs)) throw new IllegalArgumentException("Job folder escapes jobs directory");
            return readJob(realFolder.resolve("job.json"), id);
        } catch (IOException | IllegalStateException e) {
            throw new IllegalArgumentException("Cannot load job " + id + ": " + e.getMessage(), e);
        }
    }

    /** Offline integration validation; loads no Minecraft classes and changes no instance files. */
    public static JsonObject loadFile(Path json) {
        if (!json.getFileName().toString().equals("job.json")) throw new IllegalArgumentException("Expected a job.json path");
        try { return readJob(json.toAbsolutePath().normalize(), null); }
        catch (IOException | IllegalStateException e) { throw new IllegalArgumentException("Cannot load " + json + ": " + e.getMessage(), e); }
    }

    private static JsonObject readJob(Path json, String expectedId) throws IOException {
        Path realFolder = json.getParent().toRealPath();
        if (!json.toRealPath().getParent().equals(realFolder) || Files.size(json) > 2_000_000)
            throw new IllegalArgumentException("Invalid or oversized job.json");
        JsonObject job;
        try (var reader = Files.newBufferedReader(json); var jsonReader = new JsonReader(reader)) {
            jsonReader.setStrictness(Strictness.STRICT);
            job = readJson(jsonReader, 0).getAsJsonObject();
            if (jsonReader.peek() != com.google.gson.stream.JsonToken.END_DOCUMENT)
                throw new IllegalArgumentException("Trailing job.json content");
        }
        validate(job, expectedId == null ? text(job, "id") : expectedId);
        for (JsonElement element : job.getAsJsonArray("designs")) {
            JsonObject design = element.getAsJsonObject();
            Path structure = realFolder.resolve(text(design, "structure")).toRealPath();
            if (!structure.getParent().equals(realFolder) || !Files.isRegularFile(structure)
                || Files.size(structure) > 128_000_000)
                throw new IllegalArgumentException("Invalid or oversized structure for " + text(design, "id"));
            if (!sha256(structure).equals(text(design, "artifact_sha256")))
                throw new IllegalArgumentException("Structure SHA256 mismatch for " + text(design, "id"));
        }
        return job;
    }

    private static JsonElement readJson(JsonReader reader, int depth) throws IOException {
        if (depth > 64) throw new IllegalArgumentException("Job JSON is nested too deeply");
        return switch (reader.peek()) {
            case BEGIN_OBJECT -> {
                JsonObject object = new JsonObject();
                reader.beginObject();
                while (reader.hasNext()) {
                    String name = reader.nextName();
                    if (object.has(name)) throw new IllegalArgumentException("Duplicate JSON field " + name);
                    object.add(name, readJson(reader, depth + 1));
                }
                reader.endObject();
                yield object;
            }
            case BEGIN_ARRAY -> {
                JsonArray array = new JsonArray();
                reader.beginArray();
                while (reader.hasNext()) array.add(readJson(reader, depth + 1));
                reader.endArray();
                yield array;
            }
            case STRING -> new JsonPrimitive(reader.nextString());
            case NUMBER -> new JsonPrimitive(new java.math.BigDecimal(reader.nextString()));
            case BOOLEAN -> new JsonPrimitive(reader.nextBoolean());
            case NULL -> { reader.nextNull(); yield JsonNull.INSTANCE; }
            default -> throw new IllegalArgumentException("Unexpected JSON token " + reader.peek());
        };
    }

    public static void validate(JsonObject job, String expectedId) {
        if (!FORMAT.equals(text(job, "format"))) throw new IllegalArgumentException("Unsupported job format");
        if (!expectedId.matches(ID) || !expectedId.equals(text(job, "id"))) throw new IllegalArgumentException("Job ID does not match folder");
        if (!"26.2".equals(text(job, "minecraft"))) throw new IllegalArgumentException("Job requires Minecraft 26.2");
        integer(job, "data_version", 1, Integer.MAX_VALUE);
        String mode = text(job, "mode");
        if (!Set.of("place", "existing").contains(mode)) throw new IllegalArgumentException("Unknown placement mode");
        if (!job.has("origin")) throw new IllegalArgumentException("Missing origin (use null for player origin)");
        if (!job.get("origin").isJsonNull()) vector(job.get("origin"), "origin", -30_000_000, 30_000_000);
        else if (mode.equals("existing")) throw new IllegalArgumentException("Existing mode requires explicit origin");
        integer(job, "repeats", 1, 100);
        integer(job, "warmup_ticks", 0, 10_000_000);
        integer(job, "run_ticks", 1, 10_000_000);
        integer(job, "drain_ticks", 1, 1_000_000);
        JsonObject conditions = job.getAsJsonObject("conditions");
        integer(conditions, "random_tick_speed", 1, 4096);
        integer(conditions, "simulation_distance", 2, 32);
        if (!Set.of("peaceful", "easy", "normal", "hard").contains(text(conditions, "difficulty")))
            throw new IllegalArgumentException("Unknown difficulty");
        if (!text(conditions, "dimension").matches("[a-z0-9_.-]+:[a-z0-9_./-]+"))
            throw new IllegalArgumentException("Invalid dimension");
        JsonArray designs = job.getAsJsonArray("designs");
        if (designs == null || designs.isEmpty() || designs.size() > 16) throw new IllegalArgumentException("A job needs 1–16 designs");
        Set<String> ids = new HashSet<>(), colours = new HashSet<>();
        long cells = 0;
        for (JsonElement element : designs) {
            JsonObject design = element.getAsJsonObject();
            String id = text(design, "id"), colour = text(design, "counter");
            if (!id.matches(ID) || !ids.add(id)) throw new IllegalArgumentException("Invalid or duplicate design ID");
            if (!COLOURS.contains(colour) || !colours.add(colour)) throw new IllegalArgumentException("Invalid or duplicate counter colour");
            for (String field : new String[]{"blueprint_sha256", "instantiated_blueprint_sha256", "artifact_sha256"})
                if (!text(design, field).matches("[a-f0-9]{64}")) throw new IllegalArgumentException("Invalid " + field);
            if (!text(design, "structure").matches("[a-zA-Z0-9_-]+\\.nbt")) throw new IllegalArgumentException("Structure must be a local .nbt filename");
            if (!text(design, "item").matches("minecraft:[a-z0-9_./-]+")) throw new IllegalArgumentException("Expected item must be vanilla");
            int[] size = vector(design.get("size"), "size", 1, 512);
            cells += (long) size[0] * size[1] * size[2];
            if (cells > 1_000_000) throw new IllegalArgumentException("Job exceeds 1,000,000 bay cells");
            vector(design.get("offset"), "offset", -30_000_000, 30_000_000);
            integer(design, "entity_count", 0, 10_000);
            JsonArray ports = design.getAsJsonArray("ports");
            if (ports == null || ports.isEmpty()) throw new IllegalArgumentException("Each design needs a counter port");
            for (JsonElement port : ports) {
                JsonObject p = port.getAsJsonObject();
                int[] position = vector(p.get("position"), "port position", 0, 511);
                for (int axis = 0; axis < 3; axis++) if (position[axis] >= size[axis]) throw new IllegalArgumentException("Port outside design");
                if (!Set.of("down", "up", "north", "south", "west", "east").contains(text(p, "direction")))
                    throw new IllegalArgumentException("Invalid port direction");
            }
        }
    }

    public static String text(JsonObject object, String field) {
        if (object == null || !object.has(field) || !object.get(field).isJsonPrimitive()
            || !object.getAsJsonPrimitive(field).isString()) throw new IllegalArgumentException("Missing or invalid " + field);
        return object.get(field).getAsString();
    }

    public static int integer(JsonObject object, String field, int min, int max) {
        if (object == null || !object.has(field)) throw new IllegalArgumentException("Missing " + field);
        return number(object.get(field), field, min, max);
    }

    public static int[] vector(JsonElement element, String field, int min, int max) {
        if (element == null || !element.isJsonArray() || element.getAsJsonArray().size() != 3)
            throw new IllegalArgumentException(field + " must have three integers");
        int[] result = new int[3];
        for (int i = 0; i < 3; i++) result[i] = number(element.getAsJsonArray().get(i), field, min, max);
        return result;
    }

    private static int number(JsonElement value, String field, int min, int max) {
        try {
            if (!value.isJsonPrimitive() || !value.getAsJsonPrimitive().isNumber()) throw new ArithmeticException();
            int result = value.getAsBigDecimal().intValueExact();
            if (result < min || result > max) throw new ArithmeticException();
            return result;
        } catch (ArithmeticException | NumberFormatException e) {
            throw new IllegalArgumentException(field + " must be an integer in " + min + ".." + max);
        }
    }

    public static String sha256(Path file) throws IOException {
        try {
            MessageDigest digest = MessageDigest.getInstance("SHA-256");
            try (var stream = Files.newInputStream(file)) {
                byte[] buffer = new byte[65536];
                int n;
                while ((n = stream.read(buffer)) != -1) digest.update(buffer, 0, n);
            }
            return HexFormat.of().formatHex(digest.digest());
        } catch (NoSuchAlgorithmException e) { throw new AssertionError(e); }
    }
}
