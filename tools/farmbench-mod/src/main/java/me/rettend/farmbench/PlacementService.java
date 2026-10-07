package me.rettend.farmbench;

import com.google.gson.JsonArray;
import com.google.gson.JsonElement;
import com.google.gson.JsonObject;
import java.io.ByteArrayInputStream;
import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.HashSet;
import java.util.HexFormat;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.UUID;
import net.fabricmc.loader.api.FabricLoader;
import net.minecraft.SharedConstants;
import net.minecraft.core.BlockPos;
import net.minecraft.core.Direction;
import net.minecraft.core.UUIDUtil;
import net.minecraft.core.registries.BuiltInRegistries;
import net.minecraft.nbt.CompoundTag;
import net.minecraft.nbt.DoubleTag;
import net.minecraft.nbt.ListTag;
import net.minecraft.nbt.NbtAccounter;
import net.minecraft.nbt.NbtIo;
import net.minecraft.nbt.NbtUtils;
import net.minecraft.nbt.Tag;
import net.minecraft.resources.Identifier;
import net.minecraft.server.MinecraftServer;
import net.minecraft.server.level.ServerLevel;
import net.minecraft.server.level.TicketType;
import net.minecraft.tags.BlockTags;
import net.minecraft.util.ProblemReporter;
import net.minecraft.util.RandomSource;
import net.minecraft.world.entity.Entity;
import net.minecraft.world.entity.EntitySpawnReason;
import net.minecraft.world.entity.EntitySpawnRequest;
import net.minecraft.world.entity.EntityType;
import net.minecraft.world.level.ChunkPos;
import net.minecraft.world.level.block.Block;
import net.minecraft.world.level.block.Blocks;
import net.minecraft.world.level.block.HopperBlock;
import net.minecraft.world.level.block.entity.BlockEntity;
import net.minecraft.world.level.block.state.BlockState;
import net.minecraft.world.level.block.state.properties.Property;
import net.minecraft.world.level.levelgen.structure.templatesystem.StructurePlaceSettings;
import net.minecraft.world.level.levelgen.structure.templatesystem.StructureTemplate;
import net.minecraft.world.level.storage.TagValueInput;
import net.minecraft.world.level.storage.TagValueOutput;
import net.minecraft.world.phys.AABB;
import net.minecraft.world.phys.Vec3;

/** Server-thread-only placement. No save edits, global entity deletion, or automatic re-pasting. */
public final class PlacementService {
    private static final int TICKET_RADIUS = 2;
    private static final long MAX_NBT_BYTES = 256_000_000;
    private static final double POSE_TOLERANCE = 1.0 / 4096.0;
    private static final Set<String> TRANSIENT_PROPERTIES = Set.of("powered", "power", "lit", "enabled",
        "triggered", "extended", "age", "stage", "moisture", "leaves", "distance", "occupied",
        "open", "level", "honey_level", "berries", "bloom", "unstable", "signal_fire", "in_wall");
    private static final Set<String> ENTITY_METADATA = Set.of("id", "UUID", "UUIDMost", "UUIDLeast",
        "Pos", "Rotation", "Motion", "Tags", "Passengers");
    private static final Set<String> ENTITY_DYNAMICS = Set.of("Air", "Fire", "FallDistance", "OnGround",
        "PortalCooldown", "HurtTime", "DeathTime", "Age", "Health", "health", "AbsorptionAmount");
    private static final Set<String> BLOCK_ENTITY_DYNAMICS = Set.of("TransferCooldown", "LootTableSeed",
        "LastUpdate", "LastTick", "CookTime", "CookTimeTotal", "BurnTime", "cooking_time_spent",
        "cooking_total_time", "lit_time_remaining", "lit_total_time");

    private final MinecraftServer server;
    private final ServerLevel level;
    private final JsonObject job;
    private final BlockPos origin;
    private final String tag;
    private final TicketType ticketType;
    private final List<Design> designs = new ArrayList<>();
    private final Set<ChunkPos> tickets = new LinkedHashSet<>();
    private final Set<UUID> reservedUuids = new HashSet<>();
    private final Map<UUID, Entity> ownedEntities = new LinkedHashMap<>();
    private final Map<BlockPos, BlockState> recoloured = new LinkedHashMap<>();
    private final JsonObject verification = new JsonObject();
    private final JsonArray warnings = new JsonArray();
    private boolean started;
    private boolean prepared;
    private boolean cleaned;
    private String state = "not_prepared";

    public PlacementService(MinecraftServer server, ServerLevel level, JsonObject job, BlockPos origin, String runId) {
        this.server = java.util.Objects.requireNonNull(server);
        this.level = java.util.Objects.requireNonNull(level);
        this.job = java.util.Objects.requireNonNull(job).deepCopy();
        this.origin = java.util.Objects.requireNonNull(origin).immutable();
        if (runId == null || runId.isBlank()) throw new IllegalArgumentException("Missing placement run ID");
        tag = "farmbench." + JobLoader.text(job, "id") + "." + UUID.randomUUID();
        // 26.2 TicketStorage compares TicketType by object identity, so each service owns a distinct type.
        ticketType = new TicketType(TicketType.NO_TIMEOUT,
            TicketType.FLAG_LOADING | TicketType.FLAG_SIMULATION | TicketType.FLAG_KEEP_DIMENSION_ACTIVE);
        verification.addProperty("run_id", runId);
        verification.addProperty("entity_tag", tag);
        verification.addProperty("mode", JobLoader.text(job, "mode"));
        verification.add("origin", jsonPosition(this.origin));
        verification.add("warnings", warnings);
        warnings.add("Verification is an initial snapshot, not proof that a farm works or stays unchanged while ticking.");
        warnings.add("Vanilla recognized typed NBT is loaded; unknown input fields are reported, not claimed to be preserved by vanilla.");
        warnings.add("Cleanup owns only attempted, initially-air bays and the exact tagged entity UUID inventory. It does not remove dropped items, offspring, or changes outside the bays.");
    }

    public BlockPos origin() { return origin; }

