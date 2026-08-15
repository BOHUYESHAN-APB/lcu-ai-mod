package com.lcu.lcumod.action;

import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

class SafetyReflexPolicyTest {
    @Test
    void activeAutoEatContinuesWhilePlayerIsUsingItem() {
        assertTrue(SafetyReflexPolicy.shouldMaintainAutoEat(true, true, true));
        assertFalse(SafetyReflexPolicy.shouldMaintainAutoEat(true, true, false));
        assertFalse(SafetyReflexPolicy.shouldMaintainAutoEat(true, false, true));
        assertFalse(SafetyReflexPolicy.shouldMaintainAutoEat(false, true, true));
    }
}
