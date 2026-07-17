package com.lcu.lcumod.config;

import net.neoforged.neoforge.common.ModConfigSpec;

public class ModConfig {
    private static final ModConfigSpec.Builder BUILDER = new ModConfigSpec.Builder();

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
