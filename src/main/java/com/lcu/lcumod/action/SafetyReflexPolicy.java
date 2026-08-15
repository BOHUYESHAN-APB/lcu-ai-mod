package com.lcu.lcumod.action;

final class SafetyReflexPolicy {
    private SafetyReflexPolicy() {}

    static boolean shouldMaintainAutoEat(boolean autoEatActive, boolean usingItem, boolean ownedFood) {
        return autoEatActive && usingItem && ownedFood;
    }
}
