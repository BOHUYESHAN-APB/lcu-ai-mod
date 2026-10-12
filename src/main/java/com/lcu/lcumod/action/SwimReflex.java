package com.lcu.lcumod.action;

import net.minecraft.client.Minecraft;
import net.minecraft.world.entity.LivingEntity;

/**
 * P0 游泳反射：**头在水下就按住跳跃键上浮**。
 *
 * 用户实机反馈：「它不会游泳，在水里面它会死。」
 *
 * 为什么放 Java：溺水是秒级致死（空气 300 tick 见底），不能让 LLM 参与（一轮 2–20s）。
 * 与 {@link CombatReflex} 同一思路：**只写必要的键**（这里只有 jump），不碰 WASD，
 * 因此不会跟 Pathfinder / 控制流抢横向控制；调用点放在它们之后，保证是 jump 的最后写入者。
 */
public final class SwimReflex {

    /** 窒息时"完全没有起色"的连续 tick 数（用于切换策略：上浮 → 上浮+向前找出口 → 交给后端）。 */
    private static int stuckTicks = 0;
    private static int lastAir = -1;
    private static String lastAction = "";

    private static boolean active = false;

    private SwimReflex() {
    }

    /** @return true = 正在水中行动（上浮或加速游）。 */
    public static boolean tick(Minecraft mc) {
        if (!InputIsolation.isAiControlled() || mc == null || mc.player == null) {
            release();
            return false;
        }
        var player = mc.player;
        if (!player.isInWater()) {
            release();
            return false;
        }

        // 后端可以临时关掉"见水就上浮"，去执行高难度动作（下潜挖矿/救人/贴水底前进）
        if (ReflexGate.isSuppressed(ReflexGate.SWIM)) {
            release();
            return false;
        }

        // 有水肺药水/水下呼吸头盔时空气不会掉 → 不必急着上浮（用户要求"特殊处理"）。
        // 用行为判据（空气有没有在掉）而不是查物品/效果，避免依赖易变的 API。
        boolean airDropping = player.getAirSupply() < player.getMaxAirSupply() - 5;
        active = true; // 一旦决定对水中情形动手，就标记 active（否则 release() 不会真的松键）
        if (!airDropping) {
            // 不缺氧：只维持"加速游泳"（用户要求会加速游泳）
            holdFastSwim(player);
            return true;
        }

        // 正在憋气：**死也不松手**。旧实现有 8 秒上限 → 深度大/被地形挡住时到点放弃，
        // 结果就是用户实测的"氧气耗竭也不上浮"。现在只在"确实有进展"时重置计数，
        // 完全没有起色时才追加向前游（找出口），并把放弃权交给后端（reflex_override）。
        if (player.getAirSupply() > lastAir) {
            stuckTicks = 0;
            lastAir = player.getAirSupply();
        } else {
            stuckTicks++;
        }

        holdFastSwim(player);
        InputIsolation.setControlState("swim", "jump", true);
        // 证据（clones/mindcraft/patches/mineflayer-pathfinder+2.4.5.patch:631-640）：
        // 憋气时 setControlState('jump',true) **且明确 sprint=false** —— 冲刺会干扰原版的上浮；
        // 只在"不缺氧气"时才用冲刺做加速游泳（见上面的分支）。
        InputIsolation.setControlState("swim", "sprint", false);
        if (player.getXRot() > -30.0f) {
            player.setXRot(Math.max(-55.0f, player.getXRot() - 5.0f));
        }

        // 2 秒没起色（多半头上有方块/在水下洞里）→ 同时向前游找出口
        if (stuckTicks > 40) {
            InputIsolation.setControlState("swim", "forward", true);
        }
        // 15 秒仍没起色：说明本地反射解决不了，松手交给后端（它可用 control 精确操作，
        // 也可读 air_supply 做更高级的决策）——不能让本能一直霸占输入。
        if (stuckTicks > 20 * 15) {
            lastAction = "giveup";
            release();
            return false;
        }
        lastAction = "surfacing";
        return true;
    }

    /** 水里按住冲刺 = 原版加速游泳。 */
    private static void holdFastSwim(LivingEntity player) {
        InputIsolation.setControlState("swim", "sprint", true);
    }

    /** 最近一次游泳反射动作（排错用）。 */
    public static String describeState() {
        return lastAction;
    }

    public static boolean isActive() {
        return active;
    }

    private static void release() {
        if (active) {
            InputIsolation.setControlState("swim", "jump", false);
            InputIsolation.setControlState("swim", "sprint", false);
            InputIsolation.setControlState("swim", "forward", false);
            active = false;
        }
        // 非 active 时也要清（可能上一次是"只加速游泳"分支置的 active）
        stuckTicks = 0;
        lastAir = -1;
    }
}
