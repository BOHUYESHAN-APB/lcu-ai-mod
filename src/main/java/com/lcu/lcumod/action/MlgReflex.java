package com.lcu.lcumod.action;

import com.google.gson.JsonObject;
import com.lcu.lcumod.LCUMod;
import net.minecraft.client.Minecraft;
import net.minecraft.core.BlockPos;
import net.minecraft.resources.ResourceKey;
import net.minecraft.world.effect.MobEffectInstance;
import net.minecraft.world.effect.MobEffects;
import net.minecraft.world.entity.EquipmentSlot;
import net.minecraft.world.entity.ai.attributes.Attributes;
import net.minecraft.world.item.Item;
import net.minecraft.world.item.ItemStack;
import net.minecraft.world.item.Items;
import net.minecraft.world.item.enchantment.Enchantment;
import net.minecraft.world.item.enchantment.Enchantments;
import net.minecraft.world.level.block.Blocks;
import net.minecraft.world.level.block.state.BlockState;
import net.minecraft.world.phys.Vec3;

import java.util.Locale;

/**
 * P0 落地水（MLG / water bucket）反射 —— 用户点名的「高阶控制：落地水」。
 *
 * <p>用户口径：「高阶控制：落地水/MLG（下落中右键放水再收回；需 tick 级时序 + 预判落地）」。
 * 与岩浆/溺水同为 **P0 生存反射**（毫秒级闭环、可被 {@code reflex_override} 覆盖），
 * 但它是本批里**唯一会移动鼠标视角（看向正下方）**的反射 —— 因为放水必须看着地面。
 *
 * <h2>时序（tick 级）</h2>
 * <ol>
 *   <li>{@link FallPlan#decide} 判定"真会摔死"且进入反应窗口 → <b>armed</b>：选水桶到手上；</li>
 *   <li>进入放水窗口（落地前 ≤ N tick）→ 看正下方 + 右键放水 → <b>placed</b>；</li>
 *   <li>落地（入水/站稳）→ 手持空桶右键把水收回来 → <b>picked_up</b>。</li>
 * </ol>
 *
 * <h2>让路（立即放弃，绝不动作）</h2>
 * 玩家接管 / {@code reflex_override}（或 {@code all}）/ 不在世界 / MLG 被关掉 /
 * 已有安全缓冲（水 / 垫子 / 梯子 / 蛛网 / 区块未加载）→ 立即 {@code abort}。
 *
 * <h2>保守优先（不抢未来的活）</h2>
 * 只在 {@link FallPlan} 判定"**减免后**的预计落地伤害 ≥ 生命值"（真会摔死）时才动；减免来自
 * {@link FallPlan.Mitigation}（羽落/保护附魔 + 抗性 + 缓降 + 安全落距属性，见 {@link #readMitigation}），
 * 读不到物品/附魔信息时按裸伤害判（假阳性无害，假阴性会死人）。离地还远时不动手，
 * 等进入反应窗口再判。没水桶 / 来不及 → **如实上报** {@code unavailable}/{@code too_late}，绝不编造动作。
 *
 * <p>调用时机：{@code ActionExecutor.onTick} 中放在其它反射之后，保证放水时它是视角的最后写入者。
 */
public final class MlgReflex {

    /** ReflexGate 名字：后端 {@code reflex_override {name:"mlg"}} 可临时关掉本反射。 */
    public static final String GATE = "mlg";

    // ── 可验证回执（稳定字符串，供 state 上报与自检） ──
    public static final String RECEIPT_IDLE = "idle";
    public static final String RECEIPT_ARMED = "armed";
    public static final String RECEIPT_PLACING = "placing";
    public static final String RECEIPT_PLACED = "placed";
    public static final String RECEIPT_PICKED_UP = "picked_up";
    public static final String RECEIPT_TOO_LATE = "too_late";
    public static final String RECEIPT_NO_BUCKET = "unavailable:no_bucket";
    public static final String ABORT_PREFIX = "aborted:";

