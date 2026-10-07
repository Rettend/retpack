package me.rettend.farmbench;

import com.mojang.brigadier.arguments.StringArgumentType;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.TimeUnit;
import net.fabricmc.api.ModInitializer;
import net.fabricmc.fabric.api.command.v2.CommandRegistrationCallback;
import net.fabricmc.fabric.api.event.lifecycle.v1.ServerLifecycleEvents;
import net.fabricmc.fabric.api.event.lifecycle.v1.ServerTickEvents;
import net.fabricmc.loader.api.FabricLoader;
import net.minecraft.commands.CommandSourceStack;
import net.minecraft.commands.Commands;
import net.minecraft.network.chat.Component;
import net.minecraft.server.MinecraftServer;
import net.minecraft.server.permissions.Permissions;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

public final class FarmbenchMod implements ModInitializer {
    public static final Logger LOGGER = LoggerFactory.getLogger("farmbench");
    private final List<PlacementService> placements = new ArrayList<>();
    private BenchmarkSession active;
    private MinecraftServer runningServer;
    private ScheduledExecutorService watchdog;

    @Override public void onInitialize() {
        CommandRegistrationCallback.EVENT.register((dispatcher, registry, environment) -> dispatcher.register(
            Commands.literal("farmbench").requires(source -> source.permissions().hasPermission(Permissions.COMMANDS_GAMEMASTER))
                .then(Commands.literal("start").then(Commands.argument("job-id", StringArgumentType.word())
                    .executes(context -> start(context.getSource(), StringArgumentType.getString(context, "job-id")))))
                .then(Commands.literal("stop").executes(context -> stop(context.getSource())))
                .then(Commands.literal("status").executes(context -> status(context.getSource())))
                .then(Commands.literal("cleanup").executes(context -> cleanup(context.getSource())))));
        ServerLifecycleEvents.SERVER_STARTED.register(server -> {
            runningServer = server;
            placements.clear();
            active = null;
            watchdog = Executors.newSingleThreadScheduledExecutor(task -> {
                Thread thread = new Thread(task, "farmbench-watchdog");
                thread.setDaemon(true);
                return thread;
            });
            // Only enqueue server-thread work; the watchdog never reads or changes a world.
            watchdog.scheduleAtFixedRate(() -> server.executeIfPossible(() -> guarded(server, true, false)), 2, 2, TimeUnit.SECONDS);
        });
        ServerTickEvents.START_SERVER_TICK.register(server -> guarded(server, false, true));
        ServerTickEvents.END_SERVER_TICK.register(server -> guarded(server, false, false));
        ServerLifecycleEvents.SERVER_STOPPING.register(server -> {
            if (watchdog != null) watchdog.shutdownNow();
            if (active != null && !active.finished()) active.finish("failed", "Server stopped before the job completed");
            for (PlacementService placement : placements) {
                try { placement.release(); } catch (RuntimeException e) { LOGGER.error("Cannot release farmbench tickets on stop", e); }
            }
            runningServer = null;
        });
    }

    private void guarded(MinecraftServer server, boolean timeout, boolean beforeTick) {
        if (server != runningServer || active == null || active.finished()) return;
        try {
            if (timeout) active.checkTimeout();
            else if (beforeTick) active.beforeTick();
            else active.afterTick();
        } catch (RuntimeException e) {
            LOGGER.error("Farmbench job failed", e);
            active.finish("failed", e.getMessage() == null ? e.toString() : e.getMessage());
        }
    }

    private int start(CommandSourceStack source, String id) {
        if (active != null && !active.finished()) return fail(source, "A farmbench job is already running. Stop it first.");
        try {
            var player = source.getPlayerOrException();
            var config = FabricLoader.getInstance().getConfigDir();
            var job = new JobLoader(config).load(id);
            BenchmarkSession session = new BenchmarkSession(source.getServer(), player, job, config,
                message -> source.sendSystemMessage(Component.literal(message)));
            active = session;
            placements.add(session.placement());
            try { session.start(); }
            catch (RuntimeException e) { session.finish("failed", e.getMessage()); throw e; }
            source.sendSuccess(() -> Component.literal("Started farmbench " + id + ". Stay Creative and near every bay; use /farmbench stop to cancel."), false);
            return 1;
        } catch (Exception e) { return fail(source, e.getMessage() == null ? e.toString() : e.getMessage()); }
    }

    private int stop(CommandSourceStack source) {
        if (active == null || active.finished()) return fail(source, "No farmbench job is running.");
        active.finish("cancelled", "Cancelled by " + source.getTextName());
        return 1;
    }

    private int status(CommandSourceStack source) {
        source.sendSuccess(() -> Component.literal(active == null ? "No farmbench job has run in this server session." : active.status()), false);
        return 1;
    }

    private int cleanup(CommandSourceStack source) {
        if (active != null && !active.finished()) return fail(source, "Stop the current job before cleanup.");
        int cleaned = 0;
        var iterator = placements.iterator();
        while (iterator.hasNext()) {
            PlacementService placement = iterator.next();
            try {
                placement.cleanup();
                placement.release();
                iterator.remove();
                cleaned++;
            } catch (RuntimeException e) { return fail(source, "Cleanup stopped: " + e.getMessage()); }
        }
        int count = cleaned;
        source.sendSuccess(() -> Component.literal("Cleaned " + count + " tracked job placements from this server session. Existing-mode builds are not deleted; unrelated entities are untouched."), false);
        return 1;
    }

    private static int fail(CommandSourceStack source, String message) {
        source.sendFailure(Component.literal(message));
        return 0;
    }
}
