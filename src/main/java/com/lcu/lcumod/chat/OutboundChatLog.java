package com.lcu.lcumod.chat;

import java.util.ArrayDeque;
import java.util.Deque;

/**
 * 记录**本模组自己刚发出去的聊天**，用于 ChatListener 做回声去重。
 *
 * 为什么不按"发送者 == 本地玩家名"过滤：那样会把**用户自己在同一个窗口里打的字**
 * 一起吃掉。用户实机就是这样——在游戏里喊"回来"，模组一声不吭、后端一个字节都没收到。
 * 改成只丢弃"我们刚刚逐字发出去的那条"后，用户自己打的字可以正常进入决策链路。
 *
 * 纯逻辑，不依赖 Minecraft（与 EdgeTrigger 同一风格，便于自检）。
 */
public final class OutboundChatLog {

    /** 记录有效期：超过这个时间的记录不再参与去重（服务器回声通常 < 1s）。 */
    private static final long TTL_MS = 10_000L;
    private static final int MAX_ENTRIES = 16;

    private static final Deque<Entry> SENT = new ArrayDeque<>();

    private record Entry(String text, long atMs) {}

    private OutboundChatLog() {}

    /** 记录一条**我们自己发出去**的聊天正文。 */
    public static synchronized void record(String text) {
        if (text == null || text.isEmpty()) return;
        purge();
        SENT.addLast(new Entry(text, System.currentTimeMillis()));
        while (SENT.size() > MAX_ENTRIES) {
            SENT.removeFirst();
        }
    }

    /** 若 text 是刚发出去的那条则消费掉（一次性）并返回 true。 */
    public static synchronized boolean consume(String text) {
        if (text == null || text.isEmpty()) return false;
        purge();
        for (var it = SENT.iterator(); it.hasNext(); ) {
            if (it.next().text().equals(text)) {
                it.remove();
                return true;
            }
        }
        return false;
    }

    /** 仅供自检使用。 */
    static synchronized int size() {
        purge();
        return SENT.size();
    }

    private static void purge() {
        long now = System.currentTimeMillis();
        while (!SENT.isEmpty() && now - SENT.peekFirst().atMs() > TTL_MS) {
            SENT.removeFirst();
        }
    }
}
