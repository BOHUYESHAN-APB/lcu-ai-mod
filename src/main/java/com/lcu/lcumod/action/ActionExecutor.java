package com.lcu.lcumod.action;

import com.google.gson.JsonArray;
import com.google.gson.JsonObject;
import com.lcu.lcumod.LCUMod;
import com.lcu.lcumod.client.WindowTitle;
import com.lcu.lcumod.network.WireServer;
import com.lcu.lcumod.state.StateCollector;
import net.minecraft.client.Minecraft;
import net.minecraft.core.BlockPos;
import net.minecraft.core.registries.BuiltInRegistries;
import net.minecraft.network.chat.Component;
import net.minecraft.network.protocol.game.ServerboundClientCommandPacket;
import net.minecraft.network.protocol.game.ServerboundMovePlayerPacket;
import net.minecraft.util.Prediction;
import net.minecraft.world.InteractionHand;
import net.minecraft.world.entity.Entity;
import net.minecraft.world.entity.item.ItemEntity;
import net.minecraft.world.inventory.AbstractContainerMenu;
import net.minecraft.world.inventory.ContainerInput;
import net.minecraft.world.item.Item;
import net.minecraft.world.item.ItemStack;
import net.minecraft.world.item.BlockItem;
import net.minecraft.world.item.component.SwingAnimation;
import net.minecraft.world.item.crafting.Ingredient;
import net.minecraft.world.item.crafting.RecipeHolder;
import net.minecraft.world.item.crafting.RecipeType;
import net.minecraft.world.item.crafting.display.RecipeDisplayId;
import net.minecraft.world.item.crafting.display.ShapedCraftingRecipeDisplay;
import net.minecraft.world.item.crafting.display.SlotDisplayContext;
import net.minecraft.world.phys.AABB;
import net.minecraft.world.phys.BlockHitResult;
import net.minecraft.world.phys.Vec3;
import net.minecraft.core.Direction;
import net.minecraft.core.component.DataComponents;
import net.minecraft.world.level.block.BedBlock;
import net.minecraft.world.level.block.Block;
import net.minecraft.world.level.block.Blocks;

import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;

/**
 * Drains command queue from wire and executes commands via CLIENT-SIDE APIs.
 * Works in both single-player and multiplayer.
 */
public class ActionExecutor {

    // Break tracking
    private final Map<String, BreakTask> activeBreaks = new HashMap<>();
    // Continuous digging (survival mode)
    private static BlockPos diggingPos = null;
    private static Direction diggingDir = null;
    private static int diggingTicks = 0;
    private static final int DIGGING_TIMEOUT_TICKS = 600;
    // Jump cooldown
    private static int jumpCooldown = 0;
    private static final int JUMP_COOLDOWN_TICKS = 25;
    // AI/User control
    private static boolean aiControlled = true;
    private static boolean wasDead = false;
    private static int respawnRetryTicks = 0;
    private static int respawnAttempts = 0;
    private static String followTargetName = null;
    private static int followRefreshTicks = 0;

    /** 收到这些命令视为"新任务"，先取消 Java 侧持久跟随（否则会"嘴上说不跟、身体还在跟"）。 */
    private static final java.util.Set<String> NEW_TASK_COMMANDS = java.util.Set.of(
            "move_to", "follow_player", "explore", "go_to_block", "collect_blocks",
            "craft_item", "build", "trade", "mine_block", "dig_block", "place_block", "sleep", "guard",
            // 站点驻守与路径重放同样是"占住身体"的任务，必须参与新任务让路
            "go_afk", "route_follow");

    // go_to_block：两段式（先寻路过去，到达后再记忆/收尾）
    private static BlockPos pendingGoTarget = null;
    private static String pendingGoBlock = "";
    private static boolean pendingGoRemember = false;
    private static int pendingGoTicks = 0;

    // sleep：两段式（床在 4 米外→先走过去，到达后自动点床）
    private static boolean pendingSleep = false;
    private static BlockPos pendingSleepBed = null;
    private static int pendingSleepTicks = 0;

    /** 饥饿反射：阈值取自参考实现（mineflayer-auto-eat 的 minHunger=15 / mindcraft 的 14）。 */
    private static final int HUNGER_REFLEX_FOOD = 14;
    private static final String REFLEX_EAT_REQ_ID = "reflex_eat";
    private static int eatReflexCooldown = 0;

    // 卡住看门狗（该动却不动 → 停寻路 + 诚实上报，交后端重规划）
    private static double stuckLastX = Double.NaN;
    private static double stuckLastZ = Double.NaN;
    private static int stuckTicks = 0;
    private static int stuckCooldown = 0;

    // guard：在某个点附近驻守（用户"在这儿守着" → 按发言瞬间的坐标 + 水平半径）
    private static BlockPos guardPoint = null;
    private static int guardRadius = 32;
    private static int guardCheckCooldown = 0;

    // ── 站点驻守（go_afk / afk_stop）：到站点后在半径内"钉住"，被撞出去自己走回 ──
    private static final AfkController AFK = new AfkController();
    /** 走回站点的重试节流与尝试计数（连不上就诚实失败，不假装还在驻守）。 */
    private static int afkReturnCooldown = 0;
    private static int afkReturnAttempts = 0;
    private static final int AFK_MAX_RETURN_ATTEMPTS = 3;

    // ── 路径记忆与重放（route_record_start/stop、route_follow）──
    /** 现场压缩器；非 null 即"正在记录实际走过的路"。 */
    private static RouteMemory.Recorder routeRecorder = null;
    private static String routeRecordName = "";
    private static final RouteFollower ROUTE = new RouteFollower();
    /** 段内"该动却没动"的看门狗（与 tickStuckWatchdog 同思路，但对 route 独立计时，互不打架）。 */
    private static double routeStuckLastX = Double.NaN;
    private static double routeStuckLastZ = Double.NaN;
    private static int routeStuckTicks = 0;
    /** 段与段之间的到点判定容差（见 RouteMemory 抽稀步长 8 → 每段都很短）。 */
    private static final double ROUTE_REACH_XZ = 1.5;
    private static final double ROUTE_REACH_Y = 2.0;
    private static String pendingCraftItem = null;
    private static String pendingCraftReqId = null;
    private static int pendingCraftTicks = 0;
    private static int pendingCraftAttempts = 0;
    /** 进食状态机（幂等 / 饱食度-饱和度-数量三判据 / 40 tick 超时）；见 {@link EatController}。 */
    private static final EatController EAT = new EatController();
    private static int pendingEatSlot = -1;
    /** 用于进度上报的主请求 id（反射合成 id 时不刷后端进度）。 */
    private static String pendingEatPrimary = null;

    // mine_block / dig：按住 attack，直到目标方块消失或超时（20s）
    private static HoldController.Handle miningHold = null;
    private static String miningReqId = null;
    private static BlockPos miningPos = null;
    private static int miningProgressTicks = 0;
    private static final int MINE_TIMEOUT_TICKS = 20 * 20;

    // place_block：看向目标面 → 单次 use（2 tick）→ 校验目标位置是否变成要放的方块
    private static HoldController.Handle placeHold = null;
    private static String placeReqId = null;
    private static BlockPos placeExpectPos = null;
    private static Block placeExpectBlock = null;
    private static float placeYaw = 0f;
    private static float placePitch = 0f;
    private static int placeTicks = 0;
    private static final int PLACE_VERIFY_TICKS = 8;

    private static String pendingCollectItem = null;
    private static String pendingCollectReqId = null;
    private static int pendingCollectGoalCount = 0;
    private static int pendingCollectBaselineCount = 0;
    private static BlockPos pendingCollectTargetPos = null;
    private static int pendingCollectTicks = 0;
    private static int pendingCollectSearchMisses = 0;
    private static int pendingCraftGoalCount = 1;
    // Storage retrieval from remembered chests/barrels
    private static BlockPos pendingStoragePos = null;
    private static String pendingStorageTargetItem = null;
    private static int pendingStorageGoalCount = 0;
    private static int pendingStorageTicks = 0;
    private static final java.util.Set<BlockPos> triedStoragePositions = new java.util.HashSet<>();
    private static String activeTaskKind = "idle";
    private static String activeTaskStatus = "idle";
    private static String activeTaskTarget = "";
    private static String activeTaskDetail = "";
    private static double activeTaskProgress = 0.0;
    // Task statuses that end a task — transitions to these force a full state push
    private static final Set<String> TERMINAL_TASK_STATUSES = Set.of("done", "failed", "cancelled", "idle", "error");
    private int tickCount = 0;

    public static void notifyInterrupted(String reason) {
        if (LCUMod.WIRE != null) {
            JsonObject data = new JsonObject();
            data.addProperty("type", "interrupted");
            data.addProperty("reason", reason);
            LCUMod.WIRE.sendEvent("command_interrupted", data);
        }
    }

    /** Called every client tick via ActionExecutorBridge (ClientTickEvent.Post). */
    public void onTick() {
        var mc = Minecraft.getInstance();
        if (mc == null || mc.level == null || mc.player == null) return;

        // ── Input isolation (core control system) ──
        InputIsolation.tick(mc);

        // ── Window title：谁在操控 / 在干什么 / 哪个实例 / 在哪 / 什么版本 ──
        WindowTitle.tick(mc);

        // ── Pathfinder (A* navigation) ──
        Pathfinder.tick(mc);

        // ── Remembered workstations/storage POIs ──
        PoiMemory.tick(mc, tickCount);

        // ── Follow controller (persistent follow target) ──
        tickFollowTarget(mc);

        // ── Craft controller (stateful crafting) ──
        tickPendingCraft(mc);

        // ── craft_via_recipe 的结果校验（诚实 outcome：背包真多出物品才算成功，超时恒实失败）──
        ContainerController.tickCraftVerification(mc);

        // ── Storage retrieval controller (check chests/barrels before world collect) ──
        tickPendingStorageRetrieve(mc);

        // ── Collect controller (generic resource acquisition) ──
        tickPendingCollect(mc);

        // ── Eat controller (stateful eating / healing) ──
        tickPendingEat(mc);

        // ── 找方块/睡觉的两段式收尾（走过去 → 记忆/点床/诚实失败）──
        tickPendingBlockTasks(mc);

        // ── P0 饥饿反射：饿到一定程度自己吃 ──
        tickHungerReflex(mc);

        // ── 守卫：出了半径自己走回去（用户"在这儿守着"）──
        tickGuard(mc);

        // ── 站点驻守：到了站点就钉在半径内；被撞出去自己走回；keepalive 只动头不动脚 ──
        tickAfk(mc);

        // ── 路径重放：逐点走（每段都在原版寻路射程内），诚实上报"卡在第几个点" ──
        tickRoute(mc);

        // ── 路径记录：每 tick 采样真实位姿，由 Recorder 按抽稀规则决定是否记点 ──
        tickRouteRecording(mc);

        // ── 卡住看门狗：该动却不动 → 停寻路 + 诚实上报（后端据此重规划）──
        tickStuckWatchdog(mc);

        // ── Movement system (packet-based) ──
        MovementSystem.tick(mc);

        // ── Java-side autonomous behavior (works without backend) ──
        // Single-writer gate: while a backend client is connected, the Python-side
        // modes_engine owns behavior and Java autonomy must stay idle (fallback only).
        boolean backendConnected = LCUMod.WIRE != null && LCUMod.WIRE.hasClients();
        if (!backendConnected && InputIsolation.isAiControlled()) {
            boolean behaviorActive = JavaAutonomousBehavior.tick(mc);
            // If behavior is active and no backend command is pending, skip other actions
            if (behaviorActive && WireServer.commandQueue.isEmpty()) {
                return;
            }
        }

        boolean runtimeBusy = Pathfinder.isNavigating()
                || MovementSystem.isMoving()
                || JavaAutonomousBehavior.getState() != JavaAutonomousBehavior.BehaviorState.IDLE
                || AFK.isActive()
                || ROUTE.isActive()
                || !WireServer.commandQueue.isEmpty();

        // ── Human-like idle behavior (head tracking) ──
        // 驻守时**不**随机张望：站点的朝向有语义（对着刷怪点），必须由 tickAfk 独占朝向写入。
        if (InputIsolation.isAiControlled()
                && JavaAutonomousBehavior.getState() == JavaAutonomousBehavior.BehaviorState.IDLE
                && !MovementSystem.isMoving()
                && !Pathfinder.isNavigating()
                && !AFK.isActive()) {
            HumanLikeBehavior.tick(mc);
        }

        // ── Anti-AFK subtle activity pulses ──
        // 驻守/重放期间交给本层自己的节奏（驻守的 keepalive 只动头不动脚，
        // 这里的脉冲会真的横移/潜行，属于"移动"，会破坏用户要的站位）。
        ActivitySignalController.tick(mc, runtimeBusy);

        // ── High-frequency control stream ──
        // Placed after every other input writer so that, while a stream is active,
        // it is the last writer and therefore owns movement and look.
        ControlStream.tick(mc);

        // ── P0 combat reflex: 被敌对生物打了立刻还手 ──
        // 刻意放在 ControlStream 之后：紧急情形下它必须是**朝向的最后写入者**，
        // 否则控制流的视角会覆盖掉"转过去打它"，表现为"不会防御"。
        CombatReflex.tick(mc);

        // ── P0 swim reflex: 头在水下按住跳跃上浮 ──
        // 用户实机反馈"它不会游泳，在水里面它会死"。溺水是秒级致死，同样不能让 LLM 参与。
        // 只写 jump 键，不碰 WASD/朝向，因此不会跟寻路/控制流抢横向控制。
        SwimReflex.tick(mc);

        // ── Auto-respawn ──
        if (mc.player.isDeadOrDying()) {
            if (!wasDead) {
                wasDead = true;
                respawnAttempts = 0;
                respawnRetryTicks = 0;
                releaseAllInputs();
                Pathfinder.stop();
                MovementSystem.stop();
            }

            if (respawnRetryTicks-- <= 0) {
                respawnRetryTicks = 12;
                respawnAttempts++;
                LCUMod.LOGGER.info("[AutoRespawn] Attempt {}", respawnAttempts);
                var conn = mc.getConnection();
                if (conn != null) {
                    conn.send(new ServerboundClientCommandPacket(ServerboundClientCommandPacket.Action.PERFORM_RESPAWN));
                }
            }
        } else if (wasDead) {
            wasDead = false;
            respawnAttempts = 0;
            respawnRetryTicks = 0;
            ActivitySignalController.reset();
        }

        // ── Jump cooldown ──
        if (jumpCooldown > 0) jumpCooldown--;

        // ── Continuous digging ──
        handleContinuousDigging(mc);

        // ── Held-action controllers (mine / place) ──
        tickMining(mc);
        tickPlace(mc);

        // ── Drain command queue ──
        int processed = 0;
        while (processed < 5) {
            WireServer.WireCommand cmd = WireServer.commandQueue.poll();
            if (cmd == null) break;
            LCUMod.LOGGER.info("[Action] Processing: {} id={}", cmd.cmd(), cmd.id());
            executeCommand(cmd);
            processed++;
        }

        // ── Safety: release stuck keys every 100 ticks ──
        if (tickCount++ % 100 == 0 && !MovementSystem.isMoving() && !Pathfinder.isNavigating()) {
            releaseAllInputs();
        }

        if (tickCount % 10 == 0) {
            sendBehaviorSnapshot();
            sendControlState();
        }
    }

    // ── AI/User Control Toggle ──

    public static boolean isAiControlled() { return InputIsolation.isAiControlled(); }
    public static void setAiControlled(boolean v) { 
        if (v != InputIsolation.isAiControlled()) {
            toggleAiControl();
        }
    }
    public static void toggleAiControl() { 
        InputIsolation.toggleControl();
        boolean aiNow = InputIsolation.isAiControlled();
        if (!aiNow) {
            InputIsolation.clearAiControls();
            MovementSystem.stop();
            Pathfinder.stop();
            followTargetName = null;
            // 玩家接管：驻守与重放立刻让路（"嘴里说停、身体还在走"是明确禁止的）
            afkStopQuiet("ai_off");
            routeStopQuiet("ai_off");
        } else {
            InputIsolation.clearUserControls();
        }
        sendControlStateToBackend();
    }
    
    private static void sendControlStateToBackend() {
        if (LCUMod.WIRE != null) {
            JsonObject data = new JsonObject();
            data.addProperty("ai_controlled", isAiControlled());
            LCUMod.WIRE.sendEvent("control_state", data);
        }
    }

    // ── Input State (read by Mixin) ──

    private static boolean moveForward = false;
    private static boolean moveBack = false;
    private static boolean moveLeft = false;
    private static boolean moveRight = false;

    public static boolean isMovingForward() { return moveForward; }
    public static boolean isMovingBack() { return moveBack; }
    public static boolean isMovingLeft() { return moveLeft; }
    public static boolean isMovingRight() { return moveRight; }

    public static void setInput(String key, boolean state) {
        // Use InputIsolation for proper control isolation
        InputIsolation.setAiControlState(key, state);
        
        // Also update local state for backward compatibility
        switch (key) {
            case "forward" -> moveForward = state;
            case "back" -> moveBack = state;
            case "left" -> moveLeft = state;
            case "right" -> moveRight = state;
        }
    }

    public static void releaseAllInputs() {
        // Clear both AI and local states
        InputIsolation.clearAiControls();
        moveForward = false;
        moveBack = false;
        moveLeft = false;
        moveRight = false;
    }

    // ── Command Execution ──

    private void executeCommand(WireServer.WireCommand cmd) {
        var mc = Minecraft.getInstance();
        if (mc == null || mc.player == null) {
            sendResponse(cmd.id(), false, "No player");
            return;
        }

        try {
            // 任何**新任务**命令都要先取消 Java 侧的持久跟随，否则会出现
            // "嘴上说不跟了、身体还在跟着"：跟随控制器每 10 tick 就重新寻路到目标玩家，
            // 会把新任务（如 explore/go_to_block）的控制权抢走。
            if (NEW_TASK_COMMANDS.contains(cmd.cmd())) {
                followTargetName = null;
                // 也立刻释放所有“按住”：新任务到来时旧的吃/挖/放必须让路，不能粘键。
                releaseActiveHolds(HoldController.Release.NEW_TASK);
                abortEat("new_task");
                abortPendingPlace();
                // 站点驻守 / 路径重放同样"占住身体"：**除非自己就是那个新任务**，一律让路，
                // 否则会出现"嘴上接了新活、身体还钉在刷怪塔上"。
                if (!"go_afk".equals(cmd.cmd())) {
                    afkStopQuiet("new_task");
                }
                if (!"route_follow".equals(cmd.cmd())) {
                    routeStopQuiet("new_task");
                }
            }
            switch (cmd.cmd()) {
                case "move_to" -> handleMoveTo(cmd);
                case "look_at" -> handleLookAt(cmd);
                case "jump" -> {
                    if (jumpCooldown <= 0) {
                        mc.player.jumpFromGround();
                        jumpCooldown = JUMP_COOLDOWN_TICKS;
                        sendResponse(cmd.id(), true, "Jumped");
                    } else {
                        sendResponse(cmd.id(), false, "Jump on cooldown (" + jumpCooldown + " ticks)");
                    }
                }
                case "sneak" -> { mc.player.setShiftKeyDown(cmd.args() != null && cmd.args().has("sneak") ? cmd.args().get("sneak").getAsBoolean() : true); sendResponse(cmd.id(), true, "Sneak"); }
                case "sprint" -> { mc.player.setSprinting(cmd.args() == null || !cmd.args().has("sprint") || cmd.args().get("sprint").getAsBoolean()); sendResponse(cmd.id(), true, "Sprint"); }
                case "send_chat" -> handleSendChat(cmd);
                case "use_item" -> handleUseItem(cmd);
                case "select_hotbar" -> handleSelectHotbar(cmd);
                case "drop" -> handleDrop(cmd);
                case "equip" -> handleEquip(cmd);
                case "attack" -> handleAttack(cmd);
                case "stop_digging" -> handleStopDigging(cmd);
                case "get_state" -> handleGetState(cmd);
                case "shutdown" -> handleShutdown();
                case "set_control_state" -> handleSetControlState(cmd);
                case "auto_equip" -> handleAutoEquip(cmd);
                case "get_inventory" -> handleGetInventory(cmd);
                case "stop_all" -> handleStopAll(cmd);
                // AI/User control toggle
                case "toggle_ai" -> {
                    toggleAiControl();
                    sendResponse(cmd.id(), true, "AI=" + isAiControlled());
                }
                // Container interaction (mineflayer-style)
                case "use_on" -> handleUseOn(cmd);       // 通用右键：看向 + 按住右键（语义交给后端/原版）
                case "get_container" -> handleGetContainer(cmd);
                case "take_item" -> handleTakeItem(cmd);
                case "put_item" -> handlePutItem(cmd);
                case "close_container", "container_close" -> handleCloseContainer(cmd);
                // UI 驱动层：一律发窗口点击包，绝不打开客户端界面（见 ContainerController）
                case "container_move" -> handleContainerMove(cmd);
                case "container_click" -> handleContainerClick(cmd);
                case "equip_armor" -> handleEquipArmor(cmd);
                case "craft_via_recipe" -> handleCraftViaRecipe(cmd);
                // 物品身份对齐（回答"传 id 还是名字"）：id / 中英文名 / #tag → 稳定 id
                case "resolve_item" -> handleResolveItem(cmd);
                // 配方搜索（数据源 = 服务端配方显示）：output?/input?/station?/limit?
                case "search_recipes" -> handleSearchRecipes(cmd);
                case "look_at_entity" -> handleLookAtEntity(cmd);
                case "use_on_entity" -> handleUseOnEntity(cmd);
                case "behavior_enable", "toggle_behavior" -> { 
                    if (LCUMod.BEHAVIORS != null) { 
                        boolean newState = !LCUMod.BEHAVIORS.isEnabled();
                        LCUMod.BEHAVIORS.setEnabled(newState); 
                        sendResponse(cmd.id(), true, "behaviors=" + newState);
                        // Send state update to backend
                        sendBehaviorState(newState);
                    }
                }
                case "behavior_disable" -> { 
                    if (LCUMod.BEHAVIORS != null) { 
                        LCUMod.BEHAVIORS.setEnabled(false); 
                        sendResponse(cmd.id(), true, "behaviors=false");
                        sendBehaviorState(false);
                    }
                }
                // Aliases
                case "attack_entity" -> handleAttack(cmd);
                case "interact_block", "interact" -> handleInteract(cmd);
                case "mine_block", "dig_block" -> handleMine(cmd);
                case "place_block" -> handlePlace(cmd);
                // Advanced actions
                case "follow_player" -> handleFollowPlayer(cmd);
                case "craft_item" -> handleCraftItem(cmd);
                case "collect_blocks" -> handleCollectBlocks(cmd);
                case "explore" -> handleExplore(cmd);
                case "trade" -> handleTrade(cmd);
                case "sleep" -> handleSleep(cmd);
                case "find_block" -> handleFindBlock(cmd);
                case "go_to_block" -> handleGoToBlock(cmd);
                case "remember_poi" -> handleRememberPoi(cmd);
                // 站点记忆：精准 xyz + 朝向 + 水平容差（"让 AI 去刷怪塔挂机"）
                case "remember_station" -> handleRememberStation(cmd);
                // 挂机：寻路到站点 → 驻守半径内；被撞出去自己走回；keepalive 只动头不动脚
                case "go_afk" -> handleGoAfk(cmd);
                case "afk_stop" -> handleAfkStop(cmd);
                // 路径记忆与重放（洞穴超出原版寻路距离时的"线"）
                case "route_record_start" -> handleRouteRecordStart(cmd);
                case "route_record_stop" -> handleRouteRecordStop(cmd);
                case "route_follow" -> handleRouteFollow(cmd);
                // 后端回灌：把持久化在**后端**的路径整条推回模组（灌进来即可直接 route_follow）
                case "remember_route" -> handleRememberRoute(cmd);
                // 路径几何导出：把整条点列原样给后端（跨重启回灌用；别塞进每帧 state）
                case "route_export" -> handleRouteExport(cmd);
                case "equip_weapon" -> {
                    String weapon = equipBestWeapon(mc);
                    sendResponse(cmd.id(), weapon != null,
                            weapon == null ? "No weapon in hotbar" : "Equipped " + weapon);
                }
                // 守卫：在某个点附近驻守（用户"在这儿守着" → 按发言瞬间的坐标 + 水平半径）
                case "guard" -> handleGuard(cmd);
                // 区域声明与硬保护（"这里是不是农场" → 声明语义 + 硬拦）
                case "define_zone" -> handleDefineZone(cmd);
                case "clear_zone" -> handleClearZone(cmd);
                // 斜杠命令直通（原版 /locate structure|biome 这类"查世界"的能力）
                case "run_command" -> handleRunCommand(cmd);
                // 控件级点击：在**服务端弹出的**当前界面里按控件（不碰鼠标、不开界面）
                case "click_screen_button" -> handleClickScreenButton(cmd);
                // HUD 描述词：状态词后面那句话，由后端大模型输出
                case "set_hud_note" -> {
                    JsonObject noteArgs = cmd.args();
                    String noteText = noteArgs != null && noteArgs.has("text")
                            ? noteArgs.get("text").getAsString() : "";
                    double noteSeconds = noteArgs != null && noteArgs.has("seconds")
                            ? noteArgs.get("seconds").getAsDouble() : 30.0;
                    // HUD 描述词同样是 AI 生成的出站文本 → 同一套清洗（防 `/` 注入）
                    com.lcu.lcumod.chat.TextSanitizer.Result noteSanitized =
                            com.lcu.lcumod.chat.TextSanitizer.sanitize(noteText);
                    if (noteSanitized.sanitized()) {
                        LCUMod.LOGGER.warn("[Safety] set_hud_note 已清洗：{}", noteSanitized.text());
                    }
                    com.lcu.lcumod.client.HudNote.set(noteSanitized.text(), noteSeconds);
                    JsonObject noteData = new JsonObject();
                    noteData.addProperty("message", "hud note=" + com.lcu.lcumod.client.HudNote.current());
                    noteData.addProperty("text", noteSanitized.text());
                    noteData.addProperty("sanitized", noteSanitized.sanitized());
                    sendResponse(cmd.id(), true, noteData);
                }
                // 后端覆盖本能反射（用户要求：不能让本能盖过高级指令）
                case "reflex_override" -> {
                    JsonObject a = cmd.args();
                    String name = a != null && a.has("name") ? a.get("name").getAsString() : ReflexGate.ALL;
                    double seconds = a != null && a.has("seconds") ? a.get("seconds").getAsDouble() : 0.0;
                    boolean enable = a == null || !a.has("enabled") || a.get("enabled").getAsBoolean();
                    ReflexGate.suppress(name, enable ? seconds : 0.0);
                    sendResponse(cmd.id(), true, (enable ? "suppress " : "restore ") + name
                            + (enable ? " for " + seconds + "s" : ""));
                }
                case "eat" -> handleEat(cmd);
                case "drop_item" -> handleDropItem(cmd);
                case "sort_inventory" -> handleSortInventory(cmd);
                case "build" -> handleBuild(cmd);
                default -> sendResponse(cmd.id(), false, "Unknown: " + cmd.cmd());
            }
        } catch (Exception e) {
            LCUMod.LOGGER.error("[Action] Error {}: {}", cmd.cmd(), e.getMessage());
            sendResponse(cmd.id(), false, e.getMessage());
        }
    }

