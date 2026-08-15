package com.lcu.lcumod.mixin;

import com.google.gson.JsonArray;
import com.google.gson.JsonElement;
import com.google.gson.JsonParser;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.nio.charset.StandardCharsets;
import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.*;

/**
 * Regression guard: the MouseHandlerMixin suppresses cursor release while the
 * body is AI-controlled. If it is not registered in lcumod.mixins.json the
 * hook is dead code, which only surfaces at client boot — never in unit tests.
 * This keeps the registration wiring verified on every build/CI run.
 */
class MixinRegistrationTest {

    @Test
    void clientMixinIsRegisteredAndCompiled() throws Exception {
        try (InputStream in = getClass().getResourceAsStream("/lcumod.mixins.json")) {
            assertNotNull(in, "lcumod.mixins.json must be on the test classpath");
            JsonElement root = JsonParser.parseReader(new InputStreamReader(in, StandardCharsets.UTF_8));
            JsonArray client = root.getAsJsonObject().getAsJsonArray("client");
            assertTrue(
                client.contains(new com.google.gson.JsonPrimitive("MouseHandlerMixin")),
                "MouseHandlerMixin must be registered in the client mixin array"
            );
        }
        assertNotNull(
            getClass().getResource("/com/lcu/lcumod/mixin/MouseHandlerMixin.class"),
            "MouseHandlerMixin must be compiled into the artifact"
        );
    }
}
