package com.lcu.lcumod.action;

import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.List;

/**
 * 零依赖自检（接入 {@code check}）：**视角单一写者 + 寻路收敛/退避 + 非完整方块通行 + 曲线整形**。
 *
 * <p>用户实机根因："AI 在控制中、显示跟着我，但原地罚站"，日志每秒刷
 * {@code Calculated path with 47 waypoints}——后端 look 与寻路转身**两个写者互相覆盖**，
 * {@code yawDiff} 永不收敛 ⇒ 前进键永远 false。本批覆盖：
 *
 * <ol>
 *   <li><b>导航中后端 look 不覆盖路径朝向</b>（{@link LookArbiter} 优先级）；</li>
 *   <li><b>无导航时后端 look 生效</b>（绝对值、一次性）；</li>
 *   <li><b>逐步转是曲线不是跳变</b>：角速度有上限（10°/6° per tick）、角加速度有限
 *       ⇒ 加速→饱和→刹住，且**绝不越过目标**（不越过就不会在瞄准点附近抖）；</li>
 *   <li><b>曲线整形的行为断言</b>（{@link MotionCurve}）：缓入缓出端点/单调/有界/对称、
 *       角速度整形不超上限不抖、速度曲线"转弯降速 / 直线加速"、迟滞开关不来回抖、
 *       前视取点随距离变化；</li>
 *   <li><b>非完整方块的通行判据</b>（{@link Pathfinder.Pure#classifyStep}）：半砖(0.5)可迈、
 *       上半砖(1.0)需跳、楼梯两向、活板门平放可迈/立起来是墙、梯子=可爬、净空不足 ⇒ 拒绝、
 *       需要垫方块 ⇒ 拒绝（**不搭方块**策略不变）；</li>
 *   <li><b>收敛失败如实停步并回报原因</b>（{@link AlignmentGuard}）；</li>
 *   <li><b>退避期内不重复计算 / 相同结果只报一次</b>（{@link ReplanGate}）；</li>
 *   <li><b>静态扫描</b>：寻路按脚印取子形状、攀爬用原版 {@code isLadder} 判据、路点必须能站、
 *       卡住能说清"哪一格、为什么"、寻路里没有放方块、视角仍只有 {@code InputIsolation} 一个写点。</li>
 * </ol>
 *
 * <p>断言信息用 ASCII（Windows 控制台 GBK）；失败非零退出。
 */
public final class WalkCheck {

    private static int passed = 0;
    private static int failed = 0;

    public static void main(String[] args) {
        lookOwnershipChecks();
        gradualTurnChecks();
        motionCurveChecks();
        stepClassifyChecks();
        nonFullBlockChecks();
        alignmentGuardChecks();
        replanGateChecks();
        sourceScanChecks();
        comeVsFollowChecks();
        attackSafetyChecks();
        landmarkMemoryChecks();
        mobPolicyChecks();
        gearReflexChecks();
        stuckDetourChecks();
        autonomousJitterChecks();
        deathPointChecks();
        autonomousDefaultChecks();
        inputOwnershipChecks();
        inputBlockingChecks();
        mixinStaticInitChecks();
        gearReportChecks();
        perceptionTieringChecks();
        entityScanChecks();
        wireTokenArmedChecks();
        dropChannelChecks();

        System.out.println("[WalkCheck] " + passed + " checks passed, " + failed + " failed");
        if (failed > 0) {
            System.exit(1);
        }
    }

    // ── 20. 配置里的 wireToken 必须真的武装（别当摆设） ─────────────

    /**
     * 一个**什么都不做的安全开关**比没有更糟：用户以为有鉴权，其实端口对谁都开着。
     *
     * <p>实测（2026-10-12 审计）：{@code WireServer.setRequiredToken} 有定义、{@code WireAuth}
     * 有比对、hello 握手有校验，但**全仓没有调用者** ⇒ 配置项与代码谎言同谋。这里钉住三件事：
     * 启动时真的把配置值武装上、武装发生在 {@code start()} **之前**、以及武装与否都**打日志**
     * （空的要 WARN，不许静默）。
     */
    private static void wireTokenArmedChecks() {
        String mod = read(Path.of("src", "main", "java", "com", "lcu", "lcumod", "LCUMod.java"));
        String server = read(Path.of("src", "main", "java", "com", "lcu", "lcumod", "network",
                "WireServer.java"));
        String collector = read(Path.of("src", "main", "java", "com", "lcu", "lcumod", "state",
                "StateCollector.java"));
        if (mod.isEmpty() || server.isEmpty() || collector.isEmpty()) {
            check("wire-token: sources found", false);
            return;
        }

        int armed = mod.indexOf("armWireToken();");
        int started = mod.indexOf("WIRE.start()");
        check("wire-token: startup arms the token from config",
                armed > 0 && mod.contains("ModConfig.WIRE_TOKEN"));
        check("wire-token: armed before the server starts listening",
                armed > 0 && started > 0 && armed < started);
        check("wire-token: an armed token is reported (length only, never the token itself)",
                mod.contains("tokenArmed()") && mod.contains("Wire auth armed"));
        check("wire-token: an empty token is warned about, not silently accepted",
                mod.contains("LOGGER.warn") && mod.contains("No wireToken set"));
        check("wire-token: the config read is guarded (a not-yet-loaded config must not crash startup)",
                mod.contains("catch (RuntimeException notReady)"));
        check("wire-token: there is a later retry from our own tick, before any backend connection",
                mod.contains("armWireTokenIfNeeded()") && collector.contains("armWireTokenIfNeeded()")
                        && collector.contains("ClientTickEvent"));
        check("wire-token: the server really has an armed-state accessor",
                server.contains("setRequiredToken(") && server.contains("tokenArmed()")
                        && server.contains("requiredToken.isBlank()"));
        check("wire-token: blank means no auth (old behaviour kept for loopback)",
                server.contains("volatile boolean authed = requiredToken.isBlank()"));
    }


    // ── 19. mixin 的静态初始化不许炸（真机启动崩溃回归） ────────────

    /**
     * 真机事故（2026-10-12 15:13，用户实例**启动即崩**）：
     *
     * <pre>
     * java.lang.IllegalArgumentException: duplicate element: 41
     *   at java.util.Set.of(Set.java:565)
     *   at net.minecraft.client.KeyboardHandler.&lt;clinit&gt;(KeyboardHandler.java:777)
     *     {neoforge:mixin[APP:lcumod.mixins.json:KeyboardHandlerMixin from mod lcumod]}
     * </pre>
     *
     * <p>病根：mixin 里的 {@code static final Set<Integer> ALWAYS_ALLOWED = Set.of(…)}。{@code Set.of}
     * 对**重复元素**是抛异常的，而 mixin 的静态初始化会被**合并进目标类的 {@code <clinit>}** ——
     * 于是异常发生在 {@code KeyboardHandler.<clinit>}、时间点是 {@code Minecraft.<init>}，
     * 表现就是"游戏打不开"。而重复是**必然**的：javap 实证
     * {@code InputConstants.KEY_ESCAPE = SDLScancode.SDL_SCANCODE_ESCAPE = 41}、
     * {@code InputConstants.KEY_F12 = SDLScancode.SDL_SCANCODE_F12 = 69}
     * —— 所谓"三种编码都收"里有两组本来就是同一个值。
     *
     * <p>所以：① mixin 里不许出现 {@code Set.of}；② 常量集合一律走 {@link KeyShield#allowed}
     * （去重，永不抛）；③ 重复值要能被 {@link KeyShield#duplicateCodes} 报出来。
     */
    private static void mixinStaticInitChecks() {
        java.util.Set<Integer> deduped = KeyShield.allowed(41, 41, 69, 69, 27);
        check("key-shield: duplicate key codes are deduped instead of throwing", deduped.size() == 3);
        check("key-shield: the deduped set still has the real codes",
                deduped.contains(41) && deduped.contains(69) && deduped.contains(27));
        check("key-shield: the duplicate report names both real collisions",
                KeyShield.duplicateCodes(41, 41, 69, 69, 27).equals(java.util.List.of(41, 69)));
        check("key-shield: nothing duplicated reports nothing",
                KeyShield.duplicateCodes(256, 41, 301, 69, 27).isEmpty());
        check("key-shield: an empty set is allowed", KeyShield.allowed().isEmpty());
        check("key-shield: null input is tolerated", KeyShield.allowed((int[]) null).isEmpty());

        // 静态：mixin 里**不许**再出现 Set.of（那正是这次启动崩溃的写法）
        Path dir = Path.of("src", "main", "java", "com", "lcu", "lcumod", "mixin");
        java.util.List<Path> files = new java.util.ArrayList<>();
        try (java.util.stream.Stream<Path> stream = Files.list(dir)) {
            stream.filter(path -> path.toString().endsWith(".java")).forEach(files::add);
        } catch (Exception e) {
            check("mixin-static: mixin sources listed", false);
            return;
        }
        check("mixin-static: mixin sources listed", !files.isEmpty());
        boolean anyOfFactory = false;
        boolean usesShield = false;
        for (Path file : files) {
            String text = read(file);
            // `Set.of`/`Map.of` 对**重复元素**抛异常（`List.of` 不会，列表本来就允许重复）。
            if (text.contains("Set.of(") || text.contains("Map.of(")) {
                anyOfFactory = true;
            }
            if (text.contains("KeyShield.allowed(")) {
                usesShield = true;
            }
        }
        check("mixin-static: no mixin builds a set/map with of() (duplicates crash the target's <clinit>)",
                !anyOfFactory);
        check("mixin-static: the always-allowed keys go through KeyShield.allowed", usesShield);
    }

    // ── 1/2. 视角所有权：导航 > 后端 look > 不写 ──────────────────

    private static void lookOwnershipChecks() {
        // ① 导航中：后端 look 绝不覆盖路径朝向
        LookArbiter a = new LookArbiter();
        a.setBackendTarget(90f, 45f);
        a.setNavigationTarget(-90f, 30f);
        LookArbiter.Frame f = a.plan(true, true, 0f, 0f);
        check("ownership: navigating -> NAVIGATION owns view", f.source() == LookArbiter.Source.NAVIGATION);
        check("ownership: navigating -> backend yaw NOT applied", f.yaw() != 90f);
        check("ownership: navigating -> backend pitch NOT applied", f.pitch() != 45f);
        check("ownership: navigating -> turns toward path (negative, behind the cap)",
                f.yaw() < 0f && f.yaw() > -LookArbiter.NAV_MAX_YAW_STEP);
        check("ownership: navigating -> pitch turns up, within the cap",
                f.pitch() > 0f && f.pitch() <= LookArbiter.NAV_MAX_PITCH_STEP);
        // 曲线整形：第一 tick 是"加速"阶段的步长，比角速度上限小；第二 tick 更大（角加速度有限但方向一致）。
        LookArbiter.Frame second = a.plan(true, true, f.yaw(), f.pitch());
        check("ownership: navigating -> turn accelerates (2nd tick step > 1st)",
                Math.abs(second.yaw() - f.yaw()) > Math.abs(f.yaw() - 0f));

        // ② 无导航：后端 look 生效（绝对值，一次性）
        LookArbiter b = new LookArbiter();
        b.setBackendTarget(90f, 10f);
        LookArbiter.Frame back = b.plan(true, false, 0f, 0f);
        check("ownership: no navigation -> BACKEND owns view", back.source() == LookArbiter.Source.BACKEND);
        check("ownership: backend look is absolute", back.yaw() == 90f && back.pitch() == 10f);
        b.setNavigationTarget(-90f, 0f);
        check("ownership: backend still owns while not navigating",
                b.plan(true, false, 0f, 0f).source() == LookArbiter.Source.BACKEND);
        check("ownership: nav target kicks in once navigating",
                b.plan(true, true, 0f, 0f).source() == LookArbiter.Source.NAVIGATION);
        b.clearNavigationTarget();
        check("ownership: clearing nav target hands view back to backend",
                b.plan(true, true, 0f, 0f).source() == LookArbiter.Source.NONE
                        && b.plan(true, false, 0f, 0f).source() == LookArbiter.Source.BACKEND);

        // ③ AI 不持控制时一律不写（即使两个目标都在）
        LookArbiter c = new LookArbiter();
        c.setBackendTarget(90f, 45f);
        c.setNavigationTarget(-90f, 30f);
        LookArbiter.Frame none = c.plan(false, true, 12f, -7f);
        check("ownership: no AI control -> NONE", none.source() == LookArbiter.Source.NONE);
        check("ownership: no AI control -> nothing written", !none.wrote());
        check("ownership: no AI control -> yaw/pitch untouched",
                none.yaw() == 12f && none.pitch() == -7f);

        // 导航中但还没发布路径目标 -> 也不许让后端 look 抢（宁可本 tick 不写）
        LookArbiter d = new LookArbiter();
        d.setBackendTarget(90f, 0f);
        LookArbiter.Frame first = d.plan(true, true, 0f, 0f);
        check("ownership: navigating without nav target -> NONE (no backend hijack)",
                first.source() == LookArbiter.Source.NONE && !first.wrote());

        // 兜底：BACKEND 俯仰夹到 ±90；清空后什么也不写
        LookArbiter e = new LookArbiter();
        e.setBackendTarget(0f, 200f);
        check("ownership: backend pitch clamped to +90", e.plan(true, false, 0f, 0f).pitch() == 90f);
        e.setBackendTarget(0f, -200f);
        check("ownership: backend pitch clamped to -90", e.plan(true, false, 0f, 0f).pitch() == -90f);
        e.clearBackendTarget();
        check("ownership: no targets -> NONE", e.plan(true, false, 0f, 0f).source() == LookArbiter.Source.NONE);

        LookArbiter g = new LookArbiter();
        g.setBackendTarget(1f, 2f);
        g.setNavigationTarget(3f, 4f);
        g.clearAll();
        check("ownership: clearAll clears both", !g.hasBackendTarget() && !g.hasNavigationTarget());
    }

    // ── 3. 逐步转：不会瞬间对齐，曲线加速→饱和→刹住，且能收敛 ──────

