package com.lcu.lcumod.chat;

import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.assertEquals;

class ChatListenerTest {
    @Test
    void stripsVanillaPlayerPrefixWithoutChangingMessageBody() {
        assertEquals("跟着我", ChatListener.extractMessageBody("<BoHuYeShan> 跟着我", "BoHuYeShan"));
        assertEquals("hello", ChatListener.extractMessageBody("BoHuYeShan: hello", "BoHuYeShan"));
        assertEquals("plain message", ChatListener.extractMessageBody("plain message", "BoHuYeShan"));
    }
}
