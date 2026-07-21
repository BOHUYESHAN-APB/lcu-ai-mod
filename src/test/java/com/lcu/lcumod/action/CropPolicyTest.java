package com.lcu.lcumod.action;

import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

class CropPolicyTest {
    @Test
    void identifiesMatureVanillaCropsAndSeeds() {
        CropPolicy.CropState wheat = CropPolicy.inspect("minecraft:wheat", 7);
        CropPolicy.CropState beetroot = CropPolicy.inspect("minecraft:beetroots", 2);

        assertTrue(wheat.supported());
        assertTrue(wheat.mature());
        assertEquals("minecraft:wheat_seeds", wheat.seedItemId());
        assertTrue(beetroot.supported());
        assertFalse(beetroot.mature());
        assertEquals(3, beetroot.maxAge());
    }

    @Test
    void unsupportedBlocksRemainReadOnlyObservations() {
        CropPolicy.CropState state = CropPolicy.inspect("example:modded_crop", 7);

        assertFalse(state.supported());
        assertFalse(state.mature());
        assertFalse(CropPolicy.isSupported("example:modded_crop"));
    }
}
