package com.lcu.lcumod.action;

import java.util.ArrayList;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;

/**
 * 零依赖自检：站点记忆（{@link StationMemory}）+ 驻守（{@link AfkController}）+
 * 路径记忆/抽稀（{@link RouteMemory}）+ 重放（{@link RouteFollower}）。
 *
 * <p>照现有 verify* 的写法：纯 Java + JavaExec（见 build.gradle 的 {@code verifyRoute} 任务），
 * 不引入任何依赖（连 gson 都不用：这些类只用 Map/List/基本类型），随 {@code build} / {@code check} 一起跑。
 * 断言信息用 ASCII（Windows 控制台 GBK）。
 *
 * <p>覆盖任务点名的 6 条契约：
 * <ol>
 *   <li>压缩策略：直线少点、拐弯多点、拐角必留点、每段都在原版寻路射程内；</li>
 *   <li>重放链式推进：到第 i 点才走第 i+1 点，越过终点即 done；</li>
 *   <li>中断/新任务：abort 与 fail 区分，afk_stop 立刻停；</li>
 *   <li>站点驻守的漂移纠正阈值（带滞回）+ at_target / since_ticks / keepalive 不移动；</li>
 *   <li>同名覆盖：站点与路径都按名字当主键；</li>
 *   <li>持久化字段完整性：字段表存/取往返一致，且必需字段一个不少。</li>
 * </ol>
 */
public final class RouteCheck {

    private static int passed = 0;
    private static int failed = 0;

    private RouteCheck() {
    }

    public static void main(String[] args) {
        compressionPolicy();
        compressionKeepsSegmentsInVanillaRange();
        replayChainsPointByPoint();
        interruptionStopsCleanly();
        afkDriftThresholds();
        afkKeepaliveDoesNotMove();
        sameNameOverwrites();
        persistenceFieldsComplete();

        System.out.println("[RouteCheck] " + passed + " checks passed, " + failed + " failed");
        if (failed > 0) {
            System.exit(1);
        }
    }

    /** ① 压缩策略：直线少点；拐弯（锯齿）明显多点；拐角一定留下点。 */
    private static void compressionPolicy() {
        List<RouteMemory.RoutePoint> straight = new ArrayList<>();
        for (int i = 0; i <= 80; i++) {
            straight.add(new RouteMemory.RoutePoint(i, 64, 0, 90f));
        }
        List<RouteMemory.RoutePoint> straightPoints =
                RouteMemory.compress(straight, RouteMemory.DEFAULT_MIN_STEP, RouteMemory.MIN_TURN_DEGREES);
        check("straight 81 samples compress to few points (<=13)", straightPoints.size() <= 13);
        check("straight keeps at least the endpoints", straightPoints.size() >= 9);
        check("straight compression is at least 6x", straightPoints.size() * 6 <= straight.size());

        List<RouteMemory.RoutePoint> zigzag = new ArrayList<>();
        for (int i = 0; i <= 80; i++) {
            zigzag.add(new RouteMemory.RoutePoint(i, 64, (i % 2 == 0) ? 0 : 1, 0f));
        }
        List<RouteMemory.RoutePoint> zigzagPoints =
                RouteMemory.compress(zigzag, RouteMemory.DEFAULT_MIN_STEP, RouteMemory.MIN_TURN_DEGREES);
        check("zigzag keeps every turn (>= 3x straight)", zigzagPoints.size() >= straightPoints.size() * 3);
        check("zigzag keeps more points than samples would allow if straight", zigzagPoints.size() > 30);

        // L 形：先向东 20 格再向北 20 格 —— 拐角必须留下一个点（洞穴里的拐弯就是靠它还原）
        List<RouteMemory.RoutePoint> ell = new ArrayList<>();
        for (int i = 0; i <= 20; i++) {
            ell.add(new RouteMemory.RoutePoint(i, 64, 0, 90f));
        }
        for (int i = 1; i <= 20; i++) {
            ell.add(new RouteMemory.RoutePoint(20, 64, i, 180f));
        }
        List<RouteMemory.RoutePoint> ellPoints =
                RouteMemory.compress(ell, RouteMemory.DEFAULT_MIN_STEP, RouteMemory.MIN_TURN_DEGREES);
        RouteMemory.RoutePoint corner = new RouteMemory.RoutePoint(20, 64, 0, 90f);
        double nearestToCorner = Double.MAX_VALUE;
        for (RouteMemory.RoutePoint p : ellPoints) {
            nearestToCorner = Math.min(nearestToCorner, p.horizontalDistanceTo(corner));
        }
        check("corner of an L is captured (within 2 blocks)", nearestToCorner <= 2.0);
        check("L compresses to a modest number of points (<=12)", ellPoints.size() <= 12);

        // 记录器本身：第一个采样点必记；原地转身（位移 < MIN_MOVE）不记点
        RouteMemory.Recorder recorder = new RouteMemory.Recorder();
        check("recorder stores the first sample", recorder.offer(0, 64, 0, 0f) && recorder.size() == 1);
        check("recorder ignores jitter", !recorder.offer(0.05, 64, 0.05, 40f) && recorder.size() == 1);
        check("recorder stores after enough distance", recorder.offer(9, 64, 0, 0f) && recorder.size() == 2);
    }

