package com.lcu.lcumod.client;

/**
 * 窗口标题的格式化逻辑 —— **不依赖 Minecraft**，因此可以离线自检（见 TitleFormatCheck）。
 *
 * 设计目标（用户要求）：标题要能一眼看出
 *   1. 现在是谁在操控（AI / 真人）
 *   2. 正在干什么（活动）
 *   3. 这是哪一个实例/整合包
 *   4. 在哪：单人世界名 / 服务器地址 / 主菜单
 *   5. 版本：MC + 加载器
 * 空字段自动省略，不会留下多余分隔符。
 *
 * 例：
 *   [AI 操控中] 挖掘中 · Rapid Optimization-ai · 服务器 127.0.0.1:25565 · MC 26.3 / NeoForge 26.3.0.42-beta
 */
public final class TitleFormat {

    public static final String AI_CONTROL = "AI 操控中";
    public static final String USER_CONTROL = "你操控中";
    public static final String SEP = " · ";

    private TitleFormat() {
    }

    /**
     * @param state     控制权标记，如 {@link #AI_CONTROL}
     * @param activity  当前活动（可空）
     * @param instance  实例/整合包目录名（可空）
     * @param where     单人世界名 / 服务器 / 主菜单（可空）
     * @param mcVersion 如 {@code MC 26.3}（可空）
     * @param loader    如 {@code NeoForge 26.3.0.42-beta}（可空）
     */
    public static String build(String state, String activity, String instance,
                               String where, String mcVersion, String loader) {
        // "[控制权] 活动 · 实例 · 在哪 · 版本"：空字段整体省略，不留下多余分隔符。
        String tail = join(SEP, activity, instance, where, join(" / ", mcVersion, loader));
        String head = "[" + orDefault(state, "?") + "]";
        return tail.isEmpty() ? head : head + " " + tail;
    }

    /** 拼接非空片段，用给定分隔符；全空返回空串。 */
    public static String join(String sep, String... parts) {
        StringBuilder sb = new StringBuilder();
        for (String part : parts) {
            String trimmed = trimToEmpty(part);
            if (trimmed.isEmpty()) {
                continue;
            }
            if (sb.length() > 0) {
                sb.append(sep);
            }
            sb.append(trimmed);
        }
        return sb.toString();
    }

    private static String orDefault(String value, String fallback) {
        String trimmed = trimToEmpty(value);
        return trimmed.isEmpty() ? fallback : trimmed;
    }

    private static String trimToEmpty(String value) {
        return value == null ? "" : value.strip();
    }
}
