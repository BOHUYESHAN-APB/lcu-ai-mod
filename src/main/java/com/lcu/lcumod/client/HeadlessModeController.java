package com.lcu.lcumod.client;

import com.lcu.lcumod.LCUMod;
import com.lcu.lcumod.config.ModConfig;
import net.minecraft.client.CloudStatus;
import net.minecraft.client.GraphicsStatus;
import net.minecraft.client.Minecraft;
import net.minecraft.client.Options;
import net.minecraft.client.ParticleStatus;
import org.lwjgl.glfw.GLFW;

/**
 * Reduces rendering to near-zero when {@code headlessMode} is enabled.
 *
 * <p>The Minecraft client always requires a window and a GL context (there is
 * no true windowless mode), so "headless" here means: skip world rendering
 * entirely via the {@code HeadlessRenderSkipMixin}, cap the framerate, apply a
 * low-power graphics preset, and iconify the window. The wire-driven body and
 * its programmatic actions do not depend on rendering or window focus.</p>
 */
public final class HeadlessModeController {
    private static volatile boolean active;

    private HeadlessModeController() {}

    public static void applyIfEnabled(Minecraft minecraft) {
        if (!ModConfig.HEADLESS_MODE.get()) {
            return;
        }
        active = true;
        Options options = minecraft.options;
        options.framerateLimit().set(2);
        options.renderDistance().set(2);
        options.graphicsMode().set(GraphicsStatus.FAST);
        options.particles().set(ParticleStatus.MINIMAL);
        options.cloudStatus().set(CloudStatus.OFF);
        options.pauseOnLostFocus = false;
        long handle = minecraft.getWindow().getWindow();
        if (handle != 0L) {
            GLFW.glfwIconifyWindow(handle);
        }
        LCUMod.LOGGER.info(
                "[LCUMod] Headless mode active: world rendering skipped, FPS capped, low-power preset applied");
    }

    public static boolean isActive() {
        return active;
    }
}
