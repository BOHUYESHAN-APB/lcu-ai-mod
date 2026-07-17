package com.lcu.lcumod.network;

import com.google.gson.Gson;
import com.google.gson.JsonObject;
import com.lcu.lcumod.LCUMod;
import java.io.*;
import java.net.ServerSocket;
import java.net.Socket;
import java.net.InetAddress;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.concurrent.*;
import java.util.concurrent.atomic.AtomicBoolean;

/**
 * TCP JSONL wire protocol server.
 */
public class WireServer {
    private final int port;
    private final String authToken;
    private final AtomicBoolean running = new AtomicBoolean(false);
    private final Gson gson = new Gson();
    private Thread serverThread;
    private volatile Connection activeConnection;
    private Thread sendThread;
    private final BlockingQueue<String> sendQueue = new LinkedBlockingQueue<>();

    public static final PriorityCommandQueue commandQueue = new PriorityCommandQueue();

    public WireServer(int port, String authToken) {
        this.port = port;
        this.authToken = authToken == null ? "" : authToken;
    }

    public void start() {
        if (running.getAndSet(true)) return;
        serverThread = new Thread(this::runServer, "LCU-WireServer");
        serverThread.setDaemon(true);
        serverThread.start();
        // Async send thread — prevents render thread blocking on TCP writes
        sendThread = new Thread(this::sendLoop, "LCU-WireSend");
        sendThread.setDaemon(true);
        sendThread.start();
        LCUMod.LOGGER.info("[WireServer] Started on port {}", port);
    }

    public void stop() {
        running.set(false);
        if (sendThread != null) sendThread.interrupt();
        if (serverThread != null) serverThread.interrupt();
        if (activeConnection != null) activeConnection.close();
    }

    public boolean isConnected() {
        return activeConnection != null && activeConnection.isOpen();
    }

    /** Async send — queues message, returns immediately. NEVER blocks the calling thread. */
    public void send(JsonObject msg) {
        sendQueue.offer(gson.toJson(msg));
    }

    /** Background thread that drains the send queue and writes to TCP. */
    private void sendLoop() {
        while (running.get()) {
            try {
                String line = sendQueue.poll(1, TimeUnit.SECONDS);
                if (line != null) {
                    Connection conn = activeConnection;
                    if (conn != null && conn.isOpen()) {
                        conn.send(line);
                    }
                }
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
                break;
            } catch (Exception e) {
                LCUMod.LOGGER.warn("[WireServer] Send error: {}", e.getMessage());
            }
        }
    }

    public void sendEvent(String eventType, JsonObject data) {
        JsonObject msg = new JsonObject();
        msg.addProperty("type", "event");
        msg.addProperty("event", eventType);
        msg.add("data", data);
        msg.addProperty("ts", System.currentTimeMillis());
        send(msg);
    }

    public void sendResponse(String id, boolean success, JsonObject data) {
        sendResponse(id, success, data, null);
    }

    public void sendResponse(String id, boolean success, JsonObject data, String error) {
        JsonObject msg = new JsonObject();
        msg.addProperty("type", "response");
        msg.addProperty("id", id);
        msg.addProperty("success", success);
        if (data != null) msg.add("data", data);
        if (error != null) msg.addProperty("error", error);
        msg.addProperty("ts", System.currentTimeMillis());
        send(msg);
    }

    public void sendProgress(String id, double progress, String message) {
        JsonObject msg = new JsonObject();
        msg.addProperty("type", "progress");
        msg.addProperty("id", id);
        msg.addProperty("progress", progress);
        if (message != null) msg.addProperty("message", message);
        msg.addProperty("ts", System.currentTimeMillis());
        send(msg);
    }

    private void runServer() {
        try (ServerSocket ss = new ServerSocket(port, 50, InetAddress.getByName("127.0.0.1"))) {
            while (running.get()) {
                try {
                    Socket sock = ss.accept();
                    LCUMod.LOGGER.info("[WireServer] Backend connection pending from {}", sock.getRemoteSocketAddress());
                    Connection conn = new Connection(sock);
                    conn.startReader();
                } catch (IOException e) {
                    if (running.get()) {
                        LCUMod.LOGGER.error("[WireServer] Accept error: {}", e.getMessage());
                    }
                }
            }
        } catch (IOException e) {
            LCUMod.LOGGER.error("[WireServer] Failed to start on port {}: {}", port, e.getMessage());
        }
    }

