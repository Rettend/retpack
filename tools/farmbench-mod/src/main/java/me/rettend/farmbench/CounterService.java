package me.rettend.farmbench;

import carpet.CarpetSettings;
import carpet.helpers.HopperCounter;
import com.google.gson.JsonArray;
import com.google.gson.JsonElement;
import com.google.gson.JsonObject;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.UUID;
import java.util.function.IntUnaryOperator;
import me.rettend.farmbench.mixin.HopperCounterAccessor;
import net.minecraft.core.BlockPos;
import net.minecraft.core.registries.BuiltInRegistries;
import net.minecraft.server.MinecraftServer;
import net.minecraft.server.level.ServerLevel;
import net.minecraft.world.Container;
import net.minecraft.world.entity.vehicle.minecart.MinecartHopper;
import net.minecraft.world.level.ChunkPos;
import net.minecraft.world.level.block.HopperBlock;
import net.minecraft.world.level.block.entity.HopperBlockEntity;
import net.minecraft.world.phys.AABB;

final class CounterService {
    private final MinecraftServer server;
    private final ServerLevel level;
    private final JsonArray designs;
    private final Map<String, Set<BlockPos>> outputHoppers = new HashMap<>();
    private final Map<BlockPos, String> outputDirections = new HashMap<>();
    private final List<Bay> bays = new ArrayList<>();
    private final Set<ChunkPos> bayChunks = new HashSet<>();
    private final Map<UUID, String> knownTransportCarts = new HashMap<>();
    private final JsonArray drainChecks = new JsonArray();
    private String placementEntityTag;
    private boolean portsVerified;
    private long resetTick = -1;
    private final Map<String, Long> previousTotals = new HashMap<>();
    private long collisionScans;
    private long scannedChunks;

    CounterService(MinecraftServer server, ServerLevel level, JsonObject job, BlockPos origin) {
        this.server = server;
        this.level = level;
        this.designs = job.getAsJsonArray("designs");
        for (JsonElement element : designs) {
            JsonObject design = element.getAsJsonObject();
            int[] offset = JobLoader.vector(design.get("offset"), "offset", -30_000_000, 30_000_000);
            int[] size = JobLoader.vector(design.get("size"), "size", 1, 512);
            BlockPos minimum = origin.offset(offset[0], offset[1], offset[2]);
            Bay bay = new Bay(JobLoader.text(design, "id"), new AABB(minimum.getX(), minimum.getY(), minimum.getZ(),
                minimum.getX() + size[0], minimum.getY() + size[1], minimum.getZ() + size[2]));
            bays.add(bay);
            for (int x = Math.floorDiv(minimum.getX(), 16); x <= Math.floorDiv(minimum.getX() + size[0] - 1, 16); x++)
                for (int z = Math.floorDiv(minimum.getZ(), 16); z <= Math.floorDiv(minimum.getZ() + size[2] - 1, 16); z++)
                    bayChunks.add(new ChunkPos(x, z));
            Set<BlockPos> positions = new java.util.HashSet<>();
            for (JsonElement p : design.getAsJsonArray("ports")) {
                int[] position = JobLoader.vector(p.getAsJsonObject().get("position"), "port", 0, 511);
                BlockPos absolute = origin.offset(offset[0] + position[0], offset[1] + position[1], offset[2] + position[2]);
                positions.add(absolute);
                outputDirections.put(absolute, JobLoader.text(p.getAsJsonObject(), "direction"));
            }
            outputHoppers.put(JobLoader.text(design, "counter"), Set.copyOf(positions));
        }
    }

    void assertExclusive() {
        if (!CarpetSettings.hopperCounters) throw new IllegalStateException("Carpet hopperCounters was disabled");
        for (ServerLevel world : server.getAllLevels()) {
            world.getChunkSource().chunkMap.forEachBlockTickingChunk(chunk -> {
                scannedChunks++;
                for (var entry : chunk.getBlockEntities().entrySet()) {
                    if (!(entry.getValue() instanceof HopperBlockEntity)) continue;
                    BlockPos pos = entry.getKey();
                    var state = world.getBlockState(pos);
                    if (!(state.getBlock() instanceof HopperBlock)) continue;
                    String sink = BuiltInRegistries.BLOCK.getKey(world.getBlockState(pos.relative(state.getValue(HopperBlock.FACING))).getBlock()).toString();
                    for (var assigned : outputHoppers.entrySet()) {
                        if (sink.equals("minecraft:" + assigned.getKey() + "_wool")
                            && (world != level || !(portsVerified ? assigned.getValue().contains(pos)
                                : outputHoppers.values().stream().anyMatch(positions -> positions.contains(pos)))))
                            throw new IllegalStateException("Unrelated output hopper uses counter " + assigned.getKey()
                                + " in " + world.dimension().identifier() + " at " + pos.toShortString());
                    }
                }
            });
        }
        collisionScans++;
    }