    /**
     * 用户口径："视角转动……至少不要那么锯齿"。所以这里钉的是**曲线**而不是"每 tick 固定步长"：
     * 第一 tick 一定**小于**角速度上限（在加速），中途会**饱和**到上限，收尾会**减速**，
     * 且**绝不越过**目标（越过就会在瞄准点附近来回抖 = 锯齿）。
     */
    private static void gradualTurnChecks() {
        LookArbiter a = new LookArbiter();
        a.setNavigationTarget(180f, 45f);
        LookArbiter.Frame f = a.plan(true, true, 0f, 0f);
        check("gradual: 180deg target is NOT snapped (1st tick is an accelerating step)",
                f.yaw() > 0f && f.yaw() < LookArbiter.NAV_MAX_YAW_STEP);
        check("gradual: yaw step never exceeds the rate cap",
                f.yaw() - 0f <= LookArbiter.NAV_MAX_YAW_STEP);
        check("gradual: pitch step never exceeds the rate cap",
                f.pitch() > 0f && f.pitch() <= LookArbiter.NAV_MAX_PITCH_STEP);

        float yaw = 0f;
        float pitch = 0f;
        int ticks = 0;
        double peakYawStep = 0.0;
        double lastYawStep = 0.0;
        boolean sawAccelerate = false;
        boolean sawBrake = false;
        boolean overshot = false;
        double previousAbsError = 180.0;
        LookArbiter sim = new LookArbiter();
        sim.setNavigationTarget(180f, 45f);
        while (LookArbiter.yawDiff(yaw, 180f) != 0f && ticks < 200) {
            LookArbiter.Frame step = sim.plan(true, true, yaw, pitch);
            if (Math.abs(LookArbiter.yawDiff(yaw, step.yaw())) > LookArbiter.NAV_MAX_YAW_STEP + 0.001f) {
                check("gradual: step never exceeds cap (tick " + ticks + ")", false);
                return;
            }
            double moved = step.yaw() - yaw;
            double absError = Math.abs(LookArbiter.yawDiff(step.yaw(), 180f));
            if (Math.abs(moved) > 0.0 && Math.abs(moved) < LookArbiter.NAV_MAX_YAW_STEP - 1.0e-4) {
                if (ticks > 0) {
                    // 前半段步长变大 ⇒ 在加速；后半段变小 ⇒ 在刹车
                    if (Math.abs(moved) > lastYawStep) {
                        sawAccelerate = true;
                    } else {
                        sawBrake = true;
                    }
                }
            }
            if (absError > previousAbsError + 1.0e-6 || step.yaw() > 180f) {
                overshot = true;   // 越过目标（会抖）
            }
            previousAbsError = absError;
            lastYawStep = Math.abs(moved);
            peakYawStep = Math.max(peakYawStep, Math.abs(moved));
            yaw = step.yaw();
            pitch = step.pitch();
            ticks++;
        }
        check("gradual: converges within 30 ticks (got " + ticks + ")", ticks > 0 && ticks <= 30);
        check("gradual: the turn accelerates (starts below the cap)", sawAccelerate);
        check("gradual: the turn brakes before the target", sawBrake);
        check("gradual: saturates at the rate cap", Math.abs(peakYawStep - LookArbiter.NAV_MAX_YAW_STEP) < 1.0e-4);
        check("gradual: never overshoots the target (no jitter around it)", !overshot);
        check("gradual: pitch converges to 45", Math.abs(pitch - 45f) < 0.001f);
        check("gradual: once aligned it stays put",
                sim.plan(true, true, yaw, pitch).yaw() == yaw);

        // 微小偏差：直接落到目标且**不再动**（死区 ⇒ 不抖）
        LookArbiter micro = new LookArbiter();
        micro.setNavigationTarget(0.02f, 0f);
        LookArbiter.Frame m = micro.plan(true, true, 0f, 0f);
        check("gradual: micro error snaps to the target (no hunting)", m.yaw() == 0.02f);
        check("gradual: micro error does not keep turning",
                micro.plan(true, true, m.yaw(), 0f).yaw() == 0.02f);

        // 跨 ±180 边界：170 -> -170 只差 20 度，往正方向转
        LookArbiter wrap = new LookArbiter();
        wrap.setNavigationTarget(-170f, 0f);
        LookArbiter.Frame wf = wrap.plan(true, true, 170f, 0f);
        check("gradual: crosses the +-180 seam the short way", wf.yaw() > 170f && wf.yaw() <= 180f);
        check("gradual: seam diff is 20 not 340", wf.yawDiff() == 20f);

        // yawDiff 纯函数
        check("yawDiff: 0 -> 350 is -10", LookArbiter.yawDiff(0f, 350f) == -10f);
        check("yawDiff: 350 -> 10 is +20", LookArbiter.yawDiff(350f, 10f) == 20f);
        check("yawDiff: 0 -> 190 is -170", LookArbiter.yawDiff(0f, 190f) == -170f);
        check("yawDiff: 0 -> -190 is +170", LookArbiter.yawDiff(0f, -190f) == 170f);
        check("yawDiff: 0 -> 720 normalizes to 0", LookArbiter.yawDiff(0f, 720f) == 0f);
        check("yawDiff: 180 is kept (range is (-180,180])", LookArbiter.yawDiff(0f, 180f) == 180f);
    }

    // ── 4. 收敛保护：转不过去就如实停步 ──────────────────────────

    private static void alignmentGuardChecks() {
        AlignmentGuard g = new AlignmentGuard(5);
        boolean fired = false;
        for (int i = 0; i < 5; i++) {
            fired |= g.tick(false);
        }
        check("align: 5 unaligned ticks are still tolerated", !fired);
        check("align: stalledTicks counted", g.stalledTicks() == 5);
        check("align: 6th unaligned tick -> stall reported", g.tick(false));
        check("align: per-tick reporting is monotone", g.tick(false));

        AlignmentGuard h = new AlignmentGuard(5);
        for (int i = 0; i < 4; i++) {
            h.tick(false);
        }
        check("align: getting aligned clears the counter", !h.tick(true) && h.stalledTicks() == 0);
        check("align: after clearing it tolerates again", !h.tick(false));
        h.reset();
        check("align: reset clears stalledTicks", h.stalledTicks() == 0);
        check("align: timeout exposed", g.timeoutTicks() == 5);

        boolean threw = false;
        try {
            new AlignmentGuard(0);
        } catch (IllegalArgumentException ex) {
            threw = true;
        }
        check("align: ctor rejects timeout < 1", threw);
    }

    // ── 5. 退避 + 去重 ───────────────────────────────────────────

    private static void replanGateChecks() {
        ReplanGate gate = new ReplanGate(1000);
        check("backoff: first replan allowed", gate.allow("1,64,2", 0));
        check("backoff: same target inside window denied", !gate.allow("1,64,2", 999));
        check("backoff: same target at window edge allowed", gate.allow("1,64,2", 1000));
        check("backoff: window restarts from the allowed time", !gate.allow("1,64,2", 1999));
        check("backoff: different target allowed immediately", gate.allow("9,70,9", 1999));
        check("backoff: remainingMs reports full window right after allow",
                gate.remainingMs("9,70,9", 1999) == 1000);
        check("backoff: remainingMs reports the tail", gate.remainingMs("9,70,9", 2500) == 499);
        check("backoff: unknown key has no wait", gate.remainingMs("other", 2500) == 0);

        ReplanGate zero = new ReplanGate(0);
        check("backoff: zero window never blocks", zero.allow("k", 0) && zero.allow("k", 0));

        check("dedupe: first result reported", gate.allowOnce("path:1,64,2#47"));
        check("dedupe: identical result suppressed", !gate.allowOnce("path:1,64,2#47"));
        check("dedupe: different result reported", gate.allowOnce("path:1,64,2#46"));
        check("dedupe: failure reason reported once", gate.allowOnce("fail:x#no path") && !gate.allowOnce("fail:x#no path"));

        gate.reset();
        check("dedupe: reset re-allows the same result", gate.allowOnce("path:1,64,2#47"));
        check("backoff: reset clears the window", gate.allow("1,64,2", 0));

        boolean threw = false;
        try {
            new ReplanGate(-1);
        } catch (IllegalArgumentException ex) {
            threw = true;
        }
        check("backoff: ctor rejects negative window", threw);
    }

    // ── 2b. 曲线整形：移动/视角不许 0/1 突变、不许锯齿 ────────────

    /**
     * 用户口径："移动路径还有视角转动，能不能再做平滑一点……至少不要那么锯齿"、
     * "有些时候是加速曲线，有些时候是弧形的"。这里对 {@link MotionCurve} 的**行为**做断言
     * （零依赖纯逻辑，所以能直接断言而不是扫源码）。
     */
    private static void motionCurveChecks() {
        // ① 缓入缓出：端点精确 / 单调 / 有界 / 对称 / 两端导数为 0
        check("curve: easeInOut endpoints are exact",
                MotionCurve.easeInOut(0.0) == 0.0 && MotionCurve.easeInOut(1.0) == 1.0);
        check("curve: easeInOut clamps outside [0,1]",
                MotionCurve.easeInOut(-3.0) == 0.0 && MotionCurve.easeInOut(3.0) == 1.0);

        boolean monotone = true;
        boolean bounded = true;
        boolean symmetric = true;
        double previous = -1.0;
        for (int i = 0; i <= 100; i++) {
            double t = i / 100.0;
            double v = MotionCurve.easeInOut(t);
            if (v < previous - 1.0e-12) {
                monotone = false;
            }
            if (v < 0.0 || v > 1.0) {
                bounded = false;
            }
            if (Math.abs(v + MotionCurve.easeInOut(1.0 - t) - 1.0) > 1.0e-9) {
                symmetric = false;
            }
            previous = v;
        }
        check("curve: easeInOut is monotone", monotone);
        check("curve: easeInOut is bounded to [0,1]", bounded);
        check("curve: easeInOut is symmetric (slow-in == slow-out)", symmetric);
        check("curve: easeInOut has zero slope at both ends",
                MotionCurve.easeInOut(0.001) < 0.005 && (1.0 - MotionCurve.easeInOut(0.999)) < 0.005);

        // ② 角速度整形：加速 → 饱和 → 刹住；不超上限、不越过目标、到点不抖
        MotionCurve.AngularShaper shaper = new MotionCurve.AngularShaper(10.0, 4.0);
        double error = 180.0;
        double peak = 0.0;
        double lastStep = 0.0;
        int ticks = 0;
        boolean accelerating = false;
        boolean braking = false;
        boolean overCap = false;
        boolean overshoot = false;
        while (error != 0.0 && ticks < 200) {
            double step = shaper.step(error);
            if (Math.abs(step) > 10.0 + 1.0e-9) {
                overCap = true;
            }
            double remaining = error - step;
            if (Math.signum(remaining) != Math.signum(error) && remaining != 0.0) {
                overshoot = true;
            }
            if (ticks > 0 && Math.abs(step) > 1.0e-9 && Math.abs(step) < 10.0 - 1.0e-9) {
                if (Math.abs(step) > lastStep) {
                    accelerating = true;
                } else {
                    braking = true;
                }
            }
            peak = Math.max(peak, Math.abs(step));
            lastStep = Math.abs(step);
            error = remaining;
            ticks++;
        }
        check("shaper: converges (got " + ticks + " ticks)", error == 0.0 && ticks > 0 && ticks <= 30);
        check("shaper: rate cap is never exceeded", !overCap);
        check("shaper: never overshoots the target", !overshoot);
        check("shaper: accelerates first (finite angular acceleration)", accelerating);
        check("shaper: saturates at the rate cap", Math.abs(peak - 10.0) < 1.0e-9);
        check("shaper: brakes before arriving", braking);
        check("shaper: stops dead once settled (no jitter)",
                shaper.step(0.0) == 0.0 && shaper.rate() == 0.0);
        check("shaper: inside the dead zone it snaps to the target",
                shaper.step(0.01) == 0.01 && shaper.rate() == 0.0);
        shaper.reset();
        check("shaper: reset clears the rate", shaper.rate() == 0.0);

        boolean threw = false;
        try {
            new MotionCurve.AngularShaper(0.0, 4.0);
        } catch (IllegalArgumentException ex) {
            threw = true;
        }
        check("shaper: rejects a zero rate cap", threw);

        // ③ 速度曲线：偏差大降速、直线远距离加速、始终有界
        check("speed: straight line far away is full speed",
                Math.abs(MotionCurve.speedScale(0.0, 10.0) - 1.0) < 1.0e-9);
        check("speed: big heading error means low speed",
                MotionCurve.speedScale(60.0, 10.0) < MotionCurve.speedScale(10.0, 10.0));
        check("speed: very close range is slower (no 0/1 step)",
                MotionCurve.speedScale(0.0, 0.5) > 0.0
                        && MotionCurve.speedScale(0.0, 0.5) < MotionCurve.speedScale(0.0, 10.0));
        boolean speedBounded = true;
        boolean speedMonotoneInTurn = true;
        double previousSpeed = Double.MAX_VALUE;
        for (int i = 0; i <= 90; i++) {
            double v = MotionCurve.speedScale(i, 8.0);
            if (v < 0.0 || v > 1.0) {
                speedBounded = false;
            }
            if (v > previousSpeed + 1.0e-12) {
                speedMonotoneInTurn = false;
            }
            previousSpeed = v;
        }
        check("speed: always bounded to [0,1]", speedBounded);
        check("speed: monotone slower as the heading error grows", speedMonotoneInTurn);

        // ④ 迟滞：阈值附近不来回抖
        MotionCurve.Hysteresis gate = new MotionCurve.Hysteresis(0.9, 0.7);
        check("hysteresis: stays off just below the on-threshold", !gate.update(0.89));
        check("hysteresis: turns on at the on-threshold", gate.update(0.9));
        check("hysteresis: stays on inside the band (no thrash)", gate.update(0.75) && gate.update(0.71));
        check("hysteresis: turns off below the off-threshold", !gate.update(0.69));
        gate.reset();
        check("hysteresis: reset clears it", !gate.state());
        boolean threwGate = false;
        try {
            new MotionCurve.Hysteresis(0.5, 0.5);
        } catch (IllegalArgumentException ex) {
            threwGate = true;
        }
        check("hysteresis: rejects off >= on", threwGate);

        // ⑤ 前视取点（弧形）：远距离看更远、目标更近就看目标本身
        MotionCurve.Point from = new MotionCurve.Point(0.0, 64.0, 0.0);
        MotionCurve.Point to = new MotionCurve.Point(10.0, 64.0, 0.0);
        MotionCurve.Point aheadPoint = MotionCurve.lookAhead(from, to, 3.0);
        check("lookAhead: takes a point ahead along the direction",
                Math.abs(aheadPoint.x() - 3.0) < 1.0e-9 && Math.abs(aheadPoint.y() - 64.0) < 1.0e-9);
        check("lookAhead: a nearer target is not overshot",
                MotionCurve.lookAhead(from, new MotionCurve.Point(1.0, 64.0, 0.0), 3.0).x() == 1.0);
        check("lookAhead: distance scales with the range (far looks farther)",
                MotionCurve.lookAheadDistance(20.0, 2.0, 6.0) > MotionCurve.lookAheadDistance(3.0, 2.0, 6.0));
        check("lookAhead: distance is clamped to [min,max]",
                MotionCurve.lookAheadDistance(1.0, 2.0, 6.0) == 2.0
                        && MotionCurve.lookAheadDistance(1000.0, 2.0, 6.0) == 6.0);
    }

    // ── 2c. 非完整方块的通行判据（纯逻辑，直接断言规则）──────────

    /**
     * 用户口径："在一些非完整方块上，比如说是楼梯，梯子，活板门，半砖……有时候能移动，有时候不能"。
     * 这里把每条规则钉成**行为断言**（{@link Pathfinder.Pure#classifyStep} 是零依赖纯逻辑）。
     */
    private static void stepClassifyChecks() {
        Pathfinder.Pure.StepQuery half = query(0.0, 0.5, 3.0);
        check("step: half slab (0.5) is walked", step(half) == Pathfinder.Pure.Move.WALK);

        Pathfinder.Pure.StepQuery upperSlab = query(0.0, 1.0, 3.0);
        check("step: upper slab (1.0) needs a jump, not a walk",
                step(upperSlab) == Pathfinder.Pure.Move.JUMP_UP);

        Pathfinder.Pure.StepQuery carpet = query(64.0, 64.0625, 3.0);
        check("step: carpet (1/16) is walked", step(carpet) == Pathfinder.Pure.Move.WALK);

        // 楼梯两向：上行是"迈"（靠来向那半块的脚下剖面），下行是"落"（允许，摔不死）
        Pathfinder.Pure.StepQuery stairUp = query(64.0, 64.5, 3.0);
        check("step: stair ascending (entry band 0.5) is walked",
                step(stairUp) == Pathfinder.Pure.Move.WALK);
        Pathfinder.Pure.StepQuery stairDown = query(65.0, 64.5, 3.0);
        check("step: stair descending (0.5 drop) is a drop, allowed",
                step(stairDown) == Pathfinder.Pure.Move.DROP_DOWN);

        Pathfinder.Pure.StepQuery trapdoorFlat = query(64.0, 64.1875, 3.0);
        check("step: closed trapdoor (flat, 0.1875) is walked",
                step(trapdoorFlat) == Pathfinder.Pure.Move.WALK);
        Pathfinder.Pure.StepQuery trapdoorUp = new Pathfinder.Pure.StepQuery(
                64.0, 64.1875, 3.0, false, false, 0.0, false);
        check("step: raised trapdoor (footprint sweep blocked) is refused",
                !trapdoorUp.corridorClear()
                        && step(trapdoorUp) == Pathfinder.Pure.Move.REFUSE
                        && reason(trapdoorUp).contains("path blocked"));

        Pathfinder.Pure.StepQuery ladder = new Pathfinder.Pure.StepQuery(
                64.0, 64.0, 3.0, false, true, 3.0, true);
        check("step: ladder/climbable is CLIMB", step(ladder) == Pathfinder.Pure.Move.CLIMB);
        Pathfinder.Pure.StepQuery loneVine = new Pathfinder.Pure.StepQuery(
                64.0, 64.0, 3.0, false, true, 0.0, true);
        check("step: climbable with no reachable rise -> refused as 'CLIMB node, no ladder'",
                step(loneVine) == Pathfinder.Pure.Move.REFUSE
                        && reason(loneVine).contains("CLIMB node, no ladder"));

        Pathfinder.Pure.StepQuery noHeadroomStep = query(0.0, 0.5, 1.5);
        check("step: not enough headroom -> refused",
                step(noHeadroomStep) == Pathfinder.Pure.Move.REFUSE
                        && reason(noHeadroomStep).contains("no headroom"));
        Pathfinder.Pure.StepQuery jumpNoHeadroom = query(0.0, 1.0, 1.5);
        check("step: 1.0 step with no headroom -> 'needs jump, no headroom'",
                step(jumpNoHeadroom) == Pathfinder.Pure.Move.REFUSE
                        && reason(jumpNoHeadroom).contains("step 1.00 needs jump, no headroom"));

        Pathfinder.Pure.StepQuery tooHigh = query(64.0, 66.5, 4.0);
        check("step: 2.5 up is too high to jump -> refused",
                step(tooHigh) == Pathfinder.Pure.Move.REFUSE
                        && reason(tooHigh).contains("too high"));
        Pathfinder.Pure.StepQuery tooDeep = query(70.0, 64.0, 4.0);
        check("step: 6.0 drop is too deep -> refused",
                step(tooDeep) == Pathfinder.Pure.Move.REFUSE
                        && reason(tooDeep).contains("too deep"));

        Pathfinder.Pure.StepQuery noSupport = query(64.0, Double.NaN, 0.0);
        check("step: unsupported neighbour needs a block -> refused (no bridging/pillaring)",
                step(noSupport) == Pathfinder.Pure.Move.REFUSE
                        && reason(noSupport).contains("no support")
                        && reason(noSupport).contains("bridging"));

        // 从攀爬柱上出到上一层：起点脚底已经按"能到的最高位置"取（见 Pathfinder.climbAwareFromFoot），
        // 所以"梯子顶端再跨一步"就是普通一步；柱子够不到的高处照样拒绝。
        Pathfinder.Pure.StepQuery climbExit = new Pathfinder.Pure.StepQuery(
                68.0, 69.0, 3.0, true, false, 1.0, true);
        check("step: stepping off the top of a climb column is a normal step",
                step(climbExit) == Pathfinder.Pure.Move.JUMP_UP);
        Pathfinder.Pure.StepQuery climbTooFar = new Pathfinder.Pure.StepQuery(
                68.0, 72.0, 3.0, true, false, 1.0, true);
        check("step: climbing higher than the ladder reaches is refused",
                step(climbTooFar) == Pathfinder.Pure.Move.REFUSE
                        && reason(climbTooFar).contains("climb exit"));

        check("step: a refused step always carries a machine-readable reason",
                !reason(tooHigh).isEmpty() && !reason(tooDeep).isEmpty() && !reason(noSupport).isEmpty());
        check("step: an allowed step carries no reason",
                reason(half).isEmpty() && reason(ladder).isEmpty());
        check("step: player height is the vanilla 1.8", Pathfinder.Pure.PLAYER_HEIGHT == 1.8);
        check("step: thresholds match the documented policy",
                Pathfinder.Pure.STEP_UP == 0.6 && Pathfinder.Pure.MAX_JUMP == 1.2
                        && Pathfinder.Pure.MAX_DROP == 3.2);
    }