    // ── Handlers ──

    private void handleMoveTo(WireServer.WireCommand cmd) {
        var args = cmd.args();
        if (args == null || !args.has("x") || !args.has("z")) {
            sendResponse(cmd.id(), false, "Missing x,z");
            return;
        }
        double x = args.get("x").getAsDouble();
        double y = args.has("y") ? args.get("y").getAsDouble() : Minecraft.getInstance().player.getY();
        double z = args.get("z").getAsDouble();
        
        // Use Pathfinder for A* navigation
        boolean accepted = Pathfinder.navigateTo(cmd.id(), x, y, z);
        if (!accepted) {
            sendResponse(cmd.id(), false, Pathfinder.getLastFailureReason());
            return;
        }
        if (LCUMod.WIRE != null) {
            LCUMod.WIRE.sendProgress(cmd.id(), 0.05, "path accepted");
        }
        sendResponse(cmd.id(), true, "Navigating to " + (int)x + "," + (int)y + "," + (int)z);
    }

    private void handleLookAt(WireServer.WireCommand cmd) {
        var args = cmd.args();
        if (args == null || !args.has("x") || !args.has("z")) {
            sendResponse(cmd.id(), false, "Missing x,z");
            return;
        }
        var mc = Minecraft.getInstance();
        var p = mc.player;
        double x = args.get("x").getAsDouble();
        double y = args.has("y") ? args.get("y").getAsDouble() : p.getEyeY();
        double z = args.get("z").getAsDouble();
        aimAt(mc, x, y, z);
        sendResponse(cmd.id(), true, "Looked");
    }

    private static long lastChatTime = 0;
    private static final long CHAT_COOLDOWN_MS = 1500;  // 1.5s between messages

    private void handleSendChat(WireServer.WireCommand cmd) {
        if (cmd.args() == null || !cmd.args().has("message")) {
            sendResponse(cmd.id(), false, "No message");
            return;
        }
        // Rate limit: prevent server anti-spam kick
        long now = System.currentTimeMillis();
        if (now - lastChatTime < CHAT_COOLDOWN_MS) {
            sendResponse(cmd.id(), false, "Chat too fast, wait " + (CHAT_COOLDOWN_MS - (now - lastChatTime)) + "ms");
            return;
        }
        lastChatTime = now;
        try {
            var mc = Minecraft.getInstance();
            if (mc.player == null || mc.player.connection == null) {
                sendResponse(cmd.id(), false, "Not connected");
                return;
            }
            String msg = cmd.args().get("message").getAsString();
            if (msg.isEmpty()) {
                sendResponse(cmd.id(), false, "Empty message");
                return;
            }
            // 出站清洗（防注入）：**绝不**让 AI 说的话以 `/` 开头（那在聊天通道里就等于执行指令）。
            com.lcu.lcumod.chat.TextSanitizer.Result sanitized =
                    com.lcu.lcumod.chat.TextSanitizer.sanitize(msg);
            msg = sanitized.text();
            if (msg.isBlank()) {
                sendResponse(cmd.id(), false, "Empty message after sanitize");
                return;
            }
            if (sanitized.sanitized()) {
                LCUMod.LOGGER.warn("[Safety] send_chat 已清洗（剥掉行首空白/斜杠 {} 字符，行首斜杠={}）：{}",
                        sanitized.removedChars(), sanitized.hadLeadingSlash(), msg);
            }
            mc.player.connection.sendChat(msg);
            // 记下我们自己发的这条，供 ChatListener 去重（不要按玩家名整类过滤用户的话）
            com.lcu.lcumod.chat.OutboundChatLog.record(msg);
            JsonObject data = new JsonObject();
            data.addProperty("message", "Chat sent");
            data.addProperty("text", msg);
            data.addProperty("sanitized", sanitized.sanitized());
            sendResponse(cmd.id(), true, data);
        } catch (Exception e) {
            LCUMod.LOGGER.error("[Action] sendChat error: {}", e.getMessage());
            sendResponse(cmd.id(), false, "Chat error: " + e.getMessage());
        }
    }

    private void handleSelectHotbar(WireServer.WireCommand cmd) {
        if (cmd.args() == null || !cmd.args().has("index")) {
            sendResponse(cmd.id(), false, "No index");
            return;
        }
        int idx = cmd.args().get("index").getAsInt();
        if (idx >= 0 && idx < 9) {
            Minecraft.getInstance().player.getInventory().setSelectedSlot(idx);
            sendResponse(cmd.id(), true, "Slot " + idx);
        } else {
            sendResponse(cmd.id(), false, "Invalid slot " + idx);
        }
    }

    private void handleAttack(WireServer.WireCommand cmd) {
        var mc = Minecraft.getInstance();
        if (mc.player == null || mc.gameMode == null) {
            sendResponse(cmd.id(), false, "No player/gameMode");
            return;
        }

        // Find nearest living entity in crosshair direction
        Entity target = findTargetEntity(mc, 6.0);
        if (target != null) {
            // 区域硬保护：被保护区域里的动物不许打（喂食/剪毛等交互不在此列）
            ZoneGuard.Block zoneBlock = ZoneGuard.block(target.getX(), target.getY(), target.getZ(),
                    ZoneGuard.Action.ATTACK, target instanceof net.minecraft.world.entity.animal.Animal);
            if (zoneBlock != null) {
                sendZoneBlocked(cmd, zoneBlock, "attack " + target.getName().getString());
                return;
            }
            mc.gameMode.attack(mc.player, target);
            mc.player.swing(InteractionHand.MAIN_HAND, SwingAnimation.DEFAULT, true);
            sendResponse(cmd.id(), true, "Attacked " + target.getName().getString());
        } else {
            // Swing anyway
            mc.player.swing(InteractionHand.MAIN_HAND, SwingAnimation.DEFAULT, true);
            sendResponse(cmd.id(), false, "No target found");
        }
    }

    /** 区域硬拦的统一回执：{@code {blocked_by_zone, rule, action}}（后端据此告诉 LLM"这里是农场"）。 */
    private void sendZoneBlocked(WireServer.WireCommand cmd, ZoneGuard.Block blocked, String action) {
        JsonObject data = new JsonObject();
        data.addProperty("blocked_by_zone", blocked.zone());
        data.addProperty("rule", blocked.rule());
        data.addProperty("action", action);
        LCUMod.LOGGER.warn("[Zone] 拦截 {}：zone={} rule={}", action, blocked.zone(), blocked.rule());
        sendResponse(cmd.id(), false, data, "blocked_by_zone:" + blocked.zone() + " rule:" + blocked.rule());
    }

    private Entity findTargetEntity(Minecraft mc, double range) {
        var level = mc.level;
        var player = mc.player;
        if (level == null || player == null) return null;

        // Entity ray trace
        var lookVec = player.getLookAngle();
        var start = player.getEyePosition();
        var end = start.add(lookVec.scale(range));

        // Check entities in bounding box
        AABB searchBox = player.getBoundingBox().inflate(range);
        Entity nearest = null;
        double nearestDist = Double.MAX_VALUE;

        for (Entity e : level.getEntities(player, searchBox)) {
            if (!e.isAlive()) continue;
            // Check if entity is in line of sight
            var ePos = e.position();
            double dist = player.distanceTo(e);
            if (dist > range) continue;
            
            // Check angle
            var toEntity = ePos.subtract(start).normalize();
            double dot = lookVec.dot(toEntity);
            if (dot > 0.85 && dist < nearestDist) {  // ~31 degree cone
                nearest = e;
                nearestDist = dist;
            }
        }
        return nearest;
    }

    private void handleGetState(WireServer.WireCommand cmd) {
        var mc = Minecraft.getInstance();
        if (mc.player == null) { sendResponse(cmd.id(), false, "No player"); return; }

        JsonObject state = new JsonObject();
        JsonObject player = new JsonObject();
        player.addProperty("name", mc.player.getName().getString());
        player.addProperty("health", mc.player.getHealth());
        // Mod-compatible max health
        player.addProperty("max_health", mc.player.getMaxHealth());
        player.addProperty("absorption", mc.player.getAbsorptionAmount());
        player.addProperty("hunger", mc.player.getFoodData().getFoodLevel());
        player.addProperty("saturation", mc.player.getFoodData().getSaturationLevel());
        player.addProperty("x", mc.player.getX());
        player.addProperty("y", mc.player.getY());
        player.addProperty("z", mc.player.getZ());
        player.addProperty("yaw", mc.player.getYRot());
        player.addProperty("pitch", mc.player.getXRot());
        player.addProperty("gamemode", mc.player.isCreative() ? "creative" : mc.player.isSpectator() ? "spectator" : "survival");
        state.add("player", player);

        // Online players from tab list
        JsonArray players = new JsonArray();
        var conn = mc.getConnection();
        if (conn != null) {
            for (var entry : conn.getOnlinePlayers()) {
                JsonObject p = new JsonObject();
                p.addProperty("name", entry.getProfile().name());
                p.addProperty("uuid", entry.getProfile().id().toString());
                players.add(p);
            }
        }
        state.add("online_players", players);

        // Movement status
        state.addProperty("moving", MovementSystem.isMoving());
        // 与 state_update 同口径的驻守/路径状态（后端 on-demand 查询也要看得到）
        state.add("afk", describeAfk());
        state.add("route", describeRoute());
        state.add("stations", describeStations());
        state.add("routes", describeRoutes());
        state.add("zones", describeZones());
        var moveTgt = MovementSystem.getTarget();
        if (moveTgt != null) {
            JsonObject target = new JsonObject();
            target.addProperty("x", moveTgt.x);
            target.addProperty("y", moveTgt.y);
            target.addProperty("z", moveTgt.z);
            state.add("move_target", target);
        }

        // AI control and behavior state
        state.addProperty("ai_controlled", isAiControlled());
        state.addProperty("behaviors_enabled", LCUMod.BEHAVIORS != null ? LCUMod.BEHAVIORS.isEnabled() : false);

        sendResponse(cmd.id(), true, state);
    }

    /** interact / interact_block：和 use_on 是同一件事（通用右键），不再有"命中方块→useItemOn / 否则 useItem"的分支。 */
    private void handleInteract(WireServer.WireCommand cmd) {
        var mc = Minecraft.getInstance();
        if (mc.player == null || mc.gameMode == null) {
            sendResponse(cmd.id(), false, "No player");
            return;
        }
        ContainerController.genericRightClick(mc, 4);
        sendResponse(cmd.id(), true, "Right-click");
    }

    private void handleMine(WireServer.WireCommand cmd) {
        var mc = Minecraft.getInstance();
        if (mc.player == null || mc.gameMode == null || mc.level == null) {
            sendResponse(cmd.id(), false, "No player/gameMode");
            return;
        }
        var hit = mc.hitResult;
        if (!(hit instanceof BlockHitResult blockHit) || mc.level.isEmptyBlock(blockHit.getBlockPos())) {
            sendResponse(cmd.id(), false, "No block targeted");
            return;
        }

        // Auto-equip best tool (mineflayer-style)
        autoEquipForBlock(mc);

        BlockPos pos = blockHit.getBlockPos();
        Direction dir = blockHit.getDirection();

        // 区域硬保护：农场/基地里不许破坏方块（保护优先于允许）
        ZoneGuard.Block mineBlock = ZoneGuard.block(pos.getX() + 0.5, pos.getY() + 0.5, pos.getZ() + 0.5,
                ZoneGuard.Action.MINE, false);
        if (mineBlock != null) {
            sendZoneBlocked(cmd, mineBlock, "mine_block " + pos.toShortString());
            return;
        }

        // 按住 attack，直到方块消失（成功）或不再瞄准它；20s 未破坏 → 诚实失败。
        // 谓词每 tick 在 ControlStream 里复查：false = 目标达成（方块没了）/ 失去目标。
        miningHold = ControlStream.holdUntil("attack", () -> {
            Minecraft m = Minecraft.getInstance();
            if (m.level == null) {
                return false;
            }
            if (m.level.isEmptyBlock(pos)) {
                return false; // 破坏成功
            }
            return m.hitResult instanceof BlockHitResult b && b.getBlockPos().equals(pos);
        }, MINE_TIMEOUT_TICKS);
        miningReqId = cmd.id();
        miningPos = pos;
        miningProgressTicks = 0;
        setTaskState("mine", "running", pos.toShortString(), "holding attack", 0.05);
        LCUMod.LOGGER.info("[Action] Mining {} (dir={}) with tool-slot {}", pos, dir, mc.player.getInventory().getSelectedSlot());
        sendResponse(cmd.id(), true, "Digging " + pos.toShortString());
    }

    /** 挖掘 hold 的收尾：hold 已松开时判断成功/失败并诚实上报。 */
    private void tickMining(Minecraft mc) {
        if (miningHold == null) {
            return;
        }
        if (miningHold.isActive()) {
            miningProgressTicks++;
            if (miningReqId != null && miningProgressTicks % 10 == 0 && LCUMod.WIRE != null) {
                LCUMod.WIRE.sendProgress(miningReqId, 0.3, "mining");
            }
            return;
        }

        HoldController.Release reason = miningHold.releaseReason();
        boolean broken = miningPos != null && mc.level != null && mc.level.isEmptyBlock(miningPos);
        String id = miningReqId;
        BlockPos done = miningPos;
        miningHold = null;
        miningReqId = null;
        miningPos = null;
        miningProgressTicks = 0;

        String target = done == null ? "" : done.toShortString();
        if (broken) {
            setTaskState("mine", "done", target, "block broken", 1.0);
            if (id != null && LCUMod.WIRE != null) {
                LCUMod.WIRE.sendOutcome(id, "succeeded", "block broken", "");
            }
        } else {
            String code = reason == HoldController.Release.TIMEOUT ? "timeout" : reason.code();
            setTaskState("mine", "failed", target, "mine failed: " + code, 0.0);
            if (id != null) {
                sendResponse(id, false, "Mine failed: " + code);
            }
        }
        sendBehaviorSnapshot();
    }

    /** Auto-select best tool for the targeted block (used internally by handleMine). */
    private void autoEquipForBlock(Minecraft mc) {
        var hit = mc.hitResult;
        if (hit == null || hit.getType() != net.minecraft.world.phys.HitResult.Type.BLOCK) return;
        var blockPos = ((net.minecraft.world.phys.BlockHitResult) hit).getBlockPos();
        var blockState = mc.level.getBlockState(blockPos);
        var inv = mc.player.getInventory();
        int bestSlot = -1;
        float bestSpeed = 0f;
        for (int i = 0; i < 9; i++) {
            var stack = inv.getItem(i);
            if (stack.isEmpty()) continue;
            float speed = stack.getDestroySpeed(blockState);
            if (speed <= 0) continue;
            float score = stack.isCorrectToolForDrops(blockState) ? speed * 3.0f : speed * 0.5f;
            if (score > bestSpeed) { bestSpeed = score; bestSlot = i; }
        }
        if (bestSlot >= 0 && bestSlot != inv.getSelectedSlot()) {
            inv.setSelectedSlot(bestSlot);
            LCUMod.LOGGER.debug("[Action] Auto-equipped slot {} for mining", bestSlot);
        }
    }

    private void handleStopDigging(WireServer.WireCommand cmd) {
        stopDigging();
        sendResponse(cmd.id(), true, "Digging stopped");
    }

    /**
     * use_item：按住 use {@code ticks} tick（默认 4）再松开。
     * 放方块/开门/吃等原版动作需要“按住”，只按一下会被原版在下一 tick 释放掉。
     * 参数 {@code ticks} 可选，缺省 4 —— 保证与后端 {@code skills.py} 的 {@code use_item{}} 兼容。
     */
    private void handleUseItem(WireServer.WireCommand cmd) {
        var mc = Minecraft.getInstance();
        if (mc.player == null || mc.gameMode == null) {
            sendResponse(cmd.id(), false, "No player");
            return;
        }
        int ticks = 4;
        var args = cmd.args();
        if (args != null && args.has("ticks")) {
            try {
                ticks = Math.max(1, Math.min(args.get("ticks").getAsInt(), 200));
            } catch (Exception ignored) {
                // 非法值回落到默认 4
            }
        }
        ControlStream.hold("use", ticks);
        ContainerController.markAiContainerIntent();
        sendResponse(cmd.id(), true, "Using item for " + ticks + " ticks");
    }

    private void stopDigging() {
        // 取消命令级“按住 attack”的挖掘（mine_block/dig）
        if (miningHold != null) {
            ControlStream.releaseHold("attack");
            String id = miningReqId;
            miningHold = null;
            miningReqId = null;
            miningPos = null;
            miningProgressTicks = 0;
            if (id != null) {
                sendResponse(id, false, "Digging stopped");
            }
        }
        // 取消资源采集用的连续挖掘
        if (diggingPos != null) {
            var mc = Minecraft.getInstance();
            if (mc.gameMode != null) {
                mc.gameMode.stopDestroyBlock();
            }
            LCUMod.LOGGER.info("[Action] Stopped digging {}", diggingPos);
            diggingPos = null;
            diggingDir = null;
            diggingTicks = 0;
        }
    }

    private void handleContinuousDigging(Minecraft mc) {
        if (diggingPos == null) return;

        diggingTicks++;
        if (diggingTicks > DIGGING_TIMEOUT_TICKS) {
            LCUMod.LOGGER.warn("[Action] Digging timeout at {}", diggingPos);
            stopDigging();
            return;
        }

        // Check if block still exists at position
        if (mc.level.isEmptyBlock(diggingPos)) {
            // Block was broken!
            LCUMod.LOGGER.info("[Action] Block broken at {}", diggingPos);
            stopDigging();
            return;
        }

        // Continue digging every tick
        mc.gameMode.continueDestroyBlock(diggingPos, diggingDir);
        // Periodic swing animation (every 7 ticks ≈ 0.35s, like vanilla mining)
        if (diggingTicks % 7 == 0) {
            mc.player.swing(InteractionHand.MAIN_HAND, SwingAnimation.DEFAULT, true);
        }
    }

    private void handlePlace(WireServer.WireCommand cmd) {
        var mc = Minecraft.getInstance();
        if (mc.player == null || mc.gameMode == null || mc.level == null) {
            sendResponse(cmd.id(), false, "No player/gameMode");
            return;
        }
        var args = cmd.args();
        BlockPos targetPos;
        Direction dir;
        if (args != null && args.has("x") && args.has("y") && args.has("z")) {
            targetPos = new BlockPos(args.get("x").getAsInt(), args.get("y").getAsInt(), args.get("z").getAsInt());
            Direction explicit = args.has("dir") ? Direction.byName(args.get("dir").getAsString()) : null;
            dir = explicit != null ? explicit : nearestFace(mc, targetPos);
        } else if (mc.hitResult instanceof BlockHitResult blockHit) {
            targetPos = blockHit.getBlockPos();
            dir = blockHit.getDirection();
        } else {
            sendResponse(cmd.id(), false, "No block targeted");
            return;
        }

        // 区域硬保护：农场/基地里不许放置方块（保护优先于允许）；判定的是"将要被放进去的那一格"
        BlockPos expectPos = targetPos.relative(dir);
        ZoneGuard.Block placeZone = ZoneGuard.block(
                expectPos.getX() + 0.5, expectPos.getY() + 0.5, expectPos.getZ() + 0.5,
                ZoneGuard.Action.PLACE, false);
        if (placeZone != null) {
            sendZoneBlocked(cmd, placeZone, "place_block " + expectPos.toShortString());
            return;
        }

        // 物品由**后端点名**（方块选择不在本地做启发式）。点名的物品必须真在背包里：找不到就如实失败，
        // **绝不回落到"手边随便一个方块"**（用户："若 place_block 收到一个不在背包里的物品 → 如实失败"）。
        String requested = args != null && args.has("item") ? args.get("item").getAsString() : "";
        ItemPick.Result pick = resolvePlaceItem(mc, requested);
        switch (pick.outcome()) {
            case NOT_FOUND -> {
                JsonObject data = new JsonObject();
                data.addProperty("ok", false);
                data.addProperty("reason", "item_not_in_inventory");
                data.addProperty("item", pick.id());
                LCUMod.LOGGER.warn("[Place] 点名的物品不在背包：{}（不替换成别的方块）", pick.id());
                sendResponse(cmd.id(), false, data, "item_not_in_inventory: " + pick.id());
                return;
            }
            case SELECT_HOTBAR, SWAP_INTO_HOTBAR -> {
                if (pick.outcome() == ItemPick.Outcome.SWAP_INTO_HOTBAR
                        && !swapIntoHotbar(mc, pick.slot())) {
                    JsonObject data = new JsonObject();
                    data.addProperty("ok", false);
                    data.addProperty("reason", "item_not_in_hotbar");
                    data.addProperty("item", pick.id());
                    sendResponse(cmd.id(), false, data, "item_not_in_hotbar: " + pick.id());
                    return;
                }
                if (pick.outcome() == ItemPick.Outcome.SELECT_HOTBAR) {
                    mc.player.getInventory().setSelectedSlot(pick.slot());
                }
            }
            default -> {
                // NOT_REQUESTED / IN_HAND：用当前手持，保持原有行为
            }
        }

        // 手上必须是方块，否则校验的目标方块无从谈起
        ItemStack hand = mc.player.getItemInHand(InteractionHand.MAIN_HAND);
        if (!(hand.getItem() instanceof BlockItem blockItem)) {
            sendResponse(cmd.id(), false, "No block in hand");
            return;
        }
        if (!ItemPick.normalize(requested).isEmpty()
                && !ItemPick.normalize(requested).equals(ItemPick.normalize(hand.getItem().toString()))) {
            // 点名的物品没能拿到手上（交换失败/被别的规则挪走）→ 如实失败，不拿别的方块顶
            JsonObject data = new JsonObject();
            data.addProperty("ok", false);
            data.addProperty("reason", "item_not_in_hand");
            data.addProperty("item", ItemPick.normalize(requested));
            data.addProperty("held", ItemPick.normalize(hand.getItem().toString()));
            sendResponse(cmd.id(), false, data, "item_not_in_hand");
            return;
        }

        // 先把旧的 place 收尾（幂等：重复调用不叠加）
        abortPendingPlace();

        placeExpectPos = expectPos;
        placeExpectBlock = blockItem.getBlock();
        // 先看向目标面（方块面中心），否则原版 useItemOn 会打错面 → 放空
        Vec3 face = faceCenter(targetPos, dir);
        aimAt(mc, face.x, face.y, face.z);
        placeYaw = mc.player.getYRot();
        placePitch = mc.player.getXRot();
        // 单次 use：按住 use 2 tick 再松开
        placeHold = ControlStream.hold("use", 2);
        placeReqId = cmd.id();
        placeTicks = 0;
        setTaskState("place", "running", placeExpectPos.toShortString(), "placing block", 0.1);
        sendResponse(cmd.id(), true, "Placing block at " + placeExpectPos.toShortString());
    }

