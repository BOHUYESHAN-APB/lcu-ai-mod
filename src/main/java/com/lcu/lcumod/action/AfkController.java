package com.lcu.lcumod.action;

/**
 * 站点驻守（挂机）状态机 —— 用户原话："让 AI 去某些点位去挂机……保持在 radius 内（默认 1 格），
 * 被撞出去就自己走回"；{@code keepalive=true} 时"做轻微的防 AFK 动作（每几秒小幅转头/摆臂，
 * 不移动）"。
 *
 * <p>关键行为：
 * <ul>
 *   <li><b>漂移纠正带滞回</b>：水平偏差 > {@code radius} 才触发走回；一旦走回，要回到
 *       {@code radius × }{@link #RETURN_HYSTERESIS} 以内才停止 —— 否则会在边界上反复开关
 *       （"走一步、停一步"的抖动）。</li>
 *   <li><b>at_target</b>：水平偏差 ≤ {@code radius} 即为在站点上（用于上报，判定不掺滞回）。</li>
 *   <li><b>keepalive</b>：每 {@link #KEEPALIVE_INTERVAL_TICKS} tick 一次小幅转头/摆臂，
 *       <b>绝不写入任何移动键</b>（本类不含任何移动意图 API，从结构上保证不移动）。</li>
 * </ul>
 *
 * <p>本类刻意不依赖任何 Minecraft 类型，便于 {@code verifyRoute} 自检。
 */
public final class AfkController {

    /** 防 AFK 动作间隔：每 3 秒一次。 */
    public static final int KEEPALIVE_INTERVAL_TICKS = 60;
    /** 走回后要回到 radius 的多少比例才算停（滞回）。 */
    public static final double RETURN_HYSTERESIS = 0.5;
    /** keepalive 单次转头幅度（度）。 */
    public static final float KEEPALIVE_YAW_DELTA = 3.0f;
    /** keepalive 单次点头幅度（度）。 */
    public static final float KEEPALIVE_PITCH_DELTA = 1.5f;

    /** 回位尝试上限：用满即**诚实失败**，绝不无限重试、也绝不假装还在回位。 */
    public static final int MAX_RETURN_ATTEMPTS = 3;
    /** 两次回位尝试之间的节流（tick）。 */
    private static final int RETURN_COOLDOWN_TICKS = 20;
    /** 单次回位尝试"无进展"多久就判失败（tick）：3 秒没靠近就换下一次尝试。 */
    private static final int RETURN_PROGRESS_TIMEOUT_TICKS = 60;
    /** 进步判据：drift 至少减少这么多才算"在靠近"（抖动不算）。 */
    private static final double RETURN_PROGRESS_EPSILON = 0.25;

    /** 本 tick 的驻守动作。 */
    public enum Action { HOLD, RETURN }

    /**
     * 回位状态机的一步（由 {@code ActionExecutor} 每 tick 驱动）。
     *
     * <ul>
     *   <li>{@link #HOLD}：等待（还在导航 / 冷却中 / 尚未超时）；</li>
     *   <li>{@link #NAVIGATE}：发起一次回位导航（尝试计数 +1）；</li>
     *   <li>{@link #TIMEOUT}：本次尝试 3 秒无进展 —— 调用方停路，下一轮换新尝试；</li>
     *   <li>{@link #SETTLED}：已真的停在站点半径内 —— 回位结束；</li>
     *   <li>{@link #GIVE_UP}：尝试用满 —— 进失败态，原因见 {@link #failureReason()}。</li>
     * </ul>
     */
    public enum ReturnAction { HOLD, NAVIGATE, TIMEOUT, SETTLED, GIVE_UP }

    private StationMemory.Station station;
    private boolean keepalive;
    private boolean active;
    private int sinceTicks;
    private double drift;
    private boolean returning;
    private int pulses;
    private int lastPulseTick;
    // ── 回位重试状态（纯逻辑，可自检；不再由 ActionExecutor 的“是否在导航”清零）──
    private boolean returnStarted;
    private int returnAttempts;
    private int returnCooldown;
    private int returnDeadline;
    private double returnBestDrift;
    private boolean failed;
    private String failureReason = "";

