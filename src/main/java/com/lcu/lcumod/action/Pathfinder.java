package com.lcu.lcumod.action;

import com.lcu.lcumod.LCUMod;
import com.lcu.lcumod.network.WireServer;
import net.minecraft.client.Minecraft;
import net.minecraft.core.BlockPos;
import net.minecraft.world.entity.LivingEntity;
import net.minecraft.world.entity.player.Player;
import net.minecraft.world.level.Level;
import net.minecraft.world.level.block.Blocks;
import net.minecraft.world.level.block.state.BlockState;
import net.minecraft.world.phys.AABB;
import net.minecraft.world.phys.Vec3;
import net.minecraft.world.phys.shapes.Shapes;
import net.minecraft.world.phys.shapes.VoxelShape;

import java.util.ArrayList;
import java.util.Collections;
import java.util.Comparator;
import java.util.HashMap;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.PriorityQueue;
import java.util.Set;

/**
 * Player-style pathfinding with grid-based A* search.
 *
 * <p><b>落脚判据 = 玩家脚印内的碰撞剖面，不是"格子一个高度"</b>（用户实机："在非完整方块上，
 * 比如楼梯、梯子、活板门、半砖……有时候能移动，有时候不能"）：
 * <ul>
 *   <li>每格按玩家水平脚印（0.6×0.6，居中）取 {@code getCollisionShape} 的**子区域**，分别求
 *       脚部支撑高度与头部净空（见 {@link #columnProfile}）；</li>
 *   <li>走一步时还要看**靠来向那半块脚印带**（见 {@link #bandsAt}）—— 上楼梯时脚下先踩到的是
 *       下半砖（0.5）而不是格心的上一级（1.0），只按格心算就会把"迈上楼梯"误判成"要跳"；</li>
 *   <li>通行判据（迈 / 跳 / 落 / 爬 / 拒）全部收在零依赖纯逻辑 {@link Pure} 里，可被
 *       {@code verifyWalk} 直接断言；</li>
 *   <li>可攀爬 = 原版 {@code BlockState.isLadder}（含 NeoForge 的"梯子上的活板门也算梯子"）
 *       ⇒ 梯子/藤蔓/脚手架/垂泪藤都标成 CLIMB 节点，靠近后按"前进 + 跳"爬上去；</li>
 *   <li>起点 / 终点 / 每个转向点都必须落在**真能站**的位置上（净空 + 真实碰撞体双确认）。</li>
 * </ul>
 *
 * <p>纪律不变：<b>不搭方块</b>（见 {@link #NO_RESCUE_POLICY}）、视角只有一个写者
 * （{@code InputIsolation}）、对齐闸门转不过去如实停步（{@link AlignmentGuard}）。
 */
public class Pathfinder {

    private static final double PLAYER_HALF_WIDTH = 0.3;
    private static final double PLAYER_HEIGHT = Pure.PLAYER_HEIGHT;
    /** 抬脚就能迈上去的高度差（半砖/地毯/雪层/土径/闭合活板门这些"矮的"都归这里）。 */
    private static final double STEP_UP_HEIGHT = Pure.STEP_UP;
    /** 需要起跳才能上去的高度差上限。 */
    private static final double MAX_JUMP_HEIGHT = Pure.MAX_JUMP;
    /** 可攀爬柱能抬升的高度上限（一次攀爬）。 */
    private static final double MAX_CLIMB_RISE = Pure.MAX_CLIMB_RISE;
    /** 攀爬柱至少能抬升这么多，才值得标成 CLIMB 节点。 */
    private static final double MIN_CLIMB_RISE = Pure.MIN_CLIMB_RISE;
    /** 净空探测上限（格）：够判定"能不能站人"就行，不用扫到天上。 */
    private static final double HEADROOM_CAP = 4.0;
    private static final double WAYPOINT_REACH_XZ = 0.62;
    private static final double WAYPOINT_REACH_Y = 0.9;
    /**
     * 当前导航的**到达盒**半宽（XZ 每轴）：默认 {@link #WAYPOINT_REACH_XZ}；驻守（AFK）用
     * {@code AfkController.arrivalReachXz(double)} 收缩它。路点间推进仍用默认盒 —— 只有**最终目标**的到达判定
     * 走这个值（见 {@link #isAtDestination}）。
     */
    private static double activeReachXz = WAYPOINT_REACH_XZ;
    /** 前视（look-ahead）距离：距离越远看得越远 ⇒ 拐角走弧线而不是"到点才急转"。 */
    private static final double LOOK_AHEAD_MIN = 2.0;
    private static final double LOOK_AHEAD_MAX = 6.0;
    /** 冲刺迟滞（速度曲线 ≥ 0.9 开、< 0.7 关）⇒ 阈值附近不来回抖。 */
    private static final MotionCurve.Hysteresis SPRINT_GATE = new MotionCurve.Hysteresis(0.9, 0.7);
    /**
     * 动态目标（跟随）的**迟滞带**：目标没挪出这个距离就不重算路径。
     *
     * <p>对齐 mineflayer-pathfinder 的 {@code GoalFollow.hasChanged()}：它也只在该实体挪出
     * {@code range²} 时才让引擎重算 —— 重算频率由"目标走了多远"决定，不由 tick 频率决定。
     */
    private static final double RETARGET_HYSTERESIS = 2.5;
    private static final int STUCK_TIMEOUT_TICKS = 40;
    private static final int MAX_SEARCH_EXPANSIONS = 2048;
    /** 视角对准到多少度以内才允许按前进键（保留原意图）。 */
    private static final float ALIGN_TO_WALK_DEG = 65.0f;
    /** 对准到多少度以内才允许冲刺。 */
    private static final float ALIGN_TO_SPRINT_DEG = 18.0f;
    /** 连续多少 tick 仍转不过去 ⇒ 如实停步并上报（见 {@link AlignmentGuard}）。 */
    private static final int ALIGN_TIMEOUT_TICKS = 100;
    /** 同一目标的重算退避（见 {@link ReplanGate}）。 */
    private static final long REPLAN_BACKOFF_MS = ReplanGate.DEFAULT_BACKOFF_MS;

    /**
     * 寻路失败时随原因一起上报的口径（**不搭方块**）。
     *
     * 用户口径：「他尝试搭方块这件事还是不要做了……AI 要是不小心掉到某个坑里、或者被苦力怕炸到坑里，
     * 它会乱搭方块」。因此寻路/自救**绝不**放方块：宁可站着不动、宁可如实失败，也不自己造桥/立柱/填坑。
     * 放方块这件事只保留"显式任务"入口（后端点名物品 + 位置的放置命令），本类不碰。
     *
     * <p>（本文件刻意不出现放方块相关调用的字面量，{@code verifyGuard} 会静态扫描这一点。）
     */
    public static final String NO_RESCUE_POLICY = "bridging/pillar-up disabled; report for replanning";

    private static final int[][] DIRECTIONS = new int[][] {
        {1, 0}, {-1, 0}, {0, 1}, {0, -1},
        {1, 1}, {1, -1}, {-1, 1}, {-1, -1}
    };
    /** {@link #DIRECTIONS} 的单位向量（脚印带偏移量算用）。 */
    private static final double[][] DIRECTION_UNIT = new double[][] {
        {1, 0}, {-1, 0}, {0, 1}, {0, -1},
        {0.70710678, 0.70710678}, {0.70710678, -0.70710678},
        {-0.70710678, 0.70710678}, {-0.70710678, -0.70710678}
    };
    /** {@link #DIRECTIONS} 里第 i 个方向的反方向下标（"靠来向那一带"用它取）。 */
    private static final int[] REVERSE_DIRECTION = new int[] {1, 0, 3, 2, 7, 6, 5, 4};
    /** 脚印探针（复用同一个实例 ⇒ 每格每方向不产生垃圾）。 */
    private static final FootprintProbe PROBE = new FootprintProbe();

    private static Vec3 targetPos = null;
    private static List<Waypoint> currentPath = new ArrayList<>();
    private static int currentPathIndex = 0;
    private static boolean isNavigating = false;
    private static Vec3 lastPosition = null;
    private static int stuckTicks = 0;
    private static String lastFailureReason = "";
    private static String activeRequestId = null;
    /** 最近一次寻路里"最靠近目标的那一次拒绝"（卡住上报要能说清哪一格、为什么）。 */
    private static BlockPos refusalCell = null;
    private static String refusalReason = null;
    private static double refusalScore = Double.POSITIVE_INFINITY;
    /**
     * 最近一次"找可站落脚点"搜索里离目标最近的**被拒格**（起点/终点解析失败时上报用；
     * 每次搜索开头重置）。这样"could not resolve target stand position"不再是一句空话：
     * 用户一眼能看到**哪一格、为什么**（空气/未加载区块 / 净空不足 / 站不下人 / 危险方块）。
     */
    private static BlockPos standingRefusalCell = null;
    private static String standingRefusalReason = null;
    private static double standingRefusalDistance = Double.POSITIVE_INFINITY;
    /** 上一个被拒候选的原因（{@link #createStandingNode} 写、搜索层紧跟其后取走）。 */
    private static String lastStandingReason = null;
    /** 收敛保护：转不过去就如实停步，不原地无限转。 */
    private static final AlignmentGuard ALIGNMENT = new AlignmentGuard(ALIGN_TIMEOUT_TICKS);
    /** 卡住后"重算同一个目标"的退避 + 结果去重（治每秒多条 Calculated path 的抖动）。 */
    private static final ReplanGate REPLAN = new ReplanGate(REPLAN_BACKOFF_MS);
    /** 卡住后的**有界绕行预算**（最多 {@link StuckDetour#MAX_ATTEMPTS} 次，每次换法不同）。 */
    private static final StuckDetour DETOUR = new StuckDetour();
    /** 当前绕行落脚点（null = 直奔 {@link #targetPos}）；到了由 {@link #tick} 恢复"直奔目标"。 */
    private static Vec3 detourPoint = null;

    public static boolean navigateTo(String requestId, double x, double y, double z) {
        return navigateTo(requestId, x, y, z, WAYPOINT_REACH_XZ);
    }