    /** ①(续) 抽稀后的每一段都必须短到原版寻路能一口吃掉（用户点名"洞穴超出最大寻路距离"）。 */
    private static void compressionKeepsSegmentsInVanillaRange() {
        List<RouteMemory.RoutePoint> straight = new ArrayList<>();
        for (int i = 0; i <= 80; i++) {
            straight.add(new RouteMemory.RoutePoint(i, 64, 0, 90f));
        }
        List<RouteMemory.RoutePoint> points =
                RouteMemory.compress(straight, RouteMemory.DEFAULT_MIN_STEP, RouteMemory.MIN_TURN_DEGREES);
        double maxSegment = 0;
        for (int i = 1; i < points.size(); i++) {
            maxSegment = Math.max(maxSegment, points.get(i).horizontalDistanceTo(points.get(i - 1)));
        }
        check("every compressed leg is shorter than the default step (<=9)", maxSegment <= 9.0);
        check("last compressed point is the exact endpoint",
                points.get(points.size() - 1).horizontalDistanceTo(straight.get(straight.size() - 1)) <= 0.05);
    }

    /** ② 重放链式推进：逐点走，越过终点即 DONE；没到点绝不推进。 */
    private static void replayChainsPointByPoint() {
        List<RouteMemory.RoutePoint> pts = List.of(
                p(0, 64, 0), p(8, 64, 0), p(8, 64, 8), p(0, 64, 8));
        RouteFollower f = new RouteFollower();
        check("begin accepts a non-empty route", f.begin("cave", pts, 1.0));
        check("fills total", f.total() == 4 && f.index() == 0);
        check("starts active at point 0", f.isActive() && f.current().equals(pts.get(0)));
        f.beginLeg();

        check("does not advance when not reached", !f.advanceIfReached(4, 64, 4, 1.5, 2.0) && f.index() == 0);
        check("advances at point 0", f.advanceIfReached(0.4, 64, 0.4, 1.5, 2.0) && f.index() == 1);
        f.beginLeg();
        check("advances at point 1", f.advanceIfReached(8, 64, 0, 1.5, 2.0) && f.index() == 2);
        f.beginLeg();
        check("advances at point 2", f.advanceIfReached(8, 64, 8, 1.5, 2.0) && f.index() == 3);
        f.beginLeg();
        check("advances at the last point", f.advanceIfReached(0, 64, 8, 1.5, 2.0));
        check("finishes when past the last point", f.isDone() && !f.isActive() && f.index() == 4);
        check("progress reaches 1.0 at the end", f.progress() == 1.0);
        check("no current point after completion", f.current() == null);
        check("empty route is rejected", !new RouteFollower().begin("empty", List.of(), 1.0));

        // 节奏参数不改 Pathfinder 限速，只缩放"每段期望多久走完"
        RouteFollower fast = new RouteFollower();
        fast.begin("r", pts, 2.0);
        RouteFollower slow = new RouteFollower();
        slow.begin("r", pts, 0.5);
        check("higher speed shortens the leg timeout", fast.maxLegTicks() < slow.maxLegTicks());
        check("speed is clamped into [0.5, 2.0]", fast.speed() == 2.0 && slow.speed() == 0.5);
        check("stall window scales with speed", fast.stallWindowTicks() < slow.stallWindowTicks());
    }

