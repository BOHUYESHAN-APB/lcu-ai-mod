package com.lcu.lcumod.action;

import java.util.ArrayList;
import java.util.List;

/**
 * 路径重放状态机 —— 用户原话："逐点走，到第 i 个路径点再走第 i+1 个，直至终点；
 * 支持中途打断（新任务/玩家接管）；失败要诚实报'卡在第几个点'"。
 *
 * <p>{@code index} 语义：**当前正在前往的那个点**。到达即 {@link #advance()}；越过最后一个点
 * 即 {@link Status#DONE}。每段由上层交给 {@link Pathfinder} 单独走（段长 ≤ 抽稀步长 8 格，
 * 因此永远在原版寻路射程内）。
 *
 * <p>诚实失败：{@link #fail(String)} 会把 {@link #failIndex()} 钉在当前点上，理由原样保留，
 * 上层据此上报"卡在第 N 个点：<原因>"；{@link #abort(String)} 用于中途打断（新任务/玩家接管/
 * afk 接管），与失败区分开。
 *
 * <p>节奏（{@code speed}）：**不改 {@link Pathfinder} 的限速参数**，只影响本状态机对"每段
 * 应该多久推进完"的期望 —— speed 越大，段超时越短（更早诚实失败）。
 *
 * <p>本类刻意不依赖任何 Minecraft 类型，便于 {@code verifyRoute} 自检。
 */
public final class RouteFollower {

    public static final double DEFAULT_SPEED = 1.0;
    public static final double MIN_SPEED = 0.5;
    public static final double MAX_SPEED = 2.0;
    /** 单段允许的最长时间（按 speed=1 计；speed 越大越短）。 */
    public static final int DEFAULT_MAX_LEG_TICKS = 20 * 60;
    /** 单段允许的重试次数（寻路暂时失败可再试，超了才诚实失败）。 */
    public static final int DEFAULT_MAX_LEG_ATTEMPTS = 2;

    public enum Status { IDLE, RUNNING, DONE, FAILED, ABORTED }

    private String name = "";
    private List<RouteMemory.RoutePoint> points = List.of();
    private int index = 0;
    private Status status = Status.IDLE;
    private String failureReason = "";
    private int failIndex = -1;
    private double speed = DEFAULT_SPEED;
    private int maxLegAttempts = DEFAULT_MAX_LEG_ATTEMPTS;

    private int legTicks = 0;
    private int legAttempts = 0;

    /** 开始重放一条路径；点集为空则拒绝（保持 IDLE）。 */
    public boolean begin(String name, List<RouteMemory.RoutePoint> points, double speed) {
        if (points == null || points.isEmpty()) {
            return false;
        }
        this.name = name == null ? "" : name.strip();
        this.points = new ArrayList<>(points);
        this.index = 0;
        this.status = Status.RUNNING;
        this.failureReason = "";
        this.failIndex = -1;
        this.speed = normalizeSpeed(speed);
        this.legTicks = 0;
        this.legAttempts = 0;
        return true;
    }

    public void reset() {
        name = "";
        points = List.of();
        index = 0;
        status = Status.IDLE;
        failureReason = "";
        failIndex = -1;
        speed = DEFAULT_SPEED;
        legTicks = 0;
        legAttempts = 0;
    }

    public boolean isActive() {
        return status == Status.RUNNING;
    }

    public Status status() {
        return status;
    }

    public String name() {
        return name;
    }

    public List<RouteMemory.RoutePoint> points() {
        return new ArrayList<>(points);
    }

    public int index() {
        return index;
    }

    public int total() {
        return points.size();
    }

    /** 当前正在前往的点；不在重放中或已越过终点时为 null。 */
    public RouteMemory.RoutePoint current() {
        return isActive() && index >= 0 && index < points.size() ? points.get(index) : null;
    }

    public double speed() {
        return speed;
    }

    public int maxLegAttempts() {
        return maxLegAttempts;
    }

    public void setMaxLegAttempts(int attempts) {
        this.maxLegAttempts = Math.max(1, attempts);
    }

    /** 到达当前点 → 推进；越过最后一点即 DONE。返回是否发生了推进。 */
    public boolean advance() {
        if (!isActive()) {
            return false;
        }
        index++;
        legTicks = 0;
        legAttempts = 0;
        if (index >= points.size()) {
            status = Status.DONE;
            failureReason = "";
        }
        return true;
    }

    /** 若已到达给定位置则推进（判定与 {@link Pathfinder} 的到点判定同口径）。 */
    public boolean advanceIfReached(double x, double y, double z, double reachXZ, double reachY) {
        RouteMemory.RoutePoint target = current();
        if (target == null) {
            return false;
        }
        boolean reached = target.horizontalDistanceTo(x, z) <= reachXZ && Math.abs(target.y() - y) <= reachY;
        return reached && advance();
    }

    /** 开始新的一段（第一段 / 推进到下一个点后调用）。 */
    public void beginLeg() {
        legTicks = 0;
        legAttempts = 1;
    }

    /** 段内重试（寻路暂时失败）：重置计时并累加尝试次数。 */
    public void retryLeg() {
        legTicks = 0;
        legAttempts++;
    }

    /** 还能不能再试一次。 */
    public boolean canRetry() {
        return legAttempts < maxLegAttempts;
    }

    public int legAttempts() {
        return legAttempts;
    }

    /** 每 tick 调用（推进段计时）。 */
    public void tickLeg() {
        legTicks++;
    }

    public int legTicks() {
        return legTicks;
    }

    public boolean legTimedOut() {
        return legTicks >= maxLegTicks();
    }

    /** 本段允许的最大 tick（speed 越大越短）。 */
    public int maxLegTicks() {
        return Math.max(20, (int) (DEFAULT_MAX_LEG_TICKS / speed));
    }

    /** 无进展（原版寻路卡住）的容忍窗口：3 秒按 speed 缩放。 */
    public int stallWindowTicks() {
        return Math.max(20, (int) (60 / speed));
    }

    /** 诚实失败：钉住当前点索引与原因。 */
    public void fail(String reason) {
        if (status == Status.DONE) {
            return;
        }
        status = Status.FAILED;
        failIndex = index;
        failureReason = reason == null ? "" : reason;
    }

    /** 中途打断（新任务/玩家接管/被 afk 接管），不算失败。 */
    public void abort(String reason) {
        if (status != Status.RUNNING) {
            return;
        }
        status = Status.ABORTED;
        failureReason = reason == null ? "" : reason;
    }

    public boolean isFailed() {
        return status == Status.FAILED;
    }

    public boolean isDone() {
        return status == Status.DONE;
    }

    public int failIndex() {
        return failIndex;
    }

    public String failureReason() {
        return failureReason;
    }

    /** 0..1 的重放进度（按已到达的点数计）。 */
    public double progress() {
        if (points.isEmpty()) {
            return 0.0;
        }
        return Math.max(0.0, Math.min(1.0, index / (double) points.size()));
    }

    public static double normalizeSpeed(double speed) {
        if (Double.isNaN(speed) || speed <= 0) {
            return DEFAULT_SPEED;
        }
        return Math.max(MIN_SPEED, Math.min(MAX_SPEED, speed));
    }
}
