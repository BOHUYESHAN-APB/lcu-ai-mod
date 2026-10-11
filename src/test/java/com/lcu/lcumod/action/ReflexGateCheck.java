package com.lcu.lcumod.action;

/**
 * 零依赖自检：ReflexGate 的时间语义（本地反射覆盖机制）。
 * 断言信息用 ASCII（Windows 控制台 GBK）；失败非零退出。
 */
public final class ReflexGateCheck {

    private static int passed = 0;
    private static int failed = 0;

    public static void main(String[] args) {
        long t0 = 1_000_000L;

        check("default not suppressed", !ReflexGate.isSuppressed(ReflexGate.SWIM, t0));

        ReflexGate.suppress(ReflexGate.SWIM, 30, t0);
        check("suppressed right after set", ReflexGate.isSuppressed(ReflexGate.SWIM, t0));
        check("still suppressed at 29.9s", ReflexGate.isSuppressed(ReflexGate.SWIM, t0 + 29_900));
        check("expired at 30.1s", !ReflexGate.isSuppressed(ReflexGate.SWIM, t0 + 30_100));

        // 注意顺序：上面的"过期"断言会惰性清掉条目，所以这里重新抑制一次再量剩余时间
        ReflexGate.suppress(ReflexGate.SWIM, 30, t0);
        check("remaining seconds ~30", Math.abs(ReflexGate.remainingSeconds(ReflexGate.SWIM, t0) - 30.0) < 0.01);
        check("remaining 0 after expiry", ReflexGate.remainingSeconds(ReflexGate.SWIM, t0 + 31_000) == 0.0);

        // 立刻恢复
        ReflexGate.suppress(ReflexGate.SWIM, 60, t0);
        ReflexGate.suppress(ReflexGate.SWIM, 0, t0);
        check("seconds<=0 restores immediately", !ReflexGate.isSuppressed(ReflexGate.SWIM, t0));

        // all 覆盖全部名字
        ReflexGate.clearAll();
        ReflexGate.suppress(ReflexGate.ALL, 10, t0);
        check("all suppresses swim", ReflexGate.isSuppressed(ReflexGate.SWIM, t0));
        check("all suppresses combat", ReflexGate.isSuppressed(ReflexGate.COMBAT, t0));
        check("all expires", !ReflexGate.isSuppressed(ReflexGate.SWIM, t0 + 10_100));

        // 名字大小写/空白容错
        ReflexGate.clearAll();
        ReflexGate.suppress("  SWIM ", 5, t0);
        check("name is case/space tolerant", ReflexGate.isSuppressed("swim", t0));

        // 空名字不炸
        ReflexGate.suppress(null, 5, t0);
        ReflexGate.suppress("", 5, t0);
        check("null/empty name ignored", !ReflexGate.isSuppressed("", t0));

        System.out.println("[ReflexGateCheck] " + passed + " checks passed, " + failed + " failed");
        if (failed > 0) {
            System.exit(1);
        }
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
