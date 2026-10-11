package com.lcu.lcumod.action;

/**
 * {@link EdgeTrigger} 的可执行自检（M3-5 验收）。
 *
 * <p>为什么不用 JUnit：本机网络对 {@code maven.neoforged.net} 的 DNS 被污染，
 * 而 Gradle 遇到不可达仓库会**直接失败而不回退**，因此拉不到 JUnit 之类的测试依赖。
 * 这里改用纯 Java + JavaExec（见 build.gradle 的 {@code verifyLogic} 任务）：
 * 零第三方依赖、可随 {@code build} 一起跑，契约覆盖与单测等价。
 *
 * <p>契约：控制流消息是 20-50Hz 的 latest-wins，**可能快于客户端 tick**，
 * "按住"与"点按"都不能丢事件。
 *
 * <p>断言消息刻意用 ASCII：Windows 控制台是 GBK，中文输出会乱码，失败时反而看不清。
 */
public final class EdgeTriggerCheck {

    private static int passed = 0;

    private EdgeTriggerCheck() {
    }

    public static void main(String[] args) {
        tapBetweenTicksIsNotLost();
        holdFramesDoNotAccumulate();
        releaseStopsFiring();
        tapIsRetainedUntilReady();
        rapidTapsAreCappedNotUnbounded();
        resetClearsHeldAndPending();
        rejectsInvalidCapacity();

        System.out.println("[verifyLogic] EdgeTrigger: " + passed + " checks passed");
    }

    private static void check(boolean condition, String message) {
        if (!condition) {
            throw new AssertionError(message);
        }
        passed++;
    }

    /** 两次 tick 之间发生"按下+松开"时，点按必须补上而不是被吞掉。 */
    private static void tapBetweenTicksIsNotLost() {
        EdgeTrigger trigger = new EdgeTrigger(4);

        trigger.update(true);
        trigger.update(false);

        check(!trigger.isHeld(), "after release isHeld should be false");
        check(trigger.shouldFire(true), "tap must still fire once");
        check(!trigger.shouldFire(true), "tap should fire exactly once");
    }

    /** 持续按住帧不应继续累积边沿；按住期间每 tick 都能触发。 */
    private static void holdFramesDoNotAccumulate() {
        EdgeTrigger trigger = new EdgeTrigger(4);

        for (int i = 0; i < 50; i++) {
            trigger.update(true);
        }

        check(trigger.pending() == 1,
                "50 held frames should record exactly 1 edge, got " + trigger.pending());

        check(trigger.shouldFire(true), "held tick 1 should fire");
        check(trigger.shouldFire(true), "held tick 2 should fire");
        check(trigger.shouldFire(true), "held tick 3 should fire");
        check(trigger.pending() == 0,
                "first fire consumes the edge, got " + trigger.pending());
        check(trigger.isHeld(), "still held after firing");
    }

    /** 松开且无待处理时不应触发。 */
    private static void releaseStopsFiring() {
        EdgeTrigger trigger = new EdgeTrigger(4);

        trigger.update(true);
        check(trigger.shouldFire(true), "held should fire");
        trigger.update(false);

        check(!trigger.isHeld(), "after release isHeld should be false");
        check(!trigger.shouldFire(true), "released with no pending must not fire");
    }

    /** 冷却未就绪时不能消费点按，否则就绪后那一次就丢了。 */
    private static void tapIsRetainedUntilReady() {
        EdgeTrigger trigger = new EdgeTrigger(4);

        trigger.update(true);
        trigger.update(false);

        check(!trigger.shouldFire(false), "must not fire while not ready");
        check(trigger.pending() == 1,
                "not-ready must not consume the tap, got " + trigger.pending());
        check(trigger.shouldFire(true), "tap should fire once ready");
    }

    /** 狂发边沿时待处理数量必须被上限截断，不能无限堆积。 */
    private static void rapidTapsAreCappedNotUnbounded() {
        EdgeTrigger trigger = new EdgeTrigger(3);

        for (int i = 0; i < 10; i++) {
            trigger.update(true);
            trigger.update(false);
        }

        check(trigger.pending() == 3, "should be capped at 3, got " + trigger.pending());
        int fired = 0;
        while (trigger.shouldFire(true)) {
            fired++;
        }
        check(fired == 3, "should fire at most maxPending times, got " + fired);
    }

    /** reset（断线/超时/松手）必须同时清掉按住与待处理。 */
    private static void resetClearsHeldAndPending() {
        EdgeTrigger trigger = new EdgeTrigger(4);

        trigger.update(true);
        trigger.update(false);
        trigger.reset();

        check(!trigger.isHeld(), "after reset isHeld should be false");
        check(trigger.pending() == 0, "after reset pending should be 0");
        check(!trigger.shouldFire(true), "after reset must not fire");
    }

    /** 非法容量必须被拒绝（配置错误要尽早暴露）。 */
    private static void rejectsInvalidCapacity() {
        try {
            new EdgeTrigger(0);
        } catch (IllegalArgumentException expected) {
            passed++;
            return;
        }
        throw new AssertionError("maxPending=0 should throw IllegalArgumentException");
    }
}