    /**
     * 驻守（AFK）到点的**到达盒半宽**（XZ 每轴），传给
     * {@code Pathfinder.navigateTo(..., reachXz)}。
     *
     * <p>缺陷：Pathfinder 默认盒每轴 0.62 ⇒ 最差水平漂移 {@code 0.62×√2≈0.877}，大于
     * {@code StationMemory.MIN_RADIUS}（0.5）⇒ 半径 &lt; 0.88 时 Pathfinder 报"到达"，
     * 而驻守状态机眼里还在半径外 ⇒"报在回位却一步不动"。
     *
     * <p>修法（**按站点收缩**，不动全局默认盒）：取 {@code radius/√2} ⇒ 到达盒内任一点的
     * 水平漂移都 ≤ {@code radius} ⇒ Pathfinder 一报到达，{@link #settleIfWithinRadius()}
     * 就能诚实地收尾。大半径不设上限：半径 16 的站点本来就允许离得远。
     */
    public static double arrivalReachXz(double radius) {
        return radius / Math.sqrt(2.0);
    }

    /** 开始驻守。station 为 null 则拒绝（保持停用）。 */
    public boolean begin(StationMemory.Station station, boolean keepalive) {
        if (station == null) {
            return false;
        }
        this.station = station;
        this.keepalive = keepalive;
        this.active = true;
        this.sinceTicks = 0;
        this.drift = 0.0;
        this.returning = false;
        this.pulses = 0;
        this.lastPulseTick = 0;
        resetReturnCycle();
        return true;
    }

    public void stop() {
        station = null;
        keepalive = false;
        active = false;
        sinceTicks = 0;
        drift = 0.0;
        returning = false;
        pulses = 0;
        lastPulseTick = 0;
        resetReturnCycle();
    }

    /** 回位回合归零：新一次漂移越界从零额度开始（失败态也一并清掉）。 */
    private void resetReturnCycle() {
        returnStarted = false;
        returnAttempts = 0;
        returnCooldown = 0;
        returnDeadline = 0;
        returnBestDrift = 0.0;
        failed = false;
        failureReason = "";
    }

    public boolean isActive() {
        return active;
    }

    public StationMemory.Station station() {
        return station;
    }

    public String stationName() {
        return station == null ? "" : station.name();
    }

    public int sinceTicks() {
        return sinceTicks;
    }

    /** 距站点中心的水平偏差（最近一次 tick 的值）。 */
    public double drift() {
        return drift;
    }

    /** 是否在站点容差内（半径内）。 */
    public boolean atTarget() {
        return station != null && drift <= station.radius();
    }

    /** 是否正在走回站点。 */
    public boolean returning() {
        return returning;
    }

    /** 本回合已发起的回位尝试次数。 */
    public int returnAttempts() {
        return returnAttempts;
    }

    /** 回位额度用尽（进失败态）。 */
    public boolean failed() {
        return failed;
    }

    /** 失败原因（未失败为空串；失败后**非空**，供 state.afk 如实上报）。 */
    public String failureReason() {
        return failureReason;
    }

    public boolean keepaliveEnabled() {
        return keepalive;
    }

    public int pulses() {
        return pulses;
    }

    /**
     * 导航器报到达后的收尾：确实停在站点半径内就结束回位。
     *
     * <p>为什么不能只靠 {@link #tick} 的滞回（{@code radius × 0.5}）：路径节点是**格心**
     * （见 {@code Pathfinder.StandingNode.standingPos}），站点记的是玩家当时的精确位姿，
     * 两者可能差半个方块 —— 走到节点后 drift 落在 {@code (radius×0.5, radius]} 时滞回永不满足，
     * 于是"报在回位却一步不动"。这里以"物理上已经在半径内"为诚实口径收尾，
     * 并由 {@link #arrivalReachXz(double)} 保证"报到达 ⇒ 在半径内"。
     */
    public boolean settleIfWithinRadius() {
        if (!returning || station == null || drift > station.radius()) {
            return false;
        }
        returning = false;
        resetReturnCycle();
        return true;
    }

