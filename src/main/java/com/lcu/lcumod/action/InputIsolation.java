package com.lcu.lcumod.action;

import com.lcu.lcumod.LCUMod;
import net.minecraft.client.Minecraft;
import net.minecraft.client.Options;

/**
 * Input isolation system — prevents AI and user from fighting over controls.
 * 
 * KEY DESIGN:
 * 1. AI control: releaseMouse() — cursor visible, no camera control
 * 2. User control: grabMouse() — cursor hidden, camera control
 * 3. Switching requires F12 key press
 * 4. Manual takeover is explicit; focus changes never change ownership
 */
public class InputIsolation {

    // Control states
    private static boolean aiForward = false;
    private static boolean aiBack = false;
    private static boolean aiLeft = false;
    private static boolean aiRight = false;
    private static boolean aiJump = false;
    private static boolean aiSneak = false;
    private static boolean aiSprint = false;

    // Control mode
    private static boolean aiControlled = true;
    private static long lastUserActivity = 0;
    private static final long USER_TIMEOUT_MS = 10000;  // 10 seconds
    private static boolean autoReturnEnabled = false;

    // Mouse control
    private static float aiYaw = 0;
    private static float aiPitch = 0;
    private static boolean aiHasTarget = false;
    private static Boolean appliedMouseState = null;

    // ── Public API ──

    public static boolean isAiControlled() {
        return aiControlled;
    }

    /**
     * Toggle between AI and user control.
     * This is the ONLY way to switch control modes.
     */
    public static void toggleControl() {
        Minecraft mc = Minecraft.getInstance();
        
        if (aiControlled) {
            // Switch to USER control
            aiControlled = false;
            lastUserActivity = System.currentTimeMillis();
            clearAiControls();
            
            // Grab mouse — user gets camera control
            if (mc.mouseHandler != null) {
                mc.mouseHandler.grabMouse();
                appliedMouseState = Boolean.FALSE;
            }
            LCUMod.LOGGER.info("[Input] Switched to USER control (mouse grabbed)");
        } else {
            // Switch to AI control
            aiControlled = true;
            clearUserControls();
            
            // Release mouse — AI controls camera, cursor visible
            if (mc.mouseHandler != null) {
                mc.mouseHandler.releaseMouse();
                appliedMouseState = Boolean.TRUE;
            }
            LCUMod.LOGGER.info("[Input] Switched to AI control (mouse released)");
        }
    }

    /** Set ownership during world lifecycle changes without synthesizing a user takeover. */
    public static void setAiControlled(boolean enabled) {
        if (aiControlled == enabled) return;
        aiControlled = enabled;
        clearAiControls();
        if (enabled) {
            clearUserControls();
            releaseMouseWithoutFocus();
            appliedMouseState = Boolean.TRUE;
        } else {
            lastUserActivity = System.currentTimeMillis();
            appliedMouseState = Boolean.FALSE;
        }
    }

    private static void releaseMouseWithoutFocus() {
        Minecraft mc = Minecraft.getInstance();
        if (mc != null && mc.mouseHandler != null) mc.mouseHandler.releaseMouse();
    }

    /**
     * Set AI control state (like mineflayer's setControlState).
     */
    public static void setAiControlState(String control, boolean state) {
        if (!aiControlled) return;

        switch (control) {
            case "forward" -> aiForward = state;
            case "back" -> aiBack = state;
            case "left" -> aiLeft = state;
            case "right" -> aiRight = state;
            case "jump" -> aiJump = state;
            case "sneak" -> aiSneak = state;
            case "sprint" -> aiSprint = state;
        }
    }

    /**
     * Clear all AI control states.
     */
    public static void clearAiControls() {
        aiForward = false;
        aiBack = false;
        aiLeft = false;
        aiRight = false;
        aiJump = false;
        aiSneak = false;
        aiSprint = false;
        aiHasTarget = false;

        Minecraft mc = Minecraft.getInstance();
        if (mc != null && mc.options != null) {
            Options options = mc.options;
            options.keyUp.setDown(false);
            options.keyLeft.setDown(false);
            options.keyRight.setDown(false);
            options.keyDown.setDown(false);
            options.keyJump.setDown(false);
            options.keyShift.setDown(false);
            options.keySprint.setDown(false);
        }
    }

    /**
     * Clear all user control states.
     */
    public static void clearUserControls() {
        // User controls are handled by the game's input system
        // No action needed here
    }

    /**
     * Set AI look target.
     */
    public static void setAiLookTarget(float yaw, float pitch) {
        if (!aiControlled) return;
        aiYaw = yaw;
        aiPitch = pitch;
        aiHasTarget = true;
    }

    /**
     * Clear AI look target.
     */
    public static void clearAiLookTarget() {
        aiHasTarget = false;
    }

    /**
     * Record user activity (called when user presses any key).
     */
    public static void recordUserActivity() {
        if (!aiControlled) {
            lastUserActivity = System.currentTimeMillis();
        }
    }

    public static void ensureControlModeApplied(Minecraft mc) {
        if (mc.mouseHandler == null) return;
        if (aiControlled && !Boolean.TRUE.equals(appliedMouseState)) {
            mc.mouseHandler.releaseMouse();
            appliedMouseState = Boolean.TRUE;
        } else if (!aiControlled && appliedMouseState == null) {
            // Manual mode must never make an unfocused window active. F12 owns the
            // explicit transition and may capture the mouse when appropriate.
            appliedMouseState = Boolean.FALSE;
        }
    }

    /**
     * Check if user has timed out (for auto-return to AI).
     */
    public static boolean hasUserTimedOut() {
        if (aiControlled || !autoReturnEnabled) return false;
        return System.currentTimeMillis() - lastUserActivity > USER_TIMEOUT_MS;
    }

    /**
     * Enable/disable auto-return to AI after user inactivity.
     */
    public static void setAutoReturnEnabled(boolean enabled) {
        autoReturnEnabled = enabled;
    }

    // ── Tick Update ──

    /**
     * Called every tick to apply controls.
     */
    public static void tick(Minecraft mc) {
        if (mc.player == null) return;

        ensureControlModeApplied(mc);

        // Apply AI controls if AI is controlling
        if (aiControlled) {
            applyAiControls(mc);
        }
    }

    /**
     * Apply AI controls to the player.
     */
    private static void applyAiControls(Minecraft mc) {
        Options options = mc.options;

        // Set key states
        options.keyUp.setDown(aiForward);
        options.keyLeft.setDown(aiLeft);
        options.keyRight.setDown(aiRight);
        options.keyDown.setDown(aiBack);
        options.keyJump.setDown(aiJump);
        options.keyShift.setDown(aiSneak);
        options.keySprint.setDown(aiSprint);

        // Apply look target
        if (aiHasTarget) {
            mc.player.setYRot(aiYaw);
            mc.player.setXRot(aiPitch);
        }
    }

    // ── Status ──

    public static String getStatusString() {
        if (aiControlled) {
            return "AI Control";
        } else {
            long idleMs = System.currentTimeMillis() - lastUserActivity;
            long remainingMs = Math.max(0, USER_TIMEOUT_MS - idleMs);
            return String.format("User Control (%.1fs remaining)", remainingMs / 1000.0);
        }
    }

    public static boolean isAutoReturnEnabled() {
        return autoReturnEnabled;
    }
}
