package com.lcu.lcumod;

import com.lcu.lcumod.action.ActionExecutor;
import com.lcu.lcumod.behavior.BehaviorManager;
import com.lcu.lcumod.config.ModConfig;
import com.lcu.lcumod.network.WireServer;
import com.lcu.lcumod.state.StateCollector;
import com.mojang.logging.LogUtils;
import net.neoforged.bus.api.IEventBus;
import net.neoforged.fml.ModContainer;
import net.neoforged.fml.common.Mod;
import net.neoforged.fml.event.lifecycle.FMLCommonSetupEvent;
import net.neoforged.fml.loading.FMLEnvironment;
import org.slf4j.Logger;

/**
 * LCUMod — LLM Control User mod for NeoForge 26.3.
 *
 * This is a CLIENT-SIDE controller mod: it drives the player of a real Minecraft client. All of its
 * real work (state push, action execution, wire server, pathfinding) lives in client-only classes,
 * and every @EventBusSubscriber bridge is declared Dist.CLIENT.
 *
 * On a dedicated server those classes do not exist, so this common entrypoint initialises nothing
 * there — otherwise mod loading aborts with NoClassDefFoundError: net/minecraft/client/...
 * (verified with `gradlew runServer`). Servers need no installation of this mod at all.
 *
 * Event handling is done via @EventBusSubscriber bridge classes:
 *   - EventTest (debug)
 *   - StateCollector (state push)
 *   - ActionExecutorBridge → ActionExecutor (command processing)
 *   - BehaviorManagerBridge → BehaviorManager (auto behaviors)
 */
@Mod(LCUMod.MODID)
public class LCUMod {
    public static final String MODID = "lcumod";
    public static final Logger LOGGER = LogUtils.getLogger();

    public static WireServer WIRE;
    public static StateCollector STATE;
    public static ActionExecutor ACTION;
    public static BehaviorManager BEHAVIORS;

    public LCUMod(IEventBus modEventBus, ModContainer modContainer) {
        modEventBus.addListener(this::commonSetup);
        modContainer.registerConfig(net.neoforged.fml.config.ModConfig.Type.LOCAL, ModConfig.SPEC);

        if (!FMLEnvironment.getDist().isClient()) {
            LOGGER.info("[LCUMod] Dedicated server — client-side mod, nothing to initialise.");
            return;
        }

        // Create singletons (these are referenced by @EventBusSubscriber bridge classes)
        STATE = new StateCollector();
        ACTION = new ActionExecutor();
        BEHAVIORS = new BehaviorManager();
        // ChatListener is auto-discovered via @EventBusSubscriber

        // WireServer needs the Minecraft instance, so it starts in commonSetup
        // StateCollector, ActionExecutorBridge, BehaviorManagerBridge, EventTest
        // are all auto-discovered via @EventBusSubscriber annotation.
    }

    private void commonSetup(FMLCommonSetupEvent event) {
        if (!FMLEnvironment.getDist().isClient()) {
            return;
        }
        LOGGER.info("[LCUMod] Initializing...");
        WIRE = new WireServer(ModConfig.WIRE_PORT.getAsInt(), ModConfig.WIRE_BIND_ADDRESS.get());
        // 先把 token 武装上再监听：**空 token = 不鉴权**（旧行为），非空则要求后端首帧 hello。
        // 读配置值有前提（配置没加载完会抛），所以这里用**防御式**读法；真加载完时
        // `ModConfigEvent.Loading/Reloading` 会再武装一次（见下两个方法）。
        armWireToken();
        WIRE.start();
        LOGGER.info("[LCUMod] Ready. Wire server listening on port {}", ModConfig.WIRE_PORT.getAsInt());
    }

    /**
     * 把配置里的 {@code wireToken} 交给 wire server（**空 = 不鉴权**，即旧行为）。
     *
     * <p>读配置值有前提：配置没加载完时 {@code ConfigValue.get()} 会抛 —— 而在启动路径上抛异常
     * 等于**游戏打不开**。所以这里挡异常、只记一行日志，等 {@link #armWireTokenIfNeeded()} 在
     * 世界跑起来之后再补。
     */
    private static void armWireToken() {
        if (WIRE == null) {
            return;
        }
        String token;
        try {
            token = ModConfig.WIRE_TOKEN.get();
        } catch (RuntimeException notReady) {
            LOGGER.info("[LCUMod] wireToken not readable yet ({}); will arm on the first tick.",
                    notReady.getClass().getSimpleName());
            return;
        }
        WIRE.setRequiredToken(token == null ? "" : token.trim());
        if (WIRE.tokenArmed()) {
            LOGGER.info("[LCUMod] Wire auth armed ({} chars) — the backend must send a matching hello "
                    + "first (LCU_WIRE_TOKEN or .local/wire_token).", token.trim().length());
        } else {
            LOGGER.warn("[LCUMod] No wireToken set: port {} accepts any local process. For auth, set "
                    + "wireToken here AND the same value for the backend.", ModConfig.WIRE_PORT.getAsInt());
        }
    }

    /**
     * 第一次客户端 tick 时补一次（此时配置**必然**已加载）—— 只做一次，之后是空操作。
     *
     * <p>为什么挂在这里：我们自己的 {@code @SubscribeEvent} 桥里只有 tick 是"早于任何后端连接、
     * 且配置一定已加载"的钩子；不去猜 FML 配置事件的类名（本仓库里没有任何先例可查）。
     */
    public static void armWireTokenIfNeeded() {
        if (wireTokenChecked) {
            return;
        }
        wireTokenChecked = true;
        armWireToken();
    }

    private static volatile boolean wireTokenChecked = false;
}
