package com.lcu.lcumod.client;

import java.util.ArrayList;
import java.util.List;
import java.util.UUID;
import net.minecraft.client.Minecraft;
import net.minecraft.client.gui.GuiGraphics;
import net.minecraft.client.gui.components.Button;
import net.minecraft.client.gui.components.EditBox;
import net.minecraft.client.gui.screens.Screen;
import net.minecraft.network.chat.Component;
import org.lwjgl.glfw.GLFW;

public final class PlayerConversationScreen extends Screen {
    private final List<String> transcript = new ArrayList<>();
    private EditBox input;
    private Button sendButton;
    private String status = "";

    public PlayerConversationScreen() {
        super(Component.translatable("screen.lcumod.conversation.title"));
    }

    @Override
    protected void init() {
        int panelWidth = Math.min(520, width - 32);
        int left = (width - panelWidth) / 2;
        input = new EditBox(font, left, height - 48, panelWidth - 88, 20,
                Component.translatable("screen.lcumod.conversation.input"));
        input.setMaxLength(2000);
        addRenderableWidget(input);
        sendButton = addRenderableWidget(Button.builder(
                Component.translatable("screen.lcumod.conversation.send"), button -> sendMessage()
        ).bounds(left + panelWidth - 80, height - 48, 80, 20).build());
        setInitialFocus(input);
    }

    private void sendMessage() {
        String message = input.getValue().trim();
        Minecraft minecraft = Minecraft.getInstance();
        if (message.isEmpty() || minecraft.player == null || sendButton == null) return;
        input.setValue("");
        transcript.add("You: " + message);
        status = Component.translatable("screen.lcumod.conversation.sending").getString();
        sendButton.active = false;
        String serverId = minecraft.getCurrentServer() != null
                ? minecraft.getCurrentServer().ip
                : "singleplayer";
        PlayerConversationClient.send(
                minecraft.player.getUUID().toString(),
                minecraft.player.getName().getString(),
                serverId,
                UUID.randomUUID().toString(),
                message
        ).whenComplete((reply, error) -> minecraft.execute(() -> {
            if (error != null) {
                status = Component.translatable("screen.lcumod.conversation.failed").getString();
                transcript.add("System: " + rootMessage(error));
            } else {
                transcript.add("AI: " + reply);
                status = "";
            }
            if (sendButton != null) sendButton.active = true;
        }));
    }

    private static String rootMessage(Throwable error) {
        Throwable current = error;
        while (current.getCause() != null) current = current.getCause();
        return current.getMessage() == null ? current.getClass().getSimpleName() : current.getMessage();
    }

    @Override
    public boolean keyPressed(int keyCode, int scanCode, int modifiers) {
        if (keyCode == GLFW.GLFW_KEY_ENTER) {
            sendMessage();
            return true;
        }
        return super.keyPressed(keyCode, scanCode, modifiers);
    }

    @Override
    public void render(GuiGraphics graphics, int mouseX, int mouseY, float partialTick) {
        renderBackground(graphics, mouseX, mouseY, partialTick);
        int panelWidth = Math.min(520, width - 32);
        int left = (width - panelWidth) / 2;
        graphics.fill(left, 24, left + panelWidth, height - 60, 0xDD11161D);
        graphics.drawCenteredString(font, title, width / 2, 34, 0xFFFFFFFF);
        int y = 54;
        int first = Math.max(0, transcript.size() - Math.max(1, (height - 140) / 12));
        for (int index = first; index < transcript.size(); index++) {
            graphics.drawString(font, transcript.get(index), left + 12, y, 0xFFD8DEE9, false);
            y += 12;
        }
        if (!status.isEmpty()) graphics.drawString(font, status, left + 8, height - 24, 0xFFAAB2BF, false);
        super.render(graphics, mouseX, mouseY, partialTick);
    }

    @Override
    public boolean isPauseScreen() {
        return false;
    }
}
