package com.lcu.lcumod.action;

import com.lcu.lcumod.LCUMod;
import net.minecraft.client.Minecraft;
import net.minecraft.world.InteractionHand;
import net.minecraft.world.entity.Entity;
import net.minecraft.world.entity.LivingEntity;
import net.minecraft.world.entity.player.Player;
import net.minecraft.world.item.component.SwingAnimation;

/**
 * P0 战斗反射：**被敌对生物打了要立刻还手**。
 *
 * 用户实机反馈：「他手上有武器，被僵尸或者敌对生物打了，他不会防御。」
 *
 * 为什么放在 Java 而不是让 LLM 决定：按本架构的硬约束，**LLM 永远不能进应急路径**
 * （一轮 2–20s，而被打后 1 秒内不还手就可能被连击打死）。这里用原版 API
 * `player.getLastHurtByMob()` 直接拿到攻击者，当 tick 内转向 + 出手。
 *
 * 安全边界（刻意设计）：
 * - **打谁由 {@link MobPolicy} 那张表决定**（与后端模型**同一张表**）：高危险（监守者/凋灵/末影龙）
 *   **连还手都不还手** —— 如实交回模型 / 逃生；中立（末影人/狼/铁傀儡/村民…）只有**它先动手**
 *   才还手；认不出的按 UNKNOWN（不预先当敌人）。
 * - 玩家（包括服主本人）打你**不会**被打回去。
 * - 苦力怕 / 幻翼**不贴脸**（{@link MobPolicy#preferDistance} = 4 格，mindcraft 规则）。
 * - 只在 AI 控制模式下工作；用户控制模式绝不插手。
 * - 尊重原版攻击冷却（`getAttackStrengthScale`）与出手间隔，不刷攻速。
 * - **寻路中不写 WASD**（移动交给 Pathfinder / 控制流），只写**朝向 + 攻击**；只有在**没有**寻路
 *   任务时才由反射贴上去 / 后撤 —— 否则会跟寻路抢 forward 键，表现就是"卡在原地"（用户实机）。
 *
 * 调用时机：`ActionExecutor.onTick` 中**放在 ControlStream.tick 之后** —— 需要它在这种
 * 紧急情形下成为朝向的最后写入者（否则会被控制流的视角覆盖，等于不还手）。
 */
public final class CombatReflex {

    /** 出手距离（原版方块/实体交互 3.0，留点余量）。 */
    private static final double REACH = 3.5;
    /**
     * "边打边走"的贴合距离（minecraft-agent 的 defendSelf：≥4 贴上去、≤2 后撤）。
     *
     * <p>{@link MobPolicy#preferDistance} 只能把这个距离**往上抬**（苦力怕/幻翼 ≥4 不贴脸）。
     */
    private static final double APPROACH_DISTANCE = 4.0;
    /** 后撤距离（比这更近就退开，别贴脸站着挨打）。 */
    private static final double BACK_OFF_DISTANCE = 1.8;
    /** 两次反射出手的最小间隔（tick），约 0.5s。 */
    private static final int MIN_ATTACK_INTERVAL_TICKS = 10;
    /** 被打一次后持续"还记得谁打我"的时长（tick），约 3s。 */
    private static final int MEMORY_TICKS = 60;

    private static int cooldown = 0;
    private static LivingEntity lastAttacker = null;
    private static int memoryTicks = 0;
    private static String lastAction = "";
    private static boolean engaged = false;
    /** 变化驱动日志：只在这个"阶段"变了才打一行（用户口径："没有状态刷新就不刷新日志"）。 */
    private static String loggedPhase = "";

    private CombatReflex() {
    }