    /**
     * 带自定义**到达盒**（XZ 每轴半宽）的导航。
     *
     * <p>净化场景：驻守（{@code go_afk}）要求"Pathfinder 一报到达，就确实落在站点半径内"，
     * 否则会出现"报在回位却一步不动"（见 {@code AfkController.arrivalReachXz(double)}）。
     */
    public static boolean navigateTo(String requestId, double x, double y, double z, double reachXz) {
        activeReachXz = reachXz > 0.0 && !Double.isNaN(reachXz) ? reachXz : WAYPOINT_REACH_XZ;
        targetPos = new Vec3(x, y, z);
        currentPath.clear();
        currentPathIndex = 0;
        isNavigating = true;
        lastPosition = null;
        stuckTicks = 0;
        lastFailureReason = "";
        activeRequestId = requestId;
        // 新指令：上一次的绕行点与本卡住回合的绕行额度都与本条指令无关，一并归零。
        detourPoint = null;
        DETOUR.reset();
        // 新指令：退避/去重状态归零（每次导航至多上报一次相同结果），收敛计数归零。
        REPLAN.reset();
        ALIGNMENT.reset();
        SPRINT_GATE.reset();
        InputIsolation.clearNavigationLookTarget();
        return calculatePath();
    }

    /**
     * 动态目标的**非破坏性**重定向（"跟随"这类目标自己在动的场景专用）。
     *
     * <p>为什么不能直接用 {@link #navigateTo}：它把 {@code currentPathIndex} / {@code lastPosition}
     * / {@code stuckTicks} / 收敛计数 / 导航视角目标**全部清零重来**。跟随循环每 0.5 秒调一次
     * ⇒ 走了一半的进度、卡住判定历史、朝向收敛全被抹掉 ⇒ 一步都走不出去，看门狗每 3 秒报一次
     * "无位移"并停路，然后跟随循环又把它重开（用户实机：站着不动、被僵尸打死）。
     *
     * <p>口径（对齐 mineflayer-pathfinder {@code GoalFollow.hasChanged} 的迟滞带）：目标没挪出
     * {@link #RETARGET_HYSTERESIS} ⇒ **什么都不做**，继续走现有路；挪出去了 ⇒ 只重算路径，
     * **保留**卡住历史、收敛计数与导航视角目标；重算本身仍受 {@link ReplanGate} 退避约束
     * （最多每 {@link ReplanGate#DEFAULT_BACKOFF_MS} 一次）——重算频率由"目标走了多远"决定，
     * 不由 tick 频率决定。
     *
     * @return true = 现在"有路可走"（可能仍在走旧路）
     */
    public static boolean retarget(String requestId, double x, double y, double z) {
        if (!isNavigating || targetPos == null || currentPath.isEmpty()) {
            // 没在导航（或没有路）：这就是一次新的导航，走完整初始化。
            return navigateTo(requestId, x, y, z);
        }
        activeRequestId = requestId;
        Vec3 next = new Vec3(x, y, z);
        if (targetPos.distanceTo(next) < RETARGET_HYSTERESIS) {
            return true;
        }
        if (!REPLAN.allow("retarget", System.currentTimeMillis())) {
            // 退避窗口内：按现有路继续尝试（不重算、不清进度、不假装到达）。
            return true;
        }
        targetPos = next;
        // 目标挪出了迟滞带：上一次的绕行点是为"旧几何"算的，作废；本卡住回合的绕行额度保留
        // （同一个回合里不该因为目标动了一下就把预算刷新，否则会失去"有界"的意义）。
        detourPoint = null;
        return calculatePath();
    }

    /** {@link #attemptDetour()} 的结果：调用方据此决定"继续走 / 等窗口 / 如实上报"。 */
    public enum DetourOutcome {
        /** 已换到一条新路线（横向绕行点），继续走。 */
        DETOURED,
        /** 退避窗口内，或这次的绕行点没算出路 —— 等下一个窗口再试，**不**升级上报。 */
        WAIT,
        /** 绕行额度用完 —— 调用方如实停路并上报"试过 N 次绕行"。 */
        EXHAUSTED,
        /** 当前没有导航 / 没有目标 —— 无从绕行。 */
        UNAVAILABLE
    }

    /**
     * 卡住后的**自动绕行**：先换一条真的不同的路线再试（用户口径："卡住不要只停"）。
     *
     * <p>换法：把落脚目标沿"玩家→目标"连线的**垂直方向**偏移
     * {@link StuckDetour#LATERAL_OFFSET} 格，算一条到该绕行点的路；走到绕行点后由
     * {@link #tick} 恢复"直奔原目标"。两次绕行的偏移方向相反（见 {@link StuckDetour#consumeOffset()}），
     * 所以每次换法真的不同 —— 不是把同一条路重算一遍。
     *
     * <p>约束（全部由本方法自身保证，调用方只管读 {@link DetourOutcome}）：
     * <ul>
     *   <li><b>有界</b>：一个卡住回合最多 {@link StuckDetour#MAX_ATTEMPTS} 次，超了返回
     *       {@link DetourOutcome#EXHAUSTED}（交调用方如实上报）；</li>
     *   <li><b>限速</b>：重算受 {@link ReplanGate} 退避约束（同一目标每窗口至多一次），
     *       不会每 tick 重算；</li>
     *   <li><b>不做全量重设</b>：不清路径进度之外的东西 —— 卡住历史、朝向收敛计数、导航视角目标
     *       一律保留（那正是"跟着走却一步不动"的根因）；</li>
     *   <li><b>不搭方块</b>：只走既有地形（见 {@link #NO_RESCUE_POLICY}）。</li>
     * </ul>
     */
    public static DetourOutcome attemptDetour() {
        if (!isNavigating || targetPos == null) {
            return DetourOutcome.UNAVAILABLE;
        }
        if (DETOUR.exhausted()) {
            return DetourOutcome.EXHAUSTED;
        }
        if (!REPLAN.allow("detour:" + targetKey(targetPos), System.currentTimeMillis())) {
            // 退避窗口内：等窗口过去再换路，不升级上报（反正也没算路）。
            return DetourOutcome.WAIT;
        }
        double lateral = DETOUR.consumeOffset();
        if (!calculateDetourPath(lateral)) {
            // 这一次的绕行点没算出可走的路：还有额度就等下一个窗口换另一边，没有就如实上报。
            return DETOUR.canAttempt() ? DetourOutcome.WAIT : DetourOutcome.EXHAUSTED;
        }
        if (REPLAN.allowOnce("detourlog:" + targetKey(targetPos) + "#" + DETOUR.attempts())) {
            LCUMod.LOGGER.info("[Pathfinder] Stuck detour {}/{}: veering {} block(s) sideways "
                            + "(walking existing terrain only, no block placing)",
                    DETOUR.attempts(), DETOUR.maxAttempts(), (int) lateral);
        }
        return DetourOutcome.DETOURED;
    }

    /**
     * 把落脚目标横向偏移 {@code lateralOffset} 格，算一条到该绕行点的路。
     *
     * <p>只重算路径：不动 {@link #targetPos}、不清卡住历史/朝向收敛/导航视角目标、不放方块。
     * 算不出路时把 {@link #detourPoint} 复原（别把导航挂在一条空路上）。
     */
    private static boolean calculateDetourPath(double lateralOffset) {
        Minecraft mc = Minecraft.getInstance();
        if (mc.player == null || mc.level == null || targetPos == null) return false;
        Vec3 playerPos = mc.player.position();
        double dx = targetPos.x - playerPos.x;
        double dz = targetPos.z - playerPos.z;
        double horizontal = Math.sqrt(dx * dx + dz * dz);
        if (horizontal < 1.0e-3) {
            // 已经站在目标正上/正下：横向偏移没有意义（绕不出新路）。
            return false;
        }
        double perpX = -dz / horizontal;
        double perpZ = dx / horizontal;
        Vec3 detour = new Vec3(targetPos.x + perpX * lateralOffset, targetPos.y,
                targetPos.z + perpZ * lateralOffset);

        Vec3 previous = detourPoint;
        detourPoint = detour;
        if (!calculatePath()) {
            detourPoint = previous;
            return false;
        }
        return true;
    }

    /** 本卡住回合已经试过几次绕行（如实上报时要说明白"试过 N 次"）。 */
    public static int detourAttempts() {
        return DETOUR.attempts();
    }

    /** 绕行额度的上界（{@link StuckDetour#MAX_ATTEMPTS}）。 */
    public static int maxDetourAttempts() {
        return DETOUR.maxAttempts();
    }

    /**
     * 真的走动了（走出一个"卡住回合"）⇒ 绕行额度重置：下一次卡住重新给足
     * {@link StuckDetour#MAX_ATTEMPTS} 次。
     *
     * <p>阈值刻意与卡住看门狗的 0.6 格同口径（**不是** {@link #isStuck} 的 0.05 格）：
     * 被实体推来推去的微小抖动不该算"走动了"，否则绕行额度会被反复刷新，"有界"就名存实亡。
     * 因此重置点只有一个 —— 看门狗判"真走动了"的地方。
     */
    public static void resetDetourBudget() {
        DETOUR.reset();
    }

    /** 当前目标距可以重算还有多少毫秒（0 = 现在就可以；跟随循环据此决定要不要等）。 */
    public static long replanRemainingMs() {
        return REPLAN.remainingMs("retarget", System.currentTimeMillis());
    }

    public static void stop() {
        activeRequestId = null;
        targetPos = null;
        currentPath.clear();
        currentPathIndex = 0;
        isNavigating = false;
        lastPosition = null;
        stuckTicks = 0;
        activeReachXz = WAYPOINT_REACH_XZ;
        // 收工：绕行点/绕行额度一起清（下一段导航是新回合）。
        detourPoint = null;
        DETOUR.reset();
        ALIGNMENT.reset();
        SPRINT_GATE.reset();
        // 交还视角：导航结束后，后端的 look（若有）从下一 tick 起重新生效（见 LookArbiter）。
        InputIsolation.clearNavigationLookTarget();
        // 收工：只交还**导航自己拥有**的位移（不能顺手清掉别人的 —— 所有权见 InputOwnership）。
        InputIsolation.setControlState("navigation", "forward", false);
        InputIsolation.setControlState("navigation", "sprint", false);
        InputIsolation.setControlState("navigation", "jump", false);
    }

