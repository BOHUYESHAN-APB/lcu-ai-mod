package com.lcu.lcumod.action;

import net.minecraft.client.Minecraft;
import net.minecraft.core.BlockPos;
import net.minecraft.world.entity.Entity;
import net.minecraft.world.entity.Mob;
import net.minecraft.world.entity.monster.Enemy;
import net.minecraft.world.item.ItemStack;
import net.minecraft.world.level.block.Blocks;
import net.minecraft.world.phys.Vec3;

/** Always-local survival reflexes that preempt planner and autonomous work. */
public final class SafetyReflexController {
    public enum Reflex {
        NONE, LEDGE_BRAKE, DROWNING_ESCAPE, FIRE_ESCAPE, LAVA_ESCAPE, HOSTILE_ESCAPE, AUTO_EAT
    }

    private static Reflex active = Reflex.NONE;
    private static String detail = "";
    private static int eatCooldown;

    private SafetyReflexController() {}

    public static boolean tick(Minecraft mc, Runnable preemptLowerPriority) {
        if (mc.player == null || mc.level == null || !InputIsolation.isAiControlled()) {
            reset();
            return false;
        }

        Reflex next = choose(mc);
        if (next != active) {
            clearReflexInputs(mc);
            if (next != Reflex.NONE) preemptLowerPriority.run();
            active = next;
        }
        if (eatCooldown > 0) eatCooldown--;

        switch (active) {
            case LEDGE_BRAKE -> brakeAtLedge();
            case DROWNING_ESCAPE -> escapeDrowning();
            case FIRE_ESCAPE, LAVA_ESCAPE -> escapeFireOrLava();
            case HOSTILE_ESCAPE -> fleeHostile(mc);
            case AUTO_EAT -> eat(mc);
            case NONE -> { return false; }
        }
        return true;
    }

    private static Reflex choose(Minecraft mc) {
        if (mc.player.isInLava()) {
            detail = "player is in lava";
            return Reflex.LAVA_ESCAPE;
        }
        if (mc.player.isOnFire()) {
            detail = "player is on fire";
            return Reflex.FIRE_ESCAPE;
        }
        if (mc.player.isUnderWater() && mc.player.getAirSupply() < 80) {
            detail = "air supply is low";
            return Reflex.DROWNING_ESCAPE;
        }
        Entity hostile = nearestHostile(mc, 8.0);
        if (hostile != null && mc.player.getHealth() <= mc.player.getMaxHealth() * 0.4f) {
            detail = "low health near hostile " + hostile.getName().getString();
            return Reflex.HOSTILE_ESCAPE;
        }
        if (isUnsafeStepAhead(mc)) {
            detail = "unsafe drop ahead";
            return Reflex.LEDGE_BRAKE;
        }
        if (eatCooldown <= 0 && shouldUseHealingFood(mc) && findHealingFoodSlot(mc) >= 0) {
            detail = "health is low and a golden apple is available";
            return Reflex.AUTO_EAT;
        }
        if (eatCooldown <= 0 && mc.player.getFoodData().getFoodLevel() < 14 && findFoodSlot(mc) >= 0) {
            detail = "hunger below 14";
            return Reflex.AUTO_EAT;
        }
        detail = "";
        return Reflex.NONE;
    }

    private static boolean isUnsafeStepAhead(Minecraft mc) {
        if (!mc.player.onGround()) return false;
        Vec3 movement = mc.player.getDeltaMovement();
        Vec3 horizontal = new Vec3(movement.x, 0, movement.z);
        if (horizontal.lengthSqr() < 0.0025) return false;
        BlockPos ahead = BlockPos.containing(mc.player.position().add(horizontal.normalize().scale(0.9)));
        for (int depth = 1; depth <= 3; depth++) {
            BlockPos support = ahead.below(depth);
            var state = mc.level.getBlockState(support);
            if (!state.getCollisionShape(mc.level, support).isEmpty()) {
                return state.is(Blocks.LAVA) || state.is(Blocks.FIRE) || state.is(Blocks.SOUL_FIRE)
                    || state.is(Blocks.CACTUS) || state.is(Blocks.CAMPFIRE) || state.is(Blocks.SOUL_CAMPFIRE);
            }
        }
        return true;
    }

