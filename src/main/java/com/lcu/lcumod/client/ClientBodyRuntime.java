package com.lcu.lcumod.client;

import com.lcu.lcumod.LCUMod;
import com.lcu.lcumod.action.ActionExecutor;
import com.lcu.lcumod.behavior.BehaviorManager;
import com.lcu.lcumod.config.ModConfig;
import com.lcu.lcumod.config.RuntimeRole;
import com.lcu.lcumod.network.WireServer;

public final class ClientBodyRuntime {
    public static ActionExecutor ACTION;
    public static BehaviorManager BEHAVIORS;
    private static boolean started;

    private ClientBodyRuntime() {}

    public static synchronized void start() {
        if (started || !isBodyClient()) return;
        ACTION = new ActionExecutor();
        BEHAVIORS = new BehaviorManager();
        LCUMod.WIRE = new WireServer(ModConfig.WIRE_PORT.getAsInt(), ModConfig.WIRE_TOKEN.get());
        LCUMod.WIRE.start();
        started = true;
        LCUMod.LOGGER.info("[LCUMod] Headed body client ready on wire port {}", LCUMod.WIRE.getBoundPort());
    }

    public static synchronized void stop() {
        if (LCUMod.WIRE != null) LCUMod.WIRE.stop();
        LCUMod.WIRE = null;
        ACTION = null;
        BEHAVIORS = null;
        started = false;
    }

    public static boolean isBodyClient() {
        try {
            return RuntimeRole.current() == RuntimeRole.BODY_CLIENT;
        } catch (IllegalStateException ignored) {
            return false;
        }
    }
}