    public static void tick(Minecraft mc) {
        if (!isNavigating || targetPos == null) return;
        if (mc.player == null || mc.level == null) return;

        if (!InputIsolation.isAiControlled()) {
            InputIsolation.clearAiControls();
            reportProgress(0.0, "navigation canceled: user override");
            stop();
            return;
        }

        Vec3 playerPos = mc.player.position();
        if (isAtDestination(playerPos, targetPos)) {
            reportProgress(1.0, "arrived");
            stop();
            return;
        }

        // 绕行点到了 ⇒ 恢复"直奔原目标"（绕行点只在卡住时临时生效，这里是唯一的恢复点）。
        if (detourPoint != null && isReached(playerPos, detourPoint)) {
            detourPoint = null;
            calculatePath();
        }

        if (isStuck(playerPos)) {
            stuckTicks = 0;
            long nowMs = System.currentTimeMillis();
            String key = targetKey(targetPos);
            // 卡住就**如实说清"哪一格、为什么"**（用户口径：现在只能靠推理）。
            // 文案形如 "blocked at 12,64,-3: step 1.00 needs jump, no headroom"，并随
            // getLastFailureReason() 一起进 ActionExecutor 的 "stuck: ..." 上报。
            String diagnosis = explainProgressBlock(mc, playerPos);
            lastFailureReason = diagnosis;
            if (REPLAN.allow(key, nowMs)) {
                calculatePath();
            } else if (REPLAN.allowOnce("stuck:" + key + "#" + diagnosis)) {
                // 退避期内**不重复计算**（否则每秒多条 "Calculated path"）；仍按现有路继续尝试。
                // 真的走不动时，由 ActionExecutor 的卡住看门狗如实停路并上报（本地不硬撑、不自救）。
                LCUMod.LOGGER.warn("[Pathfinder] Stuck; replan backed off {}ms for target {}; {}",
                        REPLAN.remainingMs(key, nowMs), key, diagnosis);
            }
        }

        if (currentPath.isEmpty() || currentPathIndex >= currentPath.size()) {
            String key = targetKey(targetPos);
            boolean allowed = REPLAN.allow(key, System.currentTimeMillis());
            if (allowed) {
                calculatePath();
            }
            if (currentPath.isEmpty()) {
                // 真的没有路（或退避期内仍无路）→ 如实上报一次，然后停步（不假装在走、不越过退避重算）。
                String reason = lastFailureReason.isBlank() ? "no path found" : lastFailureReason;
                if (REPLAN.allowOnce("nopath:" + key + "#" + reason)) {
                    LCUMod.LOGGER.warn("[Pathfinder] No path found to target {}", key);
                    // 失败原因只报一次（相同目标 + 相同原因不重复上报）。
                    reportProgress(0.0, reason);
                }
                stop();
                return;
            }
            if (currentPathIndex >= currentPath.size()) {
                // 路点走完但退避窗口还没过：本 tick 不重算，也不越界取点（等窗口过去再看；
                // 真的卡死由 ActionExecutor 的看门狗如实处理）。
                return;
            }
        }

        // 已到达的路点可以连跳（前视取点会让 AI 抄近路，可能一次进入好几个路点的判定圈）。
        while (currentPathIndex < currentPath.size() && isReached(playerPos, currentPath.get(currentPathIndex).pos())) {
            currentPathIndex++;
        }
        if (currentPathIndex >= currentPath.size()) {
            return;
        }

        moveToward(mc, playerPos, currentPath.get(currentPathIndex));
    }

    /**
     * 本 tick 实际要去的落脚点：有绕行点（卡住自救）时先去绕行点，否则直奔 {@link #targetPos}。
     */
    private static Vec3 activeGoal() {
        return detourPoint != null ? detourPoint : targetPos;
    }

    private static boolean calculatePath() {
        Minecraft mc = Minecraft.getInstance();
        Vec3 goalPos = activeGoal();
        if (mc.player == null || mc.level == null || goalPos == null) return false;

        StandingNode start = findClosestStandingNode(mc.level, mc.player, mc.player.position(), 2, 4);
        // 诊断必须紧跟各自那次搜索取走（搜索开头会重置"最近被拒格"）；解析成功就不必构造字符串。
        String startDiagnosis = start == null
                ? describeUnresolvedStandPosition(mc.level, mc.player.position(), 2, 4,
                        "could not resolve current stand position")
                : null;
        StandingNode goal = findClosestStandingNode(mc.level, mc.player, goalPos, 3, 6);
        String goalDiagnosis = goal == null
                ? describeUnresolvedStandPosition(mc.level, goalPos, 3, 6,
                        "could not resolve target stand position")
                : null;
        currentPath.clear();
        currentPathIndex = 0;

        if (start == null || goal == null) {
            lastFailureReason = start == null && goal == null
                ? startDiagnosis + " ; " + goalDiagnosis
                : start == null ? startDiagnosis : goalDiagnosis;
            if (REPLAN.allowOnce("fail:" + failureKey() + "#" + lastFailureReason)) {
                LCUMod.LOGGER.warn("[Pathfinder] {}", lastFailureReason);
            }
            return false;
        }

        if (isGoal(start, goal)) {
            currentPath.add(new Waypoint(goal.standingPos, Pure.Move.WALK));
            if (REPLAN.allowOnce("direct:" + failureKey())) {
                LCUMod.LOGGER.info("[Pathfinder] Start and goal share the same stand node; using direct target point");
            }
            return true;
        }

        List<Waypoint> solved = findPath(mc, start, goal);
        if (solved.isEmpty()) {
            // 寻路失败的正确行为 = **如实上报**，交后端重规划。
            // 绝不"自救"：本地**不做**搭桥 / 立柱上爬 / 填坑（用户实机：掉坑或挨炸后它会乱搭方块）；
            // 也无从断言"搭桥就能过"——所以只如实说"这里没有路，且本地不会造路"。
            // 顺带把"最靠近目标的那个被拒绝的格子 + 原因"带上：用户看到"有时能走有时不能"时，
            // 一眼就能看出病根在哪一格（例：step 1.00 needs jump, no headroom）。
            String blocked = getLastBlockedReport();
            lastFailureReason = String.format("no path from %s to %s (%s)%s",
                    start.supportPos, goal.supportPos, NO_RESCUE_POLICY,
                    blocked.isEmpty() ? "" : "; " + blocked);
            if (REPLAN.allowOnce("fail:" + failureKey() + "#" + lastFailureReason)) {
                LCUMod.LOGGER.warn("[Pathfinder] {}", lastFailureReason);
            }
            return false;
        }

        currentPath.addAll(solved);
        // 相同目标的相同结果只打一次（与后端"变了才打日志"同一原则）——治每秒多条刷屏。
        if (REPLAN.allowOnce("path:" + failureKey() + "#" + currentPath.size())) {
            LCUMod.LOGGER.info("[Pathfinder] Calculated path with {} waypoints", currentPath.size());
        }
        return true;
    }

    /** 当前目标的整数格键 + 出发点格键（日志/上报去重用）。 */
    private static String failureKey() {
        Minecraft mc = Minecraft.getInstance();
        String target = targetKey(activeGoal());
        if (mc == null || mc.player == null) {
            return target;
        }
        Vec3 p = mc.player.position();
        return targetKey(p) + "->" + target;
    }

    private static List<Waypoint> findPath(Minecraft mc, StandingNode start, StandingNode goal) {
        PriorityQueue<SearchNode> open = new PriorityQueue<>(Comparator.comparingDouble(SearchNode::fScore));
        Map<BlockPos, Double> bestG = new HashMap<>();
        Set<BlockPos> closed = new HashSet<>();
        // 本次搜索的"脚印带"缓存（按格）：一格 8 个方向的脚下剖面只算一次。
        Map<Long, double[]> bands = new HashMap<>();
        resetRefusals();

        SearchNode startNode = new SearchNode(start, null, 0.0, heuristic(start.standingPos, goal.standingPos),
                Pure.Move.WALK);
        open.add(startNode);
        bestG.put(start.supportPos, 0.0);

        int expansions = 0;
        while (!open.isEmpty() && expansions < MAX_SEARCH_EXPANSIONS) {
            SearchNode current = open.poll();
            expansions++;

            if (!closed.add(current.node.supportPos)) {
                continue;
            }

            if (isGoal(current.node, goal)) {
                return reconstructPath(current);
            }

            for (Neighbor neighbor : getNeighbors(mc, current.node, goal, bands)) {
                if (closed.contains(neighbor.node().supportPos)) {
                    continue;
                }

                double transitionCost = movementCost(current.node, neighbor.node());
                double nextG = current.gScore + transitionCost;
                double knownBest = bestG.getOrDefault(neighbor.node().supportPos, Double.POSITIVE_INFINITY);
                if (nextG >= knownBest) {
                    continue;
                }

                bestG.put(neighbor.node().supportPos, nextG);
                open.add(new SearchNode(neighbor.node(), current, nextG,
                        heuristic(neighbor.node().standingPos, goal.standingPos), neighbor.move()));
            }
        }

        return Collections.emptyList();
    }

    /**
     * 从当前落脚点出发的**可走**邻居：每个方向先找这一列能不能站（脚印剖面 + 净空），
     * 再按 {@link Pure#classifyStep} 判"迈 / 跳 / 落 / 爬 / 拒"，被拒的把原因记下来
     * （卡住上报要说清哪一格、为什么）。
     *
     * <p>对角线不再额外要求"两个正交格都能站"：那在非完整方块上会误杀（半砖/台阶上斜着走很常见），
     * 而{@link #corridorClear} 的脚印扫描带已经覆盖了"斜着穿过墙角"的碰撞。
     */
    private static List<Neighbor> getNeighbors(Minecraft mc, StandingNode current, StandingNode goal,
                                               Map<Long, double[]> bands) {
        List<Neighbor> neighbors = new ArrayList<>();
        int baseX = current.supportPos.getX();
        int baseZ = current.supportPos.getZ();

        for (int i = 0; i < DIRECTIONS.length; i++) {
            int[] dir = DIRECTIONS[i];
            StandingNode candidate = findStandingNodeAtColumn(mc.level, mc.player,
                    baseX + dir[0], baseZ + dir[1], current.standingPos.y);
            if (candidate == null) {
                noteRefusal(goal, baseX + dir[0], baseZ + dir[1], "no standable spot there (drop/gap or no room)");
                continue;
            }

            Pure.Step step = classifyBetween(mc.level, mc.player, current, candidate, i, bands);
            if (!step.allowed()) {
                noteRefusal(goal, candidate.supportPos.getX(), candidate.supportPos.getZ(),
                        step.reason() + " (foot " + fmt(candidate.standingPos.y) + ")");
                continue;
            }

            neighbors.add(new Neighbor(candidate, step.move()));
        }

        // 攀爬柱上还要能**沿竖直方向**走（梯子/藤蔓/脚手架是往上爬，不是横着走）：
        // 同一列里往上/往下找下一个落脚点（柱内下一格、或柱顶出口那一层）。
        if (!Double.isNaN(current.climbTop())) {
            addVerticalNeighbor(mc, current, neighbors, 1.0, goal);
            addVerticalNeighbor(mc, current, neighbors, -1.0, goal);
        }
        return neighbors;
    }

