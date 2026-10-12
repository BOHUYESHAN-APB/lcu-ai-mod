package com.lcu.lcumod.action;

/**
 * 移动与视角的**曲线整形**：缓入缓出、有限角加速度的角速度、按偏差降速的速度曲线、
 * 迟滞开关、前视（look-ahead）取点。
 *
 * <p><b>零依赖纯逻辑</b>：本类不引用任何 Minecraft 类型（连 {@code Vec3} 都不用，自带
 * {@link Point}）⇒ 零依赖自检 {@code verifyWalk} 可以直接对它的**行为**做断言
 * （端点、单调、有界、不抖、不越界），而不是只做源码扫描。
 *
 * <p>用户口径（原话）："移动路径还有视角转动，能不能再做平滑一点……至少不要那么锯齿"、
 * "有些时候是加速曲线，有些时候是弧形的"。所以这里每一项都是**对时间的曲线**，而不是
 * "每 tick 固定步长"的硬限速 —— 后者正是"锯齿/机械"的来源：
 * <ul>
 *   <li>{@link #easeInOut(double)}：三次缓入缓出，端点精确、单调、有界；</li>
 *   <li>{@link AngularShaper}：角速度有上限、角加速度有限 ⇒ 起步能加速、到顶能饱和、收尾能刹住，
 *       且**不越过目标**（瞄准点附近的来回抖就是"锯齿"感）；</li>
 *   <li>{@link #speedScale(double, double)}：朝向偏差大就降速（转弯走弧线），直线远距离才给满速；</li>
 *   <li>{@link Hysteresis}：开关类控制（冲刺键这类 0/1 的量）的迟滞 ⇒ 阈值附近不来回抖；</li>
 *   <li>{@link #lookAhead(Point, Point, double)}：朝**前方一段距离**取瞄准点 ⇒ 拐角走弧线，
 *       而不是"到点才急转"（那正是"像外挂"的来源）。</li>
 * </ul>
 */
public final class MotionCurve {

    private MotionCurve() {
    }

    // ── 三维点（零依赖，自带，不用 Minecraft 的 Vec3）──────────────

    /**
     * 简单的三维点（零依赖，自带，不用 Minecraft 的 {@code Vec3}）：只是为了
     * {@link #lookAhead} 能在没有 Minecraft 的进程里被直接断言。
     */
    public record Point(double x, double y, double z) {
    }

    // ── 1. 缓入缓出 ──────────────────────────────────────────────

    /**
     * 三次缓入缓出（smoothstep）：{@code t<=0 -> 0}、{@code t>=1 -> 1}、中间单调不减、值域 [0,1]。
     *
     * <p>用途：把"进度"映射成"速度/权重"，端点精确 ⇒ 起停不跳变（0 与 1 处导数为 0）。
     */
    public static double easeInOut(double t) {
        if (!(t > 0.0)) {           // 含 NaN
            return 0.0;
        }
        if (t >= 1.0) {
            return 1.0;
        }
        return t * t * (3.0 - 2.0 * t);
    }

    /** 把值夹到 [0,1]（NaN 视作 0）。 */
    public static double clamp01(double v) {
        if (!(v > 0.0)) {
            return 0.0;
        }
        return Math.min(v, 1.0);
    }

    // ── 2. 有限角加速度的角速度整形 ─────────────────────────────

    /**
     * 角速度整形器（单位：度 / tick，度 / tick²）。
     *
     * <p><b>为什么不能是"每 tick 最多转 N 度"</b>：那是恒定速率 ⇒ 起步瞬间就有最大角速度
     * （"瞬移式甩头"感），收尾又不会减速（到点前一直全速，容易在目标附近来回抖）。这里改成
     * **角速度有上限、角加速度有限**：
     * <ol>
     *   <li>制动距离律 {@code rate <= sqrt(2 * accel * |error|)} ⇒ 提前减速，逼近目标时收得住；</li>
     *   <li>速率变化每 tick 不超过 {@code accel} ⇒ 起步是"加速曲线"，不会一步到顶；</li>
     *   <li>本 tick 步长不超过剩余误差 ⇒ **绝不越过目标**（不越过就不会抖）；</li>
     *   <li>进入死区（{@link #snapDeg}）直接落到目标并把速率清零 ⇒ 真正收敛，不无限逼近。</li>
     * </ol>
     */
    public static final class AngularShaper {

        /** 默认死区（度）：小于它就直接落到目标（视觉上不可察，但保证收敛）。 */
        public static final double DEFAULT_SNAP_DEG = 0.05;

        private final double maxRate;
        private final double maxAccel;
        private final double snapDeg;
        private double rate;

        public AngularShaper(double maxRateDeg, double maxAccelDegPerTick) {
            this(maxRateDeg, maxAccelDegPerTick, DEFAULT_SNAP_DEG);
        }

        public AngularShaper(double maxRateDeg, double maxAccelDegPerTick, double snapDeg) {
            if (!(maxRateDeg > 0.0)) {
                throw new IllegalArgumentException("maxRateDeg must be > 0");
            }
            if (!(maxAccelDegPerTick > 0.0)) {
                throw new IllegalArgumentException("maxAccelDegPerTick must be > 0");
            }
            if (!(snapDeg >= 0.0)) {
                throw new IllegalArgumentException("snapDeg must be >= 0");
            }
            this.maxRate = maxRateDeg;
            this.maxAccel = maxAccelDegPerTick;
            this.snapDeg = snapDeg;
        }