    /**
     * 回位状态机推进（纯逻辑，每 tick 调用一次，只在 {@link #returning()} 为真时有意义）。
     *
     * <p>修复的缺陷：旧实现把尝试计数放在 {@code ActionExecutor}，且"只要 Pathfinder 在导航就
     * 清零"⇒ 一旦导航永远不报到达（路径节点达不到精确站点位姿），计数永远归零、永不收尾。
     * 现在计数归 AfkController 所有：导航中**不清零**，只按"3 秒无进展"判本次失败，额度用满
     * 即 {@link ReturnAction#GIVE_UP}（失败态 + 非空原因）。
     *
     * @param navigating         Pathfinder 当前是否在走回
     * @param lastFailureReason  Pathfinder 最近一次失败原因（可为空）
     */
    public ReturnAction advanceReturn(boolean navigating, String lastFailureReason) {
        if (!returning || station == null) {
            return ReturnAction.HOLD;
        }
        if (failed) {
            return ReturnAction.GIVE_UP;
        }
        if (!navigating && drift <= station.radius()) {
            settleIfWithinRadius();
            return ReturnAction.SETTLED;
        }
        if (!returnStarted) {
            // 第一次调用：本次回位（可能是 go_afk 已经发起的导航）算作第 1 次尝试。
            returnStarted = true;
            returnAttempts = 1;
            returnCooldown = 0;
            returnBestDrift = drift;
            returnDeadline = sinceTicks + RETURN_PROGRESS_TIMEOUT_TICKS;
            return navigating ? ReturnAction.HOLD : ReturnAction.NAVIGATE;
        }
        if (navigating) {
            if (drift < returnBestDrift - RETURN_PROGRESS_EPSILON) {
                returnBestDrift = drift;
                returnDeadline = sinceTicks + RETURN_PROGRESS_TIMEOUT_TICKS;
            }
            return sinceTicks >= returnDeadline ? ReturnAction.TIMEOUT : ReturnAction.HOLD;
        }
        if (returnCooldown > 0) {
            returnCooldown--;
            return ReturnAction.HOLD;
        }
        if (returnAttempts >= MAX_RETURN_ATTEMPTS) {
            failed = true;
            failureReason = "cannot return to station: "
                    + (lastFailureReason == null || lastFailureReason.isBlank() ? "no path" : lastFailureReason);
            return ReturnAction.GIVE_UP;
        }
        returnAttempts++;
        returnCooldown = RETURN_COOLDOWN_TICKS;
        returnBestDrift = drift;
        returnDeadline = sinceTicks + RETURN_PROGRESS_TIMEOUT_TICKS;
        return ReturnAction.NAVIGATE;
    }

    /**
     * 每 tick 推进：更新 since_ticks / drift，并按**带滞回**的阈值决定是否需要走回。
     */
    public Action tick(double px, double pz) {
        if (!active || station == null) {
            return Action.HOLD;
        }
        sinceTicks++;
        drift = station.horizontalDistanceTo(px, pz);

        if (!returning && drift > station.radius()) {
            returning = true;
            resetReturnCycle();
        } else if (returning && drift <= station.radius() * RETURN_HYSTERESIS) {
            returning = false;
            resetReturnCycle();
        }
        return returning ? Action.RETURN : Action.HOLD;
    }

    /** 本 tick 是否该做一次防 AFK 动作（仅在站定且启用时）。 */
    public boolean keepaliveDue() {
        return active && keepalive && !returning
                && station != null && sinceTicks - lastPulseTick >= KEEPALIVE_INTERVAL_TICKS;
    }

    /** 登记一次脉冲（由 tick 侧在真的做了动作后调用）。 */
    public void markPulse() {
        pulses++;
        lastPulseTick = sinceTicks;
    }

    /** 第 n 次脉冲的转头偏移：左右交替的小幅值（防 AFK 只需"看起来在动"）。 */
    public float keepaliveYawDelta() {
        return (pulses % 2 == 0) ? KEEPALIVE_YAW_DELTA : -KEEPALIVE_YAW_DELTA;
    }

    public float keepalivePitchDelta() {
        return (pulses % 2 == 0) ? KEEPALIVE_PITCH_DELTA : -KEEPALIVE_PITCH_DELTA;
    }
}
