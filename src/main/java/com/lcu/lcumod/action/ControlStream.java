package com.lcu.lcumod.action;

import com.google.gson.JsonObject;
import com.lcu.lcumod.LCUMod;
import net.minecraft.client.Minecraft;
import net.minecraft.world.InteractionHand;
import net.minecraft.world.item.component.SwingAnimation;
import net.minecraft.world.phys.BlockHitResult;
import net.minecraft.world.phys.EntityHitResult;

import java.util.function.BooleanSupplier;

/**
 * 高频控制流（fire-and-forget）。
 *
 * 与命令通道的区别：
 * - 命令通道：请求/响应，带 id，走 PriorityCommandQueue，适合离散动作（mine/craft/move_to）
 * - 控制流：无 id、无回执、latest-wins，适合 20-50Hz 的连续运动与视角，
 *   供 VLA 与 P0 反射层这类低延迟路径使用
 *
 * 安全：超过 {@link #STALE_TIMEOUT_MS} 没有新消息、或后端断线，都会自动松手。
 * 否则 Python 崩溃或网络抖动会让按键永久卡住、角色一路跑下去。
 *
 * 线程模型：{@link #apply} 与 {@link #requestRelease} 可能由 wire 读线程调用，
 * 只写 volatile 字段；真正操作游戏对象的动作全部在 {@link #tick}（渲染线程）完成。
 */
public final class ControlStream {

    /** 超过该时长没有新的控制消息就自动松手。 */
    public static final long STALE_TIMEOUT_MS = 500L;

    private static volatile boolean active = false;
    private static volatile boolean releasePending = false;
    private static volatile long lastUpdateMs = 0L;

    private static volatile boolean forward, back, left, right, jump, sneak, sprint;

    /**
     * attack / use 是**离散意图**：控制流消息可能快于客户端 tick，
     * 只存 boolean 会把"按下又松开"整个吞掉（见 {@link EdgeTrigger}）。
     * 上限 4 防止后端异常狂发边沿时无限堆积。
     */
    private static final EdgeTrigger attackTrigger = new EdgeTrigger(4);
    private static final EdgeTrigger useTrigger = new EdgeTrigger(4);

    /**
     * 一等公民的“按住”语义（吃食物 / 挖掘 / 放方块等需要**持续按住**的动作）。
     * 与上面的边沿触发互补：边沿负责“点一下”，hold 负责“按住 N tick 到期自动松开”。
     */
    private static final HoldController holds = new HoldController();

    /** 待消费的视角增量（度），高频消息快于 tick 时累加，避免丢位移。 */
    private static volatile float lookDx, lookDy;
    private static volatile Float absYaw, absPitch;

    /** 控制流自己维护的视角目标；active 时独占视角，松手后归还。 */
    private static Float targetYaw = null;
    private static float targetPitch = 0f;

    private ControlStream() {
    }

    /** 收到一条控制流消息（wire 读线程）。 */
    public static void apply(JsonObject message) {
        JsonObject d = new JsonObject();
        try {
            if (message != null && message.has("data") && message.get("data").isJsonObject()) {
                d = message.getAsJsonObject("data");
            }
            forward = bool(d, "forward");
            back = bool(d, "back");
            left = bool(d, "left");
            right = bool(d, "right");
            jump = bool(d, "jump");
            sneak = bool(d, "sneak");
            sprint = bool(d, "sprint");
            attackTrigger.update(bool(d, "attack"));
            useTrigger.update(bool(d, "use"));

            lookDx += num(d, "look_dx");
            lookDy += num(d, "look_dy");
            absYaw = nullableFloat(d, "yaw");
            absPitch = nullableFloat(d, "pitch");
        } catch (Exception e) {
            LCUMod.LOGGER.warn("[ControlStream] 非法控制消息: {}", e.getMessage());
            return;
        }
        lastUpdateMs = System.currentTimeMillis();
        active = true;
    }

    /** 请求松手。任意线程可调；实际清理在下一个 tick 完成。 */
    public static void requestRelease() {
        releasePending = true;
        active = false;
    }

    // ── 一等公民的“按住”语义 ──────────────────────────────────────

    /**
     * 从当前 tick 起按住 {@code control} {@code ticks} tick，到期**自动松开**（宁早松不粘键）。
     * 支持的控制：{@code forward/back/left/right/jump/sneak/sprint/attack/use}。
     * 调用方可保存返回的 {@link HoldController.Handle} 观察何时/为何松开。
     */
    public static HoldController.Handle hold(String control, int ticks) {
        return holds.hold(control, ticks);
    }

    /**
     * 按住 {@code control} 直到 {@code stillWanted} 返回 false 或超过 {@code maxTicks}。
     * 松开原因在返回的 handle 上可验证（CONDITION_MET / TIMEOUT / 安全释放…）。
     */
    public static HoldController.Handle holdUntil(String control, BooleanSupplier stillWanted, int maxTicks) {
        return holds.holdUntil(control, stillWanted, maxTicks);
    }

