package com.lcu.lcumod.client;

import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.*;

class PlayerConversationClientTest {
    @Test
    void parsesUnicodeReplyFromRestrictedPlayerApi() {
        String reply = PlayerConversationClient.parseResponse(
                200,
                "{\"status\":\"completed\",\"reply\":\"你好，一起挖矿吧\"}"
        );

        assertEquals("你好，一起挖矿吧", reply);
    }

    @Test
    void rejectsErrorAndMissingReplyResponses() {
        assertThrows(IllegalStateException.class, () ->
                PlayerConversationClient.parseResponse(401, "{\"detail\":\"unauthorized\"}"));
        assertThrows(IllegalStateException.class, () ->
                PlayerConversationClient.parseResponse(200, "{\"status\":\"completed\"}"));
    }
}
