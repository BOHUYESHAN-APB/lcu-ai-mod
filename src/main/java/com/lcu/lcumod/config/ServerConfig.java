package com.lcu.lcumod.config;

import net.neoforged.neoforge.common.ModConfigSpec;

public final class ServerConfig {
    private static final ModConfigSpec.Builder BUILDER = new ModConfigSpec.Builder();

    public static final ModConfigSpec.BooleanValue FAKE_PLAYER_ENABLED = BUILDER
            .comment("Enable the future server-side fake-player body runtime")
            .define("fakePlayerEnabled", false);

    public static final ModConfigSpec.ConfigValue<String> FAKE_PLAYER_NAME = BUILDER
            .comment("Game profile name reserved for the server fake-player body")
            .define("fakePlayerName", "LCU_AI");

    public static final ModConfigSpec.IntValue FAKE_PLAYER_WIRE_PORT = BUILDER
            .comment("Loopback wire port reserved for the server fake-player body")
            .defineInRange("fakePlayerWirePort", 25569, 1024, 65535);

    public static final ModConfigSpec.ConfigValue<String> FAKE_PLAYER_WIRE_TOKEN = BUILDER
            .comment("Authentication token reserved for the server fake-player body")
            .define("fakePlayerWireToken", "");

    public static final ModConfigSpec SPEC = BUILDER.build();

    private ServerConfig() {}
}
