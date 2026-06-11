package com.lcu.lcumod.action;

import com.google.gson.JsonObject;
import com.lcu.lcumod.LCUMod;
import net.minecraft.core.BlockPos;
import net.minecraft.server.level.ServerPlayer;
import net.minecraft.world.Container;
import net.minecraft.world.entity.player.Inventory;
import net.minecraft.world.inventory.AbstractContainerMenu;
import net.minecraft.world.inventory.Slot;
import net.minecraft.world.item.ItemStack;

/**
 * Handles inventory manipulation: move items, equip, drop, container operations.
 */
public class InventoryManager {

    /**
     * Drop an item from a specific slot.
     * @param slot 0-35 for main inventory, 36-39 for armor, 40 for offhand
     */
    public static boolean dropItem(ServerPlayer player, int slot, int count) {
        Inventory inv = player.getInventory();
        ItemStack stack = inv.getItem(slot);
        if (stack.isEmpty()) return false;

        ItemStack dropped = stack.split(count);
        player.drop(dropped, false);
        inv.setItem(slot, stack.isEmpty() ? ItemStack.EMPTY : stack);
        return true;
    }

    /**
     * Move an item from one slot to another.
     * Handles stacking if target has same item type.
     */
    public static boolean moveItem(ServerPlayer player, int fromSlot, int toSlot) {
        Inventory inv = player.getInventory();
        ItemStack from = inv.getItem(fromSlot);
        ItemStack to = inv.getItem(toSlot);

        if (from.isEmpty()) return false;

        if (to.isEmpty()) {
            // Move directly
            inv.setItem(toSlot, from.copy());
            inv.setItem(fromSlot, ItemStack.EMPTY);
        } else if (ItemStack.isSameItemSameComponents(from, to) && to.getCount() < to.getMaxStackSize()) {
            // Stack
            int space = to.getMaxStackSize() - to.getCount();
            int transfer = Math.min(from.getCount(), space);
            to.grow(transfer);
            from.shrink(transfer);
            if (from.isEmpty()) {
                inv.setItem(fromSlot, ItemStack.EMPTY);
            }
        } else {
            // Swap
            inv.setItem(toSlot, from.copy());
            inv.setItem(fromSlot, to.copy());
        }
        return true;
    }

    /**
     * Equip an item from inventory to the appropriate equipment slot.
     * @param slot The inventory slot (0-35) containing the item
     */
    public static boolean equipItem(ServerPlayer player, int slot) {
        Inventory inv = player.getInventory();
        ItemStack stack = inv.getItem(slot);
        if (stack.isEmpty()) return false;

        // Determine target equipment slot
        int targetSlot = getEquipmentSlot(stack);
        if (targetSlot == -1) {
            // Not equippable — try offhand
            targetSlot = 40;
        }

        return moveItem(player, slot, targetSlot);
    }

    /**
     * Get the equipment slot for an item type.
     * Returns -1 if not equippable.
     */
    private static int getEquipmentSlot(ItemStack stack) {
        String id = stack.getDescriptionId().toLowerCase();

        // Armor detection by name pattern
        if (id.contains("helmet") || id.contains("cap") || id.contains("hood")) return 36 + 3;
        if (id.contains("chestplate") || id.contains("tunic") || id.contains("elytra")) return 36 + 2;
        if (id.contains("leggings") || id.contains("pants")) return 36 + 1;
        if (id.contains("boots") || id.contains("shoes")) return 36;

        return -1;
    }

    /**
     * Find a slot containing a specific item ID.
     * @param itemId The item ID to search for (e.g., "minecraft:diamond_pickaxe")
     * @return Slot index, or -1 if not found
     */
    public static int findItem(ServerPlayer player, String itemId) {
        Inventory inv = player.getInventory();
        for (int i = 0; i < inv.getContainerSize(); i++) {
            ItemStack stack = inv.getItem(i);
            if (!stack.isEmpty()) {
                String slotId = net.minecraft.core.registries.BuiltInRegistries
                        .ITEM.getKey(stack.getItem()).toString();
                if (slotId.equals(itemId)) {
                    return i;
                }
            }
        }
        return -1;
    }

    /**
     * Get the best tool slot for mining a specific block.
     * Returns the slot index of the best tool, or -1 if no tool found.
     */
    public static int findBestTool(ServerPlayer player, net.minecraft.world.level.block.state.BlockState blockState) {
        Inventory inv = player.getInventory();
        int bestSlot = -1;
        float bestSpeed = 0;

        for (int i = 0; i < 9; i++) { // Hotbar only
            ItemStack stack = inv.getItem(i);
            if (stack.isEmpty()) continue;

            float speed = stack.getDestroySpeed(blockState);
            if (speed > bestSpeed) {
                bestSpeed = speed;
                bestSlot = i;
            }
        }
        return bestSlot;
    }

    /**
     * Swap hotbar slot with an inventory slot.
     */
    public static boolean swapHotbar(ServerPlayer player, int invSlot, int hotbarSlot) {
        Inventory inv = player.getInventory();
        ItemStack invItem = inv.getItem(invSlot);
        ItemStack hotbarItem = inv.getItem(hotbarSlot);

        inv.setItem(hotbarSlot, invItem.copy());
        inv.setItem(invSlot, hotbarItem.copy());
        return true;
    }

    /**
     * Click on a slot in the currently open container menu.
     * Handles left click, right click, and shift click.
     */
    public static boolean clickContainerSlot(ServerPlayer player, int slotIndex, int button) {
        AbstractContainerMenu menu = player.containerMenu;
        if (menu == null || menu == player.inventoryMenu) return false;

        if (slotIndex < 0 || slotIndex >= menu.slots.size()) return false;

        // ClickType: 0=PICKUP, 1=QUICK_MOVE, 2=SWAP, etc.
        menu.clicked(slotIndex, button, net.minecraft.world.inventory.ClickType.PICKUP, player);
        return true;
    }

    /**
     * Shift-click a slot in the currently open container.
     */
    public static boolean shiftClickSlot(ServerPlayer player, int slotIndex) {
        AbstractContainerMenu menu = player.containerMenu;
        if (menu == null || menu == player.inventoryMenu) return false;

        if (slotIndex < 0 || slotIndex >= menu.slots.size()) return false;

        menu.clicked(slotIndex, 0, net.minecraft.world.inventory.ClickType.QUICK_MOVE, player);
        return true;
    }

    /**
     * Get the currently open container's slots as JSON.
     */
    public static JsonObject getContainerContents(ServerPlayer player) {
        AbstractContainerMenu menu = player.containerMenu;
        JsonObject result = new JsonObject();

        if (menu == null || menu == player.inventoryMenu) {
            result.addProperty("open", false);
            return result;
        }

        result.addProperty("open", true);
        result.addProperty("type", menu.getClass().getSimpleName());
        result.addProperty("slot_count", menu.slots.size());

        com.google.gson.JsonArray slots = new com.google.gson.JsonArray();
        for (int i = 0; i < menu.slots.size(); i++) {
            Slot slot = menu.slots.get(i);
            ItemStack stack = slot.getItem();
            JsonObject slotObj = new JsonObject();
            slotObj.addProperty("index", i);
            slotObj.addProperty("id", stack.isEmpty() ? "air" :
                    net.minecraft.core.registries.BuiltInRegistries.ITEM.getKey(stack.getItem()).toString());
            slotObj.addProperty("count", stack.getCount());
            slots.add(slotObj);
        }
        result.add("slots", slots);

        return result;
    }
}