    private static Pathfinder.Pure.StepQuery query(double fromFoot, double toFoot, double headroom) {
        return new Pathfinder.Pure.StepQuery(fromFoot, toFoot, headroom, false, false, 0.0, true);
    }

    private static Pathfinder.Pure.Move step(Pathfinder.Pure.StepQuery q) {
        return Pathfinder.Pure.classifyStep(q).move();
    }

    private static String reason(Pathfinder.Pure.StepQuery q) {
        return Pathfinder.Pure.classifyStep(q).reason();
    }

    // ── 2d. 非完整方块的实现形状（源码扫描）──────────────────────

    /**
     * 判据必须在**脚印内**求、攀爬必须用**原版判据**、路点必须过"能不能站"、卡住必须能说清
     * 哪一格为什么、视角仍然只有一个写者、寻路里绝不出现放方块。
     */
    private static void nonFullBlockChecks() {
        Path root = Path.of("src", "main", "java", "com", "lcu", "lcumod", "action");
        String pf = read(root.resolve("Pathfinder.java"));
        String look = read(root.resolve("LookArbiter.java"));
        String curve = read(root.resolve("MotionCurve.java"));
        String ii = read(root.resolve("InputIsolation.java"));
        if (pf.isEmpty() || look.isEmpty() || curve.isEmpty() || ii.isEmpty()) {
            check("footprint: sources found", false);
            return;
        }

        check("footprint: stand height comes from the player's footprint sub-shape",
                pf.contains("forAllBoxes(") && pf.contains("PLAYER_HALF_WIDTH")
                        && pf.contains("footprintTopLocal("));
        check("footprint: headroom is measured inside the footprint too",
                pf.contains("headroomAbove("));
        check("footprint: entry band per direction (stairs step onto the near half)",
                pf.contains("bandsAt(") && pf.contains("REVERSE_DIRECTION"));
        check("footprint: the sweep along the step is a real collision test",
                pf.contains("corridorClear(") && pf.contains("noCollision("));
        check("footprint: climbable comes from the vanilla criterion (BlockState.isLadder)",
                pf.contains("isLadder(") && !pf.contains("Blocks.LADDER"));
        check("footprint: climb columns are nodes with a reachable top",
                pf.contains("climbColumnTop(") && pf.contains("Move.CLIMB"));
        check("footprint: climbing is vertical (same column), not a horizontal hop",
                pf.contains("addVerticalNeighbor(") && pf.contains("classifyVertical(")
                        && pf.contains("climbAwareFromFoot("));
        check("footprint: a purely vertical waypoint still presses climb keys",
                pf.contains("horizontalDistance < 0.01") && pf.contains("waypoint.needsJump()"));
        check("footprint: every waypoint is a standable node (headroom-confirmed)",
                pf.contains("createStandingNode(") && pf.contains("hasHeadroom(level, player, standingPos)"));
        check("footprint: one single classifier drives walk/jump/drop/climb",
                pf.contains("Pure.classifyStep("));

        // 卡住必须说清"哪一格、为什么"，并进现有 stuck 上报文案
        check("diagnosis: pathfinder keeps the nearest refusal with its cell",
                pf.contains("getLastBlockedReport()") && pf.contains("refusalCell"));
        check("diagnosis: the failure reason carries the blocked cell + reason",
                pf.contains("blocked.isEmpty() ? \"\" : \"; \" + blocked"));
        check("diagnosis: being stuck re-judges the current step and says why",
                pf.contains("explainProgressBlock(") && pf.contains("no progress -> blocked by something else"));
        check("diagnosis: the stuck watch dog receives it (getLastFailureReason)",
                pf.contains("getLastFailureReason()"));

        // 纪律：不搭方块 / 视角单一写者 / 迈跳落爬由分类驱动
        check("discipline: no block placing in the pathfinder",
                !pf.contains("setBlock") && !pf.contains("place_block") && !pf.contains("useItemOn"));
        check("discipline: no-rescue policy kept", pf.contains("NO_RESCUE_POLICY"));
        check("discipline: jump is driven by the classification, not by a probe hack",
                pf.contains("waypoint.needsJump()") && !pf.contains("shouldJump"));
        check("discipline: the pathfinder still publishes look via the single writer",
                pf.contains("setNavigationLookTarget") && !pf.contains("setYRot") && !pf.contains("setXRot"));
        check("discipline: the arbiter/curve never write the view themselves",
                !look.contains("setYRot") && !look.contains("setXRot")
                        && !curve.contains("setYRot") && !curve.contains("setXRot"));
        check("discipline: only InputIsolation writes yaw/pitch",
                countOf(ii, "mc.player.setYRot(") == 1 && countOf(ii, "mc.player.setXRot(") == 1);

        // 曲线必须真的用在移动/视角上（不是"写了没用"）
        check("curve: navigation turning goes through the shaper",
                look.contains("MotionCurve.AngularShaper") && look.contains("yawShaper.step("));
        check("curve: movement speed comes from the speed curve + hysteresis",
                pf.contains("MotionCurve.speedScale(") && pf.contains("MotionCurve.Hysteresis"));
        check("curve: the view aims ahead along the path (arcs, not right-angle turns)",
                pf.contains("lookAheadPoint(") && pf.contains("MotionCurve.lookAheadDistance(")
                        && pf.contains("MotionCurve.lookAhead(") && pf.contains("walkAlongPath("));
    }

    // ── 6. 静态扫描：单一写者 ────────────────────────────────────

    private static void sourceScanChecks() {
        Path root = Path.of("src", "main", "java", "com", "lcu", "lcumod", "action");
        Path pathfinder = root.resolve("Pathfinder.java");
        Path inputIsolation = root.resolve("InputIsolation.java");
        if (!Files.isRegularFile(pathfinder) || !Files.isRegularFile(inputIsolation)) {
            check("scan: sources found (cwd=" + Path.of(".").toAbsolutePath() + ")", false);
            return;
        }

        String pf = read(pathfinder);
        String ii = read(inputIsolation);

        check("scan: Pathfinder never writes yaw directly", !pf.contains("setYRot"));
        check("scan: Pathfinder never writes pitch directly", !pf.contains("setXRot"));
        check("scan: Pathfinder publishes via the single writer", pf.contains("setNavigationLookTarget"));
        check("scan: Pathfinder clears nav look target when it stops", pf.contains("clearNavigationLookTarget"));
        check("scan: Pathfinder uses the convergence guard", pf.contains("ALIGNMENT.tick("));
        check("scan: Pathfinder uses the replan backoff", pf.contains("REPLAN.allow("));
        check("scan: Pathfinder keeps the alignment intent (65deg gate)",
                pf.contains("ALIGN_TO_WALK_DEG") && pf.contains("65.0f"));
        check("scan: Pathfinder keeps the no-rescue policy", pf.contains("NO_RESCUE_POLICY"));

        check("scan: InputIsolation is the only writer (yaw)", countOf(ii, "mc.player.setYRot(") == 1);
        check("scan: InputIsolation is the only writer (pitch)", countOf(ii, "mc.player.setXRot(") == 1);
        check("scan: InputIsolation writes through the arbiter", ii.contains("LOOK.plan("));
        check("scan: InputIsolation guards the write on the frame", ii.contains("look.wrote()"));
        check("scan: InputIsolation no longer keeps a second look channel", !ii.contains("aiHasTarget"));
    }

    // ── 6. "过来" vs "跟着"：一次性快照 vs 迟滞带动态目标（用户口径）──────

    /**
     * 用户实机根因："跟随每 0.5 秒重设一次目标 ⇒ 路径进度 / 卡住历史 / 对准收敛全被清零 ⇒
     * 一步都走不出去"。这里把修复的**结构**钉死，防止以后又被改回 navigateTo：
     * <ol>
     *   <li>跟随循环只许用 {@code Pathfinder.retarget}（不清进度），不许用 navigateTo / moveTo；</li>
     *   <li>{@code retarget} 本身不许出现任何"清零进度"的语句；</li>
     *   <li>迟滞带 / 到达距离 / 超时 / 卡住退避让路 / 目标失联如实收尾，都必须还在；</li>
     *   <li>"过来"是一次性：走 navigateTo，且**不**重定向；</li>
     *   <li>战斗反射：寻路中不接管位移、阶段变化才打日志；进食反射让路给战斗。</li>
     * </ol>
     */
    private static void comeVsFollowChecks() {
        Path root = Path.of("src", "main", "java", "com", "lcu", "lcumod", "action");
        String pf = read(root.resolve("Pathfinder.java"));
        String ae = read(root.resolve("ActionExecutor.java"));
        String cr = read(root.resolve("CombatReflex.java"));
        if (pf.isEmpty() || ae.isEmpty() || cr.isEmpty()) {
            check("come/follow: sources found", false);
            return;
        }

        String followTick = methodBody(ae, "private void tickFollowTarget(Minecraft mc)");
        String come = methodBody(ae, "private void handleComeToPlayer(WireServer.WireCommand cmd)");
        String retarget = methodBody(pf, "public static boolean retarget(");

        check("come/follow: follow tick found", !followTick.isEmpty());
        check("come/follow: follow tick retargets without wiping progress",
                followTick.contains("Pathfinder.retarget("));
        check("come/follow: follow tick never re-issues a full navigateTo",
                !followTick.contains("navigateTo(") && !followTick.contains("MovementSystem.moveTo("));
        check("come/follow: follow tick has hysteresis (arrive + chase distances)",
                followTick.contains("FOLLOW_ARRIVE_DISTANCE") && followTick.contains("FOLLOW_CHASE_DISTANCE"));
        check("come/follow: follow tick yields to the stuck watchdog backoff",
                followTick.contains("stuckCooldown > 0"));
        check("come/follow: follow tick has an honest timeout",
                followTick.contains("FOLLOW_TIMEOUT_TICKS") && followTick.contains("follow timeout"));
        check("come/follow: follow tick reports a lost target honestly",
                followTick.contains("target left the world"));

        check("come/follow: retarget exists", !retarget.isEmpty());
        check("come/follow: retarget never resets the alignment guard", !retarget.contains("ALIGNMENT.reset()"));
        check("come/follow: retarget never resets the stuck history", !retarget.contains("lastPosition = null"));
        check("come/follow: retarget never resets the replan gate wholesale", !retarget.contains("REPLAN.reset()"));
        check("come/follow: retarget keeps the navigation look target", !retarget.contains("clearNavigationLookTarget()"));
        check("come/follow: retarget is rate limited by the replan gate", retarget.contains("REPLAN.allow("));
        check("come/follow: hysteresis constant is 2.5 blocks", pf.contains("RETARGET_HYSTERESIS = 2.5"));

        check("come/follow: come is one-shot (full navigation, no retarget)",
                come.contains("Pathfinder.navigateTo(") && !come.contains("retarget("));
        check("come/follow: come clears the persistent follow first", come.contains("followTargetName = null;"));
        check("come/follow: come is dispatched", ae.contains("case \"come_to_player\" -> handleComeToPlayer(cmd);"));
        check("come/follow: come counts as a new task (cancels follow)",
                ae.contains("NEW_TASK_COMMANDS") && ae.contains("\"come_to_player\""));

        check("combat: reflex logs phase changes only", cr.contains("logPhase(") && cr.contains("loggedPhase"));
        check("combat: reflex does not steal movement while navigating",
                cr.contains("if (Pathfinder.isNavigating())"));
        check("combat: eating yields to combat", ae.contains("if (CombatReflex.isEngaged())"));

        // 纯逻辑：retarget 的限速就是 ReplanGate（同一 key 在窗口内只放行一次）。
        ReplanGate gate = new ReplanGate(2000L);
        check("come/follow: replan gate rate limits retargets",
                gate.allow("retarget", 0L) && !gate.allow("retarget", 1000L) && gate.allow("retarget", 2000L));
    }

    // ── 7. 出手安全：隔墙不打 / 不误伤旁边玩家（用户实机："他隔墙攻击，所以打到了我"）──

