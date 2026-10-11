package com.lcu.lcumod.action;

import java.util.ArrayList;
import java.util.List;
import java.util.Locale;

/**
 * {@code place_block} 的**物品解析**（纯逻辑，可自检）。
 *
 * <p>用户口径：方块选择**不由本地决定** —— 「把手里/背包有什么（id + 数量）如实给后端，
 * 让后端大模型决定用哪种方块；Java 只执行『放这个物品到这个位置』。若 {@code place_block}
 * 收到一个**不在背包里**的物品 → 如实失败（**别换别的**）」。
 *
 * <p>因此这里只做一件事：把后端点名的 {@code item} 对上背包里真实存在的那一格；对不上就是
 * 失败，**绝不回落到"手边随便一个方块"**。
 *
 * <p>物品 id 比对：大小写不敏感、命名空间可省（{@code minecraft:oak_planks} ≡ {@code oak_planks}）。
 *
 * <p>本类零依赖，可被 {@code verifyGuard} 直接自检。
 */
public final class ItemPick {

    /** 背包一格（id 为注册名，如 {@code minecraft:oak_planks}）。 */
    public record Slot(int index, String id, int count) {
    }

    /** 解析结果。 */
    public enum Outcome {
        /** 后端没点名物品 → 用当前手持（保持原有行为）。 */
        NOT_REQUESTED,
        /** 点名物品就在主手 → 直接可用。 */
        IN_HAND,
        /** 点名物品在快捷栏（其它格）→ 选中它。 */
        SELECT_HOTBAR,
        /** 点名物品在背包主区 → 需要与当前快捷栏格交换（本地只做搬运，不换物品）。 */
        SWAP_INTO_HOTBAR,
        /** 背包里根本没有这个物品 → **如实失败**。 */
        NOT_FOUND
    }

    /**
     * @param outcome 解析结论
     * @param slot    背包索引（{@link Outcome#SELECT_HOTBAR} / {@link Outcome#SWAP_INTO_HOTBAR} 时有意义）
     * @param id      归一化后的请求 id（{@link Outcome#NOT_FOUND} 时回显，便于后端自查拼写）
     */
    public record Result(Outcome outcome, int slot, String id) {
        public boolean ok() {
            return outcome != Outcome.NOT_FOUND;
        }
    }

    /** 快捷栏格数（原版固定 9）。 */
    public static final int HOTBAR_SIZE = 9;

    private ItemPick() {
    }

    /**
     * 解析后端点名的物品。
     *
     * @param requested     后端给的物品 id（空/null = 没点名）
     * @param slots         背包内容（索引 0-8 = 快捷栏，9-35 = 主区）
     * @param heldId        当前主手物品 id（可为 null）
     * @param selectedSlot  当前选中的快捷栏格（0-8），用于判断"已经在手"
     */
    public static Result resolve(String requested, List<Slot> slots, String heldId, int selectedSlot) {
        String want = normalize(requested);
        if (want.isEmpty()) {
            return new Result(Outcome.NOT_REQUESTED, -1, "");
        }
        if (want.equals(normalize(heldId))) {
            return new Result(Outcome.IN_HAND, selectedSlot, want);
        }
        List<Integer> hits = new ArrayList<>();
        if (slots != null) {
            for (Slot s : slots) {
                if (s == null || s.count() <= 0) {
                    continue;
                }
                if (want.equals(normalize(s.id()))) {
                    hits.add(s.index());
                }
            }
        }
        if (hits.isEmpty()) {
            return new Result(Outcome.NOT_FOUND, -1, want);
        }
        int best = hits.get(0);
        for (int idx : hits) {
            if (idx >= 0 && idx < HOTBAR_SIZE) {
                best = idx;
                break;
            }
        }
        if (best >= 0 && best < HOTBAR_SIZE) {
            return new Result(Outcome.SELECT_HOTBAR, best, want);
        }
        return new Result(Outcome.SWAP_INTO_HOTBAR, best, want);
    }

    /** 大小写不敏感 + 命名空间可省。 */
    public static String normalize(String itemId) {
        if (itemId == null) {
            return "";
        }
        String v = itemId.strip().toLowerCase(Locale.ROOT);
        if (v.startsWith("minecraft:")) {
            v = v.substring("minecraft:".length());
        }
        return v;
    }
}