    /** place_block 的收尾：保持朝向目标面，松手后校验目标位置是否变成要放的方块。 */
    private void tickPlace(Minecraft mc) {
        if (placeHold == null) {
            return;
        }
        if (!InputIsolation.isAiControlled()
                || (mc.mouseHandler != null && mc.mouseHandler.isMouseGrabbed())) {
            abortPendingPlace();
            return;
        }
        placeTicks++;
        // use 生效前保持朝向，避免被其它视角写入者（控制流/反射）带偏
        if (placeTicks <= 3 && mc.player != null) {
            mc.player.setYRot(placeYaw);
            mc.player.setXRot(placePitch);
        }
        if (placeTicks < PLACE_VERIFY_TICKS) {
            return;
        }

        boolean placed = placeExpectPos != null && placeExpectBlock != null
                && mc.level != null && mc.level.getBlockState(placeExpectPos).is(placeExpectBlock);
        String id = placeReqId;
        BlockPos expect = placeExpectPos;
        placeHold = null;
        placeReqId = null;
        placeExpectPos = null;
        placeExpectBlock = null;
        placeTicks = 0;

        String target = expect == null ? "" : expect.toShortString();
        if (placed) {
            setTaskState("place", "done", target, "block placed", 1.0);
            StateCollector.requestRefresh();
            if (id != null) {
                sendResponse(id, true, "Placed at " + target);
            }
        } else {
            setTaskState("place", "failed", target, "block not placed", 0.0);
            if (id != null) {
                sendResponse(id, false, "Block not placed at " + target);
            }
        }
        sendBehaviorSnapshot();
    }

    private void abortPendingPlace() {
        if (placeHold != null) {
            ControlStream.releaseHold("use");
        }
        String id = placeReqId;
        placeHold = null;
        placeReqId = null;
        placeExpectPos = null;
        placeExpectBlock = null;
        placeTicks = 0;
        if (id != null) {
            sendResponse(id, false, "Place aborted");
        }
    }

    /** 把后端点名的物品对上背包里真实存在的那一格（纯逻辑在 {@link ItemPick}）。 */
    private static ItemPick.Result resolvePlaceItem(Minecraft mc, String requested) {
        var inv = mc.player.getInventory();
        List<ItemPick.Slot> slots = new ArrayList<>();
        for (int i = 0; i < inv.getContainerSize(); i++) {
            ItemStack stack = inv.getItem(i);
            if (stack.isEmpty()) {
                continue;
            }
            slots.add(new ItemPick.Slot(i, stack.getItem().toString(), stack.getCount()));
        }
        String held = mc.player.getItemInHand(InteractionHand.MAIN_HAND).getItem().toString();
        return ItemPick.resolve(requested, slots, held, inv.getSelectedSlot());
    }

    /**
     * 把背包主区的一格换到当前快捷栏格（原版 {@code SWAP} 窗口点击，**不需要界面**）。
     *
     * <p>注意这只是"搬运同一个物品"，不是"换别的物品"；换不过去就返回 false，由调用方如实失败。
     * 开着别的容器时不动（避免和容器命令抢同一套槽位）。
     */
    private static boolean swapIntoHotbar(Minecraft mc, int invIndex) {
        var player = mc.player;
        if (player == null || mc.gameMode == null) {
            return false;
        }
        var menu = player.inventoryMenu;
        if (player.containerMenu != menu) {
            return false;
        }
        var inv = player.getInventory();
        Integer menuSlot = null;
        for (int i = 0; i < menu.slots.size(); i++) {
            var slot = menu.slots.get(i);
            if (slot.container == inv && slot.getContainerSlot() == invIndex) {
                menuSlot = i;
                break;
            }
        }
        if (menuSlot == null) {
            return false;
        }
        mc.gameMode.handleContainerInput(menu.containerId, menuSlot, inv.getSelectedSlot(),
                ContainerInput.SWAP, player);
        return true;
    }

    /** 中止进食（stop_all / 新任务 / AI 关闭）：松开 hold 并向所有 waiter 诚实回执失败。 */
    private void abortEat(String reason) {
        if (!EAT.isActive()) {
            return;
        }
        List<String> waiters = EAT.drainWaiters();
        EAT.reset();
        pendingEatSlot = -1;
        pendingEatPrimary = null;
        setTaskState("eat", "failed", "food", reason, 0.0);
        sendBehaviorSnapshot();
        for (String id : waiters) {
            if (id == null || REFLEX_EAT_REQ_ID.equals(id)) {
                continue;
            }
            JsonObject data = new JsonObject();
            data.addProperty("ok", false);
            data.addProperty("reason", reason);
            if (LCUMod.WIRE != null) {
                LCUMod.WIRE.sendResponse(id, false, data, reason);
            }
        }
    }

    /** 目标面中心（世界坐标）。 */
    private static Vec3 faceCenter(BlockPos pos, Direction dir) {
        return new Vec3(
                pos.getX() + 0.5 + dir.getStepX() * 0.5,
                pos.getY() + 0.5 + dir.getStepY() * 0.5,
                pos.getZ() + 0.5 + dir.getStepZ() * 0.5);
    }

    /** 离玩家眼睛最近的那个面（未显式给 dir 时用）。 */
    private static Direction nearestFace(Minecraft mc, BlockPos pos) {
        Vec3 center = Vec3.atCenterOf(pos);
        Vec3 eye = mc.player.getEyePosition();
        double dx = eye.x - center.x;
        double dy = eye.y - center.y;
        double dz = eye.z - center.z;
        double ax = Math.abs(dx), ay = Math.abs(dy), az = Math.abs(dz);
        if (ax >= ay && ax >= az) {
            return dx > 0 ? Direction.EAST : Direction.WEST;
        }
        if (ay >= az) {
            return dy > 0 ? Direction.UP : Direction.DOWN;
        }
        return dz > 0 ? Direction.SOUTH : Direction.NORTH;
    }

    /** 让玩家朝向某世界坐标点（并同步一次旋转包）。 */
    private void aimAt(Minecraft mc, double x, double y, double z) {
        var p = mc.player;
        double dx = x - p.getX();
        double dy = y - p.getEyeY();
        double dz = z - p.getZ();
        double dist = Math.sqrt(dx * dx + dy * dy + dz * dz);
        if (dist < 1.0E-4) {
            return;
        }
        float yaw = (float) Math.toDegrees(Math.atan2(-dx, dz));
        float pitch = (float) Math.toDegrees(-Math.asin(dy / dist));
        p.setYRot(yaw);
        p.setXRot(pitch);
        var conn = mc.getConnection();
        if (conn != null) {
            conn.send(new ServerboundMovePlayerPacket.Rot(yaw, pitch, p.onGround(), false));
        }
    }

    // ── New Handlers (mineflayer-style) ───────────────────────

    private void handleSetControlState(WireServer.WireCommand cmd) {
        // mineflayer-style: setControlState(control, state)
        // controls: forward, back, left, right, jump, sneak, sprint
        var args = cmd.args();
        if (args == null || !args.has("control") || !args.has("state")) {
            sendResponse(cmd.id(), false, "Need control + state");
            return;
        }
        var mc = Minecraft.getInstance();
        if (mc.player == null) { sendResponse(cmd.id(), false, "No player"); return; }
        String control = args.get("control").getAsString();
        boolean state = args.get("state").getAsBoolean();

        switch (control) {
            case "forward" -> {
                if (state) { MovementSystem.stop(); } // cancel auto-nav
                setInput("forward", state);
            }
            case "back" -> setInput("back", state);
            case "left" -> setInput("left", state);
            case "right" -> setInput("right", state);
            case "jump" -> {
                if (state && jumpCooldown <= 0) {
                    mc.player.jumpFromGround();
                    jumpCooldown = JUMP_COOLDOWN_TICKS;
                }
            }
            case "sneak" -> mc.player.setShiftKeyDown(state);
            case "sprint" -> mc.player.setSprinting(state);
            default -> { sendResponse(cmd.id(), false, "Unknown control: " + control); return; }
        }
        sendResponse(cmd.id(), true, control + "=" + state);
    }

    private void handleAutoEquip(WireServer.WireCommand cmd) {
        // Auto-select best tool for the block being looked at (multi-type, multi-tier)
        var mc = Minecraft.getInstance();
        if (mc.player == null || mc.gameMode == null) {
            sendResponse(cmd.id(), false, "No player");
            return;
        }
        var hit = mc.hitResult;
        if (hit == null || hit.getType() != net.minecraft.world.phys.HitResult.Type.BLOCK) {
            sendResponse(cmd.id(), false, "No block targeted");
            return;
        }
        var blockPos = ((net.minecraft.world.phys.BlockHitResult) hit).getBlockPos();
        var blockState = mc.level.getBlockState(blockPos);
        var inv = mc.player.getInventory();

        int bestSlot = -1;
        float bestSpeed = 0f;

        for (int i = 0; i < 9; i++) {
            var stack = inv.getItem(i);
            if (stack.isEmpty()) continue;
            float speed = stack.getDestroySpeed(blockState);
            if (speed <= 0) continue;
            // Correct tool gets 3x bonus to ensure it wins over bare hand/sword
            float score = stack.isCorrectToolForDrops(blockState) ? speed * 3.0f : speed * 0.5f;
            if (score > bestSpeed) {
                bestSpeed = score;
                bestSlot = i;
            }
        }

        if (bestSlot >= 0 && bestSlot != inv.getSelectedSlot()) {
            inv.setSelectedSlot(bestSlot);
            var toolStack = inv.getItem(bestSlot);
            sendResponse(cmd.id(), true, "Equipped: " + toolStack.getDisplayName().getString());
        } else if (bestSlot >= 0) {
            sendResponse(cmd.id(), true, "Already correct tool");
        } else {
            sendResponse(cmd.id(), true, "No tool, using hand");
        }
    }

    private void handleGetInventory(WireServer.WireCommand cmd) {
        var mc = Minecraft.getInstance();
        if (mc.player == null) { sendResponse(cmd.id(), false, "No player"); return; }

        JsonObject result = new JsonObject();
        JsonArray items = new JsonArray();
        var inv = mc.player.getInventory();
        for (int i = 0; i < inv.getContainerSize(); i++) {
            var stack = inv.getItem(i);
            if (stack.isEmpty()) continue;
            JsonObject item = new JsonObject();
            item.addProperty("slot", i);
            item.addProperty("name", stack.getItem().toString());
            item.addProperty("count", stack.getCount());
            item.addProperty("display", stack.getDisplayName().getString());
            items.add(item);
        }
        result.add("inventory", items);
        result.addProperty("selected", inv.getSelectedSlot());
        sendResponse(cmd.id(), true, result);
    }

    private void handleStopAll(WireServer.WireCommand cmd) {
        var mc = Minecraft.getInstance();
        followTargetName = null;
        pendingCraftItem = null;
        pendingCraftReqId = null;
        pendingCraftTicks = 0;
        pendingCraftAttempts = 0;
        pendingCraftGoalCount = 1;
        abortEat("stopped");
        pendingCollectItem = null;
        pendingCollectReqId = null;
        pendingCollectGoalCount = 0;
        pendingCollectBaselineCount = 0;
        pendingCollectTargetPos = null;
        pendingCollectTicks = 0;
        pendingCollectSearchMisses = 0;
        Pathfinder.stop();
        MovementSystem.stop();
        releaseAllInputs();
        releaseActiveHolds(HoldController.Release.MANUAL);
        abortPendingPlace();
        // 驻守 / 重放 / 记录：stop_all 一视同仁全部停下（记录直接丢弃，不留下半条线）
        AFK.stop();
        afkReturnAttempts = 0;
        afkReturnCooldown = 0;
        ROUTE.reset();
        resetRouteStall();
        routeRecorder = null;
        routeRecordName = "";
        if (mc.player != null) {
            if (mc.gameMode != null) stopDigging();
            if (mc.player.isUsingItem()) {
                mc.gameMode.releaseUsingItem(mc.player);
            }
            if (mc.player.containerMenu != null && mc.player.containerMenu != mc.player.inventoryMenu) {
                mc.player.closeContainer();
            }
        }
        clearTaskState();
        sendBehaviorSnapshot();
        sendResponse(cmd.id(), true, "All stopped");
    }

    // ── Container Interaction (mineflayer-style) ───────────────

    private void handleUseOn(WireServer.WireCommand cmd) {
        // 通用右键：**不判断命中的是方块还是实体，也不判断手上是什么** —— 看向 + 按住 use，
        // 由原版自己分派。之前这里按 hitResult 分 useItemOn/useItem 就是"特殊交互"的来源。
        var mc = Minecraft.getInstance();
        if (mc.player == null || mc.gameMode == null) {
            sendResponse(cmd.id(), false, "No player");
            return;
        }
        int ticks = 4;
        var args = cmd.args();
        if (args != null && args.has("ticks")) {
            try {
                ticks = Math.max(1, Math.min(args.get("ticks").getAsInt(), 200));
            } catch (Exception ignored) {
                // 非法值回落到默认 4
            }
        }
        ContainerController.genericRightClick(mc, ticks);
        sendResponse(cmd.id(), true, "Right-click (hold use " + ticks + "t)");
    }

    private void handleGetContainer(WireServer.WireCommand cmd) {
        // Read contents of the currently open container/chest
        var mc = Minecraft.getInstance();
        if (mc.player == null) { sendResponse(cmd.id(), false, "No player"); return; }

        var menu = mc.player.containerMenu;
        if (menu == null) { sendResponse(cmd.id(), false, "No container open"); return; }

        // Send the container's items in the response
        JsonObject result = new JsonObject();
        result.addProperty("container_id", menu.containerId);
        result.addProperty("slots", menu.slots.size());

        JsonArray items = new JsonArray();
        for (int i = 0; i < menu.slots.size(); i++) {
            var stack = menu.slots.get(i).getItem();
            if (stack.isEmpty()) continue;
            JsonObject item = new JsonObject();
            item.addProperty("slot", i);
            item.addProperty("name", stack.getItem().toString());
            item.addProperty("count", stack.getCount());
            item.addProperty("display", stack.getDisplayName().getString());
            items.add(item);
        }
        result.add("items", items);
        sendResponse(cmd.id(), true, result);
    }

    /** take_item {slot}：容器里 shift 点击取出（QUICK_MOVE）—— 统一走 ContainerController。 */
    private void handleTakeItem(WireServer.WireCommand cmd) {
        quickMoveFromCmd(cmd, "Taking from slot ");
    }

    /** put_item {slot}：玩家背包里 shift 点击存入（QUICK_MOVE）—— 同一个原版语义。 */
    private void handlePutItem(WireServer.WireCommand cmd) {
        quickMoveFromCmd(cmd, "Putting into slot ");
    }

    private void quickMoveFromCmd(WireServer.WireCommand cmd, String prefix) {
        var args = cmd.args();
        if (args == null || !args.has("slot")) {
            sendResponse(cmd.id(), false, "Need slot number");
            return;
        }
        var mc = Minecraft.getInstance();
        if (mc.player == null || mc.gameMode == null) {
            sendResponse(cmd.id(), false, "No player");
            return;
        }
        int slot = args.get("slot").getAsInt();
        // to_slot = -1 → 原版 QUICK_MOVE（shift 点击）
        sendControllerResult(cmd.id(), ContainerController.move(mc, slot, -1, 0), prefix + slot);
    }

    /** close_container / container_close：发关闭包（不开界面、幂等）。 */
    private void handleCloseContainer(WireServer.WireCommand cmd) {
        var mc = Minecraft.getInstance();
        var res = ContainerController.close(mc);
        sendControllerResult(cmd.id(), res, res.reason());
    }

    // ── UI 驱动层命令（一律发窗口点击包）──────────────────────────

    /** container_move {from_slot,to_slot,count?}；to_slot < 0 = QUICK_MOVE（背包↔快捷栏）。 */
    private void handleContainerMove(WireServer.WireCommand cmd) {
        var args = cmd.args();
        if (args == null || !args.has("from_slot")) {
            sendResponse(cmd.id(), false, "Need from_slot");
            return;
        }
        var mc = Minecraft.getInstance();
        int from = args.get("from_slot").getAsInt();
        int to = args.has("to_slot") ? args.get("to_slot").getAsInt() : -1;
        int count = args.has("count") ? args.get("count").getAsInt() : 0;
        sendControllerResult(cmd.id(), ContainerController.move(mc, from, to, count),
                "Moved slot " + from + " -> " + to);
    }

    /** container_click {slot,button?,click_type?}（click_type = ContainerInput 小写名）。 */
    private void handleContainerClick(WireServer.WireCommand cmd) {
        var args = cmd.args();
        if (args == null || !args.has("slot")) {
            sendResponse(cmd.id(), false, "Need slot");
            return;
        }
        var mc = Minecraft.getInstance();
        int slot = args.get("slot").getAsInt();
        int button = args.has("button") ? args.get("button").getAsInt() : 0;
        String clickType = args.has("click_type") ? args.get("click_type").getAsString() : "pickup";
        sendControllerResult(cmd.id(), ContainerController.click(mc, slot, button, clickType),
                "Clicked slot " + slot + " (" + clickType + ")");
    }

    /** equip_armor {}：背包里找盔甲 → 穿到对应装备槽 → 校验真的穿上了。 */
    private void handleEquipArmor(WireServer.WireCommand cmd) {
        var mc = Minecraft.getInstance();
        sendControllerResult(cmd.id(), ContainerController.equipArmor(mc), "equip_armor");
    }

    /**
     * craft_via_recipe {item?,display_id?,count?}：原版配方放置 + 结果槽 QUICK_MOVE + 诚实 outcome。
     * 产物名先经 {@link RecipeQuery#craftTarget} 对齐（**歧义不猜**，回候选），或用
     * search_recipes 给出的精确 display_id。
     */
    private void handleCraftViaRecipe(WireServer.WireCommand cmd) {
        var args = cmd.args();
        var mc = Minecraft.getInstance();
        Integer displayId = args != null && args.has("display_id") && !args.get("display_id").isJsonNull()
                ? args.get("display_id").getAsInt() : null;
        String item = args != null && args.has("item") && !args.get("item").isJsonNull()
                ? args.get("item").getAsString() : null;
        int count = args != null && args.has("count") ? args.get("count").getAsInt() : 1;

        if (displayId == null && (item == null || item.isBlank())) {
            sendResponse(cmd.id(), false, "Need item or display_id");
            return;
        }

        if (displayId != null) {
            var res = ContainerController.craftViaDisplay(mc, displayId, count);
            if (res.ok()) {
                ContainerController.trackCraftOutcome(cmd.id());
            }
            sendControllerResult(cmd.id(), res, "craft_via_recipe display=" + displayId);
            return;
        }

        // 产物对齐：歧义/未知/tag 一律拒结并回候选（不猜）
        RecipeQuery.CraftTarget target = RecipeTools.craftTarget(mc, item);
        if (!target.ok()) {
            JsonObject data = new JsonObject();
            data.addProperty("query", item);
            data.addProperty("reason", target.reason());
            data.add("candidates", candidatesJson(target.candidates()));
            data.addProperty("message", "craft_via_recipe " + item);
            if (LCUMod.WIRE != null) {
                LCUMod.WIRE.sendResponse(cmd.id(), false, data, target.reason());
            }
            return;
        }
        var res = ContainerController.craftViaRecipe(mc, target.itemId(), count);
        if (res.ok()) {
            // 配方放置包已发出：登记诚实 outcome（背包真的多出物品才算成功，超时恒实失败）
            ContainerController.trackCraftOutcome(cmd.id());
        }
        sendControllerResult(cmd.id(), res, "craft_via_recipe " + target.itemId());
    }

    /** resolve_item {query}：把 query 当 minecraft:id / 中文名 / 英文名 / #tag 对齐到真实注册表。 */
    private void handleResolveItem(WireServer.WireCommand cmd) {
        var args = cmd.args();
        String query = strArg(args, "query");
        if (query == null) {
            query = strArg(args, "item");
        }
        if (query == null) {
            sendResponse(cmd.id(), false, "Need query");
            return;
        }
        var mc = Minecraft.getInstance();
        RecipeQuery.Resolution r = RecipeTools.resolveItem(mc, query);
        JsonObject data = resolutionJson(query, r);
        if (LCUMod.WIRE == null) {
            return;
        }
        if (r.ok()) {
            LCUMod.WIRE.sendResponse(cmd.id(), true, data);
        } else {
            LCUMod.WIRE.sendResponse(cmd.id(), false, data, r.reason());
        }
    }

    /** search_recipes {output?,input?,station?,limit?}：数据源 = 服务端发来的配方显示。 */
    private void handleSearchRecipes(WireServer.WireCommand cmd) {
        var args = cmd.args();
        String output = strArg(args, "output");
        String input = strArg(args, "input");
        String station = strArg(args, "station");
        int limit = args != null && args.has("limit") ? args.get("limit").getAsInt() : 0;
        var mc = Minecraft.getInstance();
        RecipeQuery.SearchResult res = RecipeTools.searchRecipes(mc, output, input, station, limit);

        JsonObject data = new JsonObject();
        data.addProperty("reason", res.reason());
        data.addProperty("total", res.total());
        data.addProperty("truncated", res.truncated());
        data.addProperty("count", res.rows().size());
        JsonArray recipes = new JsonArray();
        for (RecipeQuery.RecipeRow row : res.rows()) {
            recipes.add(recipeRowJson(row));
        }
        data.add("recipes", recipes);
        if (!res.candidates().isEmpty()) {
            data.add("candidates", candidatesJson(res.candidates()));
        }
        if (LCUMod.WIRE == null) {
            return;
        }
        if (res.ok()) {
            LCUMod.WIRE.sendResponse(cmd.id(), true, data);
        } else {
            LCUMod.WIRE.sendResponse(cmd.id(), false, data, res.reason());
        }
    }

