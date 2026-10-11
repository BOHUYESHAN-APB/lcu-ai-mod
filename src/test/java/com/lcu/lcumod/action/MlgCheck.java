package com.lcu.lcumod.action;

/**
 * 零依赖自检：落地水（MLG）决策 {@link FallPlan} + 反射 {@link MlgReflex} 的稳定接口。
 *
 * <p>照现有 verify* 的写法：纯 Java + JavaExec（见 build.gradle 的 {@code verifyMlg} 任务），
 * 不引入任何依赖，随 {@code build} / {@code check} 一起跑。断言信息用 ASCII（Windows 控制台 GBK）。
 *
 * <p>覆盖用户点名的契约：
 * <ol>
 *   <li>决策表：危险/不危险的**边界**、{@code too_late}、{@code no_bucket}；</li>
 *   <li>判据是"预计伤害 ≥ 生命值"，不是"离地多高"（同样 10 格：满血不动、残血才动）；</li>
 *   <li>反应窗口（tick 数预算）落在合理区间；</li>
 *   <li>让路路径：关掉 / 已有安全缓冲 / 地面未知 → {@code none}；</li>
 *   <li>回执枚举稳定（receipt 字符串 + phase 枚举）；</li>
 *   <li>附魔/药水减免：羽落（每级 3 EPF，上限 4 级）、保护（每件每级 1 EPF，四件上限 16）、
 *       EPF 总上限 20、抗性提升、缓降；读不到物品信息时**按裸伤害判**（保守）。</li>
 * </ol>
 */
public final class MlgCheck {

    private static int passed = 0;
    private static int failed = 0;

    private MlgCheck() {
    }

    public static void main(String[] args) {
        notDangerousIsNone();
        notFatalIsNone();
        fatalWithoutBucketIsNoBucket();
        tooLateIsReported();
        placeWindowTriggersWater();
        armWindowPrepares();
        farDangerDoesNotAct();
        fatalityUsesHealthNotHeight();
        buffersAndUnknownGroundForbidAction();
        disabledNeverActs();
        reactionWindowSane();
        tickSimulationSane();
        receiptEnumStable();
        baseDamageMatchesVanillaFormula();
        featherFallingReducesDamage();
        protectionReducesDamage();
        mitigationSaturatesAtEpfCap();
        resistanceAndSlowFallingModeled();
        unreadableMitigationStaysConservative();
        enchantBoundaryDecidesWaterOrNot();

        System.out.println("[MlgCheck] " + passed + " checks passed, " + failed + " failed");
        if (failed > 0) {
            System.exit(1);
        }
    }

    private static FallPlan.Input in(double y, double vy, double horiz, double groundY,
                                     double fell, double health, boolean bucket) {
        return in(y, vy, horiz, groundY, fell, health, bucket, FallPlan.Mitigation.NONE);
    }

    private static FallPlan.Input in(double y, double vy, double horiz, double groundY,
                                     double fell, double health, boolean bucket,
                                     FallPlan.Mitigation mitigation) {
        return new FallPlan.Input(y, vy, horiz, groundY, fell, health, bucket, false, true, 20, mitigation);
    }

    /** float 近似相等（减免链是 float 运算，与游戏逐位一致，比较留 1e-4 余量）。 */
    private static boolean close(double actual, double expected) {
        return Math.abs(actual - expected) < 1e-4;
    }

    /** ① 不危险：下落 ≤ 3 格（原版免赔）→ none。 */
    private static void notDangerousIsNone() {
        check("drop<=3 is none", FallPlan.decide(in(2.0, -0.5, 0, 0, 0, 20, true)) == FallPlan.Decision.NONE);
        check("exactly 3-block drop is none", FallPlan.decide(in(3.0, -0.5, 0, 0, 0, 20, true)) == FallPlan.Decision.NONE);
    }

    /** ① 不致命：10 格 = 7 伤害 < 满血 20 → none（哪怕有水桶）。 */
    private static void notFatalIsNone() {
        check("10-block drop at full health is none",
                FallPlan.decide(in(10.0, -0.5, 0, 0, 0, 20, true)) == FallPlan.Decision.NONE);
    }

