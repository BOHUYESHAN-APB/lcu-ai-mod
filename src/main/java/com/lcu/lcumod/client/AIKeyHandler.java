package com.lcu.lcumod.client;

import com.lcu.lcumod.LCUMod;
import com.lcu.lcumod.action.ActionExecutor;
import com.mojang.blaze3d.platform.InputConstants;
import net.minecraft.client.KeyMapping;
import net.minecraft.client.Minecraft;
import net.neoforged.api.distmarker.Dist;
import net.neoforged.bus.api.SubscribeEvent;
import net.neoforged.fml.common.EventBusSubscriber;
import net.neoforged.neoforge.client.event.RegisterKeyMappingsEvent;
import net.neoforged.neoforge.client.event.InputEvent;
import org.lwjgl.sdl.SDLScancode;

/**
 * Keybind handler for AI/User control toggle.
 * Uses F12 key (rarely conflicts with modpacks).
 *
 * TRAP (26.3 / SDL3): InputConstants.isKeyDown(int) indexes the raw SDL keyboard-state buffer
 * (512 bytes, indexed by SDL_SCANCODE_*). A keycode constant such as SDLKeycode.SDLK_F12
 * (0x40000045 = 1073741893) as that index crashes the client with IndexOutOfBoundsException as
 * soon as anything calls KeyMapping.setAll() — e.g. MouseHandler.grabMouse() when a world finishes
 * loading. Vanilla stores scancodes here (key.fullscreen = bipush 68 = SDL_SCANCODE_F11).
 *
 * This is the ONLY way to switch between AI and user control.
 * Mouse hover and window focus do NOT trigger control switch.
 */
@EventBusSubscriber(modid = LCUMod.MODID, value = Dist.CLIENT)
public class AIKeyHandler {

    public static final KeyMapping TOGGLE_AI = new KeyMapping(
        "key.lcumod.toggle_ai",
        InputConstants.Type.KEYBOARD,
        SDLScancode.SDL_SCANCODE_F12,
        KeyMapping.Category.MISC
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
}