    /** 反射阶段（state 里的 {@code reflex_mlg.phase}）。 */
    public enum Phase { IDLE, ARMING, PLACING, PLACED, RECOVERING }

    /** 切槽生效等待（tick），与 {@link EatController#SELECT_TICKS} 一致。 */
    private static final int SELECT_TICKS = 2;
    /** 放水/收水动作的右键按住 tick 数。 */
    private static final int USE_HOLD_TICKS = 2;
    /** 收水右键按住 tick 数（略长，确保 scoop 生效）。 */
    private static final int PICKUP_HOLD_TICKS = 3;
    /** 放下水后确认水位出现的最长等待（tick）。 */
    private static final int PLACE_VERIFY_TICKS = 6;
    /** 放水的最大尝试次数（放空一次再补一次）。 */
    private static final int MAX_PLACE_ATTEMPTS = 2;
    /** 落地后收水的最大尝试次数。 */
    private static final int MAX_SCOOP_ATTEMPTS = 3;
    /** 等待落地的最长 tick 数（水位没接住也不久挂）。 */
    private static final int PLACED_WAIT_TICKS = 20;
    /** 向下扫描地面的最大深度（格）。 */
    private static final int GROUND_SCAN_DEPTH = 96;

    /** 四个盔甲位（头/胸/腿/脚）：羽落只看脚，保护四件合计。 */
    private static final EquipmentSlot[] ARMOR_SLOTS = {
            EquipmentSlot.HEAD, EquipmentSlot.CHEST, EquipmentSlot.LEGS, EquipmentSlot.FEET
    };

    private static volatile boolean enabled = true;

    private static Phase phase = Phase.IDLE;
    private static boolean armed = false;
    private static String reason = "";
    private static String receipt = RECEIPT_IDLE;
    private static int phaseTicks = 0;
    private static int bucketSlot = -1;
    private static int emptySlot = -1;
    private static HoldController.Handle useHold = null;
    private static int placeAttempts = 0;
    private static int scoopAttempts = 0;
    private static BlockPos groundPos = null;
    private static BlockPos waterPos = null;
    private static boolean placedWater = false;
    private static float savedYaw = 0f;
    private static float savedPitch = 0f;
    private static boolean lookSaved = false;
    /** 最近一次读到的摔落减免（state 上报用；null = 还没读过）。 */
    private static FallPlan.Mitigation lastMitigation = null;

    private MlgReflex() {
    }

    // ── 开关 ──

    public static void setEnabled(boolean value) {
        enabled = value;
        if (!value) {
            abort("disabled");
        }
    }

    public static boolean isEnabled() {
        return enabled;
    }

    // ── 状态上报 ──

    /** state 里的 {@code reflex_mlg}：命名与 {@code reflex_combat}/{@code reflex_swim}/{@code reflex_eat} 一致。 */
    public static JsonObject state() {
        JsonObject o = new JsonObject();
        o.addProperty("enabled", enabled);
        o.addProperty("armed", armed);
        o.addProperty("phase", phase.name().toLowerCase(Locale.ROOT));
        o.addProperty("reason", reason);
        o.addProperty("receipt", receipt);
        // 判"会不会摔死"时用到的减免（后端据此理解为什么没放水）
        FallPlan.Mitigation m = lastMitigation;
        if (m != null) {
            o.addProperty("safe_fall_distance", round(m.safeFallDistance()));
            o.addProperty("fall_damage_multiplier", round(m.fallDamageMultiplier()));
            o.addProperty("feather_falling_level", m.featherFallingLevel());
            o.addProperty("protection_level_sum", m.protectionLevelSum());
            o.addProperty("epf", FallPlan.totalEpf(m.featherFallingLevel(), m.protectionLevelSum()));
            o.addProperty("resistance_amplifier", m.resistanceAmplifier());
            o.addProperty("slow_falling", m.slowFalling());
        }
        return o;
    }