    /** @return true = 本 tick 处于交战状态（已还手或正在对峙）。 */
    public static boolean tick(Minecraft mc) {
        if (!InputIsolation.isAiControlled()) {
            return false;
        }
        if (mc == null || mc.player == null || mc.level == null || mc.gameMode == null) {
            return false;
        }
        var player = mc.player;
        if (ReflexGate.isSuppressed(ReflexGate.COMBAT)) {
            // 后端明确要求接管这场架（例如"先跑"）→ 本能不参与还手
            engaged = false;
            return false;
        }
        if (cooldown > 0) {
            cooldown--;
        }

        LivingEntity attacker;
        LivingEntity hurtBy = player.getLastHurtByMob();
        boolean freshEngagement = false;
        if (player.hurtTime > 0 && hurtBy != null && !(hurtBy instanceof Player)
                && !mayFight(hurtByTier(hurtBy), true)) {
            // 它先动手、但策略表说"不许还手"（高危险：监守者/凋灵/末影龙）⇒ **不还手**：
            // 如实记一条"拒绝"，把这场架交回模型 / 逃生路径（后端 Cowardice 会拉开距离）。
            lastAttacker = null;
            memoryTicks = 0;
            engaged = false;
            lastAction = "declined:" + hurtByTier(hurtBy).name()
                    + ":" + hurtBy.getName().getString();
            logPhase("declined:" + hurtByTier(hurtBy).name());
            releaseMovement();
            return false;
        }
        if (player.hurtTime > 0 && hurtBy != null && !(hurtBy instanceof Player)) {
            freshEngagement = lastAttacker == null;
            attacker = hurtBy;
            lastAttacker = hurtBy;
            memoryTicks = MEMORY_TICKS;
        } else if (memoryTicks > 0 && lastAttacker != null && lastAttacker.isAlive()
                && mayFight(hurtByTier(lastAttacker), false)) {
            // 架还没打完（这一 tick 没被打）：只有策略表放行才继续追 —— 中立怪只在"它先动手"
            // 的这轮里被追，高危险则立刻松手。
            memoryTicks--;
            attacker = lastAttacker;
        } else {
            lastAttacker = null;
            memoryTicks = 0;
            engaged = false;
            lastAction = "";
            logPhase("");
            releaseMovement();
            return false;
        }

        if (!attacker.isAlive() || attacker.level() != player.level()) {
            lastAttacker = null;
            engaged = false;
            logPhase("");
            releaseMovement();
            return false;
        }

        engaged = true;

        // 新的一场架：手里不是武器就先换一把（用户："它得学会切物品栏"）
        if (freshEngagement) {
            String weapon = ActionExecutor.equipBestWeapon(mc);
            lastAction = weapon == null ? "engage:barehand" : "equip:" + weapon;
            logPhase("engage:" + lastAction);
        }

        float[] aim = aimAngles(player, attacker);
        // **先转头、对准了才出手**：一边转身一边打，服务端收到的朝向与命中目标不一致，
        // 观感就是"像外挂一样打身后/打空气"（用户实机反馈）。
        // 这里用"上一 tick 的朝向"判断是否已对准，所以转身与出手天然分在两 tick，朝向先同步出去。
        double yawError = Math.abs(angleDiff(aim[0], player.getYRot()));
        double pitchError = Math.abs(aim[1] - player.getXRot());
        boolean aimed = yawError <= 15.0 && pitchError <= 20.0;

        player.setYRot(aim[0]);
        player.setXRot(Math.max(-90.0f, Math.min(90.0f, aim[1])));

        double distance = player.distanceTo(attacker);
        boolean weaponReady = player.getAttackStrengthScale(0.5f) >= 1.0f;

        // 边打边走（用户实机："攻击僵尸时不会边移动边攻击"）。参考 minecraft-agent 的 defendSelf：
        // 距离 >=4 贴上去、距离 <=2 后撤，中间原地打。
        // 但**寻路中不接管位移**：那会跟 Pathfinder 抢同一个 forward 键（谁后写谁赢），
        // 表现就是"有任务却卡在原地"。寻路时只写朝向 + 出手。
        if (Pathfinder.isNavigating()) {
            releaseMovement();
        } else {
            // 贴上去 / 后撤。苦力怕 / 幻翼**不贴脸**：策略表要求保持 ≥4 格
            // （{@link MobPolicy#preferDistance}），所以不往它身上凑，站着等它进攻击距离。
            double standoff = Math.max(APPROACH_DISTANCE,
                    MobPolicy.preferDistance(AttackRules.entityTypeId(attacker)));
            ActionExecutor.setInput("forward", distance > standoff);
            ActionExecutor.setInput("back", distance < BACK_OFF_DISTANCE);
        }

        if (distance <= REACH && weaponReady && cooldown == 0 && aimed) {
            // 出手前的统一安全检查（用户实机："他隔墙攻击，所以打到了我"）：
            // 视线被挡 / 旁边有别的玩家（横扫误伤）/ 够不着 ⇒ 不挥，如实记原因。
            AttackRules.Verdict verdict = AttackRules.check(mc, attacker);
            if (verdict != AttackRules.Verdict.OK) {
                lastAction = "blocked:" + verdict.code() + ":" + attacker.getName().getString();
                logPhase("blocked:" + verdict.code());
                return distance <= REACH * 2.0;
            }
            mc.gameMode.attack(player, attacker);
            player.swing(InteractionHand.MAIN_HAND, SwingAnimation.DEFAULT, true);
            cooldown = MIN_ATTACK_INTERVAL_TICKS;
            lastAction = "retaliate:" + attacker.getName().getString()
                    + "@" + String.format("%.1f", distance) + "m";
            logPhase("retaliate:" + attacker.getName().getString());
            return true;
        }

        lastAction = (aimed ? "facing:" : "turning:")
                + attacker.getName().getString() + "@" + String.format("%.1f", distance) + "m";
        logPhase((aimed ? "facing:" : "turning:") + attacker.getName().getString());
        return distance <= REACH * 2.0;
    }