    /** 攀爬柱上的竖直一步（{@code up = +1} 往上、{@code -1} 往下）；不可站/判据拒绝就跳过。 */
    private static void addVerticalNeighbor(Minecraft mc, StandingNode current, List<Neighbor> neighbors,
                                            double up, StandingNode goal) {
        StandingNode candidate = findStandingNodeAtColumn(mc.level, mc.player,
                current.supportPos.getX(), current.supportPos.getZ(), current.standingPos.y + up);
        if (candidate == null || candidate.supportPos.equals(current.supportPos)) {
            return;
        }
        double delta = candidate.standingPos.y - current.standingPos.y;
        if (up > 0.0 ? delta <= 1.0e-6 : delta >= -1.0e-6) {
            return;
        }
        if (up > 0.0 && candidate.standingPos.y > current.climbTop + 1.0e-6) {
            return;   // 超过柱子能到的高度：不假装能爬那么高
        }
        Pure.Step step = classifyVertical(current, candidate);
        if (!step.allowed()) {
            noteRefusal(goal, candidate.supportPos.getX(), candidate.supportPos.getZ(),
                    step.reason() + " (foot " + fmt(candidate.standingPos.y) + ")");
            return;
        }
        neighbors.add(new Neighbor(candidate, step.move()));
    }

    /**
     * 走这一步的判据：**靠来向那半块脚印带**的高度差 + 整条通道的碰撞 + 目标格的净空，
     * 全部交给零依赖纯逻辑 {@link Pure#classifyStep}（这样"半砖可迈 / 上一级要跳 / 立起来的
     * 活板门是墙 / 梯子可爬"这些规则能被自检直接断言，而不是散在 Minecraft 代码里）。
     */
    private static Pure.Step classifyBetween(Level level, Player player, StandingNode from, StandingNode to,
                                             int dirIndex, Map<Long, double[]> bands) {
        double exitBand = bandsAt(level, from.supportPos, bands)[dirIndex];
        double entryBand = bandsAt(level, to.supportPos, bands)[REVERSE_DIRECTION[dirIndex]];
        // 出发点脚底：玩家实际踩着的高度与"朝这一侧脚下那半块"取较高者（保守）。
        // 攀爬柱节点例外：梯子/藤蔓的"那半块"是薄板，它的顶是整格高，不能当脚底用。
        double fromBase = from.climb() || Double.isNaN(exitBand)
                ? from.standingPos.y
                : Math.max(from.standingPos.y, exitBand);
        double fromFoot = climbAwareFromFoot(from, fromBase);
        // 目标落脚高度：靠来向那半块脚印带（台阶：先踩到下半砖 0.5，而不是格心的上一级 1.0）；
        // 攀爬格用它的**入口**高度（见 columnProfile）。
        double toFoot = to.climb() || Double.isNaN(entryBand) ? to.standingPos.y : entryBand;
        boolean corridor = corridorClear(level, player, from.standingPos, to.standingPos);
        return classifyFor(from, to, fromFoot, toFoot, corridor);
    }

    /**
     * **垂直**一步的判据（攀爬柱内上/下、从柱顶出到上一层）—— 只走同一列，没有水平扫描带。
     */
    private static Pure.Step classifyVertical(StandingNode from, StandingNode to) {
        return classifyFor(from, to, climbAwareFromFoot(from, Double.NaN), to.standingPos.y, true);
    }

    /** 组装 {@link Pure.StepQuery}（出发点脚底 + 目标落脚高度 + 攀爬可达高度）。 */
    private static Pure.Step classifyFor(StandingNode from, StandingNode to, double fromFoot, double toFoot,
                                         boolean corridorClear) {
        double climbReach = 0.0;
        if (to.climb()) {
            climbReach = Math.max(climbReach, to.climbTop - fromFoot);
        }
        if (!Double.isNaN(from.climbTop())) {
            climbReach = Math.max(climbReach, from.climbTop - from.standingPos.y);
        }
        climbReach = Math.max(0.0, Math.min(MAX_CLIMB_RISE, climbReach));
        return Pure.classifyStep(new Pure.StepQuery(fromFoot, toFoot, to.headroom,
                !Double.isNaN(from.climbTop()), to.climb(), climbReach, corridorClear));
    }

    /**
     * 出发点脚底高度：普通格子就是站着的高度；**可攀爬格**上的玩家可以停在"脚下～柱顶"之间任意位置，
     * 所以取离目标更近的那一端（不这样取的话，"梯子顶端再跨一步上楼"会被算成一次过高的抬升，
     * 于是明明爬得上去的出口被判成走不了）。
     */
    private static double climbAwareFromFoot(StandingNode from, double preferred) {
        double base = Double.isNaN(preferred) ? from.standingPos.y : preferred;
        if (Double.isNaN(from.climbTop())) {
            return base;
        }
        double entry = from.standingPos.y;
        double top = Math.max(entry, from.climbTop());
        return Math.abs(base - entry) <= Math.abs(base - top) ? entry : top;
    }

    /**
     * 脚印**扫描带**：沿这一步的整条通道，在行进高度（取两端较高者）上逐点检查玩家碰撞盒。
     *
     * <p>这是"能走"的兜底判据：不只看两端能不能站，还要看**走过去**这一路撞不撞墙 ——
     * 立起来的活板门、栅栏残柱、斜穿墙角都在这里被拒。
     */
    private static boolean corridorClear(Level level, Player player, Vec3 from, Vec3 to) {
        double dx = to.x - from.x;
        double dz = to.z - from.z;
        double horizontal = Math.sqrt(dx * dx + dz * dz);
        double y = Math.max(from.y, to.y);
        int samples = Math.max(3, Math.min(8, 2 + (int) Math.ceil(horizontal * 4.0)));
        for (int i = 1; i <= samples; i++) {
            double t = i / (double) samples;
            if (!hasHeadroom(level, player, new Vec3(from.x + dx * t, y, from.z + dz * t))) {
                return false;
            }
        }
        return true;
    }

    /** 记下"最靠近目标的那一次拒绝"（同一格只记一次，越靠近目标越有诊断价值）。 */
    private static void noteRefusal(StandingNode goal, int x, int z, String reason) {
        double score = Math.abs(x - goal.supportPos.getX()) + Math.abs(z - goal.supportPos.getZ());
        if (refusalReason == null || score < refusalScore) {
            refusalScore = score;
            refusalReason = reason;
            refusalCell = new BlockPos(x, (int) Math.floor(goal.standingPos.y), z);
        }
    }

    private static void resetRefusals() {
        refusalCell = null;
        refusalReason = null;
        refusalScore = Double.POSITIVE_INFINITY;
    }

    private static String fmt(double value) {
        return Double.isNaN(value) ? "?" : String.format("%.2f", value);
    }

    /**
     * 离给定位置最近的**可站**落脚点（起点 / 终点都要落在真能站的位置上，否则会出现
     * "路点永远到不了"的假卡住）。
     *
     * <p>顺带记下"离目标最近的被拒格 + 原因"（{@link #standingRefusalCell}），供
     * {@link #describeUnresolvedStandPosition} 在解析失败时给出**具体**理由。
     */
    private static StandingNode findClosestStandingNode(Level level, Player player, Vec3 pos,
                                                        int horizontalRadius, int verticalRadius) {
        StandingNode best = null;
        double bestDist = Double.POSITIVE_INFINITY;
        int centerX = (int) Math.floor(pos.x);
        int centerY = (int) Math.floor(pos.y);
        int centerZ = (int) Math.floor(pos.z);
        standingRefusalCell = null;
        standingRefusalReason = null;
        standingRefusalDistance = Double.POSITIVE_INFINITY;

        for (int dx = -horizontalRadius; dx <= horizontalRadius; dx++) {
            for (int dz = -horizontalRadius; dz <= horizontalRadius; dz++) {
                for (int dy = verticalRadius; dy >= -verticalRadius; dy--) {
                    BlockPos support = new BlockPos(centerX + dx, centerY + dy, centerZ + dz);
                    StandingNode candidate = createStandingNode(level, player, support);
                    if (candidate == null) {
                        // 记"最靠近目标的那一次拒绝"：越靠近目标越能说明病根在哪一格。
                        double refusalDistance = support.distToCenterSqr(pos.x, pos.y, pos.z);
                        if (standingRefusalCell == null || refusalDistance < standingRefusalDistance) {
                            standingRefusalDistance = refusalDistance;
                            standingRefusalCell = support;
                            standingRefusalReason = lastStandingReason;
                        }
                        continue;
                    }

                    double dist = candidate.standingPos.distanceToSqr(pos);
                    if (dist < bestDist) {
                        bestDist = dist;
                        best = candidate;
                    }
                }
            }
        }
        return best;
    }

    /** "最近被拒格 + 原因"的单行文本（诊断用；没记到就说 fallback）。 */
    private static String lastStandingReasonOr(String fallback) {
        if (standingRefusalCell != null && standingRefusalReason != null) {
            return String.format("nearest cell %d,%d,%d: %s",
                    standingRefusalCell.getX(), standingRefusalCell.getY(), standingRefusalCell.getZ(),
                    standingRefusalReason);
        }
        return standingRefusalReason == null ? fallback : standingRefusalReason;
    }

    /**
     * 解析不出落脚点时给用户的**具体诊断**（用户就是靠这种字符串定位问题的）：
     * 目标坐标 + 搜过的盒子 + 那一列是否在**已加载区域** + 离它最近的被拒格和原因。
     *
     * <p>读了 {@link #standingRefusalCell}，所以必须在对应的 {@link #findClosestStandingNode}
     * 之后**立刻**调用（下一次搜索会重置它）。
     */
    private static String describeUnresolvedStandPosition(Level level, Vec3 pos, int horizontalRadius,
                                                          int verticalRadius, String label) {
        int centerX = (int) Math.floor(pos.x);
        int centerY = (int) Math.floor(pos.y);
        int centerZ = (int) Math.floor(pos.z);
        StringBuilder sb = new StringBuilder(label);
        sb.append(String.format(" at %.1f,%.1f,%.1f", pos.x, pos.y, pos.z));
        sb.append(String.format(" (searched x %d..%d y %d..%d z %d..%d",
                centerX - horizontalRadius, centerX + horizontalRadius,
                centerY - verticalRadius, centerY + verticalRadius,
                centerZ - horizontalRadius, centerZ + horizontalRadius));
        if (!level.isLoaded(new BlockPos(centerX, centerY, centerZ))) {
            // 客户端看不到的区块：方块状态读出来就是空气 ⇒ 没有支撑面。跟"地面不好"是两回事。
            sb.append("; that column is NOT in the loaded area (unloaded chunk or outside build height)");
        }
        if (standingRefusalCell != null) {
            sb.append(String.format("; nearest rejected cell %d,%d,%d: %s",
                    standingRefusalCell.getX(), standingRefusalCell.getY(), standingRefusalCell.getZ(),
                    standingRefusalReason == null ? "not standable" : standingRefusalReason));
        } else {
            sb.append("; no candidate cell in the searched box");
        }
        sb.append(")");
        return sb.toString();
    }

