package com.lcu.lcumod.action;

import net.minecraft.client.Minecraft;
import net.minecraft.world.phys.Vec3;

/**
 * Compatibility wrapper for older call sites.
 *
 * All high-level movement now routes through Pathfinder so that follow,
 * explore, wandering, and move_to all share the same navigation layer.
 * This avoids the old packet/setPos based pseudo-movement path.
 */
public class MovementSystem {

    private static float lastSpeed = 1.0f;

    public static boolean isMoving() {
        return Pathfinder.isNavigating();
    }

    public static Vec3 getTarget() {
        return Pathfinder.getTarget();
    }

    public static void moveTo(double x, double y, double z, float speed) {
        lastSpeed = speed;
        Pathfinder.navigateTo(null, x, y, z);
    }

    public static void stop() {
        Pathfinder.stop();
    }

    public static void tick(Minecraft mc) {
        // No-op. Navigation is driven entirely by Pathfinder.tick(mc).
    }

    public static float getLastSpeed() {
        return lastSpeed;
    }
}
