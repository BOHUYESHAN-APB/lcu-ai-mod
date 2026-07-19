package com.lcu.lcumod.config;

import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.*;

class RuntimeRoleTest {
    @Test
    void freshConfigurationDefaultsToHeadedBodyClient() {
        assertEquals(RuntimeRole.BODY_CLIENT, RuntimeRole.DEFAULT);
        assertEquals("body_client", RuntimeRole.DEFAULT.configValue());
    }

    @Test
    void eachSupportedRoleActivatesOnlyItsOwnRuntime() {
        assertTrue(RuntimeRole.BODY_CLIENT.activatesClientBody());
        assertFalse(RuntimeRole.BODY_CLIENT.activatesPlayerConversation());
        assertFalse(RuntimeRole.BODY_CLIENT.activatesServerFakePlayers());

        assertTrue(RuntimeRole.PLAYER_CLIENT.activatesPlayerConversation());
        assertFalse(RuntimeRole.PLAYER_CLIENT.activatesClientBody());
        assertFalse(RuntimeRole.PLAYER_CLIENT.activatesServerFakePlayers());

        assertTrue(RuntimeRole.SERVER_FAKE_PLAYER.activatesServerFakePlayers());
        assertFalse(RuntimeRole.SERVER_FAKE_PLAYER.activatesClientBody());
        assertFalse(RuntimeRole.SERVER_FAKE_PLAYER.activatesPlayerConversation());
    }

    @Test
    void invalidRoleFallsBackWithoutStartingAnActuator() {
        RuntimeRole fallback = RuntimeRole.fromConfigValue("invalid");

        assertEquals(RuntimeRole.PLAYER_CLIENT, fallback);
        assertFalse(fallback.activatesClientBody());
        assertFalse(fallback.activatesServerFakePlayers());
        assertEquals(RuntimeRole.PLAYER_CLIENT, RuntimeRole.fromConfigValue(123));
    }

    @Test
    void acceptedConfigurationValuesMatchPackagedRoles() {
        for (RuntimeRole role : RuntimeRole.values()) {
            assertTrue(RuntimeRole.isSupported(role.configValue()));
            assertEquals(role, RuntimeRole.fromConfigValue(role.configValue()));
        }
        assertFalse(RuntimeRole.isSupported("client"));
        assertFalse(RuntimeRole.isSupported(null));
    }
}
