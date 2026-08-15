package com.lcu.lcumod.client;

import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

class WorldWarmupGateTest {
    @Test
    void gateRequiresEveryWarmupTickBeforeManualReadiness() {
        WorldWarmupGate gate = new WorldWarmupGate();
        gate.begin(3);

        assertEquals(BodyRuntimePhase.WARMING_UP, gate.phase());
        assertFalse(gate.tick());
        assertFalse(gate.tick());
        assertTrue(gate.tick());
        assertEquals(BodyRuntimePhase.STARTING, gate.phase());
        gate.readyManual();
        assertEquals(BodyRuntimePhase.READY_MANUAL, gate.phase());
    }

    @Test
    void armAndDisarmRequireReadyLifecycleStates() {
        WorldWarmupGate gate = new WorldWarmupGate();
        assertFalse(gate.arm());
        gate.begin(0);
        gate.readyManual();

        assertTrue(gate.arm());
        assertEquals(BodyRuntimePhase.ARMED, gate.phase());
        assertTrue(gate.disarm());
        assertEquals(BodyRuntimePhase.READY_MANUAL, gate.phase());
    }

    @Test
    void resetInvalidatesAQueuedWarmup() {
        WorldWarmupGate gate = new WorldWarmupGate();
        gate.begin(600);
        gate.reset();

        assertEquals(BodyRuntimePhase.NO_WORLD, gate.phase());
        assertEquals(0, gate.remainingTicks());
    }
}
