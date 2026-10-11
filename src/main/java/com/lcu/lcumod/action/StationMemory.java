package com.lcu.lcumod.action;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;

/**
 * 站点记忆 —— 用户原话："有些机器可能需要 AI 去挂机，就是让 AI 去某些点位去挂机，
 * 比如说刷怪塔呀小黑塔之类的地方""它得记住精准的 xyz 的坐标"。
 *
 * <p>与 {@link PoiMemory} 的分工：{@code PoiMemory} 记的是**看到的方块**（工作台/箱子，
 * 按方块位置索引）；这里记的是**语义站点**（刷怪塔/农场/传送门…），按**名字**索引，
 * 带一份"精准 xyz + 朝向 + 水平容差"。名字是主键 —— **同名覆盖**（用户改口重新指认时
 * 不应该出现两个同名站点）。
 *
 * <p>持久性：站点一旦记下就**不会**被"扫描看不见"淘汰（与 {@code PoiMemory.persistent} 同义），
 * 因此可以记住几百格外的机器。{@link #toFields()} / {@link #restore(List)} 给出纯字段表
 * 的存/取（只用 Map/List/基本类型，零依赖），供上层落盘或自检字段完整性。
 *
 * <p>本类刻意不依赖任何 Minecraft 类型，便于 {@code verifyRoute} 自检。
 */
public final class StationMemory {

    /** 未指定 kind 时的默认类型。 */
    public static final String DEFAULT_KIND = "custom";
    /** 驻守半径默认 1 格（用户："保持在 radius 内（默认 1 格）"）。 */
    public static final double DEFAULT_RADIUS = 1.0;
    public static final double MIN_RADIUS = 0.5;
    public static final double MAX_RADIUS = 16.0;

    /** 已知的站点类型（只是标签，未知值不被拒绝，按 {@link #DEFAULT_KIND} 归类）。 */
    public static final List<String> KINDS =
            List.of("mob_farm", "enderman_farm", "iron_farm", "crop", "portal", "custom");

    /** 一个站点：精准坐标 + 朝向 + 水平容差。 */
    public record Station(String name, String kind, double x, double y, double z,
                          float yaw, float pitch, double radius, int savedAtTick) {

        public double horizontalDistanceTo(double px, double pz) {
            double dx = px - x;
            double dz = pz - z;
            return Math.sqrt(dx * dx + dz * dz);
        }

        public double distanceTo(double px, double py, double pz) {
            double dx = px - x;
            double dy = py - y;
            double dz = pz - z;
            return Math.sqrt(dx * dx + dy * dy + dz * dz);
        }

        /** 持久化字段（纯 Map，键名即线上字段名）。 */
        public Map<String, Object> toFields() {
            Map<String, Object> f = new LinkedHashMap<>();
            f.put("name", name);
            f.put("kind", kind);
            f.put("x", x);
            f.put("y", y);
            f.put("z", z);
            f.put("yaw", yaw);
            f.put("pitch", pitch);
            f.put("radius", radius);
            f.put("saved_at_tick", savedAtTick);
            return f;
        }

        public static Station fromFields(Map<String, Object> f) {
            return new Station(
                    str(f.get("name"), ""),
                    normalizeKind(str(f.get("kind"), DEFAULT_KIND)),
                    num(f.get("x"), 0.0),
                    num(f.get("y"), 0.0),
                    num(f.get("z"), 0.0),
                    (float) num(f.get("yaw"), 0.0),
                    (float) num(f.get("pitch"), 0.0),
                    normalizeRadius(num(f.get("radius"), DEFAULT_RADIUS)),
                    (int) num(f.get("saved_at_tick"), 0));
        }
    }

    private static final Map<String, Station> STATIONS = new LinkedHashMap<>();

    private StationMemory() {
    }