    public void prepare() {
        requireServerThread();
        if (prepared && !cleaned) { verify(); return; }
        if (started) throw new IllegalStateException("This placement attempt cannot be started again");
        started = true;
        state = "preflight";
        try {
            JobLoader.validate(job, JobLoader.text(job, "id"));
            if (JobLoader.integer(job, "data_version", 1, Integer.MAX_VALUE)
                != SharedConstants.getCurrentVersion().dataVersion().version())
                throw new IllegalArgumentException("Job DataVersion differs from the running Minecraft version");
            if (!job.get("origin").isJsonNull()) {
                int[] requested = JobLoader.vector(job.get("origin"), "origin", -30_000_000, 30_000_000);
                if (!origin.equals(new BlockPos(requested[0], requested[1], requested[2])))
                    throw new IllegalArgumentException("Placement origin differs from explicit job origin");
            }
            String dimension = JobLoader.text(job.getAsJsonObject("conditions"), "dimension");
            if (!dimension.equals(level.dimension().identifier().toString()))
                throw new IllegalArgumentException("Placement level differs from job dimension " + dimension);
            Path folder = checkedJobDirectory(FabricLoader.getInstance().getConfigDir(), JobLoader.text(job, "id"));
            // Read and validate every artifact before any world mutation, including counter recolouring.
            for (JsonElement value : job.getAsJsonArray("designs")) designs.add(readDesign(folder, value.getAsJsonObject()));
            for (int i = 0; i < designs.size(); i++) {
                for (int j = 0; j < i; j++) if (designs.get(i).box.intersects(designs.get(j).box))
                    throw new IllegalArgumentException("Overlapping bays: " + designs.get(j).id + " and " + designs.get(i).id);
            }
            acquireTickets();
            for (Design design : designs) preflightWorld(design);
            verification.addProperty("all_bays_preflighted_before_mutation", true);
            state = "placing";
            if (isExisting()) {
                for (Design design : designs) for (Port port : design.ports) {
                    BlockState previous = level.getBlockState(port.sink);
                    if (!previous.equals(design.wool)) {
                        recoloured.put(port.sink, previous);
                        if (!level.setBlock(port.sink, design.wool, Block.UPDATE_ALL))
                            throw new IllegalStateException("Cannot assign counter wool at " + port.sink);
                    }
                }
            } else {
                for (Design design : designs) place(design);
            }
            JsonArray snapshots = new JsonArray();
            verification.add("initial", snapshots);
            // Placement/verification happens synchronously; no benchmark ticks have been run here.
            for (Design design : designs) {
                JsonObject snapshot = inspectBlocks(design, false);
                snapshot.add("entities", inspectEntities(design, true));
                snapshot.addProperty("id", design.id);
                snapshot.addProperty("structure_sha256", design.sha256);
                snapshot.addProperty("artifact_sha256_verified", true);
                snapshot.addProperty("source_data_version", design.dataVersion);
                snapshots.add(snapshot);
            }
            verification.addProperty("initial_server_tick", server.getTickCount());
            prepared = true;
            state = "prepared";
            verify();
        } catch (IOException | RuntimeException error) {
            state = "failed";
            verification.addProperty("error", error.getMessage());
            try { cleanupOwned(); } catch (RuntimeException rollback) { error.addSuppressed(rollback); }
            try { release(); } catch (RuntimeException release) { error.addSuppressed(release); }
            throw new IllegalArgumentException("Farm placement failed: " + error.getMessage(), error);
        }
    }

    /** Runtime verification never re-pastes, re-spawns, or pretends moving entities retain their initial pose. */
    public void verify() {
        requireServerThread();
        if (!prepared || cleaned) throw new IllegalStateException("No prepared placement to verify");
        JsonArray observations = new JsonArray();
        for (Design design : designs) {
            verifyPorts(design, false);
            JsonObject observed = new JsonObject();
            observed.addProperty("id", design.id);
            observed.addProperty("ports", "hopper directions and assigned wool checked");
            observed.addProperty("full_block_states_rechecked", false);
            observed.add("entities", inspectEntities(design, false));
            observations.add(observed);
        }
        verification.add("latest", observations);
        verification.addProperty("latest_server_tick", server.getTickCount());
        verification.addProperty("runtime_scope", "Declared output ports and entity observations only; growth, pistons, inventories, and entity motion are not compared to a static blueprint after ticking.");
    }

    public void cleanup() {
        requireServerThread();
        if (cleaned) { release(); return; }
        RuntimeException failure = null;
        try { cleanupOwned(); cleaned = true; state = "cleaned"; }
        catch (RuntimeException error) { failure = error; state = "cleanup_failed"; }
        try { release(); } catch (RuntimeException error) {
            if (failure == null) failure = error; else failure.addSuppressed(error);
        }
        if (failure != null) throw failure;
    }

    /** Release just our tickets. Builds and entities intentionally remain until explicit cleanup. */
    public void release() {
        requireServerThread();
        for (ChunkPos chunk : new ArrayList<>(tickets)) {
            level.getChunkSource().removeTicketWithRadius(ticketType, chunk, TICKET_RADIUS);
            tickets.remove(chunk);
        }
    }

    public JsonObject report() {
        JsonObject result = verification.deepCopy();
        result.addProperty("state", state);
        result.addProperty("active_chunk_tickets", tickets.size());
        result.addProperty("owned_entity_uuids", ownedEntities.size());
        result.addProperty("recoloured_existing_sinks", recoloured.size());
        return result;
    }

