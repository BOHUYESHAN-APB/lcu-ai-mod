package com.lcu.lcumod.action;

import java.util.ArrayList;
import java.util.List;

/**
 * 进食状态机（纯逻辑，零 Minecraft 依赖）——把“吃到没有 / 幂等 / 超时”判定从
 * {@code ActionExecutor} 里抽出来，便于 {@code verifyHold} 自检。
 *
 * <p>为什么单独成类：旧实现只在“按一下 use”后靠 {@code isUsingItem()} 猜，猜不到就回
 * “eat failed or cannot start use animation”这类**猜测文案**；而原版吃食物需要**按住右键
 * 约 32 tick**。这里只负责可验证的事实：
 * <ul>
 *   <li>食物生效判据（三选一）：饱食度 ↑ / 饱和度 ↑ / 该槽数量 −；</li>
 *   <li>{@code MAX_TICKS} 内未生效 → {@link Outcome#TIMEOUT}；</li>
 *   <li>重复调用**幂等**：已在吃就复用同一状态（不重置计时、不重复切槽/按键），
 *       新请求作为 waiter 记下，结束时统一回执。</li>
 * </ul>
 */
public final class EatController {

    public enum Outcome {
        /** 仍在进行。 */
        NONE,
        /** 食物已生效。 */
        SUCCESS,
        /** 超时未生效。 */
        TIMEOUT
    }

    /** 切槽后等待选中生效的 tick 数（客户端 setSelectedSlot 需要一帧下发）。 */
    public static final int SELECT_TICKS = 2;
    /** 进食总超时（tick）。原版吃食物约 32 tick，40 给足余量。 */
    public static final int MAX_TICKS = 40;

    private boolean active;
    private boolean reflex;
    private int ticks;
    private int slot = -1;
    private int startHunger;
    private float startSaturation;
    private int startCount;
    private boolean useStarted;
    private Outcome outcome = Outcome.NONE;
    private final List<String> waiters = new ArrayList<>();

    /**
     * 开始或加入一次进食。
     *
     * @return true = 本次调用**新启动**了进食（调用方需切槽、稍后按住 use）；
     *         false = 已在进食（幂等复用：不重置计时/槽位，仅登记 waiter）。
     */
    public synchronized boolean begin(String reqId, boolean reflex, int slot,
                                      int hunger, float saturation, int count) {
        if (active) {
            if (reqId != null) waiters.add(reqId);
            return false;
        }
        active = true;
        this.reflex = reflex;
        ticks = 0;
        this.slot = slot;
        startHunger = hunger;
        startSaturation = saturation;
        startCount = count;
        useStarted = false;
        outcome = Outcome.NONE;
        if (reqId != null) waiters.add(reqId);
        return true;
    }

    /** 幂等接单：已经在吃时把新请求登记为 waiter（不重置状态）。 */
    public synchronized void addWaiter(String reqId) {
        if (reqId != null) waiters.add(reqId);
    }

    /** 推进一个 tick，返回结果。 */
    public synchronized Outcome tick(int hunger, float saturation, int count) {
        if (!active) {
            return Outcome.NONE;
        }
        ticks++;
        boolean tookEffect = hunger > startHunger || saturation > startSaturation || count < startCount;
        if (tookEffect) {
            finish(Outcome.SUCCESS);
            return Outcome.SUCCESS;
        }
        if (ticks >= MAX_TICKS) {
            finish(Outcome.TIMEOUT);
            return Outcome.TIMEOUT;
        }
        return Outcome.NONE;
    }

    /** 是否已过“等选中生效”的窗口、该按住 use 了。 */
    public synchronized boolean shouldStartUse() {
        return active && !useStarted && ticks >= SELECT_TICKS;
    }

    public synchronized void markUseStarted() {
        useStarted = true;
    }

    public synchronized boolean isActive() {
        return active;
    }

    /** 是否由**本地饥饿反射**发起（用于状态上报 reflex_eat）。 */
    public synchronized boolean isReflex() {
        return active && reflex;
    }

    public synchronized int slot() {
        return slot;
    }

    public synchronized int ticks() {
        return ticks;
    }

    public synchronized Outcome lastOutcome() {
        return outcome;
    }

    /** 取出并清空所有等待回执的请求 id（含首次发起者）。 */
    public synchronized List<String> drainWaiters() {
        List<String> copy = List.copyOf(waiters);
        waiters.clear();
        return copy;
    }

    /** 强制复位（stop_all / 新任务 / 玩家接管）。 */
    public synchronized void reset() {
        active = false;
        reflex = false;
        ticks = 0;
        slot = -1;
        useStarted = false;
        outcome = Outcome.NONE;
        waiters.clear();
    }

    private void finish(Outcome o) {
        outcome = o;
        active = false;
    }
}
