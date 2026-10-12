package com.lcu.lcumod.action;

import net.minecraft.client.Minecraft;
import net.minecraft.core.BlockPos;
import net.minecraft.world.entity.Entity;
import net.minecraft.world.entity.player.Player;
import net.minecraft.world.item.ItemStack;
import net.minecraft.world.phys.Vec3;

import java.util.List;
import java.util.Random;

/**
 * Java-side autonomous behavior system.
 * Works independently of Python backend - provides basic AI when:
 * - Backend is not connected
 * - LLM is processing (slow response)
 * - No backend command is given
 * 
 * Features:
 * - Wandering/patrol behavior
 * - Basic combat (attack nearby hostiles) —— **打谁由 {@link MobPolicy} 那张表决定**
 *   （用户口径："高危险的不主动打；中立的不惹；只有该处理的才处理"）
 * - Basic survival (eat when hungry, flee when low health)
 * - Head tracking of nearby players
 * - Random idle actions
 */
public class JavaAutonomousBehavior {

    private static final Random random = new Random();
    
    // State
    private static boolean enabled = true;
    private static BehaviorState currentState = BehaviorState.IDLE;
    private static int stateTicks = 0;
    private static int idleTicks = 0;
    
    // Wandering
    private static Vec3 wanderTarget = null;
    private static int wanderCooldown = 0;
    private static final int MIN_WANDER_COOLDOWN = 220;  // 11 seconds
    private static final int MAX_WANDER_COOLDOWN = 700;  // 35 seconds
    private static final double WANDER_RADIUS = 9.0;
    
    // Combat
    private static Entity attackTarget = null;
    private static int attackCooldown = 0;
    private static final int ATTACK_COOLDOWN_TICKS = 20;  // 1 second
    
    // Survival
    private static int eatCooldown = 0;
    private static final int EAT_COOLDOWN_TICKS = 40;  // 2 seconds
    private static int eatingTicksRemaining = 0;
    
    // Head tracking
    private static Entity trackTarget = null;
    private static int trackCooldown = 0;
    private static final int TRACK_COOLDOWN_TICKS = 140;  // 7 seconds
    
    // Behavior states
    public enum BehaviorState {
        IDLE,           // Doing nothing
        WANDERING,      // Walking around
        FIGHTING,       // Attacking hostile
        FLEEING,        // Running away from danger
        EATING,         // Eating food
        TRACKING,       // Looking at player
        COLLECTING      // Picking up items
    }
    
    /**
     * Main tick method - called every client tick.
     * Returns true if behavior is active (should block backend commands).
     */
    public static boolean tick(Minecraft mc) {
        if (!enabled || mc.player == null || mc.level == null) return false;
        
        stateTicks++;
        
        // Priority 1: Survival - flee if health is very low
        if (shouldFlee(mc)) {
            if (currentState != BehaviorState.FLEEING) {
                setState(BehaviorState.FLEEING);
                flee(mc);
            }
            return true;
        }
        
        // Priority 2: Combat - attack nearby hostile mobs
        if (shouldAttack(mc)) {
            if (currentState != BehaviorState.FIGHTING) {
                setState(BehaviorState.FIGHTING);
            }
            attackNearbyHostile(mc);
            return true;
        }
        
        // Priority 3: Eat if hungry or keep eating until the consume window ends
        if (currentState == BehaviorState.EATING && eatingTicksRemaining > 0) {
            eatingTicksRemaining--;
            return true;
        }

        if (shouldEat(mc)) {
            if (currentState != BehaviorState.EATING) {
                setState(BehaviorState.EATING);
                eatFood(mc);
            }
            return true;
        }
        
        // Priority 4: Track nearby players (head movement)
        if (shouldTrackPlayer(mc)) {
            if (currentState != BehaviorState.TRACKING) {
                setState(BehaviorState.TRACKING);
            }
            trackNearbyPlayer(mc);
            return false;  // Don't block other actions
        }
        
        // Priority 5: Wander if idle too long
        if (shouldWander(mc)) {
            if (currentState != BehaviorState.WANDERING) {
                setState(BehaviorState.WANDERING);
                chooseWanderTarget(mc);
            }
            wander(mc);
            return true;
        }
        
        // Default: Idle
        if (currentState != BehaviorState.IDLE) {
            setState(BehaviorState.IDLE);
        }
        idleTicks++;
        
        return false;
    }
    