    private Design readDesign(Path folder, JsonObject data) throws IOException {
        String id = JobLoader.text(data, "id");
        Path file = checkedStructurePath(folder, JobLoader.text(data, "structure"));
        if (Files.size(file) > 128_000_000) throw new IllegalArgumentException("Oversized structure " + id);
        // Hash the same bytes we parse, rather than reopening a mutable file after hashing it.
        byte[] bytes = Files.readAllBytes(file);
        String sha = sha256(bytes);
        if (!sha.equals(JobLoader.text(data, "artifact_sha256")))
            throw new IllegalArgumentException("Structure SHA256 mismatch for " + id);
        CompoundTag nbt = NbtIo.readCompressed(new ByteArrayInputStream(bytes), NbtAccounter.create(MAX_NBT_BYTES));
        int version = nbt.getInt("DataVersion").orElseThrow(() -> new IllegalArgumentException("Missing structure DataVersion for " + id));
        if (version != JobLoader.integer(job, "data_version", 1, Integer.MAX_VALUE))
            throw new IllegalArgumentException("Structure DataVersion differs from job for " + id);
        int[] size = JobLoader.vector(data.get("size"), "size", 1, 512);
        if (!java.util.Arrays.equals(size, intVector(requiredList(nbt, "size"), "structure size")))
            throw new IllegalArgumentException("Structure dimensions differ from job for " + id);
        int[] offset = JobLoader.vector(data.get("offset"), "offset", -30_000_000, 30_000_000);
        BlockPos base = checkedOffset(origin, offset[0], offset[1], offset[2]);
        BlockPos last = checkedOffset(base, size[0] - 1, size[1] - 1, size[2] - 1);
        if (level.isOutsideBuildHeight(base) || level.isOutsideBuildHeight(last)
            || !level.getWorldBorder().isWithinBounds(base) || !level.getWorldBorder().isWithinBounds(last))
            throw new IllegalArgumentException("Bay " + id + " exceeds build height or world border");
        if (nbt.contains("palettes")) throw new IllegalArgumentException("Random/multiple palettes are not supported in benchmark structures");
        List<BlockState> palette = new ArrayList<>();
        for (Tag value : requiredList(nbt, "palette")) palette.add(readState(requiredCompound(value, "palette state")));
        if (palette.isEmpty()) throw new IllegalArgumentException("Empty structure palette for " + id);
        Design design = new Design(id, base, size, sha, version,
            wool(JobLoader.text(data, "counter")), new StructureTemplate());
        for (Tag value : requiredList(nbt, "blocks")) {
            CompoundTag entry = requiredCompound(value, "structure block");
            int[] position = intVector(requiredList(entry, "pos"), "block position");
            checkNormalized(position, size, "block position");
            BlockPos relative = new BlockPos(position[0], position[1], position[2]);
            int index = entry.getInt("state").orElseThrow(() -> new IllegalArgumentException("Missing palette index"));
            if (index < 0 || index >= palette.size()) throw new IllegalArgumentException("Invalid palette index for " + id);
            BlockState expected = palette.get(index);
            CompoundTag payload = entry.contains("nbt") ? requiredCompound(entry.get("nbt"), "block entity NBT").copy() : null;
            BlockCell cell = new BlockCell(base.offset(relative), expected, payload);
            if (design.cells.put(relative, cell) != null) throw new IllegalArgumentException("Duplicate structure block at " + relative);
            design.ownedTypes.add(expected.getBlock());
            if (payload != null) {
                // Vanilla temporarily writes a barrier before replacing a block-entity cell.
                design.ownedTypes.add(Blocks.BARRIER);
                payload.putInt("x", cell.position.getX()); payload.putInt("y", cell.position.getY()); payload.putInt("z", cell.position.getZ());
                BlockEntity decoded = BlockEntity.loadStatic(cell.position, expected, payload, level.registryAccess());
                if (decoded == null || !decoded.getType().isValid(expected))
                    throw new IllegalArgumentException("Invalid block entity NBT at " + cell.position);
                cell.canonical = decoded.saveWithFullMetadata(level.registryAccess());
                cell.unrecognized = unrecognizedKeys(payload, cell.canonical, Set.of("x", "y", "z", "id"));
            }
        }
        long volume = (long) size[0] * size[1] * size[2];
        if (design.cells.size() != volume) throw new IllegalArgumentException("Structure must include every bay cell, including explicit clearance air: " + id);
        if (design.ownedTypes.contains(Blocks.PISTON) || design.ownedTypes.contains(Blocks.STICKY_PISTON)) {
            design.ownedTypes.add(Blocks.PISTON_HEAD); design.ownedTypes.add(Blocks.MOVING_PISTON);
        }
        for (JsonElement value : data.getAsJsonArray("ports")) {
            JsonObject port = value.getAsJsonObject();
            int[] p = JobLoader.vector(port.get("position"), "port position", 0, 511);
            Direction direction = direction(JobLoader.text(port, "direction"));
            if (direction == Direction.UP) throw new IllegalArgumentException("Hoppers cannot face up");
            BlockPos relative = new BlockPos(p[0], p[1], p[2]);
            BlockPos sink = relative.relative(direction);
            checkNormalized(new int[]{sink.getX(), sink.getY(), sink.getZ()}, size, "counter sink");
            BlockCell hopper = design.cells.get(relative), sinkCell = design.cells.get(sink);
            if (hopper == null || !hopper.expected.is(Blocks.HOPPER) || hopper.expected.getValue(HopperBlock.FACING) != direction)
                throw new IllegalArgumentException("Declared port is not the expected facing hopper in " + id);
            if (sinkCell == null || !sinkCell.expected.equals(design.wool))
                throw new IllegalArgumentException("Structure sink is not assigned counter wool in " + id);
            if (design.ports.stream().anyMatch(existing -> existing.hopper.equals(base.offset(relative))))
                throw new IllegalArgumentException("Duplicate counter port in " + id);
            design.ports.add(new Port(base.offset(relative), base.offset(sink), direction));
        }
        design.template.load(BuiltInRegistries.BLOCK, nbt);
        readEntities(design, requiredList(nbt, "entities"));
        if (design.entities.size() != JobLoader.integer(data, "entity_count", 0, 10_000))
            throw new IllegalArgumentException("Structure entity count (including passengers) differs from job for " + id);
        return design;
    }

    private void readEntities(Design design, ListTag entries) {
        List<CompoundTag> payloads = new ArrayList<>();
        Map<UUID, UUID> identities = new HashMap<>();
        for (Tag value : entries) {
            CompoundTag entry = requiredCompound(value, "structure entity");
            Vec3 relative = vector(requiredList(entry, "pos"), "entity position");
            checkNormalized(relative, design.size, "entity position");
            int[] attachment = intVector(requiredList(entry, "blockPos"), "entity blockPos");
            checkNormalized(attachment, design.size, "entity blockPos");
            CompoundTag payload = requiredCompound(entry.get("nbt"), "entity NBT").copy();
            payload.put("Pos", positionTag(relative));
            if (isBlockAttached(payload.getStringOr("id", "")) && !payload.contains("block_pos"))
                payload.putIntArray("block_pos", attachment);
            validateEntityCoordinates(payload, design.size, relative, 0);
            assignIdentities(payload, identities, 0);
            payloads.add(payload);
        }
        for (CompoundTag payload : payloads) {
            translateEntityNbt(payload, design.base, identities);
            List<EntityExpectation> expected = new ArrayList<>();
            collectEntityExpectations(payload, null, expected, 0);
            ProblemReporter.Collector problems = new ProblemReporter.Collector();
            Entity root = EntityType.loadEntityRecursive(TagValueInput.create(problems, level.registryAccess(), payload),
                level, new EntitySpawnRequest(EntitySpawnReason.STRUCTURE, false), entity -> entity);
            if (!problems.isEmpty()) throw new IllegalArgumentException("Invalid entity NBT in " + design.id + ": " + problems.getReport());
            if (root == null) throw new IllegalArgumentException("Vanilla could not load an entity in " + design.id);
            List<Entity> actual = root.getSelfAndPassengers().toList();
            if (actual.size() != expected.size()) throw new IllegalArgumentException("Vanilla omitted or detached a passenger in " + design.id);
            Map<UUID, Entity> byUuid = new HashMap<>();
            for (Entity entity : actual) byUuid.put(entity.getUUID(), entity);
            for (EntityExpectation expectation : expected) {
                Entity entity = byUuid.get(expectation.uuid);
                if (entity == null || !expectation.type.equals(EntityType.getKey(entity.getType()).toString())
                    || !java.util.Objects.equals(expectation.parent, entity.getVehicle() == null ? null : entity.getVehicle().getUUID()))
                    throw new IllegalArgumentException("Entity identity/type/passenger relationship changed while loading " + design.id);
                if (!isExisting() && !entity.addTag(tag)) throw new IllegalArgumentException("Cannot tag entity in " + design.id + " (too many entity tags)");
                expectation.entity = entity;
                expectation.canonical = saveEntity(entity);
                expectation.unrecognized = unrecognizedKeys(expectation.payload, expectation.canonical, ENTITY_METADATA);
                design.entities.add(expectation);
            }
            design.roots.add(root);
        }
    }

