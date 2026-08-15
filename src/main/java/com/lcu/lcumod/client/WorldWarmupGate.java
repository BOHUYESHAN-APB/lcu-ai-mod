package com.lcu.lcumod.client;

/** Pure lifecycle gate so a world cannot activate body mechanics before warm-up completes. */
final class WorldWarmupGate {
    private BodyRuntimePhase phase = BodyRuntimePhase.NO_WORLD;
    private int remainingTicks;

    BodyRuntimePhase phase() {
        return phase;
    }

    int remainingTicks() {
        return remainingTicks;
    }

    void begin(int warmupTicks) {
        remainingTicks = Math.max(0, warmupTicks);
        phase = remainingTicks == 0 ? BodyRuntimePhase.STARTING : BodyRuntimePhase.WARMING_UP;
    }

    boolean tick() {
        if (phase != BodyRuntimePhase.WARMING_UP) return false;
        if (--remainingTicks > 0) return false;
        remainingTicks = 0;
        phase = BodyRuntimePhase.STARTING;
        return true;
    }

    void readyManual() {
        if (phase == BodyRuntimePhase.STARTING) phase = BodyRuntimePhase.READY_MANUAL;
    }

    boolean arm() {
        if (phase != BodyRuntimePhase.READY_MANUAL) return false;
        phase = BodyRuntimePhase.ARMED;
        return true;
    }

    boolean disarm() {
        if (phase != BodyRuntimePhase.ARMED) return false;
        phase = BodyRuntimePhase.READY_MANUAL;
        return true;
    }

    void fail() {
        phase = BodyRuntimePhase.FAILED;
    }

    void reset() {
        remainingTicks = 0;
        phase = BodyRuntimePhase.NO_WORLD;
    }
}
