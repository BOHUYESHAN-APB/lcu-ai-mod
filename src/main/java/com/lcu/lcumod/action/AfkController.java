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

    /** 本 tick 的驻守动作。 */
    public enum Action { HOLD, RETURN }

    private StationMemory.Station station;
    private boolean keepalive;
    private boolean active;
    private int sinceTicks;
    private double drift;
    private boolean returning;
    private int pulses;
    private int lastPulseTick;

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

    public boolean keepaliveEnabled() {
        return keepalive;
    }

    public int pulses() {
        return pulses;
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
        } else if (returning && drift <= station.radius() * RETURN_HYSTERESIS) {
            returning = false;
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
