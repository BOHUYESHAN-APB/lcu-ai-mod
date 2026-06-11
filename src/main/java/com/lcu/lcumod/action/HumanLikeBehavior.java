package com.lcu.lcumod.action;

import net.minecraft.client.Minecraft;
import net.minecraft.world.entity.Entity;
import net.minecraft.world.entity.animal.Animal;
import net.minecraft.world.entity.player.Player;
import net.minecraft.world.phys.Vec3;

import java.util.ArrayList;
import java.util.List;
import java.util.Random;

/**
 * Human-like look behavior with SMOOTH rotation.
 * Inspired by mineflayer's bot.lookAt() with sensitivity quantization
 * and TouhouLittleMaid's rotlerp() smooth interpolation.
 *
 * Key principles:
 * 1. Never snap rotation instantly — always interpolate
 * 2. Max rotation speed per tick (like a real mouse)
 * 3. Random look targets with cooldowns
 * 4. Look at nearby players/animals/blocks
 */
public class HumanLikeBehavior {

    private static final Random random = new Random();
    private static long lastLookTime = 0;
    private static int lookCooldownTicks = 0;
    private static final int MIN_COOLDOWN = 180;   // 9 seconds minimum
    private static final int MAX_COOLDOWN = 420;  // 21 seconds maximum

    // Current target
    private static double targetYaw = Double.NaN;
    private static double targetPitch = Double.NaN;
    private static boolean hasTarget = false;

    // Rotation limits (like mineflayer's yawSpeed/pitchSpeed)
    private static final float MAX_YAW_PER_TICK = 1.5f;   // degrees per tick (slower = smoother)
    private static final float MAX_PITCH_PER_TICK = 1.0f;  // degrees per tick

    /**
     * Called every tick when AI is idle.
     * Randomly picks look targets and smoothly rotates toward them.
     */
    public static void tick(Minecraft mc) {
        if (mc.player == null || mc.level == null) return;

        // If we have a target, smoothly rotate toward it
        if (hasTarget) {
            smoothRotate(mc);
            return;
        }

        // Cooldown between look targets
        if (lookCooldownTicks > 0) {
            lookCooldownTicks--;
            return;
        }

        // Random chance to look at something (3% per eligible tick)
        if (random.nextInt(100) > 3) return;

        // Find nearby targets
        List<LookTarget> targets = findTargets(mc);
        if (targets.isEmpty()) {
            lookCooldownTicks = MIN_COOLDOWN + random.nextInt(MAX_COOLDOWN - MIN_COOLDOWN);
            return;
        }

        // Pick a random target (weighted)
        LookTarget chosen = weightedPick(targets);
        if (chosen != null) {
            // Calculate target yaw/pitch
            var p = mc.player;
            double dx = chosen.x - p.getX();
            double dy = chosen.y - p.getEyeY();
            double dz = chosen.z - p.getZ();
            double hDist = Math.sqrt(dx * dx + dz * dz);
            if (hDist < 0.01) return;

            targetYaw = Math.toDegrees(Math.atan2(-dx, dz));
            targetPitch = Math.toDegrees(-Math.atan2(dy, hDist));
            targetPitch = Math.max(-60, Math.min(60, targetPitch)); // limit pitch range
            hasTarget = true;
        }
    }

    /**
     * Smooth rotation toward target — like mineflayer's bot.lookAt().
     * Clamps rotation speed to MAX_YAW_PER_TICK / MAX_PITCH_PER_TICK.
     */
    private static void smoothRotate(Minecraft mc) {
        if (!hasTarget || Double.isNaN(targetYaw)) return;

        var p = mc.player;
        float currentYaw = p.getYRot();
        float currentPitch = p.getXRot();

        // Calculate difference
        double yawDiff = targetYaw - currentYaw;
        double pitchDiff = targetPitch - currentPitch;

        // Normalize yaw difference to [-180, 180]
        while (yawDiff > 180) yawDiff -= 360;
        while (yawDiff < -180) yawDiff += 360;

        // Check if we've reached the target
        if (Math.abs(yawDiff) < 1.0 && Math.abs(pitchDiff) < 1.0) {
            hasTarget = false;
            lookCooldownTicks = MIN_COOLDOWN + random.nextInt(MAX_COOLDOWN - MIN_COOLDOWN);
            return;
        }

        // Clamp rotation speed
        double yawStep = Math.max(-MAX_YAW_PER_TICK, Math.min(MAX_YAW_PER_TICK, yawDiff));
        double pitchStep = Math.max(-MAX_PITCH_PER_TICK, Math.min(MAX_PITCH_PER_TICK, pitchDiff));

        // Apply rotation
        p.setYRot((float) (currentYaw + yawStep));
        p.setXRot((float) (currentPitch + pitchStep));
    }

    private static List<LookTarget> findTargets(Minecraft mc) {
        List<LookTarget> targets = new ArrayList<>();
        Vec3 playerPos = mc.player.position();

        // Find nearby players (weight: 5)
        for (Entity entity : mc.level.players()) {
            if (entity == mc.player) continue;
            double dist = playerPos.distanceTo(entity.position());
            if (dist < 16) {
                targets.add(new LookTarget(
                    entity.getX(), entity.getEyeY(), entity.getZ(),
                    5.0 / Math.max(dist, 1)
                ));
            }
        }

        // Find nearby animals (weight: 2)
        for (Entity entity : mc.level.getEntitiesOfClass(Animal.class,
                mc.player.getBoundingBox().inflate(12))) {
            double dist = playerPos.distanceTo(entity.position());
            targets.add(new LookTarget(
                entity.getX(), entity.getEyeY(), entity.getZ(),
                2.0 / Math.max(dist, 1)
            ));
        }

        // Occasionally look at a forward-biased nearby point instead of a
        // random underground block. This avoids staring at the floor and keeps
        // idle gaze more aligned with eventual movement direction.
        if (random.nextInt(5) == 0) {
            double yawRad = Math.toRadians(mc.player.getYRot());
            double distance = 3 + random.nextDouble() * 5;
            double sideOffset = random.nextDouble() * 2.5 - 1.25;
            double bx = mc.player.getX() - Math.sin(yawRad) * distance + Math.cos(yawRad) * sideOffset;
            double by = mc.player.getEyeY() - 0.3 + random.nextDouble() * 1.4;
            double bz = mc.player.getZ() + Math.cos(yawRad) * distance + Math.sin(yawRad) * sideOffset;
            targets.add(new LookTarget(bx, by, bz, 1.0));
        }

        return targets;
    }

    private static LookTarget weightedPick(List<LookTarget> targets) {
        double totalWeight = 0;
        for (LookTarget t : targets) totalWeight += t.weight;
        if (totalWeight <= 0) return null;

        double roll = random.nextDouble() * totalWeight;
        for (LookTarget t : targets) {
            roll -= t.weight;
            if (roll <= 0) return t;
        }
        return targets.get(targets.size() - 1);
    }

    private record LookTarget(double x, double y, double z, double weight) {}
}