    private void assignIdentities(CompoundTag payload, Map<UUID, UUID> identities, int depth) {
        if (depth > 64 || reservedUuids.size() >= 10_000) throw new IllegalArgumentException("Entity passenger tree exceeds limits");
        String id = payload.getStringOr("id", "");
        Identifier identifier = Identifier.tryParse(id);
        if (identifier == null || !identifier.getNamespace().equals("minecraft") || !BuiltInRegistries.ENTITY_TYPE.containsKey(identifier))
            throw new IllegalArgumentException("Unknown/non-vanilla entity type " + id);
        UUID old = readIdentity(payload);
        UUID fresh;
        do { fresh = UUID.randomUUID(); } while (reservedUuids.contains(fresh) || level.getEntityInAnyDimension(fresh) != null);
        reservedUuids.add(fresh);
        if (old != null && identities.putIfAbsent(old, fresh) != null) throw new IllegalArgumentException("Duplicate persistent entity UUID in structure");
        payload.remove("UUIDMost"); payload.remove("UUIDLeast");
        payload.store("UUID", UUIDUtil.CODEC, fresh);
        if (payload.contains("Passengers")) for (Tag child : requiredList(payload, "Passengers"))
            assignIdentities(requiredCompound(child, "passenger"), identities, depth + 1);
    }

    private void collectEntityExpectations(CompoundTag payload, UUID parent, List<EntityExpectation> output, int depth) {
        if (depth > 64) throw new IllegalArgumentException("Entity passenger tree exceeds limits");
        UUID uuid = readIdentity(payload);
        Vec3 position = vector(requiredList(payload, "Pos"), "entity Pos");
        ListTag rotation = payload.contains("Rotation") ? requiredList(payload, "Rotation") : null;
        if (rotation != null && rotation.size() != 2) throw new IllegalArgumentException("Entity Rotation needs yaw and pitch");
        float yaw = rotation == null ? 0 : (float) finiteNumber(rotation.get(0), "entity yaw");
        float pitch = rotation == null ? 0 : (float) finiteNumber(rotation.get(1), "entity pitch");
        if (!Float.isFinite(yaw) || !Float.isFinite(pitch)) throw new IllegalArgumentException("Nonfinite entity rotation");
        output.add(new EntityExpectation(uuid, payload.getStringOr("id", ""), position, yaw, pitch, parent, payload.copy()));
        if (payload.contains("Passengers")) for (Tag child : requiredList(payload, "Passengers"))
            collectEntityExpectations(requiredCompound(child, "passenger"), uuid, output, depth + 1);
    }

    private void acquireTickets() {
        for (Design design : designs) {
            int minX = design.base.getX() >> 4, minZ = design.base.getZ() >> 4;
            int maxX = (design.base.getX() + design.size[0] - 1) >> 4;
            int maxZ = (design.base.getZ() + design.size[2] - 1) >> 4;
            for (int x = minX; x <= maxX; x++) for (int z = minZ; z <= maxZ; z++) {
                ChunkPos chunk = new ChunkPos(x, z);
                if (!tickets.add(chunk)) continue;
                var loading = level.getChunkSource().addTicketAndLoadWithRadius(ticketType, chunk, TICKET_RADIUS);
                server.managedBlock(loading::isDone);
                Object loaded = loading.join();
                if (loaded instanceof net.minecraft.server.level.ChunkResult<?> result && !result.isSuccess())
                    throw new IllegalStateException("Cannot load placement chunks: " + result.getError());
                // FULL chunks alone are not proof that saved unrelated entities have been loaded.
                level.waitForEntities(chunk, TICKET_RADIUS);
            }
        }
    }

    private void preflightWorld(Design design) {
        if (isExisting()) {
            inspectBlocks(design, true);
            matchExistingEntities(design);
            inspectEntities(design, true);
        } else {
            for (BlockCell cell : design.cells.values()) if (!level.getBlockState(cell.position).isAir())
                throw new IllegalArgumentException("Bay " + design.id + " is not empty at " + cell.position + " (fluids are not air)");
            List<Entity> collisions = level.getEntities((Entity) null, design.box, entity -> !entity.isRemoved());
            if (!collisions.isEmpty()) throw new IllegalArgumentException("Existing entity intersects bay " + design.id + ": " + collisions.getFirst().getUUID());
            for (EntityExpectation expected : design.entities) {
                checkNormalized(expected.position.subtract(design.base.getX(), design.base.getY(), design.base.getZ()), design.size, "passenger position");
                if (!level.getEntities((Entity) null, expected.entity.getBoundingBox(), entity -> !entity.isRemoved()).isEmpty())
                    throw new IllegalArgumentException("Existing entity intersects planned entity outside bay " + design.id);
                for (Design other : designs) if (other != design && expected.entity.getBoundingBox().intersects(other.box))
                    throw new IllegalArgumentException("Entity in " + design.id + " crosses another design's bay");
            }
        }
    }

    private void matchExistingEntities(Design design) {
        List<Entity> candidates = new ArrayList<>(level.getEntities((Entity) null, design.box, entity -> !entity.isRemoved()));
        if (candidates.size() != design.entities.size())
            throw new IllegalArgumentException("Existing entity count differs from structure in " + design.id + "; no entities will be spawned");
        Map<UUID, Entity> matching = new HashMap<>();
        for (EntityExpectation expected : design.entities) {
            Entity parent = expected.parent == null ? null : matching.get(expected.parent);
            Entity found = null;
            double best = Double.POSITIVE_INFINITY;
            for (Entity candidate : candidates) {
                if (!expected.type.equals(EntityType.getKey(candidate.getType()).toString()) || candidate.getVehicle() != parent) continue;
                double distance = candidate.position().distanceToSqr(expected.position);
                if (distance < best) { found = candidate; best = distance; }
            }
            if (found == null) throw new IllegalArgumentException("Existing entity type/passenger tree differs from structure in " + design.id);
            candidates.remove(found);
            matching.put(expected.uuid, found);
            expected.entity = found;
            // Existing entities are inspected, never retagged, assigned fresh identities, or owned for deletion.
        }
    }

    private void place(Design design) {
        design.attempted = true; // Entire footprint was proven air, before any design was placed.
        StructurePlaceSettings settings = new StructurePlaceSettings().setIgnoreEntities(true).setFinalizeEntities(false);
        if (!design.template.placeInWorld(level, design.base, design.base, settings, RandomSource.create(), Block.UPDATE_CLIENTS))
            throw new IllegalStateException("Vanilla structure placement failed for " + design.id);
        for (Entity root : design.roots) {
            // Inventory before insertion: even partially accepted entity trees can be rolled back safely.
            root.getSelfAndPassengers().forEach(entity -> ownedEntities.put(entity.getUUID(), entity));
            if (!level.tryAddFreshEntityWithPassengers(root))
                throw new IllegalStateException("Vanilla rejected an entity UUID/tree in " + design.id);
            for (Entity entity : root.getSelfAndPassengers().toList()) if (level.getEntityInAnyDimension(entity.getUUID()) != entity)
                throw new IllegalStateException("Entity/passenger was not inserted for " + design.id);
        }
    }

