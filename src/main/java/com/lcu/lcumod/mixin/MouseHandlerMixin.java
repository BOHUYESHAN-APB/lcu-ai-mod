package com.lcu.lcumod.mixin;

import com.lcu.lcumod.action.InputIsolation;
import net.minecraft.client.MouseHandler;
import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.injection.At;
import org.spongepowered.asm.mixin.injection.Inject;
import org.spongepowered.asm.mixin.injection.callback.CallbackInfo;

/**
 * Prevents Minecraft from recapturing the cursor only while the AI owns input.
 */
@Mixin(MouseHandler.class)
public class MouseHandlerMixin {

    @Inject(method = "grabMouse", at = @At("HEAD"), cancellable = true)
    private void onGrabMouse(CallbackInfo ci) {
        if (InputIsolation.isAiControlled()) {
            ci.cancel();
        }
    }
}