    /**
     * 变化驱动日志：**阶段变了才打一行**（用户口径"没有状态刷新就不刷新日志"）。
     *
     * <p>为什么需要它：用户实机报"被打了不会还手"时，日志里**一条战斗记录都没有** ——
     * 这个反射以前只把状态塞进 state（`reflex_combat`），日志里看不出"到底有没有进入交战、
     * 有没有出手"，只能靠猜。这里在阶段（进入交战 / 出手 / 对准 / 转身 / 结束）变化时各打一次，
     * 距离抖动不再刷屏（否则每 tick 都在变，等于刷屏）。
     */
    private static void logPhase(String phase) {
        if (phase.equals(loggedPhase)) {
            return;
        }
        loggedPhase = phase;
        if (phase.isEmpty()) {
            LCUMod.LOGGER.info("[Reflex] Combat: 交战结束（目标没了或已死亡）");
        } else if (phase.startsWith("engage")) {
            LCUMod.LOGGER.info("[Reflex] Combat: 进入交战（{}）", lastAction);
        } else if (phase.startsWith("retaliate")) {
            LCUMod.LOGGER.info("[Reflex] Combat: 出手 {}", lastAction);
        } else {
            LCUMod.LOGGER.info("[Reflex] Combat: {} {}", phase, lastAction);
        }
    }

    /** 最近一次反射动作（用于状态上报/排错）。 */
    public static String describeState() {
        return lastAction;
    }

    /** 解除交战时松开位移键（否则会出现"打完了还在后退/前冲"）。 */
    private static void releaseMovement() {
        ActionExecutor.setInput("forward", false);
        ActionExecutor.setInput("back", false);
    }

    /**
     * 它是哪一档（{@link MobPolicy}；注册名认不出 ⇒ UNKNOWN，**绝不默认敌对**）。
     *
     * <p>玩家由调用点排除（用户口径：玩家打你不还手）—— 这里拿到的都是非玩家生物。
     */
    private static MobPolicy.Tier hurtByTier(Entity entity) {
        return MobPolicy.tier(AttackRules.entityTypeId(entity));
    }

    /**
     * 反射的出手许可 —— **与后端模型同一张表**（{@link MobPolicy}）。两个问题分开问：
     *
     * <ul>
     *   <li>{@code hitThisTick}：这一 tick 是**它打的** ⇒ 问"能不能还手"
     *       （{@link MobPolicy#mayRetaliate}）。高危险 ⇒ false（不还手，交回模型/逃生）；
     *       中立 ⇒ 只有它先动手才 true。</li>
     *   <li>否则（架还没打完、这一 tick 没被打）⇒ 问"能不能主动追"
     *       （{@link MobPolicy#mayInitiate}）；中立的主动票是 false，但它先动的手这轮还没结束，
     *       所以再补一票"能还手"——中立怪**只有这一轮**被追，高危险两票都否、立刻松手。</li>
     * </ul>
     */
    private static boolean mayFight(MobPolicy.Tier tier, boolean hitThisTick) {
        if (hitThisTick) {
            return MobPolicy.mayRetaliate(tier, true);
        }
        return MobPolicy.mayInitiate(tier) || MobPolicy.mayRetaliate(tier, true);
    }

    /** 是否正在交战（HUD 显示"跟它干上了"）。 */
    public static boolean isEngaged() {
        return engaged;
    }

    /** [yaw, pitch]：从玩家眼睛指向目标中部。 */
    private static float[] aimAngles(LivingEntity player, Entity target) {
        double dx = target.getX() - player.getX();
        double dy = (target.getY() + target.getEyeHeight() * 0.5) - player.getEyeY();
        double dz = target.getZ() - player.getZ();
        double horizontal = Math.sqrt(dx * dx + dz * dz);
        float yaw = (float) Math.toDegrees(Math.atan2(-dx, dz));
        float pitch = (float) Math.toDegrees(-Math.atan2(dy, horizontal));
        return new float[]{yaw, pitch};
    }

    /** 角度差（考虑 360 环绕）。 */
    private static double angleDiff(float a, float b) {
        double diff = (a - b) % 360.0;
        if (diff > 180.0) diff -= 360.0;
        if (diff < -180.0) diff += 360.0;
        return diff;
    }
}