    private JsonObject inspectBlocks(Design design, boolean beforeRecolour) {
        JsonObject result = new JsonObject();
        JsonArray differences = new JsonArray(), blockEntities = new JsonArray();
        int exact = 0, permitted = 0, suppliedBlockEntities = 0, observedBlockEntities = 0;
        for (BlockCell cell : design.cells.values()) {
            BlockState actual = level.getBlockState(cell.position);
            boolean recolourSink = beforeRecolour && design.ports.stream().anyMatch(port -> port.sink.equals(cell.position));
            if (actual.equals(cell.expected)) exact++;
            else if ((recolourSink && actual.is(BlockTags.WOOL)) || transientStateDifference(cell.expected, actual)) {
                permitted++;
                if (differences.size() < 100) differences.add(stateDifference(cell.position, cell.expected, actual, recolourSink ? "counter wool recolouring" : "transient properties or equivalent air"));
            } else throw new IllegalArgumentException("Block/state mismatch in " + design.id + " at " + cell.position
                + ": expected " + cell.expected + ", found " + actual + ". Existing moving pistons must settle before verification.");
            BlockEntity actualEntity = level.getBlockEntity(cell.position);
            if (cell.expected.hasBlockEntity()) {
                if (actualEntity == null || !actualEntity.getType().isValid(cell.expected))
                    throw new IllegalArgumentException("Missing/wrong block entity at " + cell.position);
                observedBlockEntities++;
            } else if (actualEntity != null) throw new IllegalArgumentException("Unexpected block entity at " + cell.position);
            if (cell.canonical != null) {
                suppliedBlockEntities++;
                CompoundTag observed = actualEntity.saveWithFullMetadata(level.registryAccess());
                JsonObject nbtReport = compareCanonical(cell.canonical, observed, BLOCK_ENTITY_DYNAMICS,
                    isExisting() ? cell.payload.keySet() : null, cell.unrecognized, "block entity at " + cell.position);
                nbtReport.add("position", jsonPosition(cell.position));
                blockEntities.add(nbtReport);
            }
        }
        verifyPorts(design, beforeRecolour);
        result.addProperty("cells_checked", design.cells.size());
        result.addProperty("exact_state_matches", exact);
        result.addProperty("permitted_nonexact_states", permitted);
        result.addProperty("all_states_exact", permitted == 0);
        result.add("nonexact_state_examples", differences);
        result.addProperty("nonexact_examples_truncated", permitted > differences.size());
        result.addProperty("block_entities_present", observedBlockEntities);
        result.addProperty("supplied_block_entity_payloads_checked", suppliedBlockEntities);
        result.add("block_entity_nbt", blockEntities);
        result.addProperty("block_entity_scope", "Types/presence plus vanilla canonical typed NBT; runtime fields and unknown input fields are excluded and reported. Existing mode compares supplied recognized fields only.");
        return result;
    }

    private JsonObject inspectEntities(Design design, boolean initial) {
        JsonObject result = new JsonObject();
        JsonArray observations = new JsonArray();
        int present = 0, exactPoses = 0, missing = 0;
        for (EntityExpectation expected : design.entities) {
            Entity entity = isExisting() ? expected.entity : level.getEntityInAnyDimension(expected.uuid);
            if (entity == null || entity.isRemoved()) {
                missing++;
                if (initial) throw new IllegalArgumentException("Missing placed entity in " + design.id);
                continue;
            }
            present++;
            if (!expected.type.equals(EntityType.getKey(entity.getType()).toString()))
                throw new IllegalArgumentException("Wrong entity type in " + design.id);
            Entity expectedParent = expected.parent == null ? null : design.entities.stream()
                .filter(parent -> parent.uuid.equals(expected.parent)).findFirst().orElseThrow().entity;
            boolean relationship = entity.getVehicle() == expectedParent;
            if (initial && !relationship) throw new IllegalArgumentException("Wrong entity passenger relationship in " + design.id);
            boolean pose = entity.position().distanceToSqr(expected.position) <= POSE_TOLERANCE * POSE_TOLERANCE
                && angleDistance(entity.getYRot(), expected.yaw) < 0.01 && angleDistance(entity.getXRot(), expected.pitch) < 0.01;
            if (pose) exactPoses++;
            JsonObject observation = new JsonObject();
            observation.addProperty("type", expected.type);
            observation.addProperty("uuid", entity.getUUID().toString());
            observation.addProperty("initial_pose_matches", pose);
            observation.addProperty("passenger_relationship_matches", relationship);
            observation.add("expected_position", jsonPosition(expected.position));
            observation.add("actual_position", jsonPosition(entity.position()));
            observation.addProperty("expected_yaw", expected.yaw); observation.addProperty("actual_yaw", entity.getYRot());
            observation.addProperty("expected_pitch", expected.pitch); observation.addProperty("actual_pitch", entity.getXRot());
            if (initial) {
                observation.add("nbt", compareCanonical(expected.canonical, saveEntity(entity), union(ENTITY_METADATA, ENTITY_DYNAMICS),
                    isExisting() ? expected.payload.keySet() : null, expected.unrecognized, "entity " + expected.type));
            }
            observations.add(observation);
        }
        result.addProperty("expected_including_passengers", design.entities.size());
        result.addProperty("observed", present);
        result.addProperty("missing", missing);
        result.addProperty("matching_initial_poses", exactPoses);
        result.addProperty("spawned_by_service", !isExisting());
        result.addProperty("scope", initial ? "Count, types, passenger relationships, observed positions/rotations, and recognized canonical NBT. Pose differences are reported, not treated as exact verification."
            : "Current UUID/type/presence and pose observations only; entities may move, transform, die, or detach during operation. No respawning.");
        result.add("observations", observations);
        return result;
    }

    private void verifyPorts(Design design, boolean allowOtherWool) {
        for (Port port : design.ports) {
            BlockState hopper = level.getBlockState(port.hopper), sink = level.getBlockState(port.sink);
            if (!hopper.is(Blocks.HOPPER) || hopper.getValue(HopperBlock.FACING) != port.direction)
                throw new IllegalArgumentException("Output hopper direction changed at " + port.hopper);
            if (!(allowOtherWool ? sink.is(BlockTags.WOOL) : sink.equals(design.wool)))
                throw new IllegalArgumentException("Output sink is not " + design.wool + " at " + port.sink);
        }
    }

