package com.lcu.lcumod.action;

import net.minecraft.client.Minecraft;
import net.minecraft.core.registries.BuiltInRegistries;
import net.minecraft.world.entity.EquipmentSlot;
import net.minecraft.world.entity.ai.attributes.Attributes;
import net.minecraft.world.entity.monster.Enemy;
import net.minecraft.world.item.ArmorItem;
import net.minecraft.world.item.ItemStack;
import net.minecraft.world.inventory.ClickType;
import net.minecraft.network.protocol.game.ServerboundSetCarriedItemPacket;
import com.lcu.lcumod.config.ServerPolicy;

/** Local, server-synchronised equipment selection for the headed body. */
public final class EquipmentManager {
    private static int cooldown;

    private EquipmentManager() {}

    public static void tick(Minecraft mc) {
        if (mc.player == null || mc.level == null || mc.gameMode == null
                || !InputIsolation.isAiControlled() || mc.player.isDeadOrDying()
                || !ServerPolicy.inventoryAutomationAllowed()) return;
        if (mc.player.containerMenu != mc.player.inventoryMenu || mc.player.isUsingItem()) return;
        if (cooldown > 0) {
            cooldown--;
            return;
        }
        cooldown = 20;

        equipBestArmor(mc);
        if (hasNearbyHostile(mc) || !isUsefulWeapon(mc.player.getMainHandItem())) {
            selectBestWeapon(mc);
        }
    }

    /** Move a food stack into the hotbar using a vanilla SWAP transaction. */
    public static boolean ensureFoodInHotbar(Minecraft mc) {
        if (mc.player == null || mc.gameMode == null || mc.player.containerMenu != mc.player.inventoryMenu) return false;
        int bestSlot = bestFoodSlot(mc);
        return moveInventorySlotToHotbar(mc, bestSlot);
    }

    public static boolean ensureHealingFoodInHotbar(Minecraft mc) {
        if (mc.player == null || mc.gameMode == null || mc.player.containerMenu != mc.player.inventoryMenu) return false;
        int bestSlot = -1;
        for (int slot = 0; slot < 36; slot++) {
            if (isHealingFood(mc.player.getInventory().getItem(slot))) {
                bestSlot = slot;
                break;
            }
        }
        return moveInventorySlotToHotbar(mc, bestSlot);
    }

    public static boolean ensureItemInHotbar(Minecraft mc, String itemId) {
        if (mc.player == null || mc.gameMode == null || mc.player.containerMenu != mc.player.inventoryMenu) return false;
        for (int slot = 0; slot < 36; slot++) {
            ItemStack stack = mc.player.getInventory().getItem(slot);
            if (!stack.isEmpty() && BuiltInRegistries.ITEM.getKey(stack.getItem()).toString().equals(itemId)) {
                if (slot < 9) return true;
                int hotbarSlot = findFoodDestination(mc);
                mc.gameMode.handleInventoryMouseClick(
                    mc.player.inventoryMenu.containerId, slot, hotbarSlot, ClickType.SWAP, mc.player
                );
                ItemStack moved = mc.player.getInventory().getItem(hotbarSlot);
                return !moved.isEmpty() && BuiltInRegistries.ITEM.getKey(moved.getItem()).toString().equals(itemId);
            }
        }
        return false;
    }

    public static boolean hasItem(Minecraft mc, String itemId) {
        if (mc.player == null) return false;
        for (int slot = 0; slot < 36; slot++) {
            ItemStack stack = mc.player.getInventory().getItem(slot);
            if (!stack.isEmpty() && BuiltInRegistries.ITEM.getKey(stack.getItem()).toString().equals(itemId)) return true;
        }
        return false;
    }

    public static int itemHotbarSlot(Minecraft mc, String itemId) {
        if (mc.player == null) return -1;
        for (int slot = 0; slot < 9; slot++) {
            ItemStack stack = mc.player.getInventory().getItem(slot);
            if (!stack.isEmpty() && BuiltInRegistries.ITEM.getKey(stack.getItem()).toString().equals(itemId)) return slot;
        }
        return -1;
    }

