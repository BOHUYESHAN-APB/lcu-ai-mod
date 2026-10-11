package com.lcu.lcumod.network;

import com.google.gson.Gson;
import com.google.gson.JsonObject;
import com.lcu.lcumod.LCUMod;
import com.lcu.lcumod.action.ControlStream;
import java.io.*;
import java.net.ServerSocket;
import java.net.Socket;
import java.nio.charset.StandardCharsets;
import java.util.concurrent.*;
import java.util.concurrent.atomic.AtomicBoolean;

/**
 * TCP JSONL wire protocol server.
 */
public class WireServer {
    private final int port;
    private final AtomicBoolean running = new AtomicBoolean(false);
    private final Gson gson = new Gson();
    private Thread serverThread;
    private volatile Connection activeConnection;
    private Thread sendThread;
    private final BlockingQueue<String> sendQueue = new LinkedBlockingQueue<>();

    public static final PriorityCommandQueue commandQueue = new PriorityCommandQueue();

    /** 最近一次 bind 失败的原因；null = 正常监听。
     *  两个游戏窗口抢同一个端口时，本窗口会 bind 失败——此时聊天与状态**全部静默丢弃**，
     *  必须在游戏里把原因显示出来，否则用户只看到"发消息没反应"。 */
    public static volatile String LISTEN_ERROR = null;

    public WireServer(int port) {
        this.port = port;
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
        LCUMod.LOGGER.info("[WireServer] Starting on port {} (bind on LCU-WireServer thread)", port);
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

    /** True when at least one backend TCP client is connected.
     *  Used as the single-writer autonomy gate: while a backend owns behavior,
     *  the Java-side autonomous behavior must not run in parallel. */
    public boolean hasClients() {
        return isConnected();
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

    /**
     * 终局结果（completion="outcome" 的技能：mine_block / eat 等需要**诚实**的成功/失败）。
     * 后端只在收到 outcome 时才会把这类 run 判定为成功，因此成功必须显式下发本消息。
     *
     * @param status  succeeded / failed / cancelled
     * @param message 人类可读原因
     * @param code    机器可读原因码（如 timeout / target_lost）
     */
    public void sendOutcome(String id, String status, String message, String code) {
        JsonObject msg = new JsonObject();
        msg.addProperty("type", "outcome");
        msg.addProperty("id", id);
        msg.addProperty("status", status);
        msg.addProperty("message", message);
        if (code != null && !code.isEmpty()) {
            msg.addProperty("code", code);
        }
        msg.addProperty("ts", System.currentTimeMillis());
        send(msg);
    }

    private void runServer() {
        // 端口被占用时**不要一次失败就永久残废**：每 5s 重绑一次。
        // 典型场景：用户之前那个游戏窗口没退干净；关掉它以后本窗口应自动恢复，不必重启游戏。
        while (running.get()) {
            try (ServerSocket ss = new ServerSocket(port)) {
                LISTEN_ERROR = null;
                LCUMod.LOGGER.info("[WireServer] Listening on port {}", port);
                while (running.get()) {
                    try {
                        Socket sock = ss.accept();
                        LCUMod.LOGGER.info("[WireServer] Backend connected from {}", sock.getRemoteSocketAddress());
                        Connection conn = new Connection(sock);
                        Connection old = activeConnection;
                        if (old != null) old.close();
                        activeConnection = conn;
                        conn.startReader();
                    } catch (IOException e) {
                        if (running.get()) {
                            LCUMod.LOGGER.error("[WireServer] Accept error: {}", e.getMessage());
                        }
                    }
                }
            } catch (IOException e) {
                if (!running.get()) return;
                LISTEN_ERROR = "端口 " + port + " 被占用，本窗口收不到后端指令（" + e.getMessage() + "）"
                        + "——请只开一个装了本模组的游戏窗口（会自动重试）";
                LCUMod.LOGGER.error("[WireServer] PORT BUSY on {}: {} — 是不是开了第二个游戏窗口/有残留进程？"
                        + "5s 后重试。", port, e.getMessage());
                try {
                    Thread.sleep(5000L);
                } catch (InterruptedException ie) {
                    Thread.currentThread().interrupt();
                    return;
                }
            }
        }
    }

    class Connection {
        final Socket socket;
        final BufferedReader reader;
        final BufferedWriter writer;
        final AtomicBoolean open = new AtomicBoolean(true);

        Connection(Socket socket) throws IOException {
            this.socket = socket;
            // 必须显式指定 UTF-8：用平台默认编码的话，中文 Windows 上是 GBK，
            // 而 Python 侧按 UTF-8 解 → 只要消息里带中文（物品名/聊天/POI）整条就被丢掉、
            // 连接还会重连，表现就是"仪表盘连上了却什么数据都没有"。
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
                LCUMod.LOGGER.warn("[WireServer] Send error: {}", e.getMessage());
                close();
            }
        }

        void startReader() {
            Thread readerThread = new Thread(() -> {
                try {
                    String line;
                    while (open.get() && (line = reader.readLine()) != null) {
                        if (line.isBlank()) continue;
                        try {
                            JsonObject msg = gson.fromJson(line, JsonObject.class);
                            if (msg == null) continue;
                            String type = msg.get("type") != null && !msg.get("type").isJsonNull()
                                    ? msg.get("type").getAsString() : null;
                            if ("command".equals(type)) {
                                WireCommand cmd = new WireCommand(
                                        msg.get("id") != null ? msg.get("id").getAsString() : null,
                                        msg.get("cmd") != null ? msg.get("cmd").getAsString() : null,
                                        msg.get("args") != null ? msg.get("args").getAsJsonObject() : null
                                );
                                if (cmd.cmd() != null) {
                                    LCUMod.LOGGER.info("[WireServer] Received command: {} id={}", cmd.cmd(), cmd.id());
                                    commandQueue.submitBackend(cmd);
                                }
                            } else if ("control".equals(type)) {
                                // 高频控制流：无回执、latest-wins，只写字段，不碰游戏对象
                                ControlStream.apply(msg);
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
            // 后端断线必须立刻松手，否则控制流最后一次按键会永久卡住
            ControlStream.requestRelease();
            if (activeConnection == this) {
                activeConnection = null;
            }
        }
    }

    public record WireCommand(String id, String cmd, JsonObject args) {}
}