    /** ③ 中断/新任务：abort ≠ fail，afk_stop 立刻停，且到达终点后不会被误判成中断。 */
    private static void interruptionStopsCleanly() {
        List<RouteMemory.RoutePoint> pts = List.of(p(0, 64, 0), p(8, 64, 0));
        RouteFollower f = new RouteFollower();
        f.begin("cave", pts, 1.0);
        f.abort("new_task");
        check("abort stops the replay", !f.isActive() && f.status() == RouteFollower.Status.ABORTED);
        check("abort keeps the reason", "new_task".equals(f.failureReason()));
        check("abort is not a failure", !f.isFailed() && f.failIndex() == -1);

        check("a new follow replaces the old one", f.begin("other", pts, 1.0));
        check("replacement restarts at point 0",
                f.isActive() && f.index() == 0 && "other".equals(f.name()));

        // 诚实失败：卡在第 i 个点
        f.advanceIfReached(0, 64, 0, 1.5, 2.0);
        check("moved on to point 1 before failing", f.index() == 1);
        f.fail("no progress: A* search failed");
        check("fail marks FAILED", f.isFailed() && !f.isActive());
        check("fail pins the honest index (1 -> reported as point 2)", f.failIndex() == 1);
        check("fail keeps the reason", f.failureReason().contains("A*"));

        // 到达终点后 abort 不应该把 done 改写成 aborted
        RouteFollower done = new RouteFollower();
        done.begin("short", List.of(p(0, 64, 0)), 1.0);
        done.advanceIfReached(0, 64, 0, 1.5, 2.0);
        check("single-point route finishes", done.isDone());
        done.abort("new_task");
        check("abort does not rewrite a completed route", done.isDone());

        // 段内重试：可重试时不算失败，超过次数才失败
        RouteFollower retry = new RouteFollower();
        retry.begin("cave", pts, 1.0);
        retry.beginLeg();
        check("first attempt can retry", retry.canRetry());
        retry.retryLeg();
        check("after max attempts no retry", !retry.canRetry() && retry.legAttempts() == 2);

        // 驻守：afk_stop 立刻停，状态复位
        AfkController afk = new AfkController();
        afk.begin(station("tower", 0, 64, 0, 1.0), true);
        afk.tick(0, 0);
        check("afk is active while holding", afk.isActive() && afk.sinceTicks() == 1);
        afk.stop();
        check("afk_stop deactivates immediately", !afk.isActive());
        check("afk_stop resets counters", afk.sinceTicks() == 0 && afk.drift() == 0.0);
        check("afk_stop clears the station name", afk.stationName().isEmpty());
        check("afk begin rejects a null station", !new AfkController().begin(null, false));
    }

    /** ④ 驻守漂移纠正阈值：出半径才走回，回到半径一半才停（滞回），并如实上报 drift/at_target。 */
    private static void afkDriftThresholds() {
        AfkController afk = new AfkController();
        afk.begin(station("tower", 0, 64, 0, 1.0), false);

        check("inside radius -> HOLD", afk.tick(0, 0) == AfkController.Action.HOLD);
        check("at_target true inside radius", afk.atTarget() && afk.drift() == 0.0);
        check("still HOLD at 0.9 (<= radius)", afk.tick(0.9, 0) == AfkController.Action.HOLD);
        check("at_target true at 0.9", afk.atTarget());

        check("RETURN once drift exceeds radius", afk.tick(1.2, 0) == AfkController.Action.RETURN);
        check("at_target false when outside radius", !afk.atTarget() && afk.returning());
        check("drift is reported honestly", Math.abs(afk.drift() - 1.2) < 1e-6);

        check("hysteresis keeps RETURN inside radius (0.8 > 0.5)",
                afk.tick(0.8, 0) == AfkController.Action.RETURN);
        check("HOLD once back within half the radius", afk.tick(0.4, 0) == AfkController.Action.HOLD);
        check("returning cleared after the hysteresis band", !afk.returning() && afk.atTarget());
        check("since_ticks counts every tick", afk.sinceTicks() == 5);

        // 半径默认 1 格 + 合法区间收敛
        check("default radius is 1 block", StationMemory.DEFAULT_RADIUS == 1.0);
        check("radius 0 falls back to default", StationMemory.normalizeRadius(0) == 1.0);
        check("NaN radius falls back to default", StationMemory.normalizeRadius(Double.NaN) == 1.0);
        check("radius is clamped at the top", StationMemory.normalizeRadius(999) == StationMemory.MAX_RADIUS);
        check("radius is clamped at the bottom", StationMemory.normalizeRadius(0.01) == StationMemory.MIN_RADIUS);
    }

