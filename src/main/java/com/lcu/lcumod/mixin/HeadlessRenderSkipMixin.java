package com.lcu.lcumod.mixin;

import com.lcu.lcumod.client.HeadlessModeController;
import net.minecraft.client.Camera;
import net.minecraft.client.DeltaTracker;
import net.minecraft.client.renderer.GameRenderer;
import net.minecraft.client.renderer.LevelRenderer;
import net.minecraft.client.renderer.LightTexture;
import org.joml.Matrix4f;
import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.injection.At;
import org.spongepowered.asm.mixin.injection.Inject;
import org.spongepowered.asm.mixin.injection.callback.CallbackInfo;

/**
 * Skips world rendering entirely while {@code headlessMode} is active. World
 * rendering is by far the largest per-frame cost; the HUD/GUI still renders
 * (cheap), which keeps the game fully functional over the wire protocol.
 */
@Mixin(LevelRenderer.class)
public class HeadlessRenderSkipMixin {

    @Inject(method = "renderLevel", at = @At("HEAD"), cancellable = true)
    private void lcumod_skipRenderWhenHeadless(
            DeltaTracker deltaTracker,
            boolean renderBlockOutline,
            Camera camera,
            GameRenderer gameRenderer,
            LightTexture lightTexture,
            Matrix4f modelViewMatrix,
            Matrix4f projectionMatrix,
            CallbackInfo ci) {
        if (HeadlessModeController.isActive()) {
            ci.cancel();
        }
    }
}
