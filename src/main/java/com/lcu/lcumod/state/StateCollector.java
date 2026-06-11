package com.lcu.lcumod.state;

import com.google.gson.JsonArray;
import com.google.gson.JsonObject;
import com.lcu.lcumod.LCUMod;
import com.lcu.lcumod.config.ModConfig;
import net.minecraft.client.Minecraft;
import net.minecraft.server.level.ServerPlayer;
import net.minecraft.world.entity.*;
import net.minecraft.world.entity.monster.Enemy;
import net.minecraft.world.entity.animal.Animal;
import net.minecraft.world.entity.player.Player;
import net.minecraft.world.item.ItemStack;
import net.minecraft.world.entity.item.ItemEntity;
import net.minecraft.world.phys.AABB;
import net.neoforged.bus.api.SubscribeEvent;
import net.neoforged.fml.common.EventBusSubscriber;
import net.neoforged.neoforge.client.event.ClientTickEvent;

import java.util.List;

/**
 * Collects player and world state, pushes to wire via LAN server.
 * Uses @EventBusSubscriber for automatic registration.
 * 
 * Optimized: Only sends updates when state changes significantly.
 */
@EventBusSubscriber(modid = LCUMod.MODID, value = net.neoforged.api.distmarker.Dist.CLIENT)
public class StateCollector {
    private static int tickCounter = 0;
    
    // Last sent state for change detection
    private static double lastX = Double.NaN, lastY = Double.NaN, lastZ = Double.NaN;
    private static float lastYaw = Float.NaN, lastPitch = Float.NaN;
    private static float lastHealth = Float.NaN;
    private static int lastHunger = -1;
    private static int updateCounter = 0;
    
    // Thresholds for change detection
    private static final double POSITION_THRESHOLD = 0.1;  // 0.1 blocks
    private static final float ROTATION_THRESHOLD = 1.0f;   // 1 degree
    private static final float HEALTH_THRESHOLD = 0.5f;     // 0.5 hearts
    private static final int FORCE_UPDATE_INTERVAL = 100;   // Force update every 5 seconds