    /** ④(续) keepalive：每几秒一次、幅度很小、左右交替；未启用时永不触发。 */
    private static void afkKeepaliveDoesNotMove() {
        AfkController afk = new AfkController();
        afk.begin(station("tower", 0, 64, 0, 1.0), true);
        for (int i = 0; i < 59; i++) {
            afk.tick(0, 0);
        }
        check("keepalive not due before the interval", !afk.keepaliveDue());
        afk.tick(0, 0);
        check("keepalive due at 3s / 60 ticks", afk.keepaliveDue());

        afk.markPulse();
        check("pulse counted", afk.pulses() == 1);
        check("not due again immediately after a pulse", !afk.keepaliveDue());
        check("pulse amplitudes are small (yaw <= 5)", Math.abs(afk.keepaliveYawDelta()) <= 5.0f);
        check("pulse amplitudes are small (pitch <= 3)", Math.abs(afk.keepalivePitchDelta()) <= 3.0f);
        float firstYaw = afk.keepaliveYawDelta();
        afk.markPulse();
        check("pulses alternate direction", afk.keepaliveYawDelta() == -firstYaw);

        AfkController quiet = new AfkController();
        quiet.begin(station("tower", 0, 64, 0, 1.0), false);
        for (int i = 0; i < 600; i++) {
            quiet.tick(0, 0);
        }
        check("keepalive off never pulses", !quiet.keepaliveDue() && quiet.pulses() == 0);

        AfkController returning = new AfkController();
        returning.begin(station("tower", 0, 64, 0, 1.0), true);
        returning.tick(5, 0); // 走出去 → RETURN
        for (int i = 0; i < 120; i++) {
            returning.tick(5, 0);
        }
        check("no keepalive while walking back (would look like fidgeting)", !returning.keepaliveDue());
    }

    /** ⑤ 同名覆盖：站点与路径都以名字为主键，重新指认不会产生第二条。 */
    private static void sameNameOverwrites() {
        StationMemory.clear();
        StationMemory.remember("tower", "mob_farm", 10, 64, 20, 90f, 5f, 1.0, 100);
        check("station saved", StationMemory.size() == 1 && StationMemory.has("tower"));
        StationMemory.remember("tower", "mob_farm", 11, 65, 21, 180f, 10f, 2.0, 200);
        check("same name overwrites instead of duplicating", StationMemory.size() == 1);
        StationMemory.Station tower = StationMemory.get("tower");
        check("overwrite keeps the latest precise xyz", tower.x() == 11 && tower.y() == 65 && tower.z() == 21);
        check("overwrite keeps the latest orientation", tower.yaw() == 180f && tower.pitch() == 10f);
        check("overwrite keeps the latest radius", tower.radius() == 2.0);
        check("kind is normalized to lowercase", StationMemory.remember("k", "MOB_FARM", 0, 0, 0, 0, 0, 1, 1).kind()
                .equals("mob_farm"));
        check("unknown kind falls back to custom", StationMemory.remember("k2", "weird", 0, 0, 0, 0, 0, 1, 1).kind()
                .equals(StationMemory.DEFAULT_KIND));
        check("blank name is rejected", StationMemory.remember("  ", "mob_farm", 0, 0, 0, 0, 0, 1, 1) == null);
        check("name is trimmed", StationMemory.has("tower") && StationMemory.has(" tower "));

        RouteMemory.clear();
        List<RouteMemory.RoutePoint> first = List.of(p(0, 64, 0), p(8, 64, 0));
        List<RouteMemory.RoutePoint> second = List.of(p(0, 64, 0), p(8, 64, 0), p(8, 64, 8));
        RouteMemory.save("home", first, 10);
        RouteMemory.save("home", second, 20);
        check("route same name overwrites", RouteMemory.size() == 1);
        check("overwrite keeps the latest points", RouteMemory.get("home").points().size() == 3);
        check("overwrite keeps the latest tick", RouteMemory.get("home").savedAtTick() == 20);
        check("forget removes a route", RouteMemory.forget("home") && !RouteMemory.has("home"));
        check("empty point list is not stored", RouteMemory.save("none", List.of(), 1) == null);
    }

