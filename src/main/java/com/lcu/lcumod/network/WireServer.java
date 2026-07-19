package com.lcu.lcumod.network;

import com.google.gson.Gson;
import com.google.gson.JsonArray;
import com.google.gson.JsonObject;
import com.lcu.lcumod.action.ActionExecutor;
import java.io.*;
import java.net.ServerSocket;
import java.net.Socket;
import java.net.InetAddress;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.concurrent.*;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.Map;
import java.util.function.Supplier;

/**
 * TCP JSONL wire protocol server.
 */
public class WireServer {
    private static final System.Logger LOGGER = System.getLogger(WireServer.class.getName());
    public static final int PROTOCOL_VERSION = 3;
    private static final int MAX_FRAME_CHARS = 1024 * 1024;
    private static final int AUTH_TIMEOUT_MILLIS = 5000;
    private static final int START_TIMEOUT_MILLIS = 5000;
    private static final int SEND_QUEUE_CAPACITY = 1024;
    private final int port;
    private final String authToken;
    private final String role;
    private final Runnable disconnectHandler;
    private final Supplier<Map<String, Boolean>> policySupplier;
    private final AtomicBoolean running = new AtomicBoolean(false);
    private final Gson gson = new Gson();
    private Thread serverThread;
    private volatile ServerSocket serverSocket;
    private volatile int boundPort = -1;
    private volatile Throwable startupError;
    private volatile CountDownLatch readyLatch = new CountDownLatch(1);
    private volatile Connection activeConnection;
    private Thread sendThread;
    private final BlockingQueue<String> sendQueue = new ArrayBlockingQueue<>(SEND_QUEUE_CAPACITY);

    public static final PriorityCommandQueue commandQueue = new PriorityCommandQueue();

    public WireServer(int port, String authToken) {
        this(port, authToken, "body_client", ActionExecutor::requestBackendDisconnectStop, Map.of());
    }

    public WireServer(int port, String authToken, String role, Runnable disconnectHandler) {
        this(port, authToken, role, disconnectHandler, Map.of());
    }

    public WireServer(int port, String authToken, String role, Runnable disconnectHandler,
                      Map<String, Boolean> policy) {
        this(port, authToken, role, disconnectHandler, () -> policy == null ? Map.of() : Map.copyOf(policy));
    }

    public WireServer(int port, String authToken, String role, Runnable disconnectHandler,
                      Supplier<Map<String, Boolean>> policySupplier) {
        this.port = port;
        this.authToken = authToken == null ? "" : authToken;
        this.role = role == null || role.isBlank() ? "body_client" : role;
        this.disconnectHandler = disconnectHandler == null ? () -> {} : disconnectHandler;
        this.policySupplier = policySupplier == null ? Map::of : policySupplier;
    }

    public synchronized void start() {
        if (running.getAndSet(true)) return;
        startupError = null;
        boundPort = -1;
        readyLatch = new CountDownLatch(1);
        serverThread = new Thread(this::runServer, "LCU-WireServer");
        serverThread.setDaemon(true);
        serverThread.start();
        // Async send thread — prevents render thread blocking on TCP writes
        sendThread = new Thread(this::sendLoop, "LCU-WireSend");
        sendThread.setDaemon(true);
        sendThread.start();
        try {
            if (!readyLatch.await(START_TIMEOUT_MILLIS, TimeUnit.MILLISECONDS)) {
                stop();
                throw new IllegalStateException("Timed out binding wire server on port " + port);
            }
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            stop();
            throw new IllegalStateException("Interrupted while starting wire server", e);
        }
        if (startupError != null || boundPort < 0) {
            stop();
            throw new IllegalStateException("Failed to bind wire server on port " + port, startupError);
        }
        LOGGER.log(System.Logger.Level.INFO, "[WireServer] Started on port {0}", boundPort);
    }

