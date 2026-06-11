package com.lcu.lcumod.action;

import com.lcu.lcumod.LCUMod;
import net.minecraft.client.Minecraft;
import net.minecraft.server.MinecraftServer;
import net.neoforged.bus.api.SubscribeEvent;
import net.neoforged.fml.common.EventBusSubscriber;
import net.neoforged.neoforge.client.event.ClientTickEvent;

/**
 * Bridge: receives ClientTickEvent.Post via @EventBusSubscriber and delegates
 * to the ActionExecutor singleton instance.
 */
@EventBusSubscriber(modid = LCUMod.MODID, value = net.neoforged.api.distmarker.Dist.CLIENT)
public class ActionExecutorBridge {

    @SubscribeEvent
    public static void onClientTick(ClientTickEvent.Post event) {
        if (LCUMod.ACTION != null) {
            LCUMod.ACTION.onTick();
        }
    }
}
