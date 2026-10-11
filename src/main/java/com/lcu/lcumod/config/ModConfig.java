package com.lcu.lcumod.config;

import net.neoforged.neoforge.common.ModConfigSpec;

public class ModConfig {
    private static final ModConfigSpec.Builder BUILDER = new ModConfigSpec.Builder();

    // Wire protocol port — backend connects here
    public static final ModConfigSpec.IntValue WIRE_PORT = BUILDER
            .comment("TCP port for the JSONL wire protocol (backend connects here)")
            .defineInRange("wirePort", 25568, 1024, 65535);

    // Wire protocol listen address — tightened to loopback by default.
    // Anything else (e.g. "0.0.0.0") restores the old wildcard listen for people who really run
    // the backend on another machine. See docs/dev/SERVER_SIDE.md.
    public static final ModConfigSpec.ConfigValue<String> WIRE_BIND_ADDRESS = BUILDER
            .comment("Address the wire protocol listens on. Default 127.0.0.1 (loopback only).",
                    "Set to 0.0.0.0 to restore the old wildcard listen (backend on another machine).")
            .define("wireBindAddress", "127.0.0.1");

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