    /** ① 会摔死但没水桶 → no_bucket（如实上报，绝不编造动作）。 */
    private static void fatalWithoutBucketIsNoBucket() {
        check("fatal 100-block drop without bucket is no_bucket",
                FallPlan.decide(in(100.0, 0, 0, 0, 0, 20, false)) == FallPlan.Decision.NO_BUCKET);
    }

    /** ① 会摔死、来不及（剩余 < MIN_PLACE_TICKS）→ too_late。 */
    private static void tooLateIsReported() {
        // 已累计 30 格（必死），但只剩 1 tick 就触地
        FallPlan.Input p = in(0.2, -1.0, 0, 0, 30, 20, true);
        check("too_late when < MIN_PLACE_TICKS left",
                FallPlan.decide(p) == FallPlan.Decision.TOO_LATE);
    }

    /** ① 会摔死且进入放水窗口 → place_water。 */
    private static void placeWindowTriggersWater() {
        // 1.0 格高、初速 -0.5 → 2 tick 触地（在放水窗口内）；已累计 30 格保证致命
        FallPlan.Input p = in(1.0, -0.5, 0, 0, 30, 20, true);
        int ticks = FallPlan.ticksToLand(p.y(), p.velY(), p.groundY());
        check("scenario lands within place window (ticks=" + ticks + ")",
                ticks >= FallPlan.MIN_PLACE_TICKS
                        && ticks <= FallPlan.placeLeadTicks(p.horizontalSpeed()));
        check("place window triggers place_water", FallPlan.decide(p) == FallPlan.Decision.PLACE_WATER);
    }

    /** ① 会摔死、在反应窗口但未到放水点 → arm。 */
    private static void armWindowPrepares() {
        FallPlan.Input p = in(3.5, -0.5, 0, 0, 30, 20, true);
        int ticks = FallPlan.ticksToLand(p.y(), p.velY(), p.groundY());
        check("arm scenario ticks in (lead, ARM_LEAD] (ticks=" + ticks + ")",
                ticks > FallPlan.placeLeadTicks(p.horizontalSpeed()) && ticks <= FallPlan.ARM_LEAD_TICKS);
        check("arm window returns arm", FallPlan.decide(p) == FallPlan.Decision.ARM);
    }

    /** 保守：会摔死但还很远 → none（不抢未来的活）。 */
    private static void farDangerDoesNotAct() {
        check("fatal but far away is none (does not act early)",
                FallPlan.decide(in(100.0, 0, 0, 0, 0, 20, true)) == FallPlan.Decision.NONE);
    }

    /** 判据是伤害 vs 生命值：同样 16 格预计下落，满血不动，残血(6)才动。 */
    private static void fatalityUsesHealthNotHeight() {
        FallPlan.Input full = in(6.0, -0.5, 0, 0, 10, 20, true);
        FallPlan.Input hurt = in(6.0, -0.5, 0, 0, 10, 6, true);
        check("16-drop at full health is none", FallPlan.decide(full) == FallPlan.Decision.NONE);
        check("same drop at 6 HP is arm", FallPlan.decide(hurt) == FallPlan.Decision.ARM);
        check("damageForDrop(10)==7", FallPlan.damageForDrop(10.0) == 7.0);
        check("damageForDrop(3)==0", FallPlan.damageForDrop(3.0) == 0.0);
    }

    /** ② 让路：已有安全缓冲 / 地面未知 / 已死 → none。 */
    private static void buffersAndUnknownGroundForbidAction() {
        FallPlan.Input buffered = new FallPlan.Input(100, 0, 0, 0, 0, 20, true, true, true, 20,
                FallPlan.Mitigation.NONE);
        check("has buffer is none", FallPlan.decide(buffered) == FallPlan.Decision.NONE);

        FallPlan.Input unknown = in(100, 0, 0, Double.NaN, 0, 20, true);
        check("unknown ground (NaN) is none", FallPlan.decide(unknown) == FallPlan.Decision.NONE);

        FallPlan.Input dead = in(100, 0, 0, 0, 0, 0, true);
        check("dead player is none", FallPlan.decide(dead) == FallPlan.Decision.NONE);

        check("null input is none", FallPlan.decide(null) == FallPlan.Decision.NONE);
    }