    // ── State Management ──
    
    private static void setState(BehaviorState newState) {
        if (currentState != newState) {
            currentState = newState;
            stateTicks = 0;
        }
    }
    
    public static BehaviorState getState() {
        return currentState;
    }
    
    public static boolean isEnabled() {
        return enabled;
    }
    
    public static void setEnabled(boolean v) {
        enabled = v;
        if (!enabled) {
            currentState = BehaviorState.IDLE;
            wanderTarget = null;
            attackTarget = null;
        }
    }
    
    // ── Survival Logic ──
    
    private static boolean shouldFlee(Minecraft mc) {
        float health = mc.player.getHealth();
        float maxHealth = mc.player.getMaxHealth();
        return health < maxHealth * 0.3f;  // Flee below 30% health
    }
    
    private static void flee(Minecraft mc) {
        // Find nearest threat and run away from it.
        // 逃生只躲**真的会伤害我们**的档（高危险 / 敌对）；中立（末影人/狼）与被动（牛）不躲 ——
        // 用户口径："中立的不惹"，看见牛就逃反而是坏行为。
        Entity nearestThreat = findNearest(mc, JavaAutonomousBehavior::threatensUs);
        if (nearestThreat == null) {
            setState(BehaviorState.IDLE);
            return;
        }
        
        // Calculate flee direction (opposite of the threat)
        Vec3 playerPos = mc.player.position();
        Vec3 threatPos = nearestThreat.position();
        Vec3 fleeDir = playerPos.subtract(threatPos).normalize();
        
        // Move away
        double targetX = playerPos.x + fleeDir.x * 10;
        double targetZ = playerPos.z + fleeDir.z * 10;
        double targetY = mc.player.getY();
        
        MovementSystem.moveTo(targetX, targetY, targetZ, 1.5f);  // Sprint away
    }
    
    // ── Combat Logic ──
    
    private static boolean shouldAttack(Minecraft mc) {
        if (attackCooldown > 0) {
            attackCooldown--;
            return false;
        }
        
        // Find nearest thing worth fighting within 8 blocks（主动出手 ⇒ 过 mayInitiate）
        attackTarget = findNearest(mc, JavaAutonomousBehavior::worthFighting);
        return attackTarget != null && mc.player.distanceTo(attackTarget) < 8;
    }
    
    private static void attackNearbyHostile(Minecraft mc) {
        if (attackTarget == null || !attackTarget.isAlive()) {
            attackTarget = null;
            setState(BehaviorState.IDLE);
            return;
        }
        
        // Look at target
        lookAtEntity(mc, attackTarget);
        
        double distance = mc.player.distanceTo(attackTarget);
        // Attack if close enough —— 且**出手必须干净**：隔墙不打、旁边有别的玩家不打（统一安全检查）。
        if (distance < 4) {
            AttackRules.Verdict verdict = AttackRules.check(mc, attackTarget);
            if (verdict != AttackRules.Verdict.OK) {
                // 打不到就退避一个冷却再重扫（否则会每 tick 重选同一个目标空转）。
                attackCooldown = ATTACK_COOLDOWN_TICKS * 4;
                attackTarget = null;
                setState(BehaviorState.IDLE);
                return;
            }
            if (mc.gameMode != null) {
                mc.gameMode.attack(mc.player, attackTarget);
                mc.player.swing(net.minecraft.world.InteractionHand.MAIN_HAND, net.minecraft.world.item.component.SwingAnimation.DEFAULT, true);
            }
            attackCooldown = ATTACK_COOLDOWN_TICKS;
        } else if (MobPolicy.preferDistance(AttackRules.entityTypeId(attackTarget)) > 0) {
            // 苦力怕 / 幻翼：**不贴脸**（mindcraft 规则，保持 ≥4 格）—— 主动凑上去就是吃爆炸。
            // 这里**不走过去、也不锁着目标空转**（否则会一直占着 FIGHTING 挡后端的命令）；
            // 退避一个冷却后重扫，它真进了 4 格上面那个分支会接住。
            attackCooldown = ATTACK_COOLDOWN_TICKS;
            attackTarget = null;
            setState(BehaviorState.IDLE);
        } else {
            // Move toward target
            MovementSystem.moveTo(
                attackTarget.getX(), 
                attackTarget.getY(), 
                attackTarget.getZ(), 
                1.2f
            );
        }
    }
    
