package com.lcu.lcumod.client;

/**
 * 零依赖自检：HUD/窗口标题的"当前在干什么"词汇表（纯逻辑）。
 * 断言信息用 ASCII（Windows 控制台 GBK）；失败非零退出。
 */
public final class ActivityPhraseCheck {

    private static int passed = 0;
    private static int failed = 0;

    public static void main(String[] args) {
        // 优先级：命 > 憋气 > 打架 > 其他
        check("fire beats everything",
                ActivityPhrase.of(true, true, true, true, "travel", true, true).equals("逃命中"));
        check("underwater beats combat",
                ActivityPhrase.of(false, true, true, false, "travel", true, true).equals("憋气中"));
        check("combat beats task",
                ActivityPhrase.of(false, false, true, false, "travel", true, true).equals("跟它干上了"));
        check("sleep beats task",
                ActivityPhrase.of(false, false, false, true, "travel", true, true).equals("找床睡觉中"));

        // 任务词
        check("collect", ActivityPhrase.of(false, false, false, false, "collect", false, false).equals("捡东西中"));
        check("craft", ActivityPhrase.of(false, false, false, false, "craft", false, false).equals("造东西中"));
        check("follow", ActivityPhrase.of(false, false, false, false, "follow", false, false).equals("跟着你呢"));
        check("travel task", ActivityPhrase.of(false, false, false, false, "travel", false, false).equals("赶路中"));
        check("mine", ActivityPhrase.of(false, false, false, false, "mine", false, false).equals("挖矿中"));
        check("build", ActivityPhrase.of(false, false, false, false, "build", false, false).equals("盖房子中"));

        // 无任务时的兜底
        check("navigating fallback", ActivityPhrase.of(false, false, false, false, "", true, false).equals("赶路中"));
        check("moving fallback", ActivityPhrase.of(false, false, false, false, "", false, true).equals("遛弯中"));
        check("idle fallback", ActivityPhrase.of(false, false, false, false, "", false, false).equals("发呆中"));
        check("null task kind safe",
                ActivityPhrase.of(false, false, false, false, null, false, false).equals("发呆中"));
        check("unknown task kind safe",
                ActivityPhrase.of(false, false, false, false, "weird", false, false).equals("发呆中"));

        // 用户明确不要工程词：任何输出都不能含"寻路/导航"
        String[] samples = {
                ActivityPhrase.of(true, false, false, false, "", false, false),
                ActivityPhrase.of(false, true, false, false, "", false, false),
                ActivityPhrase.of(false, false, false, false, "travel", true, true),
                ActivityPhrase.of(false, false, false, false, "", true, true),
        };
        boolean clean = true;
        for (String sample : samples) {
            if (sample.contains("寻路") || sample.contains("导航")) {
                clean = false;
            }
        }
        check("no engineering words (xunlu/daohang)", clean);

        System.out.println("[ActivityPhraseCheck] " + passed + " checks passed, " + failed + " failed");
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
