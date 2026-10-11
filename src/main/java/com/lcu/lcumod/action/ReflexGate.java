package com.lcu.lcumod.action;

import java.util.HashMap;
import java.util.Map;

/**
 * 本能反射的**抑制开关** —— 解决"本能盖过高级指令"。
 *
 * 用户要求（原话）：「有些基础逻辑可以由快捷的自主反应来解决，但有些情况下后端可能要
 * **覆盖住它的指令**来执行一些高难度的事情，**不能让本能反应盖过某些高级反应**。」
 *
 * 例：后端要潜水去挖矿/救人 → 先 `reflex_override {name:"swim", seconds:30}` 关掉
 * "见水就上浮"，同时用控制流把它往下带；到期本能自动恢复（不需要后端记得恢复）。
 */
public final class ReflexGate {

    public static final String SWIM = "swim";
    public static final String COMBAT = "combat";
    /** 抑制全部反射。 */
    public static final String ALL = "all";

    private static final Map<String, Long> UNTIL_MS = new HashMap<>();

    private ReflexGate() {
    }

    /** 抑制某反射 seconds 秒；seconds<=0 表示立刻恢复。 */
    public static void suppress(String name, double seconds, long nowMs) {
        if (name == null || name.isBlank()) {
            return;
        }
        String key = name.trim().toLowerCase();
        if (seconds <= 0) {
            UNTIL_MS.remove(key);
            return;
        }
        UNTIL_MS.put(key, nowMs + (long) (seconds * 1000.0));
    }

    public static void suppress(String name, double seconds) {
        suppress(name, seconds, System.currentTimeMillis());
    }

    public static boolean isSuppressed(String name, long nowMs) {
        if (name == null) {
            return false;
        }
        String key = name.trim().toLowerCase();
        Long until = UNTIL_MS.get(key);
        if (until != null) {
            if (nowMs < until) {
                return true;
            }
            UNTIL_MS.remove(key);
        }
        Long all = UNTIL_MS.get(ALL);
        if (all != null) {
            if (nowMs < all) {
                return true;
            }
            UNTIL_MS.remove(ALL);
        }
        return false;
    }

    public static boolean isSuppressed(String name) {
        return isSuppressed(name, System.currentTimeMillis());
    }

    /** 剩余抑制秒数（0 = 未抑制），用于状态上报/排错。 */
    public static double remainingSeconds(String name, long nowMs) {
        Long until = name == null ? null : UNTIL_MS.get(name.trim().toLowerCase());
        if (until == null) {
            return 0.0;
        }
        long left = until - nowMs;
        return left <= 0 ? 0.0 : left / 1000.0;
    }

    public static void clearAll() {
        UNTIL_MS.clear();
    }
}
