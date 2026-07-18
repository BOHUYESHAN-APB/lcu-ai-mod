package com.lcu.lcumod.chat;

import com.google.gson.JsonObject;
import com.lcu.lcumod.LCUMod;
import com.lcu.lcumod.client.ClientBodyRuntime;
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

    @SubscribeEvent
    public static void onClientChat(ClientChatReceivedEvent event) {
        try {
            if (!ClientBodyRuntime.isBodyClient()) return;
            if (LCUMod.WIRE == null || !LCUMod.WIRE.isConnected()) return;

            // Skip system messages (death, teleport, etc.) to avoid backend noise
            if (event.isSystem()) return;

            Component msgComponent = event.getMessage();
            if (msgComponent == null) return;
            String message = msgComponent.getString();
            if (message == null || message.isEmpty()) return;

            // Skip messages sent by our own AI player (server echo of send_chat)
            String senderName = resolveSender(event);
            var mc = Minecraft.getInstance();
            if (mc.player != null && senderName.equals(mc.player.getName().getString())) return;

            msgCount++;
            LCUMod.LOGGER.info("[Chat] #{} received", msgCount);

            JsonObject data = new JsonObject();
            data.addProperty("sender", resolveSender(event));
            data.addProperty("uuid", event.getSender() != null ? event.getSender().toString() : "");
            data.addProperty("message", message);
            data.addProperty("is_system", event.isSystem());
            data.addProperty("type", event.isSystem() ? "system_chat" : "player_chat");

            LCUMod.WIRE.sendEvent("player_chat", data);
        } catch (Exception e) {
            LCUMod.LOGGER.error("[Chat] Error processing chat: {}", e.getMessage());
            // NEVER let exceptions escape — they crash the render thread → disconnect
        }
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
