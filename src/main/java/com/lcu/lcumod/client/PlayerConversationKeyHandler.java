package com.lcu.lcumod.client;

import com.lcu.lcumod.LCUMod;
import com.lcu.lcumod.config.RuntimeRole;
import com.mojang.blaze3d.platform.InputConstants;
import net.minecraft.client.KeyMapping;
import net.minecraft.client.Minecraft;
import net.neoforged.api.distmarker.Dist;
import net.neoforged.bus.api.SubscribeEvent;
import net.neoforged.fml.common.EventBusSubscriber;
import net.neoforged.neoforge.client.event.InputEvent;
import org.lwjgl.glfw.GLFW;

@EventBusSubscriber(modid = LCUMod.MODID, value = Dist.CLIENT)
public final class PlayerConversationKeyHandler {
    public static final KeyMapping OPEN_CONVERSATION = new KeyMapping(
            "key.lcumod.open_conversation",
            InputConstants.Type.KEYSYM,
            GLFW.GLFW_KEY_P,
            "key.categories.lcumod"
    );

    private PlayerConversationKeyHandler() {}

    @SubscribeEvent
    public static void onKeyPress(InputEvent.Key event) {
        if (!RuntimeRole.current().activatesPlayerConversation()) return;
        if (OPEN_CONVERSATION.consumeClick()) {
            Minecraft minecraft = Minecraft.getInstance();
            if (minecraft.player != null) {
                minecraft.setScreen(new PlayerConversationScreen());
            }
        }
    }
}
