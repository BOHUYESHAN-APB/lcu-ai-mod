package com.lcu.lcumod.config;

import net.neoforged.neoforge.common.ModConfigSpec;

public class ModConfig {
    private static final ModConfigSpec.Builder BUILDER = new ModConfigSpec.Builder();

    public static final ModConfigSpec.ConfigValue<Object> RUNTIME_ROLE = BUILDER
            .comment("Runtime role: player_client, body_client, or server_fake_player")
            .define("runtimeRole", RuntimeRole.DEFAULT.configValue(),
                    value -> true);

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
            .comment("Collect nearby blocks, entities, players, and storage metadata. Enable only where server rules permit enhanced telemetry.")
            .define("collectSurroundings", false);

    // Surroundings scan radius
    public static final ModConfigSpec.IntValue SURROUNDINGS_RADIUS = BUILDER
            .comment("Radius in blocks to scan surroundings")
            .defineInRange("surroundingsRadius", 8, 2, 32);

    public static final ModConfigSpec.BooleanValue ENABLE_AUTONOMOUS_BEHAVIORS = BUILDER
            .comment("Enable unattended Java behaviors. Keep false on public servers unless their rules explicitly permit bots.")
            .define("enableAutonomousBehaviors", false);

    public static final ModConfigSpec.BooleanValue ENABLE_ACTIVITY_SIGNALS = BUILDER
            .comment("Enable anti-AFK movement/look pulses. This is prohibited on many servers and defaults off.")
            .define("enableActivitySignals", false);

    public static final ModConfigSpec.BooleanValue REPORT_PROGRAMMATIC_ACTIVITY = BUILDER
            .comment("Report automated actions as activity to WATUT. Defaults off to avoid masking automation as user activity.")
            .define("reportProgrammaticActivity", false);

    public static final ModConfigSpec.BooleanValue RUN_IN_BACKGROUND = BUILDER
            .comment("Keep the body running while Minecraft is unfocused. Defaults off for public-server safety.")
            .define("runInBackground", false);

    public static final ModConfigSpec.BooleanValue AUTO_RESPAWN = BUILDER
            .comment("Automatically send respawn requests after death. Defaults off for public-server safety.")
            .define("autoRespawn", false);

    public static final ModConfigSpec.BooleanValue ALLOW_AUTOMATED_COMBAT = BUILDER
            .comment("Expose and execute automated attacks. Enable only where server rules explicitly permit combat automation.")
            .define("allowAutomatedCombat", false);

    public static final ModConfigSpec.BooleanValue ALLOW_MOVEMENT_AUTOMATION = BUILDER
            .comment("Allow explicit movement, follow, jump, and movement-control commands.")
            .define("allowMovementAutomation", true);

    public static final ModConfigSpec.BooleanValue ALLOW_WORLD_AUTOMATION = BUILDER
            .comment("Allow automated block breaking, block/entity interaction, collection, and placement.")
            .define("allowWorldAutomation", false);

    public static final ModConfigSpec.BooleanValue ALLOW_INVENTORY_AUTOMATION = BUILDER
            .comment("Allow automated item use, crafting, equipment, and menu mutation.")
            .define("allowInventoryAutomation", false);

    public static final ModConfigSpec.BooleanValue ALLOW_CHAT_AUTOMATION = BUILDER
            .comment("Allow the body to send automated Minecraft chat messages.")
            .define("allowChatAutomation", true);

    public static final ModConfigSpec SPEC = BUILDER.build();
}