    /** ⑥ 持久化字段完整性：字段表存/取往返一致，必需字段一个不少。 */
    private static void persistenceFieldsComplete() {
        StationMemory.clear();
        StationMemory.remember("tower", "mob_farm", 1.25, 63.5, -4.75, 90f, -12.5f, 1.5, 1234);
        StationMemory.remember("nether_portal", "portal", 300, 70, -220, 0f, 0f, 1.0, 1300);
        List<Map<String, Object>> stationFields = StationMemory.toFields();
        check("station field table has every station", stationFields.size() == 2);

        Set<String> stationKeys = new LinkedHashSet<>(stationFields.get(0).keySet());
        check("station fields include kind", stationKeys.contains("kind"));
        check("station fields include precise xyz",
                stationKeys.contains("x") && stationKeys.contains("y") && stationKeys.contains("z"));
        check("station fields include orientation (yaw/pitch)",
                stationKeys.contains("yaw") && stationKeys.contains("pitch"));
        check("station fields include radius and tick",
                stationKeys.contains("radius") && stationKeys.contains("saved_at_tick"));

        StationMemory.clear();
        StationMemory.restore(stationFields);
        StationMemory.Station restored = StationMemory.get("tower");
        check("station round-trip restores everything", restored != null && StationMemory.size() == 2);
        check("station round-trip keeps xyz exactly",
                restored.x() == 1.25 && restored.y() == 63.5 && restored.z() == -4.75);
        check("station round-trip keeps yaw/pitch exactly",
                restored.yaw() == 90f && restored.pitch() == -12.5f);
        check("station round-trip keeps kind/radius/tick",
                "mob_farm".equals(restored.kind()) && restored.radius() == 1.5 && restored.savedAtTick() == 1234);

        RouteMemory.clear();
        List<RouteMemory.RoutePoint> points = List.of(
                p(1.5, 64, 2.5), new RouteMemory.RoutePoint(9.25, 65, 2.5, 180f), p(9.25, 65, 10.75));
        RouteMemory.save("cave_tunnel", points, 4321);
        List<Map<String, Object>> routeFields = RouteMemory.toFields();
        check("route field table has every route", routeFields.size() == 1);
        Set<String> routeKeys = new LinkedHashSet<>(routeFields.get(0).keySet());
        check("route fields include name/points/saved_at_tick",
                routeKeys.contains("name") && routeKeys.contains("points") && routeKeys.contains("saved_at_tick"));

        @SuppressWarnings("unchecked")
        List<Map<String, Object>> pointFields = (List<Map<String, Object>>) routeFields.get(0).get("points");
        check("route stores every point", pointFields.size() == 3);
        Set<String> pointKeys = new LinkedHashSet<>(pointFields.get(1).keySet());
        check("each point carries xyz + yaw",
                pointKeys.contains("x") && pointKeys.contains("y") && pointKeys.contains("z") && pointKeys.contains("yaw"));

        RouteMemory.clear();
        RouteMemory.restore(routeFields);
        RouteMemory.Route restoredRoute = RouteMemory.get("cave_tunnel");
        check("route round-trip restores the route", restoredRoute != null && restoredRoute.points().size() == 3);
        check("route round-trip keeps every coordinate exactly",
                restoredRoute.points().get(1).x() == 9.25
                        && restoredRoute.points().get(1).y() == 65
                        && restoredRoute.points().get(1).z() == 2.5
                        && restoredRoute.points().get(1).yaw() == 180f);
        check("route round-trip keeps the tick", restoredRoute.savedAtTick() == 4321);

        // 恢复出来的路径必须能被重放（记忆 -> 重放闭环）
        RouteFollower follower = new RouteFollower();
        check("a restored route can be replayed", follower.begin("cave_tunnel", restoredRoute.points(), 1.0));
        check("replay of a restored route starts at point 0",
                follower.index() == 0 && follower.total() == 3);

        // 清理，避免影响同 JVM 内的其它自检（正常情况下每个 verify 任务各自独立进程）
        StationMemory.clear();
        RouteMemory.clear();
    }

    private static StationMemory.Station station(String name, double x, double y, double z, double radius) {
        return new StationMemory.Station(name, "mob_farm", x, y, z, 0f, 0f, radius, 0);
    }

    private static RouteMemory.RoutePoint p(double x, double y, double z) {
        return new RouteMemory.RoutePoint(x, y, z, 0f);
    }

    private static void check(String name, boolean ok) {
        if (ok) {
            passed++;
            System.out.println("  OK   " + name);
        } else {
            failed++;
            System.out.println("  FAIL " + name);
        }
    }
}