    /**
     * 把"出手必须先过统一安全检查"钉成结构：任何会**真打出伤害**的文件（含
     * {@code mc.gameMode.attack(}）都必须调用 {@link AttackRules}。
     *
     * <p>背景（用户实机）：他知道墙后有怪（{@code getLastHurtByMob} 记住了），就朝那个方向挥 ——
     * 中间隔着方块也照挥，于是在刷铁机边上误伤了主人。规则：视线被挡不挥；剑有横扫，旁边站着
     * 别的玩家也不挥；够不着不挥。被拒时给**机器可读原因码**，如实上报而不是假装打过了。
     */
    private static void attackSafetyChecks() {
        Path root = Path.of("src", "main", "java", "com", "lcu", "lcumod", "action");
        String rules = read(root.resolve("AttackRules.java"));
        check("attack: AttackRules exists", !rules.isEmpty());
        check("attack: uses the verified 26.3 raycast API",
                rules.contains("new ClipContext(") && rules.contains("ClipContext.Block.COLLIDER")
                        && rules.contains("HitResult.Type.BLOCK"));
        check("attack: blocked line of sight is a refusal code", rules.contains("NO_LINE_OF_SIGHT"));
        check("attack: no friendly fire near other players",
                rules.contains("PLAYER_TOO_CLOSE") && rules.contains("SWEEP_PLAYER_CLEARANCE"));
        check("attack: verdicts are machine readable", rules.contains("public String code()"));

        String[] mustGuard = {"ActionExecutor.java", "CombatReflex.java",
                "JavaAutonomousBehavior.java", "ControlStream.java"};
        for (String file : mustGuard) {
            String src = read(root.resolve(file));
            boolean attacks = src.contains("gameMode.attack(");
            boolean guarded = src.contains("AttackRules.check(") || src.contains("AttackRules.canHit(");
            check("attack: " + file + " gates its attacks behind AttackRules", !attacks || guarded);
        }

        // 武器策略（WeaponPolicy 零依赖 ⇒ 直接测逻辑，不加载任何 MC 类）：
        // 用户口径："旁边有玩家的情况下可以用斧子；重锤和矛禁止 AI 使用，因为误伤更严重。"
        check("attack: mace is banned for the AI", WeaponPolicy.isBanned("minecraft:mace"));
        check("attack: every spear tier is banned",
                WeaponPolicy.isBanned("minecraft:wooden_spear")
                        && WeaponPolicy.isBanned("minecraft:netherite_spear"));
        check("attack: banned weapons are never auto-equipped",
                WeaponPolicy.score("minecraft:mace") == 0
                        && WeaponPolicy.score("minecraft:iron_spear") == 0);
        check("attack: axe and bare hand are sweep-safe near players",
                WeaponPolicy.isSweepSafe("minecraft:iron_axe") && WeaponPolicy.isSweepSafe(""));
        check("attack: sword is not sweep-safe", !WeaponPolicy.isSweepSafe("minecraft:netherite_sword"));
        check("attack: normal preference still ranks sword over axe",
                WeaponPolicy.score("minecraft:netherite_sword") > WeaponPolicy.score("minecraft:netherite_axe"));
        check("attack: trident still ranks above axe",
                WeaponPolicy.score("minecraft:trident") > WeaponPolicy.score("minecraft:iron_axe"));
        check("attack: weapon policy stays zero dependency",
                !read(root.resolve("WeaponPolicy.java")).contains("net.minecraft"));
        check("attack: AttackRules enforces the weapon policy",
                rules.contains("WeaponPolicy.isBanned(") && rules.contains("WeaponPolicy.isSweepSafe("));
        check("attack: best-weapon pick delegates to the policy",
                read(root.resolve("ActionExecutor.java")).contains("WeaponPolicy.score("));
    }

    // ── 8. 地标记忆的标签与作废（用户口径：床没了 ⇒ 作废 bed 标签、保留 home）──

    /**
     * 用户实机口径：记忆要**带标签**；"home 的标签没有错，只是床的标签没了" ⇒ 它应该自己往 home 走、
     * 重新找床/做床；用户让它设新床时，home 也跟着变。这里把这条**生命周期**钉成结构。
     */
    private static void landmarkMemoryChecks() {
        Path root = Path.of("src", "main", "java", "com", "lcu", "lcumod", "action");
        String poi = read(root.resolve("PoiMemory.java"));
        String collector = read(Path.of("src", "main", "java", "com", "lcu", "lcumod", "state",
                "StateCollector.java"));

        check("memory: entries carry a tag set", poi.contains("final Set<String> tags"));
        check("memory: a point can hold several tags (bed + home)", poi.contains("void addTag("));
        check("memory: dropping a tag re-labels the point", poi.contains("void removeTag("));
        check("memory: first bed becomes the home too",
                poi.contains("remember(pos") && poi.contains("\"home\"") && poi.contains("\"bed\""));
        check("memory: bed loss needs the vanilla respawn point to CONFIRM it is still there",
                poi.contains("invalidateBedIfGone") && poi.contains("getRespawnData()")
                        && poi.contains("respawnIsThisBed"));
        check("memory: 'the respawn point is not this bed' must NOT mean the bed is gone",
                poi.contains("stillBed") && !poi.contains("respawnElsewhere"));
        check("memory: bed loss only drops the bed tag, home stays",
                poi.contains("removeTag(\"bed\")") && !poi.contains("removeTag(\"home\")"));
        check("memory: respawn point is remembered proactively", poi.contains("rememberLandmarks("));
        check("memory: tags are reported in the state", poi.contains("item.add(\"tags\""));
        check("memory: bed is remembered when seen", collector.contains("PoiMemory.rememberBed("));
        check("perception: nearby sweep is periodic (cached, ttl)",
                collector.contains("NEARBY_BLOCKS_TTL_TICKS") && collector.contains("cachedNearbyBlocks"));
        check("perception: moving one block re-scans immediately",
                collector.contains("distSqr(cachedNearbyBlocksOrigin)"));
        check("perception: the surroundings config values are actually read",
                collector.contains("ModConfig.COLLECT_SURROUNDINGS") && collector.contains("ModConfig.SURROUNDINGS_RADIUS"));
    }

    // ── 9. 装备反射（用户口径：丢装备给它要自己穿上）──

    /**
     * 静态钉住装备反射的**形态与边界**：
     * 走唯一那条"穿上"的路、只在 AI 控制时动手、可被后端关掉、有冷却，且**不许**动画手上的武器。
     */
    private static void gearReflexChecks() {
        String gear = read(Path.of("src", "main", "java", "com", "lcu", "lcumod", "action",
                "GearReflex.java"));
        String executor = read(Path.of("src", "main", "java", "com", "lcu", "lcumod", "action",
                "ActionExecutor.java"));

        check("gear: reuses the one equip path", gear.contains("ContainerController.equipArmor(mc)"));
        check("gear: only while the AI holds control", gear.contains("InputIsolation.isAiControlled()"));
        check("gear: can be switched off by the backend", gear.contains("ReflexGate.isSuppressed(GATE)"));
        check("gear: has a cooldown", gear.contains("GEAR_REFLEX_COOLDOWN_TICKS"));
        check("gear: silently ignores nothing-to-wear", gear.contains("already_equipped")
                && gear.contains("ContainerPlan.OK.equals(result.reason())"));
        check("gear: must not yank the held weapon out of the AI's hands",
                !gear.contains("equipBestWeapon("));   // 注释里提到它是可以的，调用不行
        check("gear: is ticked from the same place as the eat reflex",
                executor.contains("GearReflex.tick(mc)"));
    }

    // ── 10. 生物对待策略：高危险 / 敌对 / 中立 / 被动（本地反射与模型同一张表）──

    /**
     * 用户口径："**高危险的不主动打；中立的不惹；只有该处理的才处理**"。
     *
     * <p>表在零依赖的 {@link MobPolicy} 里 ⇒ 直接测逻辑（enderman/wolf/铁傀儡 ⇒ NEUTRAL 且不主动打；
     * 村民 ⇒ 不当猎物；warden ⇒ 高危险、连还手都不还手；creeper ⇒ 保持 4 格；认不出 ⇒ UNKNOWN 且
     * **不默认敌对**）；再静态扫描证明"两个本地出手点真的过了这张表、状态真的把 tier 发出去"。
     */
    private static void mobPolicyChecks() {
        Path root = Path.of("src", "main", "java", "com", "lcu", "lcumod", "action");
        Path collector = Path.of("src", "main", "java", "com", "lcu", "lcumod", "state",
                "StateCollector.java");

        // ① 中立：不惹（不许主动开打；还手要"它先动手"）
        check("mob: enderman is NEUTRAL", MobPolicy.tier("minecraft:enderman") == MobPolicy.Tier.NEUTRAL);
        check("mob: wolf is NEUTRAL", MobPolicy.tier("minecraft:wolf") == MobPolicy.Tier.NEUTRAL);
        check("mob: iron golem is NEUTRAL", MobPolicy.tier("minecraft:iron_golem") == MobPolicy.Tier.NEUTRAL);
        check("mob: zombified piglin is NEUTRAL (minecraft-data got this wrong)",
                MobPolicy.tier("minecraft:zombified_piglin") == MobPolicy.Tier.NEUTRAL);
        check("mob: neutral tiers are never initiated on",
                !MobPolicy.mayInitiate(MobPolicy.Tier.NEUTRAL));
        check("mob: neutral only retaliates after it struck first",
                !MobPolicy.mayRetaliate(MobPolicy.Tier.NEUTRAL, false)
                        && MobPolicy.mayRetaliate(MobPolicy.Tier.NEUTRAL, true));

        // ② 村民/流浪商人：**绝不当成猎物**（放在中立档 = 永久不许主动打）
        check("mob: villagers are not prey",
                MobPolicy.tier("minecraft:villager") == MobPolicy.Tier.NEUTRAL
                        && MobPolicy.tier("minecraft:wandering_trader") == MobPolicy.Tier.NEUTRAL
                        && !MobPolicy.mayInitiate(MobPolicy.tier("minecraft:villager"))
                        && !MobPolicy.mayInitiate(MobPolicy.tier("minecraft:wandering_trader")));

        // ③ 高危险：不主动打，也**不还手**（交回模型 / 逃生）
        check("mob: warden is DANGEROUS", MobPolicy.tier("minecraft:warden") == MobPolicy.Tier.DANGEROUS);
        check("mob: wither is DANGEROUS", MobPolicy.tier("minecraft:wither") == MobPolicy.Tier.DANGEROUS);
        check("mob: ender dragon is DANGEROUS",
                MobPolicy.tier("minecraft:ender_dragon") == MobPolicy.Tier.DANGEROUS);
        check("mob: dangerous is never initiated on",
                !MobPolicy.mayInitiate(MobPolicy.Tier.DANGEROUS));
        check("mob: dangerous is never retaliated against either",
                !MobPolicy.mayRetaliate(MobPolicy.Tier.DANGEROUS, true)
                        && !MobPolicy.mayRetaliate(MobPolicy.Tier.DANGEROUS, false));

        // ④ 敌对：该处理的（可主动打、被打必还手）
        check("mob: zombie is HOSTILE", MobPolicy.tier("minecraft:zombie") == MobPolicy.Tier.HOSTILE);
        check("mob: hostile can be initiated and retaliated",
                MobPolicy.mayInitiate(MobPolicy.Tier.HOSTILE)
                        && MobPolicy.mayRetaliate(MobPolicy.Tier.HOSTILE, true));

        // ⑤ 苦力怕/幻翼：敌对但**不贴脸**（mindcraft 规则：保持 4 格）
        check("mob: creeper is HOSTILE but keeps distance 4",
                MobPolicy.tier("minecraft:creeper") == MobPolicy.Tier.HOSTILE
                        && MobPolicy.preferDistance("minecraft:creeper") == 4.0);
        check("mob: phantom keeps distance 4", MobPolicy.preferDistance("minecraft:phantom") == 4.0);
        check("mob: ordinary mobs have no standoff", MobPolicy.preferDistance("minecraft:zombie") == 0.0);

        // ⑥ 被动 = 能猎的（"该处理的"）；未知 = 认不出，**绝不默认敌对**
        check("mob: cow is PASSIVE and huntable",
                MobPolicy.tier("minecraft:cow") == MobPolicy.Tier.PASSIVE
                        && MobPolicy.mayInitiate(MobPolicy.Tier.PASSIVE));
        check("mob: unknown id is UNKNOWN, never HOSTILE",
                MobPolicy.tier("minecraft:some_new_mod_mob") == MobPolicy.Tier.UNKNOWN
                        && MobPolicy.tier("") == MobPolicy.Tier.UNKNOWN
                        && MobPolicy.tier(null) == MobPolicy.Tier.UNKNOWN);
        check("mob: armor stand is not a target tier",
                MobPolicy.tier("minecraft:armor_stand") == MobPolicy.Tier.UNKNOWN);
        check("mob: unknown may be initiated (table is silent) but stays labelled unknown",
                MobPolicy.mayInitiate(MobPolicy.Tier.UNKNOWN)
                        && MobPolicy.tier("minecraft:some_new_mod_mob") != MobPolicy.Tier.HOSTILE);

        // ⑦ 命名空间 / 大小写无关（与 WeaponPolicy 同口径）
        check("mob: namespace and case are ignored",
                MobPolicy.tier("MINECRAFT:Enderman") == MobPolicy.Tier.NEUTRAL
                        && MobPolicy.tier(" enderman ") == MobPolicy.Tier.NEUTRAL);
        check("mob: wither skeleton is HOSTILE not DANGEROUS",
                MobPolicy.tier("minecraft:wither_skeleton") == MobPolicy.Tier.HOSTILE);

        // ⑧ 既有硬约束没被这次改动碰掉：重锤/矛仍然禁用
        check("mob: mace and spear stay banned for the AI",
                WeaponPolicy.isBanned("minecraft:mace") && WeaponPolicy.isBanned("minecraft:iron_spear"));

        String policy = read(root.resolve("MobPolicy.java"));
        check("mob: policy is zero dependency", !policy.isEmpty() && !policy.contains("net.minecraft"));

        // ⑨ 静态扫描：本地出手点真的过了这张表
        String reflex = read(root.resolve("CombatReflex.java"));
        String auton = read(root.resolve("JavaAutonomousBehavior.java"));
        check("mob: reflex consults mayRetaliate (being hit)", reflex.contains("MobPolicy.mayRetaliate("));
        check("mob: reflex consults mayInitiate (chasing)", reflex.contains("MobPolicy.mayInitiate("));
        check("mob: reflex keeps the creeper standoff", reflex.contains("MobPolicy.preferDistance("));
        check("mob: reflex never fights back at players", reflex.contains("hurtBy instanceof Player"));
        check("mob: reflex no longer hardcodes Enemy", !reflex.contains("instanceof Enemy"));
        check("mob: autonomous behavior consults mayInitiate",
                auton.contains("MobPolicy.mayInitiate(") && auton.contains("worthFighting"));
        check("mob: autonomous behavior only hunts the HOSTILE tier",
                auton.contains("tier == MobPolicy.Tier.HOSTILE"));
        check("mob: autonomous behavior keeps the creeper standoff",
                auton.contains("MobPolicy.preferDistance("));
        check("mob: autonomous behavior no longer hardcodes Enemy", !auton.contains("instanceof Enemy"));

        // ⑩ 静态扫描：状态里真的带 tier（后端优先信任它，关键词只是兜底）
        String state = read(collector);
        String perception = read(root.resolve("PerceptionView.java"));
        check("mob: state publishes the tier name", state.contains("MobPolicy.tier(AttackRules.entityTypeId(")
                && state.contains("tier.name()"));
        check("mob: the tier rides along in the tiered entity summary",
                perception.contains("\"tier\"") && perception.contains("tier()"));
        check("mob: entity type id comes from the registry (one place)",
                read(root.resolve("AttackRules.java")).contains("BuiltInRegistries.ENTITY_TYPE"));
    }

    // ── 11. 卡住自动绕行（用户口径：卡住不要只停，先换路线再试，试不动才如实报）──

