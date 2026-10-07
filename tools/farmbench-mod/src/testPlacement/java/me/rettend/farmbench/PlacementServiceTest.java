package me.rettend.farmbench;

import static org.junit.jupiter.api.Assertions.*;

import java.nio.file.Files;
import java.nio.file.Path;
import java.util.Map;
import java.util.UUID;
import net.minecraft.SharedConstants;
import net.minecraft.core.BlockPos;
import net.minecraft.core.UUIDUtil;
import net.minecraft.core.registries.BuiltInRegistries;
import net.minecraft.nbt.ByteTag;
import net.minecraft.nbt.CompoundTag;
import net.minecraft.nbt.DoubleTag;
import net.minecraft.nbt.IntTag;
import net.minecraft.nbt.ListTag;
import net.minecraft.nbt.NbtAccounter;
import net.minecraft.nbt.NbtIo;
import net.minecraft.nbt.StringTag;
import net.minecraft.server.Bootstrap;
import net.minecraft.world.level.levelgen.structure.templatesystem.StructureTemplate;
import org.junit.jupiter.api.Assumptions;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

class PlacementServiceTest {
    @TempDir Path temporary;

    @Test void translatesEveryPassengerInExportCoordinatesNotRelativeToParent() {
        CompoundTag root = entity(1.25, 2.5, 3.75);
        CompoundTag passenger = entity(4.125, 5.25, 6.5);
        CompoundTag nested = entity(0.5, 1.5, 2.5);
        passenger.put("Passengers", list(nested));
        root.put("Passengers", list(passenger));
        PlacementService.translateEntityNbt(root, new BlockPos(100, -20, -100), Map.of());
        assertPosition(root, 101.25, -17.5, -96.25);
        assertPosition(passenger, 104.125, -14.75, -93.5);
        assertPosition(nested, 100.5, -18.5, -97.5);
    }

    @Test void passengerWithoutExplicitPosUsesParentExportCoordinateOnce() {
        CompoundTag root = entity(1.25, 2.5, 3.75);
        CompoundTag passenger = new CompoundTag();
        root.put("Passengers", list(passenger));
        PlacementService.translateEntityNbt(root, new BlockPos(10, 20, 30), Map.of());
        assertPosition(root, 11.25, 22.5, 33.75);
        assertPosition(passenger, 11.25, 22.5, 33.75);
    }

    @Test void preservesTypedPayloadAndMotionWithoutTreatingThemAsPositions() {
        CompoundTag root = entity(0.5, 1.5, 2.5);
        root.putByte("NoGravity", (byte) 1);
        root.putShort("Air", (short) 300);
        root.putFloat("Health", 20.5f);
        root.putLong("custom_counter", 1234567890123L);
        root.putIntArray("custom_array", new int[]{1, 2, 3});
        root.put("Motion", doubles(0.125, 0.0, -0.25));
        CompoundTag item = new CompoundTag();
        item.putString("id", "minecraft:bamboo"); item.putByte("Slot", (byte) 4); item.putInt("count", 8);
        root.put("Items", list(item));
        CompoundTag before = root.copy();
        PlacementService.translateEntityNbt(root, new BlockPos(5, 10, 15), Map.of());
        root.remove("Pos"); before.remove("Pos");
        assertEquals(before, root);
        assertInstanceOf(ByteTag.class, root.get("NoGravity"));
    }

    @Test void translatesModernAndLegacyHangingAnchors() {
        CompoundTag root = entity(1.5, 2.5, 3.5);
        root.putIntArray("block_pos", new int[]{1, 2, 3});
        root.putInt("TileX", 1); root.putInt("TileY", 2); root.putInt("TileZ", 3);
        PlacementService.translateEntityNbt(root, new BlockPos(-50, 80, 100), Map.of());
        assertArrayEquals(new int[]{-49, 82, 103}, root.getIntArray("block_pos").orElseThrow());
        assertEquals(-49, root.getIntOr("TileX", 0));
        assertEquals(82, root.getIntOr("TileY", 0));
        assertEquals(103, root.getIntOr("TileZ", 0));
    }

    @Test void acceptsSupportedHangingCoordinateEncodingsAndProducesVanillaIntArray() {
        CompoundTag compound = new CompoundTag();
        compound.putInt("x", 1); compound.putInt("y", 2); compound.putInt("z", 3);
        for (var anchor : new net.minecraft.nbt.Tag[]{compound, list(IntTag.valueOf(1), IntTag.valueOf(2), IntTag.valueOf(3))}) {
            CompoundTag root = entity(1.5, 2.5, 3.5); root.put("block_pos", anchor);
            PlacementService.translateEntityNbt(root, new BlockPos(10, 20, 30), Map.of());
            assertArrayEquals(new int[]{11, 22, 33}, root.getIntArray("block_pos").orElseThrow());
        }
    }

