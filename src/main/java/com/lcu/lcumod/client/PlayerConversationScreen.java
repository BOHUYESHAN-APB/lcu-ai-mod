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
import net.minecraft.util.FormattedCharSequence;
import org.lwjgl.glfw.GLFW;

public final class PlayerConversationScreen extends Screen {
    private static final int PANEL_TOP = 20;
    private static final int PANEL_MAX_WIDTH = 620;
    private static final int SIDEBAR_WIDTH = 142;
    private static final int COMPACT_WIDTH = 430;

    private final List<PlayerConversationClient.Contact> contacts = new ArrayList<>();
    private final List<PlayerConversationClient.Message> messages = new ArrayList<>();
    private PlayerConversationClient.Contact selectedContact;
    private EditBox input;
    private Button sendButton;
    private Button refreshButton;
    private String status = "";
    private int requestGeneration;
    private int sendGeneration;
    private boolean readPending;
    private boolean sendPending;
    private int scrollLines;
    private int maxScrollLines;

    public PlayerConversationScreen() {
        super(Component.translatable("screen.lcumod.conversation.title"));
    }

    @Override
    protected void init() {
        int left = panelLeft();
        int panelWidth = panelWidth();
        int inputY = height - 34;
        input = new EditBox(font, left, inputY, Math.max(40, panelWidth - 76), 20,
                Component.translatable("screen.lcumod.conversation.input"));
        input.setMaxLength(2000);
        addRenderableWidget(input);
        sendButton = addRenderableWidget(Button.builder(
                Component.translatable("screen.lcumod.conversation.send"), button -> sendMessage()
        ).bounds(left + panelWidth - 70, inputY, 70, 20).build());
        refreshButton = addRenderableWidget(Button.builder(
                Component.translatable("screen.lcumod.conversation.refresh"), button -> loadContacts()
        ).bounds(left + panelWidth - 68, PANEL_TOP + 6, 62, 20).build());
        setInitialFocus(input);
        loadContacts();
    }

    private void loadContacts() {
        PlayerIdentity identity = playerIdentity();
        if (identity == null) return;
        int generation = ++requestGeneration;
        readPending = true;
        if (!sendPending) status = Component.translatable("screen.lcumod.conversation.loading").getString();
        updateControls();
        PlayerConversationClient.loadContacts(identity.playerId(), identity.serverId())
                .whenComplete((loadedContacts, error) -> minecraft.execute(() -> {
                    if (!isActive() || generation != requestGeneration) return;
                    if (error != null) {
                        failRead(error);
                        return;
                    }
                    String selectedId = selectedContact == null ? null : selectedContact.conversationId();
                    contacts.clear();
                    contacts.addAll(loadedContacts);
                    selectedContact = contacts.stream()
                            .filter(contact -> contact.conversationId().equals(selectedId))
                            .findFirst().orElse(contacts.isEmpty() ? null : contacts.getFirst());
                    if (selectedContact == null) {
                        messages.clear();
                        finishRead();
                    } else {
                        loadMessages(identity, selectedContact, generation);
                    }
                }));
    }

    private void loadMessages(PlayerIdentity identity, PlayerConversationClient.Contact contact, int generation) {
        PlayerConversationClient.loadMessages(identity.playerId(), identity.serverId(), contact.conversationId())
                .whenComplete((thread, error) -> minecraft.execute(() -> {
                    if (!isActive() || generation != requestGeneration || selectedContact != contact) return;
                    if (error != null) {
                        failRead(error);
                        return;
                    }
                    messages.clear();
                    messages.addAll(thread.messages());
                    scrollLines = 0;
                    finishRead();
                }));
    }

    private void selectContact(PlayerConversationClient.Contact contact) {
        if (contact == selectedContact || readPending || sendPending) return;
        selectedContact = contact;
        messages.clear();
        scrollLines = 0;
        PlayerIdentity identity = playerIdentity();
        if (identity == null) return;
        int generation = ++requestGeneration;
        readPending = true;
        status = Component.translatable("screen.lcumod.conversation.loading").getString();
        updateControls();
        loadMessages(identity, contact, generation);
    }

