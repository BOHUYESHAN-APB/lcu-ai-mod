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
import net.neoforged.fml.config.ModConfig.Type;
import net.neoforged.fml.event.lifecycle.FMLCommonSetupEvent;
import org.slf4j.Logger;

/**
 * LCUMod — LLM Control User mod for NeoForge 1.21.1.
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
        modContainer.registerConfig(Type.COMMON, ModConfig.SPEC);

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
        LOGGER.info("[LCUMod] Initializing...");
        WIRE = new WireServer(ModConfig.WIRE_PORT.getAsInt());
        WIRE.start();
        LOGGER.info("[LCUMod] Ready. Wire server listening on port {}", ModConfig.WIRE_PORT.getAsInt());
    }
}