    /** 这一列（x,z）里离参考高度最近的可站落脚点。 */
    private static StandingNode findStandingNodeAtColumn(Level level, Player player, int x, int z, double referenceY) {
        int minY = (int) Math.floor(referenceY) - 3;
        int maxY = (int) Math.floor(referenceY) + 2;
        StandingNode best = null;
        double bestVerticalDistance = Double.POSITIVE_INFINITY;

        for (int y = maxY; y >= minY; y--) {
            StandingNode candidate = createStandingNode(level, player, new BlockPos(x, y, z));
            if (candidate == null) {
                continue;
            }

            double delta = Math.abs(candidate.standingPos.y - referenceY);
            if (delta < bestVerticalDistance) {
                best = candidate;
                bestVerticalDistance = delta;
            }
        }
        return best;
    }

    /**
     * 这一格能不能站人 ⇒ 落脚节点（不能站 ⇒ {@code null}）。
     *
     * <p>判据全部来自**玩家脚印内的真实碰撞形状**（{@link #columnProfile}）：
     * 脚部支撑高度 + 头部净空 + 是否可攀爬（梯子/藤蔓/脚手架/垂泪藤…），最后用真实碰撞盒
     * （{@code level.noCollision}）兜底确认一次 —— 所以路点一定落在真能站的位置上。
     *
     * <p>"能不能站"的**规则本身**在零依赖纯逻辑 {@link Pure#classifyStanding} 里（只有那里
     * 一处口径 ⇒ 自检可以直接拿剖面做行为断言，也保证"平地/半砖/楼梯"这类判据不会被悄悄收紧）。
     */
    private static StandingNode createStandingNode(Level level, Player player, BlockPos supportPos) {
        if (player == null) {
            lastStandingReason = "no client player to test the standing box";
            return null;
        }
        ColumnProfile profile = columnProfile(level, player, supportPos);
        Vec3 standingPos = profile.hasFoot()
                ? new Vec3(supportPos.getX() + 0.5, profile.footY(), supportPos.getZ() + 0.5)
                : null;
        boolean collideFree = standingPos != null && hasHeadroom(level, player, standingPos);
        double climbRise = profile.climbable() ? profile.climbTop() - profile.footY() : 0.0;
        Pure.Standing verdict = Pure.classifyStanding(new Pure.StandingQuery(
                profile.hasFoot(), profile.headroom(), collideFree,
                isDangerousSupport(level.getBlockState(supportPos)),
                profile.climbable(), climbRise));
        if (!verdict.allowed()) {
            lastStandingReason = verdict.reason();
            return null;   // 不能站：原因留给 findClosestStandingNode 做"最近被拒格"诊断。
        }
        double climbTop = profile.climbable() ? profile.climbTop() : Double.NaN;
        // 普通落脚点（脚手架顶这类"既能站又能爬"的格子也走这里：站得住就站，柱高照样记着）。
        return new StandingNode(supportPos, standingPos, profile.headroom(), verdict.climb(), climbTop);
    }

    // ── 脚印剖面：把"一格一个高度"换成"玩家脚印内看到的那一小块碰撞形状" ──

    /**
     * 一格（列）在玩家水平脚印内的碰撞剖面（世界坐标；纯数据，判据在 {@link Pure}）。
     *
     * @param footY    脚印内碰撞面的最高点（玩家实际踩在哪）；脚印内没有碰撞面 ⇒ {@code NaN}
     * @param headroom 从 {@code footY} 起，脚印内的**连续净空**高度（格）
     * @param climbable 是否可攀爬（原版 {@code BlockState.isLadder} 判据）
     * @param climbTop 可攀爬柱能到的高度（世界 Y）；不可攀爬 ⇒ {@code NaN}
     */
    private record ColumnProfile(double footY, double headroom, boolean climbable, double climbTop) {

        static final ColumnProfile NONE = new ColumnProfile(Double.NaN, 0.0, false, Double.NaN);

        boolean hasFoot() {
            return !Double.isNaN(footY);
        }
    }

    /**
     * 一列的脚印剖面。
     *
     * <p><b>为什么不能只用"格子一个高度"</b>：{@code shape.max(Y)} 取的是整个格子的最高点 ——
     * 栅栏/墙的柱子、楼梯的上一级、立起来的活板门都会把它抬到玩家**根本站不到**的高度，
     * 于是路点落在幻影位置（用户实机："有时候能移动，有时候不能"，其实就是"路点永远到不了"）。
     * 按脚印取子区域后：脚高 = 玩家真正踩到的那一块，净空 = 玩家真正能不能把头放进去。
     */
    private static ColumnProfile columnProfile(Level level, Player player, BlockPos pos) {
        BlockState state = level.getBlockState(pos);
        if (isClimbable(state, level, pos, player)) {
            // 攀爬格：入口 = 这一格的地板线（下方那格的可站面），柱顶 = 往上还能爬到哪。
            double entryLocal = entryOffsetLocal(level, pos);
            double entryY = pos.getY() + entryLocal;
            double climbTop = Math.max(entryY, climbColumnTop(level, player, pos));
            return new ColumnProfile(entryY, headroomAbove(level, pos, entryLocal), true, climbTop);
        }
        double topLocal = footprintTopLocal(level, pos);
        if (Double.isNaN(topLocal)) {
            return ColumnProfile.NONE;
        }
        return new ColumnProfile(pos.getY() + topLocal, headroomAbove(level, pos, topLocal), false, Double.NaN);
    }

    /**
     * 脚印内碰撞形状的**最高点**（相对格底，方块单位）；脚印内没有碰撞面 ⇒ {@code NaN}。
     *
     * <p>只看与玩家脚印（0.6×0.6，格心）相交的那些盒子 —— 这正是"按脚印取子区域"。
     */
    private static double footprintTopLocal(Level level, BlockPos pos) {
        return shapeTopLocal(level, pos, pos.getX() + 0.5, pos.getZ() + 0.5);
    }

    /** 脚印中心平移到 (centerX, centerZ) 后，碰撞形状在脚印内的最高点（相对格底）。 */
    private static double shapeTopLocal(Level level, BlockPos pos, double centerX, double centerZ) {
        VoxelShape shape = level.getBlockState(pos).getCollisionShape(level, pos);
        if (shape.isEmpty()) {
            return Double.NaN;
        }
        PROBE.begin(centerX - PLAYER_HALF_WIDTH, centerX + PLAYER_HALF_WIDTH,
                centerZ - PLAYER_HALF_WIDTH, centerZ + PLAYER_HALF_WIDTH, pos.getY(), Double.NaN);
        shape.forAllBoxes(PROBE);
        return PROBE.topLocal();
    }

    /**
     * 从 {@code footLocal} 起、脚印内的**连续净空**（格）：上方最低障碍物的**底面**减去脚高，
     * 夹到 [0, {@link #HEADROOM_CAP}]。
     */
    private static double headroomAbove(Level level, BlockPos pos, double footLocal) {
        double footWorld = pos.getY() + footLocal;
        double lowest = Double.POSITIVE_INFINITY;
        int cells = (int) Math.ceil(HEADROOM_CAP);
        for (int dy = 0; dy <= cells; dy++) {
            BlockPos probe = pos.offset(0, dy, 0);
            VoxelShape shape = level.getBlockState(probe).getCollisionShape(level, probe);
            if (shape.isEmpty()) {
                continue;
            }
            PROBE.begin(pos.getX() + 0.5 - PLAYER_HALF_WIDTH, pos.getX() + 0.5 + PLAYER_HALF_WIDTH,
                    pos.getZ() + 0.5 - PLAYER_HALF_WIDTH, pos.getZ() + 0.5 + PLAYER_HALF_WIDTH,
                    probe.getY(), footWorld);
            shape.forAllBoxes(PROBE);
            lowest = Math.min(lowest, PROBE.lowestAbove());
        }
        if (!Double.isFinite(lowest)) {
            return HEADROOM_CAP;
        }
        return Math.max(0.0, Math.min(HEADROOM_CAP, lowest - footWorld));
    }

    /**
     * 一格 8 个方向的**脚下剖面带**（世界 Y）：脚印沿该方向偏移 0.3 格后，脚印内的最高碰撞面。
     *
     * <p>为什么需要它：进入一级楼梯时玩家脚下先踩到的是这一格**靠来向**的那半块（下半砖 0.5），
     * 而不是格心那半块（上一级 1.0）。只按格心算，就会把"迈上楼梯"误判成"要跳"
     * ⇒ AI 一级一级蹦（用户说的"机械/像外挂"）。{@code NaN} = 那一带没有碰撞面。
     */
    private static double[] bandsAt(Level level, BlockPos pos, Map<Long, double[]> cache) {
        double[] cached = cache.get(pos.asLong());
        if (cached != null) {
            return cached;
        }
        double[] bands = new double[DIRECTIONS.length];
        for (int i = 0; i < DIRECTIONS.length; i++) {
            double[] dir = DIRECTION_UNIT[i];
            bands[i] = shapeTopLocal(level, pos,
                    pos.getX() + 0.5 + dir[0] * PLAYER_HALF_WIDTH,
                    pos.getZ() + 0.5 + dir[1] * PLAYER_HALF_WIDTH);
        }
        cache.put(pos.asLong(), bands);
        return bands;
    }

    /** 攀爬格的**入口高度**（相对格底）：站到这一格时脚底在哪（下方那格的可站面；下方悬空则取格底）。 */
    private static double entryOffsetLocal(Level level, BlockPos pos) {
        double belowTop = footprintTopLocal(level, pos.below());
        if (Double.isNaN(belowTop)) {
            return 0.0;
        }
        return Math.max(-1.0, Math.min(1.0, belowTop - 1.0));
    }

