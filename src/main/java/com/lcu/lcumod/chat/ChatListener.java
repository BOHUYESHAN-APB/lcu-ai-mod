package com.lcu.lcumod.chat;

import com.google.gson.JsonObject;
import com.lcu.lcumod.LCUMod;
import net.minecraft.client.Minecraft;
import net.minecraft.network.chat.Component;
import net.minecraft.network.chat.contents.TranslatableContents;
import net.neoforged.bus.api.SubscribeEvent;
import net.neoforged.fml.common.EventBusSubscriber;
import net.neoforged.neoforge.client.event.ClientChatReceivedEvent;

/**
 * Captures ALL chat messages received by the CLIENT and forwards them to backend.
 * Uses ClientChatReceivedEvent (fires on client, works on multiplayer servers).
 *
 * Player messages arrive as ClientChatReceivedEvent.Player (isSystem=false).
 * System messages arrive as ClientChatReceivedEvent.System (isSystem=true).
 * We forward ALL messages and let the Python backend sort them.
 */
@EventBusSubscriber(modid = LCUMod.MODID, value = net.neoforged.api.distmarker.Dist.CLIENT)
public class ChatListener {

    private static int msgCount = 0;

    /** 最近发话者（仪表盘显示"它在听谁"，后端据此判断语境）。 */
    private static volatile String lastSpeakerName = "";
    private static volatile long lastSpeakerAt = 0L;

    public static String lastSpeakerName() {
        return lastSpeakerName;
    }

    public static long lastSpeakerAt() {
        return lastSpeakerAt;
    }

    @SubscribeEvent
    public static void onClientChat(ClientChatReceivedEvent event) {
        try {
            if (LCUMod.WIRE == null || !LCUMod.WIRE.isConnected()) return;

            // Skip system messages (death, teleport, etc.) to avoid backend noise
            if (event.isSystem()) return;

            Component msgComponent = event.getMessage();
            if (msgComponent == null) return;
            String message = msgComponent.getString();
            if (message == null || message.isEmpty()) return;

            // 只丢弃"我们刚刚自己发出去的那条"（避免 AI 对着自己的回声自问自答）。
            // 历史 bug：这里按"发送者 == 本地玩家名"整类过滤 → 用户**在同一个窗口**里打的字
            // 也被吃掉，表现为"在游戏里发消息，AI 从来没收到"（后端一个字节都没有）。
            if (OutboundChatLog.consume(stripSenderText(message))) return;

            ChatKind.Kind kind = classifyEvent(event);

            msgCount++;
            LCUMod.LOGGER.info("[Chat] #{} received kind={}", msgCount, ChatKind.wire(kind));

            String resolvedSender;
            boolean senderUnknown = false;
            if (kind == ChatKind.Kind.whisper_out) {
                // 服务器把"我发出去的私信"回显：说话的是我自己，不能编出别人的名字
                resolvedSender = "";
            } else if (kind == ChatKind.Kind.whisper_in) {
                // 私信我的人 —— 绝不是 "me"；拿不到名字就留空 + sender_unknown
                resolvedSender = whisperSenderName(event);
                senderUnknown = resolvedSender.isEmpty();
            } else {
                resolvedSender = resolveSender(event);
            }

            // 记住"最近谁在说话"（仪表盘要显示"它在听谁"，守卫要按发言瞬间的坐标执行）
            // whisper_out 是自己在说，不覆盖"在听谁"
            if (kind != ChatKind.Kind.whisper_out) {
                lastSpeakerName = resolvedSender;
                lastSpeakerAt = System.currentTimeMillis();
            }

            JsonObject data = new JsonObject();
            data.addProperty("sender", resolvedSender);
            data.addProperty("uuid", event.getSender() != null ? event.getSender().toString() : "");
            data.addProperty("message", message);
            data.addProperty("is_system", event.isSystem());
            data.addProperty("type", event.isSystem() ? "system_chat" : "player_chat");
            // 私信判据是**翻译键**（语言无关）：chat_kind 见 ChatKind；
            // whisper 仅对"别人私信我"为 true —— whisper_out（自己的回显）不算有人在跟我说话。
            data.addProperty("chat_kind", ChatKind.wire(kind));
            data.addProperty("whisper", kind == ChatKind.Kind.whisper_in);
            if (senderUnknown) {
                data.addProperty("sender_unknown", true);
            }

            // 发言那一瞬间的发送者坐标：守卫("在这儿守着")/找人 都要用它，而不是 AI 自己的位置
            var level = Minecraft.getInstance().level;
            if (level != null) {
                for (var other : level.players()) {
                    if (other.getName().getString().equals(resolvedSender)) {
                        data.addProperty("sender_x", other.getX());
                        data.addProperty("sender_y", other.getY());
                        data.addProperty("sender_z", other.getZ());
                        var self = Minecraft.getInstance().player;
                        if (self != null) {
                            data.addProperty("sender_distance", self.distanceTo(other));
                        }
                        break;
                    }
                }
            }

            LCUMod.WIRE.sendEvent("player_chat", data);
        } catch (Exception e) {
            LCUMod.LOGGER.error("[Chat] Error processing chat: {}", e.getMessage());
            // NEVER let exceptions escape — they crash the render thread → disconnect
        }
    }

