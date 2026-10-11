package com.lcu.lcumod.action;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 路径记忆 —— 用户原话："还要记录路径，因为有些时候寻路会有点复杂，玩家挖掘的洞穴
 * 可能会超出原版最大寻路距离，它得稍微记录线"。
 *
 * <p>做法：把**实际走过的路**（客户端每 tick 的真实位姿）抽稀成折线，每段都落在原版
 * 寻路射程内，重放时逐点推进（见 {@link RouteFollower}）。抽稀（压缩）规则：
 * <ul>
 *   <li>距上一个已记点 ≥ {@link #DEFAULT_MIN_STEP} 格（默认 8）→ 记一点；<b>或</b></li>
 *   <li>行进方向相对上一个已记点变化 ≥ {@link #MIN_TURN_DEGREES}（默认 30°）且已经真的动了
 *       （≥ {@link #MIN_MOVE}）→ 记一点。</li>
 * </ul>
 * 于是"直线少点、拐弯多点"：洞穴里的拐角一定留下点，长直道每 8 格才留一点。
 * 每点带 xyz + yaw（重放时同时对齐朝向）。
 *
 * <p>持久性：与 {@link StationMemory} 一样不被视野淘汰；{@link #toFields()} /
 * {@link #restore(List)} 提供纯字段表的存/取。
 *
 * <p>本类刻意不依赖任何 Minecraft 类型（也不用 gson），便于 {@code verifyRoute} 自检。
 */
public final class RouteMemory {

    /** 默认每 N 格记一点。 */
    public static final double DEFAULT_MIN_STEP = 8.0;
    /** 方向变化阈值（度）。 */
    public static final double MIN_TURN_DEGREES = 30.0;
    /** 小于这个位移视为抖动/原地转身，不更新方向基准。 */
    public static final double MIN_MOVE = 0.35;
    /** 后端回灌一条路径至少要有 2 个点（1 个点的"路径"没有意义，直接拒绝）。 */
    public static final int MIN_POINTS = 2;
    /** 后端回灌单条路径的点数上限（防呆：异常大 payload 直接拒绝，不落库）。 */
    public static final int MAX_POINTS = 2000;

    /** 路径上的一个点：精准 xyz + 朝向。 */
    public record RoutePoint(double x, double y, double z, float yaw) {

        public double horizontalDistanceTo(RoutePoint other) {
            double dx = x - other.x;
            double dz = z - other.z;
            return Math.sqrt(dx * dx + dz * dz);
        }

        /** 到任意水平位置的距离（重放判定用）。 */
        public double horizontalDistanceTo(double px, double pz) {
            double dx = px - x;
            double dz = pz - z;
            return Math.sqrt(dx * dx + dz * dz);
        }

        public Map<String, Object> toFields() {
            Map<String, Object> f = new LinkedHashMap<>();
            f.put("x", x);
            f.put("y", y);
            f.put("z", z);
            f.put("yaw", yaw);
            return f;
        }

        public static RoutePoint fromFields(Map<String, Object> f) {
            return new RoutePoint(num(f.get("x"), 0.0), num(f.get("y"), 0.0),
                    num(f.get("z"), 0.0), (float) num(f.get("yaw"), 0.0));
        }
    }

    /** 一条命名路径（同名覆盖）。 */
    public record Route(String name, List<RoutePoint> points, int savedAtTick) {

        public Route {
            points = List.copyOf(points);
        }

        public Map<String, Object> toFields() {
            Map<String, Object> f = new LinkedHashMap<>();
            f.put("name", name);
            List<Map<String, Object>> pts = new ArrayList<>();
            for (RoutePoint p : points) {
                pts.add(p.toFields());
            }
            f.put("points", pts);
            f.put("saved_at_tick", savedAtTick);
            return f;
        }

        @SuppressWarnings("unchecked")
        public static Route fromFields(Map<String, Object> f) {
            List<RoutePoint> pts = new ArrayList<>();
            Object raw = f.get("points");
            if (raw instanceof List<?> list) {
                for (Object item : list) {
                    if (item instanceof Map<?, ?> m) {
                        pts.add(RoutePoint.fromFields((Map<String, Object>) m));
                    }
                }
            }
            return new Route(str(f.get("name"), ""), pts, (int) num(f.get("saved_at_tick"), 0));
        }
    }

    private static final Map<String, Route> ROUTES = new LinkedHashMap<>();

    private RouteMemory() {
    }

    /** 存一条路径（同名覆盖）。名字为空或没有任何点则不落库，返回 null。 */
    public static Route save(String name, List<RoutePoint> points, int tickCount) {
        String clean = name == null ? "" : name.strip();
        if (clean.isEmpty() || points == null || points.isEmpty()) {
            return null;
        }
        return store(clean, points, tickCount);
    }

    private static Route store(String cleanName, List<RoutePoint> points, int tickCount) {
        Route route = new Route(cleanName, points, tickCount);
        ROUTES.put(cleanName, route);
        return route;
    }

    /** 回灌失败的原因（{@link IngestError#reason()} 给稳定的线上字符串）。 */
    public enum IngestError {
        NONE(""), NO_NAME("need name"), TOO_FEW("too few points"), TOO_MANY("too many points");

        private final String reason;

        IngestError(String reason) {
            this.reason = reason;
        }

        public String reason() {
            return reason;
        }
    }

    /** 回灌结果：成功时带落库后的路径，失败时带原因（**失败绝不改动已有条目**）。 */
    public record IngestResult(Route route, IngestError error, String detail) {
        public boolean ok() {
            return error == IngestError.NONE;
        }
    }

    /**
     * 后端回灌（持久化的真相来源在后端）：把后端存着的整条路径灌回模组。
     *
     * <p>与现场录像的区别：**不做任何抽稀**（后端存的就是真相，抽稀过一次了），
     * 只做防呆校验 —— 名字必填、点数 ∈ [{@link #MIN_POINTS}, {@link #MAX_POINTS}]。
     * 校验在落库之前，因此被拒的请求不会破坏同名旧条目。灌进来即可直接 {@code route_follow}。
     */
    public static IngestResult ingest(String name, List<RoutePoint> points, int tickCount) {
        String clean = name == null ? "" : name.strip();
        if (clean.isEmpty()) {
            return new IngestResult(null, IngestError.NO_NAME, IngestError.NO_NAME.reason());
        }
        int size = points == null ? 0 : points.size();
        if (size < MIN_POINTS) {
            return new IngestResult(null, IngestError.TOO_FEW,
                    IngestError.TOO_FEW.reason() + ": " + size + " (need >= " + MIN_POINTS + ")");
        }
        if (size > MAX_POINTS) {
            return new IngestResult(null, IngestError.TOO_MANY,
                    IngestError.TOO_MANY.reason() + ": " + size + " (max " + MAX_POINTS + ")");
        }
        return new IngestResult(store(clean, points, tickCount), IngestError.NONE, "");
    }

    public static Route get(String name) {
        return name == null ? null : ROUTES.get(name.strip());
    }

    public static boolean has(String name) {
        return get(name) != null;
    }

    public static boolean forget(String name) {
        return name != null && ROUTES.remove(name.strip()) != null;
    }

    public static int size() {
        return ROUTES.size();
    }

    public static List<Route> all() {
        return new ArrayList<>(ROUTES.values());
    }

    public static void clear() {
        ROUTES.clear();
    }

    /** 持久化字段表（全量路径）。 */
    public static List<Map<String, Object>> toFields() {
        List<Map<String, Object>> out = new ArrayList<>();
        for (Route r : ROUTES.values()) {
            out.add(r.toFields());
        }
        return out;
    }

    /** 用字段表恢复（整体替换）。 */
    public static void restore(List<Map<String, Object>> fields) {
        ROUTES.clear();
        if (fields == null) {
            return;
        }
        for (Map<String, Object> f : fields) {
            Route r = Route.fromFields(f);
            if (!r.name().isEmpty() && !r.points().isEmpty()) {
                ROUTES.put(r.name(), r);
            }
        }
    }

    /** 两个方位角之间的最小夹角（度，∈[0,180]）。 */
    public static double turnDegrees(double headingA, double headingB) {
        double d = (headingA - headingB) % 360.0;
        if (d > 180.0) {
            d -= 360.0;
        }
        if (d < -180.0) {
            d += 360.0;
        }
        return Math.abs(d);
    }

    /**
     * 离线压缩一条**已采样轨迹**（自检与"事后补记"共用；规则与现场 {@link Recorder} 完全一致）。
     * 末尾会补上最后一个采样点（终点必须精确），除非它已经落在已记点上。
     */
    public static List<RoutePoint> compress(List<RoutePoint> samples, double minStep, double minTurnDeg) {
        Recorder recorder = new Recorder(minStep, minTurnDeg, MIN_MOVE);
        if (samples != null) {
            for (RoutePoint p : samples) {
                recorder.offer(p.x(), p.y(), p.z(), p.yaw());
            }
        }
        List<RoutePoint> points = new ArrayList<>(recorder.points());
        if (!points.isEmpty() && samples != null && !samples.isEmpty()) {
            RoutePoint lastSample = samples.get(samples.size() - 1);
            RoutePoint lastStored = points.get(points.size() - 1);
            if (lastStored.horizontalDistanceTo(lastSample) > 0.05) {
                points.add(lastSample);
            }
        }
        return points;
    }

    /**
     * 现场压缩器：边走边决定"这一点值不值得记"。
     *
     * <p>方向基准按**真实位移**（上一次采样）计算，而不是按每个 tick 的噪声；位移小于
     * {@code minMove} 时直接忽略（原地转身不产生点）。
     */
    public static final class Recorder {

        private final double minStep;
        private final double minTurnDeg;
        private final double minMove;
        private final List<RoutePoint> points = new ArrayList<>();

        private boolean hasRaw = false;
        private double rawX;
        private double rawZ;
        private double headingAtLastStore = Double.NaN;

        public Recorder() {
            this(DEFAULT_MIN_STEP, MIN_TURN_DEGREES, MIN_MOVE);
        }

        public Recorder(double minStep, double minTurnDeg, double minMove) {
            this.minStep = minStep <= 0 ? DEFAULT_MIN_STEP : minStep;
            this.minTurnDeg = minTurnDeg <= 0 ? MIN_TURN_DEGREES : minTurnDeg;
            this.minMove = minMove <= 0 ? MIN_MOVE : minMove;
        }

        /** 喂一个采样点；**记下了**返回 true。 */
        public boolean offer(double x, double y, double z, float yaw) {
            if (!hasRaw) {
                store(new RoutePoint(x, y, z, yaw), Double.NaN);
                hasRaw = true;
                rawX = x;
                rawZ = z;
                return true;
            }

            double dx = x - rawX;
            double dz = z - rawZ;
            double moved = Math.sqrt(dx * dx + dz * dz);
            if (moved < minMove) {
                return false;
            }

            double heading = Math.toDegrees(Math.atan2(dz, dx));
            rawX = x;
            rawZ = z;

            RoutePoint last = points.get(points.size() - 1);
            double sinceStored = last.horizontalDistanceTo(x, z);
            boolean farEnough = sinceStored >= minStep;
            boolean turnedEnough = !Double.isNaN(headingAtLastStore)
                    && turnDegrees(heading, headingAtLastStore) >= minTurnDeg
                    && sinceStored >= minMove;
            if (farEnough || turnedEnough) {
                store(new RoutePoint(x, y, z, yaw), heading);
                return true;
            }
            return false;
        }

        private void store(RoutePoint point, double heading) {
            points.add(point);
            if (!Double.isNaN(heading)) {
                headingAtLastStore = heading;
            }
        }

        public List<RoutePoint> points() {
            return new ArrayList<>(points);
        }

        public int size() {
            return points.size();
        }

        public void clear() {
            points.clear();
            hasRaw = false;
            headingAtLastStore = Double.NaN;
        }
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
