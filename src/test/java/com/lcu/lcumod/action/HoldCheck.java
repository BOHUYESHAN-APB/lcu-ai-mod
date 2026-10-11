package com.lcu.lcumod.action;

/**
 * 零依赖自检：HoldController（一等公民的“按住”语义）+ EatController（进食状态机）。
 *
 * <p>照现有 verify* 的写法：纯 Java + JavaExec（见 build.gradle 的 {@code verifyHold} 任务），
 * 不引入任何依赖，随 {@code build} / {@code check} 一起跑。断言信息用 ASCII（Windows 控制台 GBK）。
 *
 * <p>覆盖用户点名的 6 条契约：
 * <ol>
 *   <li>hold N tick 后必松开；</li>
 *   <li>条件命中即松开；</li>
 *   <li>超时松开且带可验证原因；</li>
 *   <li>AI 关闭 / 新任务 / 玩家接管 / 反射覆盖时**立即**释放（宁早松不粘键）；</li>
 *   <li>重复 eat 幂等（不重置计时/槽位）；</li>
 *   <li>reflex_eat 在反射进食中为 true、结束后 false。</li>
 * </ol>
 */
public final class HoldCheck {

    private static int passed = 0;
    private static int failed = 0;

    private HoldCheck() {
    }

    public static void main(String[] args) {
        holdExpiresAfterNTicks();
        conditionMetReleasesImmediately();
        timeoutReleasesWithReason();
        safetyReleasesImmediately();
        replacementReleasesOld();
        eatIsIdempotentWhenAlreadyEating();
        eatSucceedsOnHungerSaturationOrCount();
        eatTimesOutAfter40Ticks();
        eatReflexFlagTracksEating();

        System.out.println("[HoldCheck] " + passed + " checks passed, " + failed + " failed");
        if (failed > 0) {
            System.exit(1);
        }
    }

    /** ① hold N tick 后必松开：恰好应用 N 次，第 N 次后释放且原因为 EXPIRED。 */
    private static void holdExpiresAfterNTicks() {
        HoldController hc = new HoldController();
        HoldController.Handle h = hc.hold("use", 3);
        check("hold is active right after creation", h.isActive());

        HoldController.TickResult r1 = hc.tick(true, false, false);
        check("hold applies tick 1", r1.applied().contains("use") && h.isActive());
        HoldController.TickResult r2 = hc.tick(true, false, false);
        check("hold applies tick 2", r2.applied().contains("use") && h.isActive());
        HoldController.TickResult r3 = hc.tick(true, false, false);
        check("hold applies tick 3 then releases", r3.applied().contains("use") && !h.isActive());
        check("hold release reason is EXPIRED", h.releaseReason() == HoldController.Release.EXPIRED);
        check("hold reason code is lowercase", "expired".equals(h.releaseCode()));

        HoldController.TickResult r4 = hc.tick(true, false, false);
        check("released hold no longer applies", r4.applied().isEmpty() && !hc.isHeld("use"));
    }

    /** ② 条件命中即松开：谓词转 false 的那一 tick 立即释放，且该 tick 不再写入。 */
    private static void conditionMetReleasesImmediately() {
        HoldController hc = new HoldController();
        boolean[] wanted = {true};
        HoldController.Handle h = hc.holdUntil("attack", () -> wanted[0], 100);

        HoldController.TickResult r1 = hc.tick(true, false, false);
        check("conditional hold applies while wanted", r1.applied().contains("attack") && h.isActive());

        wanted[0] = false;
        HoldController.TickResult r2 = hc.tick(true, false, false);
        check("conditional hold releases when condition met", !h.isActive());
        check("met tick does not apply input", r2.applied().isEmpty());
        check("condition-met reason is CONDITION_MET",
                h.releaseReason() == HoldController.Release.CONDITION_MET);
    }

    /** ③ 超时松开带原因：条件恒真，maxTicks 到点 → TIMEOUT。 */
    private static void timeoutReleasesWithReason() {
        HoldController hc = new HoldController();
        HoldController.Handle h = hc.holdUntil("use", () -> true, 4);
        for (int i = 0; i < 3; i++) {
            hc.tick(true, false, false);
        }
        check("conditional hold still active before maxTicks", h.isActive());
        hc.tick(true, false, false);
        check("conditional hold released at maxTicks", !h.isActive());
        check("timeout reason is TIMEOUT", h.releaseReason() == HoldController.Release.TIMEOUT);
    }

    /** ④ AI 关闭 / 玩家接管 / 反射覆盖 → 立即释放；新任务经 releaseAll 立即释放。 */
    private static void safetyReleasesImmediately() {
        HoldController hc = new HoldController();
        hc.hold("forward", 1000);
        HoldController.TickResult aiOff = hc.tick(false, false, false);
        check("AI disabled releases holds",
                aiOff.released().size() == 1 && !hc.isHeld("forward"));
        check("AI-disabled reason is AI_DISABLED",
                aiOff.released().get(0).releaseReason() == HoldController.Release.AI_DISABLED);

        hc.hold("use", 1000);
        HoldController.TickResult takeover = hc.tick(true, true, false);
        check("player takeover releases holds", !hc.isHeld("use"));
        check("takeover reason is PLAYER_TAKEOVER",
                takeover.released().get(0).releaseReason() == HoldController.Release.PLAYER_TAKEOVER);

        hc.hold("attack", 1000);
        HoldController.TickResult reflex = hc.tick(true, false, true);
        check("reflex override releases holds", !hc.isHeld("attack"));
        check("reflex reason is REFLEX_OVERRIDE",
                reflex.released().get(0).releaseReason() == HoldController.Release.REFLEX_OVERRIDE);

        HoldController.Handle h = hc.hold("use", 1000);
        hc.releaseAll(HoldController.Release.NEW_TASK);
        check("new task releases holds immediately", !h.isActive());
        check("new-task reason is NEW_TASK", h.releaseReason() == HoldController.Release.NEW_TASK);
    }

