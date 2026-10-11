package com.lcu.lcumod.client;

import java.util.ArrayList;
import java.util.List;
import java.util.Locale;

/**
 * {@code click_screen_button} 的**纯逻辑部分**（可自检）：放行判定 + 控件文本匹配。
 *
 * <p>背景（用户口径）：TPA 这类"服务端弹出的对话框"必须能在**不碰鼠标**的前提下按控件级触发。
 * 真正的界面遍历与触发在 {@link ScreenButtonClick}（要 Minecraft 类型），这里只放可测的判定：
 * <ul>
 *   <li>{@link #decide} —— 界面该不该动（没有界面 / 玩家自己的界面 → 让路）；</li>
 *   <li>{@link #match} —— 在候选控件里按文本找目标；找不到就把候选文字带出去给后端调规则。</li>
 * </ul>
 *
 * <p>让路口径与容器命令保持一致（见 {@code ContainerPlan.gateReason}）：
 * <ul>
 *   <li>没有界面 → {@code no_screen}（不做任何事）；</li>
 *   <li>玩家自己开的界面（原版 {@code isPauseScreen()} = 客户端本地菜单，或"不是我们发起的容器界面"）
 *       → 让路拒绝；</li>
 *   <li>其余（服务端推来的对话框 / 我们刚发起要开的容器）→ 可动。</li>
 * </ul>
 */
public final class ScreenClickPlan {

    /** 放行判定。 */
    public enum Decision {
        /** 当前没有界面。 */
        NO_SCREEN,
        /** 玩家自己的界面（本地菜单 / 非 AI 发起的容器界面）→ 让路。 */
        USER_SCREEN,
        /** 可以在此界面上点控件。 */
        OK
    }

    /** 一个可点控件（index = 遍历顺序，label = 控件文字，type = 控件类名）。 */
    public record Widget(int index, String label, String type) {
    }

    /** 匹配结果。 */
    public record Match(int index, String label, String type, List<String> candidates) {
        public boolean found() {
            return index >= 0;
        }
    }

    /** 找不到时最多回显多少个候选控件文字（够后端调规则即可，别灌爆一帧）。 */
    public static final int MAX_CANDIDATES = 24;

    private ScreenClickPlan() {
    }

    /**
     * 放行判定。
     *
     * @param screenOpen     当前是否有界面（{@code mc.gui.screen() != null}）
     * @param pauseScreen    界面自称"暂停界面"（原版 {@code Screen#isPauseScreen()} → 客户端本地菜单）
     * @param containerScreen 界面是容器界面（{@code AbstractContainerScreen}）
     * @param aiIntentOurs   该容器界面正是 AI 刚发起要开的那个（抑制界面失败时的兜底）
     */
    public static Decision decide(boolean screenOpen, boolean pauseScreen, boolean containerScreen,
                                  boolean aiIntentOurs) {
        if (!screenOpen) {
            return Decision.NO_SCREEN;
        }
        if (pauseScreen) {
            return Decision.USER_SCREEN;
        }
        if (containerScreen && !aiIntentOurs) {
            return Decision.USER_SCREEN;
        }
        return Decision.OK;
    }

    /**
     * 在候选控件里按文本找 {@code query}。
     *
     * <p>规则：先去空白 + 忽略大小写；**完全相等优先**，其次子串包含；多个命中取遍历顺序最靠前的
     * （结果稳定、可复现）。找不到时只回显候选文字（给后端调识别规则）。
     */
    public static Match match(List<Widget> widgets, String query) {
        List<String> candidates = candidates(widgets);
        String want = query == null ? "" : query.strip().toLowerCase(Locale.ROOT);
        if (want.isEmpty() || widgets == null) {
            return new Match(-1, "", "", candidates);
        }
        for (Widget w : widgets) {
            if (w == null || w.label() == null) {
                continue;
            }
            if (w.label().strip().toLowerCase(Locale.ROOT).equals(want)) {
                return new Match(w.index(), w.label(), w.type(), candidates);
            }
        }
        for (Widget w : widgets) {
            if (w == null || w.label() == null) {
                continue;
            }
            if (w.label().toLowerCase(Locale.ROOT).contains(want)) {
                return new Match(w.index(), w.label(), w.type(), candidates);
            }
        }
        return new Match(-1, "", "", candidates);
    }

    /** 候选控件文字（去重、保持遍历顺序、截断到 {@link #MAX_CANDIDATES}）。 */
    public static List<String> candidates(List<Widget> widgets) {
        List<String> out = new ArrayList<>();
        if (widgets == null) {
            return out;
        }
        for (Widget w : widgets) {
            if (w == null || w.label() == null) {
                continue;
            }
            String label = w.label().strip();
            if (label.isEmpty() || out.contains(label)) {
                continue;
            }
            out.add(label);
            if (out.size() >= MAX_CANDIDATES) {
                break;
            }
        }
        return out;
    }
}
