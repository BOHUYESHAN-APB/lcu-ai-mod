package com.lcu.lcumod.chat;

/**
 * 聊天消息分类（**纯逻辑、零依赖**，因此可被 {@code verifyChat} 直接自检）。
 *
 * <p>为什么按<b>翻译键</b>而不是渲染后的文本分类：私信的原版聊天行是**本地化文本**
 * （中文「X 悄悄地对你说…」/ 英文「X whispers to you…」），靠正则猜文本要么漏、要么误判；
 * 而聊天消息的 {@code Component} 带**翻译键**（{@code chat.type.text} /
 * {@code commands.message.display.incoming} / {@code commands.message.display.outgoing} …），
 * 翻译键**与语言无关**，这才是可靠判据。
 *
 * <p>优先看**结构**：ChatType 的注册键（{@code chat} / {@code say_command} /
 * {@code msg_command_incoming} / {@code msg_command_outgoing} / {@code emote_command} …）
 * 及其 {@code ChatTypeDecoration} 的翻译键前缀（{@code chat.type.*} /
 * {@code commands.message.display.*}），而不是具体措辞。
 *
 * <p><b>保守原则</b>：解析失败 / 未知键一律 {@link Kind#unknown}，下游把它当**普通公屏**
 * 处理 —— 绝不误判成私信。
 *
 * <p>本类刻意不碰任何 Minecraft / gson 类型。
 */
public final class ChatKind {

    /** 与后端约定的枚举串（{@link #wire}）。 */
    public enum Kind {
        /** 普通公屏（<Player> 消息 / 队伍频道）。 */
        public_text,
        /** 别人私信我（原版 /msg 收到）。 */
        whisper_in,
        /** 我私信别人被服务器回显（原版 /msg 发出）。 */
        whisper_out,
        /** 公告（/say）。 */
        announcement,
        /** 系统消息（无发送者，如死亡/加入）。 */
        system,
        /** 动作（/me）。 */
        emote,
        /** 无法识别 —— 当公屏处理。 */
        unknown
    }

    private ChatKind() {
    }

    /**
     * 按翻译键分类。{@code translationKey} 可以是 ChatType 的注册键
     * （{@code msg_command_incoming}）、也可以是 {@code ChatTypeDecoration} /
     * {@code TranslatableContents} 的翻译键（{@code commands.message.display.incoming}）。
     *
     * @param isSystem 事件本身标记为系统消息（无发送者）时为 {@code true}
     */
    public static Kind classify(String translationKey, boolean isSystem) {
        if (isSystem) {
            return Kind.system;
        }
        String k = normalize(translationKey);
        if (k.isEmpty()) {
            return Kind.unknown;
        }

        // 私信（原版 /msg）—— 结构名，语言无关
        if (k.equals("msg_command_incoming") || k.startsWith("commands.message.display.incoming")) {
            return Kind.whisper_in;
        }
        if (k.equals("msg_command_outgoing") || k.startsWith("commands.message.display.outgoing")) {
            return Kind.whisper_out;
        }

        // 公告（/say）
        if (k.equals("say_command") || k.startsWith("chat.type.announcement")) {
            return Kind.announcement;
        }

        // 动作（/me）
        if (k.equals("emote_command") || k.startsWith("chat.type.emote")) {
            return Kind.emote;
        }

        // 队伍频道（公屏性质，绝非私信）
        if (k.equals("team_msg_command_incoming") || k.equals("team_msg_command_outgoing")
                || k.startsWith("chat.type.team.")) {
            return Kind.public_text;
        }

        // 普通公屏
        if (k.equals("chat") || k.startsWith("chat.type.text")) {
            return Kind.public_text;
        }

        return Kind.unknown;
    }

    /**
     * 多候选键分类：按顺序取第一个"非 {@link Kind#unknown}"的结果；全 unknown 则 unknown。
     *
     * <p>调用方一般依次给出：ChatType 注册键 → ChatTypeDecoration 翻译键 →
     * 消息 Component 的 {@code TranslatableContents} 键。这样任意一条能识别就识别，
     * 全都不认识才落到 unknown（当公屏）。
     */
    public static Kind classifyFirst(boolean isSystem, String... translationKeys) {
        if (isSystem) {
            return Kind.system;
        }
        if (translationKeys != null) {
            for (String key : translationKeys) {
                Kind kind = classify(key, false);
                if (kind != Kind.unknown) {
                    return kind;
                }
            }
        }
        return Kind.unknown;
    }

    /** 是否"别人私信我"。 */
    public static boolean isWhisperIn(Kind kind) {
        return kind == Kind.whisper_in;
    }

    /** 是否"我私信别人（回显）"。 */
    public static boolean isWhisperOut(Kind kind) {
        return kind == Kind.whisper_out;
    }

    /** 是否是私信（进/出）。 */
    public static boolean isWhisper(Kind kind) {
        return kind == Kind.whisper_in || kind == Kind.whisper_out;
    }

    /** 上报给后端的枚举串。 */
    public static String wire(Kind kind) {
        return kind.name();
    }

    private static String normalize(String key) {
        if (key == null) {
            return "";
        }
        String s = key.trim();
        if (s.startsWith("minecraft:")) {
            s = s.substring("minecraft:".length());
        }
        return s;
    }
}
