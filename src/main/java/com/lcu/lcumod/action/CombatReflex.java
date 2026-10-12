package com.lcu.lcumod.action;

import com.lcu.lcumod.LCUMod;
import net.minecraft.client.Minecraft;
import net.minecraft.world.InteractionHand;
import net.minecraft.world.entity.Entity;
import net.minecraft.world.entity.LivingEntity;
import net.minecraft.world.entity.monster.Enemy;
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
 * - **只还击敌对生物**（`instanceof Enemy`）；玩家（包括服主本人）打你**不会**被打回去。
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
        if (player.hurtTime > 0 && hurtBy instanceof Enemy) {
            freshEngagement = lastAttacker == null;
            attacker = hurtBy;
            lastAttacker = hurtBy;
            memoryTicks = MEMORY_TICKS;
        } else if (memoryTicks > 0 && lastAttacker != null && lastAttacker.isAlive()) {
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
            ActionExecutor.setInput("forward", distance > 4.0);
            ActionExecutor.setInput("back", distance < 1.8);
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
