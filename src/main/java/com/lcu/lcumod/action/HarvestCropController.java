package com.lcu.lcumod.action;

import com.google.gson.JsonObject;
import com.lcu.lcumod.LCUMod;
import net.minecraft.client.Minecraft;
import net.minecraft.core.BlockPos;
import net.minecraft.core.registries.BuiltInRegistries;
import net.minecraft.network.protocol.game.ServerboundMovePlayerPacket;
import net.minecraft.world.InteractionHand;
import net.minecraft.world.level.block.state.BlockState;
import net.minecraft.world.level.block.state.properties.Property;
import net.minecraft.world.phys.BlockHitResult;
import net.minecraft.world.phys.HitResult;
import net.minecraft.world.phys.Vec3;

/** One verified harvest-and-replant transaction executed through normal player actions. */
final class HarvestCropController {
    private enum Phase { NAVIGATING, AIM_HARVEST, HARVESTING, AIM_PLANT, WAIT_PLANT }

    private String operationId;
    private BlockPos cropPos;
    private Vec3 interactionPos;
    private String cropId;
    private String seedItemId;
    private String targetToken;
    private int expectedAge;
    private int ticks;
    private int phaseTicks;
    private int plantAttempts;
    private int plantSeedBaseline;
    private boolean harvestConfirmed;
    private Phase phase;

    boolean isActive() { return operationId != null; }

    boolean owns(String requestId) { return isActive() && operationId.equals(requestId); }

    boolean start(Minecraft mc, String requestId, JsonObject args) {
        if (isActive() || mc.player == null || mc.level == null || mc.gameMode == null) return false;
        if (args == null || !args.has("x") || !args.has("y") || !args.has("z")
                || !args.has("block_id") || !args.has("age") || !args.has("target_token")) return false;
        BlockPos target = new BlockPos(args.get("x").getAsInt(), args.get("y").getAsInt(), args.get("z").getAsInt());
        if (!mc.level.hasChunkAt(target)) return false;
        String expectedId = args.get("block_id").getAsString();
        int requestedAge = args.get("age").getAsInt();
        String requestedToken = args.get("target_token").getAsString();
        CropPolicy.CropState crop = inspectCrop(mc.level.getBlockState(target));
        if (!crop.supported() || !crop.mature() || !expectedId.equals(blockId(mc.level.getBlockState(target)))
                || crop.age() == null || crop.age() != requestedAge
                || !requestedToken.equals(BlockSnapshotToken.create(mc.level, target, mc.level.getBlockState(target)))) return false;
        if (!EquipmentManager.hasItem(mc, crop.seedItemId())) return false;
        Vec3 stand = Pathfinder.findReachableInteractionPosition(mc, target, 4.5);
        if (stand == null) return false;
        double distance = mc.player.position().distanceTo(stand);
        if (distance > 0.85 && !MovementSystem.moveToOwned(requestId, stand.x, stand.y, stand.z, 1.0f)) return false;

        operationId = requestId;
        cropPos = target.immutable();
        interactionPos = stand;
        cropId = expectedId;
        seedItemId = crop.seedItemId();
        targetToken = requestedToken;
        expectedAge = requestedAge;
        ticks = 0;
        phaseTicks = 0;
        plantAttempts = 0;
        plantSeedBaseline = -1;
        harvestConfirmed = false;
        phase = distance > 0.85 ? Phase.NAVIGATING : Phase.AIM_HARVEST;
        return true;
    }

    void tick(Minecraft mc) {
        if (!isActive() || mc.player == null || mc.level == null || mc.gameMode == null) return;
        ticks++;
        phaseTicks++;
        if (ticks > 400) { fail("TIMEOUT", "crop transaction timed out"); return; }
        switch (phase) {
            case NAVIGATING -> tickNavigation(mc);
            case AIM_HARVEST -> aimAndStartHarvest(mc);
            case HARVESTING -> continueHarvest(mc);
            case AIM_PLANT -> aimAndPlant(mc);
            case WAIT_PLANT -> verifyPlant(mc);
        }
    }