    private static String strArg(JsonObject args, String name) {
        if (args == null || !args.has(name) || args.get(name).isJsonNull()) {
            return null;
        }
        String v = args.get(name).getAsString();
        return v == null || v.isBlank() ? null : v;
    }

    private static JsonObject resolutionJson(String query, RecipeQuery.Resolution r) {
        JsonObject data = new JsonObject();
        data.addProperty("query", query);
        data.addProperty("ok", r.ok());
        data.addProperty("reason", r.reason());
        if (r.match() != null) {
            JsonObject m = new JsonObject();
            m.addProperty("item", r.match().item());
            m.addProperty("name", r.match().name());
            m.addProperty("count", r.match().count());
            m.addProperty("tag", r.match().tag());
            data.add("match", m);
        }
        data.add("candidates", candidatesJson(r.candidates()));
        return data;
    }

    private static JsonArray candidatesJson(List<RecipeQuery.Candidate> candidates) {
        JsonArray arr = new JsonArray();
        if (candidates != null) {
            for (RecipeQuery.Candidate c : candidates) {
                JsonObject o = new JsonObject();
                o.addProperty("item", c.item());
                o.addProperty("name", c.name());
                o.addProperty("score", c.score());
                o.addProperty("reason", c.reason());
                arr.add(o);
            }
        }
        return arr;
    }

    private static JsonObject recipeRowJson(RecipeQuery.RecipeRow row) {
        JsonObject o = new JsonObject();
        o.addProperty("display_id", row.displayId());
        o.addProperty("station", row.station());
        o.addProperty("supported", row.supported());
        o.addProperty("recipe", row.recipeId());
        JsonArray inputs = new JsonArray();
        if (row.inputs() != null) {
            for (RecipeQuery.Stack s : row.inputs()) {
                inputs.add(stackJson(s));
            }
        }
        o.add("inputs", inputs);
        JsonObject out = new JsonObject();
        out.addProperty("item", row.output().item());
        out.addProperty("count", row.output().count());
        out.addProperty("name", nameForStack(row.output().item()));
        o.add("output", out);
        return o;
    }

    private static JsonObject stackJson(RecipeQuery.Stack s) {
        JsonObject o = new JsonObject();
        o.addProperty("item", s.item());
        o.addProperty("count", s.count());
        o.addProperty("name", nameForStack(s.item()));
        return o;
    }

    private static String nameForStack(String itemId) {
        return itemId != null && itemId.startsWith("#") ? itemId : RecipeTools.displayName(itemId);
    }

    /** 把 ContainerController 的结果如实转成 wire 回执（成功/失败都带机器可读 reason）。 */
    private void sendControllerResult(String id, ContainerController.Result res, String okMessage) {
        if (LCUMod.WIRE == null) {
            return;
        }
        JsonObject data = res.data() != null ? res.data() : new JsonObject();
        if (okMessage != null) {
            data.addProperty("message", okMessage);
        }
        if (res.ok()) {
            LCUMod.WIRE.sendResponse(id, true, data);
        } else {
            LCUMod.WIRE.sendResponse(id, false, data, res.reason());
        }
    }

    private void handleLookAtEntity(WireServer.WireCommand cmd) {
        // Look at a specific entity by ID (mineflayer-style)
        var args = cmd.args();
        if (args == null || !args.has("id")) {
            sendResponse(cmd.id(), false, "Need entity id");
            return;
        }
        var mc = Minecraft.getInstance();
        if (mc.player == null || mc.level == null) {
            sendResponse(cmd.id(), false, "No player/level");
            return;
        }
        int entityId = args.get("id").getAsInt();
        var entity = mc.level.getEntity(entityId);
        if (entity != null) {
            double dx = entity.getX() - mc.player.getX();
            double dy = entity.getEyeY() - mc.player.getEyeY();
            double dz = entity.getZ() - mc.player.getZ();
            double hDist = Math.sqrt(dx * dx + dz * dz);
            float yaw = (float) Math.toDegrees(Math.atan2(-dx, dz));
            float pitch = (float) Math.toDegrees(-Math.atan2(dy, hDist));
            mc.player.setYRot(yaw);
            mc.player.setXRot(pitch);
            sendResponse(cmd.id(), true, "Looking at entity " + entityId);
        } else {
            sendResponse(cmd.id(), false, "Entity " + entityId + " not found");
        }
    }

    private void handleUseOnEntity(WireServer.WireCommand cmd) {
        // Interact with a specific entity by ID (mineflayer-style)
        var args = cmd.args();
        if (args == null || !args.has("id")) {
            sendResponse(cmd.id(), false, "Need entity id");
            return;
        }
        var mc = Minecraft.getInstance();
        if (mc.player == null || mc.gameMode == null || mc.level == null) {
            sendResponse(cmd.id(), false, "No player/gameMode");
            return;
        }
        int entityId = args.get("id").getAsInt();
        var entity = mc.level.getEntity(entityId);
        if (entity != null) {
            // 看向实体 + 通用右键（不再直接 interact(entity)：语义交给后端/原版）
            ContainerController.aimAt(mc, entity.getEyePosition());
            ContainerController.genericRightClick(mc, 4);
            sendResponse(cmd.id(), true, "Right-clicking entity " + entityId);
        } else {
            sendResponse(cmd.id(), false, "Entity " + entityId + " not found");
        }
    }

    // ── Advanced Action Handlers ────────────────────────────────

    private void handleFollowPlayer(WireServer.WireCommand cmd) {
        var args = cmd.args();
        if (args == null || !args.has("player")) {
            sendResponse(cmd.id(), false, "Need player name");
            return;
        }
        var mc = Minecraft.getInstance();
        if (mc.player == null || mc.level == null) {
            sendResponse(cmd.id(), false, "No player");
            return;
        }
        String playerName = args.get("player").getAsString();
        pendingCraftItem = null;
        pendingCraftReqId = null;
        pendingCraftTicks = 0;
        pendingCraftAttempts = 0;
        abortEat("new_task");
        followTargetName = playerName;
        followRefreshTicks = 0;
        setTaskState("follow", "running", playerName, "following player", 0.1);
        
        // Find player by name
        for (var player : mc.level.players()) {
            if (player.getName().getString().equalsIgnoreCase(playerName)) {
                // Move toward player
                MovementSystem.moveTo(player.getX(), player.getY(), player.getZ(), 1.2f);
                sendBehaviorSnapshot();
                sendResponse(cmd.id(), true, "Following " + playerName);
                return;
            }
        }
        followTargetName = null;
        sendBehaviorSnapshot();
        sendResponse(cmd.id(), false, "Player " + playerName + " not found");
    }

    private void handleCraftItem(WireServer.WireCommand cmd) {
        var args = cmd.args();
        if (args == null || !args.has("item")) {
            sendResponse(cmd.id(), false, "Need item name");
            return;
        }
        var mc = Minecraft.getInstance();
        if (mc.player == null) {
            sendResponse(cmd.id(), false, "No player");
            return;
        }
        String itemName = normalizeItemName(args.get("item").getAsString());
        int requestedCount = args.has("count") ? Math.max(1, args.get("count").getAsInt()) : 1;

        followTargetName = null;

        pendingCraftItem = itemName;
        pendingCraftReqId = cmd.id();
        pendingCraftTicks = 0;
        pendingCraftAttempts = 0;
        pendingCraftGoalCount = Math.max(countInventoryItem(mc, itemName), requestedCount);
        if (pendingCraftGoalCount < requestedCount) {
            pendingCraftGoalCount = requestedCount;
        }
        if (countInventoryItem(mc, itemName) >= requestedCount) {
            pendingCraftGoalCount = countInventoryItem(mc, itemName);
        }
        clearPendingCollectTask();
        setTaskState("craft", "planning", itemName, "analyzing recipe graph", 0.02);
        sendBehaviorSnapshot();
        if (LCUMod.WIRE != null) {
            LCUMod.WIRE.sendProgress(cmd.id(), 0.05, "craft queued: " + itemName);
        }
        sendResponse(cmd.id(), true, "Crafting queued for " + itemName);
    }

    private void handleCollectBlocks(WireServer.WireCommand cmd) {
        var args = cmd.args();
        if (args == null || !args.has("block_type")) {
            sendResponse(cmd.id(), false, "Need block type");
            return;
        }
        var mc = Minecraft.getInstance();
        if (mc.player == null) {
            sendResponse(cmd.id(), false, "No player");
            return;
        }
        String blockType = normalizeItemName(args.get("block_type").getAsString());
        int count = args.has("count") ? args.get("count").getAsInt() : 1;
        followTargetName = null;

        startCollectTask(mc, cmd.id(), blockType, Math.max(1, count));
        setTaskState("collect", "searching", blockType, "searching nearby resources", 0.02);
        sendBehaviorSnapshot();
        sendResponse(cmd.id(), true, "Collecting " + blockType);
    }

    private void handleExplore(WireServer.WireCommand cmd) {
        var args = cmd.args();
        int radius = 16;
        if (args != null && args.has("radius")) {
            radius = args.get("radius").getAsInt();
        }
        var mc = Minecraft.getInstance();
        if (mc.player == null) {
            sendResponse(cmd.id(), false, "No player");
            return;
        }
        
        // Choose random position within radius
        double angle = Math.random() * Math.PI * 2;
        double distance = Math.random() * radius;
        double targetX = mc.player.getX() + Math.cos(angle) * distance;
        double targetZ = mc.player.getZ() + Math.sin(angle) * distance;
        double targetY = mc.player.getY();
        
        MovementSystem.moveTo(targetX, targetY, targetZ, 0.8f);
        sendResponse(cmd.id(), true, "Exploring area");
    }

    private void handleTrade(WireServer.WireCommand cmd) {
        var args = cmd.args();
        if (args == null || !args.has("villager_type")) {
            sendResponse(cmd.id(), false, "Need villager type");
            return;
        }
        // Honest failure: no villager UI automation yet
        sendResponse(cmd.id(), false, "trade not implemented (villager UI automation pending)");
    }

    private void handleSleep(WireServer.WireCommand cmd) {
        var mc = Minecraft.getInstance();
        if (mc.player == null || mc.level == null || mc.gameMode == null) {
            sendResponse(cmd.id(), false, "No player");
            return;
        }
        var player = mc.player;
        JsonObject data = new JsonObject();

        if (player.isSleeping()) {
            data.addProperty("status", "already_sleeping");
            data.addProperty("message", "already in bed");
            sendResponse(cmd.id(), true, data);
            return;
        }

        // 26.3 的 ClientLevel **不再暴露 getDayTime**（常量池实证：只有 getGameTime/getSkyDarken）。
        // 用**天空暗度**判定夜晚：它同时含天色与天气，正好等价于原版"夜晚或雷雨天可睡"。
        int skyDarken = mc.level.getSkyDarken();
        boolean night = skyDarken >= 4 || mc.level.isThundering();
        data.addProperty("sky_darken", skyDarken);
        // day_time 只是 gameTime 的近似（睡觉会重置真正的 dayTime），**不要**据此下判断
        data.addProperty("day_time", (int) (mc.level.getGameTime() % 24000L));
        data.addProperty("is_night", night);
        if (!night) {
            data.addProperty("status", "not_night");
            data.addProperty("message", "not night yet (sky_darken=" + skyDarken + ")");
            sendResponse(cmd.id(), false, data);
            return;
        }

        var beds = BlockFinder.scan(mc, "*_bed", 32, 1);
        if (beds.isEmpty()) {
            data.addProperty("status", "no_bed");
            data.addProperty("message", "no bed within 32m");
            sendResponse(cmd.id(), false, data);
            return;
        }

        BlockFinder.Hit bed = beds.get(0);
        BlockPos bedPos = new BlockPos(bed.x(), bed.y(), bed.z());
        data.addProperty("distance", Math.round(bed.distance() * 10.0) / 10.0);
        data.add("bed", hitJson(bed));

        if (bed.distance() > BlockFinder.REACH_DISTANCE) {
            // 关键改动：旧实现床在 4 米外就直接失败（"bed too far, move closer"）——
            // 于是表现成"他不会找床睡觉"。现在**自动寻路过去**，到达后由
            // tickPendingBlockTasks 接手点床。
            followTargetName = null;
            Pathfinder.navigateTo(cmd.id(), bed.x(), bed.y(), bed.z());
            pendingSleep = true;
            pendingSleepBed = bedPos;
            pendingSleepTicks = 0;
            setTaskState("sleep", "running", posKey(bedPos), "walking to bed", 0.1);
            data.addProperty("status", "walking");
            data.addProperty("message", "walking to bed " + posKey(bedPos));
            sendResponse(cmd.id(), true, data);
            return;
        }

        useBed(mc, bedPos);
        data.addProperty("status", "sleeping");
        data.addProperty("message", "sleeping");
        sendResponse(cmd.id(), true, data);
    }

    // ── 找方块 / 走过去 / 记忆点：用户要的"获得周围方块位置 + 寻路 + 记忆"三件套 ──

    private static JsonObject hitJson(BlockFinder.Hit h) {
        JsonObject o = new JsonObject();
        o.addProperty("x", h.x());
        o.addProperty("y", h.y());
        o.addProperty("z", h.z());
        o.addProperty("id", h.id());
        o.addProperty("distance", Math.round(h.distance() * 10.0) / 10.0);
        return o;
    }

    /** 只扫、只回报（不移动）：让 LLM/用户拿到"周围有什么、多远"。 */
    private void handleFindBlock(WireServer.WireCommand cmd) {
        var mc = Minecraft.getInstance();
        if (mc.player == null || mc.level == null) {
            sendResponse(cmd.id(), false, "No player");
            return;
        }
        JsonObject args = cmd.args();
        String block = args != null && args.has("block") ? args.get("block").getAsString() : "";
        if (block.isBlank()) {
            sendResponse(cmd.id(), false, "Need block (id or pattern like *_bed)");
            return;
        }
        int radius = args.has("radius") ? args.get("radius").getAsInt() : BlockFinder.DEFAULT_RADIUS;
        int limit = args.has("limit") ? args.get("limit").getAsInt() : BlockFinder.DEFAULT_LIMIT;

        long started = System.currentTimeMillis();
        var hits = BlockFinder.scan(mc, block, radius, limit);
        JsonObject data = new JsonObject();
        data.addProperty("block", block);
        data.addProperty("radius", radius);
        data.addProperty("count", hits.size());
        data.addProperty("took_ms", (int) (System.currentTimeMillis() - started));
        JsonArray arr = new JsonArray();
        for (BlockFinder.Hit hit : hits) {
            arr.add(hitJson(hit));
        }
        data.add("found", arr);
        if (!hits.isEmpty()) {
            data.add("nearest", hitJson(hits.get(0)));
        }
        if (hits.isEmpty()) {
            data.addProperty("message", "no " + block + " within " + radius + "m");
        }
        sendResponse(cmd.id(), !hits.isEmpty(), data);
    }

    /** 找到最近的目标方块并**自动寻路过去**；到达后（可选）写入持久记忆。 */
    private void handleGoToBlock(WireServer.WireCommand cmd) {
        var mc = Minecraft.getInstance();
        if (mc.player == null || mc.level == null) {
            sendResponse(cmd.id(), false, "No player");
            return;
        }
        JsonObject args = cmd.args();
        String block = args != null && args.has("block") ? args.get("block").getAsString() : "";
        if (block.isBlank()) {
            sendResponse(cmd.id(), false, "Need block (id or pattern like *_bed)");
            return;
        }
        int radius = args.has("radius") ? args.get("radius").getAsInt() : BlockFinder.DEFAULT_RADIUS;
        boolean remember = !args.has("remember") || args.get("remember").getAsBoolean();

        var hits = BlockFinder.scan(mc, block, radius, 1);
        if (hits.isEmpty()) {
            JsonObject data = new JsonObject();
            data.addProperty("block", block);
            data.addProperty("radius", radius);
            data.addProperty("count", 0);
            data.addProperty("message", "no " + block + " within " + radius + "m");
            sendResponse(cmd.id(), false, data);
            return;
        }

        BlockFinder.Hit hit = hits.get(0);
        followTargetName = null;
        Pathfinder.navigateTo(cmd.id(), hit.x(), hit.y(), hit.z());
        pendingGoTarget = new BlockPos(hit.x(), hit.y(), hit.z());
        pendingGoBlock = hit.id();
        pendingGoRemember = remember;
        pendingGoTicks = 0;
        setTaskState("travel", "running", hit.id() + " " + hit.x() + "," + hit.y() + "," + hit.z(),
                "heading to " + hit.id(), 0.1);

        JsonObject data = hitJson(hit);
        data.addProperty("going", true);
        data.addProperty("remember", remember);
        sendResponse(cmd.id(), true, data);
    }

    /** 主动记住一个点（当前脚下或指定坐标）。 */
    private void handleRememberPoi(WireServer.WireCommand cmd) {
        var mc = Minecraft.getInstance();
        if (mc.player == null || mc.level == null) {
            sendResponse(cmd.id(), false, "No player");
            return;
        }
        JsonObject args = cmd.args();
        String name = args != null && args.has("name") ? args.get("name").getAsString() : "";
        String category = args != null && args.has("category") && !args.get("category").getAsString().isBlank()
                ? args.get("category").getAsString() : "waypoint";
        BlockPos base = mc.player.blockPosition();
        int x = args != null && args.has("x") ? args.get("x").getAsInt() : base.getX();
        int y = args != null && args.has("y") ? args.get("y").getAsInt() : base.getY();
        int z = args != null && args.has("z") ? args.get("z").getAsInt() : base.getZ();
        BlockPos pos = new BlockPos(x, y, z);

        var state = mc.level.getBlockState(pos);
        String blockId = net.minecraft.core.registries.BuiltInRegistries.BLOCK
                .getKey(state.getBlock()).toString();
        PoiMemory.remember(pos, blockId, category, name, tickCount);
        StateCollector.requestRefresh();

        JsonObject data = new JsonObject();
        data.addProperty("saved", true);
        data.addProperty("name", name);
        data.addProperty("category", category);
        data.addProperty("x", x);
        data.addProperty("y", y);
        data.addProperty("z", z);
        data.addProperty("block_id", blockId);
        sendResponse(cmd.id(), true, data);
    }

    // ── 站点记忆 / 挂机 / 路径记忆与重放 ────────────────────────────────────────
    // 用户原话："让 AI 去某些点位去挂机（刷怪塔/小黑塔）……它得记住精准的 xyz"；
    // "还要记录路径（玩家挖的洞穴可能超出原版最大寻路距离）"。

    /**
     * 记一个站点：**显式坐标优先、缺省回退当前位姿**（同一条命令既用于"用当前位姿指认"，
     * 也用于后端把持久化的坐标回灌）；同名覆盖。
     */
    private void handleRememberStation(WireServer.WireCommand cmd) {
        var mc = Minecraft.getInstance();
        if (mc.player == null || mc.level == null) {
            sendResponse(cmd.id(), false, "No player");
            return;
        }
        JsonObject args = cmd.args();
        String name = args != null && args.has("name") ? args.get("name").getAsString().strip() : "";
        if (name.isEmpty()) {
            sendResponse(cmd.id(), false, "Need name");
            return;
        }
        String kind = args != null && args.has("kind") ? args.get("kind").getAsString() : null;
        Double x = optDouble(args, "x");
        Double y = optDouble(args, "y");
        Double z = optDouble(args, "z");
        boolean explicit = x != null && y != null && z != null;
        boolean existed = StationMemory.has(name);
        StationMemory.Station station = StationMemory.rememberWithPoseFallback(name, kind,
                x, y, z, optFloat(args, "yaw"), optFloat(args, "pitch"), optDouble(args, "radius"),
                mc.player.getX(), mc.player.getY(), mc.player.getZ(),
                mc.player.getYRot(), mc.player.getXRot(), tickCount);
        StateCollector.requestRefresh();

        JsonObject data = stationJson(station);
        data.addProperty("saved", true);
        data.addProperty("overwritten", existed);
        data.addProperty("explicit_coords", explicit);
        sendResponse(cmd.id(), true, data);
    }

    /**
     * 后端回灌整条路径（{@code remember_route}）：把后端持久化的路径点灌回模组，灌进来即可直接
     * {@code route_follow}。同名覆盖；点数 <2 或 >{@link RouteMemory#MAX_POINTS} 直接拒绝并给原因。
     */
    private void handleRememberRoute(WireServer.WireCommand cmd) {
        JsonObject args = cmd.args();
        String name = args != null && args.has("name") ? args.get("name").getAsString().strip() : "";
        if (name.isEmpty()) {
            sendResponse(cmd.id(), false, "Need name");
            return;
        }
        var raw = args != null && args.has("points") && args.get("points").isJsonArray()
                ? args.getAsJsonArray("points") : null;
        if (raw == null) {
            sendResponse(cmd.id(), false, "Need points array");
            return;
        }
        List<RouteMemory.RoutePoint> points = new ArrayList<>();
        for (int i = 0; i < raw.size(); i++) {
            var el = raw.get(i);
            if (!el.isJsonObject()) {
                sendResponse(cmd.id(), false, "Malformed point at index " + i + ": expected object");
                return;
            }
            Double px = optDouble(el.getAsJsonObject(), "x");
            Double py = optDouble(el.getAsJsonObject(), "y");
            Double pz = optDouble(el.getAsJsonObject(), "z");
            if (px == null || py == null || pz == null) {
                sendResponse(cmd.id(), false, "Malformed point at index " + i + ": need numeric x,y,z");
                return;
            }
            Float yaw = optFloat(el.getAsJsonObject(), "yaw");
            points.add(new RouteMemory.RoutePoint(px, py, pz, yaw == null ? 0f : yaw));
        }

        boolean existed = RouteMemory.has(name);
        RouteMemory.IngestResult result = RouteMemory.ingest(name, points, tickCount);
        StateCollector.requestRefresh();

        JsonObject data = new JsonObject();
        data.addProperty("saved", result.ok());
        data.addProperty("name", name);
        data.addProperty("received", points.size());
        data.addProperty("overwritten", existed);
        if (result.ok()) {
            data.addProperty("points", result.route().points().size());
            data.addProperty("source", "backend");
            sendResponse(cmd.id(), true, data);
        } else {
            data.addProperty("error", result.error().name().toLowerCase(java.util.Locale.ROOT));
            data.addProperty("reason", result.detail());
            sendResponse(cmd.id(), false, data);
        }
    }

    /** 可空数值参数（缺省/JSON null 都返回 null → 走回退）。 */
    private static Double optDouble(JsonObject args, String key) {
        if (args == null || !args.has(key) || args.get(key).isJsonNull()) {
            return null;
        }
        try {
            return args.get(key).getAsDouble();
        } catch (RuntimeException e) {
            return null;
        }
    }

    private static Float optFloat(JsonObject args, String key) {
        Double v = optDouble(args, key);
        return v == null ? null : v.floatValue();
    }

    /** 挂机：寻路到站点 → 到点后驻守（停止寻路、待在半径内，被撞出去自己走回）。 */
    private void handleGoAfk(WireServer.WireCommand cmd) {
        var mc = Minecraft.getInstance();
        if (mc.player == null || mc.level == null) {
            sendResponse(cmd.id(), false, "No player");
            return;
        }
        JsonObject args = cmd.args();
        String name = args != null && args.has("name") ? args.get("name").getAsString().strip() : "";
        if (name.isEmpty()) {
            sendResponse(cmd.id(), false, "Need name");
            return;
        }
        StationMemory.Station station = StationMemory.get(name);
        if (station == null) {
            sendResponse(cmd.id(), false, "Unknown station: " + name);
            return;
        }
        boolean keepalive = args != null && args.has("keepalive") && args.get("keepalive").getAsBoolean();

        followTargetName = null;
        MovementSystem.stop();
        // guard 也是"占住身体"的驻守：go_afk 接管后旧守卫点必须让位，否则两个驻守会互相抢控制
        guardPoint = null;
        AFK.begin(station, keepalive);
        afkReturnCooldown = 0;
        afkReturnAttempts = 0;
        double distance = station.distanceTo(mc.player.getX(), mc.player.getY(), mc.player.getZ());
        Pathfinder.navigateTo(cmd.id(), station.x(), station.y(), station.z());
        setTaskState("afk", "running", name,
                keepalive ? "heading to station (keepalive)" : "heading to station", 0.1);
        StateCollector.requestRefresh();

        JsonObject data = stationJson(station);
        data.addProperty("going", true);
        data.addProperty("keepalive", keepalive);
        data.addProperty("distance", round1(distance));
        sendResponse(cmd.id(), true, data);
    }

