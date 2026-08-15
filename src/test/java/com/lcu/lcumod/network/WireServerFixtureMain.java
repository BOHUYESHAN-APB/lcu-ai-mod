package com.lcu.lcumod.network;

import com.google.gson.JsonObject;

public final class WireServerFixtureMain {
    private WireServerFixtureMain() {}

    public static void main(String[] args) throws Exception {
        int port = Integer.parseInt(args.length > 0 ? args[0] : "0");
        String token = args.length > 1 ? args[1] : "integration-secret";
        WireServer server = new WireServer(port, token, "body_client", () -> {});
        server.start();
        System.out.println("READY " + server.getBoundPort());
        System.out.flush();
        long deadline = System.nanoTime() + 60_000_000_000L;
        long lastSnapshotAt = 0L;
        try {
            while (System.nanoTime() < deadline) {
                if (server.isConnected() && System.nanoTime() - lastSnapshotAt >= 1_000_000_000L) {
                    JsonObject player = new JsonObject();
                    player.addProperty("health", 20.0);
                    player.addProperty("max_health", 20.0);
                    JsonObject control = new JsonObject();
                    control.addProperty("ai_controlled", true);
                    JsonObject state = new JsonObject();
                    state.add("player", player);
                    state.add("control_state", control);
                    state.addProperty("game_time", 100L);
                    state.addProperty("day_time", 100L);
                    state.addProperty("fixture", "java-production-wire");
                    server.sendEvent("state_update", state);
                    lastSnapshotAt = System.nanoTime();
                }
                WireServer.WireCommand command = WireServer.commandQueue.poll();
                if (command != null) {
                    JsonObject data = new JsonObject();
                    data.addProperty("message", "fixture accepted " + command.cmd());
                    server.sendResponse(command.id(), true, data);
                    if ("move_to".equals(command.cmd())) {
                        server.sendProgress(command.id(), 0.5, "fixture moving");
                        server.sendProgress(command.id(), 1.0, "fixture arrived");
                    }
                    if ("fixture_shutdown".equals(command.cmd())) break;
                }
                Thread.sleep(5);
            }
        } finally {
            server.stop();
        }
    }
}
