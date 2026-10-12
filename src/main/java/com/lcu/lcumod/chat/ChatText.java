package com.lcu.lcumod.chat;

/**
 * 出站聊天文本的**最后一道字符防线**（纯逻辑、零依赖、无状态，可直接被自检断言）。
 *
 * <p>真机事故（2026-10-12 16:22）：AI 发出去的聊天里带了**换行符**（模型回复与"如实说明"拼串时
 * 带进来的），服务器判定聊天内容非法 ⇒ **直接把玩家踢出**。日志那句话证实了这一点：
 * {@code -> send_chat {'message': '…没有执行……）\n（不知道是哪个基地…）'}}。
 * 后端侧已经修了（{@code backend/agent/chat_sanitize.py} + 唯一出口 {@code agent/outbound.py}），
 * 但**真正把包发出去的是 Java 侧** —— 这里再兜一道：不管后端怎么变、以后谁又来发聊天，
 * 到不了聊天通道的字符一律不许出网。
 *
 * <p>口径与后端 {@code chat_sanitize.sanitize_chat} **一致**：
 * <ul>
 *   <li>换行 / 制表 / 行分隔符（{@code \r\n \r \n \t \u000B \u000C \u0085 \u2028 \u2029}）
 *       ⇒ <b>一个空格</b>（保留句子边界，别把两句话粘成一句）；</li>
 *   <li>零宽与双向控制（{@code \u200B-\u200F \u202A-\u202E \u2060-\u2064 \uFEFF \u00AD}）、
 *       C0/C1/DEL 控制字符、孤立代理项、{@code §}（染色码）⇒ <b>删除</b>；</li>
 *   <li>其余空白（连续空格、首尾空白、NBSP、全角空格…）⇒ 压成一个空格、去首尾；</li>
 *   <li>超过 {@link #MAX_CHARS}（200 字符，原版上限 256 留余量）⇒ 截断并加 {@code …}，
 *       且**不切半个代理对**（按码位计数，不是 Java 的 char 单元）。</li>
 * </ul>
 *
 * <p>{@link #wasDirty(String)} 是**零分配**的预扫描：干净文本走 {@link #sanitize(String)}
 * 的快路径（连一次 {@code substring} 都不做，直接返回同一个 String 实例）。
 *
 * <p>与后端的**唯一**差别：后端把"没有非法字符"当作干净并原样返回（连多余空格都不压），
 * 本类把首尾/连续空白也算作"要动"，因此这类文本会被规整成单个空格 —— 只会更干净，不会更脏。
 */
public final class ChatText {

    /** 聊天正文上限（字符=码位）。原版 256，这里留余量。与后端 {@code CHAT_MAX_CHARS} 一致。 */
    public static final int MAX_CHARS = 200;

    /** 染色码：聊天里出现它就是非法字符（也是伪造格式的常见手法）。 */
    private static final char SECTION_SIGN = '\u00A7';

    /** 截断 / 预览用的省略号。 */
    private static final char ELLIPSIS = '\u2026';

    private ChatText() {
    }

    /** 清洗结果：{@code text} 可直接出站；{@code dirty} 表示确实改动了原文（供审计/日志）。 */
    public record Result(String text, boolean dirty, int droppedChars) {
    }

