package com.lcu.lcumod.action;

import com.lcu.lcumod.LCUMod;
import com.lcu.lcumod.network.WireServer;
import net.minecraft.client.Minecraft;
import net.minecraft.core.BlockPos;
import net.minecraft.core.Direction;
import net.minecraft.world.level.Level;
import net.minecraft.world.level.block.Blocks;
import net.minecraft.world.level.block.state.BlockState;
import net.minecraft.world.phys.AABB;
import net.minecraft.world.phys.Vec3;
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
 * Navigation nodes represent the support block the player can stand on.
 * Each candidate stand position is validated with the player's collision box,
 * so modded partial blocks are handled by actual VoxelShape/AABB collision.
 */
public class Pathfinder {

    private static final double PLAYER_HALF_WIDTH = 0.3;
    private static final double PLAYER_HEIGHT = 1.8;
    private static final double STEP_UP_HEIGHT = 0.6;
    private static final double MAX_JUMP_HEIGHT = 1.2;
    private static final double MAX_DROP_HEIGHT = 3.2;
    private static final double WAYPOINT_REACH_XZ = 0.62;
    private static final double WAYPOINT_REACH_Y = 0.9;
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

    private static Vec3 targetPos = null;
    private static List<Vec3> currentPath = new ArrayList<>();
    private static int currentPathIndex = 0;
    private static boolean isNavigating = false;
    private static Vec3 lastPosition = null;
    private static int stuckTicks = 0;
    private static String lastFailureReason = "";
    private static String activeRequestId = null;
    /** 收敛保护：转不过去就如实停步，不原地无限转。 */
    private static final AlignmentGuard ALIGNMENT = new AlignmentGuard(ALIGN_TIMEOUT_TICKS);
    /** 卡住后"重算同一个目标"的退避 + 结果去重（治每秒多条 Calculated path 的抖动）。 */
    private static final ReplanGate REPLAN = new ReplanGate(REPLAN_BACKOFF_MS);

    public static boolean navigateTo(String requestId, double x, double y, double z) {
        targetPos = new Vec3(x, y, z);
        currentPath.clear();
        currentPathIndex = 0;
        isNavigating = true;
        lastPosition = null;
        stuckTicks = 0;
        lastFailureReason = "";
        activeRequestId = requestId;
        // 新指令：退避/去重状态归零（每次导航至多上报一次相同结果），收敛计数归零。
        REPLAN.reset();
        ALIGNMENT.reset();
        InputIsolation.clearNavigationLookTarget();
        return calculatePath();
    }