    void verifyPorts() {
        for (var entry : outputHoppers.entrySet()) {
            for (BlockPos pos : entry.getValue()) {
                var state = level.getBlockState(pos);
                if (!(state.getBlock() instanceof HopperBlock)) throw new IllegalStateException("Declared counter port is not a hopper at " + pos);
                if (!state.getValue(HopperBlock.FACING).getSerializedName().equals(outputDirections.get(pos)))
                    throw new IllegalStateException("Declared counter hopper direction changed at " + pos);
                String sink = BuiltInRegistries.BLOCK.getKey(level.getBlockState(pos.relative(state.getValue(HopperBlock.FACING))).getBlock()).toString();
                if (!sink.equals("minecraft:" + entry.getKey() + "_wool"))
                    throw new IllegalStateException("Declared hopper is not feeding assigned " + entry.getKey() + " wool at " + pos);
            }
        }
        portsVerified = true;
    }

    void reset() {
        for (String colour : outputHoppers.keySet()) HopperCounter.getCounter(colour).reset(server);
        resetTick = server.overworld().getGameTime();
        previousTotals.clear();
    }

    void assertIntegrity() {
        if (resetTick < 0) return;
        for (String colour : outputHoppers.keySet()) {
            HopperCounter counter = HopperCounter.getCounter(colour);
            if (((HopperCounterAccessor) counter).farmbench$startTick() != resetTick)
                throw new IllegalStateException("Counter " + colour + " was reset outside the runner");
            long total = counter.getTotalItems();
            if (total < previousTotals.getOrDefault(colour, 0L))
                throw new IllegalStateException("Counter " + colour + " decreased during the job");
            previousTotals.put(colour, total);
        }
    }

    /** Remember placed and matched existing carts, without changing placement ownership or tags. */
    void registerTransportCarts(JsonObject placementReport) {
        placementEntityTag = JobLoader.text(placementReport, "entity_tag");
        for (JsonElement element : placementReport.getAsJsonArray("initial")) {
            JsonObject design = element.getAsJsonObject();
            for (JsonElement observation : design.getAsJsonObject("entities").getAsJsonArray("observations")) {
                JsonObject entity = observation.getAsJsonObject();
                if ("minecraft:hopper_minecart".equals(JobLoader.text(entity, "type")))
                    knownTransportCarts.put(UUID.fromString(JobLoader.text(entity, "uuid")), JobLoader.text(design, "id"));
            }
        }
    }

    JsonObject inspectDrain(String phase) {
        DrainCheck check = new DrainCheck(phase, level.getGameTime(), bays.stream().map(Bay::id).toList());
        // Keep this same observation in the result even when the boundary subsequently fails.
        drainChecks.add(check.observation);
        for (ChunkPos chunkPosition : bayChunks) {
            var chunk = level.getChunkSource().getChunkNow(chunkPosition.x(), chunkPosition.z());
            if (chunk == null) {
                check.unchecked("Bay chunk was unloaded during drain verification: " + chunkPosition);
                continue;
            }
            for (var entry : chunk.getBlockEntities().entrySet()) {
                if (!(entry.getValue() instanceof HopperBlockEntity hopper)) continue;
                BlockPos pos = entry.getKey();
                Bay bay = containing(pos);
                if (bay != null) check.inventory(bay.id, "hopper_block", pos.getX(), pos.getY(), pos.getZ(), null,
                    hopper.getContainerSize(), slot -> count(hopper, slot));
            }
        }
        Set<UUID> observedKnown = new HashSet<>();
        for (ServerLevel world : server.getAllLevels()) {
            for (var entity : world.getAllEntities()) {
                if (!(entity instanceof MinecartHopper cart) || cart.isRemoved()) continue;
                UUID uuid = cart.getUUID();
                Bay bay = world == level ? intersecting(cart.getBoundingBox()) : null;
                String knownDesign = knownTransportCarts.get(uuid);
                boolean tagged = placementEntityTag != null && cart.entityTags().contains(placementEntityTag);
                if (knownDesign != null) observedKnown.add(uuid);
                if (bay == null) {
                    if (knownDesign != null || tagged) check.escapedCart(knownDesign, uuid.toString(), cart.getX(), cart.getY(), cart.getZ(),
                        world.dimension().identifier().toString());
                    continue;
                }
                if ((knownDesign != null && !knownDesign.equals(bay.id))
                    || ((knownDesign != null || tagged) && !bay.box.contains(cart.getX(), cart.getY(), cart.getZ()))) {
                    check.escapedCart(knownDesign, uuid.toString(), cart.getX(), cart.getY(), cart.getZ(), world.dimension().identifier().toString());
                    continue;
                }
                knownTransportCarts.putIfAbsent(uuid, bay.id);
                observedKnown.add(uuid);
                check.inventory(bay.id, "hopper_minecart", cart.getX(), cart.getY(), cart.getZ(), uuid.toString(),
                    cart.getContainerSize(), slot -> count(cart, slot));
            }
        }
        for (var entry : knownTransportCarts.entrySet()) if (!observedKnown.contains(entry.getKey()))
            check.unchecked("Known hopper minecart " + entry.getKey() + " for design " + entry.getValue() + " is missing or unloaded; its inventory cannot be verified");
        return check.finish();
    }