    /**
     * 攀爬柱能到的高度（世界 Y）：柱内每一格"站得下"的脚底位置、攀爬格自身的可踩面（脚手架顶）、
     * 以及柱顶之上那一格（若是空地且站得下 ⇒ 站在梯子最上面一格往外走就是出口）。
     *
     * <p>**绝不假装能穿过方块**：柱顶之上被方块堵住就到此为止（该位置的身体放不下就停下），
     * 上限 {@link #MAX_CLIMB_RISE} 格 —— 越过上限就不算"能爬上去"。
     */
    private static double climbColumnTop(Level level, Player player, BlockPos pos) {
        double best = Double.NEGATIVE_INFINITY;
        BlockPos cursor = pos;
        for (int step = 0; step <= (int) MAX_CLIMB_RISE; step++) {
            if (!isClimbable(level.getBlockState(cursor), level, cursor, player)) {
                break;
            }
            if (fitsAt(level, player, cursor, cursor.getY())) {
                best = Math.max(best, cursor.getY());          // 脚底停在这一格：身体放得下
            }
            double topLocal = footprintTopLocal(level, cursor);
            if (!Double.isNaN(topLocal)) {
                best = Math.max(best, cursor.getY() + topLocal);   // 攀爬格自身能踩（脚手架顶）
            }
            BlockPos above = cursor.above();
            if (!isClimbable(level.getBlockState(above), level, above, player)) {
                if (Double.isNaN(footprintTopLocal(level, above)) && fitsAt(level, player, above, above.getY())) {
                    best = Math.max(best, above.getY());       // 柱顶之上是空地 ⇒ 出口在柱顶这一格的上沿
                }
                break;
            }
            cursor = above;
        }
        return best;
    }

    /** 在某一格的某个脚底高度上，玩家碰撞盒放得下吗（身体塞不进去就不算"能站/能爬上去"）。 */
    private static boolean fitsAt(Level level, Player player, BlockPos pos, double footY) {
        return hasHeadroom(level, player, new Vec3(pos.getX() + 0.5, footY, pos.getZ() + 0.5));
    }

    /**
     * 可攀爬的判据用**原版口径**（不猜）：{@code BlockState.isLadder(level, pos, entity)} ——
     * 默认实现是 {@code state.is(BlockTags.CLIMBABLE)}（梯子/藤蔓/脚手架/垂泪藤…），
     * 活板门另有覆盖实现（梯子上方的活板门也算梯子）。见 javap 取证。
     */
    private static boolean isClimbable(BlockState state, Level level, BlockPos pos, LivingEntity entity) {
        return state.isLadder(level, pos, entity);
    }

    /** 一格在脚印内的碰撞剖面探针（不分配对象/列表；一次探一格，两种问法）。 */
    private static final class FootprintProbe implements Shapes.DoubleLineConsumer {

        private double minX;
        private double maxX;
        private double minZ;
        private double maxZ;
        private double cellBaseY;
        private double footWorld;
        private double topLocal;
        private double lowestAbove;

        void begin(double minX, double maxX, double minZ, double maxZ, double cellBaseY, double footWorld) {
            this.minX = minX;
            this.maxX = maxX;
            this.minZ = minZ;
            this.maxZ = maxZ;
            this.cellBaseY = cellBaseY;
            this.footWorld = footWorld;
            this.topLocal = Double.NaN;
            this.lowestAbove = Double.POSITIVE_INFINITY;
        }

        @Override
        public void consume(double x1, double y1, double z1, double x2, double y2, double z2) {
            if (x2 <= minX || x1 >= maxX || z2 <= minZ || z1 >= maxZ) {
                return;   // 与玩家脚印不相交 ⇒ 与"能不能站"无关
            }
            if (Double.isNaN(topLocal) || y2 > topLocal) {
                topLocal = y2;
            }
            if (Double.isNaN(footWorld)) {
                return;
            }
            double worldBottom = cellBaseY + y1;
            double worldTop = cellBaseY + y2;
            if (worldTop > footWorld && worldBottom < lowestAbove) {
                lowestAbove = worldBottom;
            }
        }

        double topLocal() {
            return topLocal;
        }

        double lowestAbove() {
            return lowestAbove;
        }
    }

    private static boolean hasHeadroom(Level level, Player player, Vec3 standingPos) {
        AABB box = playerBoxAt(standingPos);
        return level.noCollision(player, box);
    }

    private static AABB playerBoxAt(Vec3 standingPos) {
        return new AABB(
            standingPos.x - PLAYER_HALF_WIDTH,
            standingPos.y,
            standingPos.z - PLAYER_HALF_WIDTH,
            standingPos.x + PLAYER_HALF_WIDTH,
            standingPos.y + PLAYER_HEIGHT,
            standingPos.z + PLAYER_HALF_WIDTH
        );
    }

    private static boolean isDangerousSupport(BlockState state) {
        return state.is(Blocks.LAVA)
            || state.is(Blocks.FIRE)
            || state.is(Blocks.SOUL_FIRE)
            || state.is(Blocks.CACTUS)
            || state.is(Blocks.CAMPFIRE)
            || state.is(Blocks.SOUL_CAMPFIRE);
    }

    private static double heuristic(Vec3 from, Vec3 to) {
        return Math.abs(from.x - to.x) + Math.abs(from.z - to.z) + Math.abs(from.y - to.y) * 1.25;
    }

    private static double movementCost(StandingNode from, StandingNode to) {
        double dx = to.standingPos.x - from.standingPos.x;
        double dz = to.standingPos.z - from.standingPos.z;
        double horizontal = Math.sqrt(dx * dx + dz * dz);
        double vertical = to.standingPos.y - from.standingPos.y;
        double cost = horizontal;
        if (vertical > STEP_UP_HEIGHT) {
            cost += 1.1;
        } else if (vertical > 0.05) {
            cost += 0.35;
        } else if (vertical < -1.0) {
            cost += 0.4;
        }
        return cost;
    }

    private static boolean isGoal(StandingNode current, StandingNode goal) {
        return current.supportPos.getX() == goal.supportPos.getX()
            && current.supportPos.getZ() == goal.supportPos.getZ()
            && Math.abs(current.standingPos.y - goal.standingPos.y) <= MAX_JUMP_HEIGHT;
    }

    private static List<Waypoint> reconstructPath(SearchNode goal) {
        List<Waypoint> path = new ArrayList<>();
        SearchNode cursor = goal;
        while (cursor != null) {
            path.add(new Waypoint(cursor.node.standingPos, cursor.move));
            cursor = cursor.parent;
        }
        Collections.reverse(path);
        if (!path.isEmpty()) {
            path.remove(0);   // 起点不是"要走的点"
        }
        return path;
    }

    /**
     * 朝当前路点走一步：**视角瞄路径前方一点**（远距离看得更远 ⇒ 拐角提前转、走弧线），
     * **速度按曲线给**（朝向偏差大就降速、直线远距离才冲刺），**跳只由分类结论驱动**
     * （见 {@link Waypoint#needsJump()}）。
     *
     * <p>纪律不变：视角只**发布目标**，真正写视角的仍然只有 {@code InputIsolation} 一处；
     * 转不过去照样如实停步（{@link AlignmentGuard}）。
     */
    private static void moveToward(Minecraft mc, Vec3 current, Waypoint waypoint) {
        Vec3 target = waypoint.pos();
        double dx = target.x - current.x;
        double dz = target.z - current.z;
        double horizontalDistance = Math.sqrt(dx * dx + dz * dz);
        if (horizontalDistance < 0.01) {
            // 纯竖直路点（攀爬柱上一格 / 柱顶出口）：视角保持不动（还贴着梯子/藤蔓）。
            // 往上爬才给"前进 + 跳"（原版就是贴着梯子按前进/跳往上爬）；往下就松手滑下去，
            // 按住前进只会往上爬，反而到不了下一个路点。
            ALIGNMENT.reset();
            SPRINT_GATE.reset();
            boolean climbingUp = waypoint.needsJump() && target.y > current.y + 0.05;
            InputIsolation.setControlState("navigation", "forward", climbingUp);
            InputIsolation.setControlState("navigation", "sprint", false);
            InputIsolation.setControlState("navigation", "jump", climbingUp);
            return;
        }

        // 前视取点：瞄"再往前一段"的位置（前方被挡就退回瞄当前路点 —— 宁可少弧一点，也不贴墙角拐）。
        Vec3 aim = lookAheadPoint(mc, current, target);
        double aimDx = aim.x - current.x;
        double aimDz = aim.z - current.z;
        double aimHorizontal = Math.sqrt(aimDx * aimDx + aimDz * aimDz);
        if (aimHorizontal < 1.0e-4) {
            aimDx = dx;
            aimDz = dz;
            aimHorizontal = horizontalDistance;
        }
        float targetYaw = (float) Math.toDegrees(Math.atan2(-aimDx, aimDz));
        float targetPitch = (float) Math.toDegrees(-Math.atan2(aim.y - current.y, aimHorizontal));

        // View: publish the target only. InputIsolation (the single writer) turns toward it
        // gradually (curve-shaped, finite angular acceleration) this tick.
        InputIsolation.setNavigationLookTarget(targetYaw, targetPitch);

        // Alignment gate (intent preserved): only walk once we are roughly facing the waypoint.
        float yawDiff = LookArbiter.yawDiff(mc.player.getYRot(), targetYaw);
        boolean alignedEnoughToWalk = Math.abs(yawDiff) < ALIGN_TO_WALK_DEG;

        // Convergence guard: if we keep failing to face the path, report honestly and stop —
        // never spin in place, never pretend to be walking.
        if (ALIGNMENT.tick(alignedEnoughToWalk)) {
            String reason = String.format(
                    "cannot align with path (yaw off %d deg after %d ticks; player input or collision?)",
                    Math.round(yawDiff), ALIGNMENT.stalledTicks());
            LCUMod.LOGGER.warn("[Pathfinder] {}", reason);
            reportProgress(0.0, reason);
            stop();
            return;
        }

        // 速度曲线：偏差大降速（转弯走弧线）、直线远距离给满速；冲刺键走迟滞 ⇒ 阈值附近不抖。
        double speedScale = MotionCurve.speedScale(Math.abs(yawDiff), horizontalDistance);
        boolean alignedEnoughToSprint = Math.abs(yawDiff) < ALIGN_TO_SPRINT_DEG;
        boolean sprint = alignedEnoughToSprint
                && SPRINT_GATE.update(speedScale)
                && horizontalDistance > MotionCurve.FAR_DISTANCE
                && Math.abs(target.y - current.y) < 0.2;

        InputIsolation.setControlState("navigation", "forward", alignedEnoughToWalk);
        InputIsolation.setControlState("navigation", "sprint", sprint);
        // 迈/跳/落/爬都由**寻路分类**决定：抬脚就能上的（半砖/台阶）不会再多蹦一下，
        // 需要起跳的（净空够的 1 格）才按跳；可攀爬柱只在**要往上**时才按"前进 + 跳"
        // （在同一层走进梯子脚下不该蹦 —— 那正是"机械/像外挂"的观感来源）。
        boolean needsJump = waypoint.needsJump() && target.y - current.y > 0.05;
        InputIsolation.setControlState("navigation", "jump", alignedEnoughToWalk && needsJump);
    }

