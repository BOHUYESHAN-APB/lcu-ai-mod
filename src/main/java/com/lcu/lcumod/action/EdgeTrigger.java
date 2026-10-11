package com.lcu.lcumod.action;

/**
 * 边沿触发计数：把"按住"与"点按"两种语义统一到一个可测的小状态机。
 *
 * <p>为什么需要它：控制流消息是 20-50Hz 的 latest-wins，**可能比客户端 tick 更快**。
 * 如果只存一个 boolean（"当前是否要求攻击"），一次"按下又松开"若正好落在两次 tick
 * 之间就会被整个吞掉——离散意图丢失。这里在 false→true 的**边沿**上计一次待处理，
 * 由 tick 侧在游戏内冷却允许时逐个消费，因此：
 * <ul>
 *   <li>按住（{@link #isHeld()}）不需要计数，每 tick 直接判冷却即可；</li>
 *   <li>点按（tap）落在 tick 之间也不会丢，会在冷却允许时补一次。</li>
 * </ul>
 *
 * <p>线程安全：{@code update} 由 wire 读线程调用，{@code shouldFire} 由渲染线程调用，
 * 故所有方法 {@code synchronized}。
 *
 * <p>本类刻意不依赖任何 Minecraft 类型，便于单元测试。
 */
public final class EdgeTrigger {

    /** 待处理点按的上限：防止后端疯狂发边沿时无限堆积（最多补 {@code maxPending} 次）。 */
    private final int maxPending;

    private int pending;
    private boolean held;

    public EdgeTrigger(int maxPending) {
        if (maxPending < 1) {
            throw new IllegalArgumentException("maxPending must be >= 1");
        }
        this.maxPending = maxPending;
    }

    /** 收到一帧意图。只有 false→true 的边沿才计一次待处理（按住的流不会堆积）。 */
    public synchronized void update(boolean want) {
        if (want && !held) {
            pending = Math.min(pending + 1, maxPending);
        }
        held = want;
    }

    /** 当前是否处于"按住"。 */
    public synchronized boolean isHeld() {
        return held;
    }

    /** 还有多少次待处理的点按。 */
    public synchronized int pending() {
        return pending;
    }

    /**
     * 本 tick 是否应当触发一次。
     *
     * @param ready 外部条件（如游戏内攻击冷却是否就绪）。为 false 时**不消费**待处理，
     *              点按会被保留到下一次就绪，避免"冷却中丢掉一次点按"。
     */
    public synchronized boolean shouldFire(boolean ready) {
        if (!ready) {
            return false;
        }
        if (pending > 0) {
            pending--;
            return true;
        }
        return held;
    }

    /** 松开并清空待处理（断线 / 超时 / 收尾时必须调用，否则意图会残留）。 */
    public synchronized void reset() {
        pending = 0;
        held = false;
    }
}