    /** ② 让路：MLG 关掉 → 一律 none。 */
    private static void disabledNeverActs() {
        FallPlan.Input off = new FallPlan.Input(100, 0, 0, 0, 0, 20, true, false, false, 20,
                FallPlan.Mitigation.NONE);
        check("disabled reflex never acts", FallPlan.decide(off) == FallPlan.Decision.NONE);
    }

    /** ③ 反应窗口（tick 预算）落在合理区间，且水平速度只增不减、有上限。 */
    private static void reactionWindowSane() {
        check("MIN_PLACE_TICKS >= 1", FallPlan.MIN_PLACE_TICKS >= 1);
        check("PLACE_LEAD_TICKS >= MIN_PLACE_TICKS", FallPlan.PLACE_LEAD_TICKS >= FallPlan.MIN_PLACE_TICKS);
        check("ARM_LEAD_TICKS > PLACE_LEAD_TICKS", FallPlan.ARM_LEAD_TICKS > FallPlan.PLACE_LEAD_TICKS);
        check("place lead in sane ticks (2..8)",
                FallPlan.placeLeadTicks(0) >= 2 && FallPlan.placeLeadTicks(0) <= 8);
        check("arm lead in sane ticks (<=40)", FallPlan.ARM_LEAD_TICKS >= 1 && FallPlan.ARM_LEAD_TICKS <= 40);
        check("horizontal speed only widens window",
                FallPlan.placeLeadTicks(4.0) >= FallPlan.placeLeadTicks(0));
        check("horizontal widening is capped",
                FallPlan.placeLeadTicks(100.0) == FallPlan.PLACE_LEAD_TICKS + FallPlan.HORIZONTAL_LEAD_MAX);
        check("tick rate normalization", FallPlan.normalizeTickRate(0) == 20
                && FallPlan.normalizeTickRate(-5) == 20 && FallPlan.normalizeTickRate(20) == 20);
    }

    /** ③ 下落仿真：单调、边界正确、NaN 保守。 */
    private static void tickSimulationSane() {
        check("at ground -> 0 ticks", FallPlan.ticksToLand(0.0, -1.0, 0.0) == 0);
        int low = FallPlan.ticksToLand(3.0, 0, 0);
        int high = FallPlan.ticksToLand(20.0, 0, 0);
        check("higher fall takes more ticks (low=" + low + " high=" + high + ")", low > 0 && high > low);
        check("NaN position -> -1 (unknown)", FallPlan.ticksToLand(Double.NaN, 0, 0) == -1);
        check("runaway sim -> -1", FallPlan.ticksToLand(Double.MAX_VALUE, 0, Double.NEGATIVE_INFINITY) == -1);
    }

    /** ④ 回执枚举稳定（receipt 字符串 + phase 枚举）。 */
    private static void receiptEnumStable() {
        check("gate name is mlg", "mlg".equals(MlgReflex.GATE));
        check("receipt armed stable", "armed".equals(MlgReflex.RECEIPT_ARMED));
        check("receipt placed stable", "placed".equals(MlgReflex.RECEIPT_PLACED));
        check("receipt picked_up stable", "picked_up".equals(MlgReflex.RECEIPT_PICKED_UP));
        check("receipt too_late stable", "too_late".equals(MlgReflex.RECEIPT_TOO_LATE));
        check("receipt no_bucket stable", "unavailable:no_bucket".equals(MlgReflex.RECEIPT_NO_BUCKET));
        check("abort prefix stable", "aborted:".equals(MlgReflex.ABORT_PREFIX));

        MlgReflex.Phase[] phases = MlgReflex.Phase.values();
        check("five phases", phases.length == 5);
        check("phase IDLE..RECOVERING order",
                phases[0] == MlgReflex.Phase.IDLE
                        && phases[1] == MlgReflex.Phase.ARMING
                        && phases[2] == MlgReflex.Phase.PLACING
                        && phases[3] == MlgReflex.Phase.PLACED
                        && phases[4] == MlgReflex.Phase.RECOVERING);

        // 反射覆盖：reflex_override {name:"mlg"} 能让反射门抑制掉它（纯逻辑）
        ReflexGate.clearAll();
        long t0 = 1_000_000L;
        check("mlg not suppressed by default", !ReflexGate.isSuppressed(MlgReflex.GATE, t0));
        ReflexGate.suppress(MlgReflex.GATE, 5, t0);
        check("mlg suppressible via reflex_override", ReflexGate.isSuppressed(MlgReflex.GATE, t0));
        check("mlg suppression expires", !ReflexGate.isSuppressed(MlgReflex.GATE, t0 + 6_000));
        ReflexGate.suppress(ReflexGate.ALL, 5, t0);
        check("all suppresses mlg", ReflexGate.isSuppressed(MlgReflex.GATE, t0));
        ReflexGate.clearAll();
    }

