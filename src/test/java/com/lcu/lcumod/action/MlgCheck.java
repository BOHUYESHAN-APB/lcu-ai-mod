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
 *   <li>回执枚举稳定（receipt 字符串 + phase 枚举）。</li>
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

        System.out.println("[MlgCheck] " + passed + " checks passed, " + failed + " failed");
        if (failed > 0) {
            System.exit(1);
        }
    }

    private static FallPlan.Input in(double y, double vy, double horiz, double groundY,
                                     double fell, double health, boolean bucket) {
        return new FallPlan.Input(y, vy, horiz, groundY, fell, health, bucket, false, true, 20);
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
        FallPlan.Input buffered = new FallPlan.Input(100, 0, 0, 0, 0, 20, true, true, true, 20);
        check("has buffer is none", FallPlan.decide(buffered) == FallPlan.Decision.NONE);

        FallPlan.Input unknown = in(100, 0, 0, Double.NaN, 0, 20, true);
        check("unknown ground (NaN) is none", FallPlan.decide(unknown) == FallPlan.Decision.NONE);

        FallPlan.Input dead = in(100, 0, 0, 0, 0, 0, true);
        check("dead player is none", FallPlan.decide(dead) == FallPlan.Decision.NONE);

        check("null input is none", FallPlan.decide(null) == FallPlan.Decision.NONE);
    }

    /** ② 让路：MLG 关掉 → 一律 none。 */
    private static void disabledNeverActs() {
        FallPlan.Input off = new FallPlan.Input(100, 0, 0, 0, 0, 20, true, false, false, 20);
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