    /** 同一控制被新 hold 顶替时，旧 handle 以 REPLACED 释放（避免两个 hold 抢同一个键）。 */
    private static void replacementReleasesOld() {
        HoldController hc = new HoldController();
        HoldController.Handle old = hc.hold("use", 10);
        HoldController.Handle fresh = hc.holdUntil("use", () -> true, 50);
        check("new hold replaces old on same control", !old.isActive());
        check("replaced reason is REPLACED", old.releaseReason() == HoldController.Release.REPLACED);
        check("new hold is active", fresh.isActive() && hc.get("use") == fresh);
    }

    /** ⑤ 重复 eat 幂等：第二次 begin 不重置计时/槽位，只登记 waiter。 */
    private static void eatIsIdempotentWhenAlreadyEating() {
        EatController ec = new EatController();
        boolean first = ec.begin("req_1", false, 3, 10, 0.0f, 5);
        check("first eat begins", first && ec.isActive() && ec.slot() == 3);
        ec.tick(10, 0.0f, 5); // 推进 1 tick

        boolean second = ec.begin("req_2", false, 7, 10, 0.0f, 5);
        check("repeat eat is idempotent (not restarted)", !second);
        check("repeat eat keeps original slot", ec.slot() == 3);
        check("repeat eat does not reset ticks", ec.ticks() == 1);
        check("repeat eat registers waiter", ec.drainWaiters().containsAll(java.util.List.of("req_1", "req_2")));
        check("no track of use started before SELECT_TICKS", !ec.shouldStartUse());
        ec.tick(10, 0.0f, 5);
        check("use starts after SELECT_TICKS", ec.shouldStartUse() && ec.isActive());
    }

    /** 三判据：饱食度 ↑ / 饱和度 ↑ / 该槽数量 − 任一命中即成功。 */
    private static void eatSucceedsOnHungerSaturationOrCount() {
        check("hunger up => SUCCESS", eatOutcomeAfter(1, 0.0f, 5, 11, 0.0f, 5) == EatController.Outcome.SUCCESS);
        check("saturation up => SUCCESS", eatOutcomeAfter(1, 0.0f, 5, 1, 0.5f, 5) == EatController.Outcome.SUCCESS);
        check("count down => SUCCESS", eatOutcomeAfter(1, 0.0f, 5, 1, 0.0f, 4) == EatController.Outcome.SUCCESS);
        check("no change => still eating", eatOutcomeAfter(1, 0.0f, 5, 1, 0.0f, 5) == EatController.Outcome.NONE);
    }

    private static EatController.Outcome eatOutcomeAfter(int startHunger, float startSat, int startCount,
                                                         int nowHunger, float nowSat, int nowCount) {
        EatController ec = new EatController();
        ec.begin("req", false, 0, startHunger, startSat, startCount);
        return ec.tick(nowHunger, nowSat, nowCount);
    }

    /** ③(+eat) 40 tick 未生效 → TIMEOUT，且状态复位。 */
    private static void eatTimesOutAfter40Ticks() {
        EatController ec = new EatController();
        ec.begin("req", false, 0, 10, 0.0f, 5);
        EatController.Outcome last = EatController.Outcome.NONE;
        for (int i = 0; i < EatController.MAX_TICKS; i++) {
            last = ec.tick(10, 0.0f, 5);
        }
        check("eat times out at MAX_TICKS", last == EatController.Outcome.TIMEOUT);
        check("eat inactive after timeout", !ec.isActive());
        check("timeout recorded as lastOutcome", ec.lastOutcome() == EatController.Outcome.TIMEOUT);
    }

    /** ⑥ reflex_eat：反射进食中 true，结束（成功/超时）后 false。 */
    private static void eatReflexFlagTracksEating() {
        EatController ec = new EatController();
        ec.begin("reflex_eat", true, 1, 10, 0.0f, 3);
        check("reflex flag true while reflex eating", ec.isReflex());
        ec.tick(11, 0.0f, 3); // 食物生效
        check("reflex flag false after success", !ec.isReflex());

        EatController ec2 = new EatController();
        ec2.begin("reflex_eat", true, 1, 10, 0.0f, 3);
        for (int i = 0; i < EatController.MAX_TICKS; i++) {
            ec2.tick(10, 0.0f, 3);
        }
        check("reflex flag false after timeout", !ec2.isReflex() && !ec2.isActive());

        EatController ec3 = new EatController();
        ec3.begin("req", false, 1, 10, 0.0f, 3);
        check("backend eat is not reflex", !ec3.isReflex());
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