    /**
     * 用户口径：「卡住不要只停：先自动换路线（绕行）再试，试不动才如实报」。
     *
     * <p>这里钉两件事：① 绕行额度是**有界**的纯逻辑（{@link StuckDetour}：最多 2 次、每次偏移
     * 方向不同 ⇒ 每次换法真的不同）；② 看门狗**先绕行、绕行不奏效才**走原来的
     * {@code stuck:} 上报，且绕行本身不重置进度（不清卡住历史/朝向收敛/导航视角目标）、
     * 走 REPLAN 限速、不搭方块。
     */
    private static void stuckDetourChecks() {
        Path root = Path.of("src", "main", "java", "com", "lcu", "lcumod", "action");
        String pf = read(root.resolve("Pathfinder.java"));
        String ae = read(root.resolve("ActionExecutor.java"));
        String sd = read(root.resolve("StuckDetour.java"));
        if (pf.isEmpty() || ae.isEmpty() || sd.isEmpty()) {
            check("stuck-detour: sources found", false);
            return;
        }

        // ① 纯逻辑：有界 + 每次换法真的不同
        StuckDetour budget = new StuckDetour();
        check("stuck-detour: budget is bounded at 2 attempts",
                StuckDetour.MAX_ATTEMPTS == 2 && budget.maxAttempts() == 2);
        check("stuck-detour: can attempt while under budget", budget.canAttempt() && !budget.exhausted());
        double first = budget.consumeOffset();
        double second = budget.consumeOffset();
        check("stuck-detour: the second detour goes the opposite way",
                Math.signum(first) != 0.0 && Math.signum(first) == -Math.signum(second));
        check("stuck-detour: both detours are lateral (non-zero, real offset >= 2 blocks)",
                StuckDetour.LATERAL_OFFSET >= 2.0
                        && Math.abs(first) == StuckDetour.LATERAL_OFFSET
                        && Math.abs(second) == StuckDetour.LATERAL_OFFSET);
        check("stuck-detour: the two attempts are really different", first != second);
        check("stuck-detour: exhausted only after the budget is used",
                budget.exhausted() && !budget.canAttempt() && budget.attempts() == 2);
        check("stuck-detour: an over-budget attempt is rejected", throwsIllegalState(budget::consumeOffset));
        budget.reset();
        check("stuck-detour: reset restores the budget", budget.canAttempt() && budget.attempts() == 0);
        check("stuck-detour: an empty budget is rejected", throwsIllegalArgument(() -> new StuckDetour(0)));
        check("stuck-detour: budget stays zero dependency (no net.minecraft)",
                !sd.contains("net.minecraft"));

        // ② 静态：绕行换路本身不重置进度、受限速、有界、不搭方块
        String detour = methodBody(pf, "public static DetourOutcome attemptDetour()");
        String detourPath = methodBody(pf, "private static boolean calculateDetourPath(");
        check("stuck-detour: Pathfinder exposes the bounded detour entry point",
                !detour.isEmpty() && pf.contains("StuckDetour DETOUR"));
        check("stuck-detour: detour never re-issues a full reset (no navigateTo)",
                !detour.contains("navigateTo(") && !detourPath.contains("navigateTo("));
        check("stuck-detour: detour never wipes the stuck history",
                !detour.contains("lastPosition = null") && !detourPath.contains("lastPosition = null"));
        check("stuck-detour: detour never resets the alignment guard",
                !detour.contains("ALIGNMENT.reset()") && !detourPath.contains("ALIGNMENT.reset()"));
        check("stuck-detour: detour never wipes the replan gate wholesale",
                !detour.contains("REPLAN.reset()") && !detourPath.contains("REPLAN.reset()"));
        check("stuck-detour: detour keeps the navigation look target",
                !detour.contains("clearNavigationLookTarget()")
                        && !detourPath.contains("clearNavigationLookTarget()"));
        check("stuck-detour: detour is rate limited by the replan gate",
                detour.contains("REPLAN.allow(\"detour:"));
        check("stuck-detour: detour is bounded by the zero-dep budget",
                detour.contains("DETOUR.exhausted()") && detour.contains("DETOUR.consumeOffset()"));
        check("stuck-detour: each attempt shifts the goal sideways then recomputes",
                detour.contains("calculateDetourPath(") && detourPath.contains("calculatePath()"));
        check("stuck-detour: no bridging/pillar-up rescue in the detour path",
                !detour.contains("useItemOn") && !detourPath.contains("useItemOn")
                        && !detour.contains("place_block") && !detourPath.contains("place_block")
                        && !detour.contains("BlockItem") && !detourPath.contains("BlockItem"));
        check("stuck-detour: reaching the detour point resumes the real goal",
                methodBody(pf, "public static void tick(Minecraft mc)").contains("detourPoint = null"));

        // ③ 静态：看门狗先绕行、绕行不奏效才报 stuck（保留原去向与 no-rescue 口径）
        String watchdog = methodBody(ae, "private void tickStuckWatchdog(Minecraft mc)");
        check("stuck-detour: watchdog tries a detour before giving up",
                watchdog.contains("Pathfinder.attemptDetour()")
                        && watchdog.indexOf("attemptDetour()") < watchdog.indexOf("\"stuck: \""));
        check("stuck-detour: watchdog only reports stuck for exhausted/unavailable detours",
                watchdog.contains("detour != Pathfinder.DetourOutcome.EXHAUSTED")
                        && watchdog.contains("detour != Pathfinder.DetourOutcome.UNAVAILABLE")
                        && watchdog.indexOf("DetourOutcome.UNAVAILABLE") < watchdog.indexOf("\"stuck: \""));
        check("stuck-detour: a successful detour keeps the task running (no stop)",
                watchdog.contains("DetourOutcome.DETOURED")
                        && !watchdog.substring(watchdog.indexOf("DetourOutcome.DETOURED"),
                                watchdog.indexOf("DetourOutcome.EXHAUSTED")).contains("Pathfinder.stop()"));
        check("stuck-detour: the honest report says how many detours were tried",
                watchdog.contains("detourAttempts()") && watchdog.contains("\"stuck: \""));
        check("stuck-detour: the no-rescue policy is still the reported fallback",
                watchdog.contains("NO_RESCUE_POLICY"));
        check("stuck-detour: the budget only resets on real movement (same 0.6 block threshold)",
                watchdog.contains("Pathfinder.resetDetourBudget()") && watchdog.contains("moved > 0.6"));
    }

    // ── 12. 无后端自主行为的抖动（flee/wander/贴怪不再全量重置目标）──

    /**
     * 与"跟随"同一个 bug：{@code MovementSystem.moveTo} 落到 {@code Pathfinder.navigateTo}，
     * 每 tick 调用就把路径进度/卡住历史/朝向收敛清零 ⇒ 原地罚站。这里把修复形态钉死：
     * 这些路径一律走"非破坏性重定向"，只有"上一次路已废弃 / 目标换到很远的地方"才重开一条路。
     */
    private static void autonomousJitterChecks() {
        Path root = Path.of("src", "main", "java", "com", "lcu", "lcumod", "action");
        String auton = read(root.resolve("JavaAutonomousBehavior.java"));
        String ae = read(root.resolve("ActionExecutor.java"));
        if (auton.isEmpty() || ae.isEmpty()) {
            check("auton: sources found", false);
            return;
        }

        String flee = methodBody(auton, "private static void flee(Minecraft mc)");
        String wander = methodBody(auton, "private static void wander(Minecraft mc)");
        String approach = methodBody(auton, "private static void attackNearbyHostile(Minecraft mc)");
        String headTo = methodBody(auton, "private static void headTo(");

        check("auton: the full-reset moveTo is gone from the whole behavior", 
                !auton.contains("MovementSystem.moveTo("));
        check("auton: flee retargets without wiping progress",
                flee.contains("headTo(") || flee.contains("Pathfinder.retarget("));
        check("auton: wander retargets without wiping progress",
                wander.contains("headTo(") || wander.contains("Pathfinder.retarget("));
        check("auton: closing in on a mob retargets without wiping progress",
                approach.contains("headTo(") || approach.contains("Pathfinder.retarget("));
        check("auton: routing never wipes the stuck history", !auton.contains("lastPosition = null"));
        check("auton: a full navigation only happens through the one documented criterion",
                !headTo.isEmpty() && headTo.contains("ROUTE_RESTART_DISTANCE")
                        && headTo.contains("Pathfinder.retarget(")
                        && headTo.contains("Pathfinder.navigateTo(")
                        && countOf(auton, "Pathfinder.navigateTo(") == 1);
        check("auton: the restart criterion only fires when the old route is dead or far away",
                headTo.contains("Pathfinder.isNavigating()"));
        check("auton: still only runs as a no-backend fallback",
                ae.contains("!backendConnected && InputIsolation.isAiControlled()"));
    }

    // ── 10. 死亡点 + 无后端自主行为的默认开关 ──

    /**
     * 死亡点：用户口径"死了不会回死亡点捡装备" ⇒ 模组**只报事实**（死在哪、什么时候），
     * 回不回由模型决定。这里把"只报事实、不自动回去"钉成结构。
     */
    private static void deathPointChecks() {
        String death = read(Path.of("src", "main", "java", "com", "lcu", "lcumod", "action",
                "DeathPoint.java"));
        String collector = read(Path.of("src", "main", "java", "com", "lcu", "lcumod", "state",
                "StateCollector.java"));

        check("death: keeps the last few points, newest first", death.contains("MAX_POINTS = 3")
                && death.contains("addFirst("));
        check("death: remembers the last position while alive, not the respawn point",
                death.contains("lastAliveX") && death.contains("isDeadOrDying()"));
        check("death: records once per death (edge triggered)", death.contains("if (!wasAlive)"));
        check("death: stamps when it happened", death.contains("System.currentTimeMillis()"));
        check("death: never invents coordinates it never had", death.contains("Double.isNaN(x)"));
        check("death: reports facts only, never walks anywhere",
                !death.contains("Pathfinder.") && !death.contains("MovementSystem."));
        check("death: is ticked every client tick, not behind the state interval",
                collector.indexOf("DeathPoint.tick(") > 0
                        && collector.indexOf("DeathPoint.tick(") < collector.indexOf("STATE_INTERVAL"));
        check("death: surfaces in the state payload", collector.contains("state.add(\"death_points\""));
    }

    /**
     * 无后端时那套 legacy 自主行为的**默认开关**：用户口径"后台不许有自主开关/自动触发" ⇒ 默认关；
     * 想开是**偏好**（配置项），不是自主触发；P0 生存反射不受它影响。
     */
    private static void autonomousDefaultChecks() {
        String autonomy = read(Path.of("src", "main", "java", "com", "lcu", "lcumod", "action",
                "JavaAutonomousBehavior.java"));
        String config = read(Path.of("src", "main", "java", "com", "lcu", "lcumod", "config",
                "ModConfig.java"));
        String executor = read(Path.of("src", "main", "java", "com", "lcu", "lcumod", "action",
                "ActionExecutor.java"));

        check("auton: default off", autonomy.contains("private static boolean enabled = false;"));
        check("auton: the switch is a preference, not a behaviour",
                config.contains("AUTONOMOUS_FALLBACK") && config.contains(".define(\"autonomousFallback\", false)"));
        check("auton: the config drives it (and a missing config keeps it off)",
                executor.contains("AUTONOMOUS_FALLBACK.getAsBoolean()")
                        && executor.contains("JavaAutonomousBehavior.setEnabled("));
        check("auton: turning it off does not touch the P0 survival reflexes",
                !read(Path.of("src", "main", "java", "com", "lcu", "lcumod", "action",
                        "CombatReflex.java")).contains("AUTONOMOUS_FALLBACK"));
    }

    // ── 11. 输入所有权（真机"不移动"事故的结构性修复）──

    /**
     * 用户口径："CI 测试 … 就得先模拟这个逻辑，尽可能在测试阶段发现这类显著性问题。"
     *
     * <p>真机事故：`Pathfinder` 每 tick 按前进，`CombatReflex` **没交战时也每 tick** 清前进
     * ⇒ 有路、navigating=true、却一步不动。这里对**零依赖的 `InputOwnership` 直接做行为断言**
     * （最短复现就在下面），再扫源码确认三个写入者都走所有权入口。
     */
    private static void inputOwnershipChecks() {
        InputOwnership.clear();
        check("input-ownership: a stranger cannot clear someone else's input",
                !InputOwnership.allows("combat", "forward", false));
        check("input-ownership: writing true claims the key",
                InputOwnership.allows("navigation", "forward", true)
                        && InputOwnership.owns("navigation", "forward"));
        check("input-ownership: the owner may clear it, and then no longer owns it",
                InputOwnership.allows("navigation", "forward", false)
                        && !InputOwnership.owns("navigation", "forward"));

        // 真机 bug 的最短复现：导航按下前进 → 旁人来清 → 必须被拒。
        InputOwnership.clear();
        InputOwnership.allows("navigation", "forward", true);
        check("input-ownership: the combat reflex cannot wipe navigation's forward key (the live bug)",
                !InputOwnership.allows("combat", "forward", false)
                        && InputOwnership.owns("navigation", "forward"));

        // 换人接管：写 true 就是接管，之后原来那位也清不掉了。
        InputOwnership.clear();
        InputOwnership.allows("navigation", "forward", true);
        InputOwnership.allows("swim", "forward", true);
        check("input-ownership: the newest writer takes over",
                InputOwnership.owns("swim", "forward")
                        && !InputOwnership.allows("navigation", "forward", false));

        check("input-ownership: blank writer or key is refused",
                !InputOwnership.allows("", "forward", true) && !InputOwnership.allows("x", "", true));
        InputOwnership.clear();
        check("input-ownership: clear() drops every claim", InputOwnership.snapshot().isEmpty());

        Path root = Path.of("src", "main", "java", "com", "lcu", "lcumod", "action");
        String combat = read(root.resolve("CombatReflex.java"));
        String pathfinder = read(root.resolve("Pathfinder.java"));
        String swim = read(root.resolve("SwimReflex.java"));
        String isolation = read(root.resolve("InputIsolation.java"));

        check("input-ownership: the combat reflex only releases what it owns",
                combat.contains("setControlState(\"combat\", \"forward\", false)")
                        && !combat.contains("setInput(\"forward\""));
        check("input-ownership: navigation claims movement before pressing it",
                pathfinder.contains("setControlState(\"navigation\", \"forward\"")
                        && !pathfinder.contains("setAiControlState(\"forward\""));
        check("input-ownership: the swim reflex no longer writes input behind the other writers' backs",
                !swim.contains("setInput("));
        check("input-ownership: clearing AI controls also drops ownership",
                isolation.contains("InputOwnership.clear()"));
        check("input-ownership: the policy itself is zero dependency (no Minecraft imports)",
                !read(root.resolve("InputOwnership.java")).contains("import net.minecraft"));
    }

    // ── 12. 自身装备/耐久 + 原版贴图（用户口径："OBS 叠加页要显示自己的装备、耐久、主要武器"）──

