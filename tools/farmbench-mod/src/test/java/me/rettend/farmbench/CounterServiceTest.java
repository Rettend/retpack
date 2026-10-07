package me.rettend.farmbench;

import static org.junit.jupiter.api.Assertions.*;
import java.util.List;
import org.junit.jupiter.api.Test;

class CounterServiceTest {
    @Test void emptyHoppersAndCartsPassWithActualSlotCoverage() {
        var check = new CounterService.DrainCheck("pre_drain", 200, List.of("a", "b"));
        check.inventory("a", "hopper_block", 1, 2, 3, null, 5, slot -> 0);
        check.inventory("a", "hopper_minecart", 1.5, 3.25, 3.5, "cart-a", 5, slot -> 0);
        check.inventory("b", "hopper_block", 40, 2, 3, null, 5, slot -> 0);
        var report = check.finish();
        assertDoesNotThrow(() -> CounterService.requireDrained(report));
        assertEquals("passed", report.get("status").getAsString());
        assertEquals(0, report.get("pending_items").getAsLong());
        var a = report.getAsJsonArray("designs").get(0).getAsJsonObject();
        assertEquals(1, a.get("hopper_blocks_checked").getAsInt());
        assertEquals(1, a.get("hopper_minecarts_checked").getAsInt());
        assertEquals(10, a.get("slots_checked").getAsInt());
        assertTrue(report.get("limitations").getAsString().contains("Other containers"));
        assertTrue(report.get("limitations").getAsString().contains("stranded dropped items"));
    }

    @Test void pendingBlockHopperItemsFailWithPositionAndLongerDrainAdvice() {
        var check = new CounterService.DrainCheck("pre_drain", 200, List.of("a"));
        check.inventory("a", "hopper_block", -10, 64, 20, null, 5, slot -> slot == 4 ? 8 : 0);
        var report = check.finish();
        String failure = assertThrows(IllegalStateException.class, () -> CounterService.requireDrained(report)).getMessage();
        assertTrue(failure.contains("8 pending items"));
        assertTrue(failure.contains("[-10.0, 64.0, 20.0]"));
        assertTrue(failure.contains("increase drain_ticks"));
        var issue = report.getAsJsonArray("issues").get(0).getAsJsonObject();
        assertEquals("hopper_block", issue.get("kind").getAsString());
        assertEquals(4, issue.getAsJsonArray("occupied_slots").get(0).getAsJsonObject().get("slot").getAsInt());
    }

    @Test void pendingMinecartItemsFailAndKeepFractionalPositionAndIdentity() {
        var check = new CounterService.DrainCheck("post_drain", 900, List.of("a"));
        check.inventory("a", "hopper_minecart", 0.5, 65.125, -3.75, "cart-uuid", 5, slot -> slot == 0 ? 2 : slot == 3 ? 6 : 0);
        var report = check.finish();
        assertThrows(IllegalStateException.class, () -> CounterService.requireDrained(report));
        assertEquals(8, report.get("pending_items").getAsLong());
        var issue = report.getAsJsonArray("issues").get(0).getAsJsonObject();
        assertEquals("cart-uuid", issue.get("uuid").getAsString());
        assertEquals(65.125, issue.getAsJsonArray("position").get(1).getAsDouble());
    }

    @Test void escapedOrUnobservableKnownCartsCannotPassEvenWithNoPendingCount() {
        var escaped = new CounterService.DrainCheck("post_drain", 900, List.of("a"));
        escaped.escapedCart("a", "cart-uuid", 500, 65, 3, "minecraft:overworld");
        var report = escaped.finish();
        assertEquals(0, report.get("pending_items").getAsLong());
        assertThrows(IllegalStateException.class, () -> CounterService.requireDrained(report));
        assertEquals("outside_original_bay", report.getAsJsonArray("issues").get(0).getAsJsonObject().get("reason").getAsString());
        var missing = new CounterService.DrainCheck("pre_drain", 200, List.of("a"));
        missing.unchecked("Known hopper minecart is missing or unloaded");
        assertThrows(IllegalStateException.class, () -> CounterService.requireDrained(missing.finish()));
    }

    @Test void countsAllSlotsAndDesignsWithoutIntegerOverflowAndBoundsIssueExamples() {
        var check = new CounterService.DrainCheck("post_drain", 900, List.of("a", "b"));
        for (int i = 0; i < 101; i++) check.inventory(i % 2 == 0 ? "a" : "b", "hopper_block", i, 64, 0, null,
            5, slot -> Integer.MAX_VALUE);
        var report = check.finish();
        assertEquals(101L * 5 * Integer.MAX_VALUE, report.get("pending_items").getAsLong());
        assertEquals(101, report.get("issue_count").getAsInt());
        assertEquals(100, report.getAsJsonArray("issues").size());
        assertTrue(report.get("issue_examples_truncated").getAsBoolean());
    }

    @Test void broaderContainersAreOutsideTheEmptyInventoryRequirementAndIncompleteChecksFailClosed() {
        var check = new CounterService.DrainCheck("pre_drain", 200, List.of("a"));
        assertThrows(IllegalArgumentException.class, () -> check.inventory("a", "barrel", 0, 0, 0, null, 27, slot -> 64));
        assertThrows(IllegalStateException.class, () -> check.inventory("a", "hopper_block", 0, 0, 0, null, 5, slot -> -1));
        var incomplete = new com.google.gson.JsonObject(); incomplete.addProperty("status", "incomplete");
        assertThrows(IllegalStateException.class, () -> CounterService.requireDrained(incomplete));
    }
}