    @SubscribeEvent
    public static void onClientTick(ClientTickEvent.Post event) {
        tickCounter++;
        
        // Check if config is loaded before accessing it
        try {
            int interval = ModConfig.STATE_INTERVAL.getAsInt();
            if (tickCounter % interval != 0) return;
        } catch (IllegalStateException e) {
            // Config not loaded yet, skip this tick
            return;
        }

        if (LCUMod.WIRE == null || !LCUMod.WIRE.isConnected()) return;

        var mc = Minecraft.getInstance();
        if (mc.level == null || mc.player == null) return;

        Player p = mc.player;
        
        // Check if state has changed significantly
        boolean hasChanged = false;
        updateCounter++;
        
        // Position change
        if (Double.isNaN(lastX) || 
            Math.abs(p.getX() - lastX) > POSITION_THRESHOLD ||
            Math.abs(p.getY() - lastY) > POSITION_THRESHOLD ||
            Math.abs(p.getZ() - lastZ) > POSITION_THRESHOLD) {
            hasChanged = true;
        }
        
        // Rotation change
        if (Float.isNaN(lastYaw) ||
            Math.abs(p.getYRot() - lastYaw) > ROTATION_THRESHOLD ||
            Math.abs(p.getXRot() - lastPitch) > ROTATION_THRESHOLD) {
            hasChanged = true;
        }
        
        // Health change
        if (Float.isNaN(lastHealth) || Math.abs(p.getHealth() - lastHealth) > HEALTH_THRESHOLD) {
            hasChanged = true;
        }
        
        // Hunger change
        if (lastHunger < 0 || p.getFoodData().getFoodLevel() != lastHunger) {
            hasChanged = true;
        }
        
        // Force update periodically
        if (updateCounter >= FORCE_UPDATE_INTERVAL) {
            hasChanged = true;
            updateCounter = 0;
        }
        
        // Skip if no significant change
        if (!hasChanged) return;
        
        // Update last sent state
        lastX = p.getX();
        lastY = p.getY();
        lastZ = p.getZ();
        lastYaw = p.getYRot();
        lastPitch = p.getXRot();
        lastHealth = p.getHealth();
        lastHunger = p.getFoodData().getFoodLevel();

        JsonObject state = new JsonObject();

        // Player
        JsonObject player = new JsonObject();
        player.addProperty("name", p.getName().getString());
        player.addProperty("uuid", p.getUUID().toString());
        player.addProperty("health", p.getHealth());
        player.addProperty("max_health", p.getMaxHealth());
        player.addProperty("hunger", p.getFoodData().getFoodLevel());
        player.addProperty("saturation", p.getFoodData().getSaturationLevel());
        player.addProperty("x", p.getX());
        player.addProperty("y", p.getY());
        player.addProperty("z", p.getZ());
        player.addProperty("yaw", p.getYRot());
        player.addProperty("pitch", p.getXRot());
        player.addProperty("on_ground", p.onGround());
        player.addProperty("dimension", p.level().dimension().location().toString());
        if (p instanceof ServerPlayer sp) {
            player.addProperty("gamemode", sp.gameMode.getGameModeForPlayer().getName());
        } else {
            player.addProperty("gamemode", "unknown");
        }
        state.add("player", player);

        // World
        JsonObject world = new JsonObject();
        world.addProperty("time", mc.level.getDayTime());
        world.addProperty("is_day", mc.level.isDay());
        world.addProperty("is_raining", mc.level.isRaining());
        world.addProperty("light_level", mc.level.getMaxLocalRawBrightness(p.blockPosition()));
        state.add("world", world);

        // Inventory
        JsonArray inv = new JsonArray();
        var inventory = p.getInventory();
        for (int i = 0; i < inventory.getContainerSize(); i++) {
            ItemStack stack = inventory.getItem(i);
            if (!stack.isEmpty()) {
                JsonObject item = new JsonObject();
                item.addProperty("slot", i);
                item.addProperty("name", stack.getItem().toString());
                item.addProperty("count", stack.getCount());
                item.addProperty("display", stack.getDisplayName().getString());
                inv.add(item);
            }
        }
        state.add("inventory", inv);

        // Nearby entities (players, mobs, items)
        JsonArray entities = new JsonArray();
        var players = mc.level.players();
        for (var other : players) {
            if (other == p) continue;
            JsonObject e = new JsonObject();
            e.addProperty("name", other.getName().getString());
            e.addProperty("uuid", other.getUUID().toString());
            e.addProperty("distance", p.distanceTo(other));
            e.addProperty("type", "player");
            entities.add(e);
        }

        // Scan for nearby mobs and items within 16 blocks
        var searchBox = p.getBoundingBox().inflate(16);
        for (var entity : mc.level.getEntities(p, searchBox)) {
            if (entity == p) continue;
            // Skip other players (already handled above)
            if (entity instanceof Player) continue;

            JsonObject e = new JsonObject();
            e.addProperty("id", entity.getId());
            e.addProperty("name", entity.getName().getString());
            e.addProperty("distance", p.distanceTo(entity));
            e.addProperty("x", entity.getX());
            e.addProperty("y", entity.getY());
            e.addProperty("z", entity.getZ());

            if (entity instanceof ItemEntity) {
                e.addProperty("type", "item");
            } else if (entity instanceof Enemy) {
                e.addProperty("type", "hostile");
            } else if (entity instanceof Animal) {
                e.addProperty("type", "animal");
            } else {
                e.addProperty("type", "mob");
            }

            // Health info for living entities
            if (entity instanceof LivingEntity le) {
                e.addProperty("health", le.getHealth());
                e.addProperty("max_health", le.getMaxHealth());
            }

            entities.add(e);
        }
        state.add("entities", entities);

        LCUMod.WIRE.sendEvent("state_update", state);
    }
}