    /** 结束驻守（用户"别挂了/回来吧"）。 */
    private void handleAfkStop(WireServer.WireCommand cmd) {
        boolean wasActive = AFK.isActive();
        String name = AFK.stationName();
        if (wasActive) {
            AFK.stop();
            afkReturnAttempts = 0;
            afkReturnCooldown = 0;
            Pathfinder.stop();
            setTaskState("afk", "cancelled", name, "afk stopped", 0.0);
            StateCollector.requestRefresh();
        }
        JsonObject data = new JsonObject();
        data.addProperty("stopped", true);
        data.addProperty("was_active", wasActive);
        data.addProperty("station", name);
        sendResponse(cmd.id(), true, data);
    }

    /** 开始记录**实际走过的路**（抽稀：每 N 格或方向变化超阈值才记一点）。 */
    private void handleRouteRecordStart(WireServer.WireCommand cmd) {
        var mc = Minecraft.getInstance();
        if (mc.player == null) {
            sendResponse(cmd.id(), false, "No player");
            return;
        }
        JsonObject args = cmd.args();
        String name = args != null && args.has("name") ? args.get("name").getAsString().strip() : "";
        if (name.isEmpty()) {
            sendResponse(cmd.id(), false, "Need name");
            return;
        }
        double minStep = args != null && args.has("min_step")
                ? args.get("min_step").getAsDouble() : RouteMemory.DEFAULT_MIN_STEP;
        double turnDeg = args != null && args.has("turn_degrees")
                ? args.get("turn_degrees").getAsDouble() : RouteMemory.MIN_TURN_DEGREES;

        routeRecorder = new RouteMemory.Recorder(minStep, turnDeg, RouteMemory.MIN_MOVE);
        routeRecordName = name;
        routeRecorder.offer(mc.player.getX(), mc.player.getY(), mc.player.getZ(), mc.player.getYRot());
        setTaskState("route_record", "running", name, "recording", 0.0);
        StateCollector.requestRefresh();

        JsonObject data = new JsonObject();
        data.addProperty("recording", true);
        data.addProperty("name", name);
        data.addProperty("min_step", minStep);
        data.addProperty("turn_degrees", turnDeg);
        sendResponse(cmd.id(), true, data);
    }

    /** 结束记录并落库（终点强制精准补点）；同名覆盖。 */
    private void handleRouteRecordStop(WireServer.WireCommand cmd) {
        var mc = Minecraft.getInstance();
        if (mc.player == null) {
            sendResponse(cmd.id(), false, "No player");
            return;
        }
        if (routeRecorder == null) {
            sendResponse(cmd.id(), false, "Not recording");
            return;
        }
        routeRecorder.offer(mc.player.getX(), mc.player.getY(), mc.player.getZ(), mc.player.getYRot());
        List<RouteMemory.RoutePoint> points = routeRecorder.points();
        RouteMemory.RoutePoint tail = new RouteMemory.RoutePoint(
                mc.player.getX(), mc.player.getY(), mc.player.getZ(), mc.player.getYRot());
        if (points.isEmpty() || points.get(points.size() - 1).horizontalDistanceTo(tail) > 0.05) {
            points.add(tail);
        }
        String name = routeRecordName;
        routeRecorder = null;
        routeRecordName = "";
        RouteMemory.Route route = RouteMemory.save(name, points, tickCount);
        setTaskState("route_record", route == null ? "failed" : "done", name,
                route == null ? "no points recorded" : points.size() + " points",
                route == null ? 0.0 : 1.0);
        StateCollector.requestRefresh();

        JsonObject data = new JsonObject();
        data.addProperty("saved", route != null);
        data.addProperty("name", name);
        data.addProperty("points", points.size());
        // 点列不在这里重复发（最多 2000 点）：告诉后端"要用就来 route_export"
        data.addProperty("export_available", route != null);
        sendResponse(cmd.id(), route != null, data);
    }

    /** 逐点重放一条路径：每段都单独走一次原版寻路，因此洞穴再长也能一段段爬出去。 */
    private void handleRouteFollow(WireServer.WireCommand cmd) {
        var mc = Minecraft.getInstance();
        if (mc.player == null || mc.level == null) {
            sendResponse(cmd.id(), false, "No player");
            return;
        }
        JsonObject args = cmd.args();
        String name = args != null && args.has("name") ? args.get("name").getAsString().strip() : "";
        if (name.isEmpty()) {
            sendResponse(cmd.id(), false, "Need name");
            return;
        }
        RouteMemory.Route route = RouteMemory.get(name);
        if (route == null) {
            sendResponse(cmd.id(), false, "Unknown route: " + name);
            return;
        }
        double speed = args != null && args.has("speed") ? args.get("speed").getAsDouble() : RouteFollower.DEFAULT_SPEED;

        followTargetName = null;
        afkStopQuiet("route_follow");
        guardPoint = null;
        MovementSystem.stop();
        if (!ROUTE.begin(name, route.points(), speed)) {
            sendResponse(cmd.id(), false, "Route has no points: " + name);
            return;
        }
        RouteMemory.RoutePoint first = ROUTE.current();
        resetRouteStall();
        ROUTE.beginLeg();
        Pathfinder.navigateTo(cmd.id(), first.x(), first.y(), first.z());
        setTaskState("route", "running", name, "point 1/" + ROUTE.total(), ROUTE.progress());
        StateCollector.requestRefresh();

        JsonObject data = new JsonObject();
        data.addProperty("following", true);
        data.addProperty("name", name);
        data.addProperty("total", ROUTE.total());
        data.addProperty("index", 0);
        data.addProperty("speed", ROUTE.speed());
        sendResponse(cmd.id(), true, data);
    }

    /**
     * 路径几何导出：把一条路径的**完整点列原样**发给后端（不抽稀、不加工）。
     *
     * <p>为什么需要：`state.routes[]` 只有 name/点数/tick（每帧几十 KB 不能塞点列），
     * 于是路径跨模组重启回灌时后端推不出几何、只能记 `no_geometry`。这里给一条按需出口：
     * 后端要回灌某条路径时先 `route_export {name}` 取点列，再用 `remember_route` 灌回来。
     *
     * <p>失败如实：路径不存在 → `unknown_route`；点列为空 → `no_geometry`。
     */
    private void handleRouteExport(WireServer.WireCommand cmd) {
        JsonObject args = cmd.args();
        String name = args != null && args.has("name") ? args.get("name").getAsString().strip() : "";
        if (name.isEmpty()) {
            JsonObject data = new JsonObject();
            data.addProperty("ok", false);
            data.addProperty("reason", "need_name");
            sendResponse(cmd.id(), false, data, "need_name");
            return;
        }
        RouteMemory.Route route = RouteMemory.get(name);
        if (route == null) {
            JsonObject data = new JsonObject();
            data.addProperty("ok", false);
            data.addProperty("name", name);
            data.addProperty("reason", "unknown_route");
            sendResponse(cmd.id(), false, data, "unknown_route");
            return;
        }
        if (route.points() == null || route.points().isEmpty()) {
            JsonObject data = new JsonObject();
            data.addProperty("ok", false);
            data.addProperty("name", name);
            data.addProperty("reason", "no_geometry");
            sendResponse(cmd.id(), false, data, "no_geometry");
            return;
        }
        JsonObject data = new JsonObject();
        data.addProperty("ok", true);
        data.addProperty("name", route.name());
        JsonArray points = new JsonArray();
        for (RouteMemory.RoutePoint p : route.points()) {
            JsonObject o = new JsonObject();
            o.addProperty("x", p.x());
            o.addProperty("y", p.y());
            o.addProperty("z", p.z());
            o.addProperty("yaw", p.yaw());
            points.add(o);
        }
        data.add("points", points);
        data.addProperty("count", route.points().size());
        data.addProperty("saved_at_tick", route.savedAtTick());
        // 单帧一次发完（最多 2000 点）；线协议一行一条 JSON，这里顺手量一下真实字节数供排障。
        LCUMod.LOGGER.info("[Route] export '{}' points={} payloadChars={}",
                route.name(), route.points().size(), data.toString().length());
        sendResponse(cmd.id(), true, data);
    }

    /**
     * 驻守 tick：到站点后停下并钉在半径内；被撞出去走回；keepalive 只动头不动脚。
     *
     * <p>注意与 {@code tickStuckWatchdog} 的分工：**驻守期间"不动"是意图，不是卡住**，
     * 因此看门狗对驻守/重放整体让路，由本方法与 {@link #tickRoute} 各自做带上下文的判定。
     */
    private void tickAfk(Minecraft mc) {
        if (!AFK.isActive()) {
            return;
        }
        if (!InputIsolation.isAiControlled()) {
            setTaskState("afk", "cancelled", AFK.stationName(), "player takeover", 0.0);
            afkStopQuiet("player takeover");
            return;
        }
        StationMemory.Station station = AFK.station();
        if (station == null) {
            AFK.stop();
            return;
        }
        boolean returning = AFK.tick(mc.player.getX(), mc.player.getZ()) == AfkController.Action.RETURN;

        if (returning) {
            if (Pathfinder.isNavigating()) {
                afkReturnAttempts = 0; // 正在走回，别打断
                return;
            }
            if (afkReturnCooldown-- > 0) {
                return;
            }
            afkReturnCooldown = 20;
            afkReturnAttempts++;
            if (afkReturnAttempts > AFK_MAX_RETURN_ATTEMPTS) {
                String why = Pathfinder.getLastFailureReason();
                setTaskState("afk", "failed", station.name(),
                        "cannot return to station: " + (why == null || why.isBlank() ? "no path" : why), 0.0);
                Pathfinder.stop();
                AFK.stop();
                StateCollector.requestRefresh();
                LCUMod.LOGGER.warn("[AFK] 回不去站点 {}，已诚实停止驻守", station.name());
                return;
            }
            Pathfinder.navigateTo("go_afk", station.x(), station.y(), station.z());
            setTaskState("afk", "running", station.name(),
                    "returning to station (drift " + round1(AFK.drift()) + "m)", 0.5);
            StateCollector.requestRefresh();
            return;
        }

        // 站定：第一次到达时停寻路；然后保持站点朝向 + 按需 keepalive（绝不写移动键）
        if (Pathfinder.isNavigating()) {
            Pathfinder.stop();
            afkReturnAttempts = 0;
            setTaskState("afk", "running", station.name(),
                    "holding station r=" + round1(station.radius()), 1.0);
            StateCollector.requestRefresh();
        }
        faceStation(mc, station);
        if (AFK.keepaliveDue()) {
            AFK.markPulse();
            mc.player.setYRot(mc.player.getYRot() + AFK.keepaliveYawDelta());
            mc.player.setXRot(Math.max(-90.0f,
                    Math.min(90.0f, mc.player.getXRot() + AFK.keepalivePitchDelta())));
            mc.player.swing(InteractionHand.MAIN_HAND, SwingAnimation.DEFAULT, true);
        }
    }

    /** 路径重放 tick：逐点推进；段内卡住/无路 → 重试，仍失败则诚实报"卡在第几个点"。 */
    private void tickRoute(Minecraft mc) {
        if (!ROUTE.isActive()) {
            return;
        }
        if (!InputIsolation.isAiControlled()) {
            setTaskState("route", "cancelled", ROUTE.name(), "player takeover", ROUTE.progress());
            routeStopQuiet("player takeover");
            return;
        }
        double px = mc.player.getX();
        double py = mc.player.getY();
        double pz = mc.player.getZ();

        if (ROUTE.advanceIfReached(px, py, pz, ROUTE_REACH_XZ, ROUTE_REACH_Y)) {
            resetRouteStall();
            if (ROUTE.isDone()) {
                Pathfinder.stop();
                setTaskState("route", "done", ROUTE.name(),
                        "reached end (" + ROUTE.total() + " points)", 1.0);
                StateCollector.requestRefresh();
                return;
            }
            RouteMemory.RoutePoint next = ROUTE.current();
            ROUTE.beginLeg();
            Pathfinder.navigateTo("route_follow", next.x(), next.y(), next.z());
            setTaskState("route", "running", ROUTE.name(),
                    "point " + (ROUTE.index() + 1) + "/" + ROUTE.total(), ROUTE.progress());
            StateCollector.requestRefresh();
            return;
        }

        ROUTE.tickLeg();

        // 段内无进展：与 tickStuckWatchdog 同口径（3 秒 / 0.6 格，按 speed 缩放），但独立计时
        if (Double.isNaN(routeStuckLastX)) {
            routeStuckLastX = px;
            routeStuckLastZ = pz;
            routeStuckTicks = 0;
        } else if (Math.hypot(px - routeStuckLastX, pz - routeStuckLastZ) > 0.6) {
            routeStuckLastX = px;
            routeStuckLastZ = pz;
            routeStuckTicks = 0;
        } else {
            routeStuckTicks++;
        }
        if (routeStuckTicks >= ROUTE.stallWindowTicks()) {
            routeLegFailed("no progress");
            return;
        }
        if (ROUTE.legTimedOut()) {
            routeLegFailed("leg timeout");
            return;
        }
        if (!Pathfinder.isNavigating() && ROUTE.legTicks() > 20) {
            routeLegFailed(Pathfinder.getLastFailureReason());
        }
    }

    /** 一段失败：可重试就重试同一点，否则钉住索引并诚实上报。 */
    private void routeLegFailed(String reason) {
        RouteMemory.RoutePoint target = ROUTE.current();
        if (ROUTE.canRetry()) {
            ROUTE.retryLeg();
            resetRouteStall();
            if (target != null) {
                Pathfinder.navigateTo("route_follow", target.x(), target.y(), target.z());
            }
            LCUMod.LOGGER.info("[Route] 第 {} 点重试 {}/{}：{}",
                    ROUTE.index() + 1, ROUTE.legAttempts(), ROUTE.maxLegAttempts(), reason);
            return;
        }
        Pathfinder.stop();
        ROUTE.fail(reason == null || reason.isBlank() ? "no progress" : reason);
        setTaskState("route", "failed", ROUTE.name(),
                "stuck at point " + (ROUTE.failIndex() + 1) + "/" + ROUTE.total()
                        + ": " + ROUTE.failureReason(), ROUTE.progress());
        StateCollector.requestRefresh();
        LCUMod.LOGGER.warn("[Route] 卡在第 {} 个点（共 {}）：{}",
                ROUTE.failIndex() + 1, ROUTE.total(), ROUTE.failureReason());
    }

    /** 让路：停驻守（不覆盖任务状态，供新任务/stop_all 复用）。 */
    private static void afkStopQuiet(String reason) {
        if (!AFK.isActive()) {
            return;
        }
        String name = AFK.stationName();
        AFK.stop();
        afkReturnAttempts = 0;
        afkReturnCooldown = 0;
        LCUMod.LOGGER.info("[AFK] 停止驻守 {}（{}）", name, reason);
    }

    /** 让路：停路径重放（中断 ≠ 失败，原因保留在状态里）。 */
    private static void routeStopQuiet(String reason) {
        if (!ROUTE.isActive()) {
            return;
        }
        String name = ROUTE.name();
        ROUTE.abort(reason);
        resetRouteStall();
        LCUMod.LOGGER.info("[Route] 中断重放 {}（{}）", name, reason);
    }

    private static void resetRouteStall() {
        routeStuckLastX = Double.NaN;
        routeStuckLastZ = Double.NaN;
        routeStuckTicks = 0;
    }

    /** 把视角平滑地对准站点记录的朝向（限速 ±10°/±6° per tick，与原版寻路同口径）。 */
    private void faceStation(Minecraft mc, StationMemory.Station station) {
        float yawDiff = wrapDegrees(station.yaw() - mc.player.getYRot());
        float pitchDiff = station.pitch() - mc.player.getXRot();
        mc.player.setYRot(mc.player.getYRot() + Math.max(-10.0f, Math.min(10.0f, yawDiff)));
        mc.player.setXRot(Math.max(-90.0f,
                Math.min(90.0f, mc.player.getXRot() + Math.max(-6.0f, Math.min(6.0f, pitchDiff)))));
    }

    private static float wrapDegrees(float deg) {
        float d = deg % 360.0f;
        if (d > 180.0f) d -= 360.0f;
        if (d < -180.0f) d += 360.0f;
        return d;
    }

    private static JsonObject stationJson(StationMemory.Station s) {
        JsonObject o = new JsonObject();
        o.addProperty("name", s.name());
        o.addProperty("kind", s.kind());
        o.addProperty("x", s.x());
        o.addProperty("y", s.y());
        o.addProperty("z", s.z());
        o.addProperty("yaw", s.yaw());
        o.addProperty("pitch", s.pitch());
        o.addProperty("radius", s.radius());
        return o;
    }

    /** 驻守状态（state_update.afk）：station / since_ticks / at_target / drift。 */
    public static JsonObject describeAfk() {
        JsonObject o = new JsonObject();
        o.addProperty("active", AFK.isActive());
        o.addProperty("station", AFK.stationName());
        o.addProperty("since_ticks", AFK.sinceTicks());
        o.addProperty("at_target", AFK.atTarget());
        o.addProperty("drift", round1(AFK.drift()));
        o.addProperty("returning", AFK.returning());
        o.addProperty("keepalive", AFK.keepaliveEnabled());
        return o;
    }

    /** 路径状态（state_update.route）：name / index / total / active（+ 记录与失败信息）。 */
    public static JsonObject describeRoute() {
        JsonObject o = new JsonObject();
        o.addProperty("active", ROUTE.isActive());
        o.addProperty("name", ROUTE.name());
        o.addProperty("index", ROUTE.index());
        o.addProperty("total", ROUTE.total());
        o.addProperty("status", ROUTE.status().name().toLowerCase(java.util.Locale.ROOT));
        o.addProperty("failed_index", ROUTE.failIndex());
        o.addProperty("failure", ROUTE.failureReason());
        o.addProperty("recording", routeRecorder != null);
        o.addProperty("recorded_points", routeRecorder == null ? 0 : routeRecorder.size());
        return o;
    }

    /** 已记忆的站点（后端"有哪些挂机点"）。 */
    public static JsonArray describeStations() {
        JsonArray arr = new JsonArray();
        for (StationMemory.Station s : StationMemory.all()) {
            JsonObject o = stationJson(s);
            o.addProperty("saved_at_tick", s.savedAtTick());
            arr.add(o);
        }
        return arr;
    }

    /** 已记忆的路径（后端"有哪些线可以重放"）。 */
    public static JsonArray describeRoutes() {
        JsonArray arr = new JsonArray();
        for (RouteMemory.Route r : RouteMemory.all()) {
            JsonObject o = new JsonObject();
            o.addProperty("name", r.name());
            o.addProperty("points", r.points().size());
            o.addProperty("saved_at_tick", r.savedAtTick());
            arr.add(o);
        }
        return arr;
    }

    private static double round1(double v) {
        return Math.round(v * 10.0) / 10.0;
    }

    /** 两段式收尾：go_to_block 到达 → 记忆/收尾；sleep 到达 → 自动点床。 */
    private void tickPendingBlockTasks(Minecraft mc) {
        if (mc.player == null || mc.level == null) {
            return;
        }
        if (pendingGoTarget != null) {
            pendingGoTicks++;
            double distance = Math.sqrt(mc.player.blockPosition().distSqr(pendingGoTarget));
            boolean arrived = distance <= 3.0;
            boolean navigationEnded = !Pathfinder.isNavigating() && pendingGoTicks > 20;
            if (arrived || navigationEnded) {
                if (pendingGoRemember) {
                    PoiMemory.remember(pendingGoTarget, pendingGoBlock, "waypoint",
                            "found:" + pendingGoBlock, tickCount);
                }
                setTaskState("travel", arrived ? "done" : "failed", pendingGoBlock,
                        (arrived ? "arrived " : "navigation ended ") + String.format("%.1f", distance) + "m away",
                        arrived ? 1.0 : 0.0);
                pendingGoTarget = null;
                pendingGoBlock = "";
                pendingGoTicks = 0;
                StateCollector.requestRefresh();
            } else if (pendingGoTicks > 20 * 60) {
                setTaskState("travel", "failed", pendingGoBlock, "timeout after 60s", 0.0);
                Pathfinder.stop();
                pendingGoTarget = null;
                pendingGoBlock = "";
                pendingGoTicks = 0;
            }
        }

        if (pendingSleep && pendingSleepBed != null) {
            pendingSleepTicks++;
            double distance = Math.sqrt(mc.player.blockPosition().distSqr(pendingSleepBed));
            if (distance <= BlockFinder.REACH_DISTANCE) {
                useBed(mc, pendingSleepBed);
                setTaskState("sleep", "done", posKey(pendingSleepBed), "sleeping", 1.0);
                pendingSleep = false;
                pendingSleepBed = null;
            } else if (!Pathfinder.isNavigating() && pendingSleepTicks > 20) {
                setTaskState("sleep", "failed", posKey(pendingSleepBed), "cannot reach bed", 0.0);
                pendingSleep = false;
                pendingSleepBed = null;
            } else if (pendingSleepTicks > 20 * 90) {
                setTaskState("sleep", "failed", posKey(pendingSleepBed), "timeout walking to bed", 0.0);
                Pathfinder.stop();
                pendingSleep = false;
                pendingSleepBed = null;
            }
        }
    }

    private static String posKey(BlockPos pos) {
        return pos.getX() + "," + pos.getY() + "," + pos.getZ();
    }

    /** 盯着床并**通用右键**；是否允许睡由服务器裁决（本地不判断"床=睡觉"的语义）。 */
    private void useBed(Minecraft mc, BlockPos bedPos) {
        var player = mc.player;
        if (player == null || mc.gameMode == null) {
            return;
        }
        ContainerController.aimAt(mc, Vec3.atCenterOf(bedPos));
        ContainerController.genericRightClick(mc, 4);
        StateCollector.requestRefresh();
    }

    /** 供 HUD / 窗口标题显示"现在在干什么"。 */
    public static String getActiveTaskKind() {
        return activeTaskKind;
    }

    /** 正在走去睡 / 已在床（HUD 用）。 */
    public static boolean isSleepingPending() {
        return pendingSleep;
    }

    /**
     * 换成热栏里"最强武器"（用户要求：被打了要会切物品栏、先用手上的东西还击）。
     * 判定**只用物品 id 字符串**（`_sword` > `_trident/_mace` > `_axe`，材质 netherite>diamond>iron>stone>gold>wood），
     * 因此不依赖可能变化的物品类 API；非武器（方块/食物）返回 0 不参与。
     */
    public static String equipBestWeapon(Minecraft mc) {
        if (mc == null || mc.player == null) {
            return null;
        }
        var inv = mc.player.getInventory();
        int bestSlot = -1;
        int bestScore = 0;
        for (int i = 0; i < 9; i++) {
            var stack = inv.getItem(i);
            if (stack.isEmpty()) {
                continue;
            }
            int score = weaponScore(stack.getItem().toString());
            if (score > bestScore) {
                bestScore = score;
                bestSlot = i;
            }
        }
        if (bestSlot < 0) {
            return null;
        }
        inv.setSelectedSlot(bestSlot);
        return inv.getItem(bestSlot).getItem().toString();
    }

