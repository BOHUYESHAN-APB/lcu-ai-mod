package com.lcu.lcumod.state;

import com.google.gson.JsonArray;
import com.google.gson.JsonObject;
import com.lcu.lcumod.action.PoiMemory;
import com.lcu.lcumod.LCUMod;
import com.lcu.lcumod.config.ModConfig;
import com.lcu.lcumod.config.ServerPolicy;
import com.lcu.lcumod.client.ClientBodyRuntime;
import com.lcu.lcumod.compat.CuriosCompat;
import net.minecraft.client.Minecraft;
import net.minecraft.core.BlockPos;
import net.minecraft.core.registries.BuiltInRegistries;
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
import net.neoforged.fml.ModList;
import net.neoforged.neoforge.client.event.ClientTickEvent;

import java.util.ArrayList;
import java.util.Comparator;
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
    private static long lastGameTime = Long.MIN_VALUE;
    
    // Thresholds for change detection
    private static final double POSITION_THRESHOLD = 0.1;  // 0.1 blocks
    private static final float ROTATION_THRESHOLD = 1.0f;   // 1 degree
    private static final float HEALTH_THRESHOLD = 0.5f;     // 0.5 hearts
    private static final int FORCE_UPDATE_INTERVAL = 100;   // Force update every 5 seconds

    @SubscribeEvent
    public static void onClientTick(ClientTickEvent.Post event) {
        if (!ClientBodyRuntime.isBodyClient()) return;
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

        // Keep game-clock schedules responsive even while the player is idle.
        long gameTime = mc.level.getGameTime();
        if (lastGameTime == Long.MIN_VALUE || gameTime / 20L != lastGameTime / 20L) {
            hasChanged = true;
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
        lastGameTime = gameTime;

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
        world.addProperty("game_time", mc.level.getGameTime());
        world.addProperty("day_time", mc.level.getDayTime());
        world.addProperty("day_index", Math.floorDiv(mc.level.getDayTime(), 24000L));
        world.addProperty("time_of_day", Math.floorMod(mc.level.getDayTime(), 24000L));
        world.addProperty("is_day", mc.level.isDay());
        world.addProperty("is_raining", mc.level.isRaining());
        world.addProperty("light_level", mc.level.getMaxLocalRawBrightness(p.blockPosition()));
        world.addProperty("dimension", mc.level.dimension().location().toString());
        state.add("world", world);

        // Inventory
        JsonArray inv = new JsonArray();
        var inventory = p.getInventory();
        for (int i = 0; i < inventory.getContainerSize(); i++) {
            ItemStack stack = inventory.getItem(i);
            if (!stack.isEmpty()) {
                JsonObject item = new JsonObject();
                item.addProperty("slot", i);
                item.addProperty("name", BuiltInRegistries.ITEM.getKey(stack.getItem()).toString());
                item.addProperty("count", stack.getCount());
                item.addProperty("display", stack.getDisplayName().getString());
                if (i < 9) {
                    item.addProperty("section", "hotbar");
                } else if (i < 36) {
                    item.addProperty("section", "main");
                } else if (i < 40) {
                    item.addProperty("section", "armor");
                    item.addProperty("equipment_slot", switch (i) {
                        case 36 -> "feet";
                        case 37 -> "legs";
                        case 38 -> "chest";
                        default -> "head";
                    });
                } else {
                    item.addProperty("section", "offhand");
                    item.addProperty("equipment_slot", "offhand");
                }
                item.addProperty("selected", i == inventory.selected);
                inv.add(item);
            }
        }
        state.add("inventory", inv);

        JsonObject equipment = new JsonObject();
        equipment.add("mainhand", serializeEquipmentItem(p.getMainHandItem(), "mainhand"));
        equipment.add("offhand", serializeEquipmentItem(p.getOffhandItem(), "offhand"));
        JsonArray armor = new JsonArray();
        armor.add(serializeEquipmentItem(p.getItemBySlot(EquipmentSlot.HEAD), "head"));
        armor.add(serializeEquipmentItem(p.getItemBySlot(EquipmentSlot.CHEST), "chest"));
        armor.add(serializeEquipmentItem(p.getItemBySlot(EquipmentSlot.LEGS), "legs"));
        armor.add(serializeEquipmentItem(p.getItemBySlot(EquipmentSlot.FEET), "feet"));
        equipment.add("armor", armor);
        equipment.addProperty("armor_value", p.getArmorValue());
        equipment.addProperty("absorption", p.getAbsorptionAmount());
        JsonArray curios = CuriosCompat.collectEquipped(p);
        equipment.add("curios", curios);
        state.add("equipment", equipment);

        JsonObject integrations = new JsonObject();
        integrations.addProperty("curios_detected", ModList.get().isLoaded("curios"));
        integrations.addProperty("curios_items_available", CuriosCompat.isAvailable());
        integrations.addProperty("curios_item_count", curios.size());
        state.add("integrations", integrations);

        // Nearby entities (players, mobs, items)
        JsonArray entities = new JsonArray();
        boolean collectSurroundings = ServerPolicy.surroundingsCollectionAllowed();
        if (collectSurroundings) {
            var players = mc.level.players();
            for (var other : players) {
                if (other == p) continue;
                JsonObject e = new JsonObject();
                e.addProperty("name", other.getName().getString());
                e.addProperty("uuid", other.getUUID().toString());
                e.addProperty("distance", p.distanceTo(other));
                e.addProperty("type", "player");
                e.addProperty("dimension", other.level().dimension().location().toString());
                e.addProperty("armor_value", other.getArmorValue());
                e.addProperty("absorption", other.getAbsorptionAmount());
                entities.add(e);
            }
        }

        JsonArray onlinePlayers = new JsonArray();
        if (collectSurroundings && mc.getConnection() != null) {
            for (var info : mc.getConnection().getOnlinePlayers()) {
                JsonObject online = new JsonObject();
                online.addProperty("name", info.getProfile().getName());
                online.addProperty("uuid", info.getProfile().getId().toString());
                online.addProperty("latency", info.getLatency());
                if (info.getGameMode() != null) {
                    online.addProperty("game_mode", info.getGameMode().getName());
                }
                var loadedPlayer = mc.level.getPlayerByUUID(info.getProfile().getId());
                boolean loaded = loadedPlayer != null;
                online.addProperty("loaded", loaded);
                if (loaded && loadedPlayer != p) {
                    online.addProperty("distance", p.distanceTo(loadedPlayer));
                }
                if (loaded) {
                    online.addProperty("dimension", loadedPlayer.level().dimension().location().toString());
                }
                onlinePlayers.add(online);
            }
        }
        state.add("online_players", onlinePlayers);

        // Scan for nearby mobs and items within 16 blocks
        var searchBox = p.getBoundingBox().inflate(16);
        for (var entity : collectSurroundings ? mc.level.getEntities(p, searchBox) : List.<Entity>of()) {
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
                var stack = ((ItemEntity) entity).getItem();
                e.addProperty("item_id", BuiltInRegistries.ITEM.getKey(stack.getItem()).toString());
                e.addProperty("item_count", stack.getCount());
                e.addProperty("display", stack.getDisplayName().getString());
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
                e.addProperty("armor_value", le.getArmorValue());
                e.addProperty("absorption", le.getAbsorptionAmount());
            }

            entities.add(e);
        }
        state.add("entities", entities);

        // Nearby blocks/resources — compact perception map for planning
        List<JsonObject> nearbyBlocks = new ArrayList<>();
        BlockPos origin = p.blockPosition();
        if (collectSurroundings) {
            for (int dx = -6; dx <= 6; dx++) {
                for (int dy = -2; dy <= 2; dy++) {
                    for (int dz = -6; dz <= 6; dz++) {
                    BlockPos pos = origin.offset(dx, dy, dz);
                    var blockState = mc.level.getBlockState(pos);
                    if (blockState.isAir()) continue;

                    double centerX = pos.getX() + 0.5;
                    double centerY = pos.getY() + 0.5;
                    double centerZ = pos.getZ() + 0.5;
                    double distance = Math.sqrt(
                        Math.pow(centerX - p.getX(), 2)
                            + Math.pow(centerY - p.getY(), 2)
                            + Math.pow(centerZ - p.getZ(), 2)
                    );
                    if (distance > 8.5) continue;

                    JsonObject block = new JsonObject();
                    String blockId = BuiltInRegistries.BLOCK.getKey(blockState.getBlock()).toString();
                    block.addProperty("name", blockState.getBlock().getName().getString());
                    block.addProperty("block_id", blockId);
                    block.addProperty("item_id", BuiltInRegistries.ITEM.getKey(blockState.getBlock().asItem()).toString());
                    block.addProperty("distance", Math.round(distance * 10.0) / 10.0);
                    block.addProperty("x", pos.getX());
                    block.addProperty("y", pos.getY());
                    block.addProperty("z", pos.getZ());
                        nearbyBlocks.add(block);
                    }
                }
            }
        }
        nearbyBlocks.sort(Comparator.comparingDouble(block -> block.get("distance").getAsDouble()));
        JsonArray nearbyBlocksJson = new JsonArray();
        for (int i = 0; i < Math.min(32, nearbyBlocks.size()); i++) {
            nearbyBlocksJson.add(nearbyBlocks.get(i));
        }
        state.add("nearby_blocks", nearbyBlocksJson);

        JsonArray nearbyWorkstations = new JsonArray();
        if (collectSurroundings) {
            for (JsonObject item : PoiMemory.snapshot(mc, "workstation", PoiMemory.INTERACTION_RADIUS, 16)) {
                nearbyWorkstations.add(item);
            }
        }
        state.add("nearby_workstations", nearbyWorkstations);

        JsonArray nearbyStorage = new JsonArray();
        if (collectSurroundings) {
            for (JsonObject item : PoiMemory.snapshotWithContents(mc, "storage", PoiMemory.INTERACTION_RADIUS, 16)) {
                nearbyStorage.add(item);
            }
        }
        state.add("nearby_storage", nearbyStorage);

        LCUMod.WIRE.sendEvent("state_update", state);
    }

    private static JsonObject serializeEquipmentItem(ItemStack stack, String slot) {
        JsonObject item = new JsonObject();
        item.addProperty("slot", slot);
        item.addProperty("empty", stack.isEmpty());
        if (!stack.isEmpty()) {
            item.addProperty("name", BuiltInRegistries.ITEM.getKey(stack.getItem()).toString());
            item.addProperty("count", stack.getCount());
            item.addProperty("display", stack.getDisplayName().getString());
            item.addProperty("damage", stack.getDamageValue());
            item.addProperty("max_damage", stack.getMaxDamage());
        }
        return item;
    }
}
