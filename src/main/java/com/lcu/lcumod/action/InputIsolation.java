package com.lcu.lcumod.action;

import com.lcu.lcumod.LCUMod;
import net.minecraft.client.Minecraft;
import net.minecraft.client.Options;

/**
 * Input isolation system — prevents AI and user from fighting over controls.
 *
 * KEY DESIGN:
 * 1. The cursor follows one rule: while AI control is active the pointer is RELEASED, so the human
 *    can use the desktop / other windows while the AI plays. Switching to user control (F12) grabs
 *    it back, exactly like clicking into the window in vanilla. The release side is re-asserted each
 *    tick (so entering a world hands the pointer over), the grab side happens ONLY on an explicit
 *    switch — this class never grabs the pointer out from under the human.
 * 2. While AI control is active, AI key states are written into the client key bindings — but only
 *    while the AI actually holds a key, plus one release when it lets go. An idle AI never writes
 *    "all keys up", which would clobber a human playing on the same client instance.
 * 3. Switching is manual (F12 / command) — the AI never takes control on its own.
 * 4. The auto-return-to-AI-after-inactivity path exists but is OFF by default
 *    (see setAutoReturnEnabled), so a human is never hijacked mid-play.
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
    /** True while AI key states are written down and not yet released. */
    private static boolean aiKeysDirty = false;

    // Control mode
    private static boolean aiControlled = true;
    private static long lastUserActivity = 0;
    private static final long USER_TIMEOUT_MS = 10000;  // 10 seconds
    private static boolean autoReturnEnabled = false;  // OFF by default: never hijack control back

    // Look target (applied by writing yaw/pitch — the cursor is never grabbed)
    private static float aiYaw = 0;
    private static float aiPitch = 0;
    private static boolean aiHasTarget = false;

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
            // → USER control: grab the cursor so the human can look around, exactly like
            // clicking into the window does in vanilla.
            aiControlled = false;
            lastUserActivity = System.currentTimeMillis();
            clearAiControls();
            if (mc.mouseHandler != null) {
                mc.mouseHandler.grabMouse();
            }
            LCUMod.LOGGER.info("[Input] Switched to USER control (cursor grabbed)");
        } else {
            // → AI control: hand the pointer back to the human so they can use the desktop while
            // the AI plays. This is the whole point of pressing F12.
            aiControlled = true;
            clearUserControls();
            if (mc.mouseHandler != null) {
                mc.mouseHandler.releaseMouse();
            }
            LCUMod.LOGGER.info("[Input] Switched to AI control (cursor released)");
        }
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
        aiKeysDirty = false;
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

        // AI control owns the keyboard, but the human keeps the pointer: while the AI plays we keep
        // the cursor released so they can use the desktop / other windows. Only the release side is
        // re-asserted (idempotent, guarded by isMouseGrabbed) — grabbing happens ONLY on an explicit
        // F12 switch to user control, never automatically.
        if (aiControlled && mc.mouseHandler != null && mc.mouseHandler.isMouseGrabbed()) {
            mc.mouseHandler.releaseMouse();
            LCUMod.LOGGER.info("[Input] AI control: cursor released to the user");
        }

        // Check for user timeout (auto-return to AI)
        if (hasUserTimedOut()) {
            toggleControl();
            return;
        }

        // Apply AI controls if AI is controlling
        if (aiControlled) {
            applyAiControls(mc);
        }
    }

    /**
     * Apply AI controls to the player.
     *
     * Key states are only written while the AI actually holds something, plus exactly one release
     * when it lets go. Writing "all keys up" every tick would clobber a human who is using the same
     * client instance — their held key is cleared by the next tick and they cannot walk.
     */
    private static void applyAiControls(Minecraft mc) {
        Options options = mc.options;

        boolean anyKeyState = aiForward || aiBack || aiLeft || aiRight || aiJump || aiSneak || aiSprint;
        if (anyKeyState) {
            options.keyUp.setDown(aiForward);
            options.keyLeft.setDown(aiLeft);
            options.keyRight.setDown(aiRight);
            options.keyDown.setDown(aiBack);
            options.keyJump.setDown(aiJump);
            options.keyShift.setDown(aiSneak);
            options.keySprint.setDown(aiSprint);
            aiKeysDirty = true;
        } else if (aiKeysDirty) {
            // Released once, then the key bindings belong to the human again.
            options.keyUp.setDown(false);
            options.keyLeft.setDown(false);
            options.keyRight.setDown(false);
            options.keyDown.setDown(false);
            options.keyJump.setDown(false);
            options.keyShift.setDown(false);
            options.keySprint.setDown(false);
            aiKeysDirty = false;
        }

        // Apply look target (only while the AI actually has one)
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