    /**
     * 把一段文本清洗成**能安全发进聊天**的样子（纯函数）。{@code null} 视为空串。
     *
     * <p>原文本来就干净（无非法字符、无首尾/连续空白、不超上限）时，原样返回**同一个实例**
     * （快路径，零复制）；此时 {@code dirty=false}，调用方不必记日志。
     */
    public static Result sanitize(String raw) {
        if (raw == null || raw.isEmpty()) {
            return new Result("", false, 0);
        }
        if (!wasDirty(raw)) {
            return new Result(raw, false, 0);
        }
        StringBuilder out = new StringBuilder(raw.length());
        int dropped = 0;
        boolean pendingSpace = false;
        for (int i = 0; i < raw.length(); i++) {
            char c = raw.charAt(i);
            if (isLineSpace(c)) {
                // 换行/制表/行分隔符 ⇒ 一个空格（行首的空白随后被 pendingSpace 吃掉）
                if (out.length() > 0) {
                    pendingSpace = true;
                }
            } else if (isInvisible(c) || c == SECTION_SIGN || isControl(c)) {
                dropped++;
            } else if (Character.isHighSurrogate(c)) {
                if (i + 1 < raw.length() && Character.isLowSurrogate(raw.charAt(i + 1))) {
                    if (pendingSpace) {
                        out.append(' ');
                        pendingSpace = false;
                    }
                    out.append(c).append(raw.charAt(++i));   // 合法代理对：整个码位留下
                } else {
                    dropped++;                                // 孤立高代理项：坏数据，删
                }
            } else if (Character.isLowSurrogate(c)) {
                dropped++;                                    // 孤立低代理项：删
            } else if (isSpace(c)) {
                if (out.length() > 0) {
                    pendingSpace = true;                      // 其它空白 ⇒ 压成一个空格
                }
            } else {
                if (pendingSpace) {
                    out.append(' ');
                    pendingSpace = false;
                }
                out.append(c);
            }
        }
        String cleaned = out.toString();
        if (codePoints(cleaned) > MAX_CHARS) {
            cleaned = truncate(cleaned, MAX_CHARS);
        }
        return new Result(cleaned, true, dropped);
    }

    /**
     * 指令通道的裁定（与聊天**同一套字符口径**）：只有 {@code sendable=true} 才允许出网。
     *
     * <p>指令**改一个字就是另一条指令**，所以这里比聊天严 —— 三条全过才发：
     * <ol>
     *   <li>{@code dirty=false}：字符防线**一个字都没改**。换行/控制字符/{@code §}/零宽/被压掉的
     *       空白都算"指令内容变了" ⇒ 宁可不动，也不要在服务器上执行一条我们没打算执行的指令；</li>
     *   <li>清洗后**非空**；</li>
     *   <li>清洗后**不以 {@code /} 开头**（那是聊天通道的指令前缀；这条通道已有剥斜杠的一层）。</li>
     * </ol>
     * 否则 {@code sendable=false} ＋ 机器可读 {@code reason}（{@code "empty"} / {@code "modified"} /
     * {@code "leading_slash"}），调用方**如实拒绝**。注意本类**不管**黑名单 —— 那是另一层。
     */
    public record CommandVerdict(String text, boolean sendable, String reason, boolean dirty, int droppedChars) {
    }

    /** 指令通道入口：清洗 + 裁定（见 {@link CommandVerdict}）。{@code null} 视为空串。 */
    public static CommandVerdict sanitizeCommand(String raw) {
        Result r = sanitize(raw);
        String text = r.text();
        if (text.isEmpty()) {
            return new CommandVerdict(text, false, "empty", r.dirty(), r.droppedChars());
        }
        if (r.dirty()) {
            return new CommandVerdict(text, false, "modified", true, r.droppedChars());
        }
        if (text.charAt(0) == '/') {
            return new CommandVerdict(text, false, "leading_slash", false, 0);
        }
        return new CommandVerdict(text, true, "ok", false, 0);
    }

