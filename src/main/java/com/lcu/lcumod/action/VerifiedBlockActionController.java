package com.lcu.lcumod.action;

import com.google.gson.JsonObject;
import com.lcu.lcumod.LCUMod;
import net.minecraft.client.Minecraft;
import net.minecraft.core.BlockPos;
import net.minecraft.core.Direction;
import net.minecraft.core.registries.BuiltInRegistries;
import net.minecraft.network.protocol.game.ServerboundMovePlayerPacket;
import net.minecraft.world.InteractionHand;
import net.minecraft.world.level.block.state.BlockState;
import net.minecraft.world.phys.BlockHitResult;
import net.minecraft.world.phys.HitResult;
import net.minecraft.world.phys.Vec3;

/** Verified addressed block actions for the authenticated real-player body. */
final class VerifiedBlockActionController {
    enum Kind { BREAK, USE, PLACE }
    private enum Phase { NAVIGATING, AIMING, ACTING, VERIFYING }

    private String operationId;
    private Kind kind;
    private Phase phase;
    private BlockPos targetPos;
    private BlockPos placedPos;
    private Vec3 interactionPos;
    private String targetToken;
    private String targetBlockId;
    private String itemId;
    private Direction requestedFace;
    private int itemBaseline;
    private int menuBaseline;
    private int ticks;
    private int phaseTicks;

    boolean isActive() { return operationId != null; }
    boolean owns(String id) { return isActive() && operationId.equals(id); }

    boolean start(Minecraft mc, String id, Kind requestedKind, JsonObject args) {
        if (isActive() || mc.player == null || mc.level == null || mc.gameMode == null || args == null) return false;
        if (!args.has("x") || !args.has("y") || !args.has("z") || !args.has("target_token")) return false;
        BlockPos target = new BlockPos(args.get("x").getAsInt(), args.get("y").getAsInt(), args.get("z").getAsInt());
        if (!mc.level.hasChunkAt(target)) return false;
        BlockState state = mc.level.getBlockState(target);
        String token = args.get("target_token").getAsString();
        if (!token.equals(BlockSnapshotToken.create(mc.level, target, state))) return false;
        Direction face = parseFace(args);
        if (args.has("face") && face == null) return false;

        BlockPos destination = null;
        String heldItem = null;
        if (requestedKind == Kind.PLACE) {
            if (!args.has("place_x") || !args.has("place_y") || !args.has("place_z") || !args.has("item_id")) return false;
            destination = new BlockPos(
                args.get("place_x").getAsInt(), args.get("place_y").getAsInt(), args.get("place_z").getAsInt()
            );
            heldItem = args.get("item_id").getAsString();
            if (!mc.level.hasChunkAt(destination) || !mc.level.getBlockState(destination).isAir()
                    || !EquipmentManager.hasItem(mc, heldItem)) return false;
        }

        Vec3 stand = Pathfinder.findReachableInteractionPosition(mc, target, 4.5);
        if (stand == null) return false;
        double distance = mc.player.position().distanceTo(stand);
        if (distance > 0.85 && !MovementSystem.moveToOwned(id, stand.x, stand.y, stand.z, 1.0f)) return false;

        operationId = id;
        kind = requestedKind;
        targetPos = target.immutable();
        placedPos = destination == null ? null : destination.immutable();
        interactionPos = stand;
        targetToken = token;
        targetBlockId = blockId(state);
        itemId = heldItem;
        requestedFace = face;
        itemBaseline = -1;
        menuBaseline = mc.player.containerMenu.containerId;
        ticks = 0;
        phaseTicks = 0;
        phase = distance > 0.85 ? Phase.NAVIGATING : Phase.AIMING;
        return true;
    }

    void tick(Minecraft mc) {
        if (!isActive() || mc.player == null || mc.level == null || mc.gameMode == null) return;
        ticks++;
        phaseTicks++;
        if (ticks > 400) { fail("TIMEOUT", "verified block action timed out"); return; }
        switch (phase) {
            case NAVIGATING -> {
                if (mc.player.position().distanceTo(interactionPos) <= 0.9) enter(Phase.AIMING);
                else if (!Pathfinder.isNavigating()) fail("NAVIGATION_FAILED", "could not reach interaction position");
            }
            case AIMING -> aim(mc);
            case ACTING -> act(mc);
            case VERIFYING -> verify(mc);
        }
    }

    void cancel(String code, String message) {
        if (!isActive()) return;
        Minecraft mc = Minecraft.getInstance();
        if (mc.gameMode != null && kind == Kind.BREAK) mc.gameMode.stopDestroyBlock();
        if (Pathfinder.isOwnedBy(operationId)) Pathfinder.stop();
        finish("cancelled", code, message);
    }

    private void aim(Minecraft mc) {
        if (!isFresh(mc)) { fail("STALE_TARGET", "block changed before action"); return; }
        lookAt(mc, Vec3.atCenterOf(targetPos));
        if (phaseTicks < 2) return;
        BlockHitResult hit = freshHit(mc, targetPos);
        if (hit == null || requestedFace != null && hit.getDirection() != requestedFace) {
            if (phaseTicks > 30) fail("TARGET_NOT_VISIBLE", "requested block face is not visible");
            return;
        }
        enter(Phase.ACTING);
    }

