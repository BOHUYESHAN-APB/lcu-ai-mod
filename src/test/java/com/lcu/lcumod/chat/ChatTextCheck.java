package com.lcu.lcumod.chat;

import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import java.util.stream.Stream;

/**
 * 零依赖自检（替代 JUnit）：{@link ChatText} 的字符防线语义 + "防线还在不在"的静态扫描。
 *
 * <p>真机事故（2026-10-12 16:22）：AI 发出去的聊天带换行 ⇒ 服务器把玩家踢出。
 * 这里把 {@code ChatText.sanitize} 的每一条口径钉死（换行→空格 / 控制/零宽/双向控制/
 * 软连字符/孤立代理项/§→删 / 压空白 / 200 码位截断不切代理对 / 干净文本原样快路径 + 幂等），
 * 并且**静态扫描** {@code ActionExecutor.handleSendChat}（聊天）与 {@code handleRunCommand}（指令）
 * 必须挂着这道防线、全仓没有绕过它的发送点 —— 防止以后被"顺手删掉"。
 *
 * 断言信息用 ASCII（Windows 控制台是 GBK）。失败非零退出。
 */
public final class ChatTextCheck {

    private static int passed = 0;
    private static int failed = 0;

    private static final String EMOJI = "\uD83D\uDE00";   // U+1F600，一个码位/两个 char

    private static final String MAIN_ROOT = "src/main/java/com/lcu/lcumod";
    private static final String ACTION_EXECUTOR =
            MAIN_ROOT + "/action/ActionExecutor.java";

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

        incidentChecks();
        characterClassChecks();
        cleanFastPathChecks();
        limitChecks();
        previewChecks();
        commandChannelChecks();
        sourceScanChecks();

