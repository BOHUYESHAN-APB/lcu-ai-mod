package com.lcu.lcumod.chat;

/**
 * 零依赖自检（替代 JUnit）：{@link ChatKind} 的聊天分类语义。
 *
 * 锁定几条要害：
 * <ul>
 *   <li>原版各翻译键 / ChatType 注册键 → 正确分类；</li>
 *   <li>未知键一律 unknown（下游当**公屏**，绝不误判成私信）；</li>
 *   <li>whisper_out（自己私信被回显）**不能**被归为 incoming；</li>
 *   <li>分类看**翻译键**而非渲染文本 —— 中文/英文两种本地化文本走同一个键；</li>
 *   <li>AI 自己公屏回显仍按 OutboundChatLog 现有规则丢弃。</li>
 * </ul>
 *
 * 断言信息用 ASCII（Windows 控制台是 GBK）。失败非零退出。
 */
public final class ChatKindCheck {

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

        ChatKind.Kind pub = ChatKind.Kind.public_text;
        ChatKind.Kind in = ChatKind.Kind.whisper_in;
        ChatKind.Kind out = ChatKind.Kind.whisper_out;
        ChatKind.Kind ann = ChatKind.Kind.announcement;
        ChatKind.Kind emo = ChatKind.Kind.emote;
        ChatKind.Kind sys = ChatKind.Kind.system;
        ChatKind.Kind unk = ChatKind.Kind.unknown;

        // 1) 原版翻译键（ChatTypeDecoration / TranslatableContents 的键）
        check("key chat.type.text -> public_text", ChatKind.classify("chat.type.text", false) == pub);
        check("key chat.type.text.narrate -> public_text", ChatKind.classify("chat.type.text.narrate", false) == pub);
        check("key commands.message.display.incoming -> whisper_in",
                ChatKind.classify("commands.message.display.incoming", false) == in);
        check("key commands.message.display.outgoing -> whisper_out",
                ChatKind.classify("commands.message.display.outgoing", false) == out);
        check("key chat.type.announcement -> announcement",
                ChatKind.classify("chat.type.announcement", false) == ann);
        check("key chat.type.emote -> emote", ChatKind.classify("chat.type.emote", false) == emo);
        check("key chat.type.team.text -> public_text", ChatKind.classify("chat.type.team.text", false) == pub);
        check("key chat.type.team.sent -> public_text", ChatKind.classify("chat.type.team.sent", false) == pub);

        // 2) ChatType 注册键（结构优先，语言无关）
        check("registry chat -> public_text", ChatKind.classify("chat", false) == pub);
        check("registry say_command -> announcement", ChatKind.classify("say_command", false) == ann);
        check("registry msg_command_incoming -> whisper_in", ChatKind.classify("msg_command_incoming", false) == in);
        check("registry msg_command_outgoing -> whisper_out", ChatKind.classify("msg_command_outgoing", false) == out);
        check("registry emote_command -> emote", ChatKind.classify("emote_command", false) == emo);
        check("registry team_msg_command_incoming -> public_text",
                ChatKind.classify("team_msg_command_incoming", false) == pub);
        check("minecraft: namespace stripped", ChatKind.classify("minecraft:msg_command_incoming", false) == in);

        // 3) 未知键 -> unknown（当公屏）
        check("unknown custom key -> unknown", ChatKind.classify("custom.something", false) == unk);
        check("multiplayer.player.joined -> unknown", ChatKind.classify("multiplayer.player.joined", false) == unk);
        check("death.attack.generic -> unknown", ChatKind.classify("death.attack.generic", false) == unk);
        check("chat.type.admin (op) -> unknown", ChatKind.classify("chat.type.admin", false) == unk);
        check("null key -> unknown", ChatKind.classify(null, false) == unk);
        check("empty key -> unknown", ChatKind.classify("", false) == unk);
        check("blank key -> unknown", ChatKind.classify("   ", false) == unk);

        // 4) system 标记优先
        check("system flag wins over whisper key",
                ChatKind.classify("commands.message.display.incoming", true) == sys);
        check("system flag on unknown key", ChatKind.classify("whatever", true) == sys);

        // 5) whisper_out 绝不能被当成 incoming
        check("whisper_out is not whisper_in", ChatKind.classify("commands.message.display.outgoing", false) != in);
        check("isWhisperIn(outgoing) false", !ChatKind.isWhisperIn(out));
        check("isWhisperOut(outgoing) true", ChatKind.isWhisperOut(out));
        check("isWhisper(incoming) true", ChatKind.isWhisper(in));
        check("isWhisper(public_text) false", !ChatKind.isWhisper(pub));
        check("isWhisperIn(public_text) false", !ChatKind.isWhisperIn(pub));

        // 6) 键优先于文本：中文/英文两种本地化文本走同一个键
        String en = "Alex whispers to you: hi";
        String zh = "Alex 悄悄地对你说：hi";
        check("zh/en localized texts differ", !en.equals(zh));
        check("en sample carries 'whispers'", en.contains("whispers"));
        check("zh sample has no 'whisper' (text regex needs per-language table)", !zh.contains("whisper"));
        check("key-driven: incoming key -> whisper_in (text ignored)",
                ChatKind.classify("commands.message.display.incoming", false) == in);
        check("key-driven: public key -> public_text (text ignored)",
                ChatKind.classify("chat.type.text", false) == pub);

        // 7) classifyFirst（多候选键：结构 -> 装饰键 -> 组件键）
        check("classifyFirst picks first non-unknown",
                ChatKind.classifyFirst(false, "custom.x", "msg_command_incoming") == in);
        check("classifyFirst prefers first known",
                ChatKind.classifyFirst(false, "chat", "commands.message.display.incoming") == pub);
        check("classifyFirst all unknown -> unknown",
                ChatKind.classifyFirst(false, "a", "b") == unk);
        check("classifyFirst ignores nulls", ChatKind.classifyFirst(false, null, "say_command") == ann);
        check("classifyFirst system short-circuit",
                ChatKind.classifyFirst(true, "msg_command_incoming") == sys);

        // 8) 上报串
        check("wire(whisper_in)", "whisper_in".equals(ChatKind.wire(in)));
        check("wire(whisper_out)", "whisper_out".equals(ChatKind.wire(out)));
        check("wire(public_text)", "public_text".equals(ChatKind.wire(pub)));
        check("wire(unknown)", "unknown".equals(ChatKind.wire(unk)));

        // 9) AI 自己公屏回显仍按现有规则丢弃（分类之前就被 OutboundChatLog 吃掉）
        OutboundChatLog.record("AI 已到达");
        check("own public echo still dropped", OutboundChatLog.consume("AI 已到达"));
        check("own public echo consumed once only", !OutboundChatLog.consume("AI 已到达"));
        check("user typed text still passes", !OutboundChatLog.consume("回来"));

        System.out.println("[ChatKindCheck] " + passed + " checks passed, " + failed + " failed");
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