    /** 立即释放某个控制上的 hold（若存在），并松开对应输入。 */
    public static HoldController.Handle releaseHold(String control) {
        HoldController.Handle h = holds.release(control, HoldController.Release.MANUAL);
        if (h != null) {
            Minecraft mc = Minecraft.getInstance();
            if (mc != null && mc.player != null) {
                releaseHoldInput(mc, h.control());
            }
        }
        return h;
    }

    /** 立即释放全部 hold（stop_all / 断线 / 新任务 / 收尾），并松开对应输入。 */
    public static void releaseHolds(HoldController.Release reason) {
        java.util.List<HoldController.Handle> released = holds.releaseAll(reason);
        if (released.isEmpty()) {
            return;
        }
        Minecraft mc = Minecraft.getInstance();
        if (mc != null && mc.player != null) {
            for (HoldController.Handle h : released) {
                releaseHoldInput(mc, h.control());
            }
        }
    }

    public static boolean isHolding(String control) {
        return holds.isHeld(control);
    }

    /** 每个客户端 tick 调用，置于其它输入写入者之后，使其成为活跃时的最后写入者。 */
    public static void tick(Minecraft mc) {
        if (mc == null || mc.player == null) {
            return;
        }

        // ── hold 的统一安全闸门：AI 关闭 / 玩家接管 / 反射接管 → 当 tick 立即释放 ──
        boolean aiActive = InputIsolation.isAiControlled();
        boolean playerTakeover = mc.mouseHandler != null && mc.mouseHandler.isMouseGrabbed();
        boolean reflexOverride = ReflexGate.isSuppressed(ReflexGate.ALL);
        HoldController.TickResult holdTick = holds.tick(aiActive, playerTakeover, reflexOverride);
        for (HoldController.Handle h : holdTick.released()) {
            releaseHoldInput(mc, h.control());
        }

        if (releasePending) {
            releasePending = false;
            targetYaw = null;
            clearIntent();
            InputIsolation.clearAiControls();
            for (HoldController.Handle h : holds.releaseAll(HoldController.Release.MANUAL)) {
                releaseHoldInput(mc, h.control());
            }
            return;
        }

        if (!aiActive) {
            // AI 不控制时绝不写任何输入
            return;
        }

        if (active && System.currentTimeMillis() - lastUpdateMs > STALE_TIMEOUT_MS) {
            // 控制流自身超时：只松控制流自己的键，**不动**命令级 hold（吃/挖/放由命令驱动）
            active = false;
            targetYaw = null;
            clearIntent();
            InputIsolation.clearAiControls();
        }

        if (active) {
            InputIsolation.setAiControlState("forward", forward);
            InputIsolation.setAiControlState("back", back);
            InputIsolation.setAiControlState("left", left);
            InputIsolation.setAiControlState("right", right);
            InputIsolation.setAiControlState("jump", jump);
            InputIsolation.setAiControlState("sneak", sneak);
            InputIsolation.setAiControlState("sprint", sprint);

            applyLook(mc);
        }

        // 命令级 hold（吃/挖/放/use_item）：每 tick 写入，到期或条件命中已在上面的 tick 里松开
        for (String control : holdTick.applied()) {
            applyHoldInput(mc, control);
        }

        applyCombatActions(mc);
    }

    /** 把一个 hold 控制写进实际的输入（按键 / 挖掘）。 */
    private static void applyHoldInput(Minecraft mc, String control) {
        switch (control) {
            case "forward", "back", "left", "right", "jump", "sneak", "sprint" ->
                    InputIsolation.setAiControlState(control, true);
            case "use" -> {
                if (mc.options != null) {
                    mc.options.keyUse.setDown(true);
                }
            }
            case "attack" -> {
                if (mc.options != null) {
                    mc.options.keyAttack.setDown(true);
                }
                // 直接驱动挖掘：原版 continueAttack 需要 isMouseGrabbed，本模组刻意不抓鼠标，
                // 所以这里自己按 hitResult 每 tick 继续破坏（continueDestroyBlock 会自动 startDestroyBlock）。
                if (mc.gameMode != null && mc.level != null) {
                    if (mc.hitResult instanceof BlockHitResult bh
                            && !mc.level.getBlockState(bh.getBlockPos()).isAir()) {
                        mc.gameMode.continueDestroyBlock(bh.getBlockPos(), bh.getDirection());
                    } else if (mc.hitResult instanceof EntityHitResult eh
                            && AttackRules.canHit(mc, eh.getEntity())) {
                        // 统一安全检查（AttackRules）：隔墙 / 旁边有别的玩家（横扫误伤）⇒ 这一 tick 不挥。
                        mc.gameMode.attack(mc.player, eh.getEntity());
                        mc.player.swing(InteractionHand.MAIN_HAND, SwingAnimation.DEFAULT, true);
                    }
                }
            }
            default -> { /* 未知控制：忽略 */ }
        }
    }