    public synchronized void stop() {
        running.set(false);
        ServerSocket listener = serverSocket;
        if (listener != null) {
            try { listener.close(); } catch (IOException ignored) {}
        }
        Connection connection = activeConnection;
        if (connection != null) connection.close();
        activeConnection = null;
        commandQueue.clear();
        sendQueue.clear();
        if (sendThread != null) sendThread.interrupt();
        if (serverThread != null) serverThread.interrupt();
        joinThread(serverThread);
        joinThread(sendThread);
        serverSocket = null;
        serverThread = null;
        sendThread = null;
        boundPort = -1;
    }

    private static void joinThread(Thread thread) {
        if (thread == null || thread == Thread.currentThread()) return;
        try {
            thread.join(1000);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
        }
    }

    public int getBoundPort() { return boundPort; }

    public boolean isRunning() { return running.get() && boundPort >= 0; }

    public boolean isConnected() {
        return activeConnection != null && activeConnection.isOpen();
    }

    /** Async send — queues message, returns immediately. NEVER blocks the calling thread. */
    public void send(JsonObject msg) {
        if (!sendQueue.offer(gson.toJson(msg))) {
            LOGGER.log(System.Logger.Level.WARNING, "[WireServer] Dropped outbound frame because the send queue is full");
        }
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
                LOGGER.log(System.Logger.Level.WARNING, "[WireServer] Send error: {0}", e.getMessage());
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

    public void sendOutcome(String id, String status, String code, String message) {
        if (id == null || id.isBlank()) return;
        JsonObject msg = new JsonObject();
        msg.addProperty("type", "outcome");
        msg.addProperty("id", id);
        msg.addProperty("status", status);
        if (code != null && !code.isBlank()) msg.addProperty("code", code);
        if (message != null && !message.isBlank()) msg.addProperty("message", message);
        msg.addProperty("ts", System.currentTimeMillis());
        send(msg);
    }

    private void runServer() {
        try (ServerSocket ss = new ServerSocket(port, 50, InetAddress.getByName("127.0.0.1"))) {
            serverSocket = ss;
            boundPort = ss.getLocalPort();
            readyLatch.countDown();
            while (running.get()) {
                try {
                    Socket sock = ss.accept();
                    LOGGER.log(System.Logger.Level.INFO, "[WireServer] Backend connection pending from {0}", sock.getRemoteSocketAddress());
                    Connection conn = new Connection(sock);
                    conn.startReader();
                } catch (IOException e) {
                    if (running.get()) {
                        LOGGER.log(System.Logger.Level.ERROR, "[WireServer] Accept error: {0}", e.getMessage());
                    }
                }
            }
        } catch (IOException e) {
            if (boundPort < 0) {
                startupError = e;
                readyLatch.countDown();
            } else if (running.get()) {
                LOGGER.log(System.Logger.Level.ERROR, "[WireServer] Failed on port {0}: {1}", boundPort, e.getMessage());
            }
        } finally {
            serverSocket = null;
            if (running.getAndSet(false)) readyLatch.countDown();
        }
    }

    private synchronized void activate(Connection conn) {
        Connection old = activeConnection;
        if (old != null && old != conn) {
            commandQueue.clear();
            disconnectHandler.run();
            old.close();
        }
        activeConnection = conn;
        LOGGER.log(System.Logger.Level.INFO, "[WireServer] Authenticated backend connected from {0}", conn.socket.getRemoteSocketAddress());
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
            socket.setSoTimeout(AUTH_TIMEOUT_MILLIS);
            this.reader = new BufferedReader(new InputStreamReader(socket.getInputStream(), StandardCharsets.UTF_8));
            this.writer = new BufferedWriter(new OutputStreamWriter(socket.getOutputStream(), StandardCharsets.UTF_8));
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
                LOGGER.log(System.Logger.Level.WARNING, "[WireServer] Send error: {0}", e.getMessage());
                close();
            }
        }

