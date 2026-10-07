package me.rettend.farmbench.mixin;

import carpet.helpers.HopperCounter;
import it.unimi.dsi.fastutil.objects.Object2LongMap;
import net.minecraft.world.item.Item;
import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.gen.Accessor;

/** Reads raw counts, not localized command output or the command's return value. */
@Mixin(value = HopperCounter.class, remap = false)
public interface HopperCounterAccessor {
    @Accessor("counter") Object2LongMap<Item> farmbench$items();
    @Accessor("startTick") long farmbench$startTick();
    @Accessor("startMillis") long farmbench$startMillis();
}