    /** 一个 hold 结束时的收尾：松开对应按键并停止挖掘 / 取消使用。 */
    private static void releaseHoldInput(Minecraft mc, String control) {
        switch (control) {
            case "forward", "back", "left", "right", "jump", "sneak", "sprint" ->
                    InputIsolation.setAiControlState(control, false);
            case "use" -> {
                if (mc.options != null) {
                    mc.options.keyUse.setDown(false);
                }
                if (mc.gameMode != null && mc.player != null && mc.player.isUsingItem()) {
                    mc.gameMode.releaseUsingItem(mc.player);
                }
            }
            case "attack" -> {
                if (mc.options != null) {
                    mc.options.keyAttack.setDown(false);
                }
                if (mc.gameMode != null) {
                    mc.gameMode.stopDestroyBlock();
                }
            }
            default -> { /* ignore */ }
        }
    }

    /** 视角：以自己维护的目标为基准叠加增量；绝对视角优先。 */
    private static void applyLook(Minecraft mc) {
        float dx = lookDx, dy = lookDy;
        lookDx = 0f;
        lookDy = 0f;
        Float yaw = absYaw, pitch = absPitch;
        absYaw = null;
        absPitch = null;

        float baseYaw = targetYaw != null ? targetYaw : mc.player.getYRot();
        float basePitch = targetYaw != null ? targetPitch : mc.player.getXRot();

        if (yaw != null) baseYaw = yaw;
        if (pitch != null) basePitch = pitch;
        baseYaw += dx;
        basePitch = clampPitch(basePitch + dy);

        targetYaw = baseYaw;
        targetPitch = basePitch;
        InputIsolation.setAiLookTarget(baseYaw, basePitch);
    }

    /**
     * attack / use：离散意图，按游戏内冷却消费（见 {@link EdgeTrigger}）。
     *
     * <p>attack 用原版攻击冷却（{@code getAttackStrengthScale}）把关：冷却中就绪时才消费一次；
     * 点按不会被冷掉，按住则每 tick 复查。use 每次就绪即触发（吃/拉弓/放方块由原版处理）。
     */
    private static void applyCombatActions(Minecraft mc) {
        if (mc.gameMode == null) {
            return;
        }

        boolean attackReady = mc.player.getAttackStrengthScale(0.5f) >= 1.0f;
        if (attackTrigger.shouldFire(attackReady)) {
            if (mc.hitResult instanceof EntityHitResult hit && AttackRules.canHit(mc, hit.getEntity())) {
                // 统一安全检查（AttackRules）：隔墙 / 旁边有别的玩家 ⇒ 不造成伤害（仍保留挥手动画）。
                mc.gameMode.attack(mc.player, hit.getEntity());
            }
            mc.player.swing(InteractionHand.MAIN_HAND, SwingAnimation.DEFAULT, true);
        }

        if (useTrigger.shouldFire(true)) {
            mc.gameMode.useItem(mc.player, InteractionHand.MAIN_HAND);
        }
    }

    /** 清空所有离散意图（松手 / 断线 / 超时时调用，避免残留一次攻击或使用）。 */
    private static void clearIntent() {
        attackTrigger.reset();
        useTrigger.reset();
    }

    public static boolean isActive() {
        return active;
    }

    public static JsonObject describeState() {
        JsonObject o = new JsonObject();
        o.addProperty("active", active);
        o.addProperty("stale_ms", active ? System.currentTimeMillis() - lastUpdateMs : -1L);
        o.addProperty("forward", forward);
        o.addProperty("back", back);
        o.addProperty("left", left);
        o.addProperty("right", right);
        o.addProperty("jump", jump);
        o.addProperty("sneak", sneak);
        o.addProperty("sprint", sprint);
        o.addProperty("attack", attackTrigger.isHeld());
        o.addProperty("use", useTrigger.isHeld());
        o.addProperty("pending_attacks", attackTrigger.pending());
        o.addProperty("pending_uses", useTrigger.pending());
        o.addProperty("holding_attack", holds.isHeld("attack"));
        o.addProperty("holding_use", holds.isHeld("use"));
        o.addProperty("holds", holds.activeCount());
        return o;
    }

    private static boolean bool(JsonObject o, String key) {
        return o.has(key) && !o.get(key).isJsonNull() && o.get(key).getAsBoolean();
    }

    private static float num(JsonObject o, String key) {
        return o.has(key) && !o.get(key).isJsonNull() ? o.get(key).getAsFloat() : 0f;
    }

    private static Float nullableFloat(JsonObject o, String key) {
        return o.has(key) && !o.get(key).isJsonNull() ? o.get(key).getAsFloat() : null;
    }

    private static float clampPitch(float pitch) {
        return Math.max(-90f, Math.min(90f, pitch));
    }
}
