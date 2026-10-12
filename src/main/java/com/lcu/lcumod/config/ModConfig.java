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

    // Wire protocol auth token — empty means "no auth", which keeps the old behaviour for
    // loopback setups. When set, the backend must send a `hello` frame carrying this token as
    // the first line, otherwise the connection is closed. See docs/dev/SERVER_SIDE.md.
    public static final ModConfigSpec.ConfigValue<String> WIRE_TOKEN = BUILDER
            .comment("Auth token for the wire protocol. Empty (default) = no auth.",
                    "When set, the backend must send {\"type\":\"hello\",\"token\":\"...\"} first.")
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

    // 无后端时那套 legacy 自主行为（wander / track players / collect / flee）是否启用。
    // 默认**关**：用户口径"后台不许有自主开关/自动触发"，没后端时宁可站着也别自己乱跑。
    // 注意：P0 生存反射（战斗还手 / 溺水 / MLG / 进食）**不**受这个开关影响，它们该动还是会动。
    public static final ModConfigSpec.BooleanValue AUTONOMOUS_FALLBACK = BUILDER
        .comment("Run the legacy autonomous behaviour bundle (wander/track/collect) while no backend is connected.",
                "Default false: without a brain it stands still instead of acting on its own.",
                "P0 survival reflexes (combat, drowning, MLG, eating) are NOT affected by this switch.")
        .define("autonomousFallback", false);

    public static final ModConfigSpec SPEC = BUILDER.build();
}