    private void cleanupOwned() {
        if (designs.stream().anyMatch(design -> design.attempted) || !recoloured.isEmpty() || !ownedEntities.isEmpty())
            acquireTickets();
        int removedEntities = 0, discardedUninserted = 0, removedBlocks = 0, restoredSinks = 0, preservedBlocks = 0, untagged = 0;
        JsonArray unresolved = new JsonArray();
        for (Map.Entry<UUID, Entity> entry : new ArrayList<>(ownedEntities.entrySet())) {
            Entity live = level.getEntityInAnyDimension(entry.getKey());
            boolean inserted = live != null;
            if (live == null) {
                Entity previous = entry.getValue();
                if (previous.isRemoved() && previous.getRemovalReason() != Entity.RemovalReason.KILLED
                    && previous.getRemovalReason() != Entity.RemovalReason.DISCARDED) {
                    unresolved.add(entry.getKey().toString());
                    continue; // Never lose the identity inventory of an unloaded/transferred entity.
                }
                live = previous;
            }
            if (!live.isRemoved() && live.getUUID().equals(entry.getKey())) {
                if (live.entityTags().contains(tag)) {
                    // Never traverse the current passenger tree: unrelated riders are not our entities.
                    live.ejectPassengers(); live.removeVehicle(); live.discard();
                    if (inserted) removedEntities++; else discardedUninserted++;
                } else untagged++;
            }
            ownedEntities.remove(entry.getKey());
        }
        if (isExisting()) {
            for (Map.Entry<BlockPos, BlockState> entry : new ArrayList<>(recoloured.entrySet())) {
                Design owner = designs.stream().filter(design -> design.ports.stream().anyMatch(port -> port.sink.equals(entry.getKey()))).findFirst().orElseThrow();
                if (level.getBlockState(entry.getKey()).equals(owner.wool)) {
                    if (!level.setBlock(entry.getKey(), entry.getValue(), Block.UPDATE_ALL))
                        throw new IllegalStateException("Cannot restore existing counter wool at " + entry.getKey());
                    restoredSinks++;
                } else preservedBlocks++;
                recoloured.remove(entry.getKey());
            }
        } else for (Design design : designs) if (design.attempted) {
            for (BlockCell cell : design.cells.values()) {
                BlockState actual = level.getBlockState(cell.position);
                if (actual.isAir()) continue;
                // Only an initially-air, attempted bay and a known job block type qualify. Leave unknown later additions alone.
                if (!design.ownedTypes.contains(actual.getBlock())) { preservedBlocks++; continue; }
                level.removeBlockEntity(cell.position);
                if (!level.setBlock(cell.position, Blocks.AIR.defaultBlockState(), Block.UPDATE_CLIENTS | Block.UPDATE_SUPPRESS_DROPS | Block.UPDATE_SKIP_ALL_SIDEEFFECTS))
                    throw new IllegalStateException("Cannot remove owned job block at " + cell.position);
                removedBlocks++;
            }
            design.attempted = false;
        }
        JsonObject cleanup = new JsonObject();
        cleanup.addProperty("removed_owned_entities", removedEntities);
        cleanup.addProperty("discarded_uninserted_entity_objects", discardedUninserted);
        cleanup.addProperty("removed_owned_blocks", removedBlocks);
        cleanup.addProperty("restored_existing_wool", restoredSinks);
        cleanup.addProperty("preserved_unknown_or_changed_blocks", preservedBlocks);
        cleanup.addProperty("preserved_entities_with_removed_ownership_tag", untagged);
        cleanup.add("unresolved_unloaded_or_transferred_entity_uuids", unresolved);
        cleanup.addProperty("scope", "Initially-air attempted bays, restricted to job block types; exact UUID plus ownership tag for entities. Same-type later player blocks inside an owned bay cannot be distinguished from moved/grown farm blocks.");
        verification.add("cleanup", cleanup);
        if (!unresolved.isEmpty()) throw new IllegalStateException("Cleanup incomplete: owned entities left their loaded bays or dimension; UUID inventory retained. Load their chunks and retry cleanup.");
    }

    private CompoundTag saveEntity(Entity entity) {
        ProblemReporter.Collector problems = new ProblemReporter.Collector();
        TagValueOutput output = TagValueOutput.createWithContext(problems, level.registryAccess());
        if (!entity.save(output) || !problems.isEmpty()) throw new IllegalArgumentException("Entity cannot be serialized for verification: " + EntityType.getKey(entity.getType()) + " " + problems.getReport());
        return output.buildResult();
    }

    private static JsonObject compareCanonical(CompoundTag expected, CompoundTag actual, Set<String> excluded,
                                              Set<String> suppliedOnly, JsonArray unrecognized, String context) {
        JsonObject report = new JsonObject();
        JsonArray checked = new JsonArray(), notChecked = new JsonArray();
        for (String key : expected.keySet()) {
            if (excluded.contains(key) || (suppliedOnly != null && !suppliedOnly.contains(key))) {
                if (suppliedOnly == null || suppliedOnly.contains(key)) notChecked.add(key);
                continue;
            }
            if (!NbtUtils.compareNbt(expected.get(key), actual.get(key), false))
                throw new IllegalArgumentException("Recognized typed NBT field " + key + " differs for " + context);
            checked.add(key);
        }
        report.add("recognized_fields_checked", checked);
        report.add("dynamic_or_metadata_fields_not_checked", notChecked);
        report.add("unrecognized_input_fields", unrecognized.deepCopy());
        report.addProperty("raw_nbt_fully_verified", false);
        return report;
    }

    private static JsonArray unrecognizedKeys(CompoundTag source, CompoundTag canonical, Set<String> metadata) {
        JsonArray result = new JsonArray();
        for (String key : source.keySet()) if (!metadata.contains(key) && !canonical.contains(key)) result.add(key);
        return result;
    }

    private static Set<String> union(Set<String> first, Set<String> second) {
        Set<String> result = new HashSet<>(first); result.addAll(second); return result;
    }

    /** Translate only vanilla entity spatial fields, preserving typed payloads and complete passenger trees. */
    static void translateEntityNbt(CompoundTag payload, BlockPos base, Map<UUID, UUID> identities) {
        translateEntityNbt(payload, base, identities, null, 0);
    }

    private static void translateEntityNbt(CompoundTag payload, BlockPos base, Map<UUID, UUID> identities, Vec3 parentPosition, int depth) {
        if (depth > 64) throw new IllegalArgumentException("Entity passenger tree exceeds limits");
        Vec3 relative = payload.contains("Pos") ? vector(requiredList(payload, "Pos"), "entity Pos") : parentPosition;
        if (relative == null) throw new IllegalArgumentException("Missing entity position");
        payload.put("Pos", positionTag(relative.add(base.getX(), base.getY(), base.getZ())));
        if (payload.contains("block_pos")) {
            int[] value = blockPosition(payload.get("block_pos"));
            BlockPos translated = checkedOffset(base, value[0], value[1], value[2]);
            payload.putIntArray("block_pos", new int[]{translated.getX(), translated.getY(), translated.getZ()});
        }
        if (payload.contains("TileX") || payload.contains("TileY") || payload.contains("TileZ")) {
            int x = requiredInt(payload, "TileX"), y = requiredInt(payload, "TileY"), z = requiredInt(payload, "TileZ");
            BlockPos translated = checkedOffset(base, x, y, z);
            payload.putInt("TileX", translated.getX()); payload.putInt("TileY", translated.getY()); payload.putInt("TileZ", translated.getZ());
        }
        // UUID references within this exported tree follow new identities; unrelated reference UUIDs remain untouched.
        rewriteUuidReferences(payload, identities);
        if (payload.contains("Passengers")) for (Tag child : requiredList(payload, "Passengers"))
            translateEntityNbt(requiredCompound(child, "passenger"), base, identities, relative, depth + 1);
    }

