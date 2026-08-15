package com.lcu.lcumod.client;

import com.google.gson.JsonObject;
import com.lcu.lcumod.LCUMod;
import com.lcu.lcumod.action.ActionExecutor;
import com.lcu.lcumod.action.InputIsolation;
import com.lcu.lcumod.action.PoiMemory;
import com.lcu.lcumod.behavior.BehaviorManager;
import com.lcu.lcumod.config.ModConfig;
import com.lcu.lcumod.config.RuntimeRole;
import com.lcu.lcumod.config.ServerPolicy;
import com.lcu.lcumod.network.WireServer;
import com.lcu.lcumod.state.StateCollector;
import net.minecraft.client.Minecraft;
import net.minecraft.client.multiplayer.ClientLevel;

/** Owns client-world readiness; mechanics remain unavailable until warm-up completes. */
public final class ClientBodyRuntime {
    public static ActionExecutor ACTION;
    public static BehaviorManager BEHAVIORS;

    private static final WorldWarmupGate WARMUP = new WorldWarmupGate();
    private static boolean started;
    private static ClientLevel activeLevel;
    private static long worldEpoch;
    private static long controlEpoch;
    private static long warmupStartedNanos;
    private static long wireStartNanos;

    private ClientBodyRuntime() {}

    /** Bootstrap only. Socket, executor, scanning, and body admission are deferred per world. */
    public static synchronized void start() {
        if (started || !isBodyClient()) return;
        started = true;
        InputIsolation.enterManualControl();
        LCUMod.LOGGER.info("[LCUMod] Body bootstrap ready; world services wait for warm-up");
    }

    public static synchronized void beginWorldWarmup() {
        if (!isBodyClient()) return;
        if (!started) start();

        Minecraft mc = Minecraft.getInstance();
        ClientLevel level = mc.level;
        if (level == null) return;
        if (activeLevel == level && WARMUP.phase() != BodyRuntimePhase.NO_WORLD) return;

        deactivateCurrentWorld();
        activeLevel = level;
        worldEpoch++;
        controlEpoch++;
        WARMUP.begin(ModConfig.WORLD_WARMUP_TICKS.getAsInt());
        warmupStartedNanos = System.nanoTime();
        wireStartNanos = 0L;
        InputIsolation.enterManualControl();
        StateCollector.resetForWorld();
        PoiMemory.resetForWorld();
        publishControlState();
        LCUMod.LOGGER.info("[LCUMod] World epoch {} warming for {} ticks", worldEpoch, WARMUP.remainingTicks());
    }

    /** Called once per client post tick before any actuator work. */
    public static synchronized void onClientTick() {
        if (!started) return;

        Minecraft mc = Minecraft.getInstance();
        if (mc.level == null) {
            if (activeLevel != null || WARMUP.phase() != BodyRuntimePhase.NO_WORLD) invalidateWorld();
            return;
        }

        // LevelEvent.Load can fire before Minecraft.level is assigned. Polling here
        // makes the lifecycle robust to that ordering and to dimension transitions.
        if (mc.level != activeLevel || WARMUP.phase() == BodyRuntimePhase.NO_WORLD) beginWorldWarmup();
        if (mc.level != activeLevel) return;
        if (mc.player == null) {
            if (WARMUP.phase() == BodyRuntimePhase.ARMED) disarmWorld();
            return;
        }

        if (WARMUP.phase() == BodyRuntimePhase.WARMING_UP && !WARMUP.tick()) return;
        if (WARMUP.phase() == BodyRuntimePhase.STARTING) finishDeferredStartup();
    }

    public static synchronized boolean toggleControl() {
        return switch (WARMUP.phase()) {
            case READY_MANUAL -> armWorld();
            case ARMED -> disarmWorld();
            default -> false;
        };
    }

    public static synchronized void onPlayerClone() {
        if (WARMUP.phase() == BodyRuntimePhase.NO_WORLD || WARMUP.phase() == BodyRuntimePhase.WARMING_UP
                || WARMUP.phase() == BodyRuntimePhase.STARTING) return;
        worldEpoch++;
        controlEpoch++;
        if (LCUMod.WIRE != null) LCUMod.WIRE.setCommandAdmissionEnabled(false);
        WARMUP.disarm();
        if (ACTION != null) ACTION.invalidateRuntime();
        InputIsolation.enterManualControl();
        StateCollector.resetForWorld();
        PoiMemory.resetForWorld();
        if (LCUMod.WIRE != null) LCUMod.WIRE.setSidebandAdmissionEnabled(true);
        publishControlState();
        LCUMod.LOGGER.info("[LCUMod] Player rebound for world epoch {}; manual control retained", worldEpoch);
    }

