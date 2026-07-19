package com.lcu.lcumod.client;

import java.util.concurrent.CompletionException;
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

    @Test
    void parsesScopedContactsAndPersistedMessages() {
        var contacts = PlayerConversationClient.parseContactsResponse(200, """
                {"contacts":[{"id":"companion","display_name":"Maid","conversation_id":"direct_1",
                "last_activity":12.5,"message_count":2,"unread_count":1,"presence":"online",
                "status":"available"}]}
                """);
        var thread = PlayerConversationClient.parseMessagesResponse(200, """
                {"conversation":{"id":"direct_1"},"messages":[
                {"id":1,"timestamp":10,"sender":"Alice","message":"Hello","is_ai":0},
                {"id":2,"timestamp":11,"sender":"Maid","message":"Hi Alice","is_ai":1}]}
                """);

        assertEquals("Maid", contacts.getFirst().displayName());
        assertEquals(1, contacts.getFirst().unreadCount());
        assertEquals("direct_1", thread.conversationId());
        assertEquals("Hello", thread.messages().getFirst().text());
        assertTrue(thread.messages().getLast().ai());
    }

    @Test
    void rejectsMalformedOrIncompleteReadResponses() {
        assertThrows(IllegalStateException.class, () ->
                PlayerConversationClient.parseContactsResponse(200, "not-json"));
        assertThrows(IllegalStateException.class, () ->
                PlayerConversationClient.parseMessagesResponse(200, "{\"messages\":[]}"));
    }

    @Test
    void requestConstructionErrorsBecomeFailedFutures() {
        var future = PlayerConversationClient.<String>prepareRequest(() -> {
            throw new IllegalArgumentException("invalid backend URL");
        });

        CompletionException error = assertThrows(CompletionException.class, future::join);
        assertEquals("invalid backend URL", error.getCause().getMessage());
    }
}
