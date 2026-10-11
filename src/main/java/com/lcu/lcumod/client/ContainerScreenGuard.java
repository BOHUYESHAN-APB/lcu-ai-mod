package com.lcu.lcumod.client;

import com.lcu.lcumod.LCUMod;
import com.lcu.lcumod.action.ContainerController;
import net.minecraft.client.gui.screens.Screen;
import net.minecraft.client.gui.screens.inventory.AbstractContainerScreen;
import net.neoforged.api.distmarker.Dist;
import net.neoforged.bus.api.SubscribeEvent;
import net.neoforged.fml.common.EventBusSubscriber;
import net.neoforged.neoforge.client.event.ScreenEvent;

/**
 * "不抢鼠标"的另一半：把 **AI 自己开的容器界面**按住不显示。
 *
 * <p>为什么需要它：26.3 原版在收到 {@code ClientboundOpenScreenPacket} 后会自己弹容器界面
 * （javap 实证：{@code ClientPacketListener.handleOpenScreen → MenuScreens.create →
 * MenuScreens$ScreenConstructor.fromPacket}，其中先 {@code player.containerMenu = menu}，
 * 再调用 Gui 的界面设置入口把 Screen 挂上去）。也就是说"AI 右键开箱子"这件事原版会附带一个 GUI，
 * 会把玩家的键盘抢走 —— 与用户"会不会抢鼠标"的担心冲突。
 *
 * <p>做法：只订阅**可取消**的 {@code ScreenEvent.Opening}（由 Gui 的界面设置入口 post），
 * 且**只在 {@link ContainerController} 刚发起通用右键的那 ~2s 窗口内**取消容器界面；
 * 菜单本身仍然建立（原版是先建 menu 再挂界面），所以点击包驱动照旧可用。
 * 玩家自己打开的界面（窗口早已过期）一律照常显示。
 *
 * <p>本类不调用任何界面打开方法：它只是**阻止**一次打开，因此验收里"界面打开调用"的 grep 仍为空。
 */
@EventBusSubscriber(modid = LCUMod.MODID, value = Dist.CLIENT)
public final class ContainerScreenGuard {

    private ContainerScreenGuard() {
    }

    @SubscribeEvent
    public static void onScreenOpening(ScreenEvent.Opening event) {
        Screen newScreen = event.getNewScreen();
        if (newScreen == null) {
            return;
        }
        boolean containerScreen = newScreen instanceof AbstractContainerScreen<?>;
        if (!containerScreen) {
            return;
        }
        if (!ContainerController.aiContainerIntentPending()) {
            return; // 玩家自己打开的 → 照常显示
        }
        ContainerController.clearAiContainerIntent();
        event.setCanceled(true);
        LCUMod.LOGGER.debug("[LCUMod] suppressed AI-opened container screen {}",
                newScreen.getClass().getSimpleName());
    }
}