    public static boolean isArmed() {
        return armed;
    }

    public static boolean isActive() {
        return phase != Phase.IDLE;
    }

    public static String receipt() {
        return receipt;
    }

    // ── 每 tick ──

    public static void tick(Minecraft mc) {
        // ── 让路闸门：任何时候都优先判 ──
        if (!enabled) {
            if (active()) abort("disabled");
            return;
        }
        if (mc == null || mc.player == null || mc.level == null) {
            if (active()) abort("not_in_world");
            return;
        }
        if (!InputIsolation.isAiControlled()
                || (mc.mouseHandler != null && mc.mouseHandler.isMouseGrabbed())) {
            if (active()) abort("player_takeover");
            return;
        }
        if (ReflexGate.isSuppressed(GATE)) {
            if (active()) abort("reflex_override");
            return;
        }

        Scan scan = scan(mc);
        FallPlan.Input in = buildInput(mc, scan);
        FallPlan.Decision d = FallPlan.decide(in);

        if (phase == Phase.IDLE) {
            switch (d) {
                case ARM, PLACE_WATER -> {
                    groundPos = scan.groundPos;
                    waterPos = groundPos == null ? null : groundPos.above();
                    beginArming(mc, in);
                }
                case NO_BUCKET -> {
                    armed = false;
                    reason = "no_bucket";
                    receipt = RECEIPT_NO_BUCKET;
                }
                case TOO_LATE -> {
                    armed = false;
                    reason = "too_late";
                    receipt = RECEIPT_TOO_LATE;
                }
                default -> { /* NONE：什么都不做（保守） */ }
            }
            return;
        }

        switch (phase) {
            case ARMING -> {
                if (d == FallPlan.Decision.NONE) {
                    abort(scan.buffer ? "buffer" : "not_dangerous");
                    return;
                }
                if (d == FallPlan.Decision.NO_BUCKET) {
                    abort("no_bucket");
                    return;
                }
                if (d == FallPlan.Decision.TOO_LATE) {
                    abort("too_late");
                    return;
                }
                // 还在窗口前：更新预判落点，等放水窗口
                groundPos = scan.groundPos;
                waterPos = groundPos == null ? null : groundPos.above();
                tickArming(mc, d);
            }
            case PLACING -> tickPlacing(mc);
            case PLACED -> tickPlaced(mc);
            case RECOVERING -> tickRecovering(mc);
            default -> abort("bad_phase");
        }
    }

    private static boolean active() {
        return phase != Phase.IDLE || armed;
    }

    // ── 状态机 ──

    private static void beginArming(Minecraft mc, FallPlan.Input in) {
        if (!selectItem(mc, Items.WATER_BUCKET)) {
            armed = false;
            phase = Phase.IDLE;
            reason = "no_bucket";
            receipt = RECEIPT_NO_BUCKET;
            LCUMod.LOGGER.warn("[Reflex] MLG 会摔死但拿不到水桶（如实上报，不编造动作）");
            return;
        }
        bucketSlot = mc.player.getInventory().getSelectedSlot();
        savedYaw = mc.player.getYRot();
        savedPitch = mc.player.getXRot();
        lookSaved = true;
        phase = Phase.ARMING;
        phaseTicks = 0;
        placeAttempts = 0;
        scoopAttempts = 0;
        placedWater = false;
        armed = true;
        reason = "fatal_fall";
        receipt = RECEIPT_ARMED;
        LCUMod.LOGGER.info("[Reflex] MLG armed: y={} vy={} drop={} damage={} ticksToLand={} slot={}",
                round(in.y()), round(in.velY()),
                round(FallPlan.predictedDrop(in.y(), in.groundY(), in.fallDistanceSoFar())),
                round(FallPlan.mitigatedFallDamage(
                        FallPlan.predictedDrop(in.y(), in.groundY(), in.fallDistanceSoFar()), in.mitigation())),
                FallPlan.ticksToLand(in.y(), in.velY(), in.groundY()), bucketSlot);
    }