    /**
     * 该不该**主动**打它 —— 与后端模型、本地战斗反射**同一张表**（{@link MobPolicy}）。
     *
     * <ul>
     *   <li>玩家：不主动出手（用户口径 + 误伤风险）；</li>
     *   <li>高危险 / 中立：{@link MobPolicy#mayInitiate} 已经拒绝（"高危险的不主动打、中立的不惹"）；</li>
     *   <li>被动（牛羊）/ 未知：表里放行，但**生存反射不是猎手** —— 用户口径"只有该处理的才处理"，
     *       这个反射只处理会伤害我们的**明确敌对**档；猎肉交给模型（它有 attack 工具与领地保护）。</li>
     * </ul>
     */
    private static boolean worthFighting(Entity entity) {
        if (entity instanceof Player) {
            return false;
        }
        MobPolicy.Tier tier = MobPolicy.tier(AttackRules.entityTypeId(entity));
        if (!MobPolicy.mayInitiate(tier)) {
            return false;
        }
        return tier == MobPolicy.Tier.HOSTILE;
    }
    
    /** 要不要**躲开**它：高危险与敌对（认不出的不算 —— 认不出就如实不当成威胁）。 */
    private static boolean threatensUs(Entity entity) {
        MobPolicy.Tier tier = MobPolicy.tier(AttackRules.entityTypeId(entity));
        return tier == MobPolicy.Tier.DANGEROUS || tier == MobPolicy.Tier.HOSTILE;
    }
    
    /**
     * 附近最近的、满足 {@code accept} 的生物（16 格内，与实体半径口径一致）。
     *
     * <p>以前这里是"只认 {@code Enemy}"的硬编码二分；现在两个用途（主动出手 / 逃生）各自给一个
     * 判据 —— 判据都来自 {@link MobPolicy}，与后端模型同一张表。
     */
    private static Entity findNearest(Minecraft mc, java.util.function.Predicate<Entity> accept) {
        Entity nearest = null;
        double nearestDist = Double.MAX_VALUE;
        
        var searchBox = mc.player.getBoundingBox().inflate(16);
        for (Entity entity : mc.level.getEntities(mc.player, searchBox)) {
            if (!entity.isAlive() || !accept.test(entity)) {
                continue;
            }
            double dist = mc.player.distanceTo(entity);
            if (dist < nearestDist) {
                nearest = entity;
                nearestDist = dist;
            }
        }
        return nearest;
    }
    
    // ── Eating Logic ──
    
    private static boolean shouldEat(Minecraft mc) {
        if (eatCooldown > 0) {
            eatCooldown--;
            return false;
        }
        
        int hunger = mc.player.getFoodData().getFoodLevel();
        float health = mc.player.getHealth();
        float maxHealth = mc.player.getMaxHealth();
        boolean needsHealingFood = health < maxHealth && hunger < 19;
        return hunger < 14 || needsHealingFood;
    }
    
    private static void eatFood(Minecraft mc) {
        var inv = mc.player.getInventory();
        
        // Find food in hotbar
        for (int i = 0; i < 9; i++) {
            ItemStack stack = inv.getItem(i);
            if (stack.isEmpty()) continue;
            
            // Check if item is food (by checking if it has food properties)
            if (stack.get(net.minecraft.core.component.DataComponents.FOOD) != null) {
                // Select and use
                inv.setSelectedSlot(i);
                if (mc.gameMode != null) {
                    mc.gameMode.useItem(mc.player, net.minecraft.world.InteractionHand.MAIN_HAND);
                }
                eatCooldown = EAT_COOLDOWN_TICKS;
                eatingTicksRemaining = 36;
                return;
            }
        }
        
        // No food found
        eatingTicksRemaining = 0;
        setState(BehaviorState.IDLE);
    }
    
    // ── Head Tracking ──
    
    private static boolean shouldTrackPlayer(Minecraft mc) {
        if (trackCooldown > 0) {
            trackCooldown--;
            return false;
        }
        
        if (idleTicks < 40) {
            return false;
        }

        Player nearest = findNearestPlayer(mc, 12);
        if (nearest == null) {
            return false;
        }

        return random.nextInt(100) < 8;
    }
    
