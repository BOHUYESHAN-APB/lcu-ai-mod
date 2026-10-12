package com.lcu.lcumod.action;

import net.minecraft.client.Minecraft;
import net.minecraft.core.registries.BuiltInRegistries;
import net.minecraft.world.entity.Entity;
import net.minecraft.world.entity.player.Player;
import net.minecraft.world.level.ClipContext;
import net.minecraft.world.phys.HitResult;
import net.minecraft.world.phys.Vec3;

/**
 * 出手前的**统一安全检查**（用户实机："他隔墙攻击，理论上来讲他知道墙后有怪物，但他不应该去攻击
 * —— 所以刚才会打到我"）。
 *
 * <p>任何"会真的打出伤害"的调用点（{@code mc.gameMode.attack(...)}）都必须先过这里；挥空拳的
 * {@code swing(...)} 不需要（它不造成伤害，也不触发横扫）。
 *
 * <p>三条硬规则：
 * <ol>
 *   <li><b>视线</b>：从眼睛到目标身体的射线**不能被方块挡住** —— 隔墙不挥（这是本次实机 bug）；</li>
 *   <li><b>够得着</b>：按实际距离判定（不是"名字在附近"就算）；</li>
 *   <li><b>不误伤</b>：剑有**横扫**，旁边站着别的玩家时**只放行斧子/空手**（用户实机：在刷铁机边被误伤；
 *       用户口径："旁边有玩家的情况下，可以用斧子"）；</li>
 *   <li><b>禁用武器</b>：重锤 / 矛（见 {@link WeaponPolicy#isBanned}）**一律不许出手** —— 用户口径
 *       "这两个禁止 AI 使用，因为误伤更严重"。</li>
 * </ol>
 *
 * <p>被拒时返回的是**机器可读的原因码**（{@link Verdict#code()}），由调用点如实上报/写日志 ——
 * 不静默、也不假装打过了。
 */
public final class AttackRules {

    /** 攻击距离（原版方块/实体交互 3.0，留点余量，与 CombatReflex 同口径）。 */
    public static final double REACH = 3.5;

    /** 别的玩家离这么近就不挥（横扫会连带打到）——用户实机被误伤就是这个。 */
    public static final double SWEEP_PLAYER_CLEARANCE = 2.5;

    private AttackRules() {
    }

    /** 出手判定结果（顺序即优先级：先没有目标，再**目标是玩家**，再死/挡/远，最后误伤）。 */
    public enum Verdict {
        OK,
        NO_TARGET,
        PLAYER_TARGET,
        DEAD_OR_OTHER_LEVEL,
        WEAPON_BANNED,
        TOO_FAR,
        NO_LINE_OF_SIGHT,
        PLAYER_TOO_CLOSE;

        /** 机器可读原因码（回执/日志/state 都用它，不要各写一套字符串）。 */
        public String code() {
            return name().toLowerCase();
        }
    }

    /**
     * 实体的**注册名**（`minecraft:zombie`）—— 判断"该不该打"的唯一依据（见 {@link MobPolicy}）。
     *
     * <p>查不到注册名就**如实给空串**（自定义实体 / 将来的新版本）：{@link MobPolicy#tier} 会把它
     * 当 {@link MobPolicy.Tier#UNKNOWN}，绝不默认成敌对。
     */
    public static String entityTypeId(Entity entity) {
        if (entity == null) {
            return "";
        }
        var key = BuiltInRegistries.ENTITY_TYPE.getKey(entity.getType());
        return key == null ? "" : key.toString();
    }

    /** 能不能对 {@code target} 出手（{@link Verdict#OK} 才允许）。 */
    public static boolean canHit(Minecraft mc, Entity target) {
        return check(mc, target) == Verdict.OK;
    }

    /** 完整判定（含原因），调用点据此如实上报。 */
    public static Verdict check(Minecraft mc, Entity target) {
        if (mc == null || mc.player == null || mc.level == null || target == null) {
            return decide(false, false, false, false, false, false, false, false);
        }
        var player = mc.player;
        String held = heldWeaponId(player);

        // 视线：从眼睛射向目标**身体中心**；射线先撞到方块（比目标更近）就是被挡住了 ⇒ 不许挥。
        Vec3 eye = player.getEyePosition();
        Vec3 aim = target.getBoundingBox().getCenter();
        HitResult hit = mc.level.clip(new ClipContext(eye, aim,
                ClipContext.Block.COLLIDER, ClipContext.Fluid.NONE, player));
        boolean blocked = hit.getType() == HitResult.Type.BLOCK
                && hit.getLocation().distanceTo(eye) < eye.distanceTo(aim) - 0.15;

        // 误伤：横扫范围里有别的玩家（不是自己、也不是目标）⇒ 不出手（不触发横扫的武器才放行）。
        boolean playerNearby = false;
        for (Player other : mc.level.players()) {
            if (other == player || other == target) {
                continue;
            }
            if (other.distanceTo(player) <= SWEEP_PLAYER_CLEARANCE) {
                playerNearby = true;
                break;
            }
        }

        return decide(true,
                target instanceof Player,
                target.isAlive() && target.level() == player.level(),
                WeaponPolicy.isBanned(held),
                player.distanceTo(target) <= REACH,
                blocked,
                playerNearby,
                WeaponPolicy.isSweepSafe(held));
    }

    /**
     * 判定顺序的**纯逻辑内核**（零 Minecraft 依赖 ⇒ {@code verifyWalk} 直接断言）。
     *
     * <p>顺序 = 没有目标 → **目标是玩家**（用户口径："AI 不许打玩家"，2026-10 修：旧实现只在
     * 横扫循环里跳过 {@code target}，于是目标本身是玩家时不被拦）→ 死/异界 → 禁用武器 →
     * 够不着 → 视线被挡 → 旁边有玩家（只放行斧/空手）→ OK。
     */
    public static Verdict decide(boolean hasTarget, boolean targetIsPlayer, boolean aliveSameLevel,
                                 boolean weaponBanned, boolean withinReach, boolean blocked,
                                 boolean playerNearby, boolean sweepSafeWeapon) {
        if (!hasTarget) {
            return Verdict.NO_TARGET;
        }
        if (targetIsPlayer) {
            return Verdict.PLAYER_TARGET;
        }
        if (!aliveSameLevel) {
            return Verdict.DEAD_OR_OTHER_LEVEL;
        }
        if (weaponBanned) {
            return Verdict.WEAPON_BANNED;
        }
        if (!withinReach) {
            return Verdict.TOO_FAR;
        }
        if (blocked) {
            return Verdict.NO_LINE_OF_SIGHT;
        }
        if (playerNearby && !sweepSafeWeapon) {
            return Verdict.PLAYER_TOO_CLOSE;
        }
        return Verdict.OK;
    }

    /** 主手物品的注册名（`minecraft:xxx`）；空手返回空串（空手不造成伤害、也不会横扫，视为安全）。 */
    private static String heldWeaponId(Player player) {
        var stack = player.getMainHandItem();
        if (stack.isEmpty()) {
            return "";
        }
        return BuiltInRegistries.ITEM.getKey(stack.getItem()).toString();
    }
}