    @Test void rewritesOnlyKnownInternalUuidReferencesAndPreservesPassengerIdentity() {
        UUID old = UUID.fromString("00000000-0000-0000-0000-000000000001");
        UUID fresh = UUID.fromString("00000000-0000-0000-0000-000000000002");
        UUID external = UUID.fromString("00000000-0000-0000-0000-000000000003");
        CompoundTag root = entity(0.5, 0.5, 0.5);
        root.store("UUID", UUIDUtil.CODEC, fresh);
        root.store("Owner", UUIDUtil.CODEC, old);
        root.putString("external_reference", external.toString());
        root.put("uuid_list", list(StringTag.valueOf(old.toString()), StringTag.valueOf(external.toString())));
        CompoundTag passenger = entity(1.5, 1.5, 1.5);
        passenger.putString("Owner", old.toString()); root.put("Passengers", list(passenger));
        PlacementService.translateEntityNbt(root, new BlockPos(10, 20, 30), Map.of(old, fresh));
        assertEquals(fresh, root.read("UUID", UUIDUtil.CODEC).orElseThrow());
        assertEquals(fresh, root.read("Owner", UUIDUtil.CODEC).orElseThrow());
        assertEquals(fresh.toString(), passenger.getStringOr("Owner", ""));
        assertEquals(external.toString(), root.getStringOr("external_reference", ""));
        assertEquals(fresh.toString(), root.getListOrEmpty("uuid_list").getStringOr(0, ""));
        assertEquals(external.toString(), root.getListOrEmpty("uuid_list").getStringOr(1, ""));
    }

    @Test void missingExporterUuidDoesNotRequireAPersistentIdentity() {
        CompoundTag root = entity(0.5, 0.5, 0.5);
        PlacementService.translateEntityNbt(root, new BlockPos(10, 20, 30), Map.of());
        assertFalse(root.contains("UUID"));
        assertPosition(root, 10.5, 20.5, 30.5);
    }

    @Test void rejectsNonfinitePositionsIncompleteAnchorsAndDeepPassengerTrees() {
        assertThrows(IllegalArgumentException.class, () -> PlacementService.translateEntityNbt(entity(Double.NaN, 0, 0), BlockPos.ZERO, Map.of()));
        assertThrows(IllegalArgumentException.class, () -> PlacementService.translateEntityNbt(entity(0, Double.POSITIVE_INFINITY, 0), BlockPos.ZERO, Map.of()));
        CompoundTag incomplete = entity(0.5, 0.5, 0.5); incomplete.putInt("TileX", 0);
        assertThrows(IllegalArgumentException.class, () -> PlacementService.translateEntityNbt(incomplete, BlockPos.ZERO, Map.of()));
        CompoundTag badAnchor = entity(0.5, 0.5, 0.5); badAnchor.putIntArray("block_pos", new int[]{1, 2});
        assertThrows(IllegalArgumentException.class, () -> PlacementService.translateEntityNbt(badAnchor, BlockPos.ZERO, Map.of()));
        CompoundTag root = entity(0.5, 0.5, 0.5), current = root;
        for (int i = 0; i < 65; i++) {
            CompoundTag child = entity(0.5, 0.5, 0.5); current.put("Passengers", list(child)); current = child;
        }
        assertThrows(IllegalArgumentException.class, () -> PlacementService.translateEntityNbt(root, BlockPos.ZERO, Map.of()));
    }

    @Test void rejectsCoordinateOverflowAndWorldBounds() {
        assertThrows(ArithmeticException.class, () -> PlacementService.checkedOffset(new BlockPos(0, Integer.MAX_VALUE, 0), 0, 1, 0));
        assertThrows(IllegalArgumentException.class, () -> PlacementService.checkedOffset(new BlockPos(29_999_999, 0, 0), 1, 0, 0));
        assertThrows(IllegalArgumentException.class, () -> PlacementService.checkedOffset(new BlockPos(0, 0, -29_999_999), 0, 0, -1));
        assertEquals(new BlockPos(-10, 64, 10), PlacementService.checkedOffset(new BlockPos(0, 60, 0), -10, 4, 10));
    }

    @Test void confinesArtifactsToTheirExactJobDirectory() throws Exception {
        Path config = temporary.resolve("config");
        Path folder = Files.createDirectories(config.resolve("farmbench/jobs/test-job"));
        Path file = Files.write(folder.resolve("a.nbt"), new byte[]{1, 2, 3});
        assertEquals(folder.toRealPath(), PlacementService.checkedJobDirectory(config, "test-job"));
        assertEquals(file.toRealPath(), PlacementService.checkedStructurePath(folder, "a.nbt"));
        for (String name : new String[]{"../a.nbt", "..\\a.nbt", "C:\\a.nbt", "sub/a.nbt", "a.txt"})
            assertThrows(IllegalArgumentException.class, () -> PlacementService.checkedStructurePath(folder, name));
        for (String id : new String[]{"../test-job", "..", "test/job", "", "Test"})
            assertThrows(IllegalArgumentException.class, () -> PlacementService.checkedJobDirectory(config, id));
    }