    /** 从 "<Player> message" / "Player: message" 里取出正文（用于与自己发出的消息比对）。 */
    private static String stripSenderText(String raw) {
        if (raw == null) return "";
        int bracketEnd = raw.indexOf('>');
        if (raw.startsWith("<") && bracketEnd > 0 && bracketEnd < 64) {
            return raw.substring(bracketEnd + 1).trim();
        }
        int colonIdx = raw.indexOf(':');
        if (colonIdx > 0 && colonIdx < 32) {
            return raw.substring(colonIdx + 1).trim();
        }
        return raw.trim();
    }

    private static String resolveSender(ClientChatReceivedEvent event) {
        // If it's a Player event, try to get the real sender name
        if (!event.isSystem()) {
            var raw = event.getMessage().getString();
            // Player chat format: "<PlayerName> message" or just "PlayerName: message"
            int bracketEnd = raw.indexOf('>');
            if (raw.startsWith("<") && bracketEnd > 0) {
                return raw.substring(1, bracketEnd).trim();
            }
            int colonIdx = raw.indexOf(':');
            if (colonIdx > 0) {
                return raw.substring(0, colonIdx).trim();
            }
        }
        return event.getSender() != null ? event.getSender().toString() : "system";
    }

    /**
     * 分类一次收到的聊天。候选键按「结构优先」顺序：
     * <ol>
     *   <li>ChatType 注册键（{@code chat} / {@code say_command} / {@code msg_command_incoming} …）—— 语言无关的结构；</li>
     *   <li>ChatTypeDecoration 的翻译键（{@code chat.type.text} / {@code commands.message.display.incoming} …）；</li>
     *   <li>消息组件里 {@code TranslatableContents} 的翻译键。</li>
     * </ol>
     * 任意一条能识别就识别；全不认识 → unknown（下游当公屏）。
     */
    static ChatKind.Kind classifyEvent(ClientChatReceivedEvent event) {
        java.util.List<String> keys = new java.util.ArrayList<>(3);
        var bound = event.getBoundChatType();
        if (bound != null) {
            try {
                bound.chatType().unwrapKey().ifPresent(rk -> keys.add(rk.identifier().getPath()));
            } catch (Exception ignored) {
                // 结构读取失败不致命 —— 退化到翻译键
            }
            try {
                String decoKey = bound.chatType().value().chat().translationKey();
                if (decoKey != null && !decoKey.isEmpty()) {
                    keys.add(decoKey);
                }
            } catch (Exception ignored) {
                // 同上
            }
        }
        String componentKey = firstTranslationKey(event.getMessage());
        if (componentKey != null && !componentKey.isEmpty()) {
            keys.add(componentKey);
        }
        return ChatKind.classifyFirst(event.isSystem(), keys.toArray(new String[0]));
    }

    /** 递归找消息里第一个 {@code TranslatableContents} 的翻译键（键与语言无关）。 */
    static String firstTranslationKey(Component component) {
        if (component == null) return null;
        try {
            if (component.getContents() instanceof TranslatableContents tc) {
                String key = tc.getKey();
                if (key != null && !key.isEmpty()) return key;
            }
            // 装饰后的消息是单个 TranslatableContents，但保守起见也走一遍 sibling
            for (Component sibling : component.getSiblings()) {
                String key = firstTranslationKey(sibling);
                if (key != null) return key;
            }
        } catch (Exception ignored) {
            // 拿不到就当没有
        }
        return null;
    }

    /**
     * 取「私信我的人」的名字（**不是** "me"）：优先 {@code ChatType.Bound.name()}（发送者显示名），
     * 退化到消息组件的第一个参数 —— 原版 {@code incomingDirectMessage} 的参数顺序是 [SENDER, CONTENT]。
     * 拿不到就返回空串，由调用方标 {@code sender_unknown}（不编名字）。
     */
    static String whisperSenderName(ClientChatReceivedEvent event) {
        var bound = event.getBoundChatType();
        if (bound != null) {
            try {
                Component name = bound.name();
                if (name != null) {
                    String s = name.getString();
                    if (s != null && !s.isBlank()) return s.trim();
                }
            } catch (Exception ignored) {
                // 退化到参数
            }
        }
        Component msg = event.getMessage();
        if (msg != null) {
            try {
                if (msg.getContents() instanceof TranslatableContents tc) {
                    Object[] args = tc.getArgs();
                    if (args != null && args.length >= 1 && args[0] instanceof Component first) {
                        String s = first.getString();
                        if (s != null && !s.isBlank()) return s.trim();
                    }
                }
            } catch (Exception ignored) {
                // 拿不到就当空
            }
        }
        return "";
    }
}