    /** ⑤ 裸伤害 = 原版 26.3 公式 floor((落距 + 1e-6 - 免赔) × 倍率)（不是老版的 ceil(落距-3)）。 */
    private static void baseDamageMatchesVanillaFormula() {
        check("10-block drop -> 7 damage", FallPlan.damageForDrop(10.0) == 7.0);
        check("3-block drop -> 0 damage", FallPlan.damageForDrop(3.0) == 0.0);
        check("3.5-block drop -> 0 damage (26.3 floors, does not ceil)",
                FallPlan.baseFallDamage(3.5, 3.0, 1.0) == 0.0);
        check("4-block drop -> 1 damage", FallPlan.baseFallDamage(4.0, 3.0, 1.0) == 1.0);
        check("jump boost safe fall distance 4 -> 4-block drop is 0 damage",
                FallPlan.baseFallDamage(4.0, 4.0, 1.0) == 0.0);
        check("fall_damage_multiplier attribute scales damage 7 -> 14",
                FallPlan.baseFallDamage(10.0, 3.0, 2.0) == 14.0);
        check("zero fall_damage_multiplier -> 0 damage",
                FallPlan.baseFallDamage(10.0, 3.0, 0.0) == 0.0);
        check("NaN drop -> 0 damage", FallPlan.baseFallDamage(Double.NaN, 3.0, 1.0) == 0.0);
    }

    /** ⑤ 羽落：每级 3 EPF，最高 4 级 = 12 EPF；减免后不致命 → 绝不放水。 */
    private static void featherFallingReducesDamage() {
        check("feather falling EPF = 3 per level (1..4)",
                FallPlan.featherFallingEpf(1) == 3 && FallPlan.featherFallingEpf(2) == 6
                        && FallPlan.featherFallingEpf(3) == 9 && FallPlan.featherFallingEpf(4) == 12);
        check("feather falling level capped at max_level=4 -> 12 EPF",
                FallPlan.featherFallingEpf(5) == 12 && FallPlan.featherFallingEpf(99) == 12);
        check("feather falling level below 0 -> 0 EPF", FallPlan.featherFallingEpf(-1) == 0);
        check("clampFeatherFallingLevel(7) == 4", FallPlan.clampFeatherFallingLevel(7) == 4);

        FallPlan.Mitigation bare = FallPlan.Mitigation.NONE;
        FallPlan.Mitigation ff4 = new FallPlan.Mitigation(true, 3.0, 1.0, 4, 0, -1, false);
        check("bare 33.5-block drop is 30 damage (fatal at 20 HP)",
                FallPlan.mitigatedFallDamage(33.5, bare) == 30.0);
        check("feather falling IV turns 30 damage into 15.6 (non-fatal)",
                close(FallPlan.mitigatedFallDamage(33.5, ff4), 15.6));
        check("no enchants -> arm (no water decision is unaffected)",
                armScenario(bare) == FallPlan.Decision.ARM);
        check("feather falling IV -> none (never place water)",
                armScenario(ff4) == FallPlan.Decision.NONE);
    }

