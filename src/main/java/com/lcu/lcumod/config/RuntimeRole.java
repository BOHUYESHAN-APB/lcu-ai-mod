package com.lcu.lcumod.config;

public enum RuntimeRole {
    PLAYER_CLIENT("player_client"),
    BODY_CLIENT("body_client"),
    SERVER_FAKE_PLAYER("server_fake_player");

    private final String configValue;

    RuntimeRole(String configValue) {
        this.configValue = configValue;
    }

    public String configValue() {
        return configValue;
    }

    public static RuntimeRole current() {
        String value = ModConfig.RUNTIME_ROLE.get();
        for (RuntimeRole role : values()) {
            if (role.configValue.equals(value)) return role;
        }
        return PLAYER_CLIENT;
    }
}