    private void sendMessage() {
        String message = input.getValue().trim();
        PlayerIdentity identity = playerIdentity();
        if (message.isEmpty() || identity == null || sendButton == null || readPending || sendPending) return;
        input.setValue("");
        status = Component.translatable("screen.lcumod.conversation.sending").getString();
        sendPending = true;
        int generation = ++sendGeneration;
        updateControls();
        PlayerConversationClient.send(
                identity.playerId(), identity.playerName(), identity.serverId(),
                UUID.randomUUID().toString(), message
        ).whenComplete((reply, error) -> minecraft.execute(() -> {
            if (!isActive() || generation != sendGeneration) return;
            sendPending = false;
            if (error != null) {
                status = Component.translatable(
                        "screen.lcumod.conversation.failed_detail", rootMessage(error)).getString();
                updateControls();
            } else {
                loadContacts();
            }
        }));
    }

    private PlayerIdentity playerIdentity() {
        Minecraft client = Minecraft.getInstance();
        if (client.player == null) {
            status = Component.translatable("screen.lcumod.conversation.no_player").getString();
            return null;
        }
        String serverId = client.getCurrentServer() != null ? client.getCurrentServer().ip : "singleplayer";
        return new PlayerIdentity(client.player.getUUID().toString(),
                client.player.getName().getString(), serverId);
    }

    private void finishRead() {
        readPending = false;
        if (!sendPending) status = "";
        updateControls();
    }

    private void failRead(Throwable error) {
        readPending = false;
        if (!sendPending) {
            status = Component.translatable(
                    "screen.lcumod.conversation.failed_detail", rootMessage(error)).getString();
        }
        updateControls();
    }

    private void updateControls() {
        boolean idle = !readPending && !sendPending;
        if (sendButton != null) sendButton.active = idle;
        if (refreshButton != null) refreshButton.active = idle;
    }

    private boolean isActive() {
        return minecraft != null && minecraft.screen == this;
    }

    private static String rootMessage(Throwable error) {
        Throwable current = error;
        while (current.getCause() != null) current = current.getCause();
        return current.getMessage() == null ? current.getClass().getSimpleName() : current.getMessage();
    }

    @Override
    public boolean keyPressed(int keyCode, int scanCode, int modifiers) {
        if (keyCode == GLFW.GLFW_KEY_ENTER && input != null && input.isFocused()
                && !readPending && !sendPending) {
            sendMessage();
            return true;
        }
        return super.keyPressed(keyCode, scanCode, modifiers);
    }

    @Override
    public void removed() {
        requestGeneration++;
        sendGeneration++;
        readPending = false;
        sendPending = false;
        super.removed();
    }

    @Override
    public boolean mouseClicked(double mouseX, double mouseY, int button) {
        int rowLeft = panelLeft() + 5;
        int rowWidth = compact() ? panelWidth() - 10 : SIDEBAR_WIDTH - 10;
        int rowY = compact() ? PANEL_TOP + 34 : PANEL_TOP + 36;
        for (PlayerConversationClient.Contact contact : contacts) {
            if (mouseX >= rowLeft && mouseX < rowLeft + rowWidth && mouseY >= rowY && mouseY < rowY + 30) {
                selectContact(contact);
                return true;
            }
            rowY += 32;
        }
        return super.mouseClicked(mouseX, mouseY, button);
    }

    @Override
    public boolean mouseScrolled(double mouseX, double mouseY, double scrollX, double scrollY) {
        if (maxScrollLines > 0) {
            scrollLines = Math.max(0, Math.min(maxScrollLines,
                    scrollLines + (int) Math.round(scrollY * 3.0)));
            return true;
        }
        return super.mouseScrolled(mouseX, mouseY, scrollX, scrollY);
    }

    @Override
    public void render(GuiGraphics graphics, int mouseX, int mouseY, float partialTick) {
        renderBackground(graphics, mouseX, mouseY, partialTick);
        int left = panelLeft();
        int right = left + panelWidth();
        int bottom = height - 42;
        graphics.fill(left, PANEL_TOP, right, bottom, 0xE612171D);
        graphics.drawString(font, clip(title.getString(), panelWidth() - 84),
                left + 8, PANEL_TOP + 11, 0xFFF1F4F7, false);

        int threadLeft;
        int threadTop;
        if (compact()) {
            graphics.fill(left, PANEL_TOP + 32, right, PANEL_TOP + 68, 0xCC1B222A);
            renderContacts(graphics, left + 5, PANEL_TOP + 34, panelWidth() - 10);
            threadLeft = left + 8;
            threadTop = PANEL_TOP + 74;
        } else {
            graphics.fill(left, PANEL_TOP + 32, left + SIDEBAR_WIDTH, bottom, 0xCC1B222A);
            renderContacts(graphics, left + 5, PANEL_TOP + 36, SIDEBAR_WIDTH - 10);
            threadLeft = left + SIDEBAR_WIDTH + 10;
            threadTop = PANEL_TOP + 38;
        }
        renderThread(graphics, threadLeft, threadTop, right - threadLeft - 8, bottom - threadTop - 5);
        if (!status.isEmpty()) {
            graphics.drawString(font, clip(status, panelWidth() - 8), left + 4, height - 11, 0xFFB6C0CA, false);
        }
        super.render(graphics, mouseX, mouseY, partialTick);
    }

