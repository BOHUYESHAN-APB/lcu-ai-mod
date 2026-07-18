package com.lcu.lcumod.compat;

import com.google.gson.JsonArray;
import com.google.gson.JsonObject;
import com.lcu.lcumod.LCUMod;
import net.minecraft.core.registries.BuiltInRegistries;
import net.minecraft.world.entity.LivingEntity;
import net.minecraft.world.item.ItemStack;
import net.neoforged.fml.ModList;
import net.neoforged.neoforge.items.IItemHandler;

import java.lang.reflect.Method;
import java.util.Map;
import java.util.Optional;

public final class CuriosCompat {
    private static boolean initialized;
    private static boolean available;
    private static Method getCuriosInventory;
    private static Method getCurios;
    private static Method getStacks;

    private CuriosCompat() {}

    public static JsonArray collectEquipped(LivingEntity entity) {
        initialize();
        JsonArray result = new JsonArray();
        if (!available) return result;
        try {
            Optional<?> inventory = (Optional<?>) getCuriosInventory.invoke(null, entity);
            if (inventory.isEmpty()) return result;
            Map<?, ?> slots = (Map<?, ?>) getCurios.invoke(inventory.get());
            for (Map.Entry<?, ?> entry : slots.entrySet()) {
                Object stacksObject = getStacks.invoke(entry.getValue());
                if (!(stacksObject instanceof IItemHandler stacks)) continue;
                for (int index = 0; index < stacks.getSlots(); index++) {
                    ItemStack stack = stacks.getStackInSlot(index);
                    if (stack.isEmpty()) continue;
                    JsonObject item = new JsonObject();
                    item.addProperty("slot_type", String.valueOf(entry.getKey()));
                    item.addProperty("slot_index", index);
                    item.addProperty("name", BuiltInRegistries.ITEM.getKey(stack.getItem()).toString());
                    item.addProperty("count", stack.getCount());
                    item.addProperty("display", stack.getDisplayName().getString());
                    item.addProperty("damage", stack.getDamageValue());
                    item.addProperty("max_damage", stack.getMaxDamage());
                    result.add(item);
                }
            }
        } catch (ReflectiveOperationException | RuntimeException exception) {
            available = false;
            LCUMod.LOGGER.warn("[Compat/Curios] Equipment collection disabled: {}", exception.getMessage());
        }
        return result;
    }

    public static boolean isAvailable() {
        initialize();
        return available;
    }

    private static synchronized void initialize() {
        if (initialized) return;
        initialized = true;
        if (!ModList.get().isLoaded("curios")) return;
        try {
            Class<?> api = Class.forName("top.theillusivec4.curios.api.CuriosApi");
            Class<?> handler = Class.forName("top.theillusivec4.curios.api.type.capability.ICuriosItemHandler");
            Class<?> stacksHandler = Class.forName("top.theillusivec4.curios.api.type.inventory.ICurioStacksHandler");
            getCuriosInventory = api.getMethod("getCuriosInventory", LivingEntity.class);
            getCurios = handler.getMethod("getCurios");
            getStacks = stacksHandler.getMethod("getStacks");
            available = true;
            LCUMod.LOGGER.info("[Compat/Curios] Equipment collection enabled");
        } catch (ReflectiveOperationException | RuntimeException exception) {
            LCUMod.LOGGER.warn("[Compat/Curios] Compatible API unavailable: {}", exception.getMessage());
        }
    }
}
