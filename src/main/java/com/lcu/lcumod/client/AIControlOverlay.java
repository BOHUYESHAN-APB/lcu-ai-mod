package com.lcu.lcumod.client;

import com.lcu.lcumod.LCUMod;
import com.lcu.lcumod.action.InputIsolation;
import net.minecraft.client.Minecraft;
import net.neoforged.api.distmarker.Dist;
import net.neoforged.bus.api.SubscribeEvent;
import net.neoforged.fml.common.EventBusSubscriber;
import net.neoforged.neoforge.client.event.InputEvent;
import net.neoforged.neoforge.client.event.RenderGuiLayerEvent;

/**
 * HUD overlay that shows AI/User control state.
 * 
 * KEY DESIGN:
 * 1. Renders the button in top-left corner
 * 2. When AI is controlling: mouse is released (cursor visible)
 * 3. The overlay is informational only
 * 4. F12 = the sole manual takeover path
 */
@EventBusSubscriber(modid = LCUMod.MODID, value = Dist.CLIENT)
public class AIControlOverlay {

    private static final int BUTTON_WIDTH = 120;
    private static final int BUTTON_HEIGHT = 20;
    private static final int MARGIN = 4;

    @SubscribeEvent
    public static void onRenderGui(RenderGuiLayerEvent.Post event) {
        if (!ClientBodyRuntime.isBodyClient()) return;
        Minecraft mc = Minecraft.getInstance();
        if (mc.player == null || mc.level == null) return;

        var graphics = event.getGuiGraphics();
        boolean aiOn = InputIsolation.isAiControlled();

        // Button position: top-left corner
        int x = MARGIN;
        int y = MARGIN;

        // Background
        int bgColor = aiOn ? 0xCC1B5E20 : 0xCCB71C1C;
        graphics.fill(x, y, x + BUTTON_WIDTH, y + BUTTON_HEIGHT, bgColor);

        // Border
        int borderColor = aiOn ? 0xFF4CAF50 : 0xFFEF5350;
        graphics.fill(x, y, x + BUTTON_WIDTH, y + 1, borderColor);
        graphics.fill(x, y + BUTTON_HEIGHT - 1, x + BUTTON_WIDTH, y + BUTTON_HEIGHT, borderColor);
        graphics.fill(x, y, x + 1, y + BUTTON_HEIGHT, borderColor);
        graphics.fill(x + BUTTON_WIDTH - 1, y, x + BUTTON_WIDTH, y + BUTTON_HEIGHT, borderColor);

        // Text
        String text = aiOn ? "AI 控制中" : "你控制中";
        int textColor = 0xFFFFFFFF;
        int textWidth = mc.font.width(text);
        int textX = x + (BUTTON_WIDTH - textWidth) / 2;
        int textY = y + (BUTTON_HEIGHT - mc.font.lineHeight) / 2;
        graphics.drawString(mc.font, text, textX, textY, textColor, false);
    }

    /**
     * Handle mouse click events.
     * When AI is controlling and mouse is released, check if click is on button.
     */
    @SubscribeEvent
    public static void onMouseClick(InputEvent.MouseButton.Pre event) {
        if (!ClientBodyRuntime.isBodyClient()) return;
        Minecraft mc = Minecraft.getInstance();
        if (mc.player == null) return;

        if (!InputIsolation.isAiControlled()) {
            InputIsolation.recordUserActivity();
        }

        if (InputIsolation.isAiControlled()) event.setCanceled(true);
    }

    /**
     * Handle keyboard input.
     * Only records user activity here.
     * F12 toggle is handled centrally by AIKeyHandler to avoid double-toggle.
     */
    @SubscribeEvent
    public static void onKeyPress(InputEvent.Key event) {
        if (!ClientBodyRuntime.isBodyClient()) return;
        if (!InputIsolation.isAiControlled() && event.getAction() == 1) {
            InputIsolation.recordUserActivity();
        }
    }

    /**
     * Handle mouse scroll events.
     * Block when AI is controlling.
     */
    @SubscribeEvent
    public static void onMouseScroll(InputEvent.MouseScrollingEvent event) {
        if (!ClientBodyRuntime.isBodyClient()) return;
        if (InputIsolation.isAiControlled()) {
            event.setCanceled(true);
        } else {
            InputIsolation.recordUserActivity();
        }
    }

}
