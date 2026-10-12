package com.lcu.lcumod.client;

import com.lcu.lcumod.LCUMod;
import com.lcu.lcumod.action.InputIsolation;
import net.minecraft.client.Minecraft;
import net.neoforged.api.distmarker.Dist;
import net.neoforged.bus.api.SubscribeEvent;
import net.neoforged.fml.common.EventBusSubscriber;
import net.neoforged.neoforge.client.event.InputEvent;

/**
 * AI 持控制权时，**鼠标左右键也不许动游戏**（用户实机："我鼠标左键右键在这个屏幕上，它居然是有效果的"）。
 *
 * <p>原来的做法只处理了"光标不被抓走"（见 `MouseHandlerMixin` 只拦 `grabMouse`）—— 于是指针是自由的，
 * 但**点在游戏窗口上照样等于游戏内点击**：左键会打/挖、右键会放/用。AI 正在操作时这就是在跟它抢，
 * 而且可能真的改世界（用户看到的是"我点一下它居然去挖/放了"）。
 *
 * <p>为什么不用 mixin：NeoForge 的 {@code InputEvent.MouseButton.Pre} **本来就是可取消的**
 * （javap 实证 `implements net.neoforged.bus.api.ICancellableEvent`），拦在事件层更稳、也不用再碰
 * `MouseHandler` 的方法签名。跟键盘那边一样：**开着界面时一律放行**（你要用游戏内 GUI/控制台页面），
 * 并且只拦左右中三键，其它输入（滚轮、移动）不动。
 */
@EventBusSubscriber(modid = LCUMod.MODID, value = Dist.CLIENT)
public final class MouseShield {

    /** 只拦鼠标按键：0 左、1 右、2 中（其它值不动，避免误伤奇怪的设备）。 */
    private static final int BUTTON_LEFT = 0;
    private static final int BUTTON_RIGHT = 1;
    private static final int BUTTON_MIDDLE = 2;

    private MouseShield() {
    }

    @SubscribeEvent
    public static void onMouseButton(InputEvent.MouseButton.Pre event) {
        if (!InputIsolation.isAiControlled()) {
            return;
        }
        Minecraft mc = Minecraft.getInstance();
        if (mc.gui != null && mc.gui.screen() != null) {
            // 有界面开着：鼠标归那个界面（也符合"玩家开着界面 AI 让路"）。
            return;
        }
        int button = event.getButton();
        if (button != BUTTON_LEFT && button != BUTTON_RIGHT && button != BUTTON_MIDDLE) {
            return;
        }
        // 只吞"按下"（GLFW_PRESS=1）：抬起放行，避免按住状态残留。
        if (event.getAction() != 1) {
            return;
        }
        event.setCanceled(true);
    }
}