    private synchronized void activate(Connection conn) {
        Connection old = activeConnection;
        activeConnection = conn;
        if (old != null && old != conn) old.close();
        LCUMod.LOGGER.info("[WireServer] Authenticated backend connected from {}", conn.socket.getRemoteSocketAddress());
    }

    private boolean validToken(String candidate) {
        return MessageDigest.isEqual(
                authToken.getBytes(StandardCharsets.UTF_8),
                (candidate == null ? "" : candidate).getBytes(StandardCharsets.UTF_8)
        );
    }

    class Connection {
        final Socket socket;
        final BufferedReader reader;
        final BufferedWriter writer;
        final AtomicBoolean open = new AtomicBoolean(true);

        Connection(Socket socket) throws IOException {
            this.socket = socket;
            this.reader = new BufferedReader(new InputStreamReader(socket.getInputStream()));
            this.writer = new BufferedWriter(new OutputStreamWriter(socket.getOutputStream()));
        }

        boolean isOpen() { return open.get(); }

        void send(String line) {
            if (!open.get()) return;
            try {
                synchronized (writer) {
                    writer.write(line);
                    writer.newLine();
                    writer.flush();
                }
            } catch (IOException e) {
                LCUMod.LOGGER.warn("[WireServer] Send error: {}", e.getMessage());
                close();
            }
        }

        void startReader() {
            Thread readerThread = new Thread(() -> {
                try {
                    String authLine = reader.readLine();
                    if (authLine == null) return;
                    JsonObject auth = gson.fromJson(authLine, JsonObject.class);
                    String type = auth != null && auth.has("type") ? auth.get("type").getAsString() : "";
                    String token = auth != null && auth.has("token") ? auth.get("token").getAsString() : "";
                    if (!"auth".equals(type) || !validToken(token)) {
                        JsonObject denied = new JsonObject();
                        denied.addProperty("type", "auth");
                        denied.addProperty("success", false);
                        send(gson.toJson(denied));
                        return;
                    }
                    JsonObject accepted = new JsonObject();
                    accepted.addProperty("type", "auth");
                    accepted.addProperty("success", true);
                    send(gson.toJson(accepted));
                    activate(this);

                    String line;
                    while (open.get() && (line = reader.readLine()) != null) {
                        if (line.isBlank()) continue;
                        try {
                            JsonObject msg = gson.fromJson(line, JsonObject.class);
                            if (msg != null && "command".equals(msg.get("type") != null ? msg.get("type").getAsString() : null)) {
                                WireCommand cmd = new WireCommand(
                                        msg.get("id") != null ? msg.get("id").getAsString() : null,
                                        msg.get("cmd") != null ? msg.get("cmd").getAsString() : null,
                                        msg.get("args") != null ? msg.get("args").getAsJsonObject() : null
                                );
                                if (cmd.cmd() != null) {
                                    LCUMod.LOGGER.info("[WireServer] Received command: {} id={}", cmd.cmd(), cmd.id());
                                    if ("control_external".equals(cmd.cmd()) || "control_builtin".equals(cmd.cmd())) {
                                        commandQueue.submitControl(cmd);
                                    } else {
                                        commandQueue.submitBackend(cmd);
                                    }
                                }
                            }
                        } catch (Exception e) {
                            LCUMod.LOGGER.warn("[WireServer] Invalid JSON: {}", e.getMessage());
                        }
                    }
                } catch (IOException e) {
                    if (open.get()) {
                        LCUMod.LOGGER.info("[WireServer] Backend disconnected: {}", e.getMessage());
                    }
                } finally {
                    close();
                }
            }, "LCU-WireReader");
            readerThread.setDaemon(true);
            readerThread.start();
        }

        void close() {
            if (!open.getAndSet(false)) return;
            try { socket.close(); } catch (IOException ignored) {}
            LCUMod.LOGGER.info("[WireServer] Connection closed");
            if (activeConnection == this) {
                activeConnection = null;
            }
        }
    }

    public record WireCommand(String id, String cmd, JsonObject args) {}
}
