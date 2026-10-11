package com.lcu.lcumod.action;

/**
 * P0 落地水（MLG / water bucket）**决策** —— 纯逻辑、零 Minecraft 依赖，供 {@link MlgReflex}
 * 调用并由 {@code verifyMlg} 自检。
 *
 * <p>用户口径：「高阶控制：落地水/MLG（下落中右键放水再收回；需 tick 级时序 + 预判落地）」，
 * 且本架构的全局硬约束：**只在真会摔死时才动作**（宁可不动也不乱放水）。
 *
 * <h2>判据是"预计伤害"，不是"离地多高"</h2>
 * 用当前位置 + 已累计下落距离算出**总下落距离** {@code drop}，再按原版公式
 * {@code damage = ceil(drop - 3)}（{@code LivingEntity#calculateFallDamage} 的 -3 免赔）
 * 得到落地伤害；只有 {@code damage >= 当前生命值}（**真会摔死**）才动手。
 * 时间窗由 {@link #ticksToLand} 的**重力 + 阻尼仿真**给出（不是拍脑袋的"离地 N 格"）。
 *
 * <h2>保守优先</h2>
 * <ul>
 *   <li>已有安全缓冲（水 / 垫子 / 梯子 / 蛛网 / 区块未加载 / 已落地）→ {@link Decision#NONE}，不动手；</li>
 *   <li>预计不致命 → {@link Decision#NONE}；</li>
 *   <li>会摔死但离地还很远（超出反应窗口）→ 也 {@link Decision#NONE}（不抢未来的活，等近了再判）；</li>
 *   <li>会摔死、没水桶 → {@link Decision#NO_BUCKET}（如实上报，不编）；</li>
 *   <li>会摔死、但已来不及 → {@link Decision#TOO_LATE}（如实上报）。</li>
 * </ul>
 *
 * <p>所有阈值都是下面的命名常量并附依据，便于复核与自检。
 */
public final class FallPlan {

    private FallPlan() {
    }

    /** 决策结果。 */
    public enum Decision {
        /** 不危险 / 已让路 / 还没到动手的时候 —— 什么都不做。 */
        NONE,
        /** 预计会摔死，进入准备（选水桶、保持朝向），等进入放水窗口。 */
        ARM,
        /** 进入放水窗口：现在就右键放水。 */
        PLACE_WATER,
        /** 会摔死但已来不及放水（剩余 tick < {@link #MIN_PLACE_TICKS}）—— 如实上报。 */
        TOO_LATE,
        /** 会摔死但手上/背包没有水桶 —— 如实上报 unavailable，绝不编造动作。 */
        NO_BUCKET
    }

    // ── 常量（含依据） ──

    /**
     * 原版免赔高度：下落 ≤ 3 格不掉血。
     * 依据：1.8 起 {@code LivingEntity#calculateFallDamage} = {@code ceil(fallDistance - 3.0)}。
     */
    public static final double FALL_DAMAGE_FREE_DROP = 3.0;

    /** 玩家竖直重力加速度（blocks/tick²）；原版玩家 {@code 0.08}。 */
    public static final double GRAVITY = 0.08;

    /** 竖直速度每 tick 阻尼系数；原版玩家 {@code 0.98}。 */
    public static final double DRAG = 0.98;

    /**
     * 致命判定的安全余量（伤害 HP）：{@code damage >= health + margin} 才动手。
     * 取 0 表示"只要足以致死就动"；调大只会更保守（更少动手）。
     * 依据：摔落伤害无视护甲，只有 Feather Falling 会减伤——本决策不建模附魔，故宁可判轻。
     */
    public static final double FATAL_SAFETY_MARGIN_HP = 0.0;

    /**
     * 开始准备（选水桶）的反应窗口（tick）：落地前 ≤ 该 tick 数才 arm。
     * 依据：切槽生效 2 tick + 保持朝向下看若干 tick；再早就是"抢未来的活"→ 不做。
     */
    public static final int ARM_LEAD_TICKS = 12;

    /**
     * 放水提前量（tick）基准：落地前 ≤ 该值才真正右键。
     * 依据：切槽生效(2) + 按键→useItem 生效(1) + 服务端往返余量(1)。
     */
    public static final int PLACE_LEAD_TICKS = 4;

    /** 放水至少要在落地前剩下的 tick 数：少于它已经来不及 → {@link Decision#TOO_LATE}。 */
    public static final int MIN_PLACE_TICKS = 2;

    /** 水平速度对放水提前量的加成（tick 每 (格/tick)）：跑得越快越早放，覆盖落点不确定性。 */
    public static final double HORIZONTAL_LEAD_PER_SPEED = 0.5;

    /** 水平速度加成上限（tick），避免高速时提前太多。 */
    public static final int HORIZONTAL_LEAD_MAX = 4;

    /** 下落仿真上限（tick）：防极端输入死循环（400 tick = 20s，足够从世界顶掉到基岩）。 */
    public static final int MAX_SIM_TICKS = 400;

