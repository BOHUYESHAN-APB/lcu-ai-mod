package com.lcu.lcumod.chat;

/**
 * 出站文本清洗（防注入）—— **所有** AI 生成、可能进聊天或指令通道的文本都要过这一道。
 *
 * <p>用户口径：「要避免他在主动对话过程当中输出 {@code /} …… 做一个正则防御机制，
 * 防止它输入输出的时候尝试输入指令」。真正的风险在**输出侧**：AI 说的话只要以
 * {@code /} 开头，在聊天通道里就等于**执行指令**。
 *
 * <p>规则（逐行、只看行首）：
 * <ul>
 *   <li>剥掉行首的空白 —— 含半角空格、制表符、全角空格（U+3000）、其它 Unicode 空白；</li>
 *   <li>剥掉行首的斜杠 —— 半角 {@code /}、全角 {@code ／}，且**连续多个**（{@code //}、{@code ///}）一起剥；</li>
 *   <li>行内其余内容**原样保留**（不删字、不折叠空格）。</li>
 * </ul>
 *
 * <p>注意"逐行"是刻意的：单条聊天里不会出现换行，但 HUD 描述词 / 任务 detail 可能是多行的，
 * 而换行在某些插件/服务端上会被拆句 —— 每一行的行首都必须干净。副作用是**多行文本的行首缩进
 * 也会被剥掉**（这是"行首空白一律剥掉"这条规则的字面结果，且只影响格式、不影响语义）。
 *
 * <p>本类**刻意零依赖**（不碰任何 Minecraft / gson 类型），因此可以被 {@code verifyGuard} 直接自检。
 */
public final class TextSanitizer {

    /** 清洗结果：{@code text} 是可直接出站的文本，{@code sanitized} 表示**确实改动了**（供审计）。 */
    public record Result(String text, boolean sanitized, int removedChars, boolean hadLeadingSlash) {
    }

    private TextSanitizer() {
    }

    /**
     * 清洗一段出站文本。{@code null} 视为空串。
     *
     * <p>没有发生任何改动时 {@code sanitized=false}（正常文本走原样，调用方不必记日志）。
     */
    public static Result sanitize(String raw) {
        if (raw == null) {
            return new Result("", false, 0, false);
        }
        if (raw.isEmpty()) {
            return new Result("", false, 0, false);
        }
        StringBuilder out = new StringBuilder(raw.length());
        boolean changed = false;
        boolean slashSeen = false;
        int removed = 0;
        int start = 0;
        while (true) {
            int nl = raw.indexOf('\n', start);
            String line = nl < 0 ? raw.substring(start) : raw.substring(start, nl);

            int p = 0;
            while (p < line.length() && isBlank(line.charAt(p))) {
                p++;
            }
            int q = p;
            while (q < line.length() && isSlash(line.charAt(q))) {
                q++;
            }
            if (q > 0) {
                changed = true;
                removed += q;
                if (q > p) {
                    slashSeen = true;
                }
            }
            out.append(line, q, line.length());
            if (nl < 0) {
                break;
            }
            out.append('\n');
            start = nl + 1;
        }
        return new Result(out.toString(), changed, removed, slashSeen);
    }

    /** 便捷入口：只要清洗后的文本。 */
    public static String clean(String raw) {
        return sanitize(raw).text();
    }

    /** 行首空白：半角/全角空格、制表符及其它 Unicode 空白（含 NBSP）。换行不在其中（调用方已按行切分）。 */
    static boolean isBlank(char c) {
        if (c == '\n' || c == '\r') {
            return false;
        }
        return c == ' ' || c == '\t' || Character.isWhitespace(c) || Character.isSpaceChar(c);
    }

    /** 行首斜杠：半角 {@code /} 与全角 {@code ／}。 */
    static boolean isSlash(char c) {
        return c == '/' || c == '\uFF0F';
    }
}
