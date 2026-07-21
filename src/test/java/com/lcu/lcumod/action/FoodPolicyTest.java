package com.lcu.lcumod.action;

import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

class FoodPolicyTest {
    @Test
    void goldenApplesAreHealingFood() {
        assertTrue(FoodPolicy.isHealingFood("minecraft:golden_apple"));
        assertTrue(FoodPolicy.isHealingFood("minecraft:enchanted_golden_apple"));
        assertFalse(FoodPolicy.isHealingFood("minecraft:apple"));
    }
}