    public static synchronized void invalidateWorld() {
        boolean hadActiveWorld = activeLevel != null || WARMUP.phase() != BodyRuntimePhase.NO_WORLD;
        deactivateCurrentWorld();
        activeLevel = null;
        WARMUP.reset();
        if (hadActiveWorld) controlEpoch++;
        StateCollector.resetForWorld();
        PoiMemory.resetForWorld();
        publishControlState();
    }

    /** Ignore unload notifications for an older level after a dimension swap. */
    public static synchronized void invalidateWorld(ClientLevel level) {
        if (level != null && activeLevel != null && level != activeLevel) return;
        invalidateWorld();
    }

    public static synchronized void stop() {
        deactivateCurrentWorld();
        if (LCUMod.WIRE != null) LCUMod.WIRE.stop();
        LCUMod.WIRE = null;
        ACTION = null;
        BEHAVIORS = null;
        activeLevel = null;
        WARMUP.reset();
        started = false;
    }

    public static boolean isBodyClient() {
        try {
            return RuntimeRole.current().activatesClientBody();
        } catch (IllegalStateException ignored) {
            return false;
        }
    }

    public static boolean isActuatorReady() {
        return WARMUP.phase() == BodyRuntimePhase.ARMED && ACTION != null && hasCurrentPlayer();
    }

    public static boolean isObservationReady() {
        return (WARMUP.phase() == BodyRuntimePhase.READY_MANUAL || WARMUP.phase() == BodyRuntimePhase.ARMED)
                && hasCurrentPlayer();
    }

    public static boolean isSidebandReady() {
        return WARMUP.phase() == BodyRuntimePhase.READY_MANUAL && ACTION != null && hasCurrentPlayer();
    }

    public static boolean isArmed() {
        return WARMUP.phase() == BodyRuntimePhase.ARMED;
    }

    public static BodyRuntimePhase phase() {
        return WARMUP.phase();
    }

    public static int warmupRemainingTicks() {
        return WARMUP.remainingTicks();
    }

    public static long worldEpoch() {
        return worldEpoch;
    }

    public static long controlEpoch() {
        return controlEpoch;
    }

    /** Missing epochs remain compatible; malformed or mismatched supplied pairs fail closed. */
    public static String commandEpochError(JsonObject args) {
        if (args == null) return null;
        boolean hasWorldEpoch = args.has(WireServer.WORLD_EPOCH_FIELD);
        boolean hasControlEpoch = args.has(WireServer.CONTROL_EPOCH_FIELD);
        if (!hasWorldEpoch && !hasControlEpoch) return null;
        if (!hasWorldEpoch || !hasControlEpoch) return "INVALID_BODY_EPOCH: world and control epochs must be supplied together";
        try {
            Long expectedWorldEpoch = parseEpochArgument(args.get(WireServer.WORLD_EPOCH_FIELD));
            Long expectedControlEpoch = parseEpochArgument(args.get(WireServer.CONTROL_EPOCH_FIELD));
            if (expectedWorldEpoch == null || expectedControlEpoch == null) {
                return "INVALID_BODY_EPOCH: epoch values must be non-negative integers";
            }
            if (expectedWorldEpoch != worldEpoch || expectedControlEpoch != controlEpoch) {
                return "STALE_BODY_EPOCH: body lifecycle changed before command execution";
            }
            return null;
        } catch (RuntimeException ignored) {
            return "INVALID_BODY_EPOCH: epoch values must be integers";
        }
    }

    private static Long parseEpochArgument(com.google.gson.JsonElement value) {
        if (value == null || value.isJsonNull()) return null;
        String text = value.getAsString();
        if (text.isBlank() || !text.chars().allMatch(Character::isDigit)) return null;
        try {
            return Long.parseLong(text);
        } catch (NumberFormatException ignored) {
            return null;
        }
    }

    /** Publishes ownership together with the lifecycle epoch so stale backend state cannot imply control. */
    public static synchronized void publishControlState() {
        if (LCUMod.WIRE == null) return;
        JsonObject data = new JsonObject();
        data.addProperty("ai_controlled", WARMUP.phase() == BodyRuntimePhase.ARMED && InputIsolation.isAiControlled());
        data.addProperty("body_runtime_phase", WARMUP.phase().name().toLowerCase(java.util.Locale.ROOT));
        data.addProperty("body_world_epoch", worldEpoch);
        data.addProperty("body_control_epoch", controlEpoch);
        data.addProperty("actuator_ready", isActuatorReady());
        LCUMod.WIRE.sendEvent("control_state", data);
    }