        System.out.println("[ChatTextCheck] " + passed + " checks passed, " + failed + " failed");
        return new int[] { passed, failed };
    }

    // ── 1. 真机回归：带换行的那句话 ──────────────────────────────────

    private static void incidentChecks() {
        // 事故现场那句话（两句话被一个 \n 拼在一起）
        ChatText.Result r = ChatText.sanitize("(no execution)\n(unknown base)");
        check("incident: no newline survives", !r.text().contains("\n"));
        check("incident: two sentences stay separate (one space)",
                r.text().equals("(no execution) (unknown base)"));
        check("incident: reported as dirty", r.dirty());

        // 原句（全角括号 / 省略号）照原样走一遍
        String real = "（没有执行……）\n（不知道是哪个基地…）";
        String cleaned = ChatText.sanitize(real).text();
        check("incident-real: no newline survives", !cleaned.contains("\n"));
        check("incident-real: both halves kept",
                cleaned.startsWith("（没有执行") && cleaned.endsWith("）"));
        check("incident-real: separated by exactly one space", cleaned.contains("） （"));

        // 危险的换行变体：CRLF / 单独 CR / 行分隔符 / NEL / 垂直制表 / 换页
        check("incident: CRLF -> one space", ChatText.sanitize("a\r\nb").text().equals("a b"));
        check("incident: CR -> one space", ChatText.sanitize("a\rb").text().equals("a b"));
        check("incident: line separator -> one space", ChatText.sanitize("a\u2028b").text().equals("a b"));
        check("incident: paragraph separator -> one space", ChatText.sanitize("a\u2029b").text().equals("a b"));
        check("incident: NEL -> one space", ChatText.sanitize("a\u0085b").text().equals("a b"));
        check("incident: vertical tab -> one space", ChatText.sanitize("a\u000Bb").text().equals("a b"));
        check("incident: form feed -> one space", ChatText.sanitize("a\u000Cb").text().equals("a b"));
        check("incident: newline run collapses to one space",
                ChatText.sanitize("a \n\t b").text().equals("a b"));
    }

    // ── 2. 删 / 留：§、零宽、双向控制、软连字符、控制字符、孤立代理项 ──

    private static void characterClassChecks() {
        check("strike: section sign removed", ChatText.sanitize("a\u00A7cb").text().equals("acb"));
        check("strike: color code stripped whole", ChatText.sanitize("\u00A7lred").text().equals("lred"));
        check("strike: zero space removed", ChatText.sanitize("a\u200Bb").text().equals("ab"));
        check("strike: ZWJ/ZWNJ/RLM range removed",
                ChatText.sanitize("a\u200C\u200D\u200E\u200Fb").text().equals("ab"));
        check("strike: bidi override removed", ChatText.sanitize("a\u202Eb").text().equals("ab"));
        check("strike: word joiner removed", ChatText.sanitize("a\u2060b").text().equals("ab"));
        check("strike: BOM removed", ChatText.sanitize("a\uFEFFb").text().equals("ab"));
        check("strike: soft hyphen removed", ChatText.sanitize("soft\u00ADhyphen").text().equals("softhyphen"));
        check("strike: C0 controls removed", ChatText.sanitize("a\u0000\u0001\u001Fb").text().equals("ab"));
        check("strike: DEL removed", ChatText.sanitize("a\u007Fb").text().equals("ab"));
        check("strike: C1 controls removed", ChatText.sanitize("a\u0080\u009Fb").text().equals("ab"));
        check("strike: tab -> space (not deleted)", ChatText.sanitize("a\tb").text().equals("a b"));
        check("strike: lone high surrogate removed",
                ChatText.sanitize("a" + '\uD83D' + "b").text().equals("ab"));
        check("strike: lone low surrogate removed",
                ChatText.sanitize("a" + '\uDE00' + "b").text().equals("ab"));
        check("strike: valid surrogate pair kept", ChatText.sanitize("a" + EMOJI + "b").text().equals("a" + EMOJI + "b"));
        check("strike: dropped count is honest",
                ChatText.sanitize("a\u0000\u00A7\u200B b").droppedChars() == 3);
        check("strike: only illegal chars -> blank result", ChatText.sanitize("\u0000\u00A7\u200B").text().isEmpty());
        check("strike: only whitespace -> blank result", ChatText.sanitize(" \t\n ").text().isEmpty());
        check("strike: nbsp collapses to one space", ChatText.sanitize("a\u00A0\u00A0b").text().equals("a b"));
        check("strike: single interior nbsp normalized", ChatText.sanitize("a\u00A0b").text().equals("a b"));
        check("strike: single interior nbsp flagged dirty", ChatText.wasDirty("a\u00A0b"));
        check("strike: fullwidth space collapses to one space", ChatText.sanitize("a\u3000b").text().equals("a b"));
        check("strike: single interior fullwidth space flagged dirty", ChatText.wasDirty("a\u3000b"));
        check("strike: leading/trailing blank trimmed", ChatText.sanitize("  a b \t ").text().equals("a b"));
        check("strike: leading nbsp trimmed", ChatText.sanitize("\u00A0a").text().equals("a"));
    }

    // ── 3. 干净文本：快路径 + 幂等 ──────────────────────────────────

    private static void cleanFastPathChecks() {
        String clean = "hello world";
        ChatText.Result r = ChatText.sanitize(clean);
        check("clean: text unchanged", r.text().equals(clean));
        check("clean: not dirty", !r.dirty());
        check("clean: zero dropped", r.droppedChars() == 0);
        check("clean: fast path returns the same instance (zero copy)", r.text() == clean);
        check("clean: idempotent", ChatText.sanitize(r.text()).text().equals(clean));

        String cn = "你好，世界！基地在 (12, 64, -30) —— 一切都好。";
        check("clean-cn: unchanged", ChatText.sanitize(cn).text().equals(cn));
        check("clean-cn: not dirty", !ChatText.sanitize(cn).dirty());
        check("clean-cn: fast path same instance", ChatText.sanitize(cn).text() == cn);
        check("clean: same instance on the second pass too", ChatText.sanitize(cn).text() == ChatText.sanitize(cn).text());

        check("clean: internal single space kept as-is", ChatText.sanitize("a b c").text().equals("a b c"));
        check("clean: valid emoji pair not dirty", !ChatText.sanitize("hi " + EMOJI + "!").dirty());

        check("null -> empty, not dirty",
                ChatText.sanitize(null).text().isEmpty() && !ChatText.sanitize(null).dirty());
        check("empty -> empty, not dirty",
                ChatText.sanitize("").text().isEmpty() && !ChatText.sanitize("").dirty());
        check("null not dirty (predicate)", !ChatText.wasDirty(null));
        check("predicate agrees with sanitize on dirty text", ChatText.wasDirty("a\nb"));
        check("predicate agrees with sanitize on clean text", !ChatText.wasDirty("a b"));

        // 洗过一次之后再洗：不动（幂等），且第二次走快路径
        String washed = ChatText.sanitize("（甲）\n（乙）\u00A7").text();
        ChatText.Result again = ChatText.sanitize(washed);
        check("idempotent after cleaning", again.text().equals(washed) && !again.dirty());
        check("idempotent: second pass is the same instance", again.text() == washed);
    }

    // ── 4. 上限：200 码位截断，不切代理对 ────────────────────────────

    private static void limitChecks() {
        String exactly = "a".repeat(ChatText.MAX_CHARS);
        check("limit: exactly 200 chars kept", ChatText.sanitize(exactly).text().equals(exactly));
        check("limit: exactly 200 chars not dirty", !ChatText.sanitize(exactly).dirty());
        check("limit constant is 200", ChatText.MAX_CHARS == 200);

        ChatText.Result over = ChatText.sanitize("a".repeat(201));
        check("limit: 201 chars truncated to 200", over.text().length() == 200);
        check("limit: truncated form is 199 a + ellipsis",
                over.text().equals("a".repeat(199) + "\u2026"));
        check("limit: truncation is dirty", over.dirty());
        check("limit: truncated result is idempotent", ChatText.sanitize(over.text()).text().equals(over.text()));

        // 码位计数：199 个 'a' + 一个 emoji = 200 码位（201 个 char）⇒ 不该截
        String astral200 = "a".repeat(199) + EMOJI;
        check("limit: 200 code points with an emoji kept whole",
                ChatText.sanitize(astral200).text().equals(astral200)
                        && !ChatText.sanitize(astral200).dirty());

        // 截断边界正好落在代理对中间：必须整个 emoji 留下、不出现半个
        String astralOver = "a".repeat(198) + EMOJI + "b".repeat(10);
        String cut = ChatText.sanitize(astralOver).text();
        check("limit: astral char at the boundary kept whole",
                cut.equals("a".repeat(198) + EMOJI + "\u2026"));
        check("limit: truncated result never holds a lone surrogate", !hasLoneSurrogate(cut));
        check("limit: truncated result is 200 code points", codePoints(cut) == ChatText.MAX_CHARS);

        // 违禁字符 + 超长：先洗后截，顺序不影响结果
        ChatText.Result both = ChatText.sanitize("a".repeat(300) + "\n" + "\u00A7");
        check("limit: cleaning happens before truncation",
                both.text().equals("a".repeat(199) + "\u2026") && both.dirty());
    }

    // ── 5. 日志预览：绝不切半个代理对 ───────────────────────────────

    private static void previewChecks() {
        check("preview: short text kept as-is", ChatText.preview("abc", 60).equals("abc"));
        check("preview: null -> empty", ChatText.preview(null, 60).isEmpty());
        check("preview: non-positive budget -> empty", ChatText.preview("abc", 0).isEmpty());
        String p = ChatText.preview("a".repeat(1000), 10);
        check("preview: bounded", codePoints(p) == 10 && p.endsWith("\u2026"));
        String pe = ChatText.preview("a".repeat(8) + EMOJI + "b".repeat(5), 10);
        check("preview: never splits a surrogate pair",
                pe.equals("a".repeat(8) + EMOJI + "\u2026") && !hasLoneSurrogate(pe));
    }

    // ── 6. 指令通道：清洗一个字都不许改，改了就拒发 ──────────────────

    private static void commandChannelChecks() {
        ChatText.CommandVerdict ok = ChatText.sanitizeCommand("give @p minecraft:stone 64");
        check("cmd: clean command is sendable", ok.sendable() && ok.reason().equals("ok"));
        check("cmd: clean command kept verbatim", ok.text().equals("give @p minecraft:stone 64") && !ok.dirty());

        ChatText.CommandVerdict nl = ChatText.sanitizeCommand("say a\nb");
        check("cmd: newline in a command is refused", !nl.sendable());
        check("cmd: newline refusal reason is modified", nl.reason().equals("modified"));
        check("cmd: newline refusal exposes the cleaned form (never sent as-is)", nl.text().equals("say a b"));
        check("cmd: section sign in a command is refused",
                ChatText.sanitizeCommand("say \u00A7c hi").reason().equals("modified"));
        check("cmd: control char in a command is refused",
                !ChatText.sanitizeCommand("say a\u0007b").sendable());
        check("cmd: refusal reports the dropped count", ChatText.sanitizeCommand("say \u0007x").droppedChars() == 1);
        check("cmd: bidi control in a command is refused", !ChatText.sanitizeCommand("say \u202Ex").sendable());
        check("cmd: double space refused (a command is never rewritten)",
                ChatText.sanitizeCommand("say  hi").reason().equals("modified"));
        check("cmd: trailing blank refused", ChatText.sanitizeCommand("say hi ").reason().equals("modified"));

        check("cmd: bare slash refused", ChatText.sanitizeCommand("/").reason().equals("leading_slash"));
        check("cmd: bare slash is not sendable", !ChatText.sanitizeCommand("/").sendable());
        check("cmd: invisible-only command -> empty",
                ChatText.sanitizeCommand("\u0000\u200B").reason().equals("empty"));
        check("cmd: empty command -> empty", ChatText.sanitizeCommand("").reason().equals("empty"));
        ChatText.CommandVerdict nul = ChatText.sanitizeCommand(null);
        check("cmd: null command -> empty and not sendable",
                nul.reason().equals("empty") && !nul.sendable() && nul.text().isEmpty());
        check("cmd: blacklist is a different layer (kill passes the char filter)",
                ChatText.sanitizeCommand("kill @a").sendable());
    }

    // ── 7. 静态扫描：防线还在不在，全仓有没有绕过它的聊天发送点 ────────

    private static void sourceScanChecks() {
        Path executor = Path.of("src", "main", "java", "com", "lcu", "lcumod", "action", "ActionExecutor.java");
        if (!Files.isRegularFile(executor)) {
            check("scan: ActionExecutor found (cwd=" + Path.of(".").toAbsolutePath() + ")", false);
            return;
        }
        String src = read(executor);
        String body = methodBody(src, "private void handleSendChat(WireServer.WireCommand cmd)");
        check("scan: handleSendChat found", !body.isEmpty());
        check("scan: handleSendChat goes through ChatText.sanitize", body.contains("ChatText.sanitize("));
        check("scan: handleSendChat sends via the vanilla chat channel",
                body.contains("mc.player.connection.sendChat("));
        int guard = body.indexOf("ChatText.sanitize(");
        int send = body.indexOf("mc.player.connection.sendChat(");
        check("scan: the guard runs before the send", guard > 0 && send > guard);
        check("scan: exactly one chat send in ActionExecutor",
                countOf(src, "connection.sendChat(") == 1);
        check("scan: handleSendChat keeps the slash/leading-blank defense too",
                body.contains("TextSanitizer.sanitize("));

        // 指令通道：同一条防线，但更严 —— 清洗一个字都没改才发，改了就拒发（分支必须在 sendCommand 之前）
        String run = methodBody(src, "private void handleRunCommand(WireServer.WireCommand cmd)");
        check("scan: handleRunCommand found", !run.isEmpty());
        check("scan: handleRunCommand keeps the hard blacklist layer", run.contains("CommandBlacklist.check("));
        check("scan: handleRunCommand goes through the char filter", run.contains("ChatText.sanitizeCommand("));
        int cmdGuard = run.indexOf("ChatText.sanitizeCommand(");
        int cmdSend = run.indexOf("connection.sendCommand(");
        check("scan: the char filter runs before sendCommand", cmdGuard > 0 && cmdSend > cmdGuard);
        int refusal = run.indexOf("if (!text.sendable())");
        check("scan: handleRunCommand has a post-clean refusal branch",
                refusal > 0 && refusal < cmdSend
                        && run.substring(refusal, cmdSend).contains("sendResponse(cmd.id(), false"));
        check("scan: the refusal branch returns before sending",
                refusal > 0 && cmdSend > refusal && run.substring(refusal, cmdSend).contains("return;"));
        check("scan: the command goes out only through the verdict", run.contains("sendCommand(text.text())"));
        check("scan: exactly one command send in ActionExecutor",
                countOf(src, "connection.sendCommand(") == 1);

        // 唯一出口：全仓**不许**有别的聊天发送点绕过 ChatText（{@code Body.sendChat} 是线协议
        // 出口的声明，调用方必须先过 ChatText —— 今天全仓只有 handleSendChat 一个真实发送点）。
        List<String> senders = new ArrayList<>();
        List<String> bypass = new ArrayList<>();
        List<String> cmdSenders = new ArrayList<>();
        try (Stream<Path> files = Files.walk(Path.of("src", "main", "java", "com", "lcu", "lcumod"))) {
            files.filter(p -> p.toString().endsWith(".java")).forEach(p -> {
                String text = read(p);
                if (text.contains(".sendCommand(")) {
                    cmdSenders.add(p.getFileName().toString());
                }
                if (!text.contains(".sendChat(")) {   // 真实调用（不含 Body 的接口声明）
                    return;
                }
                senders.add(p.getFileName().toString());
                if (!text.contains("ChatText")) {
                    bypass.add(p.getFileName().toString());
                }
            });
        } catch (Exception ex) {
            check("scan: main sources readable", false);
            return;
        }
        check("scan: only one chat sender in main (" + senders + ")",
                senders.size() == 1 && senders.get(0).equals("ActionExecutor.java"));
        check("scan: no chat sender bypasses ChatText (" + bypass + ")", bypass.isEmpty());
        check("scan: only one command sender in main (" + cmdSenders + ")",
                cmdSenders.size() == 1 && cmdSenders.get(0).equals("ActionExecutor.java"));
    }

    // ── helpers ─────────────────────────────────────────────────────

    /** 取某个成员（含签名行）到下一个成员声明之间的源码片段（静态扫描用）。 */
    private static String methodBody(String source, String signature) {
        int start = source.indexOf(signature);
        if (start < 0) {
            return "";
        }
        int after = start + signature.length();
        int end = source.indexOf("\n    private ", after);
        int pub = source.indexOf("\n    public ", after);
        if (end < 0 || (pub >= 0 && pub < end)) {
            end = pub;
        }
        if (end < 0) {
            end = source.length();
        }
        return source.substring(start, end);
    }

    private static int countOf(String haystack, String needle) {
        int count = 0;
        int from = 0;
        while (true) {
            int idx = haystack.indexOf(needle, from);
            if (idx < 0) {
                return count;
            }
            count++;
            from = idx + needle.length();
        }
    }

    private static int codePoints(String s) {
        return s.codePointCount(0, s.length());
    }

    private static boolean hasLoneSurrogate(String s) {
        for (int i = 0; i < s.length(); i++) {
            char c = s.charAt(i);
            if (Character.isHighSurrogate(c)) {
                if (i + 1 >= s.length() || !Character.isLowSurrogate(s.charAt(i + 1))) {
                    return true;
                }
                i++;
            } else if (Character.isLowSurrogate(c)) {
                return true;
            }
        }
        return false;
    }

    private static String read(Path file) {
        try {
            return Files.readString(file, StandardCharsets.UTF_8);
        } catch (Exception ex) {
            return "";
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