    private static void trackNearbyPlayer(Minecraft mc) {
        Player nearest = findNearestPlayer(mc, 12);
        
        if (nearest != null) {
            trackTarget = nearest;
            lookAtEntity(mc, nearest);
            trackCooldown = TRACK_COOLDOWN_TICKS;
        }
    }

    private static Player findNearestPlayer(Minecraft mc, double maxDistance) {
        Player nearest = null;
        double nearestDist = Double.MAX_VALUE;

        for (Player player : mc.level.players()) {
            if (player == mc.player) continue;
            double dist = mc.player.distanceTo(player);
            if (dist < maxDistance && dist < nearestDist) {
                nearest = player;
                nearestDist = dist;
            }
        }
        return nearest;
    }
    
    private static void lookAtEntity(Minecraft mc, Entity entity) {
        if (entity == null) return;
        
        double dx = entity.getX() - mc.player.getX();
        double dy = entity.getEyeY() - mc.player.getEyeY();
        double dz = entity.getZ() - mc.player.getZ();
        double hDist = Math.sqrt(dx * dx + dz * dz);
        
        if (hDist < 0.01) return;
        
        float yaw = (float) Math.toDegrees(Math.atan2(-dx, dz));
        float pitch = (float) Math.toDegrees(-Math.atan2(dy, hDist));
        
        // Smooth rotation
        float currentYaw = mc.player.getYRot();
        float currentPitch = mc.player.getXRot();
        
        float yawDiff = yaw - currentYaw;
        float pitchDiff = pitch - currentPitch;
        
        // Normalize yaw difference
        while (yawDiff > 180) yawDiff -= 360;
        while (yawDiff < -180) yawDiff += 360;
        
        // Clamp rotation speed
        float maxYawSpeed = 3.0f;
        float maxPitchSpeed = 2.0f;
        
        float yawStep = Math.max(-maxYawSpeed, Math.min(maxYawSpeed, yawDiff));
        float pitchStep = Math.max(-maxPitchSpeed, Math.min(maxPitchSpeed, pitchDiff));
        
        mc.player.setYRot(currentYaw + yawStep);
        mc.player.setXRot(currentPitch + pitchStep);
    }
    
    // ── Wandering Logic ──
    
    private static boolean shouldWander(Minecraft mc) {
        if (wanderCooldown > 0) {
            wanderCooldown--;
            return false;
        }
        
        // Wander only after a noticeably idle pause
        return idleTicks > 180;  // 9 seconds of idle
    }
    
    private static void chooseWanderTarget(Minecraft mc) {
        // Bias wandering to the direction the player is already facing.
        // This keeps look + move decisions coherent instead of instantly
        // glancing one side and then walking the opposite way.
        double baseYawRad = Math.toRadians(mc.player.getYRot());
        double angleOffset = Math.toRadians(random.nextDouble() * 90.0 - 45.0);
        double angle = baseYawRad + angleOffset;
        double distance = 4 + random.nextDouble() * (WANDER_RADIUS - 4);

        double targetX = mc.player.getX() - Math.sin(angle) * distance;
        double targetZ = mc.player.getZ() + Math.cos(angle) * distance;
        double targetY = mc.player.getY();
        
        wanderTarget = new Vec3(targetX, targetY, targetZ);
        idleTicks = 0;
    }
    
    private static void wander(Minecraft mc) {
        if (wanderTarget == null) {
            setState(BehaviorState.IDLE);
            return;
        }
        
        // Check if we arrived
        double dx = wanderTarget.x - mc.player.getX();
        double dy = wanderTarget.y - mc.player.getY();
        double dz = wanderTarget.z - mc.player.getZ();
        double dist = Math.sqrt(dx * dx + dy * dy + dz * dz);
        
        if (dist < 1.0) {
            setState(BehaviorState.IDLE);
            wanderCooldown = MIN_WANDER_COOLDOWN + random.nextInt(MAX_WANDER_COOLDOWN - MIN_WANDER_COOLDOWN);
            wanderTarget = null;
            return;
        }
        
        // Move toward target
        MovementSystem.moveTo(wanderTarget.x, wanderTarget.y, wanderTarget.z, 0.8f);
    }
    
    // ── Status ──
    
    public static String getStatusString() {
        return String.format("Behavior: %s (ticks=%d, idle=%d)", 
            currentState.name(), stateTicks, idleTicks);
    }
}