    @Test void permitsReportedTransientStatesButNeverWrongHopperDirectionsOrBlockTypes() {
        SharedConstants.tryDetectVersion(); Bootstrap.bootStrap();
        var expected = net.minecraft.world.level.block.Blocks.HOPPER.defaultBlockState()
            .setValue(net.minecraft.world.level.block.HopperBlock.FACING, net.minecraft.core.Direction.NORTH);
        var disabled = expected.setValue(net.minecraft.world.level.block.HopperBlock.ENABLED, false);
        assertNotEquals(expected, disabled);
        assertTrue(PlacementService.transientStateDifference(expected, disabled));
        assertFalse(PlacementService.transientStateDifference(expected,
            expected.setValue(net.minecraft.world.level.block.HopperBlock.FACING, net.minecraft.core.Direction.EAST)));
        assertFalse(PlacementService.transientStateDifference(net.minecraft.world.level.block.Blocks.STONE.defaultBlockState(),
            net.minecraft.world.level.block.Blocks.COBBLESTONE.defaultBlockState()));
        assertTrue(PlacementService.transientStateDifference(net.minecraft.world.level.block.Blocks.AIR.defaultBlockState(),
            net.minecraft.world.level.block.Blocks.CAVE_AIR.defaultBlockState()));
    }

    @Test void readsRealPythonExportWithoutOpeningAWorld() throws Exception {
        Path folder = fixtureDirectory();
        Assumptions.assumeTrue(Files.isRegularFile(folder.resolve("a.nbt")), "Optional Python integration artifact has not been generated");
        var job = com.google.gson.JsonParser.parseString(Files.readString(folder.resolve("job.json"))).getAsJsonObject();
        JobLoader.validate(job, "entity-export-integration");
        assertEquals(job.getAsJsonArray("designs").get(0).getAsJsonObject().get("artifact_sha256").getAsString(), JobLoader.sha256(folder.resolve("a.nbt")));
        CompoundTag exported = NbtIo.readCompressed(folder.resolve("a.nbt"), NbtAccounter.create(10_000_000));
        assertEquals(4903, exported.getIntOr("DataVersion", 0));
        CompoundTag root = exported.getListOrEmpty("entities").getCompoundOrEmpty(0).getCompoundOrEmpty("nbt").copy();
        assertEquals("minecraft:oak_boat", root.getStringOr("id", ""));
        CompoundTag passenger = root.getListOrEmpty("Passengers").getCompoundOrEmpty(0);
        assertEquals("minecraft:pig", passenger.getStringOr("id", ""));
        PlacementService.translateEntityNbt(root, new BlockPos(-100, 64, 200), Map.of());
        assertPosition(root, -98.5, 65.0, 202.5);
        assertPosition(passenger, -98.5, 65.5, 202.5);
        assertFalse(root.contains("UUID")); assertFalse(passenger.contains("UUID"));
        // Initialize only vanilla registries. No server, level, game client, or save is created.
        SharedConstants.tryDetectVersion(); Bootstrap.bootStrap();
        StructureTemplate template = new StructureTemplate();
        template.load(BuiltInRegistries.BLOCK, exported);
        CompoundTag roundtrip = template.save(new CompoundTag());
        assertEquals(exported.get("size"), roundtrip.get("size"));
        assertEquals(1, roundtrip.getListOrEmpty("entities").size());
        CompoundTag savedRoot = roundtrip.getListOrEmpty("entities").getCompoundOrEmpty(0).getCompoundOrEmpty("nbt");
        assertEquals(exported.getListOrEmpty("entities").getCompoundOrEmpty(0).getCompoundOrEmpty("nbt"), savedRoot);
        var barrels = roundtrip.getListOrEmpty("blocks").compoundStream()
            .filter(block -> block.getCompoundOrEmpty("nbt").getStringOr("id", "").equals("minecraft:barrel")).toList();
        assertEquals(1, barrels.size());
        assertEquals(3, barrels.getFirst().getCompoundOrEmpty("nbt").getIntOr("z", -1));
        assertInstanceOf(IntTag.class, barrels.getFirst().getCompoundOrEmpty("nbt").get("z"));
    }

    private static CompoundTag entity(double x, double y, double z) {
        CompoundTag value = new CompoundTag(); value.putString("id", "minecraft:armor_stand");
        value.put("Pos", doubles(x, y, z)); return value;
    }

    private static Path fixtureDirectory() {
        String relative = "dist/farmbench/companion-integration/config/farmbench/jobs/entity-export-integration";
        for (Path parent = Path.of("").toAbsolutePath(); parent != null; parent = parent.getParent()) {
            Path candidate = parent.resolve(relative);
            if (Files.isRegularFile(candidate.resolve("a.nbt"))) return candidate;
        }
        return Path.of(relative);
    }

    private static ListTag doubles(double x, double y, double z) {
        return list(DoubleTag.valueOf(x), DoubleTag.valueOf(y), DoubleTag.valueOf(z));
    }

    private static ListTag list(net.minecraft.nbt.Tag... values) {
        ListTag result = new ListTag(); for (var value : values) result.add(value); return result;
    }

    private static void assertPosition(CompoundTag entity, double x, double y, double z) {
        ListTag position = entity.getListOrEmpty("Pos");
        assertEquals(3, position.size());
        assertEquals(x, position.getDoubleOr(0, Double.NaN));
        assertEquals(y, position.getDoubleOr(1, Double.NaN));
        assertEquals(z, position.getDoubleOr(2, Double.NaN));
    }
}
