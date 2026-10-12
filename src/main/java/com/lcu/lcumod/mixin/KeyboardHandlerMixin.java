package com.lcu.lcumod.mixin;

import com.lcu.lcumod.action.InputIsolation;
import com.lcu.lcumod.action.KeyShield;
import com.mojang.blaze3d.platform.InputConstants;
import net.minecraft.client.KeyMapping;
import net.minecraft.client.KeyboardHandler;
import net.minecraft.client.Minecraft;
import net.minecraft.client.Options;
import net.minecraft.client.input.KeyEvent;
import org.lwjgl.sdl.SDLKeycode;
import org.lwjgl.sdl.SDLScancode;
import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.injection.At;
import org.spongepowered.asm.mixin.injection.Inject;
import org.spongepowered.asm.mixin.injection.callback.CallbackInfo;

import java.util.HashSet;
import java.util.Set;

/**
 * While the AI holds control, the AI's own operation keys are its own — everything else still works.
 *
 * <p>用户口径："AI 控制中要屏蔽用户键盘（只留 F12），避免与 AI 打架"；随后实机反馈
 * "按 F12 和 ESC 都是无限效果" —— 因为上一版是**放行清单**，编码没匹配上就把开关和安全出口
 * 一起吞了，人直接被困在世界里。现在策略反过来（见 {@link KeyShield}）：
 * **只拦"操作类"键位，其余一律放行；ESC / F12 永远放行**。
 *
 * <p>名单来源：**玩家自己的按键绑定**（{@code Options.keyUp/keyDown/.../keyHotbarSlots}），
 * 而不是硬编码键码 —— 这样跟着玩家改键走，也不依赖"vanilla 到底拿哪个字段比较"的推断
 * （{@code key()} 与 {@code keycode()} 两个都比对）。
 *
 * <p>为什么是 mixin：NeoForge 的 {@code InputEvent.Key} **不可取消**（`InputEvent$Key extends
 * InputEvent extends Event`，javap 实证），而事件又是在 {@code KeyboardHandler.keyPress} 里发出来的。
 */
@Mixin(KeyboardHandler.class)
public class KeyboardHandlerMixin {

    /** 永不拦：安全出口（ESC）+ 控制开关（F12）。三种可能的编码都收，避免再因为单位判断错而锁人。 */
    private static final Set<Integer> ALWAYS_ALLOWED = Set.of(
            InputConstants.KEY_ESCAPE,
            SDLScancode.SDL_SCANCODE_ESCAPE,
            SDLKeycode.SDLK_ESCAPE,
            InputConstants.KEY_F12,
            SDLScancode.SDL_SCANCODE_F12,
            SDLKeycode.SDLK_F12);

    @Inject(method = "keyPress", at = @At("HEAD"), cancellable = true)
    private void lcuBlockKeysWhileAiPlays(long window, int action, KeyEvent event, CallbackInfo ci) {
        if (!InputIsolation.isAiControlled()) {
            return;
        }
        Minecraft mc = Minecraft.getInstance();
        if (mc.gui != null && mc.gui.screen() != null) {
            // 有界面开着：按键归界面（也符合"玩家开着界面 AI 让路"）。
            return;
        }
        if (action == 0) {
            // 只吞"按下/连发"（GLFW_PRESS=1、GLFW_REPEAT=2）；抬起放行，避免按键卡住。
            return;
        }
        if (!KeyShield.blocks(event.key(), event.keycode(), gameplayKeys(mc), ALWAYS_ALLOWED)) {
            return;
        }
        ci.cancel();
    }

    /** 要拦的键位 = 玩家自己绑定的那些"操作键"（移动 / 交互 / 热栏 / 聊天栏…）。 */
    private static Set<Integer> gameplayKeys(Minecraft mc) {
        Set<Integer> keys = new HashSet<>();
        Options options = mc.options;
        if (options == null) {
            return keys;
        }
        add(keys, options.keyUp);
        add(keys, options.keyDown);
        add(keys, options.keyLeft);
        add(keys, options.keyRight);
        add(keys, options.keyJump);
        add(keys, options.keyShift);
        add(keys, options.keySprint);
        add(keys, options.keyAttack);
        add(keys, options.keyUse);
        add(keys, options.keyInventory);
        add(keys, options.keyDrop);
        add(keys, options.keyChat);
        add(keys, options.keyCommand);
        if (options.keyHotbarSlots != null) {
            for (KeyMapping mapping : options.keyHotbarSlots) {
                add(keys, mapping);
            }
        }
        return keys;
    }

    private static void add(Set<Integer> keys, KeyMapping mapping) {
        if (mapping != null && mapping.getKey() != null) {
            keys.add(mapping.getKey().getValue());
        }
    }
}
