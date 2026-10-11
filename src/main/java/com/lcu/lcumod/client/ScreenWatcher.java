package com.lcu.lcumod.client;

import com.lcu.lcumod.LCUMod;
import net.neoforged.bus.api.SubscribeEvent;
import net.neoforged.fml.common.EventBusSubscriber;
import net.neoforged.neoforge.client.event.ScreenEvent;

/**
 * 记录"当前在哪个界面"（26.3 的 {@code Minecraft} 已经没有 screen 字段/getter，改用 NeoForge 事件）。
 *
 * 用途：AI 不在世界里时（主菜单 / 多人游戏列表 / 加载中）后端必须暂停 —— 需要让后端知道
 * 它现在停在哪个界面，而不是拿上一秒的世界状态继续下指令。
 */
@EventBusSubscriber(modid = LCUMod.MODID, value = net.neoforged.api.distmarker.Dist.CLIENT)
public final class ScreenWatcher {

    private static volatile String current = "";

    private ScreenWatcher() {
    }

    /** 当前界面类名（在世界里时为上一次打开过的界面残留，由 state 的 in_world 决定是否采信）。 */
    public static String current() {
        return current;
    }

    @SubscribeEvent
    public static void onScreenInit(ScreenEvent.Init.Post event) {
        if (event.getScreen() != null) {
            current = event.getScreen().getClass().getSimpleName();
        }
    }
}