    /**
     * 预扫描：{@code sanitize} 会不会改动这段文本。**零分配**（干净文本不复制字符串）。
     *
     * <p>{@code null} / 空串 ⇒ false。
     */
    public static boolean wasDirty(String raw) {
        if (raw == null || raw.isEmpty()) {
            return false;
        }
        boolean content = false;
        boolean pendingSpace = false;
        int chars = 0;
        for (int i = 0; i < raw.length(); i++) {
            char c = raw.charAt(i);
            if (isLineSpace(c) || isInvisible(c) || c == SECTION_SIGN || isControl(c)) {
                return true;
            }
            if (Character.isHighSurrogate(c)) {
                if (i + 1 >= raw.length() || !Character.isLowSurrogate(raw.charAt(i + 1))) {
                    return true;                              // 孤立高代理项
                }
                i++;
                chars++;
                content = true;
                pendingSpace = false;
                continue;
            }
            if (Character.isLowSurrogate(c)) {
                return true;                                  // 孤立低代理项
            }
            if (isSpace(c)) {
                if (!content || pendingSpace || c != ' ') {
                    return true;                              // 行首空白 / 连续空白 / 非普通空格（NBSP/全角…）
                }
                pendingSpace = true;
                continue;
            }
            content = true;
            pendingSpace = false;
            chars++;
        }
        if (pendingSpace) {
            return true;                                      // 行尾空白
        }
        return chars > MAX_CHARS;
    }

    /**
     * 日志 / 诊断用的短预览：最多 {@code maxChars} 个码位（含省略号），超出加 {@code …}，
     * **绝不切半个代理对**；{@code null} 视为空串。
     */
    public static String preview(String text, int maxChars) {
        if (text == null || text.isEmpty() || maxChars <= 0) {
            return "";
        }
        if (codePoints(text) <= maxChars) {
            return text;
        }
        String cut = head(text, maxChars - 1);
        return stripTrailingSpace(cut) + ELLIPSIS;
    }

    // ── 字符分类：与后端 chat_sanitize.py 的集合逐一对应 ──────────────────

    /** 换行 / 制表 / 行分隔符：**修**成一个空格（别把两句话粘成一句）。 */
    static boolean isLineSpace(char c) {
        return c == '\n' || c == '\r' || c == '\t' || c == '\u000B' || c == '\u000C'
                || c == '\u0085' || c == '\u2028' || c == '\u2029';
    }

    /** 零宽 / 双向控制 / 软连字符：删（不可见，只会被当成非法内容）。 */
    static boolean isInvisible(char c) {
        return (c >= '\u200B' && c <= '\u200F')
                || (c >= '\u202A' && c <= '\u202E')
                || (c >= '\u2060' && c <= '\u2064')
                || c == '\uFEFF'
                || c == '\u00AD';
    }

    /** C0（除已被当成空白的那些）/ C1 / DEL：删。 */
    static boolean isControl(char c) {
        return c < 0x20 || c == 0x7F || (c >= 0x80 && c <= 0x9F);
    }

    /** 其它空白（半角/全角空格、NBSP、各类 Unicode 空白）：压成一个空格。 */
    static boolean isSpace(char c) {
        return Character.isWhitespace(c) || Character.isSpaceChar(c);
    }

    // ── 码位安全的小工具 ─────────────────────────────────────────────

    private static int codePoints(String s) {
        return s.codePointCount(0, s.length());
    }

    /** 取前 {@code count} 个**码位**（count>0 且小于总码位数，调用方保证）——不切代理对。 */
    private static String head(String s, int count) {
        if (count <= 0) {
            return "";
        }
        int end = s.offsetByCodePoints(0, Math.min(count, codePoints(s)));
        return s.substring(0, end);
    }

    private static String stripTrailingSpace(String s) {
        int end = s.length();
        while (end > 0 && isSpace(s.charAt(end - 1))) {
            end--;
        }
        return end == s.length() ? s : s.substring(0, end);
    }

    /** 截断到 {@code limit} 个码位（含省略号），且**不切半个代理对**。 */
    private static String truncate(String text, int limit) {
        if (limit <= 0) {
            return "";
        }
        String cut = head(text, limit - 1);
        if (!cut.isEmpty() && Character.isHighSurrogate(cut.charAt(cut.length() - 1))) {
            cut = cut.substring(0, cut.length() - 1);   // 兜底：绝不留孤立的半个代理对
        }
        return stripTrailingSpace(cut) + ELLIPSIS;
    }
}