    private static void tickArming(Minecraft mc, FallPlan.Decision d) {
        phaseTicks++;
        if (phaseTicks <= SELECT_TICKS) {
            return; // 等切槽/交换生效
        }
        if (!isHolding(mc, Items.WATER_BUCKET)) {
            if (phaseTicks <= SELECT_TICKS + 2) {
                return; // 生效宽限
            }
            abort("no_bucket");
            return;
        }
        if (d == FallPlan.Decision.PLACE_WATER) {
            phase = Phase.PLACING;
            phaseTicks = 0;
            placeAttempts = 0;
            reason = "place_window";
            lookDown(mc);
        }
    }

    private static void tickPlacing(Minecraft mc) {
        phaseTicks++;
        lookDown(mc);
        if (!isHolding(mc, Items.WATER_BUCKET)) {
            abort("lost_bucket");
            return;
        }
        if (useHold == null || !useHold.isActive()) {
            if (placeAttempts >= MAX_PLACE_ATTEMPTS) {
                abort("place_failed");
                return;
            }
            placeAttempts++;
            useHold = ControlStream.hold("use", USE_HOLD_TICKS);
            reason = "placing_water";
            receipt = RECEIPT_PLACING;
        }
        if (waterNearby(mc)) {
            placedWater = true;
            phase = Phase.PLACED;
            phaseTicks = 0;
            reason = "water_placed";
            receipt = RECEIPT_PLACED;
            LCUMod.LOGGER.info("[Reflex] MLG 水位已出现，等待落地");
            return;
        }
        if (phaseTicks > PLACE_VERIFY_TICKS) {
            abort("place_failed");
        }
    }

    private static void tickPlaced(Minecraft mc) {
        phaseTicks++;
        if (mc.player.onGround() || mc.player.isInWater()) {
            if (selectItem(mc, Items.BUCKET)) {
                emptySlot = mc.player.getInventory().getSelectedSlot();
                phase = Phase.RECOVERING;
                phaseTicks = 0;
                scoopAttempts = 0;
                reason = "landed_scoop";
                return;
            }
            finish(RECEIPT_PLACED, "scoop_unavailable");
            return;
        }
        if (phaseTicks > PLACED_WAIT_TICKS) {
            finish(RECEIPT_PLACED, "landed_timeout");
        }
    }

    private static void tickRecovering(Minecraft mc) {
        phaseTicks++;
        lookDown(mc);
        if (useHold == null || !useHold.isActive()) {
            if (scoopAttempts >= MAX_SCOOP_ATTEMPTS) {
                finish(RECEIPT_PLACED, "scoop_timeout");
                return;
            }
            scoopAttempts++;
            useHold = ControlStream.hold("use", PICKUP_HOLD_TICKS);
            reason = "scooping_water";
        }
        if (isHolding(mc, Items.WATER_BUCKET)) {
            finish(RECEIPT_PICKED_UP, "water_recovered");
            return;
        }
        if (!waterNearby(mc)) {
            finish(RECEIPT_PLACED, "water_gone");
        }
    }

    // ── 收尾 ──

    private static void finish(String receiptCode, String why) {
        if (useHold != null) {
            ControlStream.releaseHold("use");
            useHold = null;
        }
        restoreLook();
        receipt = receiptCode;
        reason = why;
        clearState();
        LCUMod.LOGGER.info("[Reflex] MLG done: {} ({})", receiptCode, why);
    }

    private static void abort(String why) {
        if (useHold != null) {
            ControlStream.releaseHold("use");
            useHold = null;
        }
        // 玩家接管时**不**回写视角（那是玩家自己的朝向）；其余让路回写，避免一直低头。
        if (!"player_takeover".equals(why)) {
            restoreLook();
        } else {
            lookSaved = false;
        }
        boolean wasActive = active();
        reason = why;
        if (wasActive) {
            receipt = ABORT_PREFIX + why;
        }
        clearState();
        LCUMod.LOGGER.info("[Reflex] MLG abort: {}", why);
    }

