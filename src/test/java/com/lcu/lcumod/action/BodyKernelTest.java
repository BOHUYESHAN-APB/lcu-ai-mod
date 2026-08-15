package com.lcu.lcumod.action;

import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

class BodyKernelTest {
    @BeforeEach
    void resetTick() {
        BodyKernel.beginTick();
    }

    @Test
    void bundleClaimIsAtomic() {
        assertTrue(BodyKernel.claim("navigation", BodyPriority.FOREGROUND,
            BodyChannel.LOCOMOTION, BodyChannel.GAZE));
        assertFalse(BodyKernel.claim("idle", BodyPriority.IDLE,
            BodyChannel.GAZE, BodyChannel.MAIN_HAND));
        assertEquals(null, BodyKernel.ownerOf(BodyChannel.MAIN_HAND));
    }

    @Test
    void higherPriorityOwnerCanPreemptLowerPriorityClaim() {
        assertTrue(BodyKernel.claim("autonomy", BodyPriority.AUTONOMY, BodyChannel.GAZE));
        assertTrue(BodyKernel.claim("safety", BodyPriority.SAFETY, BodyChannel.GAZE));
        assertEquals("safety", BodyKernel.ownerOf(BodyChannel.GAZE));
    }

    @Test
    void equalPriorityOwnersCannotOverwriteEachOther() {
        assertTrue(BodyKernel.claim("operation-a", BodyPriority.FOREGROUND, BodyChannel.MAIN_HAND));
        assertFalse(BodyKernel.claim("operation-b", BodyPriority.FOREGROUND, BodyChannel.MAIN_HAND));
        assertEquals("operation-a", BodyKernel.ownerOf(BodyChannel.MAIN_HAND));
    }

    @Test
    void claimsExpireAtTickBoundary() {
        assertTrue(BodyKernel.claim("navigation", BodyPriority.FOREGROUND, BodyChannel.LOCOMOTION));
        BodyKernel.beginTick();
        assertTrue(BodyKernel.claim("autonomy", BodyPriority.AUTONOMY, BodyChannel.LOCOMOTION));
    }
}
