package com.lcu.lcumod.client;

/**
 * HUD 状态词后面的**描述词** —— 由后端大模型通过 wire 命令 `set_hud_note` 决定。
 *
 * 用户要求：「状态词后面还可以再跟一段内容，可以是后端大模型来输出」。
 * 例：`AI 控制中 · 找床睡觉中 · 夜里先补个觉，天亮继续挖`。
 *
 * 纯逻辑（可自检）：带 TTL，过期自动清空，不需要后端记得清理。
 */
public final class HudNote {

    private static String text = "";
    private static long untilMs = 0L;

    private HudNote() {
    }

    /** 设置描述词；seconds<=0 表示一直保留到被替换或清空。 */
    public static void set(String value, double seconds, long nowMs) {
        String trimmed = value == null ? "" : value.strip();
        if (trimmed.isEmpty()) {
            clear();
            return;
        }
        text = trimmed;
        untilMs = seconds <= 0 ? Long.MAX_VALUE : nowMs + (long) (seconds * 1000.0);
    }

    public static void set(String value, double seconds) {
        set(value, seconds, System.currentTimeMillis());
    }

    public static void clear() {
        text = "";
        untilMs = 0L;
    }

    /** 当前有效描述词（过期即清空）。 */
    public static String current(long nowMs) {
        if (text.isEmpty()) {
            return "";
        }
        if (nowMs >= untilMs) {
            clear();
            return "";
        }
        return text;
    }

    public static String current() {
        return current(System.currentTimeMillis());
    }
}
