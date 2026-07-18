package com.lcu.lcumod.config;

import net.neoforged.neoforge.common.ModConfigSpec;

public class ModConfig {
    private static final ModConfigSpec.Builder BUILDER = new ModConfigSpec.Builder();

    public static final ModConfigSpec.ConfigValue<String> RUNTIME_ROLE = BUILDER
            .comment("Runtime role: player_client, body_client, or server_fake_player")
            .define("runtimeRole", "player_client", value -> value instanceof String role && (
                    role.equals("player_client")
                    || role.equals("body_client")
                    || role.equals("server_fake_player")
            ));

    public static final ModConfigSpec.ConfigValue<String> PLAYER_BACKEND_URL = BUILDER
            .comment("Restricted player conversation API URL")
            .define("playerBackendUrl", "http://127.0.0.1:8080");

    public static final ModConfigSpec.ConfigValue<String> PLAYER_API_TOKEN = BUILDER
            .comment("Restricted PLAYER_API_TOKEN; never use the operator SDK token here")
            .define("playerApiToken", "");

    // Wire protocol port — backend connects here
    public static final ModConfigSpec.IntValue WIRE_PORT = BUILDER
            .comment("TCP port for the JSONL wire protocol (backend connects here)")
            .defineInRange("wirePort", 25568, 1024, 65535);

    public static final ModConfigSpec.ConfigValue<String> WIRE_TOKEN = BUILDER
            .comment("Shared wire authentication token; set MOD_WIRE_TOKEN to the same value in the backend")
            .define("wireToken", "");

    // State push interval in ticks (20 ticks = 1 second)
    public static final ModConfigSpec.IntValue STATE_INTERVAL = BUILDER
            .comment("How often (in ticks) to push full state snapshot to backend")
            .defineInRange("stateInterval", 10, 1, 200);

    // Whether to collect detailed surroundings (blocks/entities around player)
    public static final ModConfigSpec.BooleanValue COLLECT_SURROUNDINGS = BUILDER
            .comment("Collect surrounding blocks and entities for state push")
            .define("collectSurroundings", true);

    // Surroundings scan radius
    public static final ModConfigSpec.IntValue SURROUNDINGS_RADIUS = BUILDER
            .comment("Radius in blocks to scan surroundings")
            .defineInRange("surroundingsRadius", 8, 2, 32);

    public static final ModConfigSpec SPEC = BUILDER.build();
}