    private static int weaponScore(String itemId) {
        String id = itemId == null ? "" : itemId.toLowerCase();
        int kind;
        if (id.endsWith("_sword")) {
            kind = 40;
        } else if (id.endsWith("_trident") || id.endsWith("_mace")) {
            kind = 35;
        } else if (id.endsWith("_axe")) {
            kind = 30;
        } else {
            return 0;
        }
        int tier;
        if (id.contains("netherite")) {
            tier = 6;
        } else if (id.contains("diamond")) {
            tier = 5;
        } else if (id.contains("iron")) {
            tier = 4;
        } else if (id.contains("stone")) {
            tier = 3;
        } else if (id.contains("gold")) {
            tier = 2;
        } else {
            tier = 1;
        }
        return kind + tier;
    }

    /** 绝不自吃的有害食物（参考项目黑名单：腐肉/蜘蛛眼/毒马铃薯/河豚/紫颂果）。 */
    private static final java.util.Set<String> REFLEX_HARMFUL_FOOD = java.util.Set.of(
            "minecraft:rotten_flesh", "minecraft:spider_eye", "minecraft:poisonous_potato",
            "minecraft:pufferfish", "minecraft:chorus_fruit");

    /** 生食：能吃但收益差，只在没有更好选择时才吃（注意用**精确 id**，别用 "chicken" 子串——会误伤熟鸡肉）。 */
    private static final java.util.Set<String> REFLEX_RAW_FOOD = java.util.Set.of(
            "minecraft:chicken", "minecraft:beef", "minecraft:porkchop", "minecraft:mutton",
            "minecraft:rabbit", "minecraft:cod", "minecraft:salmon", "minecraft:tropical_fish");

    /** 守卫：在给定点（默认当前脚下）的水平半径内驻守；越界自动走回。 */
    private void handleGuard(WireServer.WireCommand cmd) {
        var mc = Minecraft.getInstance();
        if (mc.player == null || mc.level == null) {
            sendResponse(cmd.id(), false, "No player");
            return;
        }
        JsonObject args = cmd.args();
        boolean enable = args == null || !args.has("enabled") || args.get("enabled").getAsBoolean();
        if (!enable) {
            guardPoint = null;
            setTaskState("guard", "cancelled", "", "guard off", 0.0);
            sendResponse(cmd.id(), true, "Guard off");
            return;
        }
        BlockPos base = mc.player.blockPosition();
        int x = args != null && args.has("x") ? args.get("x").getAsInt() : base.getX();
        int y = args != null && args.has("y") ? args.get("y").getAsInt() : base.getY();
        int z = args != null && args.has("z") ? args.get("z").getAsInt() : base.getZ();
        int radius = args != null && args.has("radius")
                ? Math.max(4, Math.min(args.get("radius").getAsInt(), 128)) : 32;
        guardPoint = new BlockPos(x, y, z);
        guardRadius = radius;
        guardCheckCooldown = 0;
        followTargetName = null;
        var state = mc.level.getBlockState(guardPoint);
        PoiMemory.remember(guardPoint, BuiltInRegistries.BLOCK.getKey(state.getBlock()).toString(),
                "guard", "guard post", tickCount);
        setTaskState("guard", "running", posKey(guardPoint), "guarding r=" + radius + "m", 1.0);
        StateCollector.requestRefresh();
        sendResponse(cmd.id(), true, "Guarding " + posKey(guardPoint) + " r=" + radius);
    }

    /** 原版斜杠命令直通（`/locate structure minecraft:village` 这类"查世界"能力靠它）。 */
    private void handleRunCommand(WireServer.WireCommand cmd) {
        var mc = Minecraft.getInstance();
        if (mc.player == null || mc.player.connection == null) {
            sendResponse(cmd.id(), false, "No connection");
            return;
        }
        JsonObject args = cmd.args();
        String command = args != null && args.has("command") ? args.get("command").getAsString().strip() : "";
        if (command.startsWith("/")) {
            command = command.substring(1);
        }
        if (command.isBlank()) {
            sendResponse(cmd.id(), false, "Need command");
            return;
        }
        // 硬黑名单兜底（用户："有些指令绝对不可以用：kill、kick 之类"）：大小写/前导斜杠/多余空白/
        // 命名空间前缀都在 normalize 里处理；命中即拒绝，**绝不发出去**（后端白名单被绕过也拦得住）。
        CommandBlacklist.Verdict verdict = CommandBlacklist.check(command);
        if (verdict.blocked()) {
            JsonObject data = new JsonObject();
            data.addProperty("blocked", true);
            data.addProperty("reason", CommandBlacklist.REASON);
            data.addProperty("command", verdict.family());
            LCUMod.LOGGER.warn("[Safety] run_command 被硬黑名单拒绝：{}", verdict.family());
            sendResponse(cmd.id(), false, data, CommandBlacklist.REASON + ": " + verdict.family());
            return;
        }
        followTargetName = null;
        mc.player.connection.sendCommand(command);
        sendResponse(cmd.id(), true, "Ran /" + command);
    }

    /**
     * 控件级点击：在**当前已打开的**界面里找文本匹配 {@code match} 的控件，直接触发它的
     * {@code onPress} / {@code mouseClicked}（{@link com.lcu.lcumod.client.ScreenButtonClick}）。
     *
     * <p>不移动真鼠标、不发鼠标事件、不开界面；界面来自**服务端**弹的那个（我们从不主动开界面）。
     * 失败路径都是**如实**的：{@code no_screen}（当前没界面）、{@code user_screen_open}（玩家自己的
     * 界面 → 让路）、{@code no_match}（附界面上可点控件的文字，便于后端调识别规则）。
     */
    private void handleClickScreenButton(WireServer.WireCommand cmd) {
        JsonObject args = cmd.args();
        String query = args != null && args.has("match") ? args.get("match").getAsString() : "";
        var mc = Minecraft.getInstance();
        com.lcu.lcumod.client.ScreenButtonClick.Result result =
                com.lcu.lcumod.client.ScreenButtonClick.click(mc, query);
        JsonObject data = new JsonObject();
        data.addProperty("status", result.status());
        data.addProperty("screen", result.screen());
        data.addProperty("screen_title", result.screenTitle());
        data.addProperty("match", query);
        if (result.hit() != null) {
            var hit = result.hit();
            JsonObject h = new JsonObject();
            h.addProperty("type", hit.type());
            h.addProperty("text", hit.label());
            h.addProperty("x", hit.x());
            h.addProperty("y", hit.y());
            h.addProperty("width", hit.width());
            h.addProperty("height", hit.height());
            data.add("clicked", h);
        }
        JsonArray candidates = new JsonArray();
        for (String c : result.candidates()) {
            candidates.add(c);
        }
        data.add("candidates", candidates);

        boolean ok = "ok".equals(result.status());
        if (ok) {
            StateCollector.requestRefresh();
            LCUMod.LOGGER.info("[Screen] clicked '{}' in {} (match='{}')",
                    result.hit() == null ? "" : result.hit().label(), result.screen(), query);
        } else {
            LCUMod.LOGGER.warn("[Screen] click_screen_button={} (screen={}, match='{}')",
                    result.status(), result.screen(), query);
        }
        sendResponse(cmd.id(), ok, data, ok ? null : result.status());
    }

    /**
     * 声明一个区域：{@code define_zone {name, kind, x, y, z, radius?, box?, protect[]}}。
     *
     * <p>区域是**语义 + 硬保护**（用户："他无法识别这里是不是农场" → 不能靠提示词，得硬拦）。
     * {@code radius} = 球（三维欧氏）；{@code box} = 轴对齐长方体（端点含）；两者都给按 box 判。
     */
    private void handleDefineZone(WireServer.WireCommand cmd) {
        JsonObject args = cmd.args();
        if (args == null || !args.has("name")) {
            sendResponse(cmd.id(), false, "Need name");
            return;
        }
        String name = args.get("name").getAsString();
        String kind = args.has("kind") ? args.get("kind").getAsString() : "";
        var mc = Minecraft.getInstance();
        BlockPos base = mc.player != null ? mc.player.blockPosition() : BlockPos.ZERO;
        double x = args.has("x") ? args.get("x").getAsDouble() : base.getX() + 0.5;
        double y = args.has("y") ? args.get("y").getAsDouble() : base.getY();
        double z = args.has("z") ? args.get("z").getAsDouble() : base.getZ() + 0.5;
        Double radius = args.has("radius") && !args.get("radius").isJsonNull()
                ? args.get("radius").getAsDouble() : null;
        ZoneGuard.Box box = null;
        if (args.has("box") && args.get("box").isJsonObject()) {
            JsonObject b = args.getAsJsonObject("box");
            box = new ZoneGuard.Box(
                    b.has("min_x") ? b.get("min_x").getAsDouble() : 0.0,
                    b.has("min_y") ? b.get("min_y").getAsDouble() : 0.0,
                    b.has("min_z") ? b.get("min_z").getAsDouble() : 0.0,
                    b.has("max_x") ? b.get("max_x").getAsDouble() : 0.0,
                    b.has("max_y") ? b.get("max_y").getAsDouble() : 0.0,
                    b.has("max_z") ? b.get("max_z").getAsDouble() : 0.0);
        }
        List<String> protect = new ArrayList<>();
        if (args.has("protect") && args.get("protect").isJsonArray()) {
            for (var el : args.getAsJsonArray("protect")) {
                protect.add(el.getAsString());
            }
        }
        ZoneGuard.DefineResult defined = ZoneGuard.define(name, kind, x, y, z, radius, box, protect);
        if (!defined.ok()) {
            JsonObject data = new JsonObject();
            data.addProperty("ok", false);
            data.addProperty("reason", defined.reason());
            sendResponse(cmd.id(), false, data, defined.reason());
            return;
        }
        StateCollector.requestRefresh();
        ZoneGuard.Zone zone = defined.zone();
        LCUMod.LOGGER.info("[Zone] define '{}' kind={} shape={} protect={}",
                zone.name(), zone.kind(), zone.shape(), zone.protect());
        JsonObject data = new JsonObject();
        data.addProperty("ok", true);
        data.add("zone", zoneJson(zone));
        sendResponse(cmd.id(), true, data);
    }

    /** 删除一个区域（不存在则如实回 {@code unknown_zone}）。 */
    private void handleClearZone(WireServer.WireCommand cmd) {
        JsonObject args = cmd.args();
        String name = args != null && args.has("name") ? args.get("name").getAsString() : "";
        if (ZoneGuard.clear(name)) {
            StateCollector.requestRefresh();
            JsonObject data = new JsonObject();
            data.addProperty("ok", true);
            data.addProperty("cleared", name.strip());
            data.addProperty("zones", ZoneGuard.size());
            sendResponse(cmd.id(), true, data);
            return;
        }
        JsonObject data = new JsonObject();
        data.addProperty("ok", false);
        data.addProperty("reason", "unknown_zone");
        sendResponse(cmd.id(), false, data, "unknown_zone");
    }

    /** 一个区域 → JSON（状态与命令回执共用同一形状）。 */
    private static JsonObject zoneJson(ZoneGuard.Zone zone) {
        JsonObject o = new JsonObject();
        o.addProperty("name", zone.name());
        o.addProperty("kind", zone.kind());
        JsonArray protect = new JsonArray();
        for (String p : zone.protect()) {
            protect.add(p);
        }
        o.add("protect", protect);
        JsonObject center = new JsonObject();
        center.addProperty("x", zone.x());
        center.addProperty("y", zone.y());
        center.addProperty("z", zone.z());
        o.add("center", center);
        o.addProperty("shape", zone.shape());
        if (zone.radius() != null) {
            o.addProperty("radius", zone.radius());
        } else {
            o.add("radius", com.google.gson.JsonNull.INSTANCE);
        }
        if (zone.box() != null) {
            ZoneGuard.Box b = zone.box();
            JsonObject box = new JsonObject();
            box.addProperty("min_x", b.minX());
            box.addProperty("min_y", b.minY());
            box.addProperty("min_z", b.minZ());
            box.addProperty("max_x", b.maxX());
            box.addProperty("max_y", b.maxY());
            box.addProperty("max_z", b.maxZ());
            o.add("box", box);
        } else {
            o.add("box", com.google.gson.JsonNull.INSTANCE);
        }
        return o;
    }

    /** 所有区域（进 state 的 {@code zones[]}）。 */
    public static JsonArray describeZones() {
        JsonArray arr = new JsonArray();
        for (ZoneGuard.Zone zone : ZoneGuard.all()) {
            arr.add(zoneJson(zone));
        }
        return arr;
    }

    /** 驻守 tick：出了半径就走回去（只补"回家"意图，不抢战斗反射/寻路的控制权）。 */
    private void tickGuard(Minecraft mc) {
        if (guardPoint == null) {
            return;
        }
        double dx = mc.player.getX() - (guardPoint.getX() + 0.5);
        double dz = mc.player.getZ() - (guardPoint.getZ() + 0.5);
        if (Math.sqrt(dx * dx + dz * dz) <= guardRadius) {
            guardCheckCooldown = 0;
            return;
        }
        if (guardCheckCooldown-- > 0) {
            return;
        }
        guardCheckCooldown = 20;
        Pathfinder.navigateTo("guard", guardPoint.getX(), guardPoint.getY(), guardPoint.getZ());
        setTaskState("guard", "running", posKey(guardPoint), "returning to post", 0.5);
    }

    /** 路径记录采样：每 tick 喂一次真实位姿（是否记点由 {@link RouteMemory.Recorder} 内部抽稀决定）。 */
    private void tickRouteRecording(Minecraft mc) {
        if (routeRecorder == null || mc.player == null) {
            return;
        }
        routeRecorder.offer(mc.player.getX(), mc.player.getY(), mc.player.getZ(), mc.player.getYRot());
    }

    /**
     * 卡住看门狗（用户实机："追进水里就站住不动"、"有些情况下莫名其妙站在原地不动"）。
     *
     * 这是**通用**机制、不是特判：只要"当前有该走完全程的任务"，3 秒内水平位移 <0.6 格，
     * 就停止寻路、把任务诚实标成 failed 并附上 `Pathfinder` 的失败原因，随后由后端（LLM）
     * 重新规划——本地层不硬撑、也不假装还在推进。
     *
     * <p>与驻守/重放的边界：驻守期间"不动"是**意图**（用户要的就是站桩挂机），重放有自己的
     * 段级判定（带"卡在第几个点"的上下文）—— 因此这两者在此整体让路，绝不互相打架。
     */
    private void tickStuckWatchdog(Minecraft mc) {
        if (AFK.isActive() || ROUTE.isActive()) {
            stuckTicks = 0;
            stuckLastX = Double.NaN;
            return;
        }
        boolean shouldMove = Pathfinder.isNavigating() || guardPoint != null
                || pendingGoTarget != null || pendingSleep;
        if (!shouldMove) {
            stuckTicks = 0;
            stuckLastX = Double.NaN;
            return;
        }
        if (stuckCooldown > 0) {
            stuckCooldown--;
            return;
        }
        if (Double.isNaN(stuckLastX)) {
            stuckLastX = mc.player.getX();
            stuckLastZ = mc.player.getZ();
            stuckTicks = 0;
            return;
        }

        double moved = Math.sqrt(Math.pow(mc.player.getX() - stuckLastX, 2)
                + Math.pow(mc.player.getZ() - stuckLastZ, 2));
        if (moved > 0.6) {
            stuckTicks = 0;
            stuckLastX = mc.player.getX();
            stuckLastZ = mc.player.getZ();
            return;
        }

        stuckTicks++;
        if (stuckTicks < 60) {
            return; // 3 秒容忍（含正常转身/挖方块的时间）
        }

        String reason = Pathfinder.getLastFailureReason();
        String stuckReason = reason == null || reason.isBlank()
                ? "no progress; " + Pathfinder.NO_RESCUE_POLICY
                : reason;
        Pathfinder.stop();
        setTaskState(activeTaskKind == null || activeTaskKind.isBlank() ? "move" : activeTaskKind,
                "failed", activeTaskTarget, "stuck: " + stuckReason, 0.0);
        StateCollector.requestRefresh();
        LCUMod.LOGGER.warn("[Stuck] 3s 无位移，已停止寻路并上报（不搭方块自救）：{}", stuckReason);
        stuckTicks = 0;
        stuckLastX = Double.NaN;
        stuckCooldown = 20 * 5;
    }

    /**
     * P0 饥饿反射：饿到一定程度**自己找热栏里的食物吃**（用户实机反馈"它也不会吃东西"）。
     *
     * 阈值与选食物口径来自参考实现（scout 取证）：
     * - `mineflayer-auto-eat`：`minHunger=15` / `minHealth=14`，按 `foodPoints` 选最佳，且带 bannedFood 黑名单；
     * - `mindcraft`：`startAt:14`；`minecraft-agent`：吃完靠"物品数减少"确认。
     * 本实现：食物按 `FoodProperties.nutrition()/saturation()` 择优，**有害食物永不自吃**，
     * 生食仅作兜底；复用既有吃食物状态机（合成 req id，后端不会误判命令完成）。
     * 后端若要用自己的节奏控制进食，可 `reflex_override {name:"eat"}` 临时关掉本反射。
     */
    private void tickHungerReflex(Minecraft mc) {
        if (eatReflexCooldown > 0) {
            eatReflexCooldown--;
            return;
        }
        if (EAT.isActive()) {
            return; // 后端或上一次反射正在吃，交给进食状态机（幂等复用）
        }
        if (ReflexGate.isSuppressed("eat")) {
            return;
        }
        int food = mc.player.getFoodData().getFoodLevel();
        float health = mc.player.getHealth();
        boolean hungry = food <= HUNGER_REFLEX_FOOD;
        boolean hurt = health <= 14.0f && food < 20;
        if (!hungry && !hurt) {
            return;
        }

        int slot = bestFoodSlot(mc);
        if (slot < 0) {
            return;
        }

        var stack = mc.player.getInventory().getItem(slot);
        boolean started = EAT.begin(REFLEX_EAT_REQ_ID, true, slot, food,
                mc.player.getFoodData().getSaturationLevel(), stack.getCount());
        if (!started) {
            return;
        }
        pendingEatSlot = slot;
        pendingEatPrimary = null; // 反射不吃后端进度
        mc.player.getInventory().setSelectedSlot(slot); // 选中，等 2 tick 生效后再按住 use
        setTaskState("eat", "running", "food", "reflex: hungry", 0.05);
        eatReflexCooldown = 20 * 10;
        LCUMod.LOGGER.info("[Reflex] Auto-eat: food={} health={} slot={} item={}",
                food, health, slot, stack.getItem());
    }

    /** 热栏里最该吃的那一格：先排除有害食物，按营养值优先、饱和度为次；生食只在没别的可吃时兜底。 */
    private int bestFoodSlot(Minecraft mc) {
        var inv = mc.player.getInventory();
        int bestPreferred = -1;
        int bestRaw = -1;
        int bestPreferredNutrition = -1;
        float bestPreferredSaturation = -1.0f;
        int bestRawNutrition = -1;
        float bestRawSaturation = -1.0f;

        for (int i = 0; i < 9; i++) {
            var stack = inv.getItem(i);
            if (stack.isEmpty() || !stack.has(DataComponents.FOOD)) {
                continue;
            }
            var props = stack.get(DataComponents.FOOD);
            if (props == null) {
                continue;
            }
            String id = stack.getItem().toString().toLowerCase();
            if (REFLEX_HARMFUL_FOOD.contains(id)) {
                continue;
            }
            int nutrition = props.nutrition();
            float saturation = props.saturation();
            if (REFLEX_RAW_FOOD.contains(id)) {
                if (nutrition > bestRawNutrition
                        || (nutrition == bestRawNutrition && saturation > bestRawSaturation)) {
                    bestRawNutrition = nutrition;
                    bestRawSaturation = saturation;
                    bestRaw = i;
                }
            } else if (nutrition > bestPreferredNutrition
                    || (nutrition == bestPreferredNutrition && saturation > bestPreferredSaturation)) {
                bestPreferredNutrition = nutrition;
                bestPreferredSaturation = saturation;
                bestPreferred = i;
            }
        }
        return bestPreferred >= 0 ? bestPreferred : bestRaw;
    }

    /**
     * eat：找热栏食物 → 选中该槽（等选中生效）→ 按住 use 直到食物生效 → 松开。
     * 判定：饱食度 ↑ 或 饱和度 ↑ 或该槽数量 −。40 tick 未生效 → 松开并回
     * {@code {ok:false, reason:"timeout"}}。重复调用幂等（正在吃就复用，不重复切槽/不重复按）。
     */
    private void handleEat(WireServer.WireCommand cmd) {
        var mc = Minecraft.getInstance();
        if (mc.player == null || mc.gameMode == null) {
            sendResponse(cmd.id(), false, "No player");
            return;
        }

        // 幂等：正在吃就复用同一状态机，只登记 waiter，不重复切槽/按 use
        if (EAT.isActive()) {
            EAT.addWaiter(cmd.id());
            sendResponse(cmd.id(), true, "Already eating");
            return;
        }

        int slot = bestFoodSlot(mc);
        if (slot < 0) {
            sendResponse(cmd.id(), false, "No food in hotbar");
            return;
        }

        followTargetName = null;
        clearPendingCollectTask();

        var stack = mc.player.getInventory().getItem(slot);
        boolean started = EAT.begin(cmd.id(), false, slot,
                mc.player.getFoodData().getFoodLevel(),
                mc.player.getFoodData().getSaturationLevel(),
                stack.getCount());
        if (!started) {
            EAT.addWaiter(cmd.id());
            sendResponse(cmd.id(), true, "Already eating");
            return;
        }
        pendingEatSlot = slot;
        pendingEatPrimary = cmd.id();
        mc.player.getInventory().setSelectedSlot(slot); // 选中该槽，等 2 tick 生效后再按住 use
        setTaskState("eat", "running", "food", "consuming food", 0.05);
        sendBehaviorSnapshot();
        if (LCUMod.WIRE != null) {
            LCUMod.WIRE.sendProgress(cmd.id(), 0.05, "eat queued");
        }
        sendResponse(cmd.id(), true, "Eating food");
    }

    private void handleDropItem(WireServer.WireCommand cmd) {
        var args = cmd.args();
        if (args == null || !args.has("item")) {
            sendResponse(cmd.id(), false, "Need item name");
            return;
        }
        var mc = Minecraft.getInstance();
        if (mc.player == null) {
            sendResponse(cmd.id(), false, "No player");
            return;
        }
        String itemName = args.get("item").getAsString();
        int count = args.has("count") ? args.get("count").getAsInt() : 1;

        // Find item in inventory and drop
        var inv = mc.player.getInventory();
        for (int i = 0; i < inv.getContainerSize(); i++) {
            var stack = inv.getItem(i);
            if (stack.isEmpty()) continue;
            if (stack.getItem().toString().contains(itemName)) {
                // Drop item
                mc.player.drop(stack.split(count), false, Prediction.PREDICTED);
                StateCollector.requestRefresh();
                sendResponse(cmd.id(), true, "Dropped " + itemName);
                return;
            }
        }
        sendResponse(cmd.id(), false, "Item " + itemName + " not found");
    }

    private void handleDrop(WireServer.WireCommand cmd) {
        var args = cmd.args();
        if (args == null || !args.has("slot")) {
            sendResponse(cmd.id(), false, "Missing slot");
            return;
        }
        var mc = Minecraft.getInstance();
        if (mc.player == null) {
            sendResponse(cmd.id(), false, "No player");
            return;
        }
        int slot = args.get("slot").getAsInt();
        int count = args.has("count") ? args.get("count").getAsInt() : 1;

        var inv = mc.player.getInventory();
        int invSlot = slot == -1 ? inv.getSelectedSlot() : slot;
        if (invSlot < 0 || invSlot >= inv.getContainerSize()) {
            sendResponse(cmd.id(), false, "Invalid slot " + slot);
            return;
        }
        var stack = inv.getItem(invSlot);
        if (stack.isEmpty()) {
            sendResponse(cmd.id(), false, "Slot " + slot + " is empty");
            return;
        }
        int dropCount = Math.max(1, Math.min(count, stack.getCount()));
        var dropped = stack.split(dropCount);
        mc.player.drop(dropped, true, Prediction.PREDICTED);
        StateCollector.requestRefresh();
        sendResponse(cmd.id(), true, "Dropped " + dropped.getCount() + " x " + dropped.getItem());
    }

