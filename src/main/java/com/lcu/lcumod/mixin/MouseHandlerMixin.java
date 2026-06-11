package com.lcu.lcumod.mixin;

import net.minecraft.client.Minecraft;
import net.minecraft.client.MouseHandler;
import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.injection.At;
import org.spongepowered.asm.mixin.injection.Inject;
import org.spongepowered.asm.mixin.injection.callback.CallbackInfo;

/**
 * Prevents the game from grabbing/capturing the mouse cursor.
 * This allows dragging the MC window without the mouse being locked.
 * The AI controls player rotation via server-side commands anyway.
 */
@Mixin(MouseHandler.class)
public class MouseHandlerMixin {

    @Inject(method = "grabMouse", at = @At("HEAD"), cancellable = true)
    private void onGrabMouse(CallbackInfo ci) {
        // Cancel cursor capture — keep cursor free for window operations
        ci.cancel();
    }
}
