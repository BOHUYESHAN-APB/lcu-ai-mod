package com.lcu.lcumod.action;

import com.google.gson.JsonArray;
import com.google.gson.JsonObject;
import com.lcu.lcumod.LCUMod;
import net.minecraft.client.Minecraft;

import java.util.ArrayDeque;
import java.util.Deque;

/**
 * 死亡点：**死了之后自己记住死在哪儿**（用户口径："死了不会回死亡点捡装备" ⇒ 得能回去）。
 *
 * <p>只做一件事：把"死在哪、什么时候"如实交给后端。**不做**自动往回走 —— 回不回是模型的
 * 决定（能力=注册的工具，后台没有自主开关），后端为此提供 `go_to_death_point` 工具。
 *
 * <p>两个实现上的取舍：
 * <ul>
 *   <li>记的是**死前最后一次活着的位置**（每 tick 更新一个很轻的"最近活着坐标"），
 *       而不是"死亡瞬间读坐标"：客户端一死就可能立刻自动重生（`ActionExecutor` 里有自动重生），
 *       那一瞬间读到的坐标未必还站在死亡点上。这样不依赖任何时序。</li>
 *   <li>只在**刚死的那一下**记一条（活着 ↔ 死亡的边沿），不会每 tick 刷一堆积点。</li>
 * </ul>
 */
public final class DeathPoint {

    /** 最多留几个（最新在前）；后端只把最新的那个当"上次死亡点"。 */
    private static final int MAX_POINTS = 3;

    private static final Deque<JsonObject> POINTS = new ArrayDeque<>();

    private static boolean wasAlive = true;
    private static double lastAliveX = Double.NaN;
    private static double lastAliveY = Double.NaN;
    private static double lastAliveZ = Double.NaN;
    private static String lastAliveDimension = "";
    /** 已经记过的死亡点指纹，避免同一次死亡被记两遍（重生前后可能连续几 tick 都是"死"）。 */
    private static String lastRecordedSignature = "";

    private DeathPoint() {
    }

    /** 每客户端 tick 调一次（由 `StateCollector.onClientTick` 挂钩，与状态采集同一处）。 */
    public static void tick(Minecraft mc) {
        if (mc == null || mc.player == null || mc.level == null) {
            wasAlive = true;
            return;
        }
        boolean dead = mc.player.isDeadOrDying() || mc.player.getHealth() <= 0.0f;
        if (!dead) {
            lastAliveX = mc.player.getX();
            lastAliveY = mc.player.getY();
            lastAliveZ = mc.player.getZ();
            lastAliveDimension = mc.level.dimension().identifier().toString();
            wasAlive = true;
            return;
        }
        if (!wasAlive) {
            // 还是"死着"的状态（重生前的连续几 tick）：不重复记。
            return;
        }
        wasAlive = false;
        record(lastAliveX, lastAliveY, lastAliveZ, lastAliveDimension);
    }

    private static void record(double x, double y, double z, String dimension) {
        if (Double.isNaN(x) || Double.isNaN(y) || Double.isNaN(z)) {
            // 一次都还没活过（刚连服务器就死）：没有可信坐标就别编一个。
            return;
        }
        String signature = Math.round(x) + "," + Math.round(y) + "," + Math.round(z) + "@" + dimension;
        if (signature.equals(lastRecordedSignature)) {
            return;
        }
        lastRecordedSignature = signature;

        JsonObject point = new JsonObject();
        point.addProperty("x", Math.round(x * 10.0) / 10.0);
        point.addProperty("y", Math.round(y * 10.0) / 10.0);
        point.addProperty("z", Math.round(z * 10.0) / 10.0);
        point.addProperty("dimension", dimension == null ? "" : dimension);
        point.addProperty("at", System.currentTimeMillis());
        POINTS.addFirst(point);
        while (POINTS.size() > MAX_POINTS) {
            POINTS.removeLast();
        }
        LCUMod.LOGGER.info("[Death] 记住死亡点 ({}, {}, {}) @{} —— 回不回由模型决定",
                point.get("x"), point.get("y"), point.get("z"), point.get("dimension"));
    }

    /** 状态字段 `death_points`：最新在前，最多 3 条。 */
    public static JsonArray snapshot() {
        JsonArray out = new JsonArray();
        for (JsonObject point : POINTS) {
            out.add(point.deepCopy());
        }
        return out;
    }

    /** 最近一次死亡点（没有则 null）。 */
    public static JsonObject latest() {
        return POINTS.isEmpty() ? null : POINTS.peekFirst();
    }

    /** 死亡点是否发生过（后端/测试用）。 */
    public static boolean hasAny() {
        return !POINTS.isEmpty();
    }

    /** 换世界/重连用：清掉。 */
    public static void reset() {
        POINTS.clear();
        wasAlive = true;
        lastRecordedSignature = "";
        lastAliveX = Double.NaN;
        lastAliveY = Double.NaN;
        lastAliveZ = Double.NaN;
        lastAliveDimension = "";
    }

    /** 供零依赖自检引用（避免"新类没接线"这类事故）。 */
    public static String describe() {
        JsonObject latest = latest();
        if (latest == null) {
            return "no_death_point";
        }
        return "death_point " + latest.get("x") + "," + latest.get("y") + "," + latest.get("z")
                + "@" + latest.get("dimension");
    }
}