    private static void validateEntityCoordinates(CompoundTag payload, int[] size, Vec3 parent, int depth) {
        if (depth > 64) throw new IllegalArgumentException("Entity passenger tree exceeds limits");
        Vec3 position = payload.contains("Pos") ? vector(requiredList(payload, "Pos"), "entity Pos") : parent;
        checkNormalized(position, size, "entity/passenger Pos");
        if (payload.contains("block_pos")) checkNormalized(blockPosition(payload.get("block_pos")), size, "entity block_pos");
        if (payload.contains("TileX") || payload.contains("TileY") || payload.contains("TileZ"))
            checkNormalized(new int[]{requiredInt(payload, "TileX"), requiredInt(payload, "TileY"), requiredInt(payload, "TileZ")}, size, "entity TileXYZ");
        if (payload.contains("Passengers")) for (Tag child : requiredList(payload, "Passengers"))
            validateEntityCoordinates(requiredCompound(child, "passenger"), size, position, depth + 1);
    }

    private static int[] blockPosition(Tag value) {
        if (value instanceof CompoundTag compound)
            return new int[]{requiredInt(compound, "x"), requiredInt(compound, "y"), requiredInt(compound, "z")};
        if (value instanceof ListTag list) return intVector(list, "entity block_pos");
        int[] parts = value.asIntArray().orElseThrow(() -> new IllegalArgumentException("Entity block_pos must be a three-integer coordinate"));
        if (parts.length != 3) throw new IllegalArgumentException("Entity block_pos must have three integers");
        return parts;
    }

    private static void rewriteUuidReferences(CompoundTag payload, Map<UUID, UUID> identities) {
        for (String key : new ArrayList<>(payload.keySet())) {
            if (key.equals("Passengers")) continue; // Translated recursively separately.
            Tag value = payload.get(key);
            if (value instanceof CompoundTag compound) rewriteUuidReferences(compound, identities);
            else if (value instanceof ListTag list) rewriteUuidList(list, identities);
            else {
                UUID old = uuidTag(value);
                UUID replacement = old == null ? null : identities.get(old);
                if (replacement != null) {
                    if (value.getId() == Tag.TAG_STRING) payload.putString(key, replacement.toString());
                    else payload.putIntArray(key, UUIDUtil.uuidToIntArray(replacement));
                }
            }
        }
    }

    private static void rewriteUuidList(ListTag list, Map<UUID, UUID> identities) {
        for (int i = 0; i < list.size(); i++) {
            Tag value = list.get(i);
            if (value instanceof CompoundTag compound) rewriteUuidReferences(compound, identities);
            else if (value instanceof ListTag nested) rewriteUuidList(nested, identities);
            else {
                UUID old = uuidTag(value);
                UUID replacement = old == null ? null : identities.get(old);
                if (replacement != null) list.set(i, value.getId() == Tag.TAG_STRING
                    ? net.minecraft.nbt.StringTag.valueOf(replacement.toString())
                    : new net.minecraft.nbt.IntArrayTag(UUIDUtil.uuidToIntArray(replacement)));
            }
        }
    }

    private static UUID readIdentity(CompoundTag payload) {
        if (payload.contains("UUID")) {
            UUID uuid = uuidTag(payload.get("UUID"));
            if (uuid == null) throw new IllegalArgumentException("Malformed entity UUID");
            return uuid;
        }
        if (payload.contains("UUIDMost") || payload.contains("UUIDLeast"))
            return new UUID(payload.getLong("UUIDMost").orElseThrow(() -> new IllegalArgumentException("Incomplete UUIDMost/UUIDLeast")),
                payload.getLong("UUIDLeast").orElseThrow(() -> new IllegalArgumentException("Incomplete UUIDMost/UUIDLeast")));
        return null;
    }

    private static UUID uuidTag(Tag value) {
        if (value == null) return null;
        if (value.asIntArray().isPresent()) {
            int[] parts = value.asIntArray().get();
            return parts.length == 4 ? UUIDUtil.uuidFromIntArray(parts) : null;
        }
        if (value.asString().isPresent()) {
            try { return UUID.fromString(value.asString().get()); } catch (IllegalArgumentException ignored) { return null; }
        }
        return null;
    }

    static Path checkedJobDirectory(Path config, String id) throws IOException {
        if (!id.matches("[a-z0-9][a-z0-9_-]{0,63}")) throw new IllegalArgumentException("Invalid job ID");
        Path jobs = config.resolve("farmbench/jobs").toRealPath();
        Path folder = jobs.resolve(id).toRealPath();
        if (!folder.equals(jobs.resolve(id))) throw new IllegalArgumentException("Job directory is an alias or escapes config/farmbench/jobs");
        return folder;
    }

    static Path checkedStructurePath(Path folder, String filename) throws IOException {
        if (!filename.matches("[a-zA-Z0-9_-]+\\.nbt")) throw new IllegalArgumentException("Structure must be a local .nbt filename");
        Path realFolder = folder.toRealPath(), file = realFolder.resolve(filename).toRealPath();
        if (!file.getParent().equals(realFolder) || !Files.isRegularFile(file))
            throw new IllegalArgumentException("Structure escapes its job directory");
        return file;
    }

    static BlockPos checkedOffset(BlockPos base, int x, int y, int z) {
        int px = Math.addExact(base.getX(), x), py = Math.addExact(base.getY(), y), pz = Math.addExact(base.getZ(), z);
        if (Math.abs((long) px) >= 30_000_000 || Math.abs((long) pz) >= 30_000_000)
            throw new IllegalArgumentException("Placement exceeds Minecraft horizontal coordinate bounds");
        return new BlockPos(px, py, pz);
    }

    private static BlockState readState(CompoundTag tag) {
        String name = tag.getStringOr("Name", "");
        Identifier id = Identifier.tryParse(name);
        if (id == null || !id.getNamespace().equals("minecraft") || !BuiltInRegistries.BLOCK.containsKey(id))
            throw new IllegalArgumentException("Unknown/non-vanilla palette block " + name);
        BlockState state = NbtUtils.readBlockState(BuiltInRegistries.BLOCK, tag);
        if (tag.contains("Properties")) for (Map.Entry<String, Tag> entry : requiredCompound(tag.get("Properties"), "block Properties").entrySet()) {
            Property<?> property = state.getBlock().getStateDefinition().getProperty(entry.getKey());
            String value = entry.getValue().asString().orElseThrow(() -> new IllegalArgumentException("Block property must be a string"));
            if (property == null || property.getValue(value).isEmpty() || !propertyValue(state, property).equals(value))
                throw new IllegalArgumentException("Unknown/invalid palette property " + name + "[" + entry.getKey() + "=" + value + "]");
        }
        return state;
    }

    static boolean transientStateDifference(BlockState expected, BlockState actual) {
        if (expected.isAir() && actual.isAir()) return true;
        if (expected.getBlock() != actual.getBlock()) return false;
        for (Property<?> property : expected.getProperties()) {
            if (propertyValue(expected, property).equals(propertyValue(actual, property))) continue;
            boolean railShape = property.getName().equals("shape") && expected.getBlock() instanceof net.minecraft.world.level.block.BaseRailBlock;
            if (!TRANSIENT_PROPERTIES.contains(property.getName()) && !railShape) return false;
        }
        return true;
    }