    private Bay containing(BlockPos position) {
        return bays.stream().filter(bay -> bay.box.contains(position.getX(), position.getY(), position.getZ())).findFirst().orElse(null);
    }

    private Bay intersecting(AABB box) {
        return bays.stream().filter(bay -> bay.box.intersects(box)).findFirst().orElse(null);
    }

    private static int count(Container inventory, int slot) {
        var item = inventory.getItem(slot);
        return item.isEmpty() ? 0 : item.getCount();
    }

    static void requireDrained(JsonObject observation) {
        if (!"passed".equals(JobLoader.text(observation, "status"))) {
            String failure = observation.has("failure") ? JobLoader.text(observation, "failure") : "Drain inventory verification did not complete";
            throw new IllegalStateException(failure + "; increase drain_ticks and check the transport path before retrying");
        }
    }

    private record Bay(String id, AABB box) {}

    /** Pure accumulation and decision logic; inventory counts come from live server containers. */
    static final class DrainCheck {
        private final JsonObject observation = new JsonObject();
        private final Map<String, JsonObject> designs = new LinkedHashMap<>();
        private final JsonArray issues = new JsonArray();
        private long pendingItems;
        private int issueCount;
        private String firstFailure;

        DrainCheck(String phase, long gameTime, List<String> designIds) {
            observation.addProperty("phase", phase);
            observation.addProperty("game_time", gameTime);
            observation.addProperty("scope", "All hopper block inventories in loaded bay chunks and all live hopper minecarts intersecting bay bounds; known or placement-tagged carts must remain in their bay and be observable.");
            observation.addProperty("limitations", "Other containers and transport mechanisms are not checked and may hold intentional contents. This does not prove no stranded dropped items, uncollected stalks or item loss; broader transport needs operator verification.");
            observation.addProperty("status", "incomplete");
            observation.add("issues", issues);
            for (String id : designIds) {
                JsonObject counts = new JsonObject();
                counts.addProperty("id", id);
                counts.addProperty("hopper_blocks_checked", 0);
                counts.addProperty("hopper_minecarts_checked", 0);
                counts.addProperty("slots_checked", 0);
                counts.addProperty("pending_items", 0);
                designs.put(id, counts);
            }
            JsonArray rows = new JsonArray();
            designs.values().forEach(rows::add);
            observation.add("designs", rows);
        }

        void inventory(String design, String kind, double x, double y, double z, String uuid, int size, IntUnaryOperator counts) {
            JsonObject row = designs.get(design);
            if (row == null || !Set.of("hopper_block", "hopper_minecart").contains(kind) || size < 1)
                throw new IllegalArgumentException("Invalid transport inventory observation");
            long pending = 0;
            JsonArray occupied = new JsonArray();
            for (int slot = 0; slot < size; slot++) {
                int items = counts.applyAsInt(slot);
                if (items < 0) throw new IllegalStateException("Negative transport item count");
                pending += items;
                if (items != 0) {
                    JsonObject item = new JsonObject(); item.addProperty("slot", slot); item.addProperty("items", items); occupied.add(item);
                }
            }
            String checked = kind.equals("hopper_block") ? "hopper_blocks_checked" : "hopper_minecarts_checked";
            row.addProperty(checked, row.get(checked).getAsInt() + 1);
            row.addProperty("slots_checked", row.get("slots_checked").getAsInt() + size);
            row.addProperty("pending_items", row.get("pending_items").getAsLong() + pending);
            pendingItems += pending;
            if (pending > 0) {
                JsonObject issue = located(design, kind, x, y, z, uuid);
                issue.addProperty("pending_items", pending);
                issue.add("occupied_slots", occupied);
                issue(issue, "Drain left " + pending + " pending items in " + kind + " for design " + design + " at [" + x + ", " + y + ", " + z + "]");
            }
        }

