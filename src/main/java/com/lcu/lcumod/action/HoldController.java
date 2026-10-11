package com.lcu.lcumod.action;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.function.BooleanSupplier;

/**
 * 一等公民的“按住”语义 —— 解决“只有按一下、吃不到也挖不动”的根因。
 *
 * <p>背景：控制流消息是 latest-wins 的**离散意图**（{@link EdgeTrigger}），适合点按；
 * 但原版里“吃食物要按住右键约 1.6s（32 tick）”“挖掘要按住左键直到方块破坏”都是
 * **持续按住**的语义，靠调用方每个 tick 反复发 {@code set_control_state} 既容易漏、
 * 也会被“新任务/玩家接管”打断而粘键。这里把“按住 N tick、到期自动松开”做成一等公民：
 * <ul>
 *   <li>{@link #hold(String, int)}：从当前 tick 起按住 N tick，到期**必然**松开（宁早松不粘键）；</li>
 *   <li>{@link #holdUntil(String, BooleanSupplier, int)}：每 tick 复查条件，条件不满足或超时即松开，
 *       并在 {@link Handle#releaseReason()} 里留下**可验证的原因**，供上层诚实上报成功/失败。</li>
 * </ul>
 *
 * <p>安全（由 {@link #tick} 的统一入口保证）：AI 控制关闭 / 玩家接管 / 低层反射接管
 * 任一发生，**当 tick 立即释放所有 hold**，绝不粘键。
 *
 * <p>纯逻辑、零 Minecraft 依赖，线程安全（{@code synchronized}），便于 {@code verifyHold} 自检。
 */
public final class HoldController {

    /** hold 的结束原因。{@link #code()} 给出稳定的线上字符串。 */
    public enum Release {
        /** 仍在按住。 */
        NONE,
        /** 纯时长 hold 到期。 */
        EXPIRED,
        /** 条件 hold 的目标条件达成（不再需要按住）。 */
        CONDITION_MET,
        /** 条件 hold 到达最大 tick 仍未达成。 */
        TIMEOUT,
        /** AI 控制被关闭。 */
        AI_DISABLED,
        /** 玩家接管（鼠标被抓 / 玩家输入）。 */
        PLAYER_TAKEOVER,
        /** 低层反射接管（ReflexGate 覆盖）。 */
        REFLEX_OVERRIDE,
        /** 收到新任务命令。 */
        NEW_TASK,
        /** 同一控制被新的 hold 顶替。 */
        REPLACED,
        /** 显式释放（stop_all / 断线 / 收尾）。 */
        MANUAL;

        public String code() {
            return name().toLowerCase(Locale.ROOT);
        }
    }

    /** 一次 tick 的结果：本 tick 需要写入的控制 + 刚刚松开的 handle。 */
    public record TickResult(List<String> applied, List<Handle> released) {
        public boolean changed() {
            return !released.isEmpty();
        }
    }

    /**
     * 单个 hold 的句柄。上层保存它，等 {@link #isActive()} 变 false 后读
     * {@link #releaseReason()} 得到可验证的原因。
     */
    public static final class Handle {
        private final String control;
        private final BooleanSupplier stillWanted; // null = 纯时长 hold
        private int remaining;
        private boolean active = true;
        private Release reason = Release.NONE;

        private Handle(String control, BooleanSupplier stillWanted, int ticks) {
            this.control = control;
            this.stillWanted = stillWanted;
            this.remaining = ticks;
        }

        public String control() {
            return control;
        }

        public boolean isActive() {
            return active;
        }

        public Release releaseReason() {
            return reason;
        }

        public String releaseCode() {
            return reason.code();
        }

        public int remainingTicks() {
            return remaining;
        }

        private void release(Release r) {
            active = false;
            reason = r;
        }
    }

    private final Map<String, Handle> holds = new LinkedHashMap<>();