    private static void brakeAtLedge() {
        InputIsolation.setAiControlState("forward", false);
        InputIsolation.setAiControlState("back", false);
        InputIsolation.setAiControlState("sprint", false);
        InputIsolation.setAiControlState("sneak", true);
    }

    private static void escapeDrowning() {
        InputIsolation.setAiControlState("jump", true);
        InputIsolation.setAiControlState("forward", true);
    }

    private static void escapeFireOrLava() {
        InputIsolation.setAiControlState("jump", true);
        InputIsolation.setAiControlState("forward", true);
        InputIsolation.setAiControlState("sprint", true);
    }

    private static void fleeHostile(Minecraft mc) {
        Entity hostile = nearestHostile(mc, 12.0);
        if (hostile == null) return;
        Vec3 away = mc.player.position().subtract(hostile.position());
        if (away.lengthSqr() < 0.001) away = new Vec3(1, 0, 0);
        double yaw = Math.toDegrees(Math.atan2(-away.x, away.z));
        mc.player.setYRot((float) yaw);
        InputIsolation.setAiControlState("forward", true);
        InputIsolation.setAiControlState("sprint", true);
        InputIsolation.setAiControlState("jump", mc.player.horizontalCollision);
    }

    private static void eat(Minecraft mc) {
        boolean healing = shouldUseHealingFood(mc);
        if (!healing && mc.player.getFoodData().getFoodLevel() >= 19) {
            eatCooldown = 40;
            active = Reflex.NONE;
            return;
        }
        if (mc.player.isUsingItem()) return;
        int slot = healing ? findHealingFoodSlot(mc) : findFoodSlot(mc);
        if (slot < 0 || mc.gameMode == null) {
            eatCooldown = 100;
            active = Reflex.NONE;
            return;
        }
        EquipmentManager.selectHotbarSlot(mc, slot);
        mc.gameMode.useItem(mc.player, net.minecraft.world.InteractionHand.MAIN_HAND);
        eatCooldown = 10;
    }

    private static boolean shouldUseHealingFood(Minecraft mc) {
        return mc.player.getHealth() <= mc.player.getMaxHealth() * 0.6f
            && mc.player.getAbsorptionAmount() < 1.0f;
    }

    private static int findHealingFoodSlot(Minecraft mc) {
        EquipmentManager.ensureHealingFoodInHotbar(mc);
        return EquipmentManager.healingFoodHotbarSlot(mc);
    }

    private static int findFoodSlot(Minecraft mc) {
        EquipmentManager.ensureFoodInHotbar(mc);
        return EquipmentManager.bestFoodHotbarSlot(mc);
    }

    private static Entity nearestHostile(Minecraft mc, double radius) {
        Entity nearest = null;
        double nearestDistance = radius;
        for (Entity entity : mc.level.getEntities(mc.player, mc.player.getBoundingBox().inflate(radius))) {
            if (!(entity instanceof Enemy) || !(entity instanceof Mob mob) || !mob.isAlive()) continue;
            double distance = mc.player.distanceTo(entity);
            if (distance < nearestDistance) {
                nearest = entity;
                nearestDistance = distance;
            }
        }
        return nearest;
    }

    private static void clearReflexInputs(Minecraft mc) {
        InputIsolation.clearAiControls();
        if (mc.player != null && mc.player.isUsingItem() && mc.gameMode != null && active == Reflex.AUTO_EAT) {
            mc.gameMode.releaseUsingItem(mc.player);
        }
    }

    public static void reset() {
        Minecraft mc = Minecraft.getInstance();
        if (mc != null) clearReflexInputs(mc);
        active = Reflex.NONE;
        detail = "";
    }

    public static boolean isActive() { return active != Reflex.NONE; }
    public static String stateName() { return active.name().toLowerCase(); }
    public static String detail() { return detail; }
}