    private static <T extends Comparable<T>> String propertyValue(BlockState state, Property<T> property) {
        return property.getName(state.getValue(property));
    }

    private static BlockState wool(String colour) {
        Identifier id = Identifier.parse("minecraft:" + colour + "_wool");
        if (!BuiltInRegistries.BLOCK.containsKey(id)) throw new IllegalArgumentException("Unknown counter colour " + colour);
        BlockState state = BuiltInRegistries.BLOCK.getValue(id).defaultBlockState();
        if (!state.is(BlockTags.WOOL)) throw new IllegalArgumentException("Counter is not wool: " + colour);
        return state;
    }

    private static Direction direction(String name) {
        return switch (name) {
            case "down" -> Direction.DOWN; case "up" -> Direction.UP;
            case "north" -> Direction.NORTH; case "south" -> Direction.SOUTH;
            case "west" -> Direction.WEST; case "east" -> Direction.EAST;
            default -> throw new IllegalArgumentException("Unknown port direction " + name);
        };
    }

    private static boolean isBlockAttached(String id) {
        return Set.of("minecraft:painting", "minecraft:item_frame", "minecraft:glow_item_frame", "minecraft:leash_knot").contains(id);
    }

    private static int requiredInt(CompoundTag compound, String key) {
        return compound.getInt(key).orElseThrow(() -> new IllegalArgumentException("Missing integer " + key));
    }

    private static CompoundTag requiredCompound(Tag tag, String context) {
        if (!(tag instanceof CompoundTag compound)) throw new IllegalArgumentException(context + " must be a compound");
        return compound;
    }

    private static ListTag requiredList(CompoundTag compound, String key) {
        return compound.getList(key).orElseThrow(() -> new IllegalArgumentException("Missing/malformed NBT list " + key));
    }

    private static int[] intVector(ListTag list, String context) {
        if (list.size() != 3) throw new IllegalArgumentException(context + " must have three integers");
        int[] result = new int[3];
        for (int i = 0; i < 3; i++) {
            if (list.get(i).getId() != Tag.TAG_INT) throw new IllegalArgumentException(context + " must contain NBT integers");
            result[i] = list.get(i).asInt().orElseThrow();
        }
        return result;
    }

    private static Vec3 vector(ListTag list, String context) {
        if (list.size() != 3) throw new IllegalArgumentException(context + " must have three numbers");
        return new Vec3(finiteNumber(list.get(0), context), finiteNumber(list.get(1), context), finiteNumber(list.get(2), context));
    }

    private static double finiteNumber(Tag tag, String context) {
        double value = tag.asDouble().orElseThrow(() -> new IllegalArgumentException(context + " must be numeric"));
        if (!Double.isFinite(value)) throw new IllegalArgumentException(context + " must be finite");
        return value;
    }

    private static ListTag positionTag(Vec3 position) {
        ListTag result = new ListTag(); result.add(DoubleTag.valueOf(position.x));
        result.add(DoubleTag.valueOf(position.y)); result.add(DoubleTag.valueOf(position.z)); return result;
    }

    private static void checkNormalized(int[] position, int[] size, String context) {
        for (int i = 0; i < 3; i++) if (position[i] < 0 || position[i] >= size[i])
            throw new IllegalArgumentException(context + " is outside normalized structure bounds");
    }

    private static void checkNormalized(Vec3 position, int[] size, String context) {
        double[] values = {position.x, position.y, position.z};
        for (int i = 0; i < 3; i++) if (!Double.isFinite(values[i]) || values[i] < 0 || values[i] >= size[i])
            throw new IllegalArgumentException(context + " is outside normalized structure bounds");
    }

    private static String sha256(byte[] bytes) {
        try { return HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(bytes)); }
        catch (NoSuchAlgorithmException impossible) { throw new AssertionError(impossible); }
    }

    private static JsonObject stateDifference(BlockPos position, BlockState expected, BlockState actual, String reason) {
        JsonObject difference = new JsonObject(); difference.add("position", jsonPosition(position));
        difference.addProperty("expected", NbtUtils.writeBlockState(expected).toString());
        difference.addProperty("actual", NbtUtils.writeBlockState(actual).toString()); difference.addProperty("reason", reason); return difference;
    }

    private static JsonArray jsonPosition(BlockPos position) {
        JsonArray result = new JsonArray(); result.add(position.getX()); result.add(position.getY()); result.add(position.getZ()); return result;
    }

    private static JsonArray jsonPosition(Vec3 position) {
        JsonArray result = new JsonArray(); result.add(position.x); result.add(position.y); result.add(position.z); return result;
    }

    private static double angleDistance(float first, float second) {
        return Math.abs(((first - second + 540.0) % 360.0 + 360.0) % 360.0 - 180.0);
    }

    private boolean isExisting() { return "existing".equals(JobLoader.text(job, "mode")); }
    private void requireServerThread() {
        if (!server.isSameThread()) throw new IllegalStateException("Farm placement must run on the Minecraft server thread");
    }

    private record Port(BlockPos hopper, BlockPos sink, Direction direction) {}

    private static final class BlockCell {
        final BlockPos position;
        final BlockState expected;
        final CompoundTag payload;
        CompoundTag canonical;
        JsonArray unrecognized = new JsonArray();
        BlockCell(BlockPos position, BlockState expected, CompoundTag payload) {
            this.position = position; this.expected = expected; this.payload = payload;
        }
    }

    private static final class EntityExpectation {
        final UUID uuid;
        final String type;
        final Vec3 position;
        final float yaw, pitch;
        final UUID parent;
        final CompoundTag payload;
        Entity entity;
        CompoundTag canonical;
        JsonArray unrecognized = new JsonArray();
        EntityExpectation(UUID uuid, String type, Vec3 position, float yaw, float pitch, UUID parent, CompoundTag payload) {
            this.uuid = uuid; this.type = type; this.position = position; this.yaw = yaw; this.pitch = pitch;
            this.parent = parent; this.payload = payload;
        }
    }

    private static final class Design {
        final String id, sha256;
        final BlockPos base;
        final int[] size;
        final int dataVersion;
        final AABB box;
        final BlockState wool;
        final StructureTemplate template;
        final Map<BlockPos, BlockCell> cells = new LinkedHashMap<>();
        final Set<Block> ownedTypes = new HashSet<>();
        final List<Port> ports = new ArrayList<>();
        final List<Entity> roots = new ArrayList<>();
        final List<EntityExpectation> entities = new ArrayList<>();
        boolean attempted;
        Design(String id, BlockPos base, int[] size, String sha256, int dataVersion, BlockState wool, StructureTemplate template) {
            this.id = id; this.base = base; this.size = size; this.sha256 = sha256; this.dataVersion = dataVersion;
            this.wool = wool; this.template = template;
            box = new AABB(base.getX(), base.getY(), base.getZ(), base.getX() + size[0], base.getY() + size[1], base.getZ() + size[2]);
        }
    }
}
