package com.lcu.lcumod.action;

/** Pure item classification shared by runtime food selection and unit tests. */
final class FoodPolicy {
    private FoodPolicy() {}

    static boolean isHealingFood(String itemId) {
        return "minecraft:golden_apple".equals(itemId)
            || "minecraft:enchanted_golden_apple".equals(itemId);
    }
}
