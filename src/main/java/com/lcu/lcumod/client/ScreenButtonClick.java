package com.lcu.lcumod.client;

import com.lcu.lcumod.action.ContainerController;
import net.minecraft.client.Minecraft;
import net.minecraft.client.gui.components.AbstractButton;
import net.minecraft.client.gui.components.AbstractWidget;
import net.minecraft.client.gui.components.events.ContainerEventHandler;
import net.minecraft.client.gui.components.events.GuiEventListener;
import net.minecraft.client.gui.screens.Screen;
import net.minecraft.client.gui.screens.inventory.AbstractContainerScreen;
import net.minecraft.client.input.MouseButtonEvent;
import net.minecraft.client.input.MouseButtonInfo;

import java.util.ArrayList;
import java.util.List;

/**
 * 控件级点击：在**当前已打开的**界面里找文本匹配的控件，**直接触发它的 {@code onPress} /
 * {@code mouseClicked}** —— 不移动真鼠标、不模拟鼠标事件、不抓鼠标、不收键盘。
 *
 * <p>用户口径：「tpa 需要在对话列表去点同意 accept……必须用鼠标去点对话框里出现的同意和拒绝」
 * → 我们要的是**不碰鼠标**地做到同样的事：界面本身是**服务端推来的**（不是我们开的 ✓），
 * 我们只是在里面按控件。
 *
 * <p>界面来源：{@code Minecraft#gui#screen()}。**我们自己绝不开任何界面**
 * （{@code src/main} 里没有任何界面打开调用，验收 grep 为空）。
 *
 * <p>纯逻辑（放行判定 / 文本匹配）在 {@link ScreenClickPlan}，本类只负责"遍历 + 触发"。
 */
public final class ScreenButtonClick {

    /** 点到的控件（给后端审计"点了哪个控件的哪段文字"）。 */
    public record Hit(int index, String type, String label, int x, int y, int width, int height) {
    }

    /**
     * 结果。
     *
     * @param status       {@code ok} / {@code no_effect} / {@code no_screen} / {@code user_screen_open} / {@code no_match}
     * @param screen       界面类名（无界面时为空）
     * @param screenTitle  界面标题（无界面时为空）
     * @param hit          点到的控件（{@code ok} / {@code no_effect} 时有值）
     * @param candidates   界面上可点控件的文字（{@code no_match} 时给后端调规则）
     */
    public record Result(String status, String screen, String screenTitle, Hit hit, List<String> candidates) {
    }

    private static final int MAX_DEPTH = 8;

    private ScreenButtonClick() {
    }

    /** 在**当前界面**里找文本匹配 {@code query} 的控件并触发它。 */
    public static Result click(Minecraft mc, String query) {
        Screen screen = mc == null || mc.gui == null ? null : mc.gui.screen();
        if (screen == null) {
            return new Result("no_screen", "", "", null, List.of());
        }
        boolean containerScreen = screen instanceof AbstractContainerScreen<?>;
        boolean ours = false;
        if (containerScreen && mc.player != null && mc.player.containerMenu != null
                && mc.player.containerMenu != mc.player.inventoryMenu) {
            ours = ContainerController.aiContainerIntentPending()
                    && ((AbstractContainerScreen<?>) screen).getMenu() == mc.player.containerMenu;
        }
        ScreenClickPlan.Decision decision = ScreenClickPlan.decide(
                true, screen.isPauseScreen(), containerScreen, ours);
        String screenName = screen.getClass().getSimpleName();
        String title = screen.getTitle() == null ? "" : screen.getTitle().getString();
        if (decision == ScreenClickPlan.Decision.USER_SCREEN) {
            return new Result("user_screen_open", screenName, title, null, List.of());
        }

        List<AbstractWidget> widgets = new ArrayList<>();
        collect(screen, widgets, 0);
        List<ScreenClickPlan.Widget> descriptors = new ArrayList<>(widgets.size());
        for (int i = 0; i < widgets.size(); i++) {
            AbstractWidget w = widgets.get(i);
            String label = w.getMessage() == null ? "" : w.getMessage().getString();
            descriptors.add(new ScreenClickPlan.Widget(i, label, w.getClass().getSimpleName()));
        }

        ScreenClickPlan.Match match = ScreenClickPlan.match(descriptors, query);
        if (!match.found()) {
            return new Result("no_match", screenName, title, null, match.candidates());
        }

        AbstractWidget target = widgets.get(match.index());
        boolean triggered = trigger(target);
        ScreenClickPlan.Widget desc = descriptors.get(match.index());
        Hit hit = new Hit(desc.index(), desc.type(), desc.label(),
                target.getX(), target.getY(), target.getWidth(), target.getHeight());
        return new Result(triggered ? "ok" : "no_effect", screenName, title, hit, match.candidates());
    }

    /** 递归收集**可点**（active + visible）的控件；界面嵌套（列表/面板）也要能找进去。 */
    private static void collect(GuiEventListener element, List<AbstractWidget> out, int depth) {
        if (element == null || depth > MAX_DEPTH) {
            return;
        }
        if (element instanceof AbstractWidget widget && widget.visible && widget.active) {
            out.add(widget);
        }
        if (element instanceof ContainerEventHandler container) {
            for (GuiEventListener child : container.children()) {
                collect(child, out, depth + 1);
            }
        }
    }

    /** 直接触发控件：按钮走 {@code onPress}，其它可点控件走 {@code mouseClicked}（坐标用控件中心）。 */
    private static boolean trigger(AbstractWidget widget) {
        double centerX = widget.getX() + widget.getWidth() / 2.0;
        double centerY = widget.getY() + widget.getHeight() / 2.0;
        MouseButtonEvent event = new MouseButtonEvent(centerX, centerY, new MouseButtonInfo(0, 0));
        if (widget instanceof AbstractButton button) {
            button.onPress(event);
            return true;
        }
        return widget.mouseClicked(event, false);
    }
}
