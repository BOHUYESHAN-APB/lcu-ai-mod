package com.lcu.lcumod.compat;

import com.lcu.lcumod.LCUMod;
import net.neoforged.fml.ModList;

import java.lang.reflect.Method;

public final class WatutCompat {
    private static boolean initialized;
    private static boolean available;
    private static Method managerGetter;
    private static Method onAction;
    private static int lastReportedPlayerTick = Integer.MIN_VALUE;

    private WatutCompat() {}

    public static void reportProgrammaticAction(int playerTick) {
        if (lastReportedPlayerTick != Integer.MIN_VALUE && playerTick - lastReportedPlayerTick < 20) return;
        initialize();
        if (!available) return;
        try {
            Object manager = managerGetter.invoke(null);
            if (manager != null) {
                onAction.invoke(manager);
                lastReportedPlayerTick = playerTick;
            }
        } catch (ReflectiveOperationException | RuntimeException exception) {
            available = false;
            LCUMod.LOGGER.warn("[Compat/WATUT] Activity reporting disabled: {}", exception.getMessage());
        }
    }

    private static synchronized void initialize() {
        if (initialized) return;
        initialized = true;
        if (!ModList.get().isLoaded("watut")) return;
        try {
            Class<?> modClass = Class.forName("com.corosus.watut.WatutMod");
            managerGetter = modClass.getMethod("getPlayerStatusManagerClient");
            Object manager = managerGetter.invoke(null);
            if (manager == null) {
                initialized = false;
                return;
            }
            onAction = manager.getClass().getMethod("onAction");
            available = true;
            LCUMod.LOGGER.info("[Compat/WATUT] Programmatic activity reporting enabled");
        } catch (ReflectiveOperationException | RuntimeException exception) {
            LCUMod.LOGGER.warn("[Compat/WATUT] Compatible API unavailable: {}", exception.getMessage());
        }
    }
}
