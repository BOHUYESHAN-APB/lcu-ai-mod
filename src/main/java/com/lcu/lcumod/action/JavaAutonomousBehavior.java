package com.lcu.lcumod.action;

import com.lcu.lcumod.config.ServerPolicy;
import net.minecraft.client.Minecraft;
import net.minecraft.core.BlockPos;
import net.minecraft.world.entity.Entity;
import net.minecraft.world.entity.animal.Animal;
import net.minecraft.world.entity.monster.Enemy;
import net.minecraft.world.entity.player.Player;
import net.minecraft.world.item.ItemStack;
import net.minecraft.world.phys.Vec3;
import net.minecraft.world.phys.EntityHitResult;

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
 * - Basic combat (attack nearby hostiles)
 * - Basic survival (eat when hungry, flee when low health)
 * - Head tracking of nearby players
 * - Random idle actions
 */
public class JavaAutonomousBehavior {

    private static final Random random = new Random();
    
    // State
    private static boolean enabled = false;
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
            resetCurrentState();
        }
    }

    public static void resetCurrentState() {
        currentState = BehaviorState.IDLE;
        stateTicks = 0;
        idleTicks = 0;
        wanderTarget = null;
        attackTarget = null;
        trackTarget = null;
        eatingTicksRemaining = 0;
    }
    
    // ── Survival Logic ──
    
    private static boolean shouldFlee(Minecraft mc) {
        if (!ServerPolicy.movementAutomationAllowed()) return false;
        float health = mc.player.getHealth();
        float maxHealth = mc.player.getMaxHealth();
        return health < maxHealth * 0.3f;  // Flee below 30% health
    }
    
    private static void flee(Minecraft mc) {
        // Find nearest hostile and run away from it
        Entity nearestHostile = findNearestHostile(mc);
        if (nearestHostile == null) {
            setState(BehaviorState.IDLE);
            return;
        }
        
        // Calculate flee direction (opposite of hostile)
        Vec3 playerPos = mc.player.position();
        Vec3 hostilePos = nearestHostile.position();
        Vec3 fleeDir = playerPos.subtract(hostilePos).normalize();
        
        // Move away
        double targetX = playerPos.x + fleeDir.x * 10;
        double targetZ = playerPos.z + fleeDir.z * 10;
        double targetY = mc.player.getY();
        
        MovementSystem.moveTo(targetX, targetY, targetZ, 1.5f);  // Sprint away
    }
    
    // ── Combat Logic ──
    
    private static boolean shouldAttack(Minecraft mc) {
        if (!ServerPolicy.automatedCombatAllowed()) return false;
        if (attackCooldown > 0) {
            attackCooldown--;
            return false;
        }
        
        // Find nearest hostile within 8 blocks
        attackTarget = findNearestHostile(mc);
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
        
        boolean targeted = mc.hitResult instanceof EntityHitResult hit && hit.getEntity() == attackTarget;
        if (targeted && mc.player.hasLineOfSight(attackTarget)
                && mc.player.getAttackStrengthScale(0.5f) >= 0.9f) {
            if (mc.gameMode != null) {
                mc.gameMode.attack(mc.player, attackTarget);
                mc.player.swing(net.minecraft.world.InteractionHand.MAIN_HAND);
            }
            attackCooldown = ATTACK_COOLDOWN_TICKS;
        } else {
            // Move toward target
            if (ServerPolicy.movementAutomationAllowed()) {
                MovementSystem.moveTo(
                    attackTarget.getX(),
                    attackTarget.getY(),
                    attackTarget.getZ(),
                    1.2f
                );
            }
        }
    }
    
    private static Entity findNearestHostile(Minecraft mc) {
        Entity nearest = null;
        double nearestDist = Double.MAX_VALUE;
        
        var searchBox = mc.player.getBoundingBox().inflate(16);
        for (Entity entity : mc.level.getEntities(mc.player, searchBox)) {
            if (entity instanceof Enemy && entity.isAlive()) {
                double dist = mc.player.distanceTo(entity);
                if (dist < nearestDist) {
                    nearest = entity;
                    nearestDist = dist;
                }
            }
        }
        return nearest;
    }
    
    // ── Eating Logic ──
    
    private static boolean shouldEat(Minecraft mc) {
        if (!ServerPolicy.inventoryAutomationAllowed()) return false;
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
            if (stack.getItem().getFoodProperties(stack, mc.player) != null) {
                // Select and use
                EquipmentManager.selectHotbarSlot(mc, i);
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
        if (!ServerPolicy.movementAutomationAllowed()) return false;
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
