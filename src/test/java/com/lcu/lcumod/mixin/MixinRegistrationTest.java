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
 * Regression guard: any mixin that is not registered in lcumod.mixins.json is
 * dead code, which only surfaces at client boot — never in unit tests. Every
 * entry in the client array must name a class that is actually compiled into
 * the artifact, and the array must not silently shrink.
 */
class MixinRegistrationTest {

    @Test
    void everyClientMixinIsRegisteredAndCompiled() throws Exception {
        JsonArray client;
        try (InputStream in = getClass().getResourceAsStream("/lcumod.mixins.json")) {
            assertNotNull(in, "lcumod.mixins.json must be on the test classpath");
            JsonElement root = JsonParser.parseReader(new InputStreamReader(in, StandardCharsets.UTF_8));
            client = root.getAsJsonObject().getAsJsonArray("client");
        }
        assertTrue(client.size() >= 2, "expected at least MouseHandlerMixin and HeadlessRenderSkipMixin");
        for (JsonElement entry : client) {
            String simpleName = entry.getAsString();
            assertNotNull(
                getClass().getResource("/com/lcu/lcumod/mixin/" + simpleName + ".class"),
                simpleName + " must be registered in the client mixin array and compiled"
            );
        }
    }
}