    private void act(Minecraft mc) {
        if (!isFresh(mc)) { fail("STALE_TARGET", "block changed before action dispatch"); return; }
        BlockHitResult hit = freshHit(mc, targetPos);
        if (hit == null || requestedFace != null && hit.getDirection() != requestedFace) {
            fail("TARGET_NOT_VISIBLE", "requested block face is not visible");
            return;
        }
        switch (kind) {
            case BREAK -> {
                mc.gameMode.startDestroyBlock(targetPos, hit.getDirection());
                mc.player.swing(InteractionHand.MAIN_HAND);
            }
            case USE -> mc.gameMode.useItemOn(mc.player, InteractionHand.MAIN_HAND, hit);
            case PLACE -> {
                if (!targetPos.relative(hit.getDirection()).equals(placedPos)) {
                    fail("INVALID_PLACEMENT_FACE", "clicked face does not lead to requested placement position");
                    return;
                }
                if (!EquipmentManager.ensureItemInHotbar(mc, itemId)) {
                    fail("ITEM_MISSING", "placement item is unavailable");
                    return;
                }
                EquipmentManager.selectHotbarSlot(mc, EquipmentManager.itemHotbarSlot(mc, itemId));
                itemBaseline = countItem(mc, itemId);
                mc.gameMode.useItemOn(mc.player, InteractionHand.MAIN_HAND, hit);
            }
        }
        enter(Phase.VERIFYING);
    }

    private void verify(Minecraft mc) {
        BlockState state = mc.level.getBlockState(targetPos);
        switch (kind) {
            case BREAK -> {
                if (!targetToken.equals(BlockSnapshotToken.create(mc.level, targetPos, state))) {
                    if (targetBlockId.equals(blockId(state))) {
                        fail("TARGET_REPLACED", "target state changed without confirmed removal");
                    } else {
                        mc.gameMode.stopDestroyBlock();
                        succeed("BLOCK_CHANGED", "server confirmed target block changed");
                    }
                    return;
                }
                BlockHitResult hit = freshHit(mc, targetPos);
                if (hit != null) mc.gameMode.continueDestroyBlock(targetPos, hit.getDirection());
                if (phaseTicks > 120) fail("BREAK_NOT_CONFIRMED", "server did not confirm block break");
            }
            case USE -> {
                boolean stateChanged = !targetToken.equals(BlockSnapshotToken.create(mc.level, targetPos, state));
                boolean menuChanged = mc.player.containerMenu.containerId != menuBaseline;
                if (stateChanged || menuChanged) succeed("USE_CONFIRMED", "server confirmed block interaction");
                else if (phaseTicks > 40) fail("USE_NOT_CONFIRMED", "interaction produced no confirmed state or menu change");
            }
            case PLACE -> {
                BlockState placedState = placedPos == null ? null : mc.level.getBlockState(placedPos);
                boolean placed = placedState != null && !placedState.isAir()
                    && BuiltInRegistries.ITEM.getKey(placedState.getBlock().asItem()).toString().equals(itemId);
                boolean consumed = mc.player.isCreative()
                    || itemBaseline > 0 && countItem(mc, itemId) < itemBaseline;
                if (placed && consumed) succeed("PLACE_CONFIRMED", "server confirmed block placement");
                else if (phaseTicks > 40) fail("PLACE_NOT_CONFIRMED", "server did not confirm placement and item consumption");
            }
        }
    }

    private boolean isFresh(Minecraft mc) {
        return mc.level.hasChunkAt(targetPos)
            && targetToken.equals(BlockSnapshotToken.create(mc.level, targetPos, mc.level.getBlockState(targetPos)));
    }

    private void succeed(String code, String message) { finish("succeeded", code, message); }

    private void fail(String code, String message) {
        Minecraft mc = Minecraft.getInstance();
        if (mc.gameMode != null && kind == Kind.BREAK) mc.gameMode.stopDestroyBlock();
        if (Pathfinder.isOwnedBy(operationId)) Pathfinder.stop();
        finish("failed", code, message);
    }

    private void finish(String status, String code, String message) {
        String id = operationId;
        operationId = null;
        kind = null;
        phase = null;
        targetPos = null;
        placedPos = null;
        interactionPos = null;
        targetToken = null;
        targetBlockId = null;
        itemId = null;
        requestedFace = null;
        if (id != null && LCUMod.WIRE != null) LCUMod.WIRE.sendOutcome(id, status, code, message);
    }

    private void enter(Phase next) { phase = next; phaseTicks = 0; }

    private static BlockHitResult freshHit(Minecraft mc, BlockPos target) {
        HitResult result = mc.player.pick(4.5, 1.0f, false);
        if (!(result instanceof BlockHitResult hit) || hit.getType() != HitResult.Type.BLOCK) return null;
        return hit.getBlockPos().equals(target) ? hit : null;
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

    private static Direction parseFace(JsonObject args) {
        if (!args.has("face")) return null;
        try { return Direction.valueOf(args.get("face").getAsString().toUpperCase(java.util.Locale.ROOT)); }
        catch (IllegalArgumentException exception) { return null; }
    }

    private static String blockId(BlockState state) {
        return BuiltInRegistries.BLOCK.getKey(state.getBlock()).toString();
    }

    private static int countItem(Minecraft mc, String expectedId) {
        int count = 0;
        for (int slot = 0; slot < 36; slot++) {
            var stack = mc.player.getInventory().getItem(slot);
            if (!stack.isEmpty() && BuiltInRegistries.ITEM.getKey(stack.getItem()).toString().equals(expectedId)) {
                count += stack.getCount();
            }
        }
        return count;
    }
}
