package com.lcu.lcumod.action;

import net.minecraft.client.Minecraft;

import java.util.Random;

/**
 * Emits subtle player-like activity pulses while the bot is otherwise idle.
 *
 * Goal: avoid long completely-static periods that AFK / performance mods may
 * classify as inactive, without creating obviously fake wandering.
 */
public final class ActivitySignalController {

    private static final Random RANDOM = new Random();
    private static final int MIN_IDLE_BEFORE_PULSE = 320;   // 16s
    private static final int MIN_PULSE_GAP = 180;           // 9s
    private static final int MAX_PULSE_GAP = 360;           // 18s

    private static int idleTicks = 0;
    private static int nextPulseTicks = MIN_IDLE_BEFORE_PULSE;
    private static int strafePulseTicks = 0;
    private static int sneakPulseTicks = 0;
    private static String strafeDirection = "left";

    private ActivitySignalController() {}

    public static void tick(Minecraft mc, boolean busy) {
        if (mc.player == null || !InputIsolation.isAiControlled()) {
            reset();
            return;
        }

        if (busy) {
            resetTransientSignals();
            idleTicks = 0;
            return;
        }

        idleTicks++;
        applyTransientSignals();

        if (idleTicks < MIN_IDLE_BEFORE_PULSE || idleTicks < nextPulseTicks) {
            return;
        }

        emitPulse(mc);
        nextPulseTicks = idleTicks + MIN_PULSE_GAP + RANDOM.nextInt(MAX_PULSE_GAP - MIN_PULSE_GAP + 1);
    }

    private static void emitPulse(Minecraft mc) {
        if (mc.player == null) return;

        float yawDelta = (float) (RANDOM.nextDouble() * 12.0 - 6.0);
        float pitchDelta = (float) (RANDOM.nextDouble() * 4.0 - 2.0);
        mc.player.setYRot(mc.player.getYRot() + yawDelta);
        mc.player.setXRot(Math.max(-35.0f, Math.min(35.0f, mc.player.getXRot() + pitchDelta)));

        if (RANDOM.nextBoolean()) {
            strafeDirection = RANDOM.nextBoolean() ? "left" : "right";
            strafePulseTicks = 2 + RANDOM.nextInt(2);
        } else {
            sneakPulseTicks = 2;
        }
    }

    private static void applyTransientSignals() {
        if (strafePulseTicks > 0) {
            InputIsolation.setAiControlState(strafeDirection, true);
            strafePulseTicks--;
            if (strafePulseTicks == 0) {
                InputIsolation.setAiControlState("left", false);
                InputIsolation.setAiControlState("right", false);
            }
        }

        if (sneakPulseTicks > 0) {
            InputIsolation.setAiControlState("sneak", true);
            sneakPulseTicks--;
            if (sneakPulseTicks == 0) {
                InputIsolation.setAiControlState("sneak", false);
            }
        }
    }

    private static void resetTransientSignals() {
        strafePulseTicks = 0;
        sneakPulseTicks = 0;
        InputIsolation.setAiControlState("left", false);
        InputIsolation.setAiControlState("right", false);
        InputIsolation.setAiControlState("sneak", false);
    }

    public static void reset() {
        idleTicks = 0;
        nextPulseTicks = MIN_IDLE_BEFORE_PULSE;
        resetTransientSignals();
    }
}
