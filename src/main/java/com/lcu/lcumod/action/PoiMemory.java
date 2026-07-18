package com.lcu.lcumod.action;

import com.google.gson.JsonArray;
import com.google.gson.JsonObject;
import net.minecraft.client.Minecraft;
import net.minecraft.core.BlockPos;
import net.minecraft.core.registries.BuiltInRegistries;

import java.util.ArrayList;
import java.util.Comparator;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;

/**
 * Lightweight memory of nearby points-of-interest (POIs).
 *
 * Current vanilla scope:
 * - Workstations used by the current closed-loop pipeline
 * - Storage blocks used for future warehouse access
 *
 * Future migration notes (intentionally kept as comments for higher-version work):
 * - smithing_table
 * - stonecutter
 * - loom
 * - cartography_table
 * - grindstone
 * - brewing_stand
 * - shulker_box variants
 */
public final class PoiMemory {

    public static final int SCAN_RADIUS_XZ = 24;
    public static final int SCAN_RADIUS_Y = 6;
    public static final int INTERACTION_RADIUS = 32;
    private static final int RESCAN_INTERVAL_TICKS = 40;
    private static final int STALE_TICKS = 20 * 300;

    private static final Set<String> WORKSTATIONS = Set.of(
        "minecraft:crafting_table",
        "minecraft:furnace",
        "minecraft:blast_furnace",
        "minecraft:smoker"
        // "minecraft:smithing_table",
        // "minecraft:stonecutter",
        // "minecraft:loom",
        // "minecraft:cartography_table",
        // "minecraft:grindstone",
        // "minecraft:brewing_stand"
    );

    private static final Set<String> STORAGE = Set.of(
        "minecraft:chest",
        "minecraft:trapped_chest",
        "minecraft:barrel"
        // "minecraft:ender_chest",
        // "minecraft:shulker_box"
    );

    private static final Map<BlockPos, PoiEntry> MEMORY = new HashMap<>();
    private static final Map<BlockPos, Map<String, Integer>> STORAGE_CONTENTS = new HashMap<>();
    private static final Map<BlockPos, Integer> CONTENTS_LAST_SEEN = new HashMap<>();

    private PoiMemory() {
    }

    private static final class PoiEntry {
        final String blockId;
        final String category;
        int lastSeenTick;

        PoiEntry(String blockId, String category, int lastSeenTick) {
            this.blockId = blockId;
            this.category = category;
            this.lastSeenTick = lastSeenTick;
        }
    }

    public static void tick(Minecraft mc, int tickCount) {
        if (mc == null || mc.player == null || mc.level == null) {
            return;
        }
        if (tickCount % RESCAN_INTERVAL_TICKS != 0) {
            return;
        }

        BlockPos origin = mc.player.blockPosition();
        for (int dx = -SCAN_RADIUS_XZ; dx <= SCAN_RADIUS_XZ; dx++) {
            for (int dy = -SCAN_RADIUS_Y; dy <= SCAN_RADIUS_Y; dy++) {
                for (int dz = -SCAN_RADIUS_XZ; dz <= SCAN_RADIUS_XZ; dz++) {
                    BlockPos pos = origin.offset(dx, dy, dz);
                    var state = mc.level.getBlockState(pos);
                    if (state.isAir()) continue;

                    String blockId = BuiltInRegistries.BLOCK.getKey(state.getBlock()).toString();
                    if (WORKSTATIONS.contains(blockId)) {
                        MEMORY.put(pos.immutable(), new PoiEntry(blockId, "workstation", tickCount));
                    } else if (isStorageBlock(blockId)) {
                        MEMORY.put(pos.immutable(), new PoiEntry(blockId, "storage", tickCount));
                    }
                }
            }
        }

        MEMORY.entrySet().removeIf(entry -> {
            PoiEntry poi = entry.getValue();
            if (tickCount - poi.lastSeenTick > STALE_TICKS) {
                return true;
            }
            var state = mc.level.getBlockState(entry.getKey());
            String currentId = BuiltInRegistries.BLOCK.getKey(state.getBlock()).toString();
            return !currentId.equals(poi.blockId);
        });
    }

    public static BlockPos findNearest(Minecraft mc, Set<String> blockIds, int maxDistance) {
        if (mc == null || mc.player == null) {
            return null;
        }
        BlockPos best = null;
        double bestDistance = Double.MAX_VALUE;
        for (Map.Entry<BlockPos, PoiEntry> entry : MEMORY.entrySet()) {
            if (!blockIds.contains(entry.getValue().blockId)) {
                continue;
            }
            double distance = entry.getKey().distSqr(mc.player.blockPosition());
            if (distance > (double) maxDistance * maxDistance) {
                continue;
            }
            if (distance < bestDistance) {
                bestDistance = distance;
                best = entry.getKey();
            }
        }
        return best;
    }

    public static BlockPos findNearestStorage(Minecraft mc, int maxDistance) {
        List<JsonObject> storage = snapshot(mc, "storage", maxDistance, 1);
        if (storage.isEmpty()) return null;
        JsonObject nearest = storage.get(0);
        return new BlockPos(nearest.get("x").getAsInt(), nearest.get("y").getAsInt(), nearest.get("z").getAsInt());
    }

