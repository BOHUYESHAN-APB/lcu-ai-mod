package com.lcu.lcumod.chat;

import com.google.gson.JsonObject;
import com.google.gson.JsonArray;
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

            Component msgComponent = event.getMessage();
            if (msgComponent == null) return;
            JsonArray clickActions = new JsonArray();
            collectClickActions(msgComponent, clickActions);
            if (event.isSystem()) {
                if (!clickActions.isEmpty()) {
                    JsonObject data = new JsonObject();
                    data.addProperty("message", msgComponent.getString());
                    data.addProperty("is_system", true);
                    data.add("actions", clickActions);
                    LCUMod.WIRE.sendEvent("chat_clicks", data);
                }
                return;
            }
            String senderName = resolveSender(event);
            String message = extractMessageBody(msgComponent.getString(), senderName);
            if (message == null || message.isEmpty()) return;

            // Skip messages sent by our own AI player (server echo of send_chat)
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
            if (!clickActions.isEmpty()) data.add("actions", clickActions);

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

    static String extractMessageBody(String rendered, String sender) {
        if (rendered == null) return "";
        String text = rendered.trim();
        String bracketPrefix = "<" + sender + ">";
        if (!sender.isBlank() && text.regionMatches(true, 0, bracketPrefix, 0, bracketPrefix.length())) {
            return text.substring(bracketPrefix.length()).trim();
        }
        String colonPrefix = sender + ":";
        if (!sender.isBlank() && text.regionMatches(true, 0, colonPrefix, 0, colonPrefix.length())) {
            return text.substring(colonPrefix.length()).trim();
        }
        return text;
    }

    private static void collectClickActions(Component component, JsonArray actions) {
        var click = component.getStyle().getClickEvent();
        if (click != null) {
            JsonObject action = new JsonObject();
            action.addProperty("action", click.getAction().name().toLowerCase(java.util.Locale.ROOT));
            action.addProperty("value", click.getValue());
            action.addProperty("text", component.getString());
            actions.add(action);
        }
        for (Component sibling : component.getSiblings()) collectClickActions(sibling, actions);
    }
}
