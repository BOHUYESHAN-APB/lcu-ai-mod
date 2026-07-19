package com.lcu.lcumod.client;

import com.google.gson.Gson;
import com.google.gson.JsonElement;
import com.google.gson.JsonObject;
import com.google.gson.annotations.SerializedName;
import com.lcu.lcumod.config.ModConfig;
import java.net.URI;
import java.net.URLEncoder;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.Arrays;
import java.util.List;
import java.util.concurrent.CompletableFuture;

public final class PlayerConversationClient {
    private static final Gson GSON = new Gson();
    private static final HttpClient HTTP = HttpClient.newBuilder()
            .connectTimeout(Duration.ofSeconds(5))
            .build();

    private PlayerConversationClient() {}

    public record Contact(String id, String displayName, String conversationId,
                          double lastActivity, int messageCount, int unreadCount,
                          String presence, String status) {}

    public record Message(long id, double timestamp, String sender, String text, boolean ai) {}

    public record ConversationThread(String conversationId, List<Message> messages) {}

    public static CompletableFuture<List<Contact>> loadContacts(String playerId, String serverId) {
        URI uri = withQuery(endpoint("/api/player/v1/contacts"),
                "player_id=" + encode(playerId) + "&server_id=" + encode(serverId));
        return get(uri).thenApply(response -> parseContactsResponse(response.statusCode(), response.body()));
    }

    public static CompletableFuture<ConversationThread> loadMessages(String playerId, String serverId,
                                                                      String conversationId) {
        URI uri = withQuery(endpoint("/api/player/v1/conversations/" + encode(conversationId) + "/messages"),
                "player_id=" + encode(playerId) + "&server_id=" + encode(serverId) + "&limit=200");
        return get(uri).thenApply(response -> parseMessagesResponse(response.statusCode(), response.body()));
    }

    public static CompletableFuture<String> send(String playerId, String playerName,
                                                  String serverId, String clientMessageId,
                                                  String message) {
        URI endpoint = endpoint("/api/player/v1/messages");
        JsonObject payload = new JsonObject();
        payload.addProperty("player_id", playerId);
        payload.addProperty("player_name", playerName);
        payload.addProperty("server_id", serverId);
        payload.addProperty("client_message_id", clientMessageId);
        payload.addProperty("message", message);
        HttpRequest.Builder request = authorizedRequest(endpoint)
                .timeout(Duration.ofSeconds(120))
                .header("Content-Type", "application/json")
                .POST(HttpRequest.BodyPublishers.ofString(GSON.toJson(payload)));
        return HTTP.sendAsync(request.build(), HttpResponse.BodyHandlers.ofString())
                .thenApply(response -> parseResponse(response.statusCode(), response.body()));
    }

    static URI endpoint() {
        return endpoint("/api/player/v1/messages");
    }

    static URI endpoint(String path) {
        String base = ModConfig.PLAYER_BACKEND_URL.get().trim().replaceAll("/+$", "");
        URI uri = URI.create(base + path);
        String host = uri.getHost();
        boolean loopback = "127.0.0.1".equals(host) || "localhost".equalsIgnoreCase(host) || "::1".equals(host);
        if (!"https".equalsIgnoreCase(uri.getScheme()) && !loopback) {
            throw new IllegalArgumentException("Remote player conversation API requires HTTPS");
        }
        return uri;
    }

    private static CompletableFuture<HttpResponse<String>> get(URI uri) {
        HttpRequest request = authorizedRequest(uri)
                .timeout(Duration.ofSeconds(15))
                .GET()
                .build();
        return HTTP.sendAsync(request, HttpResponse.BodyHandlers.ofString());
    }

    private static HttpRequest.Builder authorizedRequest(URI uri) {
        HttpRequest.Builder request = HttpRequest.newBuilder(uri);
        String token = ModConfig.PLAYER_API_TOKEN.get().trim();
        if (!token.isEmpty()) request.header("Authorization", "Bearer " + token);
        return request;
    }

    private static String encode(String value) {
        return URLEncoder.encode(value, StandardCharsets.UTF_8);
    }

    private static URI withQuery(URI uri, String query) {
        return URI.create(uri.toString() + "?" + query);
    }

    static String parseResponse(int status, String body) {
        JsonObject payload = parsePayload(status, body);
        if (payload == null || !payload.has("reply")) {
            throw new IllegalStateException("Conversation response did not include a reply");
        }
        return payload.get("reply").getAsString();
    }

    static List<Contact> parseContactsResponse(int status, String body) {
        JsonObject payload = parsePayload(status, body);
        ContactJson[] values = GSON.fromJson(payload.get("contacts"), ContactJson[].class);
        if (values == null) throw new IllegalStateException("Contacts response did not include contacts");
        return Arrays.stream(values).map(value -> new Contact(
                value.id, value.displayName, value.conversationId, value.lastActivity,
                value.messageCount, value.unreadCount, value.presence, value.status
        )).toList();
    }

    static ConversationThread parseMessagesResponse(int status, String body) {
        JsonObject payload = parsePayload(status, body);
        JsonObject conversation = payload.has("conversation") && payload.get("conversation").isJsonObject()
                ? payload.getAsJsonObject("conversation") : null;
        MessageJson[] values = GSON.fromJson(payload.get("messages"), MessageJson[].class);
        if (conversation == null || !conversation.has("id") || values == null) {
            throw new IllegalStateException("Conversation history response is incomplete");
        }
        List<Message> messages = Arrays.stream(values).map(value -> new Message(
                value.id, value.timestamp, value.sender, value.message, value.isAi != 0
        )).toList();
        return new ConversationThread(conversation.get("id").getAsString(), messages);
    }

    private static JsonObject parsePayload(int status, String body) {
        JsonObject payload;
        try {
            payload = GSON.fromJson(body, JsonObject.class);
        } catch (RuntimeException error) {
            throw new IllegalStateException("Invalid conversation API response", error);
        }
        if (status < 200 || status >= 300) {
            JsonElement detail = payload == null ? null : payload.get("detail");
            throw new IllegalStateException(detail != null && detail.isJsonPrimitive()
                    ? detail.getAsString() : "HTTP " + status);
        }
        if (payload == null) throw new IllegalStateException("Conversation API returned an empty response");
        return payload;
    }

    private static final class ContactJson {
        String id;
        @SerializedName("display_name") String displayName;
        @SerializedName("conversation_id") String conversationId;
        @SerializedName("last_activity") double lastActivity;
        @SerializedName("message_count") int messageCount;
        @SerializedName("unread_count") int unreadCount;
        String presence;
        String status;
    }

    private static final class MessageJson {
        long id;
        double timestamp;
        String sender;
        String message;
        @SerializedName("is_ai") int isAi;
    }
}