    void cancel(String code, String message) {
        if (!isActive()) return;
        Minecraft mc = Minecraft.getInstance();
        if (mc.gameMode != null) mc.gameMode.stopDestroyBlock();
        if (Pathfinder.isOwnedBy(id())) Pathfinder.stop();
        finish("cancelled", harvestConfirmed ? "CANCELLED_AFTER_HARVEST" : code,
            harvestConfirmed ? "harvest completed but replant was cancelled" : message);
    }

    private void tickNavigation(Minecraft mc) {
        double distance = mc.player.position().distanceTo(interactionPos);
        if (distance <= 0.9) { enter(Phase.AIM_HARVEST); return; }
        if (!Pathfinder.isNavigating()) fail("NAVIGATION_FAILED", "could not reach crop interaction position");
    }

    private void aimAndStartHarvest(Minecraft mc) {
        CropPolicy.CropState current = inspectCrop(mc.level.getBlockState(cropPos));
        if (!cropId.equals(blockId(mc.level.getBlockState(cropPos))) || !current.mature()
                || current.age() == null || current.age() != expectedAge
                || !targetToken.equals(BlockSnapshotToken.create(mc.level, cropPos, mc.level.getBlockState(cropPos)))) {
            fail("STALE_TARGET", "crop changed before harvest");
            return;
        }
        lookAt(mc, Vec3.atCenterOf(cropPos));
        if (phaseTicks < 2) return;
        BlockHitResult hit = targetedHit(mc, cropPos);
        if (hit == null) {
            if (phaseTicks > 30) fail("TARGET_NOT_VISIBLE", "crop is not under the crosshair");
            return;
        }
        mc.gameMode.startDestroyBlock(cropPos, hit.getDirection());
        mc.player.swing(InteractionHand.MAIN_HAND);
        enter(Phase.HARVESTING);
    }

    private void continueHarvest(Minecraft mc) {
        BlockState state = mc.level.getBlockState(cropPos);
        if (!cropId.equals(blockId(state))) {
            harvestConfirmed = true;
            mc.gameMode.stopDestroyBlock();
            enter(Phase.AIM_PLANT);
            return;
        }
        if (!targetToken.equals(BlockSnapshotToken.create(mc.level, cropPos, state))) {
            fail("TARGET_REPLACED", "crop state changed while harvesting");
            return;
        }
        BlockHitResult hit = targetedHit(mc, cropPos);
        if (hit != null) mc.gameMode.continueDestroyBlock(cropPos, hit.getDirection());
        if (phaseTicks > 80) fail("HARVEST_NOT_CONFIRMED", "server did not confirm crop harvest");
    }

    private void aimAndPlant(Minecraft mc) {
        CropPolicy.CropState planted = inspectCrop(mc.level.getBlockState(cropPos));
        if ((mc.player.isCreative() || plantSeedBaseline > 0 && countItem(mc, seedItemId) < plantSeedBaseline)
                && cropId.equals(blockId(mc.level.getBlockState(cropPos)))
                && planted.age() != null && planted.age() == 0) {
            succeed();
            return;
        }
        if (!mc.level.getBlockState(cropPos).isAir()) {
            fail("REPLANT_BLOCKED", "harvested crop position is no longer empty");
            return;
        }
        if (!EquipmentManager.ensureItemInHotbar(mc, seedItemId)) {
            fail("SEED_MISSING", "harvest succeeded but replant seed is unavailable");
            return;
        }
        int slot = EquipmentManager.itemHotbarSlot(mc, seedItemId);
        EquipmentManager.selectHotbarSlot(mc, slot);
        plantSeedBaseline = countItem(mc, seedItemId);
        BlockPos soil = cropPos.below();
        lookAt(mc, new Vec3(cropPos.getX() + 0.5, cropPos.getY(), cropPos.getZ() + 0.5));
        if (phaseTicks < 2) return;
        BlockHitResult hit = targetedHit(mc, soil);
        if (hit == null) {
            if (phaseTicks > 30) fail("SOIL_NOT_VISIBLE", "planting soil is not under the crosshair");
            return;
        }
        mc.gameMode.useItemOn(mc.player, InteractionHand.MAIN_HAND, hit);
        plantAttempts++;
        enter(Phase.WAIT_PLANT);
    }