    private void handleEquip(WireServer.WireCommand cmd) {
        var args = cmd.args();
        if (args == null || !args.has("slot")) {
            sendResponse(cmd.id(), false, "Missing slot");
            return;
        }
        var mc = Minecraft.getInstance();
        if (mc.player == null) {
            sendResponse(cmd.id(), false, "No player");
            return;
        }
        String slot = args.get("slot").getAsString();
        if ("mainhand".equals(slot)) {
            sendResponse(cmd.id(), true, "Mainhand already selected");
            return;
        }
        int idx = -1;
        try {
            idx = Integer.parseInt(slot);
        } catch (NumberFormatException ignored) {
        }
        if (idx >= 0 && idx < 9) {
            mc.player.getInventory().setSelectedSlot(idx);
            sendResponse(cmd.id(), true, "Equipped hotbar slot " + idx);
        } else {
            sendResponse(cmd.id(), false, "Unsupported equip slot: " + slot);
        }
    }

    private void handleSortInventory(WireServer.WireCommand cmd) {
        var mc = Minecraft.getInstance();
        if (mc.player == null || mc.gameMode == null) {
            sendResponse(cmd.id(), false, "No player");
            return;
        }
        var player = mc.player;
        var menu = player.inventoryMenu;
        if (player.containerMenu != menu) {
            sendResponse(cmd.id(), false, "Close the open container first");
            return;
        }
        var inv = player.getInventory();

        // Map player-inventory index (0-35) → inventory-menu slot index via container identity
        Map<Integer, Integer> invToMenu = new HashMap<>();
        for (int i = 0; i < menu.slots.size(); i++) {
            var slot = menu.slots.get(i);
            if (slot.container == inv) {
                invToMenu.put(slot.getContainerSlot(), i);
            }
        }

        // Group partial stacks of the same item
        Map<Item, List<Integer>> groups = new HashMap<>();
        for (int i = 0; i < 36; i++) {
            var stack = inv.getItem(i);
            if (stack.isEmpty() || stack.getCount() >= stack.getMaxStackSize()) continue;
            groups.computeIfAbsent(stack.getItem(), k -> new ArrayList<>()).add(i);
        }

        final int clickCap = 20;
        int clicks = 0;
        int mergedStacks = 0;
        boolean anyMergeable = false;
        boolean capReached = false;

        for (List<Integer> slots : groups.values()) {
            if (slots.size() < 2) continue;
            anyMergeable = true;
            int anchor = 0;
            while (anchor < slots.size() - 1 && clicks + 3 <= clickCap) {
                int a = slots.get(anchor);
                var aStack = inv.getItem(a);
                if (aStack.isEmpty() || aStack.getCount() >= aStack.getMaxStackSize()) {
                    anchor++;
                    continue;
                }
                int b = -1;
                for (int gi = anchor + 1; gi < slots.size(); gi++) {
                    var bStack = inv.getItem(slots.get(gi));
                    if (!bStack.isEmpty() && bStack.getItem() == aStack.getItem()
                            && bStack.getCount() < bStack.getMaxStackSize()) {
                        b = slots.get(gi);
                        break;
                    }
                }
                if (b < 0) {
                    anchor++;
                    continue;
                }
                Integer menuA = invToMenu.get(a);
                Integer menuB = invToMenu.get(b);
                if (menuA == null || menuB == null) {
                    sendResponse(cmd.id(), false, "Inventory slot mapping failed");
                    return;
                }
                // Pick up stack A, drop it onto B to merge, place any remainder back in A.
                // handleContainerInput sends the click packet AND applies it to the client menu.
                mc.gameMode.handleContainerInput(menu.containerId, menuA, 0, ContainerInput.PICKUP, player);
                clicks++;
                mc.gameMode.handleContainerInput(menu.containerId, menuB, 0, ContainerInput.PICKUP, player);
                clicks++;
                if (!menu.getCarried().isEmpty()) {
                    mc.gameMode.handleContainerInput(menu.containerId, menuA, 0, ContainerInput.PICKUP, player);
                    clicks++;
                }
                mergedStacks++;
                anchor = b == slots.get(anchor) ? anchor + 1 : anchor;
                if (clicks >= clickCap) capReached = true;
            }
        }

        if (!anyMergeable) {
            sendResponse(cmd.id(), false, "Nothing to sort");
            return;
        }
        StateCollector.requestRefresh();
        if (capReached) {
            sendResponse(cmd.id(), true, "Sorted " + mergedStacks + " stacks (click cap of " + clickCap + " reached, repeat to continue)");
        } else {
            sendResponse(cmd.id(), true, "Sorted " + mergedStacks + " stacks");
        }
    }

    private void handleBuild(WireServer.WireCommand cmd) {
        var args = cmd.args();
        if (args == null || !args.has("x") || !args.has("y") || !args.has("z") || !args.has("structure")) {
            sendResponse(cmd.id(), false, "Need x, y, z, structure");
            return;
        }
        double x = args.get("x").getAsDouble();
        double y = args.get("y").getAsDouble();
        double z = args.get("z").getAsDouble();
        String structure = args.get("structure").getAsString();
        
        // Move to build position
        MovementSystem.moveTo(x, y, z, 1.0f);
        
        // For now, just send chat about building
        var mc = Minecraft.getInstance();
        if (mc.player != null) {
            mc.player.connection.sendChat("Building " + structure + " at " + (int)x + ", " + (int)y + ", " + (int)z);
        }
        sendResponse(cmd.id(), true, "Building " + structure);
    }

    private void handleShutdown() {
        var mc = Minecraft.getInstance();
        LCUMod.LOGGER.info("[Shutdown] Shutdown requested");
        if (mc.getSingleplayerServer() != null) {
            mc.getSingleplayerServer().halt(false);
        } else {
            var conn = mc.getConnection();
            if (conn != null) conn.getConnection().disconnect(Component.literal("AI Shutdown"));
        }
    }

    // ── State Sync ──

    private void sendBehaviorState(boolean enabled) {
        sendBehaviorSnapshot();
    }

    private void sendBehaviorSnapshot() {
        if (LCUMod.WIRE == null) {
            return;
        }
        JsonObject data = new JsonObject();
        data.addProperty("behaviors_enabled", LCUMod.BEHAVIORS == null || LCUMod.BEHAVIORS.isEnabled());
        data.addProperty("follow_target", followTargetName == null ? "" : followTargetName);
        data.addProperty("pending_craft_item", pendingCraftItem == null ? "" : pendingCraftItem);
        data.addProperty("pending_eat", EAT.isActive());
        data.addProperty("pending_collect_item", pendingCollectItem == null ? "" : pendingCollectItem);
        data.addProperty("navigating", Pathfinder.isNavigating());
        LCUMod.WIRE.sendEvent("behavior_state", data);
    }

    private void sendTaskState() {
        if (LCUMod.WIRE == null) {
            return;
        }
        JsonObject data = new JsonObject();
        data.addProperty("kind", activeTaskKind);
        data.addProperty("status", activeTaskStatus);
        data.addProperty("target", activeTaskTarget);
        data.addProperty("detail", activeTaskDetail);
        data.addProperty("progress", activeTaskProgress);
        LCUMod.WIRE.sendEvent("task_state", data);
    }

    private void setTaskState(String kind, String status, String target, String detail, double progress) {
        // 任务 detail/target 也可能带上后端给的文字（物品名、目标名）→ 同一套出站清洗兜底
        com.lcu.lcumod.chat.TextSanitizer.Result cleanTarget =
                com.lcu.lcumod.chat.TextSanitizer.sanitize(target);
        com.lcu.lcumod.chat.TextSanitizer.Result cleanDetail =
                com.lcu.lcumod.chat.TextSanitizer.sanitize(detail);
        if (cleanTarget.sanitized() || cleanDetail.sanitized()) {
            LCUMod.LOGGER.warn("[Safety] task detail 已清洗：target={} detail={}",
                    cleanTarget.text(), cleanDetail.text());
        }
        activeTaskKind = kind;
        activeTaskStatus = status;
        activeTaskTarget = cleanTarget.text();
        activeTaskDetail = cleanDetail.text();
        activeTaskProgress = Math.max(0.0, Math.min(1.0, progress));
        // Terminal transition: force the next state_update so the backend sees the task end immediately
        if (TERMINAL_TASK_STATUSES.contains(status)) {
            StateCollector.requestRefresh();
        }
        sendTaskState();
    }

    private void clearTaskState() {
        activeTaskKind = "idle";
        activeTaskStatus = "idle";
        activeTaskTarget = "";
        activeTaskDetail = "";
        activeTaskProgress = 0.0;
        // Terminal transition: force the next state_update so the backend sees the task end immediately
        StateCollector.requestRefresh();
        sendTaskState();
    }

    private void sendControlState() {
        if (LCUMod.WIRE != null) {
            JsonObject data = new JsonObject();
            data.addProperty("ai_controlled", isAiControlled());
            LCUMod.WIRE.sendEvent("control_state", data);
        }
    }

    // ── Response helpers ──

    private void sendResponse(String id, boolean success, String msg) {
        if (LCUMod.WIRE != null) {
            JsonObject data = new JsonObject();
            data.addProperty("message", msg);
            LCUMod.WIRE.sendResponse(id, success, data, success ? null : msg);
        }
    }

    private void sendResponse(String id, boolean success, JsonObject data) {
        if (LCUMod.WIRE != null) LCUMod.WIRE.sendResponse(id, success, data);
    }

    /** 带机器可读原因码的回执（安全拦截 / 区域拦截 / 如实失败都用它）。 */
    private void sendResponse(String id, boolean success, JsonObject data, String error) {
        if (LCUMod.WIRE != null) LCUMod.WIRE.sendResponse(id, success, data, error);
    }

    private void tickFollowTarget(Minecraft mc) {
        if (followTargetName == null || !InputIsolation.isAiControlled() || mc.player == null || mc.level == null) {
            return;
        }

        if (followRefreshTicks-- > 0) {
            return;
        }
        followRefreshTicks = 10;

        for (var player : mc.level.players()) {
            if (!player.getName().getString().equalsIgnoreCase(followTargetName) || player == mc.player) {
                continue;
            }

            double distance = mc.player.distanceTo(player);
            if (distance > 4.5) {
                MovementSystem.moveTo(player.getX(), player.getY(), player.getZ(), 1.1f);
            } else if (distance < 2.0 && Pathfinder.isNavigating()) {
                Pathfinder.stop();
            }
            return;
        }
    }

    private void tickPendingCraft(Minecraft mc) {
        if (pendingCraftItem == null || pendingCraftReqId == null || mc.player == null || mc.level == null || mc.gameMode == null) {
            return;
        }

        if (hasItem(mc, pendingCraftItem)) {
            if (LCUMod.WIRE != null) {
                LCUMod.WIRE.sendProgress(pendingCraftReqId, 1.0, "crafted: " + pendingCraftItem);
            }
            pendingCraftItem = null;
            pendingCraftReqId = null;
            pendingCraftTicks = 0;
            pendingCraftAttempts = 0;
            pendingCraftGoalCount = 1;
            clearTaskState();
            sendBehaviorSnapshot();
            return;
        }

        if (pendingCollectReqId != null && pendingCollectReqId.equals(pendingCraftReqId)) {
            return;
        }

        pendingCraftTicks++;
        if (pendingCraftTicks % 10 != 0) {
            return;
        }

        int currentCount = countInventoryItem(mc, pendingCraftItem);
        int missingCount = Math.max(0, pendingCraftGoalCount - currentCount);
        CraftingPlanner.CraftPlan plan = CraftingPlanner.plan(mc, pendingCraftItem, missingCount);
        if (!plan.missingRaw.isEmpty()) {
            Map.Entry<String, Integer> firstMissing = plan.missingRaw.entrySet().iterator().next();
            startCollectTask(mc, pendingCraftReqId, firstMissing.getKey(), firstMissing.getValue());
            String detail = "collecting raw resource " + firstMissing.getKey() + " x" + firstMissing.getValue();
            setTaskState("craft", "collecting", pendingCraftItem, detail, Math.min(0.45, 0.1 + pendingCraftAttempts * 0.05));
            if (LCUMod.WIRE != null) {
                LCUMod.WIRE.sendProgress(pendingCraftReqId, Math.min(0.45, 0.1 + pendingCraftAttempts * 0.05), detail);
            }
            sendBehaviorSnapshot();
            return;
        }

        if (!plan.success || plan.steps.isEmpty()) {
            if (LCUMod.WIRE != null) {
                LCUMod.WIRE.sendProgress(pendingCraftReqId, 0.0, plan.failureReason.isBlank() ? "no craft path for " + pendingCraftItem : plan.failureReason);
            }
            pendingCraftItem = null;
            pendingCraftReqId = null;
            pendingCraftGoalCount = 1;
            clearTaskState();
            sendBehaviorSnapshot();
            return;
        }

        CraftingPlanner.CraftStep step = plan.steps.get(0);

        if (step.mode.equals("craft")) {
            boolean needsTable = step.needsCraftingTable;
            if (needsTable && !isCraftingTableOpen(mc)) {
                if (!ContainerController.openNearestContainer(mc, "minecraft:crafting_table")) {
                    if (LCUMod.WIRE != null) {
                        LCUMod.WIRE.sendProgress(pendingCraftReqId, 0.0, "need nearby crafting table for " + pendingCraftItem);
                    }
                    pendingCraftItem = null;
                    pendingCraftReqId = null;
                    pendingCraftGoalCount = 1;
                    clearTaskState();
                    sendBehaviorSnapshot();
                }
                return;
            }

            pendingCraftAttempts++;
            RecipeDisplayId displayId = ContainerController.resolveRecipeDisplayId(step.recipe);
            if (displayId != null) {
                mc.gameMode.handlePlaceRecipe(mc.player.containerMenu.containerId, displayId, false);
                mc.gameMode.handleContainerInput(mc.player.containerMenu.containerId, 0, 0, ContainerInput.QUICK_MOVE, mc.player);
            } else {
                LCUMod.LOGGER.debug("[Action] No recipe display id for craft step {}", step.itemId);
            }
            String detail = "craft step " + pendingCraftAttempts + ": " + step.itemId + " x" + step.craftOperations;
            setTaskState("craft", "crafting", pendingCraftItem, detail, Math.min(0.9, 0.35 + pendingCraftAttempts * 0.12));
            if (LCUMod.WIRE != null) {
                LCUMod.WIRE.sendProgress(
                    pendingCraftReqId,
                    Math.min(0.9, 0.35 + pendingCraftAttempts * 0.12),
                    detail
                );
            }
        } else {
            if (!isProcessingStationOpen(mc, step.mode)) {
                if (!ContainerController.openNearestContainer(mc, step.stationBlockId)) {
                    if (LCUMod.WIRE != null) {
                        LCUMod.WIRE.sendProgress(pendingCraftReqId, 0.0, "need nearby " + step.stationBlockId + " for " + pendingCraftItem);
                    }
                    pendingCraftItem = null;
                    pendingCraftReqId = null;
                    pendingCraftGoalCount = 1;
                    clearTaskState();
                    sendBehaviorSnapshot();
                }
                return;
            }

            if (pickupProcessedOutputIfReady(mc, step.itemId)) {
                setTaskState("craft", "processing", pendingCraftItem, "collecting processed output", Math.min(0.95, 0.55 + pendingCraftAttempts * 0.05));
                return;
            }

            if (!ensureProcessingFuel(mc)) {
                String fuelTarget = selectFuelCollectionTarget(mc);
                if (LCUMod.WIRE != null) {
                    LCUMod.WIRE.sendProgress(pendingCraftReqId, 0.2, "collecting furnace fuel: " + fuelTarget);
                }
                startCollectTask(mc, pendingCraftReqId, fuelTarget, 1);
                setTaskState("craft", "collecting", pendingCraftItem, "collecting furnace fuel: " + fuelTarget, 0.2);
                sendBehaviorSnapshot();
                return;
            }

            if (isProcessingStationBusy(mc)) {
                String detail = step.mode + " in progress for " + pendingCraftItem;
                setTaskState("craft", "processing", pendingCraftItem, detail, Math.min(0.88, 0.45 + pendingCraftAttempts * 0.06));
                if (LCUMod.WIRE != null) {
                    LCUMod.WIRE.sendProgress(
                        pendingCraftReqId,
                        Math.min(0.88, 0.45 + pendingCraftAttempts * 0.06),
                        detail
                    );
                }
                return;
            }

            pendingCraftAttempts++;
            RecipeDisplayId displayId = ContainerController.resolveRecipeDisplayId(step.recipe);
            if (displayId != null) {
                mc.gameMode.handlePlaceRecipe(mc.player.containerMenu.containerId, displayId, false);
            } else {
                LCUMod.LOGGER.debug("[Action] No recipe display id for craft step {}", step.itemId);
            }
            String detail = step.mode + " step " + pendingCraftAttempts + ": " + step.itemId + " x" + step.craftOperations;
            setTaskState("craft", "processing", pendingCraftItem, detail, Math.min(0.9, 0.45 + pendingCraftAttempts * 0.08));
            if (LCUMod.WIRE != null) {
                LCUMod.WIRE.sendProgress(
                    pendingCraftReqId,
                    Math.min(0.9, 0.45 + pendingCraftAttempts * 0.08),
                    detail
                );
            }
        }

        if (pendingCraftAttempts >= 8 && countInventoryItem(mc, pendingCraftItem) < pendingCraftGoalCount) {
            if (LCUMod.WIRE != null) {
                String errorMsg = buildCraftFailureReason(mc, pendingCraftItem);
                LCUMod.WIRE.sendProgress(pendingCraftReqId, 0.0, errorMsg);
            }
            pendingCraftItem = null;
            pendingCraftReqId = null;
            pendingCraftGoalCount = 1;
            clearTaskState();
            sendBehaviorSnapshot();
        }
    }

    private void tickPendingCollect(Minecraft mc) {
        if (pendingCollectItem == null || pendingCollectReqId == null || mc.player == null || mc.level == null || mc.gameMode == null) {
            return;
        }

        int currentCount = countInventoryItem(mc, pendingCollectItem);
        if (currentCount >= pendingCollectGoalCount) {
            if (LCUMod.WIRE != null) {
                LCUMod.WIRE.sendProgress(pendingCollectReqId, pendingCraftReqId != null && pendingCollectReqId.equals(pendingCraftReqId) ? 0.5 : 1.0,
                    "collected " + pendingCollectItem + " x" + Math.max(0, currentCount - pendingCollectBaselineCount));
            }
            clearPendingCollectTask();
            if (pendingCraftReqId == null) {
                clearTaskState();
            }
            sendBehaviorSnapshot();
            return;
        }

        // If storage retrieval is active, let tickPendingStorageRetrieve handle it
        if (pendingStoragePos != null) {
            return;
        }

        // Try storage source before world search (only on early search attempts)
        if (pendingCollectSearchMisses < 3 && tryStorageSourceForCollect(mc)) {
            setTaskState(resolveRootTaskKind(), "storage", pendingCollectItem,
                "checking storage for " + pendingCollectItem, collectProgress(mc));
            return;
        }

        pendingCollectTicks++;
        if (pendingCollectTicks % 5 != 0) {
            return;
        }

        ItemEntity nearbyDrop = findNearbyCollectibleItem(mc, pendingCollectItem, 12.0);
        if (nearbyDrop != null) {
            pendingCollectTargetPos = null;
            MovementSystem.moveTo(nearbyDrop.getX(), nearbyDrop.getY(), nearbyDrop.getZ(), 1.1f);
            setTaskState(resolveRootTaskKind(), "collecting", pendingCollectItem, "picking up nearby drop", collectProgress(mc));
            return;
        }

        if (pendingCollectTargetPos != null && !isMatchingCollectBlock(mc, pendingCollectTargetPos, pendingCollectItem)) {
            pendingCollectTargetPos = null;
        }

        if (pendingCollectTargetPos == null) {
            pendingCollectTargetPos = findNearestCollectibleBlock(mc, pendingCollectItem, 20);
            if (pendingCollectTargetPos == null) {
                pendingCollectSearchMisses++;
                setTaskState(resolveRootTaskKind(), "searching", pendingCollectItem, "searching for collectible block or drop", collectProgress(mc));
                if (pendingCollectSearchMisses >= 12) {
                    if (LCUMod.WIRE != null) {
                        LCUMod.WIRE.sendProgress(pendingCollectReqId, 0.0, "no collectible source found for " + pendingCollectItem);
                    }
                    clearPendingCollectTask();
                    if (pendingCraftReqId == null) {
                        clearTaskState();
                    }
                    sendBehaviorSnapshot();
                }
                return;
            }
            pendingCollectSearchMisses = 0;
        }

        double distance = mc.player.distanceToSqr(Vec3.atCenterOf(pendingCollectTargetPos));
        if (distance > 16.0) {
            MovementSystem.moveTo(pendingCollectTargetPos.getX() + 0.5, pendingCollectTargetPos.getY(), pendingCollectTargetPos.getZ() + 0.5, 1.0f);
            setTaskState(resolveRootTaskKind(), "moving", pendingCollectItem, "moving to resource node", collectProgress(mc));
            return;
        }

        if (diggingPos == null || !pendingCollectTargetPos.equals(diggingPos)) {
            // 区域硬保护同样管到这条路：`collect_blocks` 也是"破坏方块"，农场的作物/建筑一样不许动。
            ZoneGuard.Block collectZone = ZoneGuard.block(
                    pendingCollectTargetPos.getX() + 0.5,
                    pendingCollectTargetPos.getY() + 0.5,
                    pendingCollectTargetPos.getZ() + 0.5,
                    ZoneGuard.Action.MINE, false);
            if (collectZone != null) {
                String reqId = pendingCollectReqId;
                JsonObject data = new JsonObject();
                data.addProperty("blocked_by_zone", collectZone.zone());
                data.addProperty("rule", collectZone.rule());
                data.addProperty("action", "collect_blocks " + pendingCollectTargetPos.toShortString());
                pendingCollectItem = null;
                pendingCollectReqId = null;
                pendingCollectTargetPos = null;
                setTaskState("collect", "failed", "",
                        "blocked_by_zone:" + collectZone.zone() + " rule:" + collectZone.rule(), 0.0);
                if (reqId != null && LCUMod.WIRE != null) {
                    LCUMod.WIRE.sendResponse(reqId, false, data,
                            "blocked_by_zone:" + collectZone.zone() + " rule:" + collectZone.rule());
                }
                return;
            }
            autoEquipForBlock(mc, pendingCollectTargetPos);
            mc.player.lookAt(net.minecraft.commands.arguments.EntityAnchorArgument.Anchor.EYES, Vec3.atCenterOf(pendingCollectTargetPos));
            mc.gameMode.startDestroyBlock(pendingCollectTargetPos, Direction.UP);
            mc.player.swing(InteractionHand.MAIN_HAND, SwingAnimation.DEFAULT, true);
            diggingPos = pendingCollectTargetPos;
            diggingDir = Direction.UP;
            diggingTicks = 0;
        }
        setTaskState(resolveRootTaskKind(), "mining", pendingCollectItem, "mining resource block", collectProgress(mc));
    }

    // ── Storage Retrieval ─────────────────────────────────────────

    /**
     * Try to find a remembered storage container for the current collect task.
     * Sets up pendingStorage fields; tickPendingStorageRetrieve handles the rest.
     */
    private boolean tryStorageSourceForCollect(Minecraft mc) {
        if (pendingCollectItem == null) return false;
        BlockPos storagePos = null;
        for (var poi : PoiMemory.snapshotSortedByItemMatch(mc, "storage", pendingCollectItem, PoiMemory.INTERACTION_RADIUS, 16)) {
            BlockPos candidate = new BlockPos(
                poi.get("x").getAsInt(),
                poi.get("y").getAsInt(),
                poi.get("z").getAsInt()
            );
            if (triedStoragePositions.contains(candidate)) {
                continue;
            }
            storagePos = candidate;
            break;
        }
        if (storagePos == null) return false;

        int currentCount = countInventoryItem(mc, pendingCollectItem);
        int stillNeeded = Math.max(1, pendingCollectGoalCount - currentCount);

        pendingStoragePos = storagePos;
        pendingStorageTargetItem = pendingCollectItem;
        pendingStorageGoalCount = stillNeeded;
        pendingStorageTicks = 0;
        triedStoragePositions.add(storagePos.immutable());
        return true;
    }