        void escapedCart(String design, String uuid, double x, double y, double z, String dimension) {
            JsonObject issue = located(design, "hopper_minecart", x, y, z, uuid);
            issue.addProperty("dimension", dimension);
            issue.addProperty("reason", "outside_original_bay");
            issue(issue, "Known/tagged hopper minecart " + uuid + " left its bay at [" + x + ", " + y + ", " + z + "] in " + dimension + "; its transport inventory cannot be accepted as drained");
        }

        void unchecked(String reason) {
            JsonObject issue = new JsonObject(); issue.addProperty("reason", reason); issue(issue, reason);
        }

        private void issue(JsonObject issue, String failure) {
            issueCount++;
            if (issues.size() < 100) issues.add(issue);
            if (firstFailure == null) firstFailure = failure;
        }

        JsonObject finish() {
            observation.addProperty("status", issueCount == 0 ? "passed" : "failed");
            observation.addProperty("pending_items", pendingItems);
            observation.addProperty("issue_count", issueCount);
            observation.addProperty("issue_examples_truncated", issueCount > issues.size());
            if (firstFailure != null) observation.addProperty("failure", firstFailure);
            return observation;
        }

        private static JsonObject located(String design, String kind, double x, double y, double z, String uuid) {
            JsonObject issue = new JsonObject();
            if (design != null) issue.addProperty("design", design);
            issue.addProperty("kind", kind);
            JsonArray position = new JsonArray(); position.add(x); position.add(y); position.add(z); issue.add("position", position);
            if (uuid != null) issue.addProperty("uuid", uuid);
            return issue;
        }
    }

    JsonArray read(long measuredTicks) {
        assertIntegrity();
        JsonArray observations = new JsonArray();
        for (JsonElement element : designs) {
            JsonObject design = element.getAsJsonObject();
            String colour = JobLoader.text(design, "counter"), expectedItem = JobLoader.text(design, "item");
            HopperCounter counter = HopperCounter.getCounter(colour);
            HopperCounterAccessor raw = (HopperCounterAccessor) counter;
            JsonObject breakdown = new JsonObject();
            long expectedCount = 0;
            for (var entry : raw.farmbench$items().object2LongEntrySet()) {
                String item = BuiltInRegistries.ITEM.getKey(entry.getKey()).toString();
                long count = entry.getLongValue();
                if (count < 0) throw new IllegalStateException("Counter overflow");
                breakdown.addProperty(item, count);
                if (item.equals(expectedItem)) expectedCount = count;
                else if (count != 0) throw new IllegalStateException("Unexpected item " + item + " in " + colour + " counter");
            }
            JsonObject result = new JsonObject();
            for (String field : new String[]{"id", "counter", "item", "blueprint_sha256", "instantiated_blueprint_sha256", "artifact_sha256"})
                result.add(field, design.get(field).deepCopy());
            result.addProperty("items", expectedCount);
            result.addProperty("items_per_hour", PhaseGuard.itemsPerHour(expectedCount, measuredTicks));
            result.add("raw_items", breakdown);
            result.addProperty("raw_total_items", counter.getTotalItems());
            result.addProperty("counter_start_game_time", raw.farmbench$startTick());
            result.addProperty("counter_start_millis", raw.farmbench$startMillis());
            result.addProperty("counter_read_game_time", server.overworld().getGameTime());
            observations.add(result);
        }
        return observations;
    }

    JsonObject report() {
        JsonObject report = new JsonObject();
        report.addProperty("method", "direct_carpet_counter_map_accessor");
        report.addProperty("collision_scope", "All block-ticking chunks in all dimensions, before each phase and every 20 server loops; unloaded and newly loaded chunks between scans are not continuously inspected.");
        report.addProperty("collision_scans", collisionScans);
        report.addProperty("chunks_scanned", scannedChunks);
        report.addProperty("reset_scope", "Assigned colours only; their pre-job totals are intentionally not preserved.");
        report.addProperty("declared_counter_sinks_verified", portsVerified);
        report.addProperty("port_integrity", "Hopper type, declared facing and assigned wool sink checked every server tick after placement.");
        report.addProperty("counter_integrity", "After each runner reset, start tick and nondecreasing totals checked every server tick.");
        report.add("drain_checks", drainChecks.deepCopy());
        return report;
    }
}