    /**
     * 两件事必须钉死：
     * <ol>
     *   <li><b>取值与自洽</b>：可损坏/不可损坏的边界、{@code durability = max_durability - damage}、
     *       畸形值钳位、空槽的规范零值、指纹随耐久变化 —— 直接行为断言零依赖的那半（{@link GearReport}）；</li>
     *   <li><b>形态</b>：四槽固定顺序、空槽也上报（不是缺字段）、贴图**只在装备集合变了**时发一次、
     *       已发过的 id 不重发、读不到就跳过（不编不占位）、贴图走客户端资源管理器的实证 API。</li>
     * </ol>
     */
    private static void gearReportChecks() {
        // ① 纯逻辑：全新的剑 = 满载耐久
        GearReport.Pure.SlotFacts fresh = GearReport.Pure.facts("minecraft:iron_sword", "铁剑", 1, true, 0, 250);
        check("gear-report: a fresh sword reports full durability",
                fresh.durability() == 250 && fresh.damage() == 0 && fresh.maxDurability() == 250
                        && fresh.damageable() && fresh.selfConsistent());

        // ② 三元组自洽：durability = max - damage
        GearReport.Pure.SlotFacts worn = GearReport.Pure.facts("minecraft:iron_helmet", "铁头盔", 1, true, 45, 165);
        check("gear-report: durability = max_durability - damage", worn.durability() == 120
                && worn.damage() == 45 && worn.maxDurability() == 165 && worn.selfConsistent());

        // ③ 快坏了也如实报（剩余 1），不美化
        GearReport.Pure.SlotFacts nearly = GearReport.Pure.facts("minecraft:iron_pickaxe", "铁镐", 1, true, 249, 250);
        check("gear-report: a nearly broken tool reports 1 left (no rounding up)",
                nearly.durability() == 1 && nearly.damage() == 249 && nearly.selfConsistent());

        // ④ 不可损坏（方块/食物）：damageable=false 且三个数全 0 —— 诚实，不编上限
        GearReport.Pure.SlotFacts block = GearReport.Pure.facts("minecraft:dirt", "泥土", 64, false, 0, 0);
        check("gear-report: non-damageable items report false with all-zero durability",
                !block.damageable() && block.durability() == 0 && block.maxDurability() == 0
                        && block.damage() == 0 && block.count() == 64 && block.selfConsistent());

        // ⑤ 畸形输入不许漏出自相矛盾的数：上限 0 的"可损坏物" ⇒ 按不可损坏报
        GearReport.Pure.SlotFacts noMax = GearReport.Pure.facts("a:b", "b", 1, true, 5, 0);
        check("gear-report: damageable with max 0 is reported as non-damageable (no made-up max)",
                !noMax.damageable() && noMax.selfConsistent());

        // ⑥ 越界 damage / 负数 ⇒ 钳位，三元组仍然自洽
        GearReport.Pure.SlotFacts over = GearReport.Pure.facts("a:b", "b", 1, true, 999, 100);
        GearReport.Pure.SlotFacts under = GearReport.Pure.facts("a:b", "b", 1, true, -7, 100);
        check("gear-report: out-of-range damage is clamped and stays consistent",
                over.damage() == 100 && over.durability() == 0 && over.selfConsistent()
                        && under.damage() == 0 && under.durability() == 100 && under.selfConsistent());

        // ⑦ 空槽 = 规范零值对象（**不是**缺字段）
        check("gear-report: the empty slot is the canonical zero object",
                GearReport.Pure.EMPTY_SLOT.isEmpty() && GearReport.Pure.EMPTY_SLOT.count() == 0
                        && GearReport.Pure.EMPTY_SLOT.selfConsistent());

        // ⑧ 四槽顺序固定（web 不许猜）
        check("gear-report: armor slots are fixed head,chest,legs,feet",
                GearReport.Pure.ARMOR_SLOTS.length == 4
                        && "head".equals(GearReport.Pure.ARMOR_SLOTS[0]) && "chest".equals(GearReport.Pure.ARMOR_SLOTS[1])
                        && "legs".equals(GearReport.Pure.ARMOR_SLOTS[2]) && "feet".equals(GearReport.Pure.ARMOR_SLOTS[3]));

        // ⑨ 指纹：六个空槽也占位（数量固定），且**耐久掉了指纹必须变**（OBS 的耐久条不许停住）
        List<GearReport.Pure.SlotFacts> emptySlots = List.of(GearReport.Pure.EMPTY_SLOT, GearReport.Pure.EMPTY_SLOT,
                GearReport.Pure.EMPTY_SLOT, GearReport.Pure.EMPTY_SLOT, GearReport.Pure.EMPTY_SLOT, GearReport.Pure.EMPTY_SLOT);
        String emptyKey = GearReport.Pure.slotKey(emptySlots);
        check("gear-report: six empty slots still produce a fixed fingerprint",
                "-|-|-|-|-|-".equals(emptyKey) && emptyKey.equals(GearReport.Pure.slotKey(emptySlots)));

        GearReport.Pure.SlotFacts swinging = GearReport.Pure.facts("minecraft:iron_sword", "铁剑", 1, true, 1, 250);
        String fullKey = GearReport.Pure.slotKey(List.of(GearReport.Pure.EMPTY_SLOT, GearReport.Pure.EMPTY_SLOT,
                GearReport.Pure.EMPTY_SLOT, GearReport.Pure.EMPTY_SLOT, fresh, GearReport.Pure.EMPTY_SLOT));
        String wornKey = GearReport.Pure.slotKey(List.of(GearReport.Pure.EMPTY_SLOT, GearReport.Pure.EMPTY_SLOT,
                GearReport.Pure.EMPTY_SLOT, GearReport.Pure.EMPTY_SLOT, swinging, GearReport.Pure.EMPTY_SLOT));
        check("gear-report: losing durability changes the fingerprint (state must not go stale)",
                !fullKey.equals(wornKey) && fullKey.contains("minecraft:iron_sword")
                        && wornKey.contains("minecraft:iron_sword"));

        // ⑩ 静态扫描：状态里真的带 gear、且 gear 变化会触发上报
        Path collector = Path.of("src", "main", "java", "com", "lcu", "lcumod", "state", "StateCollector.java");
        String stateSrc = read(collector);
        String gearSrc = read(Path.of("src", "main", "java", "com", "lcu", "lcumod", "action", "GearReport.java"));
        check("gear-report: GearReport source found", !gearSrc.isEmpty());

        // 测的那一半**真的**零依赖（自检 JVM 的类路径上没有游戏类，能直接测它全靠这条界线）
        int pureStart = gearSrc.indexOf("public static final class Pure {");
        int pureEnd = gearSrc.indexOf("// ── Minecraft 侧");
        String pureSrc = pureStart >= 0 && pureEnd > pureStart ? gearSrc.substring(pureStart, pureEnd) : "";
        check("gear-report: the tested half carries no game classes at all",
                !pureSrc.isEmpty() && !pureSrc.contains("net.minecraft") && !pureSrc.contains("Minecraft")
                        && !pureSrc.contains("import ")
                        && pureSrc.contains("EMPTY_SLOT") && pureSrc.contains("selfConsistent()"));

        check("gear-report: state_update carries the gear block",
                stateSrc.contains("state.add(\"gear\", GearReport.build(mc))"));
        check("gear-report: a gear change forces a state update",
                stateSrc.contains("GearReport.slotKey(p)") && stateSrc.contains("lastGearKey"));
        check("gear-report: icons are published from the tick path, before the throttled send gate",
                stateSrc.contains("GearReport.publishIcons(mc, p)")
                        && stateSrc.indexOf("GearReport.publishIcons(mc, p)")
                            < stateSrc.indexOf("boolean hasChanged = false;"));

        // ⑪ 四槽永远在（循环里无条件 add，空槽给零值对象）
        check("gear-report: all four armor slots are always reported (empty ones too)",
                gearSrc.contains("for (int i = 0; i < Pure.ARMOR_SLOTS.length; i++)")
                        && gearSrc.contains("o.addProperty(\"slot\", Pure.ARMOR_SLOTS[i])")
                        && gearSrc.contains("armor.add(o)")
                        && gearSrc.contains("EMPTY_SLOT"));

        // ⑫ 每个槽位对象都带全集字段（web 只按 key 取值）
        String toJsonBody = methodBody(gearSrc, "public static JsonObject toJson(");
        check("gear-report: every slot object carries the full key set",
                toJsonBody.contains("\"item_id\"") && toJsonBody.contains("\"name\"")
                        && toJsonBody.contains("\"count\"") && toJsonBody.contains("\"durability\"")
                        && toJsonBody.contains("\"max_durability\"") && toJsonBody.contains("\"damage\"")
                        && toJsonBody.contains("\"damageable\""));
        check("gear-report: the durability triple is computed in exactly one place",
                countOf(gearSrc, "maxDamage - dmg") == 1 && gearSrc.contains("Math.min(Math.max(0, damage), maxDamage)"));

        // ⑬ 贴图：只在装备集合变了时发一次（有已发集合），读不到就跳过
        String publishBody = methodBody(gearSrc, "public static void publishIcons(");
        check("gear-report: the icon event only goes out when the gear set changed",
                publishBody.contains("key.equals(lastIconKey)") && publishBody.contains("lastIconKey = key")
                        && publishBody.contains("if (icons.size() == 0)"));
        check("gear-report: ids already sent are never re-sent",
                publishBody.contains("sentIcons.contains(id)") && publishBody.contains("sentIcons.add(id)"));
        check("gear-report: an unreadable texture is skipped, never faked or padded",
                publishBody.contains("unavailableIcons.add(id)")
                        && countOf(methodBody(gearSrc, "private static String readIconBase64("), "return null") >= 3);
        check("gear-report: icons are sent as the item_icons event",
                gearSrc.contains("LCUMod.WIRE.sendEvent(\"item_icons\"") && publishBody.contains("data.add(\"icons\", icons)"));

        // ⑭ 贴图来源：客户端资源管理器 + 实证过的 26.3 API（Identifier.fromNamespaceAndPath / Resource.open）
        String readIconBody = methodBody(gearSrc, "private static String readIconBase64(");
        check("gear-report: textures come from the client resource manager (verified 26.3 API)",
                readIconBody.contains("mc.getResourceManager().getResource(")
                        && readIconBody.contains("Identifier.fromNamespaceAndPath(")
                        && readIconBody.contains("resource.get().open()")
                        && readIconBody.contains("textures/item/")
                        && readIconBody.contains("Base64.getEncoder()"));

        // ⑮ 只发装备槽 + 主/副手里的东西（不是整个物品栏）
        check("gear-report: only the equipped slots + hands get icons",
                gearSrc.contains("private static List<String> equippedIds(Player p)")
                        && methodBody(gearSrc, "private static List<String> equippedIds(").contains("currentSlots(p)"));

        // ⑯ 只 import 用得上的东西（贴图/耐久这条链不牵扯别的包，更不碰后端）
        boolean onlyNeededImports = true;
        for (String line : gearSrc.split("\n")) {
            String trimmed = line.trim();
            if (!trimmed.startsWith("import ")) {
                continue;
            }
            if (!(trimmed.startsWith("import com.google.gson.")
                    || trimmed.startsWith("import com.lcu.lcumod.LCUMod;")
                    || trimmed.startsWith("import net.minecraft.")
                    || trimmed.startsWith("import java.io.InputStream;")
                    || trimmed.startsWith("import java.util."))) {
                onlyNeededImports = false;
                System.out.println("       unexpected import: " + trimmed);
            }
        }
        check("gear-report: imports only what the job needs", onlyNeededImports);

        // ⑰ 顺带核对：血/饥饿/饱和度**已经在 player 里**（不重复加第二份）
        check("gear-report: health/hunger/saturation are reported once (not duplicated)",
                countOf(stateSrc, "player.addProperty(\"health\"") == 1
                        && countOf(stateSrc, "player.addProperty(\"hunger\"") == 1
                        && countOf(stateSrc, "player.addProperty(\"saturation\"") == 1
                        && stateSrc.contains("p.getFoodData().getSaturationLevel()"));
    }

    // ── 13. 感知分层：实体/方块默认只给计数+最近+威胁（威胁才带坐标）──

    /**
     * 用户口径："不要把周围生物的坐标动态全都传给他 —— 村庄/繁殖基地/农场附近大量生物移动就会大量刷屏。
     * 要设计一个**层级**：有些东西需要的时候再获取坐标，不需要的默认当**无关信息屏蔽**掉。"
     *
     * <p>行为断言打在零依赖的 {@link PerceptionView.Pure} 上（分档聚合 / 计数自洽 / threats 截断到 3 /
     * 坐标只挂在 threats+players / by_block 稳定排序 / home_bed 绑定判定），形态用静态扫描钉住
     * （state 真的发摘要、逐实体 x/y/z 的旧写法已不在、纯逻辑那半不碰游戏类）。
     */
    private static void perceptionTieringChecks() {
        Path actionRoot = Path.of("src", "main", "java", "com", "lcu", "lcumod", "action");
        Path collector = Path.of("src", "main", "java", "com", "lcu", "lcumod", "state",
                "StateCollector.java");

        // ① 10 只被动：passive=10、count 自洽、没有威胁、没有被当坐标携带者
        List<PerceptionView.Pure.Row> passive = new java.util.ArrayList<>();
        for (int i = 0; i < 10; i++) {
            passive.add(mobRow("cow" + i, "PASSIVE", 5.0 + i));
        }
        PerceptionView.Pure.EntitySummary s10 = PerceptionView.Pure.summarizeEntities(passive);
        check("perception: 10 passive mobs -> by_kind.passive == 10",
                s10.byKind().get("passive") == 10);
        check("perception: count equals the sum of by_kind",
                s10.count() == 10 && sumKinds(s10.byKind()) == s10.count());
        check("perception: passive mobs raise no threats", s10.threats().isEmpty());
        check("perception: passive mobs carry no coordinates anywhere (threats/players empty)",
                s10.threats().isEmpty() && s10.players().isEmpty());

        // ② 3 只敌对 + 5 只被动 + 1 只 20 格外的敌对：threats 正好 3 条、按距离升序、16 格外的不进来
        List<PerceptionView.Pure.Row> mixed = new java.util.ArrayList<>();
        mixed.add(mobRow("zombie-a", "HOSTILE", 3.0));
        mixed.add(mobRow("zombie-b", "HOSTILE", 6.0));
        mixed.add(mobRow("zombie-c", "HOSTILE", 9.0));
        mixed.add(mobRow("zombie-far", "HOSTILE", 20.0));   // > 16 ⇒ 不算威胁（但仍计入 by_kind）
        for (int i = 0; i < 5; i++) {
            mixed.add(mobRow("cow" + i, "PASSIVE", 4.0 + i));
        }
        PerceptionView.Pure.EntitySummary sm = PerceptionView.Pure.summarizeEntities(mixed);
        check("perception: exactly 3 threats are kept", sm.threats().size() == 3);
        check("perception: threats are sorted by distance ascending",
                sm.threats().size() == 3
                        && sm.threats().get(0).name().equals("zombie-a")
                        && sm.threats().get(1).name().equals("zombie-b")
                        && sm.threats().get(2).name().equals("zombie-c"));
        check("perception: a hostile beyond 16 blocks is not a threat (but still counted)",
                sm.threats().size() == 3 && sm.byKind().get("hostile") == 4);
        check("perception: hostile threats carry the tier and the kind",
                sm.threats().get(0).type().equals("hostile")
                        && sm.threats().get(0).tier().equals("HOSTILE"));
        check("perception: mixed count is self-consistent",
                sm.count() == 9 && sumKinds(sm.byKind()) == sm.count()
                        && sm.byKind().get("passive") == 5);

        // ③ 玩家**总在** players 里、总带坐标；超过 8 个只留最近的 8 个
        List<PerceptionView.Pure.Row> withPlayers = new java.util.ArrayList<>();
        withPlayers.add(playerRow("BoHuYeShan", 850.6, 63.0, -2190.2, 4.0));
        for (int i = 0; i < 9; i++) {
            withPlayers.add(playerRow("p" + i, 0, 0, 0, 30.0 + i));
        }
        PerceptionView.Pure.EntitySummary sp = PerceptionView.Pure.summarizeEntities(withPlayers);
        check("perception: the player is always present in players",
                sp.players().stream().anyMatch(pl -> pl.name().equals("BoHuYeShan")));
        check("perception: players always carry coordinates",
                sp.players().get(0).name().equals("BoHuYeShan") && sp.players().get(0).x() == 851
                        && sp.players().get(0).y() == 63 && sp.players().get(0).z() == -2190);
        check("perception: players are capped at the 8 nearest", sp.players().size() == 8);

        // ④ items 只给 count + nearest（nearest 带 owner/age/despawn_in，不带坐标）
        List<PerceptionView.Pure.Row> items = new java.util.ArrayList<>();
        items.add(itemRow("minecraft:oak_log", 1, 3.1, "", 240, 5760));
        items.add(itemRow("minecraft:stick", 2, 8.0, "BoHuYeShan", 10, 5990));
        PerceptionView.Pure.EntitySummary si = PerceptionView.Pure.summarizeEntities(items);
        check("perception: items are aggregated (count only)", si.items().count() == 2);
        check("perception: items.nearest is the closest one with its facts",
                si.items().nearest() != null
                        && si.items().nearest().itemId().equals("minecraft:oak_log")
                        && si.items().nearest().itemCount() == 1
                        && si.items().nearest().owner().isEmpty()
                        && si.items().nearest().age() == 240
                        && si.items().nearest().despawnIn() == 5760);

        // ⑤ by_block 稳定排序：同数量按名字升序、最多 12 种，count 是**总数**（不只 top-12）
        List<PerceptionView.Pure.BlockRow> blocks = new java.util.ArrayList<>();
        blocks.add(new PerceptionView.Pure.BlockRow("minecraft:oak_log", 1.0));
        blocks.add(new PerceptionView.Pure.BlockRow("minecraft:oak_log", 2.0));
        blocks.add(new PerceptionView.Pure.BlockRow("minecraft:oak_log", 3.0));
        for (int i = 0; i < 12; i++) {
            blocks.add(new PerceptionView.Pure.BlockRow("minecraft:b" + (char) ('a' + i), 5.0));
        }
        PerceptionView.Pure.BlockSummary sb = PerceptionView.Pure.summarizeBlocks(blocks);
        check("perception: block count is the total, not the listed kinds", sb.count() == 15);
        check("perception: by_block lists at most 12 kinds", sb.byBlock().size() == 12);
        check("perception: by_block is sorted by count desc, then name asc (stable)",
                sb.byBlock().keySet().iterator().next().equals("minecraft:oak_log")
                        && sb.byBlock().get("minecraft:oak_log") == 3
                        && new java.util.ArrayList<>(sb.byBlock().keySet()).subList(1, 12)
                                .equals(sortedNames("minecraft:b", 11)));
        check("perception: blocks.nearest is the closest block id (no coordinates)",
                sb.nearest() != null && sb.nearest().blockId().equals("minecraft:oak_log")
                        && sb.nearest().distance() == 1.0);

        // ⑥ 坐标只出现在 threats/players 上（记录形状层面 nearest/items/by_block 就没有 x/y/z）
        check("perception: only threat/player records carry x/y/z components",
                hasCoord(PerceptionView.Pure.Threat.class) && hasCoord(PerceptionView.Pure.PlayerRow.class)
                        && !hasCoord(PerceptionView.Pure.Nearest.class)
                        && !hasCoord(PerceptionView.Pure.ItemNearest.class)
                        && !hasCoord(PerceptionView.Pure.BlockNearest.class));

        // ⑦ home_bed：没记过 ⇒ 没绑；重生点就是它 ⇒ 绑上；重生点在别处 / 读不到 ⇒ 没绑
        PerceptionView.Pure.HomeBed none =
                PerceptionView.Pure.homeBed(false, 0, 0, 0, true, 851, 63, -2191);
        check("perception: no remembered bed -> home_bed absent and not bound",
                !none.haveBed() && !none.bound());
        PerceptionView.Pure.HomeBed boundBed =
                PerceptionView.Pure.homeBed(true, 851, 63, -2191, true, 851, 63, -2191);
        check("perception: respawn == remembered bed -> bound",
                boundBed.haveBed() && boundBed.bound());
        check("perception: respawn elsewhere -> not bound",
                !PerceptionView.Pure.homeBed(true, 851, 63, -2191, true, 100, 64, 200).bound());
        check("perception: bed remembered but respawn unreadable -> not bound (no fabrication)",
                !PerceptionView.Pure.homeBed(true, 851, 63, -2191, false, 0, 0, 0).bound());

        // ⑧ 静态扫描：state 真的发摘要；逐实体 x/y/z 的旧写法已不在
        String stateSrc = read(collector);
        String pvSrc = read(actionRoot.resolve("PerceptionView.java"));
        check("perception: PerceptionView source found", !pvSrc.isEmpty());
        check("perception: state publishes the tiered entity summary",
                stateSrc.contains("state.add(\"entities\", PerceptionView.entities("));
        check("perception: nearby_blocks goes through the block summary",
                stateSrc.contains("PerceptionView.blocks(")
                        && stateSrc.contains("state.add(\"nearby_blocks\", collectNearbyBlocks("));
        check("perception: the old per-entity x/y/z JSON writing is gone",
                !stateSrc.contains("e.addProperty(\"x\"") && !stateSrc.contains("\"x\", entity.getX()")
                        && !stateSrc.contains("\"y\", entity.getY()")
                        && !stateSrc.contains("\"z\", entity.getZ()"));
        check("perception: home_bed is published beside gear",
                stateSrc.contains("state.add(\"home_bed\"") && stateSrc.contains("home_bed_bound")
                        && stateSrc.contains("getRespawnData()"));

        // ⑨ 零依赖界线 + JSON 里坐标只写在 threats/players
        int pureStart = pvSrc.indexOf("public static final class Pure {");
        int pureEnd = pvSrc.indexOf("// ── Minecraft 侧");
        String pureSrc = pureStart >= 0 && pureEnd > pureStart ? pvSrc.substring(pureStart, pureEnd) : "";
        check("perception: the tested half carries no game classes at all",
                !pureSrc.isEmpty() && !pureSrc.contains("net.minecraft") && !pureSrc.contains("Minecraft"));
        check("perception: nearest/item/block JSON carry no x/y/z",
                !methodBody(pvSrc, "private static JsonObject nearestJson(").contains("\"x\"")
                        && !methodBody(pvSrc, "private static JsonObject itemJson(").contains("\"x\"")
                        && !methodBody(pvSrc, "private static JsonObject blocksJson(").contains("\"x\""));
        check("perception: threat/player JSON do carry x/y/z",
                methodBody(pvSrc, "private static JsonObject threatJson(").contains("\"x\"")
                        && methodBody(pvSrc, "private static JsonObject playerJson(").contains("\"x\""));
    }