    private static void finishDeferredStartup() {
        if (LCUMod.WIRE == null) {
            LCUMod.WIRE = new WireServer(
                ModConfig.WIRE_PORT.getAsInt(), ModConfig.WIRE_TOKEN.get(), "body_client",
                ActionExecutor::requestBackendDisconnectStop, ServerPolicy::snapshot
            );
            LCUMod.WIRE.setCommandAdmissionEnabled(false);
            LCUMod.WIRE.setSidebandAdmissionEnabled(false);
            wireStartNanos = System.nanoTime();
            LCUMod.WIRE.startAsync();
            return;
        }

        if (!LCUMod.WIRE.isRunning()) {
            if (LCUMod.WIRE.getStartupError() != null) {
                Throwable startupError = LCUMod.WIRE.getStartupError();
                WARMUP.fail();
                LCUMod.WIRE.stop();
                LCUMod.WIRE = null;
                LCUMod.LOGGER.error("[LCUMod] Wire startup failed after world warm-up", startupError);
            }
            return;
        }

        if (ACTION == null) ACTION = new ActionExecutor();
        if (BEHAVIORS == null) BEHAVIORS = new BehaviorManager();
        ACTION.prepareForWorldManual();
        LCUMod.WIRE.setCommandAdmissionEnabled(false);
        WARMUP.readyManual();
        LCUMod.WIRE.setSidebandAdmissionEnabled(true);
        StateCollector.resetForWorld();
        publishControlState();
        long elapsedMillis = (System.nanoTime() - warmupStartedNanos) / 1_000_000L;
        long wireMillis = wireStartNanos == 0L ? 0L : (System.nanoTime() - wireStartNanos) / 1_000_000L;
        LCUMod.LOGGER.info("[LCUMod] World epoch {} ready for manual control after {} ms; wire bind={} ms",
            worldEpoch, elapsedMillis, wireMillis);
    }

    private static boolean armWorld() {
        if (ACTION == null || LCUMod.WIRE == null || !LCUMod.WIRE.isRunning()) return false;
        if (!WARMUP.arm()) return false;
        controlEpoch++;
        LCUMod.WIRE.setCommandAdmissionEnabled(true);
        try {
            ACTION.armForWorld();
            return true;
        } catch (RuntimeException exception) {
            LCUMod.WIRE.setCommandAdmissionEnabled(false);
            WARMUP.disarm();
            controlEpoch++;
            ACTION.disarmForWorld();
            LCUMod.LOGGER.error("[LCUMod] Failed to arm body runtime", exception);
            return false;
        }
    }

    private static boolean disarmWorld() {
        if (!WARMUP.disarm()) return false;
        controlEpoch++;
        if (LCUMod.WIRE != null) LCUMod.WIRE.setCommandAdmissionEnabled(false);
        if (LCUMod.WIRE != null) LCUMod.WIRE.setSidebandAdmissionEnabled(true);
        if (ACTION != null) ACTION.disarmForWorld();
        InputIsolation.enterManualControl();
        return true;
    }

    /** Backend disarm is idempotent but never grants ownership without the F12 transition. */
    public static synchronized boolean disarmFromBackend() {
        if (WARMUP.phase() == BodyRuntimePhase.ARMED) return disarmWorld();
        if (WARMUP.phase() != BodyRuntimePhase.READY_MANUAL) return false;
        if (LCUMod.WIRE != null) {
            LCUMod.WIRE.setCommandAdmissionEnabled(false);
            LCUMod.WIRE.setSidebandAdmissionEnabled(true);
        }
        InputIsolation.enterManualControl();
        publishControlState();
        return true;
    }

    private static void deactivateCurrentWorld() {
        if (LCUMod.WIRE != null) {
            LCUMod.WIRE.setCommandAdmissionEnabled(false);
            LCUMod.WIRE.setSidebandAdmissionEnabled(false);
        }
        if (ACTION != null) ACTION.invalidateRuntime();
        InputIsolation.enterManualControl();
    }

    private static boolean hasCurrentPlayer() {
        Minecraft mc = Minecraft.getInstance();
        return activeLevel != null && mc.level == activeLevel && mc.player != null;
    }
}
