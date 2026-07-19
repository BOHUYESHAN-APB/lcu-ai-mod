package com.lcu.lcumod;

import com.lcu.lcumod.client.ClientBodyRuntime;
import com.lcu.lcumod.config.RuntimeRole;
import com.lcu.lcumod.config.ServerPolicy;
import net.minecraft.client.Minecraft;
import net.neoforged.api.distmarker.Dist;
import net.neoforged.bus.api.SubscribeEvent;
import net.neoforged.fml.ModContainer;
import net.neoforged.fml.common.EventBusSubscriber;
import net.neoforged.fml.common.Mod;
import net.neoforged.fml.event.lifecycle.FMLClientSetupEvent;

// Client-only mod class.
@Mod(value = LCUMod.MODID, dist = Dist.CLIENT)
@EventBusSubscriber(modid = LCUMod.MODID, bus = EventBusSubscriber.Bus.MOD, value = Dist.CLIENT)
public class LCUModClient {
    public LCUModClient(ModContainer container) {
        // Client-side setup placeholder
    }

    /**
     * Disable pause-on-lost-focus + raw mouse input.
     * AI keeps running when window is unfocused.
     * Mouse isn't captured when dragging the window.
     */
    @SubscribeEvent
    public static void onClientSetup(FMLClientSetupEvent event) {
        event.enqueueWork(() -> {
            if (RuntimeRole.current() != RuntimeRole.BODY_CLIENT) {
                LCUMod.LOGGER.info("[LCUMod] Player client role active; actuator runtime is disabled");
                return;
            }
            ClientBodyRuntime.start();
            var options = Minecraft.getInstance().options;
            options.pauseOnLostFocus = !ServerPolicy.backgroundExecutionAllowed();
            if (!options.pauseOnLostFocus) {
                LCUMod.LOGGER.warn("[LCUMod] Background execution enabled by explicit server-policy configuration");
            }
        });
    }
}
