package com.lcu.lcumod.action;

import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;

/**
 * 零依赖自检（接入 {@code check}）：**视角单一写者 + 寻路收敛/退避**。
 *
 * <p>用户实机根因："AI 在控制中、显示跟着我，但原地罚站"，日志每秒刷
 * {@code Calculated path with 47 waypoints}——后端 look 与寻路转身**两个写者互相覆盖**，
 * {@code yawDiff} 永不收敛 ⇒ 前进键永远 false。本批覆盖：
 *
 * <ol>
 *   <li><b>导航中后端 look 不覆盖路径朝向</b>（{@link LookArbiter} 优先级）；</li>
 *   <li><b>无导航时后端 look 生效</b>（绝对值、一次性）；</li>
 *   <li><b>逐步转不会瞬间对齐</b>（限速 10°/6° per tick，且能收敛）；</li>
 *   <li><b>收敛失败如实停步并回报原因</b>（{@link AlignmentGuard}）；</li>
 *   <li><b>退避期内不重复计算 / 相同结果只报一次</b>（{@link ReplanGate}）；</li>
 *   <li><b>静态扫描</b>：寻路不再直接写视角（{@code setYRot}/{@code setXRot} 只出现在
 *       {@code InputIsolation} 的单一写点）。</li>
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
        alignmentGuardChecks();
        replanGateChecks();
        sourceScanChecks();
        comeVsFollowChecks();
        attackSafetyChecks();
        landmarkMemoryChecks();
        gearReflexChecks();

        System.out.println("[WalkCheck] " + passed + " checks passed, " + failed + " failed");
        if (failed > 0) {
            System.exit(1);
        }
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
        check("ownership: navigating -> turns toward path (-10 after 1 tick)", f.yaw() == -10f);
        check("ownership: navigating -> pitch steps 6 toward path", f.pitch() == 6f);

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

    // ── 3. 逐步转：不会瞬间对齐，且能收敛 ────────────────────────

    private static void gradualTurnChecks() {
        LookArbiter a = new LookArbiter();
        a.setNavigationTarget(180f, 45f);
        LookArbiter.Frame f = a.plan(true, true, 0f, 0f);
        check("gradual: 180deg target is NOT snapped (yaw=10 after 1 tick)", f.yaw() == 10f);
        check("gradual: yaw step is capped at 10", f.yaw() - 0f == LookArbiter.NAV_MAX_YAW_STEP);
        check("gradual: pitch step is capped at 6", f.pitch() == 6f);

        float yaw = 0f;
        float pitch = 0f;
        int ticks = 0;
        LookArbiter sim = new LookArbiter();
        sim.setNavigationTarget(180f, 45f);
        while (LookArbiter.yawDiff(yaw, 180f) != 0f && ticks < 200) {
            LookArbiter.Frame step = sim.plan(true, true, yaw, pitch);
            // 单 tick 位移绝不越界
            if (Math.abs(LookArbiter.yawDiff(yaw, step.yaw())) > LookArbiter.NAV_MAX_YAW_STEP + 0.001f) {
                check("gradual: step never exceeds cap (tick " + ticks + ")", false);
                return;
            }
            yaw = step.yaw();
            pitch = step.pitch();
            ticks++;
        }
        check("gradual: converges to 180 within 18 ticks (got " + ticks + ")", ticks == 18);
        check("gradual: pitch converges to 45", Math.abs(pitch - 45f) < 0.001f);
        check("gradual: once aligned it stays put",
                sim.plan(true, true, yaw, pitch).yaw() == yaw);

        // 跨 ±180 边界：170 -> -170 只差 20 度，往正方向转
        LookArbiter wrap = new LookArbiter();
        wrap.setNavigationTarget(-170f, 0f);
        LookArbiter.Frame wf = wrap.plan(true, true, 170f, 0f);
        check("gradual: crosses the +-180 seam the short way", wf.yaw() == 180f);
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
        check("memory: bed loss is detected from the vanilla respawn point",
                poi.contains("invalidateBedIfGone") && poi.contains("getRespawnData()"));
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