    private static boolean moveInventorySlotToHotbar(Minecraft mc, int bestSlot) {
        if (bestSlot < 0) return false;
        if (bestSlot < 9) return true;
        for (int inventorySlot = 9; inventorySlot < 36; inventorySlot++) {
            if (inventorySlot != bestSlot) continue;
            int hotbarSlot = findFoodDestination(mc);
            int menuSlot = inventorySlot;
            mc.gameMode.handleInventoryMouseClick(
                mc.player.inventoryMenu.containerId, menuSlot, hotbarSlot, ClickType.SWAP, mc.player
            );
            return isFood(mc.player.getInventory().getItem(hotbarSlot), mc);
        }
        return false;
    }

    public static int healingFoodHotbarSlot(Minecraft mc) {
        for (int slot = 0; slot < 9; slot++) {
            if (isHealingFood(mc.player.getInventory().getItem(slot))) return slot;
        }
        return -1;
    }

    static boolean isHealingFood(ItemStack stack) {
        return !stack.isEmpty()
            && FoodPolicy.isHealingFood(BuiltInRegistries.ITEM.getKey(stack.getItem()).toString());
    }

    public static void selectHotbarSlot(Minecraft mc, int slot) {
        if (mc.player == null || slot < 0 || slot >= 9) return;
        mc.player.getInventory().selected = slot;
        if (mc.getConnection() != null) {
            mc.getConnection().send(new ServerboundSetCarriedItemPacket(slot));
        }
    }

    public static int bestFoodHotbarSlot(Minecraft mc) {
        int best = -1;
        float score = Float.NEGATIVE_INFINITY;
        for (int slot = 0; slot < 9; slot++) {
            float candidate = foodScore(mc.player.getInventory().getItem(slot), mc);
            if (candidate > score) {
                score = candidate;
                best = candidate > 0 ? slot : -1;
            }
        }
        return best;
    }

    private static int bestFoodSlot(Minecraft mc) {
        int best = -1;
        float score = Float.NEGATIVE_INFINITY;
        for (int slot = 0; slot < 36; slot++) {
            float candidate = foodScore(mc.player.getInventory().getItem(slot), mc);
            if (candidate > score) {
                score = candidate;
                best = candidate > 0 ? slot : -1;
            }
        }
        return best;
    }

    private static void equipBestArmor(Minecraft mc) {
        for (EquipmentSlot equipmentSlot : new EquipmentSlot[] {
                EquipmentSlot.HEAD, EquipmentSlot.CHEST, EquipmentSlot.LEGS, EquipmentSlot.FEET
        }) {
            int source = findBestArmorSlot(mc, equipmentSlot);
            if (source < 0) continue;
            ItemStack current = mc.player.getItemBySlot(equipmentSlot);
            ItemStack candidate = mc.player.getInventory().getItem(source);
            if (armorScore(candidate) <= armorScore(current) + 0.25) continue;
            swapArmor(mc, source, equipmentSlot);
        }
    }

    private static int findBestArmorSlot(Minecraft mc, EquipmentSlot target) {
        int best = -1;
        float score = armorScore(mc.player.getItemBySlot(target));
        for (int slot = 0; slot < 36; slot++) {
            ItemStack stack = mc.player.getInventory().getItem(slot);
            if (!(stack.getItem() instanceof ArmorItem armor) || armor.getEquipmentSlot() != target) continue;
            float candidate = armorScore(stack);
            if (candidate > score) {
                score = candidate;
                best = slot;
            }
        }
        return best;
    }

    private static float armorScore(ItemStack stack) {
        if (!(stack.getItem() instanceof ArmorItem armor)) return 0.0f;
        float durability = stack.getMaxDamage() <= 0
            ? 1.0f
            : (stack.getMaxDamage() - stack.getDamageValue()) / (float) stack.getMaxDamage();
        return armor.getDefense() + armor.getToughness() * 0.25f + durability * 0.5f;
    }

