package com.lcu.lcumod.state;

import com.google.gson.JsonArray;
import com.google.gson.JsonObject;
import com.lcu.lcumod.action.AttackRules;
import com.lcu.lcumod.action.BlockFinder;
import com.lcu.lcumod.action.MobPolicy;
import com.lcu.lcumod.action.PoiMemory;
import com.lcu.lcumod.LCUMod;
import com.lcu.lcumod.config.ModConfig;
import net.minecraft.client.Minecraft;
import net.minecraft.core.BlockPos;
import net.minecraft.core.registries.BuiltInRegistries;
import net.minecraft.server.level.ServerPlayer;
import net.minecraft.world.entity.*;
import net.minecraft.world.entity.player.Player;
import net.minecraft.world.item.ItemStack;
import net.minecraft.world.entity.item.ItemEntity;
import net.minecraft.world.phys.AABB;
import net.neoforged.bus.api.SubscribeEvent;
import net.neoforged.fml.common.EventBusSubscriber;
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
    private static boolean lastOnFire = false;
    private static boolean lastInLava = false;
    private static int lastAirSupply = 300;
    private static int updateCounter = 0;
    // 夜晚/睡眠状态（变化要立刻上报，"夜晚自动睡觉"规则依赖它）
    private static boolean lastNight = false;
    private static boolean lastSleeping = false;
    // 附近的床：缓存扫描结果（每 40 tick 刷新一次，避免每次上报都扫 27 万方块）
    private static int bedScanCooldown = 0;
    private static JsonArray cachedBeds = new JsonArray();

    // 周边方块感知：**限半径 + 定期探测**（参考项目也是"每轮现算 + 冷却"，不是每 tick 全扫）。
    private static JsonArray cachedNearbyBlocks = null;
    private static BlockPos cachedNearbyBlocksOrigin = null;
    private static int cachedNearbyBlocksTick = 0;
    private static final int NEARBY_BLOCKS_TTL_TICKS = 20;   // 最长一秒重扫一次；走出一格则立刻重扫
    // Set by requestRefresh() to force the next tick to send a full state_update
    private static volatile boolean refreshRequested = false;
    
    // Thresholds for change detection
    private static final double POSITION_THRESHOLD = 0.1;  // 0.1 blocks
    private static final float ROTATION_THRESHOLD = 1.0f;   // 1 degree
    private static final float HEALTH_THRESHOLD = 0.5f;     // 0.5 hearts
    private static final int FORCE_UPDATE_INTERVAL = 100;   // Force update every 5 seconds

    /**
     * 周边方块感知：**限半径（配置） + 定期探测（带缓存）**。
     *
     * <p>用户口径："感知要限定半径 + 定期探测"；参考项目（mindcraft）也是每轮现算 + prompt 冷却，
     * 不是每 tick 全扫。这里缓存 {@value #NEARBY_BLOCKS_TTL_TICKS} tick，玩家**走出一格**立刻重扫
     * （站着不动时就没必要一秒扫几十次）；半径与总开关走 {@code ModConfig}
     * —— 这两个配置项以前**声明了却没人读**，等于摆设，现在真的生效。
     */
    private static JsonArray collectNearbyBlocks(Minecraft mc, Player p, BlockPos origin) {
        boolean enabled;
        int radius;
        try {
            enabled = ModConfig.COLLECT_SURROUNDINGS.getAsBoolean();
            radius = Math.max(2, ModConfig.SURROUNDINGS_RADIUS.getAsInt());
        } catch (IllegalStateException e) {
            enabled = true;
            radius = 8;
        }
        if (!enabled) {
            return new JsonArray();
        }
        if (cachedNearbyBlocks != null
                && tickCounter - cachedNearbyBlocksTick < NEARBY_BLOCKS_TTL_TICKS
                && cachedNearbyBlocksOrigin != null
                && origin.distSqr(cachedNearbyBlocksOrigin) < 1.0) {
            return cachedNearbyBlocks;
        }

        List<JsonObject> nearbyBlocks = new ArrayList<>();
        for (int dx = -radius; dx <= radius; dx++) {
            for (int dy = -2; dy <= 2; dy++) {
                for (int dz = -radius; dz <= radius; dz++) {
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
                    if (distance > radius) continue;

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
        nearbyBlocks.sort(Comparator.comparingDouble(block -> block.get("distance").getAsDouble()));
        JsonArray nearbyBlocksJson = new JsonArray();
        for (int i = 0; i < Math.min(32, nearbyBlocks.size()); i++) {
            nearbyBlocksJson.add(nearbyBlocks.get(i));
        }
        cachedNearbyBlocks = nearbyBlocksJson;
        cachedNearbyBlocksOrigin = origin.immutable();
        cachedNearbyBlocksTick = tickCounter;
        return nearbyBlocksJson;
    }

    /** Force the next collector tick (next client tick, bypassing the interval and
     *  change detection) to send a full state_update immediately. */
    public static void requestRefresh() {
        refreshRequested = true;
    }

    private static String lastScreenName = "";
    private static int presenceTicks = 0;
    /** 当前容器内容指纹（-1 = 没开外部容器）；容器开关或内容变化要立刻上报。 */
    private static int lastContainerFingerprint = -1;

    /**
     * 不在世界里时发的最小状态：`in_world=false` + 当前界面名。
     *
     * 后端据此暂停（不调 LLM、不下指令、不烧 token）；界面一变立刻发，其余每 2 秒心跳一次。
     * 记忆分区直接用已有的 `world.server`（ip）/`world.world_name`，不再另立字段。
     */
    private static void sendPresenceOnly(Minecraft mc, boolean forceRefresh) {
        String screen = com.lcu.lcumod.client.ScreenWatcher.current();
        if (screen == null) {
            screen = "";
        }
        presenceTicks++;
        if (!forceRefresh && screen.equals(lastScreenName) && presenceTicks % 40 != 0) {
            return;
        }
        lastScreenName = screen;
        JsonObject state = new JsonObject();
        state.addProperty("in_world", false);
        state.addProperty("ui_screen", screen);
        state.addProperty("timestamp", System.currentTimeMillis());
        LCUMod.WIRE.sendEvent("state_update", state);
    }

    /** 单个装备槽摘要：{@code name}=物品 id（注册名）、{@code count}、{@code display}；空槽 → 空对象 {@code {}}。 */
    private static JsonObject slotItem(ItemStack stack) {
        JsonObject o = new JsonObject();
        if (stack != null && !stack.isEmpty()) {
            o.addProperty("name", stack.getItem().toString());
            o.addProperty("count", stack.getCount());
            o.addProperty("display", stack.getDisplayName().getString());
        }
        return o;
    }

    /** 存档/服务器标识：单人用存档名，多人用服务器条目名；无法确定时返回空串。 */
    private static String resolveWorldName(Minecraft mc) {
        try {
            if (mc.hasSingleplayerServer() && mc.getSingleplayerServer() != null) {
                return mc.getSingleplayerServer().getWorldData().getLevelName();
            }
            var server = mc.getCurrentServer();
            if (server != null && server.name != null) {
                return server.name;
            }
        } catch (Exception ignored) {
            // 身份信息缺失不应影响状态上报
        }
        return "";
    }

    /** 服务器标识：单人固定为 singleplayer，多人用 ip；无法确定时返回 unknown。 */
    private static String resolveServerLabel(Minecraft mc) {
        try {
            if (mc.hasSingleplayerServer()) {
                return "singleplayer";
            }
            var server = mc.getCurrentServer();
            if (server != null && server.ip != null && !server.ip.isBlank()) {
                return server.ip;
            }
        } catch (Exception ignored) {
            // 同上
        }
        return "unknown";
    }

    @SubscribeEvent
    public static void onClientTick(ClientTickEvent.Post event) {
        tickCounter++;
        boolean forceRefresh = refreshRequested;
        refreshRequested = false;

        // 死亡点：每 tick 更新"最近活着的位置"，死了就记一条 —— 这一步**不能**受 interval 节流，
        // 否则死亡那一瞬间可能刚好被跳过。
        com.lcu.lcumod.action.DeathPoint.tick(Minecraft.getInstance());

        // Check if config is loaded before accessing it
        try {
            int interval = ModConfig.STATE_INTERVAL.getAsInt();
            if (tickCounter % interval != 0 && !forceRefresh) return;
        } catch (IllegalStateException e) {
            // Config not loaded yet, skip this tick
            return;
        }

        if (LCUMod.WIRE == null || !LCUMod.WIRE.isConnected()) return;

        var mc = Minecraft.getInstance();
        if (mc.level == null || mc.player == null) {
            // 不在世界里（主菜单 / 多人列表 / 加载中）：**也必须上报**，
            // 否则后端拿着上一秒的世界状态继续下指令、继续烧 token。
            sendPresenceOnly(mc, forceRefresh);
            return;
        }

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
        
        // Survival flags: fire, lava and drowning must propagate immediately so the
        // P0 reflex layer can react. Air supply only steps every 10 ticks to avoid
        // sending a state update on every single tick while submerged.
        if (p.isOnFire() != lastOnFire
            || p.isInLava() != lastInLava
            || Math.abs(p.getAirSupply() - lastAirSupply) >= 10) {
            hasChanged = true;
        }

        // 夜晚降临 / 上床睡觉：必须立刻上报，否则"夜晚自动睡觉"要等下一次位置变化才知道
        {
            boolean nightNow = mc.level.getSkyDarken() >= 4 || mc.level.isThundering();
            boolean sleepingNow = p.isSleeping();
            if (nightNow != lastNight || sleepingNow != lastSleeping) {
                hasChanged = true;
                lastNight = nightNow;
                lastSleeping = sleepingNow;
            }
        }
        
        // 容器开/关/内容变化必须立刻上报：后端靠 open_container.items 才知道每格是什么、能不能点
        {
            int fingerprintNow = com.lcu.lcumod.action.ContainerController.containerFingerprint(mc);
            if (fingerprintNow != lastContainerFingerprint) {
                hasChanged = true;
                lastContainerFingerprint = fingerprintNow;
            }
        }

        // Force update periodically
        if (updateCounter >= FORCE_UPDATE_INTERVAL) {
            hasChanged = true;
            updateCounter = 0;
        }
        
        // Skip if no significant change (unless a refresh was requested)
        if (!hasChanged && !forceRefresh) return;
        
        // Update last sent state
        lastX = p.getX();
        lastY = p.getY();
        lastZ = p.getZ();
        lastYaw = p.getYRot();
        lastPitch = p.getXRot();
        lastHealth = p.getHealth();
        lastHunger = p.getFoodData().getFoodLevel();
        lastOnFire = p.isOnFire();
        lastInLava = p.isInLava();
        lastAirSupply = p.getAirSupply();

        JsonObject state = new JsonObject();
        // 在场判定：不在世界里时后端必须停下来（主菜单/多人列表/加载中 → in_world=false）
        state.addProperty("in_world", true);
        state.addProperty("ui_screen", "");
        // 本能反射正在干什么：后端据此**不要重复**同一件事（后端与本地反射同时出手 = 动作抖动/循环）
        state.addProperty("reflex_combat", com.lcu.lcumod.action.CombatReflex.isEngaged());
        state.addProperty("reflex_swim", com.lcu.lcumod.action.SwimReflex.isActive());
        state.addProperty("reflex_eat", com.lcu.lcumod.action.ActionExecutor.isReflexEating());
        // 落地水（MLG）反射：armed/phase/reason/receipt（与上面三个反射同口径）
        state.add("reflex_mlg", com.lcu.lcumod.action.MlgReflex.state());

        // Player
        JsonObject player = new JsonObject();
        player.addProperty("name", p.getName().getString());
        player.addProperty("uuid", p.getUUID().toString());
        player.addProperty("health", p.getHealth());
        player.addProperty("max_health", p.getMaxHealth());
        player.addProperty("hunger", p.getFoodData().getFoodLevel());
        // 隐藏饱食度：饱食度 ≥18 **且** 饱和度 >0 才会自然回血（用户点名要搞清楚这两个概念）
        player.addProperty("saturation", p.getFoodData().getSaturationLevel());
        player.addProperty("x", p.getX());
        player.addProperty("y", p.getY());
        player.addProperty("z", p.getZ());
        player.addProperty("yaw", p.getYRot());
        player.addProperty("pitch", p.getXRot());
        player.addProperty("on_ground", p.onGround());
        player.addProperty("on_fire", p.isOnFire());
        player.addProperty("fire_ticks", p.getRemainingFireTicks());
        player.addProperty("air_ticks", p.getAirSupply());
        player.addProperty("max_air_ticks", p.getMaxAirSupply());
        player.addProperty("in_lava", p.isInLava());
        player.addProperty("in_water", p.isInWater());
        player.addProperty("fall_distance", p.fallDistance);
        player.addProperty("dimension", p.level().dimension().identifier().toString());
        if (p instanceof ServerPlayer sp) {
            player.addProperty("gamemode", sp.gameMode.getGameModeForPlayer().getName());
        } else {
            player.addProperty("gamemode", "unknown");
        }
        // 已穿装备：头/胸/腿/脚 + 主手/副手，是**带名字的槽位**（只加字段，不动上面既有字段语义）。
        // 需要它的原因：MLG 预判"会不会摔死"要先知道羽落（脚）与保护（四件）——物品 id 即注册名。
        // 注：inventory 是按容器槽位平铺的原始清单（装备槽也在其中，索引在物品栏之后），
        // 但那里看不出"哪一格是哪个部位"，所以这里再给一份按部位命名的。
        JsonObject equipment = new JsonObject();
        equipment.add("head", slotItem(p.getItemBySlot(EquipmentSlot.HEAD)));
        equipment.add("chest", slotItem(p.getItemBySlot(EquipmentSlot.CHEST)));
        equipment.add("legs", slotItem(p.getItemBySlot(EquipmentSlot.LEGS)));
        equipment.add("feet", slotItem(p.getItemBySlot(EquipmentSlot.FEET)));
        equipment.add("main_hand", slotItem(p.getItemBySlot(EquipmentSlot.MAINHAND)));
        equipment.add("off_hand", slotItem(p.getItemBySlot(EquipmentSlot.OFFHAND)));
        player.add("equipment", equipment);
        state.add("player", player);

        // World
        JsonObject world = new JsonObject();
        world.addProperty("time", mc.level.getOverworldClockTime());
        world.addProperty("is_day", mc.level.isBrightOutside());
        world.addProperty("is_raining", mc.level.isRaining());
        world.addProperty("light_level", mc.level.getMaxLocalRawBrightness(p.blockPosition()));
        world.addProperty("server", resolveServerLabel(mc));
        world.addProperty("world_name", resolveWorldName(mc));
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

        // 当前操作中的容器（无则空对象 {}）—— 后端据此知道槽位布局与让路情况
        state.add("open_container", com.lcu.lcumod.action.ContainerController.describeState(mc));

        // 在线玩家：仪表盘"在线玩家"面板 + "它在听谁"。以前这里只有 entities（前端没接上），
        // 所以面板永远"暂无玩家"。speaking = 最近发过话的人。
        JsonArray playersArr = new JsonArray();
        String speaker = com.lcu.lcumod.chat.ChatListener.lastSpeakerName();
        List<JsonObject> playerRows = new ArrayList<>();
        for (var other : mc.level.players()) {
            if (other == p) {
                continue;
            }
            JsonObject row = new JsonObject();
            row.addProperty("name", other.getName().getString());
            row.addProperty("x", Math.round(other.getX() * 10.0) / 10.0);
            row.addProperty("y", Math.round(other.getY() * 10.0) / 10.0);
            row.addProperty("z", Math.round(other.getZ() * 10.0) / 10.0);
            row.addProperty("distance", Math.round(p.distanceTo(other) * 10.0) / 10.0);
            row.addProperty("speaking", !speaker.isEmpty() && speaker.equals(other.getName().getString()));
            playerRows.add(row);
        }
        playerRows.sort(Comparator.comparingDouble(row -> row.get("distance").getAsDouble()));
        for (int i = 0; i < Math.min(8, playerRows.size()); i++) {
            playersArr.add(playerRows.get(i));
        }
        state.add("players", playersArr);
        state.addProperty("last_speaker", speaker);
        state.addProperty("hud_note", com.lcu.lcumod.client.HudNote.current());

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
                var stack = ((ItemEntity) entity).getItem();
                e.addProperty("item_id", BuiltInRegistries.ITEM.getKey(stack.getItem()).toString());
                e.addProperty("item_count", stack.getCount());
                e.addProperty("display", stack.getDisplayName().getString());
                // 谁丢的 + 掉多久了 + 还有多久消失：**判断该不该捡的是模型**（用户口径），
                // 我们只把事实摆出来。owner 为空 = 方块/发射器掉的（不是谁丢的）。
                ItemEntity item = (ItemEntity) entity;
                Entity owner = item.getOwner();
                e.addProperty("owner", owner == null ? "" : owner.getName().getString());
                e.addProperty("owner_type", owner == null ? ""
                        : (owner instanceof Player ? "player" : "other"));
                int age = item.getAge();
                e.addProperty("age", age);
                e.addProperty("despawn_in", Math.max(0, item.lifespan - age));
            } else {
                // 生物对待档位：**与本地反射 / 后端模型同一张表**（用户口径：高危险的不主动打、
                // 中立的不惹、只有该处理的才处理）。认不出 ⇒ UNKNOWN，**不默认 hostile**；
                // 后端优先信任这个字段，认不出/旧客户端才回落到自己的关键词兜底。
                MobPolicy.Tier tier = MobPolicy.tier(AttackRules.entityTypeId(entity));
                e.addProperty("tier", tier.name());
                // 旧字段 `type` 保留（老后端/旧工具还在读），按档位映射回"敌对/动物/其他"。
                e.addProperty("type", legacyType(tier));
            }

            // Health info for living entities
            if (entity instanceof LivingEntity le) {
                e.addProperty("health", le.getHealth());
                e.addProperty("max_health", le.getMaxHealth());
            }

            entities.add(e);
        }
        state.add("entities", entities);

        // Nearby blocks/resources — compact perception map for planning
        state.add("nearby_blocks", collectNearbyBlocks(mc, p, p.blockPosition()));

        JsonArray nearbyWorkstations = new JsonArray();
        for (JsonObject item : PoiMemory.snapshot(mc, "workstation", PoiMemory.INTERACTION_RADIUS, 16)) {
            nearbyWorkstations.add(item);
        }
        state.add("nearby_workstations", nearbyWorkstations);

        JsonArray nearbyStorage = new JsonArray();
        for (JsonObject item : PoiMemory.snapshotWithContents(mc, "storage", PoiMemory.INTERACTION_RADIUS, 16)) {
            nearbyStorage.add(item);
        }
        state.add("nearby_storage", nearbyStorage);

        // ── 夜晚/睡觉（LLM 与"夜晚自动睡觉"规则都要用）──
        // 26.3 没有 getDayTime：用天空暗度（含天色+天气）判定夜/雷雨，等价原版可睡条件。
        int skyDarken = mc.level.getSkyDarken();
        boolean night = skyDarken >= 4 || mc.level.isThundering();
        state.addProperty("sky_darken", skyDarken);
        state.addProperty("day_time", (int) (mc.level.getGameTime() % 24000L)); // 近似值，勿据此判断
        state.addProperty("is_night", night);
        state.addProperty("can_sleep", night && !mc.player.isSleeping());

        // 生物群系（用户要求：AI 要知道 location/群系参数，才能带玩家去找某群系/结构）
        try {
            state.addProperty("biome", mc.level.getBiome(mc.player.blockPosition())
                    .unwrapKey().map(key -> key.identifier().toString()).orElse(""));
        } catch (Exception e) {
            state.addProperty("biome", "");
        }

        // 附近的床：**缓存扫描**（每 40 tick 一次），否则每次 state_update 都扫 27 万个方块
        if (bedScanCooldown-- <= 0) {
            bedScanCooldown = 40;
            JsonArray beds = new JsonArray();
            for (BlockFinder.Hit hit : BlockFinder.scan(mc, "*_bed", 24, 4)) {
                JsonObject bed = new JsonObject();
                bed.addProperty("x", hit.x());
                bed.addProperty("y", hit.y());
                bed.addProperty("z", hit.z());
                bed.addProperty("id", hit.id());
                bed.addProperty("distance", Math.round(hit.distance() * 10.0) / 10.0);
                beds.add(bed);
            }
            cachedBeds = beds;
            // 看到床就**主动**记一次（用户口径："附近有床应该主动去保存坐标点，不用人类说；
            // 床的位置默认作为 home；一般主动保存 1 次即可"）。
            if (!beds.isEmpty()) {
                JsonObject nearest = beds.get(0).getAsJsonObject();
                PoiMemory.rememberBed(
                        new net.minecraft.core.BlockPos(nearest.get("x").getAsInt(),
                                nearest.get("y").getAsInt(), nearest.get("z").getAsInt()),
                        nearest.get("id").getAsString(), mc.player.tickCount);
            }
        }
        state.add("nearby_beds", cachedBeds);

        // 死亡点（最新在前，最多 3 条）：**只报事实**（死在哪、什么时候），
        // 要不要回去/去捡装备由模型决定（后端有 go_to_death_point 工具）。
        state.add("death_points", com.lcu.lcumod.action.DeathPoint.snapshot());

        // 主动记录过的记忆点（持久，可能很远）
        JsonArray remembered = new JsonArray();
        for (JsonObject item : PoiMemory.snapshotRemembered(mc, 16)) {
            remembered.add(item);
        }
        state.add("remembered_pois", remembered);

        // 站点 / 路径记忆与驻守状态（用户："记住精准 xyz 去挂机"、"记录路径"）
        // afk: {station, since_ticks, at_target, drift}；route: {name, index, total, active}
        state.add("afk", com.lcu.lcumod.action.ActionExecutor.describeAfk());
        state.add("route", com.lcu.lcumod.action.ActionExecutor.describeRoute());
        state.add("stations", com.lcu.lcumod.action.ActionExecutor.describeStations());
        state.add("routes", com.lcu.lcumod.action.ActionExecutor.describeRoutes());
        // 区域声明（语义 + 保护项）：后端据此**事前**就知道"这里是农场，别挖/别放/别打动物"
        state.add("zones", com.lcu.lcumod.action.ActionExecutor.describeZones());

        LCUMod.WIRE.sendEvent("state_update", state);
    }

    /**
     * 旧字段 `type` 的兼容映射：{@link MobPolicy.Tier} → `hostile` / `animal` / `mob`。
     *
     * <p>为什么不直接删掉旧字段：后端别处（`entities.py` 的 kind 归一、prompt 渲染"附近实体"）
     * 与旧版客户端都在读 `type`。**新字段 `tier` 才是权威**，后端优先信任它；这里只是让老读法
     * 不至于把中立怪（末影人/狼/铁傀儡）继续当"hostile"。
     */
    private static String legacyType(MobPolicy.Tier tier) {
        return switch (tier) {
            case DANGEROUS, HOSTILE -> "hostile";
            case PASSIVE -> "animal";
            case NEUTRAL, UNKNOWN -> "mob";
        };
    }
}