    /**
     * State machine for storage retrieval.
     * Navigate → right-click → scan & withdraw → close.
     * Called every tick from onTick() before tickPendingCollect.
     */
    private void tickPendingStorageRetrieve(Minecraft mc) {
        if (pendingStoragePos == null || mc.player == null || mc.level == null || mc.gameMode == null) {
            return;
        }

        // Abort if the collect task was cancelled or target changed
        if (pendingCollectItem == null
            || !CraftingPlanner.matchesRegistryId(pendingStorageTargetItem, pendingCollectItem)) {
            clearPendingStorageTask();
            return;
        }

        pendingStorageTicks++;

        // Timeout: 100 ticks (5 seconds) — prevent getting stuck
        if (pendingStorageTicks > 100) {
            cleanupAndClearStorage(mc);
            return;
        }

        double distSq = mc.player.distanceToSqr(Vec3.atCenterOf(pendingStoragePos));

        // ── Phase 1: Not close enough → navigate ──
        if (distSq > 9.0) {
            MovementSystem.moveTo(
                pendingStoragePos.getX() + 0.5, pendingStoragePos.getY(),
                pendingStoragePos.getZ() + 0.5, 1.0f);
            setTaskState(resolveRootTaskKind(), "storage", pendingStorageTargetItem,
                "moving to storage", 0.15);
            return;
        }

        boolean containerOpen = mc.player.containerMenu != null
            && mc.player.containerMenu != mc.player.inventoryMenu;

        // ── Phase 2: Close enough but container not open → right-click ──
        if (!containerOpen) {
            // Small settle delay after arriving
            if (pendingStorageTicks < 6) {
                setTaskState(resolveRootTaskKind(), "storage", pendingStorageTargetItem,
                    "arrived at storage", 0.2);
                return;
            }
            ContainerController.openContainerAt(mc, pendingStoragePos, 6);
            setTaskState(resolveRootTaskKind(), "storage", pendingStorageTargetItem,
                "opening storage", 0.3);
            return;
        }

        // ── Phase 3: Container is open → scan & withdraw ──
        int foundStacks = withdrawMatchingFromContainer(mc, pendingStorageTargetItem, pendingStorageGoalCount);

        if (foundStacks > 0 && LCUMod.WIRE != null && pendingCollectReqId != null) {
            int totalNow = countInventoryItem(mc, pendingStorageTargetItem);
            int gained = totalNow - pendingCollectBaselineCount;
            LCUMod.WIRE.sendProgress(pendingCollectReqId, 0.45,
                "withdrew "
                + pendingStorageTargetItem + " x" + Math.max(0, gained)
                + " from storage");
        }

        // ── Phase 4: Close container and clear state ──
        mc.player.closeContainer();
        clearPendingStorageTask();
    }

    /**
     * Scan the currently open container menu for items matching itemId,
     * shift-click each matching stack to transfer to player inventory.
     * Returns the number of stacks withdrawn.
     */
    private int withdrawMatchingFromContainer(Minecraft mc, String itemId, int maxCount) {
        if (mc.player.containerMenu == null || mc.player.containerMenu == mc.player.inventoryMenu) {
            return 0;
        }
        int totalSlots = mc.player.containerMenu.slots.size();
        // Player inventory occupies the last ~36 slots (27 main + 9 hotbar).
        // Container slots are everything before that.
        int containerEndSlot = Math.max(0, totalSlots - 36);

        // Scan all container slots and cache contents
        Map<String, Integer> contents = new java.util.HashMap<>();
        List<Integer> matchingSlots = new ArrayList<>();
        for (int slot = 0; slot < containerEndSlot; slot++) {
            var stack = mc.player.containerMenu.slots.get(slot).getItem();
            if (stack.isEmpty()) continue;
            String stackId = BuiltInRegistries.ITEM.getKey(stack.getItem()).toString();
            contents.merge(stackId, stack.getCount(), Integer::sum);
            if (CraftingPlanner.matchesRegistryId(stackId, itemId)) {
                matchingSlots.add(slot);
                if (matchingSlots.size() * 64 >= maxCount) break;
            }
        }

        // Update POI memory cache with known contents
        if (pendingStoragePos != null) {
            PoiMemory.updateStorageContents(pendingStoragePos, contents, tickCount);
        }

        if (matchingSlots.isEmpty()) return 0;

        // Shift-click each matching slot to withdraw
        for (int slot : matchingSlots) {
            mc.gameMode.handleContainerInput(
                mc.player.containerMenu.containerId, slot, 0,
                ContainerInput.QUICK_MOVE, mc.player);
        }

        return matchingSlots.size();
    }

    /** Close open container if any, then clear storage state. */
    private void cleanupAndClearStorage(Minecraft mc) {
        if (mc.player.containerMenu != null && mc.player.containerMenu != mc.player.inventoryMenu) {
            mc.player.closeContainer();
        }
        clearPendingStorageTask();
    }

    private void clearPendingStorageTask() {
        pendingStoragePos = null;
        pendingStorageTargetItem = null;
        pendingStorageGoalCount = 0;
        pendingStorageTicks = 0;
    }

    private boolean hasItem(Minecraft mc, String itemName) {
        for (int i = 0; i < mc.player.getInventory().getContainerSize(); i++) {
            var stack = mc.player.getInventory().getItem(i);
            if (stack.isEmpty()) continue;
            String id = BuiltInRegistries.ITEM.getKey(stack.getItem()).toString();
            if (id.equals(itemName) || id.endsWith(":" + itemName)) {
                return true;
            }
        }
        return false;
    }

    /**
     * 26.3: server-side recipe placement (ServerboundPlaceRecipePacket) needs a RecipeDisplayId
     * (the index the server's RecipeManager assigned to the recipe's display) instead of a RecipeHolder.
     * 解析统一放在 {@link ContainerController#resolveRecipeDisplayId}（容器驱动层单点）。
     */
    private boolean isCraftingTableOpen(Minecraft mc) {
        return mc.player.containerMenu != null
            && mc.player.containerMenu != mc.player.inventoryMenu
            && mc.player.containerMenu.slots.size() >= 10;
    }

    private boolean isProcessingStationOpen(Minecraft mc, String mode) {
        if (mc.player.containerMenu == null || mc.player.containerMenu == mc.player.inventoryMenu) {
            return false;
        }
        String menuName = mc.player.containerMenu.getClass().getSimpleName().toLowerCase();
        return switch (mode) {
            case "smelt" -> menuName.contains("furnace") && !menuName.contains("blast") && !menuName.contains("smoker");
            case "blast" -> menuName.contains("blast");
            case "smoke" -> menuName.contains("smoker");
            default -> false;
        };
    }

    private boolean pickupProcessedOutputIfReady(Minecraft mc, String expectedItemId) {
        if (mc.player.containerMenu == null || mc.player.containerMenu.slots.size() < 3) {
            return false;
        }
        var outputStack = mc.player.containerMenu.slots.get(2).getItem();
        if (outputStack.isEmpty()) {
            return false;
        }
        String outputId = BuiltInRegistries.ITEM.getKey(outputStack.getItem()).toString();
        if (!CraftingPlanner.matchesRegistryId(outputId, expectedItemId)) {
            return false;
        }
        mc.gameMode.handleContainerInput(mc.player.containerMenu.containerId, 2, 0, ContainerInput.QUICK_MOVE, mc.player);
        return true;
    }

    private boolean isProcessingStationBusy(Minecraft mc) {
        if (mc.player.containerMenu == null || mc.player.containerMenu.slots.size() < 3) {
            return false;
        }
        return !mc.player.containerMenu.slots.get(0).getItem().isEmpty()
            || !mc.player.containerMenu.slots.get(2).getItem().isEmpty();
    }

    private boolean ensureProcessingFuel(Minecraft mc) {
        if (mc.player.containerMenu == null || mc.player.containerMenu.slots.size() < 3) {
            return false;
        }

        var fuelStack = mc.player.containerMenu.slots.get(1).getItem();
        if (!fuelStack.isEmpty() && fuelStack.has(DataComponents.COOKING_FUEL)) {
            return true;
        }

        int fuelSlot = findFuelInventoryMenuSlot(mc);
        if (fuelSlot < 0) {
            return false;
        }

        mc.gameMode.handleContainerInput(mc.player.containerMenu.containerId, fuelSlot, 0, ContainerInput.PICKUP, mc.player);
        mc.gameMode.handleContainerInput(mc.player.containerMenu.containerId, 1, 0, ContainerInput.PICKUP, mc.player);
        if (!mc.player.containerMenu.getCarried().isEmpty()) {
            mc.gameMode.handleContainerInput(mc.player.containerMenu.containerId, fuelSlot, 0, ContainerInput.PICKUP, mc.player);
        }
        return true;
    }

    private int findFuelInventoryMenuSlot(Minecraft mc) {
        if (mc.player.containerMenu == null) {
            return -1;
        }
        for (int slot = 3; slot < mc.player.containerMenu.slots.size(); slot++) {
            var stack = mc.player.containerMenu.slots.get(slot).getItem();
            if (!stack.isEmpty() && stack.has(DataComponents.COOKING_FUEL)) {
                return slot;
            }
        }
        return -1;
    }

    private String selectFuelCollectionTarget(Minecraft mc) {
        ItemEntity nearbyFuel = findNearbyFuelItem(mc, 16.0);
        if (nearbyFuel != null) {
            return BuiltInRegistries.ITEM.getKey(nearbyFuel.getItem().getItem()).toString();
        }

        String nearbyFuelBlockItem = findNearbyFuelBlockItem(mc, 16);
        if (nearbyFuelBlockItem != null && !nearbyFuelBlockItem.isBlank()) {
            return nearbyFuelBlockItem;
        }

        return "coal";
    }

    private ItemEntity findNearbyFuelItem(Minecraft mc, double radius) {
        AABB searchBox = mc.player.getBoundingBox().inflate(radius);
        ItemEntity nearest = null;
        double nearestDistance = Double.MAX_VALUE;
        for (Entity entity : mc.level.getEntities(mc.player, searchBox)) {
            if (!(entity instanceof ItemEntity itemEntity)) continue;
            if (!itemEntity.getItem().has(DataComponents.COOKING_FUEL)) continue;
            double distance = mc.player.distanceToSqr(itemEntity);
            if (distance < nearestDistance) {
                nearest = itemEntity;
                nearestDistance = distance;
            }
        }
        return nearest;
    }

    private String findNearbyFuelBlockItem(Minecraft mc, int radius) {
        BlockPos origin = mc.player.blockPosition();
        String bestFuelItem = null;
        double bestDistance = Double.MAX_VALUE;
        for (int dx = -radius; dx <= radius; dx++) {
            for (int dy = -4; dy <= 4; dy++) {
                for (int dz = -radius; dz <= radius; dz++) {
                    BlockPos pos = origin.offset(dx, dy, dz);
                    var blockState = mc.level.getBlockState(pos);
                    if (blockState.isAir()) continue;
                    var blockItem = blockState.getBlock().asItem();
                    if (blockItem == net.minecraft.world.item.Items.AIR) continue;
                    var blockStack = new net.minecraft.world.item.ItemStack(blockItem);
                    if (!blockStack.has(DataComponents.COOKING_FUEL)) continue;
                    double distance = origin.distSqr(pos);
                    if (distance < bestDistance) {
                        bestDistance = distance;
                        bestFuelItem = BuiltInRegistries.ITEM.getKey(blockItem).toString();
                    }
                }
            }
        }
        return bestFuelItem;
    }

    private String normalizeItemName(String itemName) {
        String compact = itemName.replace(" ", "").trim();
        return switch (compact) {
            case "木剑" -> "wooden_sword";
            case "木镐" -> "wooden_pickaxe";
            case "木棍" -> "stick";
            case "木板" -> "oak_planks";
            case "工作台" -> "crafting_table";
            case "石剑" -> "stone_sword";
            case "石镐" -> "stone_pickaxe";
            default -> compact;
        };
    }

    private void tickPendingEat(Minecraft mc) {
        if (!EAT.isActive() || mc.player == null || mc.gameMode == null) {
            return;
        }
        // 玩家接管 / AI 关闭：立即中止进食（不能在没有输入控制权时假装还在吃）
        if (!InputIsolation.isAiControlled()
                || (mc.mouseHandler != null && mc.mouseHandler.isMouseGrabbed())) {
            abortEat("takeover");
            return;
        }

        var inv = mc.player.getInventory();
        int slot = EAT.slot();
        int count = (slot >= 0 && slot < 9) ? inv.getItem(slot).getCount() : 0;
        EatController.Outcome outcome = EAT.tick(
                mc.player.getFoodData().getFoodLevel(),
                mc.player.getFoodData().getSaturationLevel(),
                count);

        if (outcome != EatController.Outcome.NONE) {
            finishEat(outcome);
            return;
        }

        // 等选中生效后再按住 use；谓词 = “还在吃”，吃完/超时即 release
        if (EAT.shouldStartUse()) {
            EAT.markUseStarted();
            ControlStream.holdUntil("use", EAT::isActive, EatController.MAX_TICKS + 5);
        }
        if (pendingEatPrimary != null && EAT.ticks() % 10 == 0 && LCUMod.WIRE != null) {
            LCUMod.WIRE.sendProgress(pendingEatPrimary, 0.4, "eating in progress");
        }
    }

    /** 进食结束：松开 hold，按可验证结果诚实回执（成功 = outcome succeeded；超时 = ok:false/reason:timeout）。 */
    private void finishEat(EatController.Outcome outcome) {
        List<String> waiters = EAT.drainWaiters();
        EAT.reset();
        pendingEatSlot = -1;
        pendingEatPrimary = null;

        boolean success = outcome == EatController.Outcome.SUCCESS;
        setTaskState("eat", success ? "done" : "failed", "food",
                success ? "ate food" : "timeout", success ? 1.0 : 0.0);
        sendBehaviorSnapshot();

        for (String id : waiters) {
            if (id == null || REFLEX_EAT_REQ_ID.equals(id)) {
                continue; // 反射合成 id：后端没有对应请求，不回执
            }
            if (success) {
                if (LCUMod.WIRE != null) {
                    LCUMod.WIRE.sendOutcome(id, "succeeded", "ate food", "");
                }
            } else {
                JsonObject data = new JsonObject();
                data.addProperty("ok", false);
                data.addProperty("reason", "timeout");
                if (LCUMod.WIRE != null) {
                    LCUMod.WIRE.sendResponse(id, false, data, "timeout");
                }
            }
        }
    }

    private void startCollectTask(Minecraft mc, String requestId, String itemId, int desiredAdditionalCount) {
        pendingCollectItem = itemId;
        pendingCollectReqId = requestId;
        pendingCollectBaselineCount = countInventoryItem(mc, itemId);
        pendingCollectGoalCount = pendingCollectBaselineCount + Math.max(1, desiredAdditionalCount);
        pendingCollectTargetPos = null;
        pendingCollectTicks = 0;
        pendingCollectSearchMisses = 0;
    }

    private void clearPendingCollectTask() {
        pendingCollectItem = null;
        pendingCollectReqId = null;
        pendingCollectGoalCount = 0;
        pendingCollectBaselineCount = 0;
        pendingCollectTargetPos = null;
        pendingCollectTicks = 0;
        pendingCollectSearchMisses = 0;
        triedStoragePositions.clear();
        clearPendingStorageTask();
    }

    public static int countInventoryItem(Minecraft mc, String itemId) {
        int total = 0;
        for (int i = 0; i < mc.player.getInventory().getContainerSize(); i++) {
            var stack = mc.player.getInventory().getItem(i);
            if (stack.isEmpty()) continue;
            String id = BuiltInRegistries.ITEM.getKey(stack.getItem()).toString();
            if (CraftingPlanner.matchesRegistryId(id, itemId)) {
                total += stack.getCount();
            }
        }
        return total;
    }

    private ItemEntity findNearbyCollectibleItem(Minecraft mc, String itemId, double radius) {
        AABB searchBox = mc.player.getBoundingBox().inflate(radius);
        ItemEntity nearest = null;
        double nearestDistance = Double.MAX_VALUE;
        for (Entity entity : mc.level.getEntities(mc.player, searchBox)) {
            if (!(entity instanceof ItemEntity itemEntity)) continue;
            String dropId = BuiltInRegistries.ITEM.getKey(itemEntity.getItem().getItem()).toString();
            if (!matchesCollectTargetId(dropId, itemId)) continue;
            double distance = mc.player.distanceToSqr(itemEntity);
            if (distance < nearestDistance) {
                nearest = itemEntity;
                nearestDistance = distance;
            }
        }
        return nearest;
    }

    private BlockPos findNearestCollectibleBlock(Minecraft mc, String itemId, int radius) {
        BlockPos origin = mc.player.blockPosition();
        BlockPos best = null;
        double bestDistance = Double.MAX_VALUE;
        for (int dx = -radius; dx <= radius; dx++) {
            for (int dy = -4; dy <= 4; dy++) {
                for (int dz = -radius; dz <= radius; dz++) {
                    BlockPos pos = origin.offset(dx, dy, dz);
                    if (!isMatchingCollectBlock(mc, pos, itemId)) continue;
                    double distance = origin.distSqr(pos);
                    if (distance < bestDistance) {
                        best = pos.immutable();
                        bestDistance = distance;
                    }
                }
            }
        }
        return best;
    }

    private boolean isMatchingCollectBlock(Minecraft mc, BlockPos pos, String itemId) {
        var state = mc.level.getBlockState(pos);
        if (state.isAir()) {
            return false;
        }
        String blockId = BuiltInRegistries.BLOCK.getKey(state.getBlock()).toString();
        if (matchesCollectTargetId(blockId, itemId)) {
            return true;
        }
        String itemFromBlock = BuiltInRegistries.ITEM.getKey(state.getBlock().asItem()).toString();
        return matchesCollectTargetId(itemFromBlock, itemId);
    }

    private boolean matchesCollectTargetId(String candidateId, String targetId) {
        if (CraftingPlanner.matchesRegistryId(candidateId, targetId)) {
            return true;
        }

        String candidatePath = resourcePath(candidateId);
        String targetPath = resourcePath(targetId);
        if (candidatePath.isBlank() || targetPath.isBlank()) {
            return false;
        }

        if (candidatePath.contains(targetPath) || targetPath.contains(candidatePath)) {
            return true;
        }

        String targetToken = resourceToken(targetPath);
        return !targetToken.isBlank() && candidatePath.contains(targetToken);
    }

    private String resourcePath(String registryId) {
        if (registryId == null || registryId.isBlank()) {
            return "";
        }
        int separator = registryId.indexOf(':');
        return (separator >= 0 ? registryId.substring(separator + 1) : registryId).toLowerCase();
    }

    private String resourceToken(String resourcePath) {
        String token = resourcePath.toLowerCase();
        String[] removablePrefixes = {"raw_", "deepslate_", "stripped_"};
        for (String prefix : removablePrefixes) {
            if (token.startsWith(prefix)) {
                token = token.substring(prefix.length());
            }
        }
        String[] removableSuffixes = {"_ore", "_block", "_ingot", "_nugget", "_dust", "_gem"};
        for (String suffix : removableSuffixes) {
            if (token.endsWith(suffix)) {
                token = token.substring(0, token.length() - suffix.length());
            }
        }
        return token;
    }

    private void autoEquipForBlock(Minecraft mc, BlockPos blockPos) {
        var blockState = mc.level.getBlockState(blockPos);
        var inv = mc.player.getInventory();
        int bestSlot = -1;
        float bestSpeed = 0f;
        for (int i = 0; i < 9; i++) {
            var stack = inv.getItem(i);
            if (stack.isEmpty()) continue;
            float speed = stack.getDestroySpeed(blockState);
            if (speed <= 0) continue;
            float score = stack.isCorrectToolForDrops(blockState) ? speed * 3.0f : speed * 0.5f;
            if (score > bestSpeed) {
                bestSpeed = score;
                bestSlot = i;
            }
        }
        if (bestSlot >= 0 && bestSlot != inv.getSelectedSlot()) {
            inv.setSelectedSlot(bestSlot);
        }
    }

    private String resolveRootTaskKind() {
        if (pendingCraftReqId != null) return "craft";
        if (pendingCollectReqId != null) return "collect";
        if (EAT.isActive()) return "eat";
        if (followTargetName != null) return "follow";
        if (Pathfinder.isNavigating()) return "move";
        return "idle";
    }

    private String buildCraftFailureReason(Minecraft mc, String itemId) {
        StringBuilder sb = new StringBuilder("无法合成 ").append(itemId).append("：");
        boolean first = true;

        // 检查配方是否存在
        RecipeHolder<?> recipe = ContainerController.findRecipeByResult(mc, itemId);
        if (recipe == null) {
            sb.append("无配方");
            return sb.toString();
        }

        // 检查是否需要工作台
        boolean needsTable = true;
        for (var display : recipe.value().display()) {
            if (display instanceof ShapedCraftingRecipeDisplay shaped) {
                needsTable = shaped.width() > 2 || shaped.height() > 2;
            } else {
                needsTable = recipe.value().placementInfo().ingredients().size() > 4;
            }
            break;
        }
        if (needsTable && !isCraftingTableOpen(mc)) {
            if (!first) sb.append("；");
            sb.append("需要工作台（附近没有）");
            first = false;
        }

        // 检查材料
        var ingredients = recipe.value().placementInfo().ingredients();
        for (Ingredient ingredient : ingredients) {
            if (ingredient == null || ingredient.isEmpty()) continue;
            var options = ingredient.items().toList();
            boolean hasAny = false;
            for (var item : options) {
                ItemStack stack = item.value().getDefaultInstance();
                if (stack.isEmpty()) continue;
                String stackId = BuiltInRegistries.ITEM.getKey(stack.getItem()).toString();
                if (countInventoryItem(mc, stackId) > 0) {
                    hasAny = true;
                    break;
                }
            }
            if (!hasAny) {
                if (!first) sb.append("；");
                sb.append("缺材料：").append(options.get(0).value().getDefaultInstance().getDisplayName().getString());
                first = false;
            }
        }

        if (first) {
            sb.append("未知原因");
        }
        return sb.toString();
    }

    private double collectProgress(Minecraft mc) {
        if (pendingCollectItem == null || pendingCollectGoalCount <= pendingCollectBaselineCount) {
            return 0.0;
        }
        int current = countInventoryItem(mc, pendingCollectItem);
        int gained = Math.max(0, current - pendingCollectBaselineCount);
        int required = Math.max(1, pendingCollectGoalCount - pendingCollectBaselineCount);
        return Math.min(0.95, gained / (double) required);
    }

    /** 本地饥饿反射是否正在进食（用于 state 里的 reflex_eat）。 */
    public static boolean isReflexEating() {
        return EAT.isReflex();
    }

    /** 立即释放所有“按住”（新任务 / stop_all / 玩家接管等），并停止关联的挖掘/使用收尾。 */
    private static void releaseActiveHolds(HoldController.Release reason) {
        ControlStream.releaseHolds(reason);
    }

    // ── Break Tasks ──

    private void tickBreaks() {
        activeBreaks.values().removeIf(t -> t.tick());
    }

    static class BreakTask {
        final BlockPos pos;
        final int totalTicks;
        int ticks;
        BreakTask(BlockPos pos, int totalTicks) { this.pos = pos; this.totalTicks = totalTicks; this.ticks = 0; }
        boolean tick() { return ++ticks >= totalTicks; }
    }
}
