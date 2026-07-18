package com.lcu.lcumod;

import com.lcu.lcumod.config.ModConfig;
import com.lcu.lcumod.config.ServerConfig;
import com.lcu.lcumod.network.WireServer;
import com.mojang.logging.LogUtils;
import net.neoforged.bus.api.IEventBus;
import net.neoforged.fml.ModContainer;
import net.neoforged.fml.common.Mod;
import net.neoforged.fml.config.ModConfig.Type;
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
    public LCUMod(IEventBus modEventBus, ModContainer modContainer) {
        modContainer.registerConfig(Type.COMMON, ModConfig.SPEC);
        modContainer.registerConfig(Type.SERVER, ServerConfig.SPEC);
        LOGGER.info("[LCUMod] Common runtime loaded without client body activation");
    }
}