    private static void clearState() {
        phase = Phase.IDLE;
        armed = false;
        phaseTicks = 0;
        bucketSlot = -1;
        emptySlot = -1;
        groundPos = null;
        waterPos = null;
        placedWater = false;
        placeAttempts = 0;
        scoopAttempts = 0;
    }

    private static void restoreLook() {
        if (!lookSaved) {
            return;
        }
        lookSaved = false;
        Minecraft mc = Minecraft.getInstance();
        if (mc != null && mc.player != null && InputIsolation.isAiControlled()) {
            mc.player.setYRot(savedYaw);
            mc.player.setXRot(savedPitch);
        }
    }

    private static void lookDown(Minecraft mc) {
        if (mc.player != null) {
            mc.player.setXRot(90.0f);
        }
    }

    // ── 世界扫描 / 物品 ──

    private static final class Scan {
        double groundY = Double.NaN;
        BlockPos groundPos = null;
        boolean buffer = false;
        boolean unloaded = false;
    }

    /** 从脚底向下找第一块可站立的方块，同时把"安全缓冲"测出来（水/垫子/梯子/蛛网/未加载）。 */
    private static Scan scan(Minecraft mc) {
        Scan s = new Scan();
        var player = mc.player;
        if (player.onGround() || player.isInWater() || player.onClimbable() || player.isFallFlying()) {
            s.buffer = true;
            return s;
        }
        var level = mc.level;
        BlockPos.MutableBlockPos p = new BlockPos.MutableBlockPos();
        int minY = level.getMinY();
        int startY = (int) Math.floor(player.getY()) - 1;
        for (int y = startY; y >= minY && (startY - y) <= GROUND_SCAN_DEPTH; y--) {
            p.set(player.getBlockX(), y, player.getBlockZ());
            if (!level.isLoaded(p)) {
                s.buffer = true;
                s.unloaded = true;
                return s;
            }
            BlockState st = level.getBlockState(p);
            if (isBufferBlock(st)) {
                s.buffer = true;
                return s;
            }
            if (!st.getCollisionShape(level, p).isEmpty()) {
                s.groundPos = p.immutable();
                s.groundY = y + 1.0;
                return s;
            }
        }
        return s; // 虚空 / 超出扫描深度：groundY 保持 NaN → FallPlan 保守不动
    }

    private static boolean isBufferBlock(BlockState st) {
        if (!st.getFluidState().isEmpty()) {
            return true; // 水/岩浆都会免掉摔落伤害（落进去即可）→ 无需 MLG
        }
        return st.is(Blocks.COBWEB)
                || st.is(Blocks.HAY_BLOCK)
                || st.is(Blocks.SLIME_BLOCK)
                || st.is(Blocks.HONEY_BLOCK)
                || st.is(Blocks.POWDER_SNOW)
                || st.is(Blocks.SWEET_BERRY_BUSH)
                || st.is(Blocks.LADDER)
                || st.is(Blocks.SCAFFOLDING);
    }

    private static FallPlan.Input buildInput(Minecraft mc, Scan s) {
        var player = mc.player;
        Vec3 v = player.getDeltaMovement();
        double horizontal = Math.hypot(v.x, v.z);
        FallPlan.Mitigation mitigation = readMitigation(mc);
        lastMitigation = mitigation;
        return new FallPlan.Input(
                player.getY(), v.y, horizontal,
                s.groundY, player.fallDistance, player.getHealth(),
                hasItem(mc, Items.WATER_BUCKET), s.buffer, enabled, 20,
                mitigation);
    }