    /**
     * 前视点：沿**剩余路径**往前走 {@link #LOOK_AHEAD_MIN}..{@link #LOOK_AHEAD_MAX} 格的位置
     * （到目标越远看得越远 ⇒ 用户说的"弧形"），取点规则本身来自零依赖纯逻辑
     * {@link MotionCurve#lookAhead}（端点/距离都能被自检直接断言）。
     *
     * <p>取点还受"脚印扫描带"约束：从玩家到该点的直线如果被挡（正要拐的墙角），就退回瞄当前路点。
     */
    private static Vec3 lookAheadPoint(Minecraft mc, Vec3 playerPos, Vec3 waypoint) {
        Vec3 goal = activeGoal();
        double ahead = MotionCurve.lookAheadDistance(goal == null ? 0.0 : goal.distanceTo(playerPos),
                LOOK_AHEAD_MIN, LOOK_AHEAD_MAX);
        Vec3 alongPath = walkAlongPath(playerPos, ahead);
        Vec3 reference = alongPath == null ? waypoint : alongPath;
        MotionCurve.Point aim = MotionCurve.lookAhead(curvePoint(playerPos), curvePoint(reference), ahead);
        Vec3 point = new Vec3(aim.x(), aim.y(), aim.z());
        if (point.distanceToSqr(playerPos) < 0.01) {
            return waypoint;
        }
        double y = Math.max(playerPos.y, point.y);
        if (!corridorClear(mc.level, mc.player, new Vec3(playerPos.x, y, playerPos.z), new Vec3(point.x, y, point.z))) {
            return waypoint;
        }
        return point;
    }

    private static MotionCurve.Point curvePoint(Vec3 vec) {
        return new MotionCurve.Point(vec.x, vec.y, vec.z);
    }

    /** 沿剩余路径往前走 {@code ahead} 格的那个点（路径比它短就取最后一个点）。 */
    private static Vec3 walkAlongPath(Vec3 from, double ahead) {
        if (currentPath.isEmpty() || currentPathIndex >= currentPath.size()) {
            return null;
        }
        Vec3 previous = from;
        Vec3 last = null;
        double travelled = 0.0;
        for (int i = currentPathIndex; i < currentPath.size(); i++) {
            Vec3 next = currentPath.get(i).pos();
            last = next;
            double segment = previous.distanceTo(next);
            if (travelled + segment >= ahead) {
                double t = segment < 1.0e-6 ? 0.0 : (ahead - travelled) / segment;
                return new Vec3(previous.x + (next.x - previous.x) * t,
                        previous.y + (next.y - previous.y) * t,
                        previous.z + (next.z - previous.z) * t);
            }
            travelled += segment;
            previous = next;
        }
        return last;
    }

    /** 目标的整数格键（退避/去重都用它，避免浮点抖动造成"看起来是新目标"）。 */
    private static String targetKey(Vec3 target) {
        if (target == null) return "none";
        return (int) Math.floor(target.x) + "," + (int) Math.floor(target.y) + "," + (int) Math.floor(target.z);
    }

    private static boolean isStuck(Vec3 currentPos) {
        if (lastPosition == null) {
            lastPosition = currentPos;
            stuckTicks = 0;
            return false;
        }

        if (lastPosition.distanceTo(currentPos) < 0.05) {
            stuckTicks++;
        } else {
            stuckTicks = 0;
            lastPosition = currentPos;
        }
        return stuckTicks >= STUCK_TIMEOUT_TICKS;
    }

    private static boolean isReached(Vec3 current, Vec3 target) {
        return Math.abs(current.x - target.x) <= WAYPOINT_REACH_XZ
            && Math.abs(current.z - target.z) <= WAYPOINT_REACH_XZ
            && Math.abs(current.y - target.y) <= WAYPOINT_REACH_Y;
    }

    /**
     * 最终目标的到达判定：用**当前导航的到达盒**（驻守会收缩它）；路点间推进仍用默认盒。
     */
    private static boolean isAtDestination(Vec3 current, Vec3 target) {
        return Math.abs(current.x - target.x) <= activeReachXz
            && Math.abs(current.z - target.z) <= activeReachXz
            && Math.abs(current.y - target.y) <= WAYPOINT_REACH_Y;
    }

    public static boolean isNavigating() {
        return isNavigating;
    }

    public static Vec3 getTarget() {
        return targetPos;
    }

    public static String getStatusString() {
        if (!isNavigating || targetPos == null) {
            return "Idle";
        }
        return String.format("Navigating to (%.0f, %.0f, %.0f) path=%d idx=%d detour=%d/%d",
            targetPos.x, targetPos.y, targetPos.z, currentPath.size(), currentPathIndex,
            DETOUR.attempts(), DETOUR.maxAttempts());
    }

    public static String getLastFailureReason() {
        return lastFailureReason;
    }

    /**
     * 最近一次寻路里"最靠近目标的那一次拒绝"的可读文案，形如
     * {@code blocked at 12,64,-3: step 1.00 needs jump, no headroom}；没有拒绝 ⇒ 空串。
     */
    public static String getLastBlockedReport() {
        if (refusalCell == null || refusalReason == null) {
            return "";
        }
        return String.format("blocked at %d,%d,%d: %s",
                refusalCell.getX(), refusalCell.getY(), refusalCell.getZ(), refusalReason);
    }

    /**
     * "卡住时报清是哪一格、为什么"：把**当前这一步**（玩家所在落脚点 → 当前路点）重新判一遍。
     *
     * <p>四种如实结论（都不瞎猜）：
     * <ul>
     *   <li>玩家脚下没有可站节点 ⇒ 说"当前位置站不住"（半砖边缘/水里/被推下去）；</li>
     *   <li>路点格没有可站节点 ⇒ 说"路点落在站不住的位置"（本不该发生，发生就是 bug 证据）；</li>
     *   <li>判据拒绝 ⇒ 给出原因（"step 1.00 needs jump, no headroom" / "CLIMB node, no ladder"…）；</li>
     *   <li>判据通过却真没走 ⇒ 如实说"这一步本身可走，是被别的东西挡住（实体/水/碰撞）"。</li>
     * </ul>
     */
    public static String explainProgressBlock(Minecraft mc, Vec3 playerPos) {
        if (mc == null || mc.level == null || mc.player == null) {
            return "no client level to diagnose";
        }
        if (currentPath.isEmpty() || currentPathIndex >= currentPath.size()) {
            return "no waypoint left to walk (path empty)";
        }
        Vec3 waypoint = currentPath.get(currentPathIndex).pos();
        StandingNode from = findClosestStandingNode(mc.level, mc.player, playerPos, 1, 3);
        if (from == null) {
            return String.format("no standable node under the player at %.1f,%.1f,%.1f (not on walkable ground): %s",
                    playerPos.x, playerPos.y, playerPos.z, lastStandingReasonOr("unknown"));
        }
        StandingNode to = findClosestStandingNode(mc.level, mc.player, waypoint, 1, 3);
        if (to == null) {
            return String.format("waypoint %s has no standable node (path point is not walkable ground): %s",
                    targetKey(waypoint), lastStandingReasonOr("unknown"));
        }
        int dirIndex = directionIndex(to.supportPos.getX() - from.supportPos.getX(),
                to.supportPos.getZ() - from.supportPos.getZ());
        if (dirIndex < 0) {
            if (to.supportPos.getX() == from.supportPos.getX() && to.supportPos.getZ() == from.supportPos.getZ()) {
                // 同一列的竖直一步（攀爬柱上/下、柱顶出口）
                Pure.Step vertical = classifyVertical(from, to);
                return vertical.allowed()
                        ? String.format("climb step %s -> %s looks fine (%s) but there was no progress"
                                        + " -> blocked by something else (entity/ladder lost)",
                                fmt(from.standingPos.y), fmt(to.standingPos.y), vertical.move())
                        : String.format("blocked at %s: %s", targetKey(waypoint), vertical.reason());
            }
            return String.format("waypoint %s is not an adjacent cell (stale path)", targetKey(waypoint));
        }
        Pure.Step step = classifyBetween(mc.level, mc.player, from, to, dirIndex, new HashMap<>());
        if (step.allowed()) {
            return String.format(
                    "step to %s looks walkable (%s) but there was no progress -> blocked by something else"
                            + " (entity/water/collision)",
                    targetKey(waypoint), step.move());
        }
        return String.format("blocked at %s: %s", targetKey(waypoint), step.reason());
    }

    /** 方向下标（{@link #DIRECTIONS} 里等于 (dx,dz) 的那一个）；不是相邻格 ⇒ -1。 */
    private static int directionIndex(int dx, int dz) {
        for (int i = 0; i < DIRECTIONS.length; i++) {
            if (DIRECTIONS[i][0] == dx && DIRECTIONS[i][1] == dz) {
                return i;
            }
        }
        return -1;
    }

    private static void reportProgress(double progress, String message) {
        if (activeRequestId != null && LCUMod.WIRE != null) {
            LCUMod.WIRE.sendProgress(activeRequestId, progress, message);
        }
    }

    // ── 通行判据（零依赖纯逻辑）──────────────────────────────────