    /** ⑤ 保护：每件每级 1 EPF、四件上限 16；对摔落生效（火/爆炸/弹射保护对摔落无效，不建模）。 */
    private static void protectionReducesDamage() {
        check("protection EPF = 1 per level, capped at 4x4=16",
                FallPlan.protectionEpf(4) == 4 && FallPlan.protectionEpf(16) == 16
                        && FallPlan.protectionEpf(17) == 16 && FallPlan.protectionEpf(-3) == 0);
        check("protection level clamped per piece to 0..4",
                FallPlan.clampProtectionLevel(5) == 4 && FallPlan.clampProtectionLevel(-1) == 0);

        FallPlan.Mitigation prot4 = new FallPlan.Mitigation(true, 3.0, 1.0, 0, 16, -1, false);
        check("4x protection IV turns 30 damage into 10.8 (non-fatal)",
                close(FallPlan.mitigatedFallDamage(33.5, prot4), 10.8));
        check("4x protection IV -> none (never place water)",
                armScenario(prot4) == FallPlan.Decision.NONE);
    }

    /** ⑤ 叠加：EPF 总和按原版截断到 20 → 最多减免 80%；贴上 EPF 上限的浮点精度也要一致。 */
    private static void mitigationSaturatesAtEpfCap() {
        check("total EPF saturates at EPF_CAP=20",
                FallPlan.totalEpf(4, 16) == 20 && FallPlan.totalEpf(4, 8) == 20
                        && FallPlan.totalEpf(2, 4) == 10 && FallPlan.totalEpf(0, 0) == 0
                        && FallPlan.totalEpf(-1, -1) == 0);
        check("EPF cap = 20 (CombatRules clamp)", FallPlan.EPF_CAP == 20);
        check("EPF 0 -> no reduction", FallPlan.applyEpf(100.0f, 0) == 100.0f);
        check("EPF 20 -> 20% of damage (max 80% reduction)",
                close(FallPlan.applyEpf(100.0f, 20), 20.0));
        check("EPF above cap behaves like the cap",
                FallPlan.applyEpf(100.0f, 40) == FallPlan.applyEpf(100.0f, 20));

        FallPlan.Mitigation both = new FallPlan.Mitigation(true, 3.0, 1.0, 4, 16, -1, false);
        check("feather IV + 4x protection IV = 80% reduction (30 -> 6 damage)",
                close(FallPlan.mitigatedFallDamage(33.5, both), 6.0));
        check("both enchants -> none (never place water)",
                armScenario(both) == FallPlan.Decision.NONE);
    }

    /** ⑤ 抗性提升与缓降：前者按 (25-5*(amp+1))/25 减免，后者直接免摔落伤害。 */
    private static void resistanceAndSlowFallingModeled() {
        check("no resistance -> damage unchanged", FallPlan.applyResistance(100.0f, -1) == 100.0f);
        check("resistance I -> 80% of damage", FallPlan.applyResistance(100.0f, 0) == 80.0f);
        check("resistance amplification clamps at 0 damage",
                FallPlan.applyResistance(100.0f, 4) == 0.0f
                        && FallPlan.applyResistance(100.0f, 9) == 0.0f);

        FallPlan.Mitigation res1 = new FallPlan.Mitigation(true, 3.0, 1.0, 0, 0, 0, false);
        check("resistance I makes 30 damage 24 -> still fatal (arm)",
                armScenario(res1) == FallPlan.Decision.ARM);
        FallPlan.Mitigation res2 = new FallPlan.Mitigation(true, 3.0, 1.0, 0, 0, 1, false);
        check("resistance II makes 30 damage 18 -> non-fatal (none)",
                armScenario(res2) == FallPlan.Decision.NONE);

        FallPlan.Mitigation slow = new FallPlan.Mitigation(true, 3.0, 1.0, 0, 0, -1, true);
        check("slow falling -> 0 damage", FallPlan.mitigatedFallDamage(1000.0, slow) == 0.0);
        check("slow falling -> none even inside the arm window",
                armScenario(slow) == FallPlan.Decision.NONE);
    }

