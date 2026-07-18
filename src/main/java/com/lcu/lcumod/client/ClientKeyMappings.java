package com.lcu.lcumod.client;

import com.lcu.lcumod.LCUMod;
import net.neoforged.api.distmarker.Dist;
import net.neoforged.bus.api.SubscribeEvent;
import net.neoforged.fml.common.EventBusSubscriber;
import net.neoforged.neoforge.client.event.RegisterKeyMappingsEvent;

@EventBusSubscriber(modid = LCUMod.MODID, bus = EventBusSubscriber.Bus.MOD, value = Dist.CLIENT)
public final class ClientKeyMappings {
    private ClientKeyMappings() {}

    @SubscribeEvent
    public static void register(RegisterKeyMappingsEvent event) {
        event.register(AIKeyHandler.TOGGLE_AI);
        event.register(PlayerConversationKeyHandler.OPEN_CONVERSATION);
    }
}
