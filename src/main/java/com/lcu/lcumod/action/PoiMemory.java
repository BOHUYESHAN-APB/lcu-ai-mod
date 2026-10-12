package com.lcu.lcumod.action;

import com.lcu.lcumod.LCUMod;

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
        /**
         * 主标签（状态里报的就是它；向后兼容）。**可变**：床被拆了之后主标签会从 {@code bed}
         * 退成 {@code home} —— 点还在、"家"的意图还在，只是"床"这个标签作废了。
         */
        String category;
        /** 全部标签：一个点可以同时是 {@code bed} + {@code home}（用户口径"记忆内容要有标签"）。 */
        final Set<String> tags = new java.util.LinkedHashSet<>();
        /** 主动记录的点不会被"扫描看不见"淘汰 —— 这就是"记忆"与"当前视野"的区别。 */
        final boolean persistent;
        final String name;
        int lastSeenTick;

        PoiEntry(String blockId, String category, int lastSeenTick) {
            this(blockId, category, lastSeenTick, false, "");
        }

        PoiEntry(String blockId, String category, int lastSeenTick, boolean persistent, String name) {
            this.blockId = blockId;
            this.category = category == null || category.isBlank() ? "waypoint" : category;
            this.lastSeenTick = lastSeenTick;
            this.persistent = persistent;
            this.name = name == null ? "" : name;
            this.tags.add(this.category);
        }

        /** 挂标签（幂等）。 */
        void addTag(String tag) {
            if (tag != null && !tag.isBlank()) {
                tags.add(tag);
            }
        }

        /** 摘标签；摘掉的若是主标签，主标签退到剩下的第一个（空则退回 waypoint）。 */
        void removeTag(String tag) {
            if (tags.remove(tag) && tag.equals(category)) {
                category = tags.isEmpty() ? "waypoint" : tags.iterator().next();
            }
        }

        boolean hasTag(String tag) {
            return tags.contains(tag);
        }
    }

    /** 主动记录一个点（"记住这里"/走到目标方块后）。persistent → 不会被扫描淘汰。
     *
     * <p>**同一个点可以挂多个标签**（例如"床"这条记录随后又挂上 `home`）：已存在就只补标签，
     * 不覆盖原有信息 —— 用户口径"记忆内容要有标签"、且"home 的标签没错，只是床的标签没了"。
     */
    public static void remember(BlockPos pos, String blockId, String category, String name, int tickCount) {
        if (pos == null) {
            return;
        }
        BlockPos key = pos.immutable();
        String tag = category == null || category.isBlank() ? "waypoint" : category;
        PoiEntry existing = MEMORY.get(key);
        if (existing != null && existing.persistent) {
            existing.addTag(tag);
            existing.lastSeenTick = tickCount;
            return;
        }
        MEMORY.put(key, new PoiEntry(blockId == null ? "" : blockId, tag, tickCount, true, name));
    }

    /** 主动记录过的点（可能很远、可能不在视野内），按距离排序。 */
    public static List<JsonObject> snapshotRemembered(Minecraft mc, int limit) {
        List<JsonObject> result = new ArrayList<>();
        if (mc == null || mc.player == null) {
            return result;
        }
        BlockPos origin = mc.player.blockPosition();
        for (Map.Entry<BlockPos, PoiEntry> entry : MEMORY.entrySet()) {
            PoiEntry poi = entry.getValue();
            if (!poi.persistent) {
                continue;
            }
            JsonObject item = new JsonObject();
            item.addProperty("block_id", poi.blockId);
            item.addProperty("category", poi.category);
            JsonArray tagArray = new JsonArray();
            for (String tag : poi.tags) {
                tagArray.add(tag);
            }
            item.add("tags", tagArray);
            item.addProperty("name", poi.name);
            item.addProperty("x", entry.getKey().getX());
            item.addProperty("y", entry.getKey().getY());
            item.addProperty("z", entry.getKey().getZ());
            item.addProperty("distance", Math.round(Math.sqrt(entry.getKey().distSqr(origin)) * 10.0) / 10.0);
            result.add(item);
        }
        result.sort(Comparator.comparingDouble(item -> item.get("distance").getAsDouble()));
        return result.size() <= limit ? result : new ArrayList<>(result.subList(0, limit));
    }

    public static void tick(Minecraft mc, int tickCount) {
        if (mc == null || mc.player == null || mc.level == null) {
            return;
        }
        if (tickCount % RESCAN_INTERVAL_TICKS != 0) {
            return;
        }

        // 主动记地标（重生点/出生地）—— 不用人类提醒，每局只写一次。
        rememberLandmarks(mc, tickCount);
        // 床丢了就作废 bed 标签（home 留着，它会自己回去重找/重做）。
        invalidateBedIfGone(mc, tickCount);

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
                    } else if (STORAGE.contains(blockId)) {
                        MEMORY.put(pos.immutable(), new PoiEntry(blockId, "storage", tickCount));
                    }
                }
            }
        }

        MEMORY.entrySet().removeIf(entry -> {
            PoiEntry poi = entry.getValue();
            if (poi.persistent) {
                return false;
            }
            if (tickCount - poi.lastSeenTick > STALE_TICKS) {
                return true;
            }
            var state = mc.level.getBlockState(entry.getKey());
            String currentId = BuiltInRegistries.BLOCK.getKey(state.getBlock()).toString();
            return !currentId.equals(poi.blockId);
        });
    }

    /**
     * 主动记住"家"这一类地标（**不用人类说**，用户口径："应该主动去保存坐标点，而不是人类说了他才做"）。
     *
     * <p>记的是**原版的重生点**（{@code getRespawnData()}）：它默认就是世界出生地，
     * **睡过床之后自动变成那张床的位置** —— 所以"床的位置默认作为 home"直接用原版语义，
     * 不需要我们自己去猜哪张床算家（哪张床都不需要判）。
     *
     * <p>幂等：同类地标已经记过就不再写（"一般情况下，主动保存 1 次即可"）。
     */
    public static void rememberLandmarks(Minecraft mc, int tickCount) {
        if (mc == null || mc.level == null || mc.player == null) {
            return;
        }
        try {
            var data = mc.level.getRespawnData();
            if (data != null && data.pos() != null && !hasCategory("respawn")) {
                remember(data.pos(), "", "respawn", "重生点（出生地/床）", tickCount);
                LCUMod.LOGGER.info("[PoiMemory] 主动记住重生点 {}（出生地；睡过床后它自动变成床）",
                        data.pos().toShortString());
            }
        } catch (Exception e) {
            LCUMod.LOGGER.debug("[PoiMemory] 读取重生点失败: {}", e.toString());
        }
    }

    /**
     * 看到床就主动记一次（"床的位置默认作为 home"、"不用人类说"）。
     *
     * <p>第一次看到床时：这个点同时挂 **{@code bed} + {@code home}** 两个标签
     * （用户口径："用户让他造床 / 他第一次找到的床的位置，会被设置为 home，当然可后续变更"）。
     */
    public static void rememberBed(BlockPos pos, String blockId, int tickCount) {
        if (pos == null) {
            return;
        }
        boolean firstBed = !hasTag("bed");
        remember(pos, blockId == null ? "" : blockId, "bed", "床", tickCount);
        remember(pos, blockId == null ? "" : blockId, "home", "家", tickCount);
        if (firstBed) {
            LCUMod.LOGGER.info("[PoiMemory] 第一次看到床 {} → 记为 床 + home（后续可变更）", pos.toShortString());
        }
    }

    /**
     * 床丢了/被拆了 ⇒ **把 {@code bed} 标签作废**（{@code home} 标签留着 —— 那是"还想去的地方"）。
     *
     * <p><b>判据只用"亲眼看过的方块"</b>：那个位置加载着、且已经不是床方块 ⇒ 床真的没了。
     * 原版重生点只用来做**正向确认**（等于这张床 ⇒ 确定它还在），**不能用来否定** ——
     * 玩家没在这张床上睡过，重生点就永远不会是它。
     *
     * <p>教训（真机日志，2026-10-12）：上一版把"复活点不是这张床"也当成"床没了"，于是
     * `第一次看到床 → 1 秒后作废 bed 标签 → 下次又'第一次看到'` 反复循环（880/852/851 一路重记）。
     * 用户原话："你再读一下日志，它有刚才有没有找到床" —— 一读就现形。
     */
    public static void invalidateBedIfGone(Minecraft mc, int tickCount) {
        if (mc == null || mc.level == null) {
            return;
        }
        BlockPos respawn = null;
        try {
            var data = mc.level.getRespawnData();
            respawn = data == null ? null : data.pos();
        } catch (Exception ignored) {
            // 拿不到重生点就只靠方块检查（少一条正向确认，不影响结论）。
        }
        for (Map.Entry<BlockPos, PoiEntry> entry : MEMORY.entrySet()) {
            PoiEntry poi = entry.getValue();
            if (!poi.hasTag("bed")) {
                continue;
            }
            BlockPos pos = entry.getKey();
            if (!mc.level.isLoaded(pos)) {
                // 区块没加载 ⇒ 看不到就没资格下结论（不猜）。
                continue;
            }
            String currentId = BuiltInRegistries.BLOCK.getKey(mc.level.getBlockState(pos).getBlock()).toString();
            boolean stillBed = currentId.endsWith("_bed");
            boolean respawnIsThisBed = respawn != null && respawn.equals(pos);
            if (stillBed || respawnIsThisBed) {
                if (respawnIsThisBed) {
                    // 原版说"复活点就是这张床" ⇒ 正向确认它还在。
                    poi.lastSeenTick = tickCount;
                }
                continue;
            }
            poi.removeTag("bed");
            poi.lastSeenTick = tickCount;
            LCUMod.LOGGER.warn("[PoiMemory] 床没了（那个位置已经不是床方块）→ 作废 bed 标签，保留 home {}"
                            + "（接下来自己回去找床/重新做一张）", pos.toShortString());
        }
    }

    /** 记忆里是否已有某个标签（幂等用：主动存一次就够）。 */
    private static boolean hasTag(String tag) {
        for (PoiEntry poi : MEMORY.values()) {
            if (poi.hasTag(tag)) {
                return true;
            }
        }
        return false;
    }

    /** 记忆里是否已有某个类别（幂等用：主动存一次就够）。 */
    private static boolean hasCategory(String category) {
        for (PoiEntry poi : MEMORY.values()) {
            if (category.equals(poi.category)) {
                return true;
            }
        }
        return false;
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
        return findNearest(mc, STORAGE, maxDistance);
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
}