        void startReader() {
            Thread readerThread = new Thread(() -> {
                try {
                    String authLine = readLineLimited();
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
                    accepted.addProperty("protocol_version", PROTOCOL_VERSION);
                    accepted.addProperty("role", role);
                    JsonArray capabilities = new JsonArray();
                    capabilities.add("state");
                    capabilities.add("actions");
                    capabilities.add("progress");
                    accepted.add("capabilities", capabilities);
                    Map<String, Boolean> currentPolicy;
                    try {
                        currentPolicy = Map.copyOf(policySupplier.get());
                    } catch (RuntimeException exception) {
                        currentPolicy = Map.of();
                    }
                    accepted.add("tools", ToolCatalog.describe(currentPolicy));
                    JsonObject policyState = new JsonObject();
                    currentPolicy.forEach(policyState::addProperty);
                    accepted.add("policy", policyState);
                    send(gson.toJson(accepted));
                    activate(this);
                    socket.setSoTimeout(0);

                    String line;
                    while (open.get() && (line = readLineLimited()) != null) {
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
                                    LOGGER.log(System.Logger.Level.INFO, "[WireServer] Received command: {0} id={1}", cmd.cmd(), cmd.id());
                                    if ("control_external".equals(cmd.cmd()) || "control_builtin".equals(cmd.cmd())) {
                                        for (WireCommand discarded : commandQueue.submitControl(cmd)) {
                                            rejectPreempted(discarded);
                                        }
                                    } else if ("stop_all".equals(cmd.cmd())) {
                                        for (WireCommand discarded : commandQueue.submitStop(cmd)) {
                                            rejectPreempted(discarded);
                                        }
                                    } else {
                                        if (!commandQueue.submitBackend(cmd)) {
                                            JsonObject data = new JsonObject();
                                            data.addProperty("message", "command queue is full");
                                            WireServer.this.sendResponse(cmd.id(), false, data, "QUEUE_FULL");
                                        }
                                    }
                                }
                            }
                        } catch (Exception e) {
                            LOGGER.log(System.Logger.Level.WARNING, "[WireServer] Invalid JSON: {0}", e.getMessage());
                        }
                    }
                } catch (IOException e) {
                    if (open.get()) {
                        LOGGER.log(System.Logger.Level.INFO, "[WireServer] Backend disconnected: {0}", e.getMessage());
                    }
                } finally {
                    close();
                }
            }, "LCU-WireReader");
            readerThread.setDaemon(true);
            readerThread.start();
        }

        String readLineLimited() throws IOException {
            StringBuilder line = new StringBuilder();
            int value;
            while ((value = reader.read()) != -1) {
                if (value == '\n') break;
                if (value != '\r') line.append((char) value);
                if (line.length() > MAX_FRAME_CHARS) {
                    throw new IOException("wire frame exceeds maximum size");
                }
            }
            if (value == -1 && line.isEmpty()) return null;
            return line.toString();
        }

        private void rejectPreempted(WireCommand discarded) {
            if (discarded.id() == null || discarded.id().isBlank()) return;
            if ("cancel_operation".equals(discarded.cmd())) {
                JsonObject data = new JsonObject();
                data.addProperty("message", "cancel request superseded by stop_all");
                WireServer.this.sendResponse(discarded.id(), true, data, null);
                return;
            }
            if (switch (discarded.cmd()) {
                case "move_to", "mine_block", "mine_block_at", "follow_player",
                     "collect_blocks", "craft_item", "eat" -> true;
                default -> false;
            }) {
                WireServer.this.sendOutcome(
                    discarded.id(), "cancelled", "QUEUE_PREEMPTED", "operation preempted by stop_all");
                return;
            }
            JsonObject data = new JsonObject();
            data.addProperty("message", "command preempted by stop_all");
            WireServer.this.sendResponse(discarded.id(), false, data, "QUEUE_PREEMPTED");
        }

        void close() {
            if (!open.getAndSet(false)) return;
            try { socket.close(); } catch (IOException ignored) {}
            LOGGER.log(System.Logger.Level.INFO, "[WireServer] Connection closed");
            if (activeConnection == this) {
                activeConnection = null;
                commandQueue.clear();
                disconnectHandler.run();
            }
        }
    }

    public record WireCommand(String id, String cmd, JsonObject args) {}
}
