package com.lcu.lcumod.action;

import com.lcu.lcumod.LCUMod;
import com.lcu.lcumod.client.ClientBodyRuntime;
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
        if (ClientBodyRuntime.ACTION != null) {
            ClientBodyRuntime.ACTION.onTick();
        }
    }
}