        /**
         * 本 tick 允许的角位移（带符号）。
         *
         * @param error 目标 - 当前（度，已 wrap 到 (-180,180]）
         * @return 本 tick 应当施加的角位移（度，带符号）；{@code |返回值| <= max(死区, |error|)}
         */
        public double step(double error) {
            if (Double.isNaN(error)) {
                rate = 0.0;
                return 0.0;
            }
            double abs = Math.abs(error);
            if (abs <= snapDeg) {
                rate = 0.0;
                return error;                       // 死区内：精确落到目标
            }
            double dir = Math.signum(error);
            // 制动距离律：在这个速率下还能刹得住的距离 = rate²/(2*accel) <= |error| ⇒ rate <= sqrt(2*accel*|error|)
            double brakeRate = Math.sqrt(2.0 * maxAccel * abs);
            double wanted = dir * Math.min(maxRate, brakeRate);
            // 有限角加速度：速率每 tick 的变化被夹住
            double deltaRate = Math.max(-maxAccel, Math.min(maxAccel, wanted - rate));
            rate += deltaRate;
            if (Math.abs(rate) > maxRate) {
                rate = dir * maxRate;               // 数值兜底，绝不越上限
            }
            // 不越过目标
            if (Math.abs(rate) > abs) {
                return error;
            }
            return rate;
        }

        /** 当前角速度（度 / tick，带符号）。 */
        public double rate() {
            return rate;
        }

        public double maxRate() {
            return maxRate;
        }

        public double maxAccel() {
            return maxAccel;
        }

        /** 当前速率是否已经贴近目标（用于判断"收了"）。 */
        public boolean settled(double error) {
            return Math.abs(error) <= snapDeg;
        }

        public void reset() {
            rate = 0.0;
        }
    }

    // ── 3. 速度曲线（转弯降速 / 直线加速）───────────────────────

    /** 朝向偏差到这个角度就没有速度了（度）。 */
    public static final double TURN_SLOW_DEG = 45.0;
    /** 近距离（格）以下不给加速。 */
    public static final double NEAR_DISTANCE = 1.5;
    /** 远距离（格）以上给满速。 */
    public static final double FAR_DISTANCE = 4.5;
    /** 近距离基础速度系数（不为 0 ⇒ 贴近目标时也不是"0/1 突变"）。 */
    public static final double BASE_SCALE = 0.35;

    /**
     * 速度系数（0..1）：**朝向偏差大就降速**（转弯走弧线），**直线远距离才加速**。
     *
     * @param absYawDiffDeg 与路点方向的偏航差绝对值（度）
     * @param distance      到当前路点的水平距离（格）
     */
    public static double speedScale(double absYawDiffDeg, double distance) {
        double align = 1.0 - easeInOut(clamp01(Math.abs(absYawDiffDeg) / TURN_SLOW_DEG));
        double range = easeInOut(clamp01((distance - NEAR_DISTANCE) / (FAR_DISTANCE - NEAR_DISTANCE)));
        return align * (BASE_SCALE + (1.0 - BASE_SCALE) * range);
    }

    // ── 4. 迟滞开关（冲刺键这类 0/1 量）─────────────────────────

    /**
     * 迟滞开关：{@code value >= on} 才开，{@code value < off} 才关 —— 阈值附近不会来回抖。
     *
     * <p>（冲刺键是 0/1 的，无法"半按"；但对"什么时候按"必须限抖，否则速度感就是锯齿。）
     */
    public static final class Hysteresis {

        private final double on;
        private final double off;
        private boolean state;

        public Hysteresis(double on, double off) {
            if (!(off < on)) {
                throw new IllegalArgumentException("off must be < on");
            }
            this.on = on;
            this.off = off;
        }

        /** 用本 tick 的观测值更新状态，返回更新后的开关状态。 */
        public boolean update(double value) {
            if (state) {
                if (value < off) {
                    state = false;
                }
            } else if (value >= on) {
                state = true;
            }
            return state;
        }

        public boolean state() {
            return state;
        }

        public double onThreshold() {
            return on;
        }

        public double offThreshold() {
            return off;
        }

        public void reset() {
            state = false;
        }
    }

    // ── 5. 前视取点（弧线而不是急转）────────────────────────────

    /**
     * 从 {@code from} 朝 {@code to} 方向、前方 {@code ahead} 处的点。
     *
     * <p>取这样的点当视角目标 ⇒ 看向前方而不是脚下那个路点，拐弯时提前开始转 ⇒ 走**弧线**；
     * 目标本身比 {@code ahead} 还近时就瞄目标本身（不越过目标看）。
     */
    public static Point lookAhead(Point from, Point to, double ahead) {
        if (!(ahead > 0.0)) {
            return to;
        }
        double dx = to.x() - from.x();
        double dy = to.y() - from.y();
        double dz = to.z() - from.z();
        double length = Math.sqrt(dx * dx + dy * dy + dz * dz);
        if (length < 1.0e-6 || length <= ahead) {
            return to;
        }
        double scale = ahead / length;
        return new Point(from.x() + dx * scale, from.y() + dy * scale, from.z() + dz * scale);
    }

    /** 前视距离：由"到目标的距离"决定 —— 距离越远看得越远，夹在 [{@code min},{@code max}]。 */
    public static double lookAheadDistance(double distanceToGoal, double min, double max) {
        if (!(distanceToGoal > 0.0)) {
            return min;
        }
        return Math.max(min, Math.min(max, distanceToGoal * 0.35));
    }
}
