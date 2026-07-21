package com.lcu.lcumod.action;

import com.google.gson.JsonArray;
import com.google.gson.JsonObject;
import com.mojang.blaze3d.platform.InputConstants;
import com.mojang.blaze3d.platform.NativeImage;
import net.minecraft.client.KeyMapping;
import net.minecraft.client.Minecraft;
import net.minecraft.client.Screenshot;
import net.minecraft.client.gui.screens.inventory.AbstractContainerScreen;

import java.io.IOException;
import java.util.Base64;

/** On-demand Minecraft-local GUI observation and single-step input. */
public final class GuiAutomation {
    private static String fingerprint = "none";
    private static long revision;

    private GuiAutomation() {}

    public static JsonObject observe(Minecraft mc, boolean includeImage) throws IOException {
        refreshRevision(mc);
        JsonObject result = describeScreen(mc);
        result.addProperty("screen_revision", revision);
        result.add("keybindings", describeKeyMappings(mc));
        if (includeImage) {
            try (NativeImage image = Screenshot.takeScreenshot(mc.getMainRenderTarget())) {
                byte[] png = image.asByteArray();
                if (png.length > 700_000) throw new IOException("GUI screenshot exceeds 700000 bytes");
                result.addProperty("image_media_type", "image/png");
                result.addProperty("image_base64", Base64.getEncoder().encodeToString(png));
                result.addProperty("image_width", image.getWidth());
                result.addProperty("image_height", image.getHeight());
            }
        }
        return result;
    }

    public static JsonArray describeKeyMappings(Minecraft mc) {
        JsonArray items = new JsonArray();
        KeyMapping[] mappings = mc.options.keyMappings;
        for (KeyMapping mapping : mappings) {
            JsonObject item = new JsonObject();
            item.addProperty("id", mapping.getName());
            item.addProperty("label", mapping.getTranslatedKeyMessage().getString());
            item.addProperty("category", mapping.getCategory());
            item.addProperty("key", mapping.getKey().getName());
            int conflicts = 0;
            for (KeyMapping other : mappings) {
                if (other != mapping && mapping.same(other)) conflicts++;
            }
            item.addProperty("conflicts", conflicts);
            items.add(item);
        }
        return items;
    }

    public static boolean click(Minecraft mc, long expectedRevision, double x, double y, int button) {
        refreshRevision(mc);
        if (mc.screen == null || revision != expectedRevision || button < 0 || button > 2) return false;
        if (x < 0 || y < 0 || x >= mc.getWindow().getGuiScaledWidth() || y >= mc.getWindow().getGuiScaledHeight()) return false;
        boolean handled = mc.screen.mouseClicked(x, y, button);
        refreshRevision(mc);
        return handled;
    }

    public static boolean pressKey(Minecraft mc, long expectedRevision, String mappingId) {
        refreshRevision(mc);
        if (revision != expectedRevision || mappingId == null || mappingId.isBlank()) return false;
        for (KeyMapping mapping : mc.options.keyMappings) {
            if (!mapping.getName().equals(mappingId)) continue;
            InputConstants.Key key = mapping.getKey();
            KeyMapping.click(key);
            return true;
        }
        return false;
    }

    private static JsonObject describeScreen(Minecraft mc) {
        JsonObject result = new JsonObject();
        result.addProperty("open", mc.screen != null);
        result.addProperty("screen_class", mc.screen == null ? "" : mc.screen.getClass().getName());
        result.addProperty("title", mc.screen == null ? "" : mc.screen.getTitle().getString());
        result.addProperty("gui_width", mc.getWindow().getGuiScaledWidth());
        result.addProperty("gui_height", mc.getWindow().getGuiScaledHeight());
        result.addProperty("gui_scale", mc.getWindow().getGuiScale());
        if (mc.player != null && mc.player.containerMenu != null) {
            var menu = mc.player.containerMenu;
            result.addProperty("menu_class", menu.getClass().getName());
            result.addProperty("container_id", menu.containerId);
            result.addProperty("state_id", menu.getStateId());
            result.addProperty("slot_count", menu.slots.size());
        }
        if (mc.screen instanceof AbstractContainerScreen<?> containerScreen) {
            result.addProperty("container_screen", true);
        }
        return result;
    }

    private static void refreshRevision(Minecraft mc) {
        String current = screenFingerprint(mc);
        if (!current.equals(fingerprint)) {
            fingerprint = current;
            revision++;
        }
    }

    private static String screenFingerprint(Minecraft mc) {
        String screen = mc.screen == null ? "none" : mc.screen.getClass().getName() + "|" + mc.screen.getTitle().getString();
        if (mc.player == null || mc.player.containerMenu == null) return screen;
        var menu = mc.player.containerMenu;
        return screen + "|" + menu.getClass().getName() + "|" + menu.containerId + "|" + menu.getStateId()
            + "|" + mc.getWindow().getGuiScaledWidth() + "x" + mc.getWindow().getGuiScaledHeight();
    }
}
