package com.lcu.lcumod.client;

import com.google.gson.Gson;
import com.google.gson.JsonObject;
import com.lcu.lcumod.config.ModConfig;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.time.Duration;
import java.util.concurrent.CompletableFuture;

public final class PlayerConversationClient {
    private static final Gson GSON = new Gson();
    private static final HttpClient HTTP = HttpClient.newBuilder()
            .connectTimeout(Duration.ofSeconds(5))
            .build();

    private PlayerConversationClient() {}

    public static CompletableFuture<String> send(String playerId, String playerName,
                                                  String serverId, String clientMessageId,
                                                  String message) {
        URI endpoint = endpoint();
        JsonObject payload = new JsonObject();
        payload.addProperty("player_id", playerId);
        payload.addProperty("player_name", playerName);
        payload.addProperty("server_id", serverId);
        payload.addProperty("client_message_id", clientMessageId);
        payload.addProperty("message", message);
        HttpRequest.Builder request = HttpRequest.newBuilder(endpoint)
                .timeout(Duration.ofSeconds(120))
                .header("Content-Type", "application/json")
                .POST(HttpRequest.BodyPublishers.ofString(GSON.toJson(payload)));
        String token = ModConfig.PLAYER_API_TOKEN.get().trim();
        if (!token.isEmpty()) request.header("Authorization", "Bearer " + token);
        return HTTP.sendAsync(request.build(), HttpResponse.BodyHandlers.ofString())
                .thenApply(response -> parseResponse(response.statusCode(), response.body()));
    }

    static URI endpoint() {
        String base = ModConfig.PLAYER_BACKEND_URL.get().trim().replaceAll("/+$", "");
        URI uri = URI.create(base + "/api/player/v1/messages");
        String host = uri.getHost();
        boolean loopback = "127.0.0.1".equals(host) || "localhost".equalsIgnoreCase(host) || "::1".equals(host);
        if (!"https".equalsIgnoreCase(uri.getScheme()) && !loopback) {
            throw new IllegalArgumentException("Remote player conversation API requires HTTPS");
        }
        return uri;
    }

    static String parseResponse(int status, String body) {
        JsonObject payload = GSON.fromJson(body, JsonObject.class);
        if (status < 200 || status >= 300) {
            String detail = payload != null && payload.has("detail") ? payload.get("detail").toString() : "HTTP " + status;
            throw new IllegalStateException(detail);
        }
        if (payload == null || !payload.has("reply")) {
            throw new IllegalStateException("Conversation response did not include a reply");
        }
        return payload.get("reply").getAsString();
    }
}
