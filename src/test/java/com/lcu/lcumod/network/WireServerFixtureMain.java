package com.lcu.lcumod.network;

import com.google.gson.Gson;
import com.google.gson.JsonObject;
import com.lcu.lcumod.action.ControlStream;
import java.io.IOException;
import java.net.ServerSocket;

/**
 * Headless driver for the <b>real</b> production {@link WireServer}.
 *
 * <p>Purpose: prove that the Java-side wire protocol implementation really talks to the Python
 * backend, with no game client involved. This class only exists in the {@code test} source set,
 * so it can never end up in {@code build/libs/lcumod-*.jar}.
 *
 * <p>What is real here: the whole TCP/JSONL path — {@link WireServer#start()}, its accept loop,
 * its {@code Connection} UTF-8 reader/writer, the shared {@link WireServer#commandQueue} that
 * {@code ActionExecutor} drains in the game, {@link WireServer#sendEvent} /
 * {@link WireServer#sendResponse} / {@link WireServer#sendProgress} / {@link WireServer#sendOutcome}
 * and the {@code ControlStream.apply} branch of the reader.
 *
 * <p>What is <b>not</b> real, stated plainly: nothing in the game consumes the command queue here,
 * so this main drains {@code commandQueue} itself and answers with the same message shapes
 * {@code ActionExecutor} emits ({@code response} / {@code progress} / {@code outcome}). That is a
 * test driver standing in for the game loop, not a simulation of the game.
 *
 * <p>Usage: {@code WireServerFixtureMain [port] [bindAddress]} — port {@code 0} (the default)
 * makes the fixture pick a free port itself and print {@code READY <port>} on stdout.
 *
 * <p>Protocol notes for 26.3: there is no handshake and no auth token on the wire (the old
 * 1.21.1 token/hello exchange was dropped); the first frame the fixture sends is an ordinary
 * {@code state_update} event.
 */
public final class WireServerFixtureMain {

    /** How often a state event is pushed while a backend is connected. */
    private static final long STATE_INTERVAL_MS = 250L;
    /** Hard self-destruct so a leaked fixture process can never linger forever. */
    private static final long DEFAULT_LIFETIME_MS = 180_000L;

    private static final Gson GSON = new Gson();

    private WireServerFixtureMain() {}

    public static void main(String[] args) throws Exception {
        int requestedPort = args.length > 0 ? Integer.parseInt(args[0]) : 0;
        String bindAddress = args.length > 1 && !args[1].isBlank() ? args[1] : "127.0.0.1";
        int port = requestedPort != 0 ? requestedPort : freePort();

        WireServer server = new WireServer(port, bindAddress);
        server.start();

        // The server thread binds asynchronously. Give it a moment, then report the port: the
        // driver retries `connect` anyway (the server re-binds every 5s after a busy port), so
        // READY means "started", never "pretend it worked".
        Thread.sleep(300L);
        String listenError = WireServer.LISTEN_ERROR;
        if (listenError != null) {
            trace("FIXTURE-ERROR " + listenError);
            server.stop();
            System.exit(2);
        }

        trace("READY " + port);

        long deadline = System.nanoTime() + DEFAULT_LIFETIME_MS * 1_000_000L;
        long lastStateAt = 0L;
        boolean sawClient = false;
        try {
            while (System.nanoTime() < deadline) {
                boolean connected = server.isConnected();
                if (connected && !sawClient) {
                    sawClient = true;
                    trace("CONNECTED backend client attached to " + bindAddress + ":" + port);
                }
                if (connected && System.nanoTime() - lastStateAt >= STATE_INTERVAL_MS * 1_000_000L) {
                    server.sendEvent("state_update", stateSnapshot());
                    lastStateAt = System.nanoTime();
                }

                WireServer.WireCommand cmd = WireServer.commandQueue.poll();
                if (cmd != null) {
                    if (!respond(server, cmd)) {
                        break;
                    }
                }
                Thread.sleep(5L);
            }
        } finally {
            server.stop();
            trace("STOPPED");
        }
    }

    /** Mirrors the message shapes ActionExecutor sends; returns false to stop the fixture. */
    private static boolean respond(WireServer server, WireServer.WireCommand cmd) {
        trace("RECV " + GSON.toJson(cmd));
        JsonObject data = new JsonObject();
        data.addProperty("message", "fixture accepted " + cmd.cmd());
        if (cmd.args() != null) {
            data.add("echo", cmd.args());
        }
        if ("fixture_progress".equals(cmd.cmd())) {
            server.sendProgress(cmd.id(), 0.5, "fixture half way");
            server.sendProgress(cmd.id(), 1.0, "fixture arrived");
            server.sendOutcome(cmd.id(), "succeeded", "fixture finished", null);
            trace("SENT progress 0.5+1.0 and outcome succeeded for " + cmd.id());
        } else {
            server.sendResponse(cmd.id(), true, data);
            trace("SENT response ok for " + cmd.id());
        }
        if ("fixture_fail".equals(cmd.cmd())) {
            JsonObject empty = new JsonObject();
            server.sendResponse(cmd.id(), false, empty, "fixture said no");
            trace("SENT response failure for " + cmd.id());
        }
        return !"fixture_shutdown".equals(cmd.cmd());
    }

    /**
     * Same shape StateCollector uses around a minimal in-world update, plus
     * {@link ControlStream#describeState()} so the driver can observe that a fire-and-forget
     * {@code control} frame really reached the Java control path.
     */
    private static JsonObject stateSnapshot() {
        JsonObject player = new JsonObject();
        player.addProperty("health", 20.0);
        player.addProperty("max_health", 20.0);

        JsonObject world = new JsonObject();
        world.addProperty("server", "fixture");
        world.addProperty("world_name", "wire-fixture");

        JsonObject state = new JsonObject();
        state.addProperty("in_world", true);
        state.addProperty("timestamp", System.currentTimeMillis());
        state.addProperty("fixture", "java-production-wire");
        state.addProperty("game_time", 1000L);
        state.addProperty("day_time", 1000L);
        state.add("world", world);
        state.add("player", player);
        state.add("control_state", ControlStream.describeState());
        return state;
    }

    private static int freePort() throws IOException {
        try (ServerSocket s = new ServerSocket(0)) {
            return s.getLocalPort();
        }
    }

    private static void trace(String line) {
        System.out.println(line);
        System.out.flush();
    }
}
