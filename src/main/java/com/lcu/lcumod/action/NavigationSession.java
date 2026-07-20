package com.lcu.lcumod.action;

/** Request ownership and bounded recovery state for one active navigation. */
final class NavigationSession {
    private static final int MIN_DEADLINE_TICKS = 200;
    private static final int MAX_DEADLINE_TICKS = 2400;
    private static final int MAX_REPATH_ATTEMPTS = 3;

    enum StartDecision {
        STARTED,
        RETARGETED,
        CONFLICT
    }

    private boolean active;
    private String requestId;
    private int deadlineTick;
    private int repathAttempts;

    StartDecision begin(String incomingRequestId, int currentTick, double distance) {
        if (active && requestId != null && !requestId.equals(incomingRequestId)) {
            return StartDecision.CONFLICT;
        }
        if (active && requestId == null && incomingRequestId == null) {
            return StartDecision.RETARGETED;
        }
        active = true;
        requestId = incomingRequestId;
        repathAttempts = 0;
        deadlineTick = currentTick + deadlineTicks(distance);
        return StartDecision.STARTED;
    }

    boolean consumeRepathAttempt() {
        if (repathAttempts >= MAX_REPATH_ATTEMPTS) {
            return false;
        }
        repathAttempts++;
        return true;
    }

    boolean isTimedOut(int currentTick) {
        return active && currentTick >= deadlineTick;
    }

    boolean hasOwnedOperation() {
        return active && requestId != null;
    }

    String requestId() {
        return requestId;
    }

    int repathAttempts() {
        return repathAttempts;
    }

    void clear() {
        active = false;
        requestId = null;
        deadlineTick = 0;
        repathAttempts = 0;
    }

    static int deadlineTicks(double distance) {
        int estimated = (int) Math.ceil(Math.max(0.0, distance) * 40.0) + 200;
        return Math.max(MIN_DEADLINE_TICKS, Math.min(MAX_DEADLINE_TICKS, estimated));
    }
}
