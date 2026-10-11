package com.lcu.lcumod.action;

/**
 * 寻路"卡住 → 重算"的**退避 + 结果去重**（纯逻辑，不依赖任何 Minecraft 类型，便于零依赖自检
 * {@code verifyWalk}）。
 *
 * <p>背景（用户实机）：卡住判定停路后，下一 tick 就重新发起**同一个目标** ⇒ 每秒多条
 * {@code Calculated path with 47 waypoints} 刷屏。修复：
 * <ul>
 *   <li><b>退避</b>：同一目标在 {@value #DEFAULT_BACKOFF_MS}ms 内**不重复计算**；</li>
 *   <li><b>去重</b>：相同"目标 + 结果"的日志/失败上报**只打一次**——与后端正在做的那批
 *       （"变了才打日志"）同一原则。</li>
 * </ul>
 *
 * <p>与后端显式指令的边界：{@code Pathfinder.navigateTo}（后端/其它控制器点名要走的命令）
 * **不走**退避——那是新指令，必须如实执行；退避只挡"卡住后自己重算同一个目标"这类抖动。
 */
public final class ReplanGate {

    public static final long DEFAULT_BACKOFF_MS = 2000L;

    private final long backoffMs;
    private String lastKey = null;
    private long lastAllowedAtMs = 0L;
    private String lastReportKey = null;

    public ReplanGate() {
        this(DEFAULT_BACKOFF_MS);
    }

    public ReplanGate(long backoffMs) {
        if (backoffMs < 0) {
            throw new IllegalArgumentException("backoffMs must be >= 0");
        }
        this.backoffMs = backoffMs;
    }

    /**
     * 是否允许现在重算 {@code key} 这个目标。
     *
     * <p>同一 key 在退避窗口内返回 false（不重复计算）；允许时记下时间，窗口从**这次允许**
     * 开始重新计。
     */
    public boolean allow(String key, long nowMs) {
        if (key == null) {
            return true;
        }
        if (key.equals(lastKey) && nowMs - lastAllowedAtMs < backoffMs) {
            return false;
        }
        lastKey = key;
        lastAllowedAtMs = nowMs;
        return true;
    }

    /** 距可以重算该目标还有多少毫秒（0 = 现在就可以）。 */
    public long remainingMs(String key, long nowMs) {
        if (key == null || !key.equals(lastKey)) {
            return 0L;
        }
        long left = backoffMs - (nowMs - lastAllowedAtMs);
        return Math.max(0L, left);
    }

    /**
     * 相同"目标 + 结果"只放行一次（用于去重日志与失败上报）。
     *
     * @return true 表示这个结果键是新的，应当打印/上报
     */
    public boolean allowOnce(String reportKey) {
        if (reportKey == null) {
            return true;
        }
        if (reportKey.equals(lastReportKey)) {
            return false;
        }
        lastReportKey = reportKey;
        return true;
    }

    /** 清空全部状态（新的导航指令到来时调用：每次导航至多上报一次相同结果）。 */
    public void reset() {
        lastKey = null;
        lastAllowedAtMs = 0L;
        lastReportKey = null;
    }
}
