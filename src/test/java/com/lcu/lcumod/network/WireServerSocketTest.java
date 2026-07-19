package com.lcu.lcumod.network;

import com.google.gson.Gson;
import com.google.gson.JsonObject;
import java.io.BufferedReader;
import java.io.BufferedWriter;
import java.io.InputStreamReader;
import java.io.OutputStreamWriter;
import java.net.Socket;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.*;

class WireServerSocketTest {
    private static final Gson GSON = new Gson();
    private WireServer server;

    @AfterEach
    void cleanup() {
        if (server != null) server.stop();
        WireServer.commandQueue.clear();
    }

    @Test
    void authenticatesAndExchangesFramesOverRealLoopbackSocket() throws Exception {
        server = new WireServer(0, "shared-secret", "body_client", () -> {});
        server.start();

        try (Peer peer = new Peer(server.getBoundPort())) {
            JsonObject auth = peer.authenticate("shared-secret");
            assertTrue(auth.get("success").getAsBoolean());
            assertEquals(WireServer.PROTOCOL_VERSION, auth.get("protocol_version").getAsInt());
            assertEquals("body_client", auth.get("role").getAsString());
            assertTrue(auth.getAsJsonArray("tools").size() >= 10);
            assertTrue(auth.getAsJsonArray("tools").asList().stream()
                .anyMatch(tool -> tool.getAsJsonObject().get("command").getAsString().equals("craft_item")));
            assertTrue(auth.getAsJsonArray("tools").asList().stream()
                .anyMatch(tool -> tool.getAsJsonObject().get("command").getAsString().equals("cancel_operation")));

            peer.send("""
                    {"type":"command","id":"req-1","cmd":"jump","args":{"message":"你好"}}
                    """.trim());
            WireServer.WireCommand command = awaitCommand();
            assertEquals("req-1", command.id());
            assertEquals("jump", command.cmd());
            assertEquals("你好", command.args().get("message").getAsString());

            JsonObject eventData = new JsonObject();
            eventData.addProperty("message", "矿工你好");
            server.sendEvent("player_chat", eventData);
            JsonObject event = peer.read();
            assertEquals("event", event.get("type").getAsString());
            assertEquals("矿工你好", event.getAsJsonObject("data").get("message").getAsString());
        }
    }

    @Test
    void rejectsWrongTokenWithoutAcceptingCommands() throws Exception {
        server = new WireServer(0, "correct", "body_client", () -> {});
        server.start();

        try (Peer peer = new Peer(server.getBoundPort())) {
            JsonObject auth = peer.authenticate("wrong");
            assertFalse(auth.get("success").getAsBoolean());
            assertNull(WireServer.commandQueue.poll());
            assertFalse(server.isConnected());
        }
    }

    @Test
    void stopClosesListenerAndAllowsImmediateRebind() {
        server = new WireServer(0, "token", "body_client", () -> {});
        server.start();
        int port = server.getBoundPort();
        assertTrue(server.isRunning());

        server.stop();
        assertFalse(server.isRunning());

        WireServer rebound = new WireServer(port, "token", "body_client", () -> {});
        try {
            assertTimeoutPreemptively(Duration.ofSeconds(3), rebound::start);
            assertEquals(port, rebound.getBoundPort());
        } finally {
            rebound.stop();
        }
    }

    private static WireServer.WireCommand awaitCommand() throws InterruptedException {
        long deadline = System.nanoTime() + Duration.ofSeconds(2).toNanos();
        WireServer.WireCommand command;
        while ((command = WireServer.commandQueue.poll()) == null && System.nanoTime() < deadline) {
            Thread.sleep(10);
        }
        assertNotNull(command, "command did not reach the production queue");
        return command;
    }

    private static final class Peer implements AutoCloseable {
        private final Socket socket;
        private final BufferedReader reader;
        private final BufferedWriter writer;

        private Peer(int port) throws Exception {
            socket = new Socket("127.0.0.1", port);
            socket.setSoTimeout(2000);
            reader = new BufferedReader(new InputStreamReader(socket.getInputStream(), StandardCharsets.UTF_8));
            writer = new BufferedWriter(new OutputStreamWriter(socket.getOutputStream(), StandardCharsets.UTF_8));
        }

        private JsonObject authenticate(String token) throws Exception {
            JsonObject auth = new JsonObject();
            auth.addProperty("type", "auth");
            auth.addProperty("token", token);
            send(GSON.toJson(auth));
            return read();
        }

        private void send(String line) throws Exception {
            writer.write(line);
            writer.newLine();
            writer.flush();
        }

        private JsonObject read() throws Exception {
            String line = reader.readLine();
            assertNotNull(line, "wire connection closed before a frame arrived");
            return GSON.fromJson(line, JsonObject.class);
        }

        @Override
        public void close() throws Exception {
            socket.close();
        }
    }
}
