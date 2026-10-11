package com.lcu.lcumod.mixin;

import com.lcu.lcumod.action.InputIsolation;
import net.minecraft.client.MouseHandler;
import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.injection.At;
import org.spongepowered.asm.mixin.injection.Inject;
import org.spongepowered.asm.mixin.injection.callback.CallbackInfo;

/**
 * Keeps the mouse cursor released while the AI holds control, so the human can use the desktop /
 * other windows while the AI plays.
 *
 * GUARDED, never unconditional. {@code grabMouse} is only cancelled while
 * {@link InputIsolation#isAiControlled()} is true:
 *   - AI control  → the vanilla grab is suppressed (pointer stays free).
 *   - user control → the grab is left untouched, so the human can look around exactly like vanilla.
 * The switch back to user control flips the flag BEFORE calling {@code grabMouse}, so the explicit
 * re-grab in {@code InputIsolation.toggleControl()} still works.
 *
 * This is the "never wrestle the mouse away from the player" rule: the mod only ever *releases* the
 * pointer, it never grabs it. See docs/dev/INPUT_MODEL_COMPARISON.md.
 *
 * Verified against MC 26.3 (javap on minecraft_26.3_client.jar):
 *   public class net.minecraft.client.MouseHandler { public void grabMouse(); ... }
 */
@Mixin(MouseHandler.class)
public class MouseHandlerMixin {

    @Inject(method = "grabMouse", at = @At("HEAD"), cancellable = true)
    private void onGrabMouse(CallbackInfo ci) {
        if (InputIsolation.isAiControlled()) {
            // AI owns control: keep the cursor free for the human's desktop use.
            ci.cancel();
        }
        // Otherwise (user control) fall through to vanilla grabMouse — the player gets their mouse.
    }
}