    /** ⑤ 读不到物品/附魔信息 → 保守：忽略全部减免，按裸伤害判（宁可按会死处理）。 */
    private static void unreadableMitigationStaysConservative() {
        check("UNREADABLE ignores every reduction (30 damage stays 30)",
                FallPlan.mitigatedFallDamage(33.5, FallPlan.Mitigation.UNREADABLE) == 30.0);
        check("null mitigation behaves like UNREADABLE",
                FallPlan.mitigatedFallDamage(33.5, null) == 30.0);
        check("readable=false ignores levels/effects even if they are set",
                FallPlan.mitigatedFallDamage(33.5,
                        new FallPlan.Mitigation(false, 3.0, 1.0, 4, 16, 3, true)) == 30.0);
        check("UNREADABLE stays fatal -> arm (conservative, never a false negative)",
                armScenario(FallPlan.Mitigation.UNREADABLE) == FallPlan.Decision.ARM);
        check("null mitigation stays fatal -> arm",
                armScenario(null) == FallPlan.Decision.ARM);
        check("UNREADABLE inside the place window -> place_water",
                FallPlan.decide(in(1.0, -0.5, 0, 0, 30, 20, true, FallPlan.Mitigation.UNREADABLE))
                        == FallPlan.Decision.PLACE_WATER);
    }

    /** ⑤ 附魔等级边界：致死/不致死两侧，以及"伤害正好等于生命值仍算死"的原版语义。 */
    private static void enchantBoundaryDecidesWaterOrNot() {
        FallPlan.Mitigation ff2 = new FallPlan.Mitigation(true, 3.0, 1.0, 2, 0, -1, false);
        FallPlan.Mitigation ff3 = new FallPlan.Mitigation(true, 3.0, 1.0, 3, 0, -1, false);
        check("feather falling II keeps 30 damage lethal (22.8 >= 20)",
                FallPlan.mitigatedFallDamage(33.5, ff2) >= 20.0);
        check("feather falling II -> arm", armScenario(ff2) == FallPlan.Decision.ARM);
        check("feather falling III makes 30 damage non-lethal (19.2 < 20)",
                FallPlan.mitigatedFallDamage(33.5, ff3) < 20.0);
        check("feather falling III -> none", armScenario(ff3) == FallPlan.Decision.NONE);

        // 伤害正好等于生命值 → 原版仍然致死（hurt 把血打到 0）→ 必须动手
        FallPlan.Input exact = in(3.5, -0.5, 0, 0, 24.5, 25.0, true, FallPlan.Mitigation.NONE);
        check("damage == health is still fatal (place/arm, not none)",
                FallPlan.decide(exact) == FallPlan.Decision.ARM);

        // 25 伤害 + 20 EPF：float 精度下略小于 5 HP → 5 点血能活（与游戏逐位一致）
        FallPlan.Mitigation both = new FallPlan.Mitigation(true, 3.0, 1.0, 4, 16, -1, false);
        check("25 damage + 20 EPF is just below 5 HP (float-faithful)",
                FallPlan.mitigatedFallDamage(28.0, both) < 5.0
                        && FallPlan.mitigatedFallDamage(28.0, both) > 4.9999);
        check("5 HP survivor at that boundary -> none",
                FallPlan.decide(in(3.5, -0.5, 0, 0, 24.5, 5.0, true, both)) == FallPlan.Decision.NONE);
    }

    /** 反应窗口内的"会摔死"场景（drop = 33.5 格，裸伤害 30）：用于观察减免把 arm 压成 none。 */
    private static FallPlan.Decision armScenario(FallPlan.Mitigation mitigation) {
        FallPlan.Input p = in(3.5, -0.5, 0, 0, 30, 20, true, mitigation);
        int ticks = FallPlan.ticksToLand(p.y(), p.velY(), p.groundY());
        if (ticks <= FallPlan.placeLeadTicks(p.horizontalSpeed()) || ticks > FallPlan.ARM_LEAD_TICKS) {
            throw new IllegalStateException("scenario not in arm window: ticks=" + ticks);
        }
        return FallPlan.decide(p);
    }

    private static void check(String name, boolean ok) {
        if (ok) {
            passed++;
            System.out.println("  OK   " + name);
        } else {
            failed++;
            System.out.println("  FAIL " + name);
        }
    }
}