    /**
     * 非完整方块 / 台阶 / 攀爬的**通行判据**（纯逻辑，零 Minecraft 依赖 ⇒ {@code verifyWalk}
     * 可以直接做行为断言）。
     *
     * <p>用户口径（原话）："在一些非完整方块上，比如说是楼梯、梯子、活板门、半砖……有时候能移动，
     * 有时候不能"。所以每一条规则都写成可断言的纯函数，而不是散在 Minecraft 调用里：
     * <ul>
     *   <li>净空够 + 支撑高度差 ≤ {@link #STEP_UP} ⇒ {@link Move#WALK}（半砖/地毯/雪层/土径/
     *       闭合活板门这些"矮的"都归这里）；</li>
     *   <li>高 1 格左右 + 头顶净空够 ⇒ {@link Move#JUMP_UP}；**净空不够 ⇒ 拒绝**，理由带
     *       "needs jump, no headroom"；</li>
     *   <li>下降 ≤ {@link #MAX_DROP} ⇒ {@link Move#DROP_DOWN}（摔不死）；</li>
     *   <li>可攀爬（梯子/藤蔓/脚手架/垂泪藤…）⇒ {@link Move#CLIMB}；没有柱 ⇒ 拒绝
     *       "CLIMB node, no ladder"；</li>
     *   <li>目标格站不住（脚印内没有支撑面）⇒ 拒绝 "no support here: bridging/pillar-up disabled"
     *       —— <b>不搭方块/不立柱</b> 的策略不变（见 {@link #NO_RESCUE_POLICY}）。</li>
     * </ul>
     */
    public static final class Pure {

        /** 玩家高度（格）：站人的净空要求。 */
        public static final double PLAYER_HEIGHT = 1.8;
        /** 抬脚就能迈上去的高度差。 */
        public static final double STEP_UP = 0.6;
        /** 需要起跳才能上去的高度差上限。 */
        public static final double MAX_JUMP = 1.2;
        /** 允许下落（摔不死）的高度差上限。 */
        public static final double MAX_DROP = 3.2;
        /** 可攀爬柱一次能抬升的高度上限。 */
        public static final double MAX_CLIMB_RISE = 6.0;
        /** 攀爬柱至少能抬升这么多，才值得走"爬"这条路。 */
        public static final double MIN_CLIMB_RISE = 1.0;

        private Pure() {
        }

        /** 一步怎么走。 */
        public enum Move {
            /** 抬脚走（高度差 ≤ STEP_UP）。 */
            WALK,
            /** 起跳上去。 */
            JUMP_UP,
            /** 落下去（≤ MAX_DROP）。 */
            DROP_DOWN,
            /** 攀爬（梯子/藤蔓/脚手架/垂泪藤…）。 */
            CLIMB,
            /** 拒绝（见 {@link Step#reason()}）。 */
            REFUSE
        }

        /**
         * 一步的判据结果。
         *
         * @param move   分类结论
         * @param reason 拒绝原因（机器可读、可进日志/上报）；允许时为空串
         */
        public record Step(Move move, String reason) {

            public boolean allowed() {
                return move != Move.REFUSE;
            }
        }

        /**
         * 一次通行的输入剖面。
         *
         * @param fromFoot       出发点脚底高度（世界 Y；已含"朝这一侧脚下那半块"的保守取值）
         * @param toFoot         目标落脚高度（世界 Y；台阶取**靠来向那半块**的脚下剖面）
         * @param toHeadroom     目标格脚底以上的连续净空（格）
         * @param fromClimb      出发点就在攀爬柱上
         * @param toClimb        目标格可攀爬
         * @param climbReach     这一步能从攀爬里拿到的抬升（格，0 = 没有柱）
         * @param corridorClear  两点之间"脚印扫描带"在行进高度上无碰撞
         */
        public record StepQuery(double fromFoot, double toFoot, double toHeadroom,
                                boolean fromClimb, boolean toClimb, double climbReach,
                                boolean corridorClear) {
        }

        private static Step allow(Move move) {
            return new Step(move, "");
        }

        private static Step refuse(String reason) {
            return new Step(Move.REFUSE, reason);
        }

        /** 这一步能不能走、怎么走（规则见 {@link Pure} 的类注释）。 */
        public static Step classifyStep(StepQuery q) {
            if (q.toClimb()) {
                if (q.climbReach() < MIN_CLIMB_RISE) {
                    return refuse("CLIMB node, no ladder");
                }
                double rise = q.toFoot() - q.fromFoot();
                if (rise > q.climbReach() + 1.0e-6) {
                    return refuse(String.format("CLIMB node: ladder only reaches %.2f up (need %.2f)",
                            q.climbReach(), rise));
                }
                if (rise > MAX_CLIMB_RISE) {
                    return refuse(String.format("CLIMB node: rise %.2f > max %.1f", rise, MAX_CLIMB_RISE));
                }
                return allow(Move.CLIMB);
            }
            if (Double.isNaN(q.toFoot())) {
                return refuse("no support here: bridging/pillar-up disabled");
            }
            if (!q.corridorClear()) {
                return refuse("path blocked by collider (footprint sweep)");
            }
            double delta = q.toFoot() - q.fromFoot();
            if (q.toHeadroom() + 1.0e-6 < PLAYER_HEIGHT) {
                if (delta > STEP_UP) {
                    return refuse(String.format("step %.2f needs jump, no headroom", delta));
                }
                return refuse(String.format("no headroom (%.2f < %.1f)", q.toHeadroom(), PLAYER_HEIGHT));
            }
            // 从最佳可达位置起算，一步最多抬升 MAX_JUMP（攀爬柱已经把"能到多高"算进 fromFoot 了）。
            if (delta > MAX_JUMP) {
                return q.fromClimb()
                        ? refuse(String.format("climb exit %.2f too high (ladder reaches %.2f)",
                                delta, q.climbReach()))
                        : refuse(String.format("step %.2f too high (max jump %.1f)", delta, MAX_JUMP));
            }
            if (delta < -MAX_DROP) {
                return refuse(String.format("drop %.2f too deep (max %.1f)", -delta, MAX_DROP));
            }
            if (delta > STEP_UP) {
                return allow(Move.JUMP_UP);
            }
            if (delta < -0.05) {
                return allow(Move.DROP_DOWN);
            }
            return allow(Move.WALK);
        }

        // ── 落脚点（"这一格能不能站人"）的判据：同样零依赖，跟步子判据同一处口径 ──

        /**
         * 一格"能不能站人"的**剖面输入**（世界无关的纯数据；由 {@code Pathfinder.columnProfile}
         * 从玩家脚印内的碰撞形状量出来）。
         *
         * @param footPresent 脚印内有支撑面（不是空气 / 未加载区块 / 无碰撞形状）
         * @param headroom    从脚底起的连续净空（格）
         * @param collideFree 玩家碰撞盒放在落脚点上时不撞任何东西（真实 {@code noCollision}）
         * @param dangerous   支撑方块本身危险（岩浆/火/仙人掌/营火）
         * @param climbable   这一格可攀爬（原版 {@code BlockState.isLadder}）
         * @param climbRise   能从这根攀爬柱拿到的抬升（格；不可攀爬传 0）
         */
        public record StandingQuery(boolean footPresent, double headroom, boolean collideFree,
                                    boolean dangerous, boolean climbable, double climbRise) {
        }

        /**
         * "这一格能不能站"的判据结果。
         *
         * @param allowed 接受（可以在这格落脚）
         * @param climb   接受且是**攀爬柱入口**（靠近后按前进 + 跳往上爬）
         * @param reason  拒绝原因（机器可读、可进日志/上报）；接受时为空串
         */
        public record Standing(boolean allowed, boolean climb, String reason) {
        }

        private static Standing acceptStanding(boolean climb) {
            return new Standing(true, climb, "");
        }

        private static Standing refuseStanding(String reason) {
            return new Standing(false, false, reason);
        }

        /**
         * 这一格能不能站人（普通平地 / 半砖 / 楼梯 / 闭合活板门 / 地毯都走"普通落脚点"这条路）。
         *
         * <p>口径与 {@link #classifyStep} 一致，且只有这一处：
         * <ul>
         *   <li>脚印内没有支撑面（空气 / **未加载区块** / 没有碰撞形状）⇒ 拒绝
         *       {@code no support within footprint ...}；</li>
         *   <li>落脚点上放不下玩家碰撞盒 ⇒ 拒绝 {@code standing box collides ...}；</li>
         *   <li>可攀爬且真能抬升（≥ {@link #MIN_CLIMB_RISE}）⇒ 接受为**攀爬柱入口**；</li>
         *   <li>支撑方块危险 ⇒ 拒绝；</li>
         *   <li>净空不够站人（&lt; {@link #PLAYER_HEIGHT}）⇒ 拒绝；</li>
         *   <li>其余 ⇒ 接受为普通落脚点。</li>
         * </ul>
         * <b>绝不搭方块</b>：站不住就是站不住，如实拒绝。
         */
        public static Standing classifyStanding(StandingQuery q) {
            if (!q.footPresent()) {
                return refuseStanding("no support within footprint (air, chunk not loaded, or no collider)");
            }
            if (!q.collideFree()) {
                return refuseStanding("standing box collides (the player does not fit here)");
            }
            if (q.climbable() && q.climbRise() >= MIN_CLIMB_RISE) {
                return acceptStanding(true);
            }
            if (q.dangerous()) {
                return refuseStanding("dangerous support (lava/fire/cactus/campfire)");
            }
            if (q.headroom() + 1.0e-6 < PLAYER_HEIGHT) {
                return refuseStanding(String.format("insufficient headroom (%.2f < %.1f)",
                        q.headroom(), PLAYER_HEIGHT));
            }
            return acceptStanding(false);
        }
    }

    // ── 内部数据结构 ────────────────────────────────────────────

    /**
     * 一个落脚点。
     *
     * @param supportPos  格键（A* 的节点键）
     * @param standingPos 脚底应该站在哪（世界坐标）
     * @param headroom    该格脚底以上的连续净空（格）
     * @param climb       是否"攀爬柱入口"节点（靠近后按前进 + 跳往上爬）
     * @param climbTop    该格**可攀爬**时：这根柱能到的高度（世界 Y）；不可攀爬 ⇒ NaN
     */
    private record StandingNode(BlockPos supportPos, Vec3 standingPos, double headroom,
                                boolean climb, double climbTop) {
    }

    /** 路径点：落脚位置 + 这一步**怎么走**（驱动按键：迈/跳/落/爬）。 */
    private record Waypoint(Vec3 pos, Pure.Move move) {

        boolean needsJump() {
            return move == Pure.Move.JUMP_UP || move == Pure.Move.CLIMB;
        }
    }

    /** 一个可走的邻居 + 走法。 */
    private record Neighbor(StandingNode node, Pure.Move move) {
    }

    private static class SearchNode {
        private final StandingNode node;
        private final SearchNode parent;
        private final double gScore;
        private final double hScore;
        private final Pure.Move move;

        private SearchNode(StandingNode node, SearchNode parent, double gScore, double hScore, Pure.Move move) {
            this.node = node;
            this.parent = parent;
            this.gScore = gScore;
            this.hScore = hScore;
            this.move = move;
        }

        private double fScore() {
            return gScore + hScore;
        }
    }
}
