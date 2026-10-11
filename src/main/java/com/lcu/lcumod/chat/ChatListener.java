package com.lcu.lcumod.chat;

import com.google.gson.JsonObject;
import com.lcu.lcumod.LCUMod;
import net.minecraft.client.Minecraft;
import net.minecraft.network.chat.Component;
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

            msgCount++;
            LCUMod.LOGGER.info("[Chat] #{} received", msgCount);

            String resolvedSender = resolveSender(event);
            // 记住"最近谁在说话"（仪表盘要显示"它在听谁"，守卫要按发言瞬间的坐标执行）
            lastSpeakerName = resolvedSender;
            lastSpeakerAt = System.currentTimeMillis();

            JsonObject data = new JsonObject();
            data.addProperty("sender", resolvedSender);
            data.addProperty("uuid", event.getSender() != null ? event.getSender().toString() : "");
            data.addProperty("message", message);
            data.addProperty("is_system", event.isSystem());
            data.addProperty("type", event.isSystem() ? "system_chat" : "player_chat");

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
}
