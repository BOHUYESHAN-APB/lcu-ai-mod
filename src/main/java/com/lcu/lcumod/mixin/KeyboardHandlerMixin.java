package com.lcu.lcumod.mixin;

import com.lcu.lcumod.action.InputIsolation;
import net.minecraft.client.KeyboardHandler;
import net.minecraft.client.Minecraft;
import net.minecraft.client.input.KeyEvent;
import org.lwjgl.sdl.SDLKeycode;
import org.lwjgl.sdl.SDLScancode;
import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.injection.At;
import org.spongepowered.asm.mixin.injection.Inject;
import org.spongepowered.asm.mixin.injection.callback.CallbackInfo;

/**
 * While the AI holds control, the physical keyboard does nothing except the control switch.
 *
 * <p>用户口径："AI 控制中要屏蔽用户键盘（只留 F12），避免与 AI 打架"——人按 W 的时候
 * {@code InputIsolation} 每 tick 在写自己的 WASD/视角，两方一起写表现为"人一按它就抖/停"。
 * 鼠标那一半早就由 {@link MouseHandlerMixin} 处理（光标保持释放），键盘这一半原来**没有实现**：
 * {@code AIKeyHandler.shouldCaptureKeyboard()} 声明了一直没人调用。这里补上真正的拦截点。
 *
 * <p>为什么是 mixin 而不是订阅事件：NeoForge 的 {@code InputEvent.Key} **不可取消**
 * （{@code InputEvent$Key extends InputEvent extends Event}，javap 实证），吞不掉按键；
 * 而且事件是在 {@code KeyboardHandler.keyPress} 里发出来的，靠它自己也拦不住后续处理。
 *
 * <p>**放行清单**（不是一句话"只留 F12"，这三条都是刻意的）：
 * <ul>
 *   <li><b>F12</b> —— 唯一的控制权开关，吞掉它人就出不来了；</li>
 *   <li><b>ESC</b> —— 安全出口：要能开菜单/退出/暂停，否则等于把人锁在世界里；</li>
 *   <li><b>有界面开着时全部放行</b> —— 按键归那个界面（聊天框、控制台页面…），
 *       也正好符合另一条硬约束"玩家自己开着界面时 AI 让路"。</li>
 * </ul>
 * 抬起（release）一律放行：按下被吞了就不会有"按下状态"需要抬起，放行能避免按键卡住的老问题。
 *
 * <p>实测要点（26.3）：{@code KeyboardHandler.keyPress(long window, int action, KeyEvent event)}，
 * {@code KeyEvent.key()/keycode()} 分别是平台键码与扫描码，两个都按 F12/ESC 比对
 * （{@code AIKeyHandler} 的 KeyMapping 用的是 {@code SDL_SCANCODE_F12}）。
 */
@Mixin(KeyboardHandler.class)
public class KeyboardHandlerMixin {

    @Inject(method = "keyPress", at = @At("HEAD"), cancellable = true)
    private void lcuBlockKeysWhileAiPlays(long window, int action, KeyEvent event, CallbackInfo ci) {
        if (!InputIsolation.isAiControlled()) {
            // 玩家控制：一切照旧，人和原版完全一样。
            return;
        }
        Minecraft mc = Minecraft.getInstance();
        if (mc.gui != null && mc.gui.screen() != null) {
            // 有界面开着：按键归界面（也符合"玩家开着界面 AI 让路"）。
            return;
        }
        if (isAlwaysAllowed(event)) {
            return;
        }
        if (action != 1 && action != 2) {
            // 只吞"按下/连发"（GLFW_PRESS=1、GLFW_REPEAT=2）；抬起放行，避免按键卡住。
            return;
        }
        ci.cancel();
    }

    /** 控制权开关与安全出口（F12 / ESC，键码与扫描码都认）。 */
    static boolean isAlwaysAllowed(KeyEvent event) {
        int key = event.key();
        int scancode = event.keycode();
        return key == SDLKeycode.SDLK_F12
            || scancode == SDLScancode.SDL_SCANCODE_F12
            || key == SDLKeycode.SDLK_ESCAPE
            || scancode == SDLScancode.SDL_SCANCODE_ESCAPE;
    }
}
