package com.lcu.lcumod.action;

import java.util.Set;

/**
 * 服务器指令**硬黑名单**（Java 侧兜底，不可被话术/配置绕过）。
 *
 * <p>用户口径：「有些指令**绝对不可以**用：kill、kick 之类」。后端白名单是"能不能做"的授权门，
 * 这里是"永远不许"的硬门 —— 即使后端被提示词注入骗了、即使白名单里误配了，也在这里被拒。
 *
 * <p>匹配口径（都要处理）：大小写、前导 {@code /}（含多个、含全角 {@code ／}）、多余空白、
 * 命名空间前缀（{@code minecraft:kill} / {@code bukkit:kick} / {@code essentials:ban}）。
 *
 * <p>本类零依赖（不碰 Minecraft / gson），可被 {@code verifyGuard} 直接自检。
 */
public final class CommandBlacklist {

    /** 服务器管理 / 自杀 / 破坏体验类：一律硬拒绝。 */
    public static final java.util.List<String> HARD = java.util.List.of(
            "kill", "kick", "ban", "ban-ip", "pardon", "op", "deop", "stop",
            "restart", "whitelist", "difficulty", "gamerule", "save-off");

    private static final Set<String> HARD_SET = Set.copyOf(HARD);

    /** 稳定的机器可读原因码（后端据此识别"是硬门拦的，不是能力缺失"）。 */
    public static final String REASON = "hard_blacklist";

    /**
     * 判定结果。
     *
     * @param blocked 是否拒绝
     * @param reason  非空表示拒绝原因（恒为 {@link #REASON}）
     * @param family  归一化后的指令族（如 {@code kill}）；无法解析时为空串
     * @param familyIsBlacklisted family 是否命中硬黑名单（供审计）
     */
    public record Verdict(boolean blocked, String reason, String family) {
    }

    private CommandBlacklist() {
    }

    /**
     * 归一化：剥掉前导空白/{@code /}/{@code ／}，取第一个 token，去命名空间前缀，转小写。
     *
     * <p>例：{@code "  //MINECRAFT:kill  @a"} → {@code "kill"}；{@code " / gamerule"} → {@code "gamerule"}。
     */
    public static String normalize(String rawCommand) {
        if (rawCommand == null) {
            return "";
        }
        int i = 0;
        while (i < rawCommand.length() && (isBlank(rawCommand.charAt(i)) || isSlash(rawCommand.charAt(i)))) {
            i++;
        }
        String rest = rawCommand.substring(i).strip();
        if (rest.isEmpty()) {
            return "";
        }
        int end = 0;
        while (end < rest.length() && !isBlank(rest.charAt(end))) {
            end++;
        }
        String token = rest.substring(0, end);
        int colon = token.lastIndexOf(':');
        if (colon >= 0 && colon + 1 < token.length()) {
            token = token.substring(colon + 1);
        }
        return token.toLowerCase(java.util.Locale.ROOT);
    }

    /** 判定一条指令是否被硬黑名单拦住。 */
    public static Verdict check(String rawCommand) {
        String family = normalize(rawCommand);
        if (!family.isEmpty() && HARD_SET.contains(family)) {
            return new Verdict(true, REASON, family);
        }
        return new Verdict(false, "", family);
    }

    private static boolean isBlank(char c) {
        return c == ' ' || c == '\t' || c == '\n' || c == '\r'
                || Character.isWhitespace(c) || Character.isSpaceChar(c);
    }

    private static boolean isSlash(char c) {
        return c == '/' || c == '\uFF0F';
    }
}