    /** 按住 {@code control} {@code ticks} tick；tick<=0 视为 1（至少按住一次）。 */
    public synchronized Handle hold(String control, int ticks) {
        return install(control, new Handle(control, null, Math.max(1, ticks)));
    }

    /**
     * 按住 {@code control} 直到 {@code stillWanted} 返回 false 或超过 {@code maxTicks}。
     *
     * @param stillWanted 每 tick 复查：true = 继续按住，false = 目标达成、立即松开。
     * @param maxTicks    上限（防止条件永不满足时粘键）；tick<=0 视为 1。
     */
    public synchronized Handle holdUntil(String control, BooleanSupplier stillWanted, int maxTicks) {
        return install(control, new Handle(control, stillWanted, Math.max(1, maxTicks)));
    }

    private Handle install(String control, Handle h) {
        if (control == null || control.isBlank()) {
            h.release(Release.MANUAL);
            return h;
        }
        Handle prev = holds.get(control);
        if (prev != null && prev != h && prev.isActive()) {
            prev.release(Release.REPLACED);
        }
        holds.put(control, h);
        return h;
    }

    /** 立即释放全部 hold（返回被释放的 handle，便于调用方收尾上报）。 */
    public synchronized List<Handle> releaseAll(Release reason) {
        List<Handle> out = new ArrayList<>();
        for (Handle h : holds.values()) {
            if (h.isActive()) {
                h.release(reason);
                out.add(h);
            }
        }
        holds.clear();
        return out;
    }

    /** 立即释放单个控制上的 hold（不存在或已释放则返回 null）。 */
    public synchronized Handle release(String control, Release reason) {
        Handle h = holds.remove(control);
        if (h != null && h.isActive()) {
            h.release(reason);
            return h;
        }
        return null;
    }

    public synchronized boolean isHeld(String control) {
        Handle h = holds.get(control);
        return h != null && h.isActive();
    }

    public synchronized Handle get(String control) {
        return holds.get(control);
    }

    public synchronized int activeCount() {
        int n = 0;
        for (Handle h : holds.values()) {
            if (h.isActive()) n++;
        }
        return n;
    }

    public synchronized boolean isEmpty() {
        return holds.isEmpty();
    }

    /**
     * 推进一个游戏 tick。安全标志优先：任一为真 → 立即释放全部 hold。
     * 否则：条件 hold 先复查（条件不满足 → CONDITION_MET 松开，本 tick 不写输入），
     * 仍需要的 hold 计入 {@code applied} 并递减剩余 tick，递减到 0 时松开
     * （纯时长 → EXPIRED，条件 hold → TIMEOUT）。
     *
     * @param aiActive        AI 是否仍在控制
     * @param playerTakeover  玩家是否接管（鼠标被抓 / 玩家输入）
     * @param reflexOverride  低层反射是否接管（ReflexGate 覆盖）
     */
    public synchronized TickResult tick(boolean aiActive, boolean playerTakeover, boolean reflexOverride) {
        if (!aiActive || playerTakeover || reflexOverride) {
            Release r = !aiActive ? Release.AI_DISABLED
                    : playerTakeover ? Release.PLAYER_TAKEOVER
                    : Release.REFLEX_OVERRIDE;
            return new TickResult(List.of(), releaseAll(r));
        }

        List<String> applied = new ArrayList<>();
        List<Handle> released = new ArrayList<>();

        for (Handle h : holds.values()) {
            if (!h.isActive()) continue;
            if (h.stillWanted != null && !h.stillWanted.getAsBoolean()) {
                h.release(Release.CONDITION_MET);
                released.add(h);
                continue;
            }
            applied.add(h.control);
        }

        for (String control : applied) {
            Handle h = holds.get(control);
            if (h == null || !h.isActive()) continue;
            if (--h.remaining <= 0) {
                h.release(h.stillWanted == null ? Release.EXPIRED : Release.TIMEOUT);
                released.add(h);
            }
        }

        holds.values().removeIf(h -> !h.isActive());
        return new TickResult(List.copyOf(applied), released);
    }
}