    public static void updateStorageContents(BlockPos pos, Map<String, Integer> contents, int tickCount) {
        STORAGE_CONTENTS.put(pos.immutable(), new HashMap<>(contents));
        CONTENTS_LAST_SEEN.put(pos.immutable(), tickCount);
    }

    public static int getStorageItemCount(BlockPos pos, String itemId) {
        Map<String, Integer> contents = STORAGE_CONTENTS.get(pos);
        if (contents == null) return 0;
        int total = 0;
        for (Map.Entry<String, Integer> entry : contents.entrySet()) {
            if (CraftingPlanner.matchesRegistryId(entry.getKey(), itemId)) {
                total += entry.getValue();
            }
        }
        return total;
    }

    public static boolean hasKnownContents(BlockPos pos) {
        return STORAGE_CONTENTS.containsKey(pos);
    }

    public static List<JsonObject> snapshotSortedByItemMatch(Minecraft mc, String category, String targetItemId, int maxDistance, int limit) {
        List<JsonObject> items = snapshot(mc, category, maxDistance, limit);
        items.sort((a, b) -> {
            BlockPos posA = new BlockPos(a.get("x").getAsInt(), a.get("y").getAsInt(), a.get("z").getAsInt());
            BlockPos posB = new BlockPos(b.get("x").getAsInt(), b.get("y").getAsInt(), b.get("z").getAsInt());
            int countA = getStorageItemCount(posA, targetItemId);
            int countB = getStorageItemCount(posB, targetItemId);
            if (countA != countB) return Integer.compare(countB, countA);
            return Double.compare(a.get("distance").getAsDouble(), b.get("distance").getAsDouble());
        });
        return items;
    }

    public static List<JsonObject> snapshotWithContents(Minecraft mc, String category, int maxDistance, int limit) {
        List<JsonObject> result = new ArrayList<>();
        if (mc == null || mc.player == null) {
            return result;
        }
        for (Map.Entry<BlockPos, PoiEntry> entry : MEMORY.entrySet()) {
            PoiEntry poi = entry.getValue();
            if (!poi.category.equals(category)) {
                continue;
            }
            double distance = Math.sqrt(entry.getKey().distSqr(mc.player.blockPosition()));
            if (distance > maxDistance) {
                continue;
            }
            JsonObject item = new JsonObject();
            item.addProperty("block_id", poi.blockId);
            item.addProperty("distance", Math.round(distance * 10.0) / 10.0);
            item.addProperty("x", entry.getKey().getX());
            item.addProperty("y", entry.getKey().getY());
            item.addProperty("z", entry.getKey().getZ());

            Map<String, Integer> contents = STORAGE_CONTENTS.get(entry.getKey());
            item.addProperty("contents_known", contents != null);
            if (contents != null && !contents.isEmpty()) {
                JsonArray contentsArr = new JsonArray();
                for (var contentEntry : contents.entrySet()) {
                    JsonObject contentItem = new JsonObject();
                    contentItem.addProperty("item_id", contentEntry.getKey());
                    contentItem.addProperty("count", contentEntry.getValue());
                    contentsArr.add(contentItem);
                }
                item.add("contents", contentsArr);
            }

            result.add(item);
        }
        result.sort(Comparator.comparingDouble(item -> item.get("distance").getAsDouble()));
        return result.size() <= limit ? result : result.subList(0, limit);
    }

    public static List<JsonObject> snapshot(Minecraft mc, String category, int maxDistance, int limit) {
        List<JsonObject> result = new ArrayList<>();
        if (mc == null || mc.player == null) {
            return result;
        }
        for (Map.Entry<BlockPos, PoiEntry> entry : MEMORY.entrySet()) {
            PoiEntry poi = entry.getValue();
            if (!poi.category.equals(category)) {
                continue;
            }
            double distance = Math.sqrt(entry.getKey().distSqr(mc.player.blockPosition()));
            if (distance > maxDistance) {
                continue;
            }
            JsonObject item = new JsonObject();
            item.addProperty("block_id", poi.blockId);
            item.addProperty("distance", Math.round(distance * 10.0) / 10.0);
            item.addProperty("x", entry.getKey().getX());
            item.addProperty("y", entry.getKey().getY());
            item.addProperty("z", entry.getKey().getZ());
            result.add(item);
        }
        result.sort(Comparator.comparingDouble(item -> item.get("distance").getAsDouble()));
        return result.size() <= limit ? result : result.subList(0, limit);
    }

    private static boolean isStorageBlock(String blockId) {
        if (STORAGE.contains(blockId)) return true;
        int separator = blockId.indexOf(':');
        String namespace = separator >= 0 ? blockId.substring(0, separator) : "minecraft";
        String path = separator >= 0 ? blockId.substring(separator + 1) : blockId;
        if (namespace.equals("ironchest")) {
            return path.endsWith("chest");
        }
        if (namespace.equals("sophisticatedstorage")) {
            return path.contains("chest") || path.contains("barrel");
        }
        return false;
    }
}