    /** 决策输入。全部为原始分量，便于自检与复现。 */
    public record Input(
            /** 当前脚底 Y（格）。 */
            double y,
            /** 当前竖直速度（格/tick，向下为负）。 */
            double velY,
            /** 当前水平速度（格/tick）。 */
            double horizontalSpeed,
            /** 预测的落地地面顶面 Y（<b>NaN</b> 表示未知 → 保守不动）。 */
            double groundY,
            /** 到此刻为止已累计的下落距离（{@code Entity.fallDistance}）。 */
            double fallDistanceSoFar,
            /** 当前生命值（HP）。 */
            double health,
            /** 手上/背包是否有水桶。 */
            boolean hasWaterBucket,
            /** 是否已有安全缓冲（在水里 / 垫子 / 梯子 / 蛛网 / 区块未加载 / 已落地）。 */
            boolean hasBuffer,
            /** MLG 反射是否开启（默认开）。关闭 → 一律不动手。 */
            boolean enabled,
            /** 客户端 tick 速率（ticks/sec），仅用于合理性归一。 */
            int tickRate) {
    }

    /** 归一后的客户端 tick 速率（≤0 / 异常 → 20）。 */
    public static int normalizeTickRate(int tickRate) {
        return tickRate <= 0 ? 20 : tickRate;
    }

    /** 总下落距离 → 原版落地伤害（HP）。 */
    public static double damageForDrop(double drop) {
        if (Double.isNaN(drop) || drop <= FALL_DAMAGE_FREE_DROP) {
            return 0.0;
        }
        return Math.ceil(drop - FALL_DAMAGE_FREE_DROP);
    }

    /** 总下落距离：已累计的下落距离 + 当前位置到地面顶面的剩余高度（只会 ≥0）。 */
    public static double predictedDrop(double y, double groundY, double fallDistanceSoFar) {
        return fallDistanceSoFar + Math.max(0.0, y - groundY);
    }

    /**
     * 放水提前量（tick）：水平速度越快越早（落点不确定性更大），但有上限。
     * 水平速度 0 → {@link #PLACE_LEAD_TICKS}。
     */
    public static int placeLeadTicks(double horizontalSpeed) {
        if (Double.isNaN(horizontalSpeed)) {
            horizontalSpeed = 0.0;
        }
        int extra = (int) Math.round(Math.max(0.0, horizontalSpeed) * HORIZONTAL_LEAD_PER_SPEED);
        return PLACE_LEAD_TICKS + Math.min(HORIZONTAL_LEAD_MAX, extra);
    }

    /**
     * 从当前竖直状态仿真到脚底触地所需 tick 数（重力 + 阻尼，原版玩家物理）。
     *
     * @return 触地所需 tick 数；已在地面/地面以下 → 0；仿真超 {@link #MAX_SIM_TICKS} 仍不触地
     *         （或输入为 NaN）→ -1（视为"算不出"，保守不动）。
     */
    public static int ticksToLand(double y, double velY, double groundY) {
        if (Double.isNaN(y) || Double.isNaN(velY) || Double.isNaN(groundY)) {
            return -1;
        }
        double cy = y;
        double cv = velY;
        for (int t = 0; t <= MAX_SIM_TICKS; t++) {
            if (cy <= groundY) {
                return t;
            }
            cv = (cv - GRAVITY) * DRAG;
            cy += cv;
        }
        return -1;
    }

    /** 决策主入口。 */
    public static Decision decide(Input in) {
        if (in == null || !in.enabled) {
            return Decision.NONE;                      // 关掉 → 一律不动手
        }
        if (in.hasBuffer) {
            return Decision.NONE;                      // 已有安全缓冲 → 让路
        }
        if (in.health <= 0.0) {
            return Decision.NONE;                      // 已死 → 不折腾
        }
        double drop = predictedDrop(in.y, in.groundY, in.fallDistanceSoFar);
        if (Double.isNaN(drop)) {
            return Decision.NONE;                      // 地面预测未知 → 保守不动
        }
        double damage = damageForDrop(drop);
        if (damage < in.health + FATAL_SAFETY_MARGIN_HP) {
            return Decision.NONE;                      // 预计伤害不足以致死 → 不动手
        }
        if (!in.hasWaterBucket) {
            return Decision.NO_BUCKET;                 // 真会死但没水桶 → 如实上报
        }
        int ticks = ticksToLand(in.y, in.velY, in.groundY);
        if (ticks < 0) {
            return Decision.NONE;                      // 算不出触地时间 → 保守不动
        }
        if (ticks < MIN_PLACE_TICKS) {
            return Decision.TOO_LATE;                  // 已来不及，如实上报
        }
        if (ticks <= placeLeadTicks(in.horizontalSpeed)) {
            return Decision.PLACE_WATER;               // 进入放水窗口
        }
        if (ticks <= ARM_LEAD_TICKS) {
            return Decision.ARM;                       // 会死且已进入反应窗口 → 开始准备
        }
        return Decision.NONE;                          // 会死但还远 → 不抢未来的活，等近了再判
    }
}