    private static void swapArmor(Minecraft mc, int inventorySlot, EquipmentSlot target) {
        int armorMenuSlot = switch (target) {
            case HEAD -> 5;
            case CHEST -> 6;
            case LEGS -> 7;
            case FEET -> 8;
            default -> -1;
        };
        if (armorMenuSlot < 0) return;
        int sourceMenuSlot = inventorySlot < 9 ? 36 + inventorySlot : inventorySlot;
        int menuId = mc.player.inventoryMenu.containerId;
        mc.gameMode.handleInventoryMouseClick(menuId, sourceMenuSlot, 0, ClickType.PICKUP, mc.player);
        mc.gameMode.handleInventoryMouseClick(menuId, armorMenuSlot, 0, ClickType.PICKUP, mc.player);
        if (!mc.player.inventoryMenu.getCarried().isEmpty()) {
            mc.gameMode.handleInventoryMouseClick(menuId, sourceMenuSlot, 0, ClickType.PICKUP, mc.player);
        }
    }

    private static void selectBestWeapon(Minecraft mc) {
        int bestSlot = -1;
        float bestScore = isUsefulWeapon(mc.player.getMainHandItem()) ? weaponScore(mc.player.getMainHandItem()) : 0;
        for (int slot = 0; slot < 9; slot++) {
            ItemStack stack = mc.player.getInventory().getItem(slot);
            float score = weaponScore(stack);
            if (score > bestScore) {
                bestScore = score;
                bestSlot = slot;
            }
        }
        if (bestSlot >= 0) selectHotbarSlot(mc, bestSlot);
    }

    private static boolean hasNearbyHostile(Minecraft mc) {
        return mc.level.getEntities(mc.player, mc.player.getBoundingBox().inflate(10)).stream()
            .anyMatch(entity -> entity instanceof Enemy && entity.isAlive());
    }

    private static boolean isUsefulWeapon(ItemStack stack) {
        return weaponScore(stack) > 0;
    }

    private static float weaponScore(ItemStack stack) {
        if (stack.isEmpty() || (stack.getMaxDamage() > 0 && stack.getDamageValue() >= stack.getMaxDamage() - 1)) return 0;
        double attackDamage = stack.getAttributeModifiers().modifiers().stream()
            .filter(entry -> entry.attribute().equals(Attributes.ATTACK_DAMAGE))
            .mapToDouble(entry -> entry.modifier().amount())
            .sum();
        String id = BuiltInRegistries.ITEM.getKey(stack.getItem()).toString();
        float material = id.contains("netherite") ? 5 : id.contains("diamond") ? 4 : id.contains("iron") ? 3
            : id.contains("golden") ? 2 : id.contains("stone") ? 1.5f : 1;
        if (attackDamage > 0) return (float) (100 + attackDamage * 10 + material);
        if (id.endsWith("_sword")) return 100 + material;
        if (id.endsWith("_axe")) return 80 + material;
        if (id.endsWith("_trident")) return 90 + material;
        if (id.endsWith("_bow") || id.endsWith("_crossbow")) return 70 + material;
        return 0;
    }

    private static int findFoodDestination(Minecraft mc) {
        for (int slot = 0; slot < 9; slot++) {
            if (mc.player.getInventory().getItem(slot).isEmpty()) return slot;
        }
        return 0;
    }

    private static boolean isFood(ItemStack stack, Minecraft mc) {
        return !stack.isEmpty() && stack.getItem().getFoodProperties(stack, mc.player) != null;
    }

    private static float foodScore(ItemStack stack, Minecraft mc) {
        if (!isFood(stack, mc)) return 0;
        var food = stack.getItem().getFoodProperties(stack, mc.player);
        String id = BuiltInRegistries.ITEM.getKey(stack.getItem()).toString();
        boolean famine = mc.player.getFoodData().getFoodLevel() <= 5;
        if (!famine && java.util.Set.of(
                "minecraft:rotten_flesh", "minecraft:spider_eye", "minecraft:poisonous_potato",
                "minecraft:pufferfish", "minecraft:chicken"
        ).contains(id)) return -100;
        return food.nutrition() * 4.0f + food.saturation() * 2.0f;
    }
}
