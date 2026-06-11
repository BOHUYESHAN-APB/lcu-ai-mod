package com.lcu.lcumod.client;

import com.lcu.lcumod.LCUMod;
import com.lcu.lcumod.action.ActionExecutor;
import com.lcu.lcumod.action.InputIsolation;
import com.lcu.lcumod.action.MovementSystem;
import com.mojang.blaze3d.platform.InputConstants;
import net.minecraft.client.KeyMapping;
import net.minecraft.client.Minecraft;
import net.neoforged.api.distmarker.Dist;
import net.neoforged.bus.api.SubscribeEvent;
import net.neoforged.fml.common.EventBusSubscriber;
import net.neoforged.neoforge.client.event.RegisterKeyMappingsEvent;
import net.neoforged.neoforge.client.event.InputEvent;
import org.lwjgl.glfw.GLFW;

/**
 * Keybind handler for AI/User control toggle.
 * Uses F12 key (rarely conflicts with modpacks).
 * 
 * This is the ONLY way to switch between AI and user control.
 * Mouse hover and window focus do NOT trigger control switch.
 */
@EventBusSubscriber(modid = LCUMod.MODID, value = Dist.CLIENT)
public class AIKeyHandler {

    public static final KeyMapping TOGGLE_AI = new KeyMapping(
        "key.lcumod.toggle_ai",
        InputConstants.Type.KEYSYM,
        GLFW.GLFW_KEY_F12,
        "key.categories.lcumod"
    );

    @SubscribeEvent
    public static void onRegisterKey(RegisterKeyMappingsEvent event) {
        event.register(TOGGLE_AI);
    }

    @SubscribeEvent
    public static void onKeyPress(InputEvent.Key event) {
        if (TOGGLE_AI.consumeClick()) {
            // Toggle control mode
            ActionExecutor.toggleAiControl();
            
            boolean newState = ActionExecutor.isAiControlled();
            LCUMod.LOGGER.info("[Key] AI control toggled: {}", newState);
        }
    }

    /**
     * Called from mouse event handler to check if mouse should be captured.
     * Returns true if the event should be cancelled (AI is controlling).
     */
    public static boolean shouldCaptureMouse() {
        return InputIsolation.isAiControlled();
    }

    /**
     * Called from keyboard event handler to check if key should be captured.
     * Returns true if the event should be cancelled (AI is controlling).
     */
    public static boolean shouldCaptureKeyboard() {
        return InputIsolation.isAiControlled();
    }
}
