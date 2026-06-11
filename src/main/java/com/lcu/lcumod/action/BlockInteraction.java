package com.lcu.lcumod.action;

import com.google.gson.JsonObject;
import com.lcu.lcumod.LCUMod;
import net.minecraft.core.BlockPos;
import net.minecraft.core.Direction;
import net.minecraft.server.level.ServerLevel;
import net.minecraft.server.level.ServerPlayer;
import net.minecraft.world.InteractionHand;
import net.minecraft.world.item.ItemStack;
import net.minecraft.world.level.block.state.BlockState;
import net.minecraft.world.phys.BlockHitResult;
import net.minecraft.world.phys.Vec3;

/**
 * Handles block interaction: breaking (with timing), placing, and using blocks.
 */
public class BlockInteraction {

    /**
     * Break a block with proper timing based on tool and block hardness.
     * Returns the estimated ticks needed to break the block.
     */
    public static int getBreakTime(ServerPlayer player, BlockPos pos) {
        ServerLevel level = player.serverLevel();
        BlockState state = level.getBlockState(pos);

        if (state.isAir()) return 0;

        float hardness = state.getDestroySpeed(level, pos);
        if (hardness < 0) return -1; // Unbreakable (bedrock, etc.)

        // Get the player's tool speed for this block
        ItemStack tool = player.getMainHandItem();
        float toolSpeed = tool.getDestroySpeed(state);

        // Minecraft break time formula
        // ticks = ceil(1.5 * hardness / toolSpeed) if can harvest
        // ticks = ceil(3.0 * hardness / toolSpeed) if can't harvest
        boolean canHarvest = state.requiresCorrectToolForDrops() ?
                tool.isCorrectToolForDrops(state) : true;

        float multiplier = canHarvest ? 1.5f : 3.0f;
        int ticks = (int) Math.ceil(multiplier * hardness / toolSpeed);

        return Math.max(ticks, 1);
    }

    /**
     * Auto-select the best tool for a block and switch to it.
     * Returns the slot switched to, or -1 if no tool found.
     */
    public static int autoSelectTool(ServerPlayer player, BlockPos pos) {
        ServerLevel level = player.serverLevel();
        BlockState state = level.getBlockState(pos);

        int bestSlot = InventoryManager.findBestTool(player, state);
        if (bestSlot >= 0 && bestSlot != player.getInventory().selected) {
            player.getInventory().selected = bestSlot;
        }
        return bestSlot;
    }

    /**
     * Place a block from the player's hand at a target position.
     * Determines the best face to place against.
     */
    public static boolean placeBlock(ServerPlayer player, BlockPos target, ItemStack stack) {
        if (stack.isEmpty()) return false;

        ServerLevel level = player.serverLevel();
        BlockState targetState = level.getBlockState(target);

        // The block to place on needs an adjacent face
        // Find the best face to click on
        Direction bestFace = null;
        BlockPos bestPos = null;

        // Check each face of the target position
        for (Direction face : Direction.values()) {
            BlockPos adjacent = target.relative(face);
            BlockState adjState = level.getBlockState(adjacent);

            // Adjacent block must be solid (to place against)
            if (adjState.isSolid()) {
                bestFace = face.getOpposite();
                bestPos = adjacent;
                break;
            }
        }

        if (bestFace == null || bestPos == null) {
            // Can't find a solid block to place against
            // Try placing on top of a solid block below
            BlockPos below = target.below();
            if (level.getBlockState(below).isSolid()) {
                bestFace = Direction.UP;
                bestPos = below;
            } else {
                return false;
            }
        }

        // Calculate hit position (center of the face)
        Vec3 hitVec = Vec3.atCenterOf(bestPos)
                .relative(bestFace, 0.5);

        BlockHitResult hitResult = new BlockHitResult(
                hitVec, bestFace, bestPos, false
        );

        // Use the item to place the block
        var result = player.gameMode.useItemOn(
                player, level, stack, InteractionHand.MAIN_HAND, hitResult
        );

        return result.consumesAction();
    }

    /**
     * Right-click on a block to open it (chest, furnace, etc.).
     */
    public static boolean interactWithBlock(ServerPlayer player, BlockPos pos) {
        ServerLevel level = player.serverLevel();
        BlockState state = level.getBlockState(pos);

        // Calculate hit position (center of block)
        Vec3 hitVec = Vec3.atCenterOf(pos);
        BlockHitResult hitResult = new BlockHitResult(
                hitVec, Direction.UP, pos, false
        );

        // Use item on block
        var result = state.useItemOn(
                player.getMainHandItem(), level, player,
                InteractionHand.MAIN_HAND, hitResult
        );

        if (result.consumesAction()) return true;

        // Fallback: try block interaction directly
        player.swing(InteractionHand.MAIN_HAND);
        return false;
    }
}
