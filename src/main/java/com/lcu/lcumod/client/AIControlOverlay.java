package com.lcu.lcumod.client;

import com.lcu.lcumod.LCUMod;
import com.lcu.lcumod.action.ActionExecutor;
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
 * 3. Click on button = switch to user control
 * 4. F12 = switch control mode
 * 5. When user is controlling: mouse is grabbed (cursor hidden)
 */
@EventBusSubscriber(modid = LCUMod.MODID, value = Dist.CLIENT)
public class AIControlOverlay {

    private static final int BUTTON_WIDTH = 120;
    private static final int BUTTON_HEIGHT = 20;
    private static final int MARGIN = 4;
    /** 状态框实际宽度（文字变长会变宽），点击命中要与绘制一致。 */
    private static int lastBoxWidth = BUTTON_WIDTH;

    @SubscribeEvent
    public static void onRenderGui(RenderGuiLayerEvent.Post event) {
        Minecraft mc = Minecraft.getInstance();
        if (mc.player == null || mc.level == null) return;

        var graphics = event.getGuiGraphics();
        boolean aiOn = InputIsolation.isAiControlled();

        // Button position: top-left corner
        int x = MARGIN;
        int y = MARGIN;

        // 文字 = 控制权 + 当前在干什么（趣味词）+ 后端大模型给的描述词
        String note = com.lcu.lcumod.client.HudNote.current();
        String text = (aiOn ? "AI 控制中" : "你控制中") + " · " + WindowTitle.activity(mc)
                + (note.isEmpty() ? "" : " · " + note);
        int boxWidth = Math.max(BUTTON_WIDTH, mc.font.width(text) + 12);
        lastBoxWidth = boxWidth;

        // Background
        int bgColor = aiOn ? 0xCC1B5E20 : 0xCCB71C1C;
        graphics.fill(x, y, x + boxWidth, y + BUTTON_HEIGHT, bgColor);

        // Border
        int borderColor = aiOn ? 0xFF4CAF50 : 0xFFEF5350;
        graphics.fill(x, y, x + boxWidth, y + 1, borderColor);
        graphics.fill(x, y + BUTTON_HEIGHT - 1, x + boxWidth, y + BUTTON_HEIGHT, borderColor);
        graphics.fill(x, y, x + 1, y + BUTTON_HEIGHT, borderColor);
        graphics.fill(x + boxWidth - 1, y, x + boxWidth, y + BUTTON_HEIGHT, borderColor);

        // Text
        int textColor = 0xFFFFFFFF;
        int textWidth = mc.font.width(text);
        int textX = x + (boxWidth - textWidth) / 2;
        int textY = y + (BUTTON_HEIGHT - mc.font.lineHeight) / 2;
        graphics.text(mc.font, text, textX, textY, textColor, false);

        // 端口被占用（多半是开了第二个游戏窗口）时必须**在游戏里**看见：
        // 否则表现为"聊天/控制全没反应"，用户根本不知道 wire 根本没监听。
        String listenError = com.lcu.lcumod.network.WireServer.LISTEN_ERROR;
        if (listenError != null) {
            String warn = "⚠ " + listenError;
            int warnWidth = mc.font.width(warn) + 8;
            int warnY = y + BUTTON_HEIGHT + MARGIN;
            graphics.fill(x, warnY, x + warnWidth, warnY + 14, 0xCC7F1D1D);
            graphics.text(mc.font, warn, x + 4, warnY + 3, 0xFFFFD54F, false);
        }
    }

    /**
     * Handle mouse click events.
     * When AI is controlling and mouse is released, check if click is on button.
     */
    @SubscribeEvent
    public static void onMouseClick(InputEvent.MouseButton.Pre event) {
        Minecraft mc = Minecraft.getInstance();
        if (mc.player == null) return;

        if (!InputIsolation.isAiControlled()) {
            InputIsolation.recordUserActivity();
        }

        // Only handle left click release (action = 1)
        if (event.getButton() != 0 || event.getAction() != 1) return; // 0 = left button, 1 = released

        // Only check when AI is controlling (mouse is released)
        if (!InputIsolation.isAiControlled()) return;

        // Get mouse position
        double mouseX = mc.mouseHandler.xpos();
        double mouseY = mc.mouseHandler.ypos();

        // Scale to GUI coordinates
        double scale = mc.getWindow().getGuiScale();
        double guiX = mouseX / scale;
        double guiY = mouseY / scale;

        // Check if click is on the button
        if (isClickInButton(guiX, guiY)) {
            handleClick();
            event.setCanceled(true); // Cancel the event so it doesn't trigger other actions
        }
    }

    /**
     * Handle keyboard input.
     * Only records user activity here.
     * F12 toggle is handled centrally by AIKeyHandler to avoid double-toggle.
     */
    @SubscribeEvent
    public static void onKeyPress(InputEvent.Key event) {
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
        if (InputIsolation.isAiControlled()) {
            event.setCanceled(true);
        } else {
            InputIsolation.recordUserActivity();
        }
    }

    /**
     * Check if a click is within the button area.
     */
    public static boolean isClickInButton(double mouseX, double mouseY) {
        return mouseX >= MARGIN && mouseX <= MARGIN + lastBoxWidth
            && mouseY >= MARGIN && mouseY <= MARGIN + BUTTON_HEIGHT;
    }

    /**
     * Handle button click - toggle AI/User control.
     */
    public static void handleClick() {
        ActionExecutor.toggleAiControl();
        boolean newState = ActionExecutor.isAiControlled();
        LCUMod.LOGGER.info("[HUD] AI control toggled: {}", newState);
    }
}