    /**
     * 记一个站点（同名覆盖）。返回落库后的条目；名字为空则返回 null（不落库）。
     */
    public static Station remember(String name, String kind, double x, double y, double z,
                                   float yaw, float pitch, double radius, int tickCount) {
        String clean = name == null ? "" : name.strip();
        if (clean.isEmpty()) {
            return null;
        }
        Station station = new Station(clean, normalizeKind(kind),
                round3(x), round3(y), round3(z), yaw, pitch, normalizeRadius(radius), tickCount);
        STATIONS.put(clean, station);
        return station;
    }

    public static Station get(String name) {
        return name == null ? null : STATIONS.get(name.strip());
    }

    /**
     * 后端回灌（持久化的真相来源在后端）：**显式参数优先、缺省回退当前位姿**。
     *
     * <p>与 {@link #remember} 同名同义、只是参数可为 null —— 于是同一条命令既能"用当前位姿指认"
     * 也能"把后端存着的坐标推回来"，两条路径共用同一份落库逻辑（字段天然一致）。
     * 缺省口径：坐标/朝向缺失 → 当前位姿；{@code radius} 缺失 → {@link #DEFAULT_RADIUS}；
     * {@code kind} 缺失 → {@link #DEFAULT_KIND}。
     */
    public static Station rememberWithPoseFallback(String name, String kind,
                                                   Double x, Double y, Double z,
                                                   Float yaw, Float pitch, Double radius,
                                                   double poseX, double poseY, double poseZ,
                                                   float poseYaw, float posePitch, int tickCount) {
        return remember(name, kind,
                x == null ? poseX : x,
                y == null ? poseY : y,
                z == null ? poseZ : z,
                yaw == null ? poseYaw : yaw,
                pitch == null ? posePitch : pitch,
                radius == null ? DEFAULT_RADIUS : radius,
                tickCount);
    }

    public static boolean has(String name) {
        return get(name) != null;
    }

    /** 删除一个站点（用户"这个点不要了"）。 */
    public static boolean forget(String name) {
        return name != null && STATIONS.remove(name.strip()) != null;
    }

    public static int size() {
        return STATIONS.size();
    }

    /** 当前全部站点（插入顺序的副本）。 */
    public static List<Station> all() {
        return new ArrayList<>(STATIONS.values());
    }

    public static void clear() {
        STATIONS.clear();
    }

    /** 已知类型或默认类型（大小写归一到小写）。 */
    public static String normalizeKind(String kind) {
        if (kind == null || kind.isBlank()) {
            return DEFAULT_KIND;
        }
        String k = kind.strip().toLowerCase(Locale.ROOT);
        return KINDS.contains(k) ? k : DEFAULT_KIND;
    }

    /** 容差收敛到 [MIN_RADIUS, MAX_RADIUS]；非法值（NaN/<=0）取默认 1 格。 */
    public static double normalizeRadius(double radius) {
        if (Double.isNaN(radius) || radius <= 0) {
            return DEFAULT_RADIUS;
        }
        return Math.max(MIN_RADIUS, Math.min(MAX_RADIUS, radius));
    }

    /** 持久化字段表（全量站点）。 */
    public static List<Map<String, Object>> toFields() {
        List<Map<String, Object>> out = new ArrayList<>();
        for (Station s : STATIONS.values()) {
            out.add(s.toFields());
        }
        return out;
    }

    /** 用字段表恢复（整体替换，同名后者覆盖前者）。 */
    public static void restore(List<Map<String, Object>> fields) {
        STATIONS.clear();
        if (fields == null) {
            return;
        }
        for (Map<String, Object> f : fields) {
            Station s = Station.fromFields(f);
            if (!s.name().isEmpty()) {
                STATIONS.put(s.name(), s);
            }
        }
    }

    private static double round3(double v) {
        return Math.round(v * 1000.0) / 1000.0;
    }

    private static String str(Object v, String fallback) {
        return v == null ? fallback : String.valueOf(v);
    }

    private static double num(Object v, double fallback) {
        if (v instanceof Number n) {
            return n.doubleValue();
        }
        if (v instanceof String s) {
            try {
                return Double.parseDouble(s);
            } catch (NumberFormatException ignored) {
                return fallback;
            }
        }
        return fallback;
    }
}
