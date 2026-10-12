package com.lcu.lcumod.state;

import com.google.gson.JsonArray;
import com.google.gson.JsonElement;
import com.google.gson.JsonNull;
import com.google.gson.JsonObject;
import com.lcu.lcumod.LCUMod;
import com.lcu.lcumod.action.AttackRules;
import com.lcu.lcumod.action.BlockFinder;
import com.lcu.lcumod.action.GearReport;
import com.lcu.lcumod.action.MobPolicy;
import com.lcu.lcumod.action.PerceptionView;
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
    // 缓存的是**聚合摘要**（计数 + by_block + nearest），不是逐方块坐标。
    private static JsonObject cachedNearbyBlocks = null;
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
     * <p>返回的是**聚合摘要**（{@code count} + {@code by_block} + {@code nearest}，见
     * {@link PerceptionView.Pure#summarizeBlocks}）—— **不给逐方块坐标**（用户口径：默认把无关的
     * 周边细节屏蔽掉）。要知道某一种方块具体在哪，走按需查询（后端 {@code find_block}）。
     */
    private static JsonObject collectNearbyBlocks(Minecraft mc, Player p, BlockPos origin) {
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
            return PerceptionView.blocks(List.of());
        }
        if (cachedNearbyBlocks != null
                && tickCounter - cachedNearbyBlocksTick < NEARBY_BLOCKS_TTL_TICKS
                && cachedNearbyBlocksOrigin != null
                && origin.distSqr(cachedNearbyBlocksOrigin) < 1.0) {
            return cachedNearbyBlocks;
        }

        List<PerceptionView.Pure.BlockRow> nearbyBlocks = new ArrayList<>();
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

                    String blockId = BuiltInRegistries.BLOCK.getKey(blockState.getBlock()).toString();
                    nearbyBlocks.add(new PerceptionView.Pure.BlockRow(blockId, distance));
                }
            }
        }
        cachedNearbyBlocks = PerceptionView.blocks(nearbyBlocks);
        cachedNearbyBlocksOrigin = origin.immutable();
        cachedNearbyBlocksTick = tickCounter;
        return cachedNearbyBlocks;
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
    /** 六槽装备指纹（id/数量/已损）：换装备、掉耐久都要立刻上报（OBS 的装备/耐久面板靠它）。 */
    private static String lastGearKey = "";

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
        // 第一次 tick 时补一次 wire token 武装：此刻配置必然已加载（启动阶段那次读可能太早）。
        // 放在这里是因为它是"早于任何后端连接、且不依赖去猜 FML 配置事件类名"的钩子。
        LCUMod.armWireTokenIfNeeded();
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

        // 原版贴图（装备槽 + 主/副手）：**只在装备集合变了**时发一次（内部有已发集合，集合不变
        // 一个字节都不发）。放在节流门之前：贴图跟屏幕分辨率一样是"换装备才变"的东西，
        // 没必要等下一次 state_update。读不到的 id 直接跳过，web 退回文字显示。
        GearReport.publishIcons(mc, p);

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

        // 装备变化（换/脱、数量、耐久掉了）必须立刻上报：OBS 的装备与耐久面板读的就是 gear
        {
            String gearKeyNow = GearReport.slotKey(p);
            if (!gearKeyNow.equals(lastGearKey)) {
                hasChanged = true;
                lastGearKey = gearKeyNow;
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

        // 装备与耐久（OBS 叠加页要的"自己的装备 + 耐久 + 主要攻击类武器的耐久"）：
        // 四槽**固定顺序**且空槽也给零值对象（不让 web 猜）；不可损坏物如实 damageable:false；
        // 三元组自洽 durability = max_durability - damage。贴图走 item_icons 事件（不在每帧状态里）。
        state.add("gear", GearReport.build(mc));

        // 家的那张床 + "绑上了没"（**事实字段，放在 gear 旁边**）：
        //   home_bed       = 我们记着的那张床（PoiMemory 里带 `bed` 标签的点中最远…最近的一个）；
        //                    没记过 ⇒ null（不许编）。
        //   home_bed_bound = 原版当前重生点（getRespawnData().pos()）**==** home_bed ⇒ true；
        //                    没记过床 / 拿不到重生点 ⇒ false（不许编）。
        //
        // **语义边界（别读错）**：原版右键点一次床就绑定重生点（任何时候、白天晴天也一样）；"现在不能睡"
        // 只是不睡、**不影响绑定**。所以 home_bed_bound=false **只代表"没绑上"**，**不代表床没了** ——
        // 床没了的判据是"那个位置已经不是床方块"，见 PoiMemory.invalidateBedIfGone（它会把 bed 标签摘掉，
        // 那个点自然也就不再算作 home_bed）。
        {
            int bedX = 0, bedY = 0, bedZ = 0;
            boolean haveBed = false;
            for (JsonObject poi : PoiMemory.snapshotRemembered(mc, Integer.MAX_VALUE)) {
                if (hasTag(poi, "bed")) {
                    // snapshotRemembered 按距离排序 ⇒ 第一个带 bed 标签的就是最近的那张床。
                    bedX = poi.get("x").getAsInt();
                    bedY = poi.get("y").getAsInt();
                    bedZ = poi.get("z").getAsInt();
                    haveBed = true;
                    break;
                }
            }
            int resX = 0, resY = 0, resZ = 0;
            boolean haveRespawn = false;
            try {
                var respawnData = mc.level.getRespawnData();
                if (respawnData != null && respawnData.pos() != null) {
                    resX = respawnData.pos().getX();
                    resY = respawnData.pos().getY();
                    resZ = respawnData.pos().getZ();
                    haveRespawn = true;
                }
            } catch (Exception ignored) {
                // 拿不到重生点 ⇒ 如实报"没绑上"，不编。
            }
            PerceptionView.Pure.HomeBed homeBed = PerceptionView.Pure.homeBed(
                    haveBed, bedX, bedY, bedZ, haveRespawn, resX, resY, resZ);
            if (homeBed.haveBed()) {
                JsonObject bed = new JsonObject();
                bed.addProperty("x", homeBed.x());
                bed.addProperty("y", homeBed.y());
                bed.addProperty("z", homeBed.z());
                state.add("home_bed", bed);
            } else {
                state.add("home_bed", JsonNull.INSTANCE);
            }
            state.addProperty("home_bed_bound", PerceptionView.homeBedBound(homeBed));
        }

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

        // ── Nearby entities：**分级摘要**（不是逐条带坐标的清单）────────────────────
        // 用户口径：默认把无关的周边实体屏蔽掉，只有"要处理的/人"才给坐标 —— 村庄/繁殖基地附近
        // 大量生物移动不会再把 prompt 刷屏。规则见 PerceptionView.Pure#summarizeEntities：
        //   count + by_kind（七档自洽）+ nearest（不带坐标）+ threats（危险/敌对且 ≤16 格，最多 3 条，
        //   按距离升序，**才带坐标**）+ players（**总是带坐标**，≤8）+ items（聚合 + 最近一件，不带坐标）。
        // 要某一只的具体坐标/血量时走**按需**路径（后端 find_entity / where_is），这里删坐标不丢能力。
        List<PerceptionView.Pure.Row> entityRows = new ArrayList<>();
        for (var other : mc.level.players()) {
            if (other == p) continue;
            entityRows.add(new PerceptionView.Pure.Row(
                    other.getName().getString(), "", true, false,
                    other.getX(), other.getY(), other.getZ(), p.distanceTo(other),
                    true, other.getHealth(),
                    "", 0, "", 0, 0));
        }

        // Scan for nearby mobs and items within 16 blocks
        var searchBox = p.getBoundingBox().inflate(16);
        for (var entity : mc.level.getEntities(p, searchBox)) {
            if (entity == p) continue;
            // Skip other players (already handled above)
            if (entity instanceof Player) continue;

            double distance = p.distanceTo(entity);
            if (entity instanceof ItemEntity item) {
                var stack = item.getItem();
                // 谁丢的 + 掉多久了 + 还有多久消失：**判断该不该捡的是模型**（用户口径），
                // 我们只把事实摆出来。owner 为空 = 方块/发射器掉的（不是谁丢的）。
                Entity owner = item.getOwner();
                int age = item.getAge();
                entityRows.add(new PerceptionView.Pure.Row(
                        entity.getName().getString(), "", false, true,
                        entity.getX(), entity.getY(), entity.getZ(), distance,
                        false, 0.0,
                        BuiltInRegistries.ITEM.getKey(stack.getItem()).toString(), stack.getCount(),
                        owner == null ? "" : owner.getName().getString(), age,
                        Math.max(0, item.lifespan - age)));
            } else {
                // 生物对待档位：**与本地反射 / 后端模型同一张表**（用户口径：高危险的不主动打、
                // 中立的不惹、只有该处理的才处理）。认不出 ⇒ UNKNOWN，**不默认 hostile**。
                MobPolicy.Tier tier = MobPolicy.tier(AttackRules.entityTypeId(entity));
                boolean living = entity instanceof LivingEntity;
                entityRows.add(new PerceptionView.Pure.Row(
                        entity.getName().getString(), tier.name(), false, false,
                        entity.getX(), entity.getY(), entity.getZ(), distance,
                        living, living ? ((LivingEntity) entity).getHealth() : 0.0,
                        "", 0, "", 0, 0));
            }
        }
        state.add("entities", PerceptionView.entities(entityRows));

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
     * 判断一条 {@link PoiMemory} 记忆点是否带某个标签（`home_bed` 取"带 bed 标签"的那张床）。
     */
    private static boolean hasTag(JsonObject poi, String tag) {
        JsonElement tags = poi.get("tags");
        if (tags == null || !tags.isJsonArray()) {
            return false;
        }
        for (JsonElement t : tags.getAsJsonArray()) {
            if (tag.equals(t.getAsString())) {
                return true;
            }
        }
        return false;
    }
}