    private void renderContacts(GuiGraphics graphics, int left, int top, int availableWidth) {
        int y = top;
        for (PlayerConversationClient.Contact contact : contacts) {
            boolean selected = contact == selectedContact;
            graphics.fill(left, y, left + availableWidth, y + 30, selected ? 0xFF34414D : 0xFF242C34);
            int presenceColor = "online".equals(contact.presence()) ? 0xFF66C58A : 0xFF79838D;
            graphics.fill(left + 6, y + 7, left + 10, y + 11, presenceColor);
            int unreadWidth = contact.unreadCount() > 0 ? 20 : 0;
            graphics.drawString(font, clip(contact.displayName(), availableWidth - 24 - unreadWidth),
                    left + 15, y + 5, 0xFFF2F5F7, false);
            String contactStatus = Component.translatable(
                    "screen.lcumod.conversation.status." + contact.status()).getString();
            graphics.drawString(font, clip(contactStatus, availableWidth - 22),
                    left + 15, y + 17, 0xFF9EAAB5, false);
            if (contact.unreadCount() > 0) {
                graphics.drawString(font, Integer.toString(contact.unreadCount()),
                        left + availableWidth - 16, y + 11, 0xFFFFD166, false);
            }
            if (compact()) break;
            y += 32;
        }
    }

    private void renderThread(GuiGraphics graphics, int left, int top, int availableWidth, int availableHeight) {
        if (selectedContact == null) {
            graphics.drawString(font, Component.translatable("screen.lcumod.conversation.empty"),
                    left, top, 0xFF98A3AD, false);
            return;
        }
        List<RenderedLine> lines = new ArrayList<>();
        for (PlayerConversationClient.Message message : messages) {
            String sender = message.ai() ? selectedContact.displayName() : message.sender();
            int color = message.ai() ? 0xFFB9D7FF : 0xFFF0F2F4;
            List<FormattedCharSequence> wrapped = font.split(Component.literal(sender + ": " + message.text()),
                    Math.max(20, availableWidth));
            for (FormattedCharSequence line : wrapped) lines.add(new RenderedLine(line, color));
            lines.add(new RenderedLine(FormattedCharSequence.EMPTY, color));
        }
        int lineHeight = 11;
        int visibleLines = Math.max(1, availableHeight / lineHeight);
        maxScrollLines = Math.max(0, lines.size() - visibleLines);
        scrollLines = Math.min(scrollLines, maxScrollLines);
        int first = Math.max(0, lines.size() - visibleLines - scrollLines);
        int last = Math.min(lines.size(), first + visibleLines);
        int y = top;
        for (int index = first; index < last; index++) {
            graphics.drawString(font, lines.get(index).text(), left, y, lines.get(index).color(), false);
            y += lineHeight;
        }
    }

    private String clip(String value, int maxWidth) {
        if (value == null) return "";
        if (font.width(value) <= maxWidth) return value;
        String suffix = "...";
        int end = value.length();
        while (end > 0 && font.width(value.substring(0, end) + suffix) > maxWidth) end--;
        return value.substring(0, end) + suffix;
    }

    private boolean compact() {
        return width < COMPACT_WIDTH;
    }

    private int panelWidth() {
        return Math.max(160, Math.min(PANEL_MAX_WIDTH, width - 20));
    }

    private int panelLeft() {
        return (width - panelWidth()) / 2;
    }

    @Override
    public boolean isPauseScreen() {
        return false;
    }

    private record PlayerIdentity(String playerId, String playerName, String serverId) {}
    private record RenderedLine(FormattedCharSequence text, int color) {}
}