    public static void stop() {
        activeRequestId = null;
        targetPos = null;
        currentPath.clear();
        currentPathIndex = 0;
        isNavigating = false;
        lastPosition = null;
        stuckTicks = 0;
        ALIGNMENT.reset();
        // 交还视角：导航结束后，后端的 look（若有）从下一 tick 起重新生效（见 LookArbiter）。
        InputIsolation.clearNavigationLookTarget();
        InputIsolation.setAiControlState("forward", false);
        InputIsolation.setAiControlState("sprint", false);
        InputIsolation.setAiControlState("jump", false);
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
        if (isReached(playerPos, targetPos)) {
            reportProgress(1.0, "arrived");
            stop();
            return;
        }

        if (isStuck(playerPos)) {
            stuckTicks = 0;
            long nowMs = System.currentTimeMillis();
            String key = targetKey(targetPos);
            if (REPLAN.allow(key, nowMs)) {
                calculatePath();
            } else if (REPLAN.allowOnce("stuck:" + key)) {
                // 退避期内**不重复计算**（否则每秒多条 "Calculated path"）；仍按现有路继续尝试。
                // 真的走不动时，由 ActionExecutor 的卡住看门狗如实停路并上报（本地不硬撑、不自救）。
                LCUMod.LOGGER.warn("[Pathfinder] Stuck; replan backed off {}ms for target {}",
                        REPLAN.remainingMs(key, nowMs), key);
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

        Vec3 nextPoint = currentPath.get(currentPathIndex);
        if (isReached(playerPos, nextPoint)) {
            currentPathIndex++;
            if (currentPathIndex >= currentPath.size()) {
                return;
            }
            nextPoint = currentPath.get(currentPathIndex);
        }

        moveToward(mc, playerPos, nextPoint);
    }

    private static boolean calculatePath() {
        Minecraft mc = Minecraft.getInstance();
        if (mc.player == null || mc.level == null || targetPos == null) return false;

        StandingNode start = findClosestStandingNode(mc.level, mc.player.position(), 2, 4);
        StandingNode goal = findClosestStandingNode(mc.level, targetPos, 3, 6);
        currentPath.clear();
        currentPathIndex = 0;

        if (start == null || goal == null) {
            lastFailureReason = start == null && goal == null
                ? "could not resolve start and goal stand positions"
                : start == null
                    ? "could not resolve current stand position"
                    : "could not resolve target stand position";
            if (REPLAN.allowOnce("fail:" + failureKey() + "#" + lastFailureReason)) {
                LCUMod.LOGGER.warn("[Pathfinder] {}", lastFailureReason);
            }
            return false;
        }

        if (isGoal(start, goal)) {
            currentPath.add(goal.standingPos);
            if (REPLAN.allowOnce("direct:" + failureKey())) {
                LCUMod.LOGGER.info("[Pathfinder] Start and goal share the same stand node; using direct target point");
            }
            return true;
        }

        List<Vec3> solved = findPath(mc, start, goal);
        if (solved.isEmpty()) {
            // 寻路失败的正确行为 = **如实上报**，交后端重规划。
            // 绝不"自救"：本地**不做**搭桥 / 立柱上爬 / 填坑（用户实机：掉坑或挨炸后它会乱搭方块）；
            // 也无从断言"搭桥就能过"——所以只如实说"这里没有路，且本地不会造路"。
            lastFailureReason = String.format(
                    "no path from %s to %s (%s)", start.supportPos, goal.supportPos, NO_RESCUE_POLICY);
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
        String target = targetKey(targetPos);
        if (mc == null || mc.player == null) {
            return target;
        }
        Vec3 p = mc.player.position();
        return targetKey(p) + "->" + target;
    }

    private static List<Vec3> findPath(Minecraft mc, StandingNode start, StandingNode goal) {
        PriorityQueue<SearchNode> open = new PriorityQueue<>(Comparator.comparingDouble(SearchNode::fScore));
        Map<BlockPos, Double> bestG = new HashMap<>();
        Set<BlockPos> closed = new HashSet<>();

        SearchNode startNode = new SearchNode(start, null, 0.0, heuristic(start.standingPos, goal.standingPos));
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

            for (StandingNode neighbor : getNeighbors(mc, current.node)) {
                if (closed.contains(neighbor.supportPos)) {
                    continue;
                }

                double transitionCost = movementCost(current.node, neighbor);
                double nextG = current.gScore + transitionCost;
                double knownBest = bestG.getOrDefault(neighbor.supportPos, Double.POSITIVE_INFINITY);
                if (nextG >= knownBest) {
                    continue;
                }

                bestG.put(neighbor.supportPos, nextG);
                open.add(new SearchNode(neighbor, current, nextG, heuristic(neighbor.standingPos, goal.standingPos)));
            }
        }

        return Collections.emptyList();
    }

    private static List<StandingNode> getNeighbors(Minecraft mc, StandingNode current) {
        List<StandingNode> neighbors = new ArrayList<>();
        int baseX = current.supportPos.getX();
        int baseZ = current.supportPos.getZ();

        for (int[] dir : DIRECTIONS) {
            int nextX = baseX + dir[0];
            int nextZ = baseZ + dir[1];
            StandingNode candidate = findStandingNodeAtColumn(mc.level, nextX, nextZ, current.standingPos.y);
            if (candidate == null) {
                continue;
            }

            double verticalDelta = candidate.standingPos.y - current.standingPos.y;
            if (verticalDelta > MAX_JUMP_HEIGHT || verticalDelta < -MAX_DROP_HEIGHT) {
                continue;
            }

            if (Math.abs(dir[0]) + Math.abs(dir[1]) == 2) {
                if (findStandingNodeAtColumn(mc.level, baseX + dir[0], baseZ, current.standingPos.y) == null) continue;
                if (findStandingNodeAtColumn(mc.level, baseX, baseZ + dir[1], current.standingPos.y) == null) continue;
            }

            if (!canMoveBetween(mc, current.standingPos, candidate.standingPos)) {
                continue;
            }

            neighbors.add(candidate);
        }
        return neighbors;
    }

    private static boolean canMoveBetween(Minecraft mc, Vec3 from, Vec3 to) {
        if (mc.level == null) return false;
        int samples = 4;
        for (int i = 1; i <= samples; i++) {
            double t = i / (double) samples;
            double x = from.x + (to.x - from.x) * t;
            double z = from.z + (to.z - from.z) * t;
            double y = Math.max(from.y, to.y);
            if (!hasHeadroom(mc.level, mc.player, new Vec3(x, y, z))) {
                return false;
            }
        }
        return true;
    }

    private static StandingNode findClosestStandingNode(Level level, Vec3 pos, int horizontalRadius, int verticalRadius) {
        StandingNode best = null;
        double bestDist = Double.POSITIVE_INFINITY;
        int centerX = (int) Math.floor(pos.x);
        int centerY = (int) Math.floor(pos.y);
        int centerZ = (int) Math.floor(pos.z);

        for (int dx = -horizontalRadius; dx <= horizontalRadius; dx++) {
            for (int dz = -horizontalRadius; dz <= horizontalRadius; dz++) {
                for (int dy = verticalRadius; dy >= -verticalRadius; dy--) {
                    BlockPos support = new BlockPos(centerX + dx, centerY + dy, centerZ + dz);
                    StandingNode candidate = createStandingNode(level, support);
                    if (candidate == null) {
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

    private static StandingNode findStandingNodeAtColumn(Level level, int x, int z, double referenceY) {
        int minY = (int) Math.floor(referenceY) - 3;
        int maxY = (int) Math.floor(referenceY) + 2;
        StandingNode best = null;
        double bestVerticalDistance = Double.POSITIVE_INFINITY;

        for (int y = maxY; y >= minY; y--) {
            StandingNode candidate = createStandingNode(level, new BlockPos(x, y, z));
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

    private static StandingNode createStandingNode(Level level, BlockPos supportPos) {
        Minecraft mc = Minecraft.getInstance();
        if (mc.player == null) return null;

        BlockState supportState = level.getBlockState(supportPos);
        double topHeight = getTopHeight(level, supportPos, supportState);
        if (topHeight <= 0) {
            return null;
        }

        if (isDangerousSupport(supportState)) {
            return null;
        }

        Vec3 standingPos = new Vec3(supportPos.getX() + 0.5, supportPos.getY() + topHeight, supportPos.getZ() + 0.5);
        if (!hasHeadroom(level, mc.player, standingPos)) {
            return null;
        }

        return new StandingNode(supportPos, standingPos);
    }

    private static boolean hasHeadroom(Level level, net.minecraft.world.entity.player.Player player, Vec3 standingPos) {
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

    private static double getTopHeight(Level level, BlockPos pos, BlockState state) {
        if (state.isAir()) return 0;
        VoxelShape shape = state.getCollisionShape(level, pos);
        if (shape.isEmpty()) return 0;
        return shape.max(Direction.Axis.Y);
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

    private static List<Vec3> reconstructPath(SearchNode goal) {
        List<Vec3> path = new ArrayList<>();
        SearchNode cursor = goal;
        while (cursor != null) {
            path.add(cursor.node.standingPos);
            cursor = cursor.parent;
        }
        Collections.reverse(path);
        if (!path.isEmpty()) {
            path.remove(0);
        }
        return path;
    }

    private static void moveToward(Minecraft mc, Vec3 current, Vec3 target) {
        double dx = target.x - current.x;
        double dz = target.z - current.z;
        double dy = target.y - current.y;
        double horizontalDistance = Math.sqrt(dx * dx + dz * dz);
        if (horizontalDistance < 0.01) {
            ALIGNMENT.reset();
            return;
        }

        float targetYaw = (float) Math.toDegrees(Math.atan2(-dx, dz));
        float targetPitch = (float) Math.toDegrees(-Math.atan2(dy, horizontalDistance));

        // View: publish the target only. InputIsolation (the single writer) turns toward it
        // gradually this tick — navigation wins over the backend look while a path exists.
        InputIsolation.setNavigationLookTarget(targetYaw, targetPitch);

        // Alignment gate (intent preserved): only walk once we are roughly facing the waypoint.
        float yawDiff = LookArbiter.yawDiff(mc.player.getYRot(), targetYaw);
        boolean alignedEnoughToWalk = Math.abs(yawDiff) < ALIGN_TO_WALK_DEG;
        boolean alignedEnoughToSprint = Math.abs(yawDiff) < ALIGN_TO_SPRINT_DEG;

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

        InputIsolation.setAiControlState("forward", alignedEnoughToWalk);
        InputIsolation.setAiControlState("sprint",
                alignedEnoughToSprint && horizontalDistance > 4.0 && Math.abs(dy) < 0.2);

        boolean shouldJump = dy > STEP_UP_HEIGHT || (dy > 0.18 && horizontalDistance < 1.2);
        if (!shouldJump && mc.player != null && mc.player.onGround()) {
            Vec3 probe = current.add(dx / horizontalDistance * 0.35, 0, dz / horizontalDistance * 0.35);
            shouldJump = !hasHeadroom(mc.level, mc.player, new Vec3(probe.x, Math.max(current.y, target.y), probe.z));
        }
        InputIsolation.setAiControlState("jump", alignedEnoughToWalk && shouldJump);
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
        return String.format("Navigating to (%.0f, %.0f, %.0f) path=%d idx=%d",
            targetPos.x, targetPos.y, targetPos.z, currentPath.size(), currentPathIndex);
    }

    public static String getLastFailureReason() {
        return lastFailureReason;
    }

    private static void reportProgress(double progress, String message) {
        if (activeRequestId != null && LCUMod.WIRE != null) {
            LCUMod.WIRE.sendProgress(activeRequestId, progress, message);
        }
    }

    private record StandingNode(BlockPos supportPos, Vec3 standingPos) {}

    private static class SearchNode {
        private final StandingNode node;
        private final SearchNode parent;
        private final double gScore;
        private final double hScore;

        private SearchNode(StandingNode node, SearchNode parent, double gScore, double hScore) {
            this.node = node;
            this.parent = parent;
            this.gScore = gScore;
            this.hScore = hScore;
        }

        private double fScore() {
            return gScore + hScore;
        }
    }
}