    /** 一条生物行（零依赖）：tier 名 + 距离。 */
    private static PerceptionView.Pure.Row mobRow(String name, String tier, double distance) {
        return new PerceptionView.Pure.Row(name, tier, false, false, 0, 64, 0, distance, true, 20.0,
                "", 0, "", 0, 0);
    }

    /** 一条玩家行（零依赖）：永远带坐标。 */
    private static PerceptionView.Pure.Row playerRow(String name, double x, double y, double z,
                                                     double distance) {
        return new PerceptionView.Pure.Row(name, "", true, false, x, y, z, distance, true, 20.0,
                "", 0, "", 0, 0);
    }

    /** 一条掉落物行（零依赖）：物品事实，不带坐标。 */
    private static PerceptionView.Pure.Row itemRow(String itemId, int count, double distance,
                                                   String owner, int age, int despawnIn) {
        return new PerceptionView.Pure.Row(itemId, "", false, true, 0, 64, 0, distance, false, 0.0,
                itemId, count, owner, age, despawnIn);
    }

    private static int sumKinds(java.util.Map<String, Integer> byKind) {
        int sum = 0;
        for (int value : byKind.values()) {
            sum += value;
        }
        return sum;
    }

    /** 记录形状里有没有 x/y/z 分量（"坐标只出现在 threats/players 上"的机器可验证表述）。 */
    private static boolean hasCoord(Class<?> recordClass) {
        for (java.lang.reflect.RecordComponent component : recordClass.getRecordComponents()) {
            String name = component.getName();
            if (name.equals("x") || name.equals("y") || name.equals("z")) {
                return true;
            }
        }
        return false;
    }

    private static List<String> sortedNames(String prefix, int count) {
        List<String> names = new java.util.ArrayList<>();
        for (int i = 0; i < count; i++) {
            names.add(prefix + (char) ('a' + i));
        }
        return names;
    }

    // ── 13. 按需实体扫描（scan_entities）──

    /**
     * 用户口径："有些东西**需要的时候再获取坐标**，不需要的默认当无关信息屏蔽掉。"
     * 于是 {@code state_update.entities} 变成了分级摘要（只有威胁/玩家/最近掉落物带坐标，
     * 见 {@link PerceptionView.Pure}）。这条按需回路就是补回来的那一半：模型问"附近有没有牛"
     * 时才用 {@code scan_entities} 把那一片的**具体坐标**取回来。
     *
     * <p>行为断言打在零依赖的 {@link EntityScan.Pure} 上（all 不筛档 / passive 只留 PASSIVE /
     * hostile 不含 DANGEROUS 与 NEUTRAL（DANGEROUS 单独一档）/ radius 边界包含 / 距离升序 /
     * limit 截断且 total 是真实总数 / limit 超上限钳到 48、radius 超上限如实拒绝 / 认不出的实体归
     * unknown），形态用静态扫描钉住（分发表里有这个 case、handler 读 radius/kind/limit、
     * **只读**（不碰 Pathfinder/InputIsolation）、纯逻辑那半不碰游戏类、分档复用 MobPolicy、
     * 只读命令**不**进 NEW_TASK_COMMANDS）。
     */
    private static void entityScanChecks() {
        Path actionRoot = Path.of("src", "main", "java", "com", "lcu", "lcumod", "action");
        Path executor = actionRoot.resolve("ActionExecutor.java");
        Path entityScan = actionRoot.resolve("EntityScan.java");

        // ① kind=all 不筛档：四档生物 + 认不出的 + 玩家 + 掉落物，一个不落
        java.util.List<EntityScan.Pure.Row> mixed = new java.util.ArrayList<>();
        mixed.add(scanMob(1, "PASSIVE", 2.0));
        mixed.add(scanMob(2, "HOSTILE", 3.0));
        mixed.add(scanMob(3, "DANGEROUS", 4.0));
        mixed.add(scanMob(4, "NEUTRAL", 5.0));
        mixed.add(scanMob(5, "UNKNOWN", 6.0));
        mixed.add(scanPlayer(6, 7.0));
        mixed.add(scanItem(7, 8.0));
        EntityScan.Pure.Result all = EntityScan.Pure.scan(mixed, "all", 16, 24);
        check("entity-scan: kind=all filters nothing (7 of 7)",
                all.total() == 7 && all.rows().size() == 7 && !all.truncated());

        // ② kind=passive 只留 PASSIVE（认不出的 / 玩家 / 掉落物都不在）
        EntityScan.Pure.Result passive = EntityScan.Pure.scan(mixed, "passive", 16, 24);
        check("entity-scan: kind=passive keeps exactly the PASSIVE rows",
                passive.total() == 1 && passive.rows().get(0).name().equals("mob1"));
        check("entity-scan: kind=passive excludes unknown/player/item",
                passive.rows().stream().noneMatch(r -> r.kind().equals("unknown")
                        || r.player() || r.item()));

        // ③ 每档各归各的：hostile **不含** DANGEROUS/NEUTRAL（DANGEROUS 是单独一档）
        EntityScan.Pure.Result hostile = EntityScan.Pure.scan(mixed, "hostile", 16, 24);
        check("entity-scan: kind=hostile is exactly HOSTILE (not dangerous, not neutral)",
                hostile.total() == 1 && hostile.rows().get(0).tier().equals("HOSTILE")
                        && hostile.rows().stream().noneMatch(r -> r.tier().equals("DANGEROUS")
                                || r.tier().equals("NEUTRAL")));
        check("entity-scan: dangerous is its own filter",
                EntityScan.Pure.scan(mixed, "dangerous", 16, 24).total() == 1
                        && EntityScan.Pure.scan(mixed, "dangerous", 16, 24).rows().get(0)
                                .tier().equals("DANGEROUS"));
        check("entity-scan: neutral is its own filter",
                EntityScan.Pure.scan(mixed, "neutral", 16, 24).total() == 1
                        && EntityScan.Pure.scan(mixed, "neutral", 16, 24).rows().get(0)
                                .tier().equals("NEUTRAL"));
        check("entity-scan: player/item are their own filters",
                EntityScan.Pure.scan(mixed, "player", 16, 24).total() == 1
                        && EntityScan.Pure.scan(mixed, "player", 16, 24).rows().get(0).player()
                        && EntityScan.Pure.scan(mixed, "item", 16, 24).total() == 1
                        && EntityScan.Pure.scan(mixed, "item", 16, 24).rows().get(0).item());

        // ④ radius 边界：**刚好等于半径算在内**、超一点排除
        java.util.List<EntityScan.Pure.Row> edge = new java.util.ArrayList<>();
        edge.add(scanMob(1, "PASSIVE", 15.9));
        edge.add(scanMob(2, "PASSIVE", 16.0));
        edge.add(scanMob(3, "PASSIVE", 16.1));
        EntityScan.Pure.Result bounded = EntityScan.Pure.scan(edge, "all", 16, 24);
        check("entity-scan: radius boundary is inclusive (16.0 in, 16.1 out)",
                bounded.total() == 2 && bounded.rows().stream().noneMatch(r -> r.distance() > 16.0));

        // ⑤ 排序：距离升序（同距离按 id 升序 —— 稳定，不让输出抖）
        java.util.List<EntityScan.Pure.Row> messy = new java.util.ArrayList<>();
        messy.add(scanMob(9, "PASSIVE", 9.0));
        messy.add(scanMob(3, "PASSIVE", 1.0));
        messy.add(scanMob(7, "PASSIVE", 5.0));
        messy.add(scanMob(2, "PASSIVE", 5.0));
        EntityScan.Pure.Result sorted = EntityScan.Pure.scan(messy, "all", 16, 24);
        boolean ascending = true;
        for (int i = 1; i < sorted.rows().size(); i++) {
            ascending &= sorted.rows().get(i - 1).distance() <= sorted.rows().get(i).distance();
        }
        check("entity-scan: rows come back sorted by distance ascending", ascending);
        check("entity-scan: equal distances break ties by id (stable)",
                sorted.rows().get(1).id() == 2 && sorted.rows().get(2).id() == 7);

        // ⑥ limit 截断：total 是**截断前**的真实总数、truncated 如实
        java.util.List<EntityScan.Pure.Row> many = new java.util.ArrayList<>();
        for (int i = 0; i < 60; i++) {
            many.add(scanMob(i, "PASSIVE", 1.0 + i * 0.1));
        }
        EntityScan.Pure.Result cut = EntityScan.Pure.scan(many, "all", 32, 24);
        check("entity-scan: limit truncates the returned rows", cut.rows().size() == 24);
        check("entity-scan: total is the real pre-truncation count", cut.total() == 60);
        check("entity-scan: truncated is reported truthfully", cut.truncated());
        EntityScan.Pure.Result fits = EntityScan.Pure.scan(many.subList(0, 10), "all", 32, 48);
        check("entity-scan: nothing is truncated when everything fits",
                fits.total() == 10 && fits.rows().size() == 10 && !fits.truncated());

        // ⑦ limit 超上限**钳到上限**（不放大、也不静默改成别的数）
        check("entity-scan: limit is clamped to the 48 cap",
                EntityScan.Pure.clampLimit(1000) == 48 && EntityScan.Pure.clampLimit(48) == 48
                        && EntityScan.Pure.clampLimit(24) == 24 && EntityScan.Pure.clampLimit(0) == 1);

        // ⑧ radius 超上限**如实拒绝**（绝不偷偷放大成 32）
        check("entity-scan: radius over the 32 cap is refused, never silently enlarged",
                "radius_too_large".equals(EntityScan.Pure.radiusErrorCode(33))
                        && EntityScan.Pure.radiusErrorCode(32) == null
                        && "bad_radius".equals(EntityScan.Pure.radiusErrorCode(0)));

        // ⑨ kind 认不出 ⇒ 如实拒绝；**实体**认不出 ⇒ unknown（all 里在、passive 里不在）
        check("entity-scan: an unknown kind filter is refused (bad_kind)",
                EntityScan.Pure.canonicalKind("cow") == null
                        && "all".equals(EntityScan.Pure.canonicalKind(null))
                        && "all".equals(EntityScan.Pure.canonicalKind("  "))
                        && "passive".equals(EntityScan.Pure.canonicalKind("  PASSIVE ")));
        check("entity-scan: an unrecognized entity tier lands in unknown",
                scanMob(1, "UNKNOWN", 1.0).kind().equals("unknown")
                        && scanMob(2, "WEIRD_NEW_TIER", 1.0).kind().equals("unknown"));
        check("entity-scan: kind=unknown selects only the unrecognized rows",
                EntityScan.Pure.scan(mixed, "unknown", 16, 24).total() == 1
                        && EntityScan.Pure.scan(mixed, "unknown", 16, 24).rows().get(0)
                                .tier().equals("UNKNOWN"));
        check("entity-scan: unknown entities are in kind=all but not in kind=passive",
                EntityScan.Pure.scan(mixed, "all", 16, 24).rows().stream()
                        .anyMatch(r -> r.kind().equals("unknown"))
                        && EntityScan.Pure.scan(mixed, "passive", 16, 24).rows().stream()
                                .noneMatch(r -> r.kind().equals("unknown")));

        // ⑩ 静态扫描：分发表 / handler 形态 / 只读 / 纯逻辑那半零依赖 / 复用分档表 / 不当新任务
        String aeSrc = read(executor);
        String esSrc = read(entityScan);
        check("entity-scan: EntityScan source found", !esSrc.isEmpty());
        check("entity-scan: the command is dispatched",
                aeSrc.contains("case \"scan_entities\" -> handleScanEntities(cmd);"));

        String body = methodBody(aeSrc, "private void handleScanEntities(");
        check("entity-scan: handler reads radius/kind/limit",
                !body.isEmpty() && body.contains("\"radius\"") && body.contains("\"kind\"")
                        && body.contains("\"limit\""));
        check("entity-scan: handler uses the defaults and the pure validators",
                body.contains("EntityScan.DEFAULT_RADIUS") && body.contains("EntityScan.DEFAULT_LIMIT")
                        && body.contains("EntityScan.Pure.radiusErrorCode(")
                        && body.contains("EntityScan.Pure.clampLimit("));
        check("entity-scan: the caps are declared once (16/32 radius, 24/48 limit)",
                esSrc.contains("DEFAULT_RADIUS = 16") && esSrc.contains("MAX_RADIUS = 32")
                        && esSrc.contains("DEFAULT_LIMIT = 24") && esSrc.contains("MAX_LIMIT = 48"));
        check("entity-scan: the handler is read-only (no pathfinding, no input writes)",
                !body.contains("Pathfinder.") && !body.contains("InputIsolation.")
                        && !body.contains("navigateTo") && !body.contains("setControlState"));
        check("entity-scan: not in the world is honest (code + ok:false), never an empty list",
                body.contains("\"not_in_world\"") && body.contains("data.addProperty(\"ok\", false)"));

        int pureStart = esSrc.indexOf("public static final class Pure {");
        int pureEnd = esSrc.indexOf("// ── Minecraft 侧");
        String pureSrc = pureStart >= 0 && pureEnd > pureStart ? esSrc.substring(pureStart, pureEnd) : "";
        check("entity-scan: the tested half carries no game classes at all",
                !pureSrc.isEmpty() && !pureSrc.contains("net.minecraft") && !pureSrc.contains("Minecraft"));
        check("entity-scan: tiering reuses the one shared table",
                esSrc.contains("MobPolicy.tier(AttackRules.entityTypeId("));
        check("entity-scan: coordinates are block coords (same rounding as the summary)",
                esSrc.contains("o.addProperty(\"x\", (int) Math.round(row.x()))")
                        && esSrc.contains("o.addProperty(\"z\", (int) Math.round(row.z()))"));
        check("entity-scan: the response carries the frozen fields",
                esSrc.contains("data.addProperty(\"total\", result.total())")
                        && esSrc.contains("data.addProperty(\"returned\", result.rows().size())")
                        && esSrc.contains("data.addProperty(\"truncated\", result.truncated())")
                        && esSrc.contains("data.add(\"entities\", entities)"));
        String newTasks = methodBody(aeSrc, "private static final java.util.Set<String> NEW_TASK_COMMANDS");
        check("entity-scan: a read-only command is not marked as a new task",
                !newTasks.isEmpty() && !newTasks.contains("scan_entities"));
    }