    /**
     * 读当前摔落减免（{@link FallPlan.Mitigation}）：安全落距/倍率属性 + 四件盔甲附魔 + 药水效果。
     *
     * <p>零猜测、全部走原版 API：属性用 {@code getAttributeValue}（已含跳跃提升等修饰符），
     * 附魔用物品的 {@code DataComponents.ENCHANTMENTS}（与 {@code EnchantmentHelper.getItemEnchantmentLevel} 同源）。
     */
    private static FallPlan.Mitigation readMitigation(Minecraft mc) {
        var player = mc.player;
        int featherFalling = FallPlan.clampFeatherFallingLevel(
                enchantLevel(player.getItemBySlot(EquipmentSlot.FEET), Enchantments.FEATHER_FALLING));
        int protection = 0;
        for (EquipmentSlot slot : ARMOR_SLOTS) {
            protection += FallPlan.clampProtectionLevel(
                    enchantLevel(player.getItemBySlot(slot), Enchantments.PROTECTION));
        }
        MobEffectInstance resistance = player.getEffect(MobEffects.RESISTANCE);
        return new FallPlan.Mitigation(
                true,
                player.getAttributeValue(Attributes.SAFE_FALL_DISTANCE),
                player.getAttributeValue(Attributes.FALL_DAMAGE_MULTIPLIER),
                featherFalling,
                protection,
                resistance == null ? -1 : resistance.getAmplifier(),
                player.hasEffect(MobEffects.SLOW_FALLING));
    }

    /**
     * 物品上的附魔等级；没有 → 0。
     *
     * <p>按注册名（{@code ResourceKey}）匹配，不依赖 Holder 是否绑定注册表 → 不会抛异常。
     */
    private static int enchantLevel(ItemStack stack, ResourceKey<Enchantment> enchantment) {
        if (stack == null || stack.isEmpty()) {
            return 0;
        }
        for (var entry : stack.getEnchantments().entrySet()) {
            if (entry.getKey().unwrapKey().filter(enchantment::equals).isPresent()) {
                return entry.getIntValue();
            }
        }
        return 0;
    }

    private static boolean hasItem(Minecraft mc, Item item) {
        return findItem(mc, item) >= 0;
    }

    private static int findItem(Minecraft mc, Item item) {
        var inv = mc.player.getInventory();
        for (int i = 0; i < inv.getContainerSize(); i++) {
            ItemStack st = inv.getItem(i);
            if (!st.isEmpty() && st.getItem() == item) {
                return i;
            }
        }
        return -1;
    }

    private static boolean isHolding(Minecraft mc, Item item) {
        return mc.player.getItemInHand(net.minecraft.world.InteractionHand.MAIN_HAND).getItem() == item;
    }

    /** 把指定物品弄到手上（热栏直接选，主背包走窗口 SWAP），成功返回 true。 */
    private static boolean selectItem(Minecraft mc, Item item) {
        int slot = findItem(mc, item);
        if (slot < 0) {
            return false;
        }
        var inv = mc.player.getInventory();
        if (slot < 9) {
            inv.setSelectedSlot(slot);
            return true;
        }
        // 主背包 → 与当前手持格交换（复用 ActionExecutor 的窗口 SWAP，不打开界面）
        return ActionExecutor.swapIntoHotbar(mc, slot);
    }

    /** 脚下/附近是否已有水（放置成功 / 收水是否完成）。 */
    private static boolean waterNearby(Minecraft mc) {
        if (mc.level == null || mc.player == null) {
            return false;
        }
        BlockPos base = waterPos != null ? waterPos : mc.player.blockPosition();
        BlockPos.MutableBlockPos p = new BlockPos.MutableBlockPos();
        for (int dx = -1; dx <= 1; dx++) {
            for (int dy = -1; dy <= 1; dy++) {
                for (int dz = -1; dz <= 1; dz++) {
                    p.set(base.getX() + dx, base.getY() + dy, base.getZ() + dz);
                    if (mc.level.getBlockState(p).is(Blocks.WATER)) {
                        return true;
                    }
                }
            }
        }
        return false;
    }

    private static double round(double v) {
        return Math.round(v * 100.0) / 100.0;
    }
}
