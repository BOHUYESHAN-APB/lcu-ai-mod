package com.lcu.lcumod.action;

import java.util.Map;

/** Pure crop semantics for the first verified vanilla farming slice. */
final class CropPolicy {
    private static final Map<String, CropDefinition> CROPS = Map.of(
        "minecraft:wheat", new CropDefinition(7, "minecraft:wheat_seeds"),
        "minecraft:carrots", new CropDefinition(7, "minecraft:carrot"),
        "minecraft:potatoes", new CropDefinition(7, "minecraft:potato"),
        "minecraft:beetroots", new CropDefinition(3, "minecraft:beetroot_seeds")
    );

    private CropPolicy() {}

    static boolean isSupported(String blockId) {
        return CROPS.containsKey(blockId);
    }

    static CropState inspect(String blockId, Integer age) {
        CropDefinition definition = CROPS.get(blockId);
        if (definition == null || age == null) {
            return new CropState(false, false, age, -1, "");
        }
        return new CropState(true, age >= definition.maxAge(), age, definition.maxAge(), definition.seedItemId());
    }

    record CropDefinition(int maxAge, String seedItemId) {}
    record CropState(boolean supported, boolean mature, Integer age, int maxAge, String seedItemId) {}
}