    private void verifyPlant(Minecraft mc) {
        CropPolicy.CropState planted = inspectCrop(mc.level.getBlockState(cropPos));
        boolean seedConsumed = mc.player.isCreative()
            || plantSeedBaseline > 0 && countItem(mc, seedItemId) < plantSeedBaseline;
        if (cropId.equals(blockId(mc.level.getBlockState(cropPos))) && planted.age() != null
                && planted.age() == 0 && seedConsumed) {
            succeed();
        } else if (phaseTicks >= 12) {
            if (plantAttempts >= 3) fail("REPLANT_NOT_CONFIRMED", "harvest succeeded but server did not confirm replant");
            else enter(Phase.AIM_PLANT);
        }
    }

    private void succeed() { finish("succeeded", "HARVESTED_AND_REPLANTED", "crop harvested and replanted"); }

    private void fail(String code, String message) {
        Minecraft mc = Minecraft.getInstance();
        if (mc.gameMode != null) mc.gameMode.stopDestroyBlock();
        if (Pathfinder.isOwnedBy(id())) Pathfinder.stop();
        finish("failed", code, message);
    }

    private void finish(String status, String code, String message) {
        String id = operationId;
        operationId = null;
        cropPos = null;
        interactionPos = null;
        cropId = null;
        seedItemId = null;
        targetToken = null;
        phase = null;
        if (id != null && LCUMod.WIRE != null) LCUMod.WIRE.sendOutcome(id, status, code, message);
    }

    private String id() { return operationId; }

    private void enter(Phase next) { phase = next; phaseTicks = 0; }

    private static BlockHitResult targetedHit(Minecraft mc, BlockPos pos) {
        HitResult fresh = mc.player.pick(4.5, 1.0f, false);
        if (!(fresh instanceof BlockHitResult hit) || hit.getType() != HitResult.Type.BLOCK) return null;
        return hit.getBlockPos().equals(pos) ? hit : null;
    }

    private static void lookAt(Minecraft mc, Vec3 target) {
        Vec3 eye = mc.player.getEyePosition();
        double dx = target.x - eye.x;
        double dy = target.y - eye.y;
        double dz = target.z - eye.z;
        double horizontal = Math.sqrt(dx * dx + dz * dz);
        float yaw = (float) Math.toDegrees(Math.atan2(-dx, dz));
        float pitch = (float) Math.toDegrees(-Math.atan2(dy, horizontal));
        mc.player.setYRot(yaw);
        mc.player.setXRot(pitch);
        if (mc.getConnection() != null) {
            mc.getConnection().send(new ServerboundMovePlayerPacket.Rot(yaw, pitch, mc.player.onGround()));
        }
    }

    private static CropPolicy.CropState inspectCrop(BlockState state) {
        Integer age = null;
        for (Property<?> property : state.getProperties()) {
            if (!"age".equals(property.getName())) continue;
            try { age = Integer.parseInt(propertyValueName(state, property)); }
            catch (NumberFormatException ignored) { age = null; }
        }
        return CropPolicy.inspect(blockId(state), age);
    }

    private static String blockId(BlockState state) {
        return BuiltInRegistries.BLOCK.getKey(state.getBlock()).toString();
    }

    private static int countItem(Minecraft mc, String itemId) {
        int count = 0;
        for (int slot = 0; slot < 36; slot++) {
            var stack = mc.player.getInventory().getItem(slot);
            if (!stack.isEmpty() && BuiltInRegistries.ITEM.getKey(stack.getItem()).toString().equals(itemId)) {
                count += stack.getCount();
            }
        }
        return count;
    }

    private static <T extends Comparable<T>> String propertyValueName(BlockState state, Property<T> property) {
        return property.getName(state.getValue(property));
    }
}
