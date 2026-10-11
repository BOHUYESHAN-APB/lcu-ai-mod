package com.lcu.lcumod.chat;

/**
 * 零依赖自检（替代 JUnit）：OutboundChatLog 的回声去重语义。
 *
 * 背景：ChatListener 曾按"发送者 == 本地玩家名"整类过滤，把**用户自己打的字**也吃掉，
 * 表现为"在游戏里发消息 AI 从来没收到"。这里锁死新语义：只丢弃模组自己刚发出去的那条。
 *
 * 断言信息用 ASCII（Windows 控制台是 GBK）。失败非零退出。
 */
public final class OutboundChatLogCheck {

    private static int passed = 0;
    private static int failed = 0;

    public static void main(String[] args) {
        int[] r = run();
        if (r[1] > 0) {
            System.exit(1);
        }
    }

    /** 跑完整套自检并打印摘要，返回 {passed, failed}。 */
    static int[] run() {
        passed = 0;
        failed = 0;

        // 1) 自己发的消息会被记录，并且只能消费一次
        OutboundChatLog.record("AI 已到达");
        check("own message consumed once", OutboundChatLog.consume("AI 已到达"));
        check("own message not consumable twice", !OutboundChatLog.consume("AI 已到达"));

        // 2) 用户自己打的字（从未被 record）必须放行 —— 这就是实机那个 bug
        check("user typed text is NOT filtered", !OutboundChatLog.consume("回来"));

        // 3) 多条互不影响
        OutboundChatLog.record("a");
        OutboundChatLog.record("b");
        check("multi: a", OutboundChatLog.consume("a"));
        check("multi: b still consumable", OutboundChatLog.consume("b"));
        check("multi: nothing left", OutboundChatLog.size() == 0);

        // 4) 空串/空值不炸
        check("null consume safe", !OutboundChatLog.consume(null));
        check("empty consume safe", !OutboundChatLog.consume(""));
        OutboundChatLog.record(null);
        OutboundChatLog.record("");
        check("null/empty record ignored", OutboundChatLog.size() == 0);

        // 5) 上限裁剪（最多记 16 条）
        for (int i = 0; i < 40; i++) {
            OutboundChatLog.record("msg-" + i);
        }
        check("size capped at 16", OutboundChatLog.size() == 16);
        check("newest kept", OutboundChatLog.consume("msg-39"));
        check("oldest evicted", !OutboundChatLog.consume("msg-0"));

        System.out.println("[OutboundChatLogCheck] " + passed + " checks passed, " + failed + " failed");
        return new int[] { passed, failed };
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
