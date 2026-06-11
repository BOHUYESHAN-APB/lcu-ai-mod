package com.lcu.lcumod.debug;

import com.lcu.lcumod.LCUMod;
import net.minecraft.client.Minecraft;
import net.neoforged.bus.api.SubscribeEvent;
import net.neoforged.fml.common.EventBusSubscriber;
import net.neoforged.neoforge.client.event.ClientTickEvent;

/**
 * DEBUG: Test if @EventBusSubscriber auto-discovery works for ClientTickEvent.
 * Remove once event bus is confirmed working.
 */
@EventBusSubscriber(modid = LCUMod.MODID, value = net.neoforged.api.distmarker.Dist.CLIENT)
public class EventTest {

    private static int tickCount = 0;

    @SubscribeEvent
    public static void onClientTick(ClientTickEvent.Post event) {
        var mc = Minecraft.getInstance();
        if (mc.level == null || mc.player == null) return;
        if (tickCount++ % 100 == 0) {
            LCUMod.LOGGER.info("[EventTest] CLIENT TICK #{}: player={} health={}",
                tickCount, mc.player.getName().getString(), mc.player.getHealth());
        }
    }
}