    /** 一条扫描行（零依赖）：id + tier 名 + 距离。 */
    private static EntityScan.Pure.Row scanMob(int id, String tier, double distance) {
        return new EntityScan.Pure.Row(id, "mob" + id, tier, false, false, 0, 64, 0, distance,
                true, 20.0, "", 0, "", 0, 0);
    }

    /** 一条玩家行（零依赖）：person 类。 */
    private static EntityScan.Pure.Row scanPlayer(int id, double distance) {
        return new EntityScan.Pure.Row(id, "p" + id, "", true, false, 0, 64, 0, distance,
                true, 20.0, "", 0, "", 0, 0);
    }

    /** 一条掉落物行（零依赖）：item 类（物品事实与摘要同口径）。 */
    private static EntityScan.Pure.Row scanItem(int id, double distance) {
        return new EntityScan.Pure.Row(id, "minecraft:oak_log", "", false, true, 0, 64, 0, distance,
                false, 0.0, "minecraft:oak_log", 1, "", 240, 5760);
    }

    // ── 12. 键盘/鼠标屏蔽（fail-open，ESC/F12 永不拦）──

    /**
     * 真机事故：上一版键盘屏蔽用"放行清单"，编码没匹配上 ⇒ **把控制开关 F12 和安全出口 ESC 一起吞了**，
     * 人被困在世界里；同时鼠标左右键还是有效的（点在窗口上等于游戏内点击）。
     * 这里把策略钉成行为断言 + 源码形状：
     */
    private static void inputBlockingChecks() {
        java.util.Set<Integer> gameplay = java.util.Set.of(87, 65, 83, 68);   // W A S D
        java.util.Set<Integer> escape = java.util.Set.of(256, 41);            // ESC 的两种可能编码
        java.util.Set<Integer> f12 = java.util.Set.of(301, 69);               // F12 的两种可能编码
        java.util.Set<Integer> allowed = new java.util.HashSet<>();
        allowed.addAll(escape);
        allowed.addAll(f12);

        check("keys: ESC is never blocked", !KeyShield.blocks(256, 256, gameplay, allowed)
                && !KeyShield.blocks(41, 41, gameplay, allowed));
        check("keys: F12 is never blocked", !KeyShield.blocks(301, 301, gameplay, allowed)
                && !KeyShield.blocks(69, 69, gameplay, allowed));
        check("keys: an unknown key passes (fail-open, never lock the human out)",
                !KeyShield.blocks(9999, 9999, gameplay, allowed));
        check("keys: a gameplay key is blocked", KeyShield.blocks(87, 87, gameplay, allowed));
        check("keys: matching on either field works", KeyShield.blocks(0, 87, gameplay, allowed));
        check("keys: no bindings means nothing gets blocked at all",
                !KeyShield.blocks(87, 87, java.util.Set.of(), allowed));
        check("keys: exemption beats the gameplay list",
                !KeyShield.blocks(256, 256, java.util.Set.of(87, 256), allowed));

        String keysSource = read(Path.of("src", "main", "java", "com", "lcu", "lcumod", "mixin",
                "KeyboardHandlerMixin.java"));
        String mouseSource = read(Path.of("src", "main", "java", "com", "lcu", "lcumod", "client",
                "MouseShield.java"));
        check("keys: the shield consults the policy instead of hardcoding an allow list",
                keysSource.contains("KeyShield.blocks(") && keysSource.contains("ALWAYS_ALLOWED"));
        check("keys: ESC and F12 are exempt in more than one encoding",
                keysSource.contains("SDL_SCANCODE_ESCAPE") && keysSource.contains("SDL_SCANCODE_F12")
                        && keysSource.contains("KEY_ESCAPE") && keysSource.contains("KEY_F12"));
        check("keys: the blocked set comes from the player's own bindings",
                keysSource.contains("options.keyUp") && keysSource.contains("keyHotbarSlots"));
        check("mouse: buttons are blocked through the cancelable NeoForge event",
                mouseSource.contains("InputEvent.MouseButton.Pre") && mouseSource.contains("setCanceled(true)"));
        check("mouse: only while the AI holds control, and never over an open screen",
                mouseSource.contains("InputIsolation.isAiControlled()")
                        && mouseSource.contains("mc.gui.screen() != null"));
        check("mouse: only the three real buttons are touched",
                mouseSource.contains("BUTTON_LEFT") && mouseSource.contains("BUTTON_RIGHT")
                        && mouseSource.contains("BUTTON_MIDDLE"));
    }

    // ── 21. 丢物品必须走原版“丢出”通道（服务端真执行） ────────────────

    /**
     * 用户实机根因（2026-10-12）：后端 {@code drop_item -> ok}，但"地上没有掉落物"。
     * {@code mc.player.drop(stack, false, Prediction.PREDICTED)} 是**世界逻辑**那一层，
     * {@code LivingEntity.drop} 第一句就是 {@code if (this.level().isClientSide()) return null;}
     * ⇒ 客户端只删了本地背包（预测"东西没了"），**一个包都没发**，服务器不知道。
     *
     * <p>这里钉三件事：
     * <ol>
     *   <li><b>背包索引 → 菜单槽位</b>的纯逻辑（{@link ActionExecutor.Pure#inventoryIndexToMenuSlot}）
     *       含边界：0、8、9、35、盔甲 36..39、副手 40、以及**没有菜单槽**的 41/42 与越界；</li>
     *   <li><b>数量语义</b>（{@link ActionExecutor.Pure#dropCountFor}）：该丢几个丢几个，不因 API 限制丢整叠；</li>
     *   <li><b>静态扫描</b>：{@code ActionExecutor} 里不许再出现客户端本地丢弃
     *       （{@code .drop(} / {@code Prediction.PREDICTED}），两个处理函数都必须走
     *       {@code handleContainerInput(..., ContainerInput.THROW, ...)} 这条原版通道；
     *       且该通道只允许出现在那一个丢东西的辅助方法里。</li>
     *   <li><b>创造模式不许复制物品</b>：菜单 THROW 在客户端会额外发创造模式丢弃包
     *       （服务端多丢一份），创造模式因此改走"按 Q"的玩家动作包。</li>
     * </ol>
     */
    private static void dropChannelChecks() {
        Path root = Path.of("src", "main", "java", "com", "lcu", "lcumod", "action");
        String ae = read(root.resolve("ActionExecutor.java"));
        if (ae.isEmpty()) {
            check("drop: ActionExecutor source found", false);
            return;
        }

        // ── 纯逻辑：背包索引 → InventoryMenu 菜单槽位 ──
        int missing = ActionExecutor.Pure.MENU_NOT_FOUND;
        check("drop/map: hotbar 0 -> menu 36", ActionExecutor.Pure.inventoryIndexToMenuSlot(0) == 36);
        check("drop/map: hotbar 8 -> menu 44", ActionExecutor.Pure.inventoryIndexToMenuSlot(8) == 44);
        check("drop/map: inv 9 -> menu 9", ActionExecutor.Pure.inventoryIndexToMenuSlot(9) == 9);
        check("drop/map: inv 35 -> menu 35", ActionExecutor.Pure.inventoryIndexToMenuSlot(35) == 35);
        check("drop/map: helmet 39 -> menu 5", ActionExecutor.Pure.inventoryIndexToMenuSlot(39) == 5);
        check("drop/map: boots 36 -> menu 8", ActionExecutor.Pure.inventoryIndexToMenuSlot(36) == 8);
        check("drop/map: offhand 40 -> menu 45", ActionExecutor.Pure.inventoryIndexToMenuSlot(40) == 45);
        check("drop/map: body armor 41 has no menu slot", ActionExecutor.Pure.inventoryIndexToMenuSlot(41) == missing);
        check("drop/map: saddle 42 has no menu slot", ActionExecutor.Pure.inventoryIndexToMenuSlot(42) == missing);
        check("drop/map: -1 is refused", ActionExecutor.Pure.inventoryIndexToMenuSlot(-1) == missing);
        check("drop/map: 43 (container size) is refused", ActionExecutor.Pure.inventoryIndexToMenuSlot(43) == missing);

        boolean allInMenu = true;
        boolean distinct = true;
        java.util.Set<Integer> seen = new java.util.HashSet<>();
        for (int i = 0; i < 36; i++) {
            int menu = ActionExecutor.Pure.inventoryIndexToMenuSlot(i);
            if (menu < 0 || menu > 45) {
                allInMenu = false;
            }
            if (!seen.add(menu)) {
                distinct = false;
            }
        }
        check("drop/map: every inventory slot 0..35 lands in the menu 0..45", allInMenu);
        check("drop/map: the 36 inventory slots map to 36 distinct menu slots", distinct);
        check("drop/map: menu 45 is the offhand and nothing else",
                ActionExecutor.Pure.inventoryIndexToMenuSlot(40) == ActionExecutor.Pure.SHIELD_SLOT
                        && ActionExecutor.Pure.SHIELD_SLOT == 45);
        check("drop/map: the hotbar band starts where InventoryMenu says (36)",
                ActionExecutor.Pure.USE_ROW_SLOT_START == 36);
        check("drop/map: the armor band starts where InventoryMenu says (5)",
                ActionExecutor.Pure.ARMOR_SLOT_START == 5);

        // ── 纯逻辑：数量语义 ──
        check("drop/count: 1 of 64 asks for exactly 1", ActionExecutor.Pure.dropCountFor(1, 64) == 1);
        check("drop/count: 32 of 64 asks for 32, not the whole stack", ActionExecutor.Pure.dropCountFor(32, 64) == 32);
        check("drop/count: asking more than held caps at the stack", ActionExecutor.Pure.dropCountFor(99, 64) == 64);
        check("drop/count: 0 still means one, never zero", ActionExecutor.Pure.dropCountFor(0, 64) == 1);
        check("drop/count: a negative count still means one", ActionExecutor.Pure.dropCountFor(-5, 64) == 1);
        check("drop/count: nothing held drops nothing", ActionExecutor.Pure.dropCountFor(3, 0) == 0);

        // ── 静态扫描（只看代码，注释里提旧写法不算犯规） ──
        String code = codeOnly(ae);
        check("drop/scan: the client-local Player.drop call is gone", !code.contains(".drop("));
        check("drop/scan: no Prediction.PREDICTED left in code", !code.contains("Prediction.PREDICTED"));

        String dropItemBody = methodBody(ae, "private void handleDropItem(WireServer.WireCommand cmd)");
        String dropBody = methodBody(ae, "private void handleDrop(WireServer.WireCommand cmd)");
        String throwBody = methodBody(ae,
                "private static String throwInventorySlot(Minecraft mc, int invSlot, int count)");
        check("drop/scan: handleDropItem found", !dropItemBody.isEmpty());
        check("drop/scan: handleDrop found", !dropBody.isEmpty());
        check("drop/scan: the throw helper found", !throwBody.isEmpty());

        check("drop: handleDropItem fails honestly when the throw is refused",
                dropItemBody.contains("refused != null") && dropItemBody.contains("false, refused"));
        check("drop: handleDrop fails honestly when the throw is refused",
                dropBody.contains("refused != null") && dropBody.contains("false, refused"));
        check("drop/scan: the vanilla THROW channel lives in exactly one place (the helper)",
                countOf(code, "ContainerInput.THROW") == 2);
        check("drop: both handlers share the one throw helper",
                dropItemBody.contains("throwInventorySlot(") && dropBody.contains("throwInventorySlot("));
        check("drop: both handlers report the real count, not the request",
                dropItemBody.contains("dropCountFor(") && dropBody.contains("dropCountFor("));
        check("drop: a not-found item is an honest failure",
                dropItemBody.contains("not found") && dropItemBody.contains("false"));
        check("drop: the helper only claims success by sending",
                throwBody.contains("return null;") && !throwBody.contains(".drop("));

        check("drop: the helper uses the pure mapping",
                throwBody.contains("Pure.inventoryIndexToMenuSlot("));
        check("drop: the helper double-checks the mapping against the live menu",
                throwBody.contains("getContainerSlot() != invSlot"));
        check("drop: button 1 throws the whole stack",
                throwBody.contains(", 1, ContainerInput.THROW"));
        check("drop: button 0 is repeated for partial counts",
                throwBody.contains(", 0, ContainerInput.THROW") && throwBody.contains("i < want; i++"));
        check("drop: the helper refuses while another container is open",
                throwBody.contains("Another container is open"));
        check("drop: the helper refuses when something sits on the cursor",
                throwBody.contains("getCarried().isEmpty()"));
        check("drop: the helper refuses an empty or unknown slot",
                throwBody.contains("is empty") && throwBody.contains("Invalid slot "));
        check("drop: creative mode never uses the click path (it would duplicate)",
                throwBody.contains("hasInfiniteMaterials()") && throwBody.contains("Creative mode: only the selected slot"));
        check("drop: creative mode drops the selected slot through the Q action packets",
                throwBody.contains("gameMode.dropItem(player, true)") && throwBody.contains("gameMode.dropItem(player, false)"));

        int pureStart = ae.indexOf("public static final class Pure {");
        int pureEnd = ae.indexOf("public static int dropCountFor(");
        check("drop: the pure mapping half is really pure (no game classes)",
                pureStart >= 0 && pureEnd > pureStart
                        && !ae.substring(pureStart, pureEnd).contains("net.minecraft"));
    }

    /** 去掉 {@code //} 行注释与块注释：静态扫描只看代码。 */
    private static String codeOnly(String source) {
        StringBuilder out = new StringBuilder(source.length());
        int i = 0;
        while (i < source.length()) {
            char c = source.charAt(i);
            if (c == '/' && i + 1 < source.length() && source.charAt(i + 1) == '/') {
                while (i < source.length() && source.charAt(i) != '\n') {
                    i++;
                }
            } else if (c == '/' && i + 1 < source.length() && source.charAt(i + 1) == '*') {
                i += 2;
                while (i + 1 < source.length() && !(source.charAt(i) == '*' && source.charAt(i + 1) == '/')) {
                    i++;
                }
                i = Math.min(source.length(), i + 2);
            } else {
                out.append(c);
                i++;
            }
        }
        return out.toString();
    }

    /** 取某个成员（含签名行）到下一个成员声明之间的源码片段（静态扫描用）。 */
    private static String methodBody(String source, String signature) {
        int start = source.indexOf(signature);
        if (start < 0) {
            return "";
        }
        int after = start + signature.length();
        int end = source.indexOf("\n    private ", after);
        int pub = source.indexOf("\n    public ", after);
        if (end < 0 || (pub >= 0 && pub < end)) {
            end = pub;
        }
        if (end < 0) {
            end = source.length();
        }
        return source.substring(start, end);
    }

    // ── helpers ──────────────────────────────────────────────────

    private static String read(Path file) {
        try {
            return Files.readString(file, StandardCharsets.UTF_8);
        } catch (Exception ex) {
            return "";
        }
    }

    private static int countOf(String haystack, String needle) {
        int count = 0;
        int from = 0;
        while (true) {
            int idx = haystack.indexOf(needle, from);
            if (idx < 0) {
                return count;
            }
            count++;
            from = idx + needle.length();
        }
    }

    private static boolean throwsIllegalState(Runnable action) {
        try {
            action.run();
            return false;
        } catch (IllegalStateException ex) {
            return true;
        }
    }

    private static boolean throwsIllegalArgument(Runnable action) {
        try {
            action.run();
            return false;
        } catch (IllegalArgumentException ex) {
            return true;
        }
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
