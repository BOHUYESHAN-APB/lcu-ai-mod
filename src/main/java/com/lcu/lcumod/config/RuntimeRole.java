package com.lcu.lcumod.config;

public enum RuntimeRole {
    PLAYER_CLIENT("player_client"),
    BODY_CLIENT("body_client"),
    SERVER_FAKE_PLAYER("server_fake_player");

    public static final RuntimeRole DEFAULT = BODY_CLIENT;

    private final String configValue;

    RuntimeRole(String configValue) {
        this.configValue = configValue;
    }

    public String configValue() {
        return configValue;
    }

    public boolean activatesClientBody() {
        return this == BODY_CLIENT;
    }

    public boolean activatesPlayerConversation() {
        return this == PLAYER_CLIENT;
    }

    public boolean activatesServerFakePlayers() {
        return this == SERVER_FAKE_PLAYER;
    }

    public static boolean isSupported(String value) {
        for (RuntimeRole role : values()) {
            if (role.configValue.equals(value)) return true;
        }
        return false;
    }

    public static RuntimeRole fromConfigValue(Object value) {
        for (RuntimeRole role : values()) {
            if (role.configValue.equals(value)) return role;
        }
        return PLAYER_CLIENT;
    }

    public static RuntimeRole current() {
        return fromConfigValue(ModConfig.RUNTIME_ROLE.get());
    }
}
