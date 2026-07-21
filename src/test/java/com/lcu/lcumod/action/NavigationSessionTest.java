package com.lcu.lcumod.action;

import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

class NavigationSessionTest {
    @Test
    void ownedNavigationRejectsInternalAndDifferentRequestReplacement() {
        NavigationSession session = new NavigationSession();

        assertEquals(NavigationSession.StartDecision.STARTED, session.begin("move-1", 100, 10));
        assertEquals(NavigationSession.StartDecision.CONFLICT, session.begin(null, 101, 12));
        assertEquals(NavigationSession.StartDecision.CONFLICT, session.begin("move-2", 101, 12));
        assertTrue(session.hasOwnedOperation());
        assertEquals("move-1", session.requestId());
    }

    @Test
    void internalRetargetPreservesRecoveryBudget() {
        NavigationSession session = new NavigationSession();

        assertEquals(NavigationSession.StartDecision.STARTED, session.begin(null, 0, 5));
        assertTrue(session.consumeRepathAttempt());
        assertEquals(NavigationSession.StartDecision.RETARGETED, session.begin(null, 10, 6));
        assertEquals(1, session.repathAttempts());
    }

    @Test
    void childNavigationRetargetsForSameOwnerWithoutReportingLifecycle() {
        NavigationSession session = new NavigationSession();

        assertEquals(NavigationSession.StartDecision.STARTED, session.begin("farm-1", 0, 5, false));
        assertFalse(session.reportsLifecycle());
        assertEquals(NavigationSession.StartDecision.RETARGETED, session.begin("farm-1", 10, 7, false));
        assertEquals(NavigationSession.StartDecision.CONFLICT, session.begin("move-2", 11, 2, true));
    }

    @Test
    void suspensionExtendsDeadlineByPausedTicks() {
        NavigationSession session = new NavigationSession();
        session.begin("move-1", 0, 1);

        session.suspend(100);
        assertFalse(session.isTimedOut(500));
        session.resume(500);

        assertFalse(session.isTimedOut(639));
        assertTrue(session.isTimedOut(640));
    }

    @Test
    void recoveryAndDeadlineAreBounded() {
        NavigationSession session = new NavigationSession();
        session.begin("move-1", 20, 1);

        assertTrue(session.consumeRepathAttempt());
        assertTrue(session.consumeRepathAttempt());
        assertTrue(session.consumeRepathAttempt());
        assertFalse(session.consumeRepathAttempt());
        assertFalse(session.isTimedOut(259));
        assertTrue(session.isTimedOut(260));
        assertEquals(2400, NavigationSession.deadlineTicks(1000));
    }

    @Test
    void clearingOwnershipAllowsNextOperation() {
        NavigationSession session = new NavigationSession();
        session.begin("move-1", 0, 2);

        session.clear();

        assertFalse(session.hasOwnedOperation());
        assertEquals(NavigationSession.StartDecision.STARTED, session.begin("move-2", 10, 2));
    }
}
