package com.lcu.lcumod.action;

/**
 * P0 落地水（MLG / water bucket）**决策** —— 纯逻辑、零 Minecraft 依赖，供 {@link MlgReflex}
 * 调用并由 {@code verifyMlg} 自检。
 *
 * <p>用户口径：「高阶控制：落地水/MLG（下落中右键放水再收回；需 tick 级时序 + 预判落地）」，
 * 且本架构的全局硬约束：**只在真会摔死时才动作**（宁可不动也不乱放水）。
 *
 * <h2>判据是"预计伤害"，不是"离地多高"</h2>
 * 用当前位置 + 已累计下落距离算出**总下落距离** {@code drop}，再按**原版 26.3 实证公式**算落地伤害
 * （{@link #baseFallDamage}）并叠加**庇护附魔/药水减免**（{@link Mitigation}、{@link #mitigatedFallDamage}），
 * 只有"减免后仍 {@code damage >= 当前生命值}"（**真会摔死**）才动手。
 * 时间窗由 {@link #ticksToLand} 的**重力 + 阻尼仿真**给出（不是拍脑袋的"离地 N 格"）。
 *
 * <h2>原版公式实证（NeoForge 26.3.0.57-beta / MC 26.3 客户端 jar 反汇编 + 数据包）</h2>
 * <ol>
 *   <li><b>裸伤害</b>：{@code Block#fallOn} 调 {@code Entity#causeFallDamage(落距, 1.0F, 摔落伤害源)}
 *       → {@code LivingEntity#calculateFallDamage(double,float)} =
 *       {@code floor((落距 + 1.0E-6 - Attributes.SAFE_FALL_DISTANCE) * 倍率参数 *
 *       Attributes.FALL_DAMAGE_MULTIPLIER)}（普通摔落倍率参数 = 1.0F）。
 *       属性默认值：{@code SAFE_FALL_DISTANCE = 3.0}（范围 -1024..1024）、
 *       {@code FALL_DAMAGE_MULTIPLIER = 1.0}（范围 0..100），均为同步属性。</li>
 *   <li><b>护甲不减摔落</b>：{@code fall} 伤害类型在 {@code #minecraft:bypasses_armor} 中
 *       → {@code getDamageAfterArmorAbsorb} 原样返回。</li>
 *   <li><b>附魔/药水减免</b>：{@code fall} 不在 {@code #bypasses_effects}/{@code #bypasses_enchantments}/
 *       {@code #bypasses_resistance} 中 → {@code LivingEntity#getDamageAfterMagicAbsorb} 生效：
 *       先抗性提升（每级 5/25），再用 {@code EnchantmentHelper#getDamageProtection} 的 EPF：
 *       {@code CombatRules#getDamageAfterMagicAbsorb(damage, epf) = damage * (1 - clamp(epf,0,20)/25)}。</li>
 *   <li><b>羽落</b>（{@code data/minecraft/enchantment/feather_falling.json}）：EPF =
 *       {@code 3 * 等级}（linear base 3 / per_level 3），{@code max_level = 4} → 最高 12；
 *       仅对 {@code #minecraft:is_fall} 且非 {@code #bypasses_invulnerability} 的伤害生效。</li>
 *   <li><b>保护</b>（{@code protection.json}）：每件 EPF = {@code 1 * 等级}，{@code max_level = 4}，
 *       对除 {@code #bypasses_invulnerability} 外的一切伤害生效 → 四件合计最高 16。
 *       火/爆炸/弹射保护分别要求 {@code is_fire}/{@code is_explosion}/{@code is_projectile}
 *       → **对摔落永不生效**，不建模。</li>
 *   <li><b>缓降 buff</b>：{@code LivingEntity#travel} 中着有 {@code SLOW_FALLING}（或飘浮）时每 tick
 *       调 {@code resetFallDistance()}（且重力被压到 {@code min(重力, 0.01)}）→ 摔落伤害恒为 0。</li>
 *   <li><b>跳跃提升</b>：{@code MobEffects.JUMP_BOOST} 注册了
 *       {@code SAFE_FALL_DISTANCE +1/级} 的属性修饰符（ADD_VALUE）→ 直接读属性值即已包含。</li>
 * </ol>
 *
 * <h2>未建模（如实列出，读不到/不在本反射职责内）</h2>
 * <ul>
 *   <li>着陆方块自带的落距削减（{@code Block#getFallDistanceReduction}，如干草块 0.8）——
 *       这些方块在 {@link MlgReflex} 的扫描里已被当作"安全缓冲"直接让路；</li>
 *   <li>世界外的其它 mod 对 {@code fall_damage_multiplier} 的加成（读属性值已覆盖属性途径）；
 *       其它 mod 自定义的"摔落减免"效果/附魔；</li>
 *   <li>吸收（absorption）与"不死图腾"：前者只是护盾，不改变"是否致命"；后者是死亡后触发，不在预判范围；</li>
 *   <li>落点处的方块具体形状（只按第一块有碰撞的形状顶面估算落距）。</li>
 * </ul>
 *
 * <h2>保守优先</h2>
 * <ul>
 *   <li>已有安全缓冲（水 / 垫子 / 梯子 / 蛛网 / 区块未加载 / 已落地）→ {@link Decision#NONE}，不动手；</li>
 *   <li>减免后不致命 → {@link Decision#NONE}；</li>
 *   <li>读不到物品/附魔信息（{@link Mitigation#UNREADABLE}）→ **按裸伤害判**（假阳性无害，假阴性会死人）；</li>
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
     * 原版免赔高度默认值（格）：下落 ≤ 3 格不进入伤害计算。
     * 依据：{@code Attributes.SAFE_FALL_DISTANCE} 默认值 {@code 3.0}（跳跃提升每级 +1）。
     */
    public static final double SAFE_FALL_DISTANCE_DEFAULT = 3.0;

    /** 原版摔落伤害倍率属性默认值。依据：{@code Attributes.FALL_DAMAGE_MULTIPLIER} 默认 {@code 1.0}。 */
    public static final double FALL_DAMAGE_MULTIPLIER_DEFAULT = 1.0;

    /** 原版 {@code calculateFallPower} 的防浮点误差项。依据：{@code LivingEntity#calculateFallPower} 里的 {@code 1.0E-6}。 */
    public static final double FALL_DAMAGE_EPSILON = 1.0E-6;

    /** 羽落每级 EPF。依据：{@code feather_falling.json} 的 {@code linear(base 3, per_level_above_first 3)}。 */
    public static final int FEATHER_FALLING_EPF_PER_LEVEL = 3;

    /** 羽落最高等级（→ 最高 {@code 12} EPF）。依据：{@code feather_falling.json} 的 {@code max_level = 4}。 */
    public static final int FEATHER_FALLING_MAX_LEVEL = 4;

    /** 保护每级 EPF（每件）。依据：{@code protection.json} 的 {@code linear(base 1, per_level_above_first 1)}。 */
    public static final int PROTECTION_EPF_PER_LEVEL = 1;

    /** 保护最高等级（每件）。依据：{@code protection.json} 的 {@code max_level = 4}。 */
    public static final int PROTECTION_MAX_LEVEL = 4;

    /** 盔甲位数量（头/胸/腿/脚）—— 保护四件都算。 */
    public static final int ARMOR_SLOT_COUNT = 4;

    /** EPF 上限。依据：{@code CombatRules#getDamageAfterMagicAbsorb} 里 {@code Mth.clamp(epf, 0, 20)}。 */
    public static final int EPF_CAP = 20;

    /** 每点 EPF 减免 {@code 1/25} 的伤害。依据：同上（{@code epf / 25.0F}）。 */
    public static final float EPF_DIVISOR = 25.0f;

    /** 抗性提升每级减免 {@code 5/25} 的伤害。依据：{@code LivingEntity#getDamageAfterMagicAbsorb} 的 {@code (amp+1)*5}。 */
    public static final int RESISTANCE_REDUCTION_PER_LEVEL = 5;

    /** 抗性提升的分母常量 {@code 25}（原版是 {@code 25.0F}）。 */
    public static final float RESISTANCE_DIVISOR = 25.0f;

    /** 玩家竖直重力加速度（blocks/tick²）；原版玩家 {@code 0.08}。 */
    public static final double GRAVITY = 0.08;

    /** 竖直速度每 tick 阻尼系数；原版玩家 {@code 0.98}。 */
    public static final double DRAG = 0.98;

    /**
     * 致命判定的安全余量（伤害 HP）：{@code damage >= health + margin} 才动手。
     * 取 0 表示"只要足以致死就动"；调大只会更保守（更少动手）。
     * 依据：伤害管线（裸伤害 → 抗性 → EPF）已实证建模，不需要额外放大；
     * 减免信息读不到时由 {@link Mitigation#UNREADABLE} 走"按裸伤害判"的保守分支。
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

    /**
     * 摔落减免信息（全部来自"读得到"的原始分量，便于自检与复现）。
     *
     * <p>只建模原版代码里**实证看得到**的项：安全落距/倍率属性、羽落 EPF、保护 EPF、抗性提升、缓降。
     *
     * @param readable            能否读到物品/附魔信息；<b>false → 全部减免忽略，按裸伤害判</b>（保守）
     * @param safeFallDistance    {@code Attributes.SAFE_FALL_DISTANCE} 当前值（含跳跃提升修饰符；默认 3.0）
     * @param fallDamageMultiplier {@code Attributes.FALL_DAMAGE_MULTIPLIER} 当前值（默认 1.0）
     * @param featherFallingLevel 脚部羽落等级（0..4；超范围会被 {@link #clampFeatherFallingLevel} 截断）
     * @param protectionLevelSum  四件盔甲"保护"等级合计（每件已截断到 0..4 → 0..16）
     * @param resistanceAmplifier 抗性提升 amplifier；{@code -1} = 没有该效果
     * @param slowFalling         是否有缓降（原版每 tick 清空 fallDistance → 摔落伤害恒 0）
     */
    public record Mitigation(
            boolean readable,
            double safeFallDistance,
            double fallDamageMultiplier,
            int featherFallingLevel,
            int protectionLevelSum,
            int resistanceAmplifier,
            boolean slowFalling) {

        /** 读不到物品/附魔信息时的保守取值：默认属性 + 无任何减免。 */
        public static final Mitigation UNREADABLE = new Mitigation(
                false,
                SAFE_FALL_DISTANCE_DEFAULT,
                FALL_DAMAGE_MULTIPLIER_DEFAULT,
                0, 0, -1, false);

        /** 无减免但信息可读（裸身无附魔无药水）：用于自检与"读到但什么都没有"的场景。 */
        public static final Mitigation NONE = new Mitigation(
                true,
                SAFE_FALL_DISTANCE_DEFAULT,
                FALL_DAMAGE_MULTIPLIER_DEFAULT,
                0, 0, -1, false);
    }

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
            int tickRate,
            /** 摔落减免信息；<b>null</b> 等价于 {@link Mitigation#UNREADABLE}（保守）。 */
            Mitigation mitigation) {
    }

    /** 归一后的客户端 tick 速率（≤0 / 异常 → 20）。 */
    public static int normalizeTickRate(int tickRate) {
        return tickRate <= 0 ? 20 : tickRate;
    }

    /** 附魔等级截断到合法区间 {@code [0, FEATHER_FALLING_MAX_LEVEL]}。 */
    public static int clampFeatherFallingLevel(int level) {
        return Math.max(0, Math.min(level, FEATHER_FALLING_MAX_LEVEL));
    }

    /** 附魔等级截断到合法区间 {@code [0, PROTECTION_MAX_LEVEL]}（每件）。 */
    public static int clampProtectionLevel(int level) {
        return Math.max(0, Math.min(level, PROTECTION_MAX_LEVEL));
    }

    /** 羽落 EPF：每级 3，等级上限 4 → 最高 12。 */
    public static int featherFallingEpf(int level) {
        return FEATHER_FALLING_EPF_PER_LEVEL * clampFeatherFallingLevel(level);
    }

    /** 保护 EPF：等级合计（每件已截断）× 每级 1，上限 {@code 4 件 × 4 级 = 16}。 */
    public static int protectionEpf(int protectionLevelSum) {
        int capped = Math.max(0, Math.min(protectionLevelSum, PROTECTION_MAX_LEVEL * ARMOR_SLOT_COUNT));
        return PROTECTION_EPF_PER_LEVEL * capped;
    }

    /** 总 EPF：羽落 + 保护，再按原版截断到 {@link #EPF_CAP}（20）。 */
    public static int totalEpf(int featherFallingLevel, int protectionLevelSum) {
        int sum = featherFallingEpf(featherFallingLevel) + protectionEpf(protectionLevelSum);
        return Math.max(0, Math.min(sum, EPF_CAP));
    }

    /** EPF → 伤害的换算（float，与游戏一致）。依据：{@code CombatRules#getDamageAfterMagicAbsorb} 的 {@code damage * (1 - clamp(epf,0,20)/25)}。 */
    public static float applyEpf(float damage, int epf) {
        int capped = Math.max(0, Math.min(epf, EPF_CAP));
        return damage * (1.0f - capped / EPF_DIVISOR);
    }

    /**
     * 抗性提升减免（float，与游戏一致）。
     * 依据：{@code LivingEntity#getDamageAfterMagicAbsorb} 的 {@code max(damage * (25 - 5*(amp+1)) / 25, 0)}；
     * {@code amplifier < 0} = 没有该效果 → 原样返回。
     */
    public static float applyResistance(float damage, int amplifier) {
        if (amplifier < 0) {
            return damage;
        }
        int remaining = (int) RESISTANCE_DIVISOR - RESISTANCE_REDUCTION_PER_LEVEL * (amplifier + 1);
        return Math.max(damage * remaining / RESISTANCE_DIVISOR, 0.0f);
    }

    /**
     * 裸摔落伤害（HP）—— 原版 {@code LivingEntity#calculateFallDamage(double,float)} 的等价实现。
     *
     * <p>{@code floor((drop + 1.0E-6 - safeFallDistance) * fallDamageMultiplier)}
     * （普通摔落的倍率参数恒为 1.0F，已并入倍数）。
     * 输入异常时保守退化：免赔值非有限 → 按 0 算（更疼），倍率非有限 → 按默认 1.0 算；
     * {@code drop} 非有限值返回 0（{@link #decide} 在此之前已用 NaN 检查让路）。
     */
    public static double baseFallDamage(double drop, double safeFallDistance, double fallDamageMultiplier) {
        if (!Double.isFinite(drop)) {
            return 0.0;
        }
        double safe = Double.isFinite(safeFallDistance) ? Math.max(0.0, safeFallDistance) : 0.0;
        double multiplier = Double.isFinite(fallDamageMultiplier) ? Math.max(0.0, fallDamageMultiplier) : FALL_DAMAGE_MULTIPLIER_DEFAULT;
        double power = drop + FALL_DAMAGE_EPSILON - safe;
        if (power <= 0.0) {
            return 0.0;                     // 免赔区间 / 反向 → 原版 floor 后为非正数，不造成伤害
        }
        return Math.floor(power * multiplier);
    }

    /** 裸摔落伤害（HP），用默认属性（免赔 3 格、倍率 1）。 */
    public static double damageForDrop(double drop) {
        return baseFallDamage(drop, SAFE_FALL_DISTANCE_DEFAULT, FALL_DAMAGE_MULTIPLIER_DEFAULT);
    }

    /**
     * 叠加减免后的摔落伤害（HP）。
     *
     * <p>顺序照原版 {@code getDamageAfterMagicAbsorb}：先抗性提升，再 EPF（羽落 + 保护）。
     * 减免链用 float 运算（与游戏一致），避免 double 近似在"伤害正好卡在生命值上"的一线之差里误判。
     * 缓降 → 0（原版每 tick 清空 fallDistance）。
     * {@code mitigation} 为 {@code null} 或 {@link Mitigation#readable()} 为 false → **忽略所有减免**，
     * 只按默认属性的裸伤害返回（保守：宁可按会死处理）。
     */
    public static double mitigatedFallDamage(double drop, Mitigation mitigation) {
        if (mitigation == null || !mitigation.readable()) {
            return damageForDrop(drop);
        }
        if (mitigation.slowFalling()) {
            return 0.0;
        }
        double base = baseFallDamage(drop, mitigation.safeFallDistance(), mitigation.fallDamageMultiplier());
        if (base <= 0.0) {
            return 0.0;
        }
        float damage = (float) base;
        damage = applyResistance(damage, mitigation.resistanceAmplifier());
        damage = applyEpf(damage, totalEpf(mitigation.featherFallingLevel(), mitigation.protectionLevelSum()));
        return damage;
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
     * <p>注意：有缓降 buff 时原版重力被压到 0.01，本仿真会高估下降速度；但缓降时
     * {@link #mitigatedFallDamage} 恒为 0 → {@link #decide} 在此之前就返回 {@link Decision#NONE}，
     * 不会用到该结果。
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
        double damage = mitigatedFallDamage(drop, in.mitigation());
        if (damage < in.health + FATAL_SAFETY_MARGIN_HP) {
            return Decision.NONE;                      // 减免后不足以致死 → 不动手
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
