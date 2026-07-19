package com.lcu.lcumod.config;

import net.neoforged.neoforge.common.ModConfigSpec;
import java.util.Map;

/** Fail-closed access to policy-sensitive client features. */
public final class ServerPolicy {
    private ServerPolicy() {}

    public static boolean autonomousBehaviorsAllowed() {
        return enabled(ModConfig.ENABLE_AUTONOMOUS_BEHAVIORS);
    }

    public static boolean activitySignalsAllowed() {
        return enabled(ModConfig.ENABLE_ACTIVITY_SIGNALS);
    }

    public static boolean programmaticActivityReportingAllowed() {
        return enabled(ModConfig.REPORT_PROGRAMMATIC_ACTIVITY);
    }

    public static boolean backgroundExecutionAllowed() {
        return enabled(ModConfig.RUN_IN_BACKGROUND);
    }

    public static boolean autoRespawnAllowed() {
        return enabled(ModConfig.AUTO_RESPAWN);
    }

    public static boolean automatedCombatAllowed() {
        return enabled(ModConfig.ALLOW_AUTOMATED_COMBAT);
    }

    public static boolean movementAutomationAllowed() {
        return enabled(ModConfig.ALLOW_MOVEMENT_AUTOMATION);
    }

    public static boolean worldAutomationAllowed() {
        return enabled(ModConfig.ALLOW_WORLD_AUTOMATION);
    }

    public static boolean inventoryAutomationAllowed() {
        return enabled(ModConfig.ALLOW_INVENTORY_AUTOMATION);
    }

    public static boolean chatAutomationAllowed() {
        return enabled(ModConfig.ALLOW_CHAT_AUTOMATION);
    }

    public static boolean surroundingsCollectionAllowed() {
        return enabled(ModConfig.COLLECT_SURROUNDINGS);
    }

    public static Map<String, Boolean> snapshot() {
        return Map.ofEntries(
            Map.entry("allowMovementAutomation", movementAutomationAllowed()),
            Map.entry("allowWorldAutomation", worldAutomationAllowed()),
            Map.entry("allowInventoryAutomation", inventoryAutomationAllowed()),
            Map.entry("allowAutomatedCombat", automatedCombatAllowed()),
            Map.entry("allowChatAutomation", chatAutomationAllowed()),
            Map.entry("enableAutonomousBehaviors", autonomousBehaviorsAllowed()),
            Map.entry("enableActivitySignals", activitySignalsAllowed()),
            Map.entry("reportProgrammaticActivity", programmaticActivityReportingAllowed()),
            Map.entry("runInBackground", backgroundExecutionAllowed()),
            Map.entry("autoRespawn", autoRespawnAllowed()),
            Map.entry("collectSurroundings", surroundingsCollectionAllowed())
        );
    }

    private static boolean enabled(ModConfigSpec.BooleanValue value) {
        try {
            return value.getAsBoolean();
        } catch (IllegalStateException ignored) {
            return false;
        }
    }
}
