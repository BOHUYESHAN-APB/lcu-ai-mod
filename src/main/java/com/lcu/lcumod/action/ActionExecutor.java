package com.lcu.lcumod.action;

import com.google.gson.JsonArray;
import com.google.gson.JsonObject;
import com.lcu.lcumod.LCUMod;
import com.lcu.lcumod.client.ClientBodyRuntime;
import com.lcu.lcumod.compat.WatutCompat;
import com.lcu.lcumod.config.ServerPolicy;
import com.lcu.lcumod.network.WireServer;
import net.minecraft.client.Minecraft;
import net.minecraft.core.BlockPos;
import net.minecraft.core.registries.BuiltInRegistries;
import net.minecraft.network.chat.Component;
import net.minecraft.network.protocol.game.ServerboundClientCommandPacket;
import net.minecraft.network.protocol.game.ServerboundMovePlayerPacket;
import net.minecraft.world.InteractionHand;
import net.minecraft.world.entity.Entity;
import net.minecraft.world.entity.item.ItemEntity;
import net.minecraft.world.inventory.ClickType;
import net.minecraft.world.inventory.CraftingMenu;
import net.minecraft.world.item.crafting.RecipeHolder;
import net.minecraft.world.item.crafting.RecipeType;
import net.minecraft.world.phys.AABB;
import net.minecraft.world.phys.BlockHitResult;
import net.minecraft.world.phys.EntityHitResult;
import net.minecraft.world.phys.Vec3;
import net.minecraft.core.Direction;
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
    private static String diggingReqId = null;
    private static final int DIGGING_TIMEOUT_TICKS = 600;
    private static final Set<String> HAND_ACTION_COMMANDS = Set.of(
        "attack", "attack_entity", "use_item", "use_on", "use_on_entity", "interact_block_at",
        "mine_block", "dig_block", "mine_block_at", "place_block", "interact_block", "interact",
        "eat", "select_hotbar", "equip_item", "auto_equip", "inventory_click", "container_button",
        "place_recipe", "take_item", "put_item", "drop_item", "sort_inventory", "craft_item"
    );
    private static final Set<String> MOVEMENT_COMMANDS = Set.of(
        "move_to", "jump", "sneak", "sprint", "set_control_state", "follow_player",
        "collect_blocks", "craft_item", "explore", "build"
    );
    private static final Set<String> WORLD_COMMANDS = Set.of(
        "mine_block", "dig_block", "mine_block_at", "use_on", "use_on_entity",
        "interact_block_at", "interact_block", "interact", "place_block", "collect_blocks", "craft_item",
        "trade", "sleep", "build"
    );
    private static final Set<String> INVENTORY_COMMANDS = Set.of(
        "use_item", "select_hotbar", "equip_item", "auto_equip", "inventory_click", "container_button",
        "place_recipe", "take_item", "put_item", "drop_item", "sort_inventory",
        "craft_item", "eat", "trade", "build"
    );
    // Jump cooldown
    private static int jumpCooldown = 0;
    private static final int JUMP_COOLDOWN_TICKS = 25;
    // AI/User control
    private static boolean aiControlled = false;
    private static boolean wasDead = false;
    private static int respawnRetryTicks = 0;
    private static int respawnAttempts = 0;
    private static String followTargetName = null;
    private static String followReqId = null;
    private static String suspendedFollowTargetName = null;
    private static int followRefreshTicks = 0;
    private static String pendingCraftItem = null;
    private static String pendingCraftReqId = null;
    private static int pendingCraftTicks = 0;
    private static int pendingCraftAttempts = 0;
    private static int pendingCraftNoProgressAttempts = 0;
    private static boolean pendingCraftAwaitingOutput = false;
    private static boolean pendingCraftOutputClicked = false;
    private static int pendingCraftOutputWaitTicks = 0;
    private static int pendingCraftOutputMenuId = -1;
    private static int pendingCraftOutputBaseline = 0;
    private static String pendingCraftExpectedOutput = null;
    private static int pendingCraftProcessingStallTicks = 0;
    private static BlockPos pendingCraftStationPos = null;
    private static Vec3 pendingCraftStationStandPos = null;
    private static int pendingCraftStationUseAttempts = 0;
    private static String pendingEatReqId = null;
    private static int pendingEatTicks = 0;
    private static int pendingEatAttempts = 0;
    private static int pendingEatStartHunger = -1;
    private static float pendingEatStartHealth = -1;
    private static String pendingCollectItem = null;
    private static String pendingCollectReqId = null;
    private static int pendingCollectGoalCount = 0;
    private static int pendingCollectBaselineCount = 0;
    private static BlockPos pendingCollectTargetPos = null;
    private static int pendingCollectTicks = 0;
    private static int pendingCollectSearchMisses = 0;
    private static int pendingCraftGoalCount = 1;
    private static String lastCraftPlanDetail = "";
    // Storage retrieval from remembered chests/barrels
    private static BlockPos pendingStoragePos = null;
    private static BlockPos pendingStorageInteractionPos = null;
    private static Vec3 pendingStorageStandPos = null;
    private static String pendingStorageTargetItem = null;
    private static int pendingStorageGoalCount = 0;
    private static int pendingStorageStartCount = 0;
    private static int pendingStorageTicks = 0;
    private static int pendingStorageOpenSentTick = -1;
    private static int pendingStorageOpenAttempts = 0;
    private static int pendingStorageOwnedMenuId = -1;
    private static int pendingStorageMenuOpenTick = -1;
    private static int pendingStorageClickTick = -1;
    private static int pendingStorageClickBaseline = 0;
    private static int pendingStorageClickAttempts = 0;
    private static final java.util.Set<BlockPos> triedStoragePositions = new java.util.HashSet<>();
    private static String activeTaskKind = "idle";
    private static String activeTaskStatus = "idle";
    private static String activeTaskTarget = "";
    private static String activeTaskDetail = "";
    private static double activeTaskProgress = 0.0;
    private static boolean externalControlActive = false;
    private static boolean behaviorEnabledBeforeExternal = true;
    private static long activeFencingToken = 0;
    private static volatile boolean backendDisconnectStopRequested = false;
    private int tickCount = 0;
    private boolean activitySignalsWereAllowed;
    private boolean movementWasAllowed = true;
    private boolean inventoryWasAllowed;
    private boolean backgroundSuspended;

    /** Called every client tick via ActionExecutorBridge (ClientTickEvent.Post). */
    public void onTick() {
        var mc = Minecraft.getInstance();
        if (mc == null || mc.level == null || mc.player == null) return;

        if (!ServerPolicy.backgroundExecutionAllowed() && !mc.isWindowActive()) {
            if (!backgroundSuspended) {
                backgroundSuspended = true;
                stopAllRuntime();
            }
            for (WireServer.WireCommand pending : WireServer.commandQueue.drain()) {
                if (isSafetyControl(pending.cmd())) {
                    executeCommand(pending);
                } else {
                    sendResponse(pending.id(), false,
                        "POLICY_DISABLED: background execution is disabled while Minecraft is unfocused");
                }
            }
            return;
        }
        backgroundSuspended = false;

        if (backendDisconnectStopRequested) {
            backendDisconnectStopRequested = false;
            if (InputIsolation.isAiControlled()) {
                InputIsolation.toggleControl();
                sendControlStateToBackend();
            }
            handleStopAll(new WireServer.WireCommand("backend-disconnect", "stop_all", new JsonObject()));
        }

        enforceActivePolicies(mc);

        // ── Input isolation (core control system) ──
        InputIsolation.tick(mc);

        // ── Pathfinder (A* navigation) ──
        Pathfinder.tick(mc);

        // ── Remembered workstations/storage POIs ──
        if (ServerPolicy.surroundingsCollectionAllowed()) {
            PoiMemory.tick(mc, tickCount);
        }

        // ── Follow controller (persistent follow target) ──
        tickFollowTarget(mc);

        // ── Craft controller (stateful crafting) ──
        tickPendingCraft(mc);

        // ── Storage retrieval controller (check chests/barrels before world collect) ──
        tickPendingStorageRetrieve(mc);

        // ── Collect controller (generic resource acquisition) ──
        tickPendingCollect(mc);

        // ── Eat controller (stateful eating / healing) ──
        tickPendingEat(mc);

        // ── Movement system (packet-based) ──
        MovementSystem.tick(mc);

        boolean optionalAutonomy = ServerPolicy.autonomousBehaviorsAllowed()
                && (ClientBodyRuntime.BEHAVIORS == null || ClientBodyRuntime.BEHAVIORS.isEnabled());

        boolean manualTaskActive = hasManualTask();
        boolean behaviorActive = false;

        // ── Java-side autonomous behavior (works without backend) ──
        if (InputIsolation.isAiControlled() && optionalAutonomy && !manualTaskActive) {
            behaviorActive = JavaAutonomousBehavior.tick(mc);
        }

        boolean runtimeBusy = Pathfinder.isNavigating()
                || MovementSystem.isMoving()
                || JavaAutonomousBehavior.getState() != JavaAutonomousBehavior.BehaviorState.IDLE
                || !WireServer.commandQueue.isEmpty();

        if (InputIsolation.isAiControlled() && runtimeBusy
                && ServerPolicy.programmaticActivityReportingAllowed()) {
            WatutCompat.reportProgrammaticAction(mc.player.tickCount);
        }

        // If behavior is active and no backend command is pending, skip other actions.
        if (behaviorActive && WireServer.commandQueue.isEmpty()) {
            return;
        }

        // ── Human-like idle behavior (head tracking) ──
        if (InputIsolation.isAiControlled()
                && optionalAutonomy
                && !manualTaskActive
                && JavaAutonomousBehavior.getState() == JavaAutonomousBehavior.BehaviorState.IDLE
                && !MovementSystem.isMoving()
                && !Pathfinder.isNavigating()) {
            HumanLikeBehavior.tick(mc);
        }

        // ── Anti-AFK subtle activity pulses ──
        if (ServerPolicy.activitySignalsAllowed()) {
            ActivitySignalController.tick(mc, runtimeBusy);
        }

        // ── Auto-respawn ──
        if (mc.player.isDeadOrDying()) {
            if (!wasDead) {
                wasDead = true;
                respawnAttempts = 0;
                respawnRetryTicks = 0;
                stopAllRuntime();
            }

            if (ServerPolicy.autoRespawnAllowed() && respawnRetryTicks-- <= 0) {
                respawnRetryTicks = 12;
                respawnAttempts++;
                LCUMod.LOGGER.info("[AutoRespawn] Attempt {}", respawnAttempts);
                var conn = mc.getConnection();
                if (conn != null) {
                    conn.send(new ServerboundClientCommandPacket(ServerboundClientCommandPacket.Action.PERFORM_RESPAWN));
                }
            }
            return;
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

        // ── Drain command queue ──
        WireServer.WireCommand cmd = WireServer.commandQueue.poll();
        if (cmd != null) {
            LCUMod.LOGGER.info("[Action] Processing: {} id={}", cmd.cmd(), cmd.id());
            executeCommand(cmd);
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
            backendDisconnectStopRequested = true;
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

    public static void requestBackendDisconnectStop() {
        backendDisconnectStopRequested = true;
    }

    private void enforceActivePolicies(Minecraft mc) {
        boolean movementAllowed = ServerPolicy.movementAutomationAllowed();
        boolean inventoryAllowed = ServerPolicy.inventoryAutomationAllowed();
        boolean movementRevoked = !movementAllowed
            && (Pathfinder.isNavigating() || MovementSystem.isMoving() || followReqId != null
                || pendingCollectReqId != null || pendingCraftReqId != null);
        boolean worldRevoked = !ServerPolicy.worldAutomationAllowed()
            && (diggingPos != null || pendingCollectReqId != null || pendingCraftReqId != null);
        boolean inventoryRevoked = !inventoryAllowed
            && (pendingCraftReqId != null || pendingEatReqId != null || pendingStorageTargetItem != null);
        if (movementRevoked || worldRevoked || inventoryRevoked) {
            stopAllRuntime();
        }
        if (!ServerPolicy.autonomousBehaviorsAllowed()
                && JavaAutonomousBehavior.getState() != JavaAutonomousBehavior.BehaviorState.IDLE) {
            JavaAutonomousBehavior.resetCurrentState();
            MovementSystem.stop();
            if (mc.player.isUsingItem() && mc.gameMode != null) mc.gameMode.releaseUsingItem(mc.player);
        }
        if (!movementAllowed && movementWasAllowed) InputIsolation.clearAiControls();
        if (!inventoryAllowed && inventoryWasAllowed
                && mc.player.isUsingItem() && mc.gameMode != null) {
            mc.gameMode.releaseUsingItem(mc.player);
        }
        movementWasAllowed = movementAllowed;
        inventoryWasAllowed = inventoryAllowed;
        boolean activitySignalsAllowed = ServerPolicy.activitySignalsAllowed();
        if (!activitySignalsAllowed && activitySignalsWereAllowed) {
            ActivitySignalController.reset();
        }
        activitySignalsWereAllowed = activitySignalsAllowed;
    }

    private boolean isSafetyControl(String command) {
        return switch (command) {
            case "stop_all", "stop_digging", "cancel_operation", "control_external",
                 "control_builtin", "behavior_disable" -> true;
            default -> false;
        };
    }

    // ── Command Execution ──

    private void executeCommand(WireServer.WireCommand cmd) {
        var mc = Minecraft.getInstance();
        if (mc == null || mc.player == null) {
            sendResponse(cmd.id(), false, "No player");
            return;
        }

        try {
            if (!"control_external".equals(cmd.cmd()) && !"control_builtin".equals(cmd.cmd())
                    && externalControlActive && commandFencingToken(cmd) != activeFencingToken) {
                sendResponse(cmd.id(), false, "Stale or missing control fencing token");
                return;
            }
            if (!isAiControlled() && requiresArmedBody(cmd.cmd())) {
                sendResponse(cmd.id(), false, "BODY_DISARMED: press F12 or explicitly arm this body before actions");
                return;
            }
            if (MOVEMENT_COMMANDS.contains(cmd.cmd()) && !ServerPolicy.movementAutomationAllowed()) {
                sendResponse(cmd.id(), false, "POLICY_DISABLED: movement automation is disabled");
                return;
            }
            if (WORLD_COMMANDS.contains(cmd.cmd()) && !ServerPolicy.worldAutomationAllowed()) {
                sendResponse(cmd.id(), false, "POLICY_DISABLED: world automation is disabled");
                return;
            }
            if (INVENTORY_COMMANDS.contains(cmd.cmd()) && !ServerPolicy.inventoryAutomationAllowed()) {
                sendResponse(cmd.id(), false, "POLICY_DISABLED: inventory automation is disabled");
                return;
            }
            if ("send_chat".equals(cmd.cmd()) && !ServerPolicy.chatAutomationAllowed()) {
                sendResponse(cmd.id(), false, "POLICY_DISABLED: chat automation is disabled");
                return;
            }
            if (diggingPos != null && HAND_ACTION_COMMANDS.contains(cmd.cmd())) {
                sendResponse(cmd.id(), false, "HANDS_BUSY: a mining operation owns the hands channel");
                return;
            }
            switch (cmd.cmd()) {
                case "control_external" -> handleControlExternal(cmd);
                case "control_builtin" -> handleControlBuiltin(cmd);
                case "move_to" -> handleMoveTo(cmd);
                case "look_at" -> handleLookAt(cmd);
                case "jump" -> {
                    if (jumpCooldown <= 0 && mc.player.onGround()) {
                        mc.player.jumpFromGround();
                        jumpCooldown = JUMP_COOLDOWN_TICKS;
                        sendResponse(cmd.id(), true, "Jumped");
                    } else {
                        sendResponse(cmd.id(), false, mc.player.onGround()
                            ? "Jump on cooldown (" + jumpCooldown + " ticks)" : "Jump requires ground contact");
                    }
                }
                case "sneak" -> { mc.player.setShiftKeyDown(cmd.args() != null && cmd.args().has("sneak") ? cmd.args().get("sneak").getAsBoolean() : true); sendResponse(cmd.id(), true, "Sneak"); }
                case "sprint" -> { mc.player.setSprinting(cmd.args() == null || !cmd.args().has("sprint") || cmd.args().get("sprint").getAsBoolean()); sendResponse(cmd.id(), true, "Sprint"); }
                case "send_chat" -> handleSendChat(cmd);
                case "use_item" -> { mc.gameMode.useItem(mc.player, InteractionHand.MAIN_HAND); sendResponse(cmd.id(), true, "Item used"); }
                case "select_hotbar" -> handleSelectHotbar(cmd);
                case "attack" -> handleAttack(cmd);
                case "stop_digging" -> handleStopDigging(cmd);
                case "get_state" -> handleGetState(cmd);
                case "shutdown" -> handleShutdown();
                case "set_control_state" -> handleSetControlState(cmd);
                case "auto_equip" -> handleAutoEquip(cmd);
                case "get_inventory" -> handleGetInventory(cmd);
                case "get_recipes" -> handleGetRecipes(cmd);
                case "stop_all" -> handleStopAll(cmd);
                case "cancel_operation" -> handleCancelOperation(cmd);
                // AI/User control toggle
                case "toggle_ai" -> {
                    toggleAiControl();
                    sendResponse(cmd.id(), true, "AI=" + isAiControlled());
                }
                // Container interaction (mineflayer-style)
                case "use_on" -> handleUseOn(cmd);       // right-click block/entity
                case "interact_block_at" -> handleInteractBlockAt(cmd);
                case "mine_block_at" -> handleMineBlockAt(cmd);
                case "equip_item" -> handleEquipItem(cmd);
                case "get_container" -> handleGetContainer(cmd);
                case "inventory_click" -> handleInventoryClick(cmd);
                case "container_button" -> handleContainerButton(cmd);
                case "place_recipe" -> handlePlaceRecipe(cmd);
                case "take_item" -> handleTakeItem(cmd);
                case "put_item" -> handlePutItem(cmd);
                case "close_container" -> handleCloseContainer(cmd);
                case "look_at_entity" -> handleLookAtEntity(cmd);
                case "use_on_entity" -> handleUseOnEntity(cmd);
                case "behavior_enable" -> {
                    if (ClientBodyRuntime.BEHAVIORS != null) {
                        ClientBodyRuntime.BEHAVIORS.setEnabled(true);
                    }
                    JavaAutonomousBehavior.setEnabled(true);
                    sendResponse(cmd.id(), true, "behaviors=true");
                    sendBehaviorState(true);
                }
                case "toggle_behavior" -> {
                    if (ClientBodyRuntime.BEHAVIORS != null) {
                        boolean newState = !ClientBodyRuntime.BEHAVIORS.isEnabled();
                        ClientBodyRuntime.BEHAVIORS.setEnabled(newState);
                        JavaAutonomousBehavior.setEnabled(newState);
                        sendResponse(cmd.id(), true, "behaviors=" + newState);
                        // Send state update to backend
                        sendBehaviorState(newState);
                    }
                }
                case "behavior_disable" -> { 
                    stopAllRuntime();
                    if (ClientBodyRuntime.BEHAVIORS != null) {
                        ClientBodyRuntime.BEHAVIORS.setEnabled(false);
                    }
                    JavaAutonomousBehavior.setEnabled(false);
                    sendResponse(cmd.id(), true, "behaviors=false");
                    sendBehaviorState(false);
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
        double dx = args.get("x").getAsDouble() - p.getX();
        double dy = (args.has("y") ? args.get("y").getAsDouble() : p.getEyeY()) - p.getEyeY();
        double dz = args.get("z").getAsDouble() - p.getZ();
        double dist = Math.sqrt(dx * dx + dz * dz);
        if (dist > 0.01) {
            float yaw = (float) Math.toDegrees(Math.atan2(-dx, dz));
            float pitch = (float) Math.toDegrees(-Math.asin(dy / Math.sqrt(dx*dx + dy*dy + dz*dz)));
            p.setYRot(yaw);
            p.setXRot(pitch);
            var conn = mc.getConnection();
            if (conn != null) conn.send(new ServerboundMovePlayerPacket.Rot(yaw, pitch, p.onGround()));
        }
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
            mc.player.connection.sendChat(msg);
            sendResponse(cmd.id(), true, "Chat sent");
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
            Minecraft.getInstance().player.getInventory().selected = idx;
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

        if (!ServerPolicy.automatedCombatAllowed()) {
            sendResponse(cmd.id(), false, "POLICY_DISABLED: automated combat is disabled");
            return;
        }
        if (mc.player.getAttackStrengthScale(0.5f) < 0.9f) {
            sendResponse(cmd.id(), false, "Attack cooldown is not ready");
            return;
        }
        Entity target = findEntityTarget(mc, cmd);
        if (target != null) {
            mc.gameMode.attack(mc.player, target);
            mc.player.swing(InteractionHand.MAIN_HAND);
            sendResponse(cmd.id(), true, "Attacked " + target.getName().getString());
        } else {
            // Swing anyway
            mc.player.swing(InteractionHand.MAIN_HAND);
            sendResponse(cmd.id(), false, "No target found");
        }
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
                p.addProperty("name", entry.getProfile().getName());
                p.addProperty("uuid", entry.getProfile().getId().toString());
                players.add(p);
            }
        }
        state.add("online_players", players);

        // Movement status
        state.addProperty("moving", MovementSystem.isMoving());
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
        state.addProperty("behaviors_enabled", JavaAutonomousBehavior.isEnabled()
                && (ClientBodyRuntime.BEHAVIORS == null || ClientBodyRuntime.BEHAVIORS.isEnabled()));

        sendResponse(cmd.id(), true, state);
    }

    private void handleInteract(WireServer.WireCommand cmd) {
        var mc = Minecraft.getInstance();
        if (mc.player == null || mc.gameMode == null) {
            sendResponse(cmd.id(), false, "No player");
            return;
        }
        if (pendingCraftReqId != null || pendingCollectReqId != null || pendingEatReqId != null || followReqId != null) {
            sendResponse(cmd.id(), false, "CONFLICT: another foreground operation is active");
            return;
        }
        // Right-click the block the player is looking at
        var hit = mc.hitResult;
        if (hit != null && hit.getType() == net.minecraft.world.phys.HitResult.Type.BLOCK) {
            var blockHit = (net.minecraft.world.phys.BlockHitResult) hit;
            mc.gameMode.useItemOn(mc.player, InteractionHand.MAIN_HAND, blockHit);
            sendResponse(cmd.id(), true, "Interacted");
        } else {
            sendResponse(cmd.id(), false, "No block targeted; use use_item for held-item use");
        }
    }

    private void handleMine(WireServer.WireCommand cmd) {
        var mc = Minecraft.getInstance();
        if (mc.player == null || mc.gameMode == null) {
            sendResponse(cmd.id(), false, "No player/gameMode");
            return;
        }
        if (diggingReqId != null) {
            sendResponse(cmd.id(), false, "CONFLICT: another mining operation is active");
            return;
        }
        var hit = mc.hitResult;
        if (hit != null && hit.getType() == net.minecraft.world.phys.HitResult.Type.BLOCK) {
            var blockHit = (net.minecraft.world.phys.BlockHitResult) hit;
            BlockPos pos = blockHit.getBlockPos();
            Direction dir = blockHit.getDirection();

            // Auto-equip best tool (mineflayer-style)
            if (ServerPolicy.inventoryAutomationAllowed()) {
                autoEquipForBlock(mc);
            }

            // Start digging — track for continuous ticks
            mc.gameMode.startDestroyBlock(pos, dir);
            mc.player.swing(InteractionHand.MAIN_HAND);
            diggingPos = pos;
            diggingDir = dir;
            diggingTicks = 0;
            diggingReqId = cmd.id();
            LCUMod.LOGGER.info("[Action] Started digging {} with tool-slot {}", pos, mc.player.getInventory().selected);
            sendResponse(cmd.id(), true, "Digging " + pos.toShortString());
        } else {
            sendResponse(cmd.id(), false, "No block targeted");
        }
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
        if (bestSlot >= 0 && bestSlot != inv.selected) {
            inv.selected = bestSlot;
            LCUMod.LOGGER.debug("[Action] Auto-equipped slot {} for mining", bestSlot);
        }
    }

    private void handleStopDigging(WireServer.WireCommand cmd) {
        stopDigging("cancelled", "STOP_DIGGING", "digging stopped");
        sendResponse(cmd.id(), true, "Digging stopped");
    }

    private Entity findEntityTarget(Minecraft mc, WireServer.WireCommand cmd) {
        if (!(mc.hitResult instanceof EntityHitResult entityHit)) return null;
        Entity target = entityHit.getEntity();
        if (!target.isAlive() || !mc.player.hasLineOfSight(target)) return null;
        var args = cmd.args();
        if (args != null && args.has("entity_id")) {
            return target.getId() == args.get("entity_id").getAsInt() ? target : null;
        }
        return target;
    }

    private void handleInteractBlockAt(WireServer.WireCommand cmd) {
        var mc = Minecraft.getInstance();
        BlockPos pos = parseBlockPos(cmd.args());
        if (mc.player == null || mc.gameMode == null || pos == null) {
            sendResponse(cmd.id(), false, "Need player, game mode, and x/y/z");
            return;
        }
        BlockHitResult blockHit = targetedBlockHit(mc, pos);
        if (blockHit == null) {
            sendResponse(cmd.id(), false, "Block must be the current unobstructed crosshair target");
            return;
        }
        Direction face = parseDirection(cmd.args());
        if (cmd.args().has("face") && face == null) {
            sendResponse(cmd.id(), false, "Invalid block face");
            return;
        }
        if (face != null && blockHit.getDirection() != face) {
            sendResponse(cmd.id(), false, "Requested face is not the current targeted face");
            return;
        }
        mc.gameMode.useItemOn(mc.player, InteractionHand.MAIN_HAND, blockHit);
        sendResponse(cmd.id(), true, "Interacting with " + pos.toShortString());
    }

    private void handleMineBlockAt(WireServer.WireCommand cmd) {
        var mc = Minecraft.getInstance();
        BlockPos pos = parseBlockPos(cmd.args());
        if (mc.player == null || mc.gameMode == null || pos == null) {
            sendResponse(cmd.id(), false, "Need player, game mode, and x/y/z");
            return;
        }
        if (diggingReqId != null) {
            sendResponse(cmd.id(), false, "CONFLICT: another mining operation is active");
            return;
        }
        BlockHitResult blockHit = targetedBlockHit(mc, pos);
        if (blockHit == null || mc.level.getBlockState(pos).isAir()) {
            sendResponse(cmd.id(), false, "Block must be the current unobstructed crosshair target");
            return;
        }
        Direction face = parseDirection(cmd.args());
        if (cmd.args().has("face") && face == null) {
            sendResponse(cmd.id(), false, "Invalid block face");
            return;
        }
        if (ServerPolicy.inventoryAutomationAllowed()) {
            autoEquipForBlock(mc, pos);
        }
        if (face != null && blockHit.getDirection() != face) {
            sendResponse(cmd.id(), false, "Requested face is not the current targeted face");
            return;
        }
        mc.gameMode.startDestroyBlock(pos, blockHit.getDirection());
        mc.player.swing(InteractionHand.MAIN_HAND);
        diggingPos = pos;
        diggingDir = blockHit.getDirection();
        diggingTicks = 0;
        diggingReqId = cmd.id();
        sendResponse(cmd.id(), true, "Digging " + pos.toShortString());
    }

    private void handleEquipItem(WireServer.WireCommand cmd) {
        var args = cmd.args();
        if (args == null || !args.has("item")) {
            sendResponse(cmd.id(), false, "Need item");
            return;
        }
        String itemId = args.get("item").getAsString();
        var inv = Minecraft.getInstance().player.getInventory();
        for (int slot = 0; slot < 9; slot++) {
            var stack = inv.getItem(slot);
            if (!stack.isEmpty() && CraftingPlanner.matchesItemId(
                BuiltInRegistries.ITEM.getKey(stack.getItem()).toString(), itemId)) {
                inv.selected = slot;
                sendResponse(cmd.id(), true, "Equipped in main hand: " + itemId);
                return;
            }
        }
        sendResponse(cmd.id(), false, "Item is not in hotbar: " + itemId);
    }

    private BlockPos parseBlockPos(JsonObject args) {
        if (args == null || !args.has("x") || !args.has("y") || !args.has("z")) return null;
        return new BlockPos(args.get("x").getAsInt(), args.get("y").getAsInt(), args.get("z").getAsInt());
    }

    private Direction parseDirection(JsonObject args) {
        if (args != null && args.has("face")) {
            try { return Direction.valueOf(args.get("face").getAsString().toUpperCase()); }
            catch (IllegalArgumentException ignored) { return null; }
        }
        return null;
    }

    private BlockHitResult targetedBlockHit(Minecraft mc, BlockPos pos) {
        if (!(mc.hitResult instanceof BlockHitResult blockHit)) return null;
        return blockHit.getBlockPos().equals(pos) ? blockHit : null;
    }

    private void stopDigging() {
        stopDigging(null, null, null);
    }

    private void stopDigging(String status, String code, String message) {
        if (diggingPos != null) {
            var mc = Minecraft.getInstance();
            if (mc.gameMode != null) {
                mc.gameMode.stopDestroyBlock();
            }
            LCUMod.LOGGER.info("[Action] Stopped digging {}", diggingPos);
            diggingPos = null;
            diggingDir = null;
            diggingTicks = 0;
            if (status != null) sendOperationOutcome(diggingReqId, status, code, message);
            diggingReqId = null;
        }
    }

    private void handleContinuousDigging(Minecraft mc) {
        if (diggingPos == null) return;

        diggingTicks++;
        if (diggingTicks > DIGGING_TIMEOUT_TICKS) {
            LCUMod.LOGGER.warn("[Action] Digging timeout at {}", diggingPos);
            stopDigging("failed", "DIG_TIMEOUT", "digging timed out");
            return;
        }

        // Check if block still exists at position
        if (mc.level.isEmptyBlock(diggingPos)) {
            // Block was broken!
            LCUMod.LOGGER.info("[Action] Block broken at {}", diggingPos);
            stopDigging("succeeded", "BLOCK_BROKEN", "block broken");
            return;
        }

        BlockHitResult blockHit = targetedBlockHit(mc, diggingPos);
        if (blockHit == null || blockHit.getDirection() != diggingDir) {
            stopDigging("cancelled", "TARGET_LOST", "digging target is no longer under the crosshair");
            return;
        }

        // Continue digging every tick
        mc.gameMode.continueDestroyBlock(diggingPos, diggingDir);
        // Periodic swing animation (every 7 ticks ≈ 0.35s, like vanilla mining)
        if (diggingTicks % 7 == 0) {
            mc.player.swing(InteractionHand.MAIN_HAND);
        }
    }

    private void handlePlace(WireServer.WireCommand cmd) {
        var mc = Minecraft.getInstance();
        if (mc.player == null || mc.gameMode == null) {
            sendResponse(cmd.id(), false, "No player/gameMode");
            return;
        }
        var hit = mc.hitResult;
        if (hit != null && hit.getType() == net.minecraft.world.phys.HitResult.Type.BLOCK) {
            var blockHit = (net.minecraft.world.phys.BlockHitResult) hit;
            mc.gameMode.useItemOn(mc.player, InteractionHand.MAIN_HAND, blockHit);
            sendResponse(cmd.id(), true, "Placed");
        } else {
            sendResponse(cmd.id(), false, "No block targeted");
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
                if (state && jumpCooldown <= 0 && mc.player.onGround()) {
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

        if (bestSlot >= 0 && bestSlot != inv.selected) {
            inv.selected = bestSlot;
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
        result.addProperty("selected", inv.selected);
        sendResponse(cmd.id(), true, result);
    }

    private void handleStopAll(WireServer.WireCommand cmd) {
        stopAllRuntime();
        sendResponse(cmd.id(), true, "All stopped");
    }

    private void handleCancelOperation(WireServer.WireCommand cmd) {
        var args = cmd.args();
        if (args == null || !args.has("operation_id")) {
            sendResponse(cmd.id(), false, "Need operation_id");
            return;
        }

        String operationId = args.get("operation_id").getAsString();
        boolean cancelled = Pathfinder.cancelOperation(operationId, "operation cancelled by controller");
        if (operationId.equals(diggingReqId)) {
            stopDigging("cancelled", "CANCELLED", "digging cancelled by controller");
            cancelled = true;
        }
        if (operationId.equals(pendingCraftReqId)) {
            sendOperationOutcome(operationId, "cancelled", "CANCELLED", "craft cancelled by controller");
            finishCraftTask();
            cancelled = true;
        } else if (operationId.equals(pendingCollectReqId)) {
            sendOperationOutcome(operationId, "cancelled", "CANCELLED", "collection cancelled by controller");
            clearPendingCollectTask();
            clearTaskState();
            cancelled = true;
        }
        if (operationId.equals(pendingEatReqId)) {
            sendOperationOutcome(operationId, "cancelled", "CANCELLED", "eat cancelled by controller");
            pendingEatReqId = null;
            pendingEatTicks = 0;
            pendingEatAttempts = 0;
            clearTaskState();
            cancelled = true;
        }
        if (operationId.equals(followReqId)) {
            followTargetName = null;
            followReqId = null;
            MovementSystem.stop();
            sendOperationOutcome(operationId, "cancelled", "CANCELLED", "follow cancelled by controller");
            clearTaskState();
            cancelled = true;
        }
        sendBehaviorSnapshot();
        sendResponse(cmd.id(), cancelled, cancelled ? "Operation cancelled" : "Operation is not active");
    }

    private void stopAllRuntime() {
        var mc = Minecraft.getInstance();
        cancelActiveOperations("STOP_ALL", "all operations stopped");
        followTargetName = null;
        followReqId = null;
        suspendedFollowTargetName = null;
        pendingCraftItem = null;
        pendingCraftReqId = null;
        pendingCraftTicks = 0;
        pendingCraftAttempts = 0;
        pendingCraftNoProgressAttempts = 0;
        clearPendingCraftOutput();
        pendingCraftProcessingStallTicks = 0;
        clearPendingCraftStation();
        pendingCraftGoalCount = 1;
        pendingEatReqId = null;
        pendingEatTicks = 0;
        pendingEatAttempts = 0;
        clearPendingCollectTask();
        Pathfinder.cancelActiveOperation("STOP_ALL", "all operations stopped");
        MovementSystem.stop();
        JavaAutonomousBehavior.resetCurrentState();
        releaseAllInputs();
        if (mc.player != null) {
            if (mc.gameMode != null) stopDigging();
            if (mc.gameMode != null && mc.player.isUsingItem()) {
                mc.gameMode.releaseUsingItem(mc.player);
            }
            if (mc.player.containerMenu != null && mc.player.containerMenu != mc.player.inventoryMenu) {
                mc.player.closeContainer();
            }
        }
        clearTaskState();
        sendBehaviorSnapshot();
    }

    private void cancelActiveOperations(String code, String message) {
        java.util.Set<String> requestIds = new java.util.HashSet<>();
        if (diggingReqId != null) requestIds.add(diggingReqId);
        if (followReqId != null) requestIds.add(followReqId);
        if (pendingCraftReqId != null) requestIds.add(pendingCraftReqId);
        if (pendingCollectReqId != null) requestIds.add(pendingCollectReqId);
        if (pendingEatReqId != null) requestIds.add(pendingEatReqId);
        for (String requestId : requestIds) {
            sendOperationOutcome(requestId, "cancelled", code, message);
        }
    }

    private static void sendOperationOutcome(String requestId, String status, String code, String message) {
        if (requestId != null && LCUMod.WIRE != null) {
            LCUMod.WIRE.sendOutcome(requestId, status, code, message);
        }
    }

    private boolean requiresArmedBody(String command) {
        return switch (command) {
            case "control_external", "control_builtin", "get_state", "get_inventory", "get_container",
                 "stop_all", "cancel_operation", "toggle_ai", "behavior_disable", "send_chat" -> false;
            default -> true;
        };
    }

    private long commandFencingToken(WireServer.WireCommand cmd) {
        var args = cmd.args();
        if (args == null || !args.has("__lcu_fencing_token")) return 0;
        try {
            return args.get("__lcu_fencing_token").getAsLong();
        } catch (Exception ignored) {
            return 0;
        }
    }

    private void handleControlExternal(WireServer.WireCommand cmd) {
        long token = commandFencingToken(cmd);
        if (token <= 0 || token < activeFencingToken) {
            sendResponse(cmd.id(), false, "Stale control fencing token");
            return;
        }
        if (!externalControlActive || token > activeFencingToken) {
            behaviorEnabledBeforeExternal = JavaAutonomousBehavior.isEnabled()
                    && (ClientBodyRuntime.BEHAVIORS == null || ClientBodyRuntime.BEHAVIORS.isEnabled());
        }
        activeFencingToken = token;
        externalControlActive = true;
        if (ClientBodyRuntime.BEHAVIORS != null) ClientBodyRuntime.BEHAVIORS.setEnabled(false);
        JavaAutonomousBehavior.setEnabled(false);
        HumanLikeBehavior.reset();
        ActivitySignalController.reset();
        handleStopAll(cmd);
    }

    private void handleControlBuiltin(WireServer.WireCommand cmd) {
        long token = commandFencingToken(cmd);
        if (!externalControlActive) {
            sendResponse(cmd.id(), true, "Built-in control already active");
            return;
        }
        if (token != activeFencingToken) {
            sendResponse(cmd.id(), false, "Stale control fencing token");
            return;
        }
        handleStopAll(cmd);
        externalControlActive = false;
        if (ClientBodyRuntime.BEHAVIORS != null) ClientBodyRuntime.BEHAVIORS.setEnabled(behaviorEnabledBeforeExternal);
        JavaAutonomousBehavior.setEnabled(behaviorEnabledBeforeExternal);
        HumanLikeBehavior.reset();
        ActivitySignalController.reset();
        sendBehaviorSnapshot();
    }

    // ── Container Interaction (mineflayer-style) ───────────────

    private void handleUseOn(WireServer.WireCommand cmd) {
        // Right-click whatever player is looking at (block or entity)
        var mc = Minecraft.getInstance();
        if (mc.player == null || mc.gameMode == null) {
            sendResponse(cmd.id(), false, "No player");
            return;
        }
        var hit = mc.hitResult;
        if (hit instanceof net.minecraft.world.phys.BlockHitResult blockHit) {
            mc.gameMode.useItemOn(mc.player, InteractionHand.MAIN_HAND, blockHit);
            sendResponse(cmd.id(), true, "Right-clicked block");
        } else if (hit instanceof net.minecraft.world.phys.EntityHitResult entityHit) {
            mc.gameMode.interact(mc.player, entityHit.getEntity(), InteractionHand.MAIN_HAND);
            sendResponse(cmd.id(), true, "Right-clicked entity");
        } else {
            sendResponse(cmd.id(), false, "No block/entity targeted; use use_item for held-item use");
        }
    }

    private void handleGetContainer(WireServer.WireCommand cmd) {
        // Read contents of the currently open container/chest
        var mc = Minecraft.getInstance();
        if (mc.player == null) { sendResponse(cmd.id(), false, "No player"); return; }

        var menu = mc.player.containerMenu;
        if (menu == null || menu == mc.player.inventoryMenu) { sendResponse(cmd.id(), false, "No container open"); return; }

        // Send the container's items in the response
        JsonObject result = new JsonObject();
        result.addProperty("container_id", menu.containerId);
        result.addProperty("menu_class", menu.getClass().getName());
        result.addProperty("menu_adapter", menuAdapterName(menu));
        result.addProperty("state_id", menu.getStateId());
        result.addProperty("slots", menu.slots.size());
        var carried = menu.getCarried();
        if (!carried.isEmpty()) {
            JsonObject carriedItem = new JsonObject();
            carriedItem.addProperty("name", BuiltInRegistries.ITEM.getKey(carried.getItem()).toString());
            carriedItem.addProperty("count", carried.getCount());
            result.add("carried", carriedItem);
        }

        JsonArray items = new JsonArray();
        for (int i = 0; i < menu.slots.size(); i++) {
            var stack = menu.slots.get(i).getItem();
            JsonObject item = new JsonObject();
            item.addProperty("slot", i);
            boolean playerSlot = menu.slots.get(i).container == mc.player.getInventory();
            item.addProperty("scope", playerSlot ? "player" : "storage");
            item.addProperty("menu_scope", playerSlot ? "player" : "menu");
            item.addProperty("role", playerSlot ? "player_inventory" : menuSlotRole(menu, i));
            item.addProperty("empty", stack.isEmpty());
            if (!stack.isEmpty()) {
                item.addProperty("name", BuiltInRegistries.ITEM.getKey(stack.getItem()).toString());
                item.addProperty("count", stack.getCount());
                item.addProperty("display", stack.getDisplayName().getString());
            }
            items.add(item);
        }
        result.add("items", items);
        sendResponse(cmd.id(), true, result);
    }

    private String menuAdapterName(net.minecraft.world.inventory.AbstractContainerMenu menu) {
        String name = menu.getClass().getSimpleName().toLowerCase();
        if (name.contains("blastfurnace")) return "blast_furnace";
        if (name.contains("furnace")) return "furnace";
        if (name.contains("smoker")) return "smoker";
        if (name.contains("stonecutter")) return "stonecutter";
        if (name.contains("smithing")) return "smithing";
        if (name.contains("brewing")) return "brewing_stand";
        if (name.contains("anvil")) return "anvil";
        if (name.contains("grindstone")) return "grindstone";
        if (name.contains("loom")) return "loom";
        if (name.contains("cartography")) return "cartography";
        if (name.contains("enchantment")) return "enchanting";
        if (name.contains("merchant")) return "merchant";
        if (name.contains("crafting")) return "crafting_table";
        if (name.contains("shulker")) return "shulker_box";
        if (name.contains("chest")) return "container";
        return "generic";
    }

    private String menuSlotRole(net.minecraft.world.inventory.AbstractContainerMenu menu, int slot) {
        return switch (menuAdapterName(menu)) {
            case "crafting_table" -> slot == 0 ? "result" : slot <= 9 ? "crafting_grid" : "menu";
            case "furnace", "blast_furnace", "smoker" -> switch (slot) {
                case 0 -> "input";
                case 1 -> "fuel";
                case 2 -> "result";
                default -> "menu";
            };
            case "stonecutter" -> slot == 0 ? "input" : slot == 1 ? "result" : "menu";
            case "smithing" -> switch (slot) {
                case 0 -> "template";
                case 1 -> "base";
                case 2 -> "addition";
                case 3 -> "result";
                default -> "menu";
            };
            case "brewing_stand" -> slot <= 2 ? "bottle" : slot == 3 ? "ingredient" : slot == 4 ? "fuel" : "menu";
            case "anvil", "grindstone", "cartography" -> slot <= 1 ? "input" : slot == 2 ? "result" : "menu";
            case "loom" -> switch (slot) {
                case 0 -> "banner";
                case 1 -> "dye";
                case 2 -> "pattern";
                case 3 -> "result";
                default -> "menu";
            };
            case "enchanting" -> slot == 0 ? "item" : slot == 1 ? "lapis" : "menu";
            case "merchant" -> slot <= 1 ? "payment" : slot == 2 ? "result" : "menu";
            case "container", "shulker_box", "generic" -> "storage";
            default -> "menu";
        };
    }

    private void handleGetRecipes(WireServer.WireCommand cmd) {
        var args = cmd.args();
        var mc = Minecraft.getInstance();
        if (mc.level == null || args == null || !args.has("item")) {
            sendResponse(cmd.id(), false, "Need item and loaded world");
            return;
        }
        String target = args.get("item").getAsString();
        JsonArray recipes = new JsonArray();
        for (RecipeHolder<?> holder : mc.level.getRecipeManager().getRecipes()) {
            var resultStack = holder.value().getResultItem(mc.level.registryAccess());
            if (resultStack.isEmpty()) continue;
            String resultId = BuiltInRegistries.ITEM.getKey(resultStack.getItem()).toString();
            if (!CraftingPlanner.matchesItemId(resultId, target)) continue;

            JsonObject recipe = new JsonObject();
            recipe.addProperty("recipe_id", holder.id().toString());
            recipe.addProperty("recipe_type", BuiltInRegistries.RECIPE_TYPE.getKey(holder.value().getType()).toString());
            recipe.addProperty("result_item", resultId);
            recipe.addProperty("result_count", resultStack.getCount());
            recipe.addProperty("crafting_table_required",
                holder.value().getType() == RecipeType.CRAFTING && !holder.value().canCraftInDimensions(2, 2));
            JsonArray ingredients = new JsonArray();
            holder.value().getIngredients().forEach(ingredient -> {
                if (ingredient == null || ingredient.isEmpty()) return;
                JsonArray alternatives = new JsonArray();
                for (var stack : ingredient.getItems()) {
                    if (!stack.isEmpty()) alternatives.add(BuiltInRegistries.ITEM.getKey(stack.getItem()).toString());
                }
                ingredients.add(alternatives);
            });
            recipe.add("ingredients", ingredients);
            recipes.add(recipe);
            if (recipes.size() >= 128) break;
        }
        JsonObject result = new JsonObject();
        result.addProperty("item", target);
        result.add("recipes", recipes);
        sendResponse(cmd.id(), true, result);
    }

    private void handleInventoryClick(WireServer.WireCommand cmd) {
        var args = cmd.args();
        var mc = Minecraft.getInstance();
        if (mc.player == null || mc.gameMode == null || args == null
            || !args.has("container_id") || !args.has("expected_state_id") || !args.has("slot") || !args.has("click_type")) {
            sendResponse(cmd.id(), false, "Need container_id, expected_state_id, slot, and click_type");
            return;
        }
        var menu = mc.player.containerMenu;
        int containerId = args.get("container_id").getAsInt();
        int slot = args.get("slot").getAsInt();
        int button = args.has("button") ? args.get("button").getAsInt() : 0;
        if (menu == null || menu.containerId != containerId || slot < 0 || slot >= menu.slots.size()
            || !matchesExpectedMenuState(args, menu)) {
            sendResponse(cmd.id(), false, "Stale container or invalid slot");
            return;
        }
        ClickType clickType;
        try {
            clickType = ClickType.valueOf(args.get("click_type").getAsString().toUpperCase());
        } catch (IllegalArgumentException exception) {
            sendResponse(cmd.id(), false, "Unsupported click_type");
            return;
        }
        if (!java.util.Set.of(ClickType.PICKUP, ClickType.QUICK_MOVE, ClickType.SWAP, ClickType.THROW).contains(clickType)) {
            sendResponse(cmd.id(), false, "click_type is not allowed");
            return;
        }
        if (clickType == ClickType.SWAP && (button < 0 || button > 8)) {
            sendResponse(cmd.id(), false, "SWAP button must be hotbar index 0-8");
            return;
        }
        mc.gameMode.handleInventoryMouseClick(containerId, slot, button, clickType, mc.player);
        sendResponse(cmd.id(), true, "Inventory click sent");
    }

    private void handleContainerButton(WireServer.WireCommand cmd) {
        var args = cmd.args();
        var mc = Minecraft.getInstance();
        if (mc.player == null || mc.gameMode == null || args == null
            || !args.has("container_id") || !args.has("expected_state_id") || !args.has("button_id")) {
            sendResponse(cmd.id(), false, "Need container_id, expected_state_id, and button_id");
            return;
        }
        int containerId = args.get("container_id").getAsInt();
        if (mc.player.containerMenu == null || mc.player.containerMenu.containerId != containerId
            || !matchesExpectedMenuState(args, mc.player.containerMenu)) {
            sendResponse(cmd.id(), false, "Stale container id");
            return;
        }
        mc.gameMode.handleInventoryButtonClick(containerId, args.get("button_id").getAsInt());
        sendResponse(cmd.id(), true, "Container button sent");
    }

    private void handlePlaceRecipe(WireServer.WireCommand cmd) {
        var args = cmd.args();
        var mc = Minecraft.getInstance();
        if (mc.player == null || mc.level == null || mc.gameMode == null || args == null
            || !args.has("container_id") || !args.has("expected_state_id") || !args.has("recipe_id")) {
            sendResponse(cmd.id(), false, "Need container_id, expected_state_id, and recipe_id");
            return;
        }
        int containerId = args.get("container_id").getAsInt();
        if (mc.player.containerMenu == null || mc.player.containerMenu.containerId != containerId
            || !matchesExpectedMenuState(args, mc.player.containerMenu)) {
            sendResponse(cmd.id(), false, "Stale container id");
            return;
        }
        String recipeId = args.get("recipe_id").getAsString();
        RecipeHolder<?> recipe = mc.level.getRecipeManager().getRecipes().stream()
            .filter(candidate -> candidate.id().toString().equals(recipeId))
            .findFirst().orElse(null);
        if (recipe == null) {
            sendResponse(cmd.id(), false, "Unknown recipe: " + recipeId);
            return;
        }
        boolean craftAll = args.has("craft_all") && args.get("craft_all").getAsBoolean();
        mc.gameMode.handlePlaceRecipe(containerId, recipe, craftAll);
        sendResponse(cmd.id(), true, "Recipe placement sent: " + recipeId);
    }

    private void handleTakeItem(WireServer.WireCommand cmd) {
        // Take item from container slot (shift-click to player inventory)
        var args = cmd.args();
        if (args == null || !args.has("container_id") || !args.has("slot") || !args.has("expected_state_id")) {
            sendResponse(cmd.id(), false, "Need container_id, expected_state_id, and slot number");
            return;
        }
        var mc = Minecraft.getInstance();
        if (mc.player == null || mc.gameMode == null) {
            sendResponse(cmd.id(), false, "No player");
            return;
        }
        var menu = mc.player.containerMenu;
        if (menu == null || menu == mc.player.inventoryMenu) { sendResponse(cmd.id(), false, "No container open"); return; }

        int containerId = args.get("container_id").getAsInt();
        int slot = args.get("slot").getAsInt();
        if (containerId != menu.containerId || !matchesExpectedMenuState(args, menu)) {
            sendResponse(cmd.id(), false, "Stale container id");
            return;
        }
        if (slot < 0 || slot >= menu.slots.size()
            || menu.slots.get(slot).container == mc.player.getInventory()
            || !menu.slots.get(slot).mayPickup(mc.player)) {
            sendResponse(cmd.id(), false, "Invalid slot " + slot);
            return;
        }

        // Shift-click to transfer item quickly (button=1, clickType=QUICK_MOVE)
        mc.gameMode.handleInventoryMouseClick(menu.containerId, slot, 0,
            net.minecraft.world.inventory.ClickType.QUICK_MOVE, mc.player);
        sendResponse(cmd.id(), true, "Taking from slot " + slot);
    }

    private void handlePutItem(WireServer.WireCommand cmd) {
        // Put item from player inventory into container (shift-click)
        var args = cmd.args();
        if (args == null || !args.has("container_id") || !args.has("slot") || !args.has("expected_state_id")) {
            sendResponse(cmd.id(), false, "Need container_id, expected_state_id, and slot number");
            return;
        }
        var mc = Minecraft.getInstance();
        if (mc.player == null || mc.gameMode == null) {
            sendResponse(cmd.id(), false, "No player");
            return;
        }
        var menu = mc.player.containerMenu;
        if (menu == null || menu == mc.player.inventoryMenu) { sendResponse(cmd.id(), false, "No container open"); return; }

        int containerId = args.get("container_id").getAsInt();
        int slot = args.get("slot").getAsInt();
        if (containerId != menu.containerId || !matchesExpectedMenuState(args, menu)) {
            sendResponse(cmd.id(), false, "Stale container id");
            return;
        }
        if (slot < 0 || slot >= menu.slots.size()
            || menu.slots.get(slot).container != mc.player.getInventory()
            || !menu.slots.get(slot).mayPickup(mc.player)) {
            sendResponse(cmd.id(), false, "Invalid slot " + slot);
            return;
        }

        // Shift-click from player inventory section into container
        mc.gameMode.handleInventoryMouseClick(menu.containerId, slot, 0,
            net.minecraft.world.inventory.ClickType.QUICK_MOVE, mc.player);
        sendResponse(cmd.id(), true, "Putting into slot " + slot);
    }

    private void handleCloseContainer(WireServer.WireCommand cmd) {
        var mc = Minecraft.getInstance();
        if (mc.player != null) {
            mc.player.closeContainer();
            sendResponse(cmd.id(), true, "Container closed");
        } else {
            sendResponse(cmd.id(), false, "No player");
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
        if (entity != null && entity.isAlive() && mc.player.distanceTo(entity) <= 4.5
            && mc.player.hasLineOfSight(entity)) {
            mc.gameMode.interact(mc.player, entity, InteractionHand.MAIN_HAND);
            sendResponse(cmd.id(), true, "Interacted with entity " + entityId);
        } else {
            sendResponse(cmd.id(), false, "Entity " + entityId + " is missing, blocked, or out of reach");
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
        if (followReqId != null) {
            sendResponse(cmd.id(), false, "CONFLICT: follow operation already active");
            return;
        }
        if (pendingEatReqId != null || pendingCollectReqId != null) {
            sendResponse(cmd.id(), false, "CONFLICT: inventory or collection operation is active");
            return;
        }
        if (pendingCraftItem != null) {
            sendResponse(cmd.id(), false, "CONFLICT: craft operation currently owns movement and inventory");
            return;
        }
        Pathfinder.stop();
        MovementSystem.stop();
        JavaAutonomousBehavior.resetCurrentState();
        followTargetName = playerName;
        followReqId = cmd.id();
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
        followReqId = null;
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
        String itemName = normalizeCraftItemName(args.get("item").getAsString());
        int requestedCount = args.has("count") ? Math.max(1, args.get("count").getAsInt()) : 1;

        if (pendingCraftItem != null && countInventoryItem(mc, pendingCraftItem) < pendingCraftGoalCount) {
            sendResponse(cmd.id(), false, "CONFLICT: already crafting " + pendingCraftItem);
            return;
        }
        if (pendingEatReqId != null || pendingCollectReqId != null) {
            sendResponse(cmd.id(), false, "CONFLICT: inventory or collection operation is active");
            return;
        }

        if (followTargetName != null) {
            suspendedFollowTargetName = followTargetName;
            followTargetName = null;
            Pathfinder.stop();
            MovementSystem.stop();
        }

        pendingCraftItem = itemName;
        pendingCraftReqId = cmd.id();
        pendingCraftTicks = 0;
        pendingCraftAttempts = 0;
        pendingCraftNoProgressAttempts = 0;
        clearPendingCraftOutput();
        pendingCraftProcessingStallTicks = 0;
        clearPendingCraftStation();
        lastCraftPlanDetail = "queued";
        int baselineCount = countInventoryItem(mc, itemName);
        pendingCraftGoalCount = (int) Math.min(Integer.MAX_VALUE, (long) baselineCount + requestedCount);
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
        if (pendingCraftReqId != null || pendingCollectReqId != null || pendingEatReqId != null || followReqId != null) {
            sendResponse(cmd.id(), false, "CONFLICT: another foreground operation is active");
            return;
        }
        String blockType = normalizeItemName(args.get("block_type").getAsString());
        int count = args.has("count") ? args.get("count").getAsInt() : 1;

        startCollectTask(mc, cmd.id(), blockType, Math.max(1, count));
        setTaskState("collect", "searching", blockType, "searching nearby resources", 0.02);
        sendBehaviorSnapshot();
        sendResponse(cmd.id(), true, "Collecting " + blockType);
    }

    private void handleExplore(WireServer.WireCommand cmd) {
        sendResponse(cmd.id(), false, "UNSUPPORTED: safe exploration policy is not implemented");
    }

    private void handleTrade(WireServer.WireCommand cmd) {
        var args = cmd.args();
        if (args == null || !args.has("villager_type")) {
            sendResponse(cmd.id(), false, "Need villager type");
            return;
        }
        sendResponse(cmd.id(), false, "UNSUPPORTED: verified villager trading is not implemented");
    }

    private void handleSleep(WireServer.WireCommand cmd) {
        sendResponse(cmd.id(), false, "UNSUPPORTED: verified bed navigation and sleep are not implemented");
    }

    private void handleEat(WireServer.WireCommand cmd) {
        var mc = Minecraft.getInstance();
        if (mc.player == null || mc.gameMode == null) {
            sendResponse(cmd.id(), false, "No player");
            return;
        }
        if (pendingCraftReqId != null || pendingCollectReqId != null || pendingEatReqId != null || followReqId != null) {
            sendResponse(cmd.id(), false, "CONFLICT: another foreground operation is active");
            return;
        }

        if (!hasFoodInHotbar(mc)) {
            sendResponse(cmd.id(), false, "No food in hotbar");
            return;
        }

        pendingEatReqId = cmd.id();
        pendingEatTicks = 0;
        pendingEatAttempts = 0;
        pendingEatStartHunger = mc.player.getFoodData().getFoodLevel();
        pendingEatStartHealth = mc.player.getHealth();
        setTaskState("eat", "running", "food", "consuming food", 0.05);
        startEating(mc);
        sendBehaviorSnapshot();
        if (LCUMod.WIRE != null) {
            LCUMod.WIRE.sendProgress(cmd.id(), 0.05, "eat queued");
        }
        sendResponse(cmd.id(), true, "Eating food");
    }

    private void handleDropItem(WireServer.WireCommand cmd) {
        sendResponse(cmd.id(), false,
            "UNSUPPORTED: item dropping requires an acknowledged inventory transaction");
    }

    private void handleSortInventory(WireServer.WireCommand cmd) {
        sendResponse(cmd.id(), false, "UNSUPPORTED: deterministic inventory sorting is not implemented");
    }

    private void handleBuild(WireServer.WireCommand cmd) {
        var args = cmd.args();
        if (args == null || !args.has("x") || !args.has("y") || !args.has("z") || !args.has("structure")) {
            sendResponse(cmd.id(), false, "Need x, y, z, structure");
            return;
        }
        sendResponse(cmd.id(), false, "UNSUPPORTED: protected-region-aware building is not implemented");
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
        data.addProperty("behaviors_enabled", JavaAutonomousBehavior.isEnabled()
                && (ClientBodyRuntime.BEHAVIORS == null || ClientBodyRuntime.BEHAVIORS.isEnabled()));
        data.addProperty("follow_target", followTargetName == null ? "" : followTargetName);
        data.addProperty("suspended_follow_target", suspendedFollowTargetName == null ? "" : suspendedFollowTargetName);
        data.addProperty("pending_craft_item", pendingCraftItem == null ? "" : pendingCraftItem);
        data.addProperty("pending_eat", pendingEatReqId != null);
        data.addProperty("pending_collect_item", pendingCollectItem == null ? "" : pendingCollectItem);
        data.addProperty("pending_craft_goal_count", pendingCraftGoalCount);
        data.addProperty("pending_craft_current_count", pendingCraftItem == null ? 0 : countInventoryItem(Minecraft.getInstance(), pendingCraftItem));
        data.addProperty("pending_collect_baseline_count", pendingCollectBaselineCount);
        data.addProperty("pending_collect_goal_count", pendingCollectGoalCount);
        data.addProperty("pending_collect_current_count", pendingCollectItem == null ? 0 : countInventoryItem(Minecraft.getInstance(), pendingCollectItem));
        data.addProperty("last_craft_plan", lastCraftPlanDetail);
        data.addProperty("navigating", Pathfinder.isNavigating());
        data.addProperty("active_behavior", JavaAutonomousBehavior.getState().name().toLowerCase());
        data.addProperty("movement_owner", currentMovementOwner());
        LCUMod.WIRE.sendEvent("behavior_state", data);
    }

    private boolean matchesExpectedMenuState(JsonObject args, net.minecraft.world.inventory.AbstractContainerMenu menu) {
        return args != null && args.has("expected_state_id")
            && args.get("expected_state_id").getAsInt() == menu.getStateId();
    }

    private boolean hasManualTask() {
        return followTargetName != null
                || pendingCraftItem != null
                || pendingEatReqId != null
                || pendingCollectItem != null
                || pendingStorageTargetItem != null
                || !"idle".equals(activeTaskKind);
    }

    private String currentMovementOwner() {
        if (followTargetName != null) return "follow";
        if (hasManualTask()) return "task";
        if (JavaAutonomousBehavior.getState() != JavaAutonomousBehavior.BehaviorState.IDLE) return "autonomy";
        return "none";
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
        activeTaskKind = kind;
        activeTaskStatus = status;
        activeTaskTarget = target == null ? "" : target;
        activeTaskDetail = detail == null ? "" : detail;
        activeTaskProgress = Math.max(0.0, Math.min(1.0, progress));
        sendTaskState();
    }

    private void clearTaskState() {
        activeTaskKind = "idle";
        activeTaskStatus = "idle";
        activeTaskTarget = "";
        activeTaskDetail = "";
        activeTaskProgress = 0.0;
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

        if (countInventoryItem(mc, pendingCraftItem) >= pendingCraftGoalCount) {
            if (LCUMod.WIRE != null) {
                LCUMod.WIRE.sendProgress(pendingCraftReqId, 1.0, "crafted: " + pendingCraftItem);
            }
            sendOperationOutcome(pendingCraftReqId, "succeeded", "CRAFTED", "crafted: " + pendingCraftItem);
            finishCraftTask();
            return;
        }

        if (pendingCraftAwaitingOutput) {
            tickPendingCraftOutput(mc);
            return;
        }
        if (pendingCraftNoProgressAttempts >= 3) {
            if (LCUMod.WIRE != null) {
                LCUMod.WIRE.sendProgress(pendingCraftReqId, 0.0, "craft made no inventory progress: " + pendingCraftItem);
            }
            sendOperationOutcome(pendingCraftReqId, "failed", "NO_PROGRESS", "craft made no inventory progress: " + pendingCraftItem);
            finishCraftTask();
            return;
        }

        if (pendingCollectReqId != null && pendingCollectReqId.equals(pendingCraftReqId)) {
            return;
        }

        pendingCraftTicks++;
        if (pendingCraftTicks % 10 != 0) {
            return;
        }

        CraftingPlanner.CraftPlan plan = CraftingPlanner.plan(mc, pendingCraftItem, pendingCraftGoalCount);
        lastCraftPlanDetail = plan.describe();
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
            String reason = plan.failureReason.isBlank() ? "no craft path for " + pendingCraftItem : plan.failureReason;
            if (LCUMod.WIRE != null) {
                LCUMod.WIRE.sendProgress(pendingCraftReqId, 0.0, reason);
            }
            sendOperationOutcome(pendingCraftReqId, "failed", "NO_CRAFT_PATH", reason);
            finishCraftTask();
            return;
        }

        CraftingPlanner.CraftStep step = plan.steps.get(0);

        if (step.mode.equals("craft")) {
            boolean needsTable = step.needsCraftingTable;
            if (needsTable && !isCraftingTableOpen(mc)) {
                if (!openNearbyCraftingTable(mc)) {
                    if (LCUMod.WIRE != null) {
                        LCUMod.WIRE.sendProgress(pendingCraftReqId, 0.0, "need nearby crafting table for " + pendingCraftItem);
                    }
                    sendOperationOutcome(pendingCraftReqId, "failed", "STATION_UNAVAILABLE", "need nearby crafting table for " + pendingCraftItem);
                    finishCraftTask();
                }
                return;
            }

            if (!needsTable
                && mc.player.containerMenu != mc.player.inventoryMenu
                && !(mc.player.containerMenu instanceof CraftingMenu)) {
                mc.player.closeContainer();
                return;
            }

            pendingCraftAttempts++;
            mc.gameMode.handlePlaceRecipe(mc.player.containerMenu.containerId, step.recipe, false);
            pendingCraftAwaitingOutput = true;
            pendingCraftOutputClicked = false;
            pendingCraftOutputWaitTicks = 0;
            pendingCraftOutputMenuId = mc.player.containerMenu.containerId;
            pendingCraftExpectedOutput = step.itemId;
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
                if (!openNearbyProcessingStation(mc, step.stationBlockId)) {
                    if (LCUMod.WIRE != null) {
                        LCUMod.WIRE.sendProgress(pendingCraftReqId, 0.0, "need nearby " + step.stationBlockId + " for " + pendingCraftItem);
                    }
                    sendOperationOutcome(pendingCraftReqId, "failed", "STATION_UNAVAILABLE", "need nearby " + step.stationBlockId + " for " + pendingCraftItem);
                    finishCraftTask();
                }
                return;
            }

            if (pickupProcessedOutputIfReady(mc, step.itemId)) {
                pendingCraftProcessingStallTicks = 0;
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
                pendingCraftProcessingStallTicks += 10;
                if (pendingCraftProcessingStallTicks > 400) {
                    if (LCUMod.WIRE != null) {
                        LCUMod.WIRE.sendProgress(pendingCraftReqId, 0.0, "processing station blocked by unrelated or stalled contents");
                    }
                    sendOperationOutcome(pendingCraftReqId, "failed", "STATION_BLOCKED", "processing station blocked by unrelated or stalled contents");
                    finishCraftTask();
                    return;
                }
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
            pendingCraftProcessingStallTicks = 0;
            mc.gameMode.handlePlaceRecipe(mc.player.containerMenu.containerId, step.recipe, false);
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

    }

    private void tickPendingCraftOutput(Minecraft mc) {
        pendingCraftOutputWaitTicks++;
        var menu = mc.player.containerMenu;
        if (menu == null || menu.containerId != pendingCraftOutputMenuId || menu.slots.isEmpty()) {
            pendingCraftNoProgressAttempts++;
            clearPendingCraftOutput();
            return;
        }

        if (!pendingCraftOutputClicked) {
            var output = menu.slots.get(0).getItem();
            if (!output.isEmpty()) {
                String outputId = BuiltInRegistries.ITEM.getKey(output.getItem()).toString();
                if (CraftingPlanner.matchesRegistryId(outputId, pendingCraftExpectedOutput)) {
                    pendingCraftOutputBaseline = countInventoryItem(mc, pendingCraftExpectedOutput);
                    mc.gameMode.handleInventoryMouseClick(menu.containerId, 0, 0, ClickType.QUICK_MOVE, mc.player);
                    pendingCraftOutputClicked = true;
                    pendingCraftOutputWaitTicks = 0;
                    return;
                }
            }
        } else if (countInventoryItem(mc, pendingCraftExpectedOutput) > pendingCraftOutputBaseline) {
            pendingCraftNoProgressAttempts = 0;
            clearPendingCraftOutput();
            pendingCraftTicks = 0;
            return;
        }

        if (pendingCraftOutputWaitTicks > 30) {
            pendingCraftNoProgressAttempts++;
            clearPendingCraftOutput();
        }
    }

    private void clearPendingCraftOutput() {
        pendingCraftAwaitingOutput = false;
        pendingCraftOutputClicked = false;
        pendingCraftOutputWaitTicks = 0;
        pendingCraftOutputMenuId = -1;
        pendingCraftOutputBaseline = 0;
        pendingCraftExpectedOutput = null;
    }

    private void finishCraftTask() {
        pendingCraftItem = null;
        pendingCraftReqId = null;
        pendingCraftTicks = 0;
        pendingCraftAttempts = 0;
        pendingCraftNoProgressAttempts = 0;
        clearPendingCraftOutput();
        pendingCraftProcessingStallTicks = 0;
        clearPendingCraftStation();
        pendingCraftGoalCount = 1;
        clearPendingCollectTask();
        if (suspendedFollowTargetName != null) {
            followTargetName = suspendedFollowTargetName;
            suspendedFollowTargetName = null;
            followRefreshTicks = 0;
            setTaskState("follow", "running", followTargetName, "resumed after craft", 0.1);
        } else {
            clearTaskState();
        }
        sendBehaviorSnapshot();
    }

    private void tickPendingCollect(Minecraft mc) {
        if (pendingCollectItem == null || pendingCollectReqId == null || mc.player == null || mc.level == null || mc.gameMode == null) {
            return;
        }

        int currentCount = countInventoryItem(mc, pendingCollectItem);
        if (currentCount >= pendingCollectGoalCount) {
            boolean parentCraft = pendingCraftReqId != null && pendingCollectReqId.equals(pendingCraftReqId);
            String detail = "collected " + pendingCollectItem + " x" + Math.max(0, currentCount - pendingCollectBaselineCount);
            if (LCUMod.WIRE != null) {
                LCUMod.WIRE.sendProgress(pendingCollectReqId, parentCraft ? 0.5 : 1.0, detail);
            }
            if (!parentCraft) sendOperationOutcome(pendingCollectReqId, "succeeded", "COLLECTED", detail);
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
        if (ServerPolicy.inventoryAutomationAllowed()
                && pendingCollectSearchMisses < 3 && tryStorageSourceForCollect(mc)) {
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
                    boolean parentCraft = pendingCraftReqId != null && pendingCraftReqId.equals(pendingCollectReqId);
                    String requestId = pendingCollectReqId;
                    String detail = "no collectible source found for " + pendingCollectItem;
                    if (LCUMod.WIRE != null) {
                        LCUMod.WIRE.sendProgress(requestId, 0.0, detail);
                    }
                    sendOperationOutcome(requestId, "failed", "NO_SOURCE", detail);
                    clearPendingCollectTask();
                    if (parentCraft) {
                        finishCraftTask();
                    } else {
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
            if (ServerPolicy.inventoryAutomationAllowed()) {
                autoEquipForBlock(mc, pendingCollectTargetPos);
            }
            mc.player.lookAt(net.minecraft.commands.arguments.EntityAnchorArgument.Anchor.EYES, Vec3.atCenterOf(pendingCollectTargetPos));
            BlockHitResult blockHit = targetedBlockHit(mc, pendingCollectTargetPos);
            if (blockHit == null) {
                setTaskState(resolveRootTaskKind(), "aiming", pendingCollectItem,
                    "waiting for a visible resource face", collectProgress(mc));
                return;
            }
            mc.gameMode.startDestroyBlock(pendingCollectTargetPos, blockHit.getDirection());
            mc.player.swing(InteractionHand.MAIN_HAND);
            diggingPos = pendingCollectTargetPos;
            diggingDir = blockHit.getDirection();
            diggingTicks = 0;
            diggingReqId = null;
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
        BlockPos storageInteractionPos = null;
        Vec3 storageStandPos = null;
        for (var poi : PoiMemory.snapshotSortedByItemMatch(mc, "storage", pendingCollectItem, PoiMemory.INTERACTION_RADIUS, 16)) {
            BlockPos candidate = new BlockPos(
                poi.get("x").getAsInt(),
                poi.get("y").getAsInt(),
                poi.get("z").getAsInt()
            );
            if (triedStoragePositions.contains(candidate)) {
                continue;
            }
            if (PoiMemory.getStorageItemCount(candidate, pendingCollectItem) <= 0) {
                continue;
            }
            StorageApproach approach = findStorageApproach(mc, candidate);
            if (approach == null) {
                triedStoragePositions.add(candidate.immutable());
                return true;
            }
            storagePos = candidate;
            storageInteractionPos = approach.interactionPos;
            storageStandPos = approach.standPos;
            break;
        }
        if (storagePos == null) {
            for (var poi : PoiMemory.snapshot(mc, "storage", PoiMemory.INTERACTION_RADIUS, 16)) {
                BlockPos candidate = new BlockPos(
                    poi.get("x").getAsInt(),
                    poi.get("y").getAsInt(),
                    poi.get("z").getAsInt()
                );
                if (triedStoragePositions.contains(candidate) || PoiMemory.hasKnownContents(candidate)) continue;
                StorageApproach approach = findStorageApproach(mc, candidate);
                if (approach == null) {
                    triedStoragePositions.add(candidate.immutable());
                    return true;
                }
                storagePos = candidate;
                storageInteractionPos = approach.interactionPos;
                storageStandPos = approach.standPos;
                break;
            }
        }
        if (storagePos == null) return false;

        int currentCount = countInventoryItem(mc, pendingCollectItem);
        int stillNeeded = Math.max(1, pendingCollectGoalCount - currentCount);

        pendingStoragePos = storagePos;
        pendingStorageInteractionPos = storageInteractionPos;
        pendingStorageStandPos = storageStandPos;
        pendingStorageTargetItem = pendingCollectItem;
        pendingStorageGoalCount = stillNeeded;
        pendingStorageStartCount = currentCount;
        pendingStorageTicks = 0;
        triedStoragePositions.add(storagePos.immutable());
        return true;
    }

    private StorageApproach findStorageApproach(Minecraft mc, BlockPos storagePos) {
        StorageApproach best = null;
        double bestDistance = Double.POSITIVE_INFINITY;
        for (BlockPos interactionPos : PoiMemory.getStorageInteractionPositions(mc, storagePos)) {
            Vec3 standPos = Pathfinder.findReachableInteractionPosition(mc, interactionPos, 4.25);
            if (standPos == null) continue;
            double distance = standPos.distanceToSqr(mc.player.position());
            if (distance < bestDistance) {
                best = new StorageApproach(interactionPos.immutable(), standPos);
                bestDistance = distance;
            }
        }
        return best;
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

        if (pendingCollectItem == null
            || !CraftingPlanner.matchesRegistryId(pendingStorageTargetItem, pendingCollectItem)) {
            clearPendingStorageTask();
            return;
        }
        if (!InputIsolation.isAiControlled() || mc.player.isDeadOrDying()) {
            cleanupAndClearStorage(mc);
            return;
        }

        pendingStorageTicks++;
        if (pendingStorageTicks > 400) {
            cleanupAndClearStorage(mc);
            return;
        }

        if (pendingStorageStandPos == null || pendingStorageInteractionPos == null) {
            cleanupAndClearStorage(mc);
            return;
        }

        double standDistSq = mc.player.position().distanceToSqr(pendingStorageStandPos);
        if (standDistSq > 1.0) {
            Vec3 movementTarget = MovementSystem.getTarget();
            if (!MovementSystem.isMoving()
                || movementTarget == null
                || movementTarget.distanceToSqr(pendingStorageStandPos) > 0.25) {
                boolean started = MovementSystem.moveTo(
                    pendingStorageStandPos.x, pendingStorageStandPos.y,
                    pendingStorageStandPos.z, 1.0f);
                if (!started) {
                    cleanupAndClearStorage(mc);
                    return;
                }
            }
            setTaskState(resolveRootTaskKind(), "storage", pendingStorageTargetItem,
                "moving to storage", 0.15);
            return;
        }
        if (MovementSystem.isMoving()) {
            MovementSystem.stop();
        }

        var menu = mc.player.containerMenu;
        boolean inventoryMenu = menu == null || menu == mc.player.inventoryMenu;
        if (pendingStorageOwnedMenuId < 0) {
            if (!inventoryMenu) {
                if (pendingStorageOpenSentTick < 0) {
                    clearPendingStorageTask();
                    return;
                }
                if (!isLikelyStorageMenu(mc)) {
                    clearPendingStorageTask();
                    return;
                }
                pendingStorageOwnedMenuId = menu.containerId;
                pendingStorageMenuOpenTick = pendingStorageTicks;
                setTaskState(resolveRootTaskKind(), "storage", pendingStorageTargetItem,
                    "inspecting storage", 0.35);
                return;
            }

            if (pendingStorageOpenSentTick < 0) {
                mc.player.lookAt(net.minecraft.commands.arguments.EntityAnchorArgument.Anchor.EYES,
                    Vec3.atCenterOf(pendingStorageInteractionPos));
                BlockHitResult hitResult = targetedBlockHit(mc, pendingStorageInteractionPos);
                if (hitResult == null) return;
                mc.gameMode.useItemOn(mc.player, InteractionHand.MAIN_HAND, hitResult);
                pendingStorageOpenSentTick = pendingStorageTicks;
                pendingStorageOpenAttempts++;
            } else if (pendingStorageTicks - pendingStorageOpenSentTick > 30) {
                if (pendingStorageOpenAttempts >= 2) {
                    clearPendingStorageTask();
                    return;
                }
                pendingStorageOpenSentTick = -1;
            }
            setTaskState(resolveRootTaskKind(), "storage", pendingStorageTargetItem,
                "opening storage", 0.3);
            return;
        }

        if (inventoryMenu || menu.containerId != pendingStorageOwnedMenuId) {
            clearPendingStorageTask();
            return;
        }
        if (pendingStorageTicks - pendingStorageMenuOpenTick < 4) return;

        int currentCount = countInventoryItem(mc, pendingStorageTargetItem);
        if (pendingStorageClickTick >= 0) {
            if (currentCount > pendingStorageClickBaseline) {
                pendingStorageClickTick = -1;
                pendingStorageClickAttempts = 0;
            } else if (pendingStorageTicks - pendingStorageClickTick <= 20) {
                return;
            } else {
                pendingStorageClickTick = -1;
                pendingStorageClickAttempts++;
                if (pendingStorageClickAttempts >= 2) {
                    closeOwnedStorageMenu(mc);
                    clearPendingStorageTask();
                    return;
                }
            }
        }

        if (currentCount - pendingStorageStartCount >= pendingStorageGoalCount) {
            scanStorageMenu(mc, pendingStorageTargetItem);
            if (LCUMod.WIRE != null && pendingCollectReqId != null) {
                LCUMod.WIRE.sendProgress(pendingCollectReqId, 0.45,
                    "withdrew " + pendingStorageTargetItem + " x" + (currentCount - pendingStorageStartCount) + " from storage");
            }
            closeOwnedStorageMenu(mc);
            clearPendingStorageTask();
            return;
        }

        StorageScan scan = scanStorageMenu(mc, pendingStorageTargetItem);
        if (scan.matchingSlot < 0) {
            closeOwnedStorageMenu(mc);
            clearPendingStorageTask();
            return;
        }
        pendingStorageClickBaseline = currentCount;
        pendingStorageClickTick = pendingStorageTicks;
        mc.gameMode.handleInventoryMouseClick(
            menu.containerId, scan.matchingSlot, 0,
            ClickType.QUICK_MOVE, mc.player);
    }

    private StorageScan scanStorageMenu(Minecraft mc, String itemId) {
        if (mc.player.containerMenu == null || mc.player.containerMenu == mc.player.inventoryMenu) {
            return new StorageScan(Map.of(), -1);
        }
        Map<String, Integer> contents = new java.util.HashMap<>();
        int matchingSlot = -1;
        for (int slot = 0; slot < mc.player.containerMenu.slots.size(); slot++) {
            var menuSlot = mc.player.containerMenu.slots.get(slot);
            if (menuSlot.container == mc.player.getInventory() || !menuSlot.mayPickup(mc.player)) continue;
            var stack = menuSlot.getItem();
            if (stack.isEmpty()) continue;
            String stackId = BuiltInRegistries.ITEM.getKey(stack.getItem()).toString();
            contents.merge(stackId, stack.getCount(), Integer::sum);
            if (matchingSlot < 0 && CraftingPlanner.matchesItemId(stackId, itemId)) {
                matchingSlot = slot;
            }
        }

        if (pendingStoragePos != null) {
            PoiMemory.updateStorageContents(pendingStoragePos, contents, tickCount);
        }
        return new StorageScan(contents, matchingSlot);
    }

    private boolean isLikelyStorageMenu(Minecraft mc) {
        var menu = mc.player.containerMenu;
        if (menu == null || menu == mc.player.inventoryMenu) return false;
        String name = menu.getClass().getSimpleName().toLowerCase();
        if (name.contains("craft") || name.contains("furnace") || name.contains("smoker")
            || name.contains("anvil") || name.contains("enchant") || name.contains("merchant")) {
            return false;
        }
        return menu.slots.stream().anyMatch(slot ->
            slot.container != mc.player.getInventory() && slot.mayPickup(mc.player));
    }

    private void closeOwnedStorageMenu(Minecraft mc) {
        if (mc.player.containerMenu != null
            && mc.player.containerMenu != mc.player.inventoryMenu
            && mc.player.containerMenu.containerId == pendingStorageOwnedMenuId) {
            mc.player.closeContainer();
        }
    }

    private void cleanupAndClearStorage(Minecraft mc) {
        closeOwnedStorageMenu(mc);
        clearPendingStorageTask();
    }

    private void clearPendingStorageTask() {
        pendingStoragePos = null;
        pendingStorageInteractionPos = null;
        pendingStorageStandPos = null;
        pendingStorageTargetItem = null;
        pendingStorageGoalCount = 0;
        pendingStorageStartCount = 0;
        pendingStorageTicks = 0;
        pendingStorageOpenSentTick = -1;
        pendingStorageOpenAttempts = 0;
        pendingStorageOwnedMenuId = -1;
        pendingStorageMenuOpenTick = -1;
        pendingStorageClickTick = -1;
        pendingStorageClickBaseline = 0;
        pendingStorageClickAttempts = 0;
    }

    private record StorageApproach(BlockPos interactionPos, Vec3 standPos) {
    }

    private record StorageScan(Map<String, Integer> contents, int matchingSlot) {
    }

    private boolean hasItem(Minecraft mc, String itemName) {
        for (int i = 0; i < mc.player.getInventory().getContainerSize(); i++) {
            var stack = mc.player.getInventory().getItem(i);
            if (stack.isEmpty()) continue;
            String id = BuiltInRegistries.ITEM.getKey(stack.getItem()).toString();
            if (CraftingPlanner.matchesRegistryId(id, itemName)) {
                return true;
            }
        }
        return false;
    }

    private RecipeHolder<?> findRecipeByResult(Minecraft mc, String itemName) {
        for (RecipeHolder<?> recipe : mc.level.getRecipeManager().getRecipes()) {
            if (recipe.value().getType() != RecipeType.CRAFTING) continue;
            var result = recipe.value().getResultItem(mc.level.registryAccess());
            if (result.isEmpty()) continue;
            String id = BuiltInRegistries.ITEM.getKey(result.getItem()).toString();
            if (CraftingPlanner.matchesRegistryId(id, itemName)) {
                return recipe;
            }
        }
        return null;
    }

    private boolean isCraftingTableOpen(Minecraft mc) {
        return mc.player.containerMenu instanceof CraftingMenu;
    }

    private boolean openNearbyCraftingTable(Minecraft mc) {
        BlockPos remembered = PoiMemory.findNearest(mc, Set.of("minecraft:crafting_table"), PoiMemory.INTERACTION_RADIUS);
        if (remembered != null && mc.level.getBlockState(remembered).is(Blocks.CRAFTING_TABLE)) {
            return navigateAndUseStation(mc, remembered);
        }

        BlockPos playerPos = mc.player.blockPosition();
        for (int dx = -4; dx <= 4; dx++) {
            for (int dy = -2; dy <= 2; dy++) {
                for (int dz = -4; dz <= 4; dz++) {
                    BlockPos pos = playerPos.offset(dx, dy, dz);
                    if (!mc.level.getBlockState(pos).is(Blocks.CRAFTING_TABLE)) continue;
                    return navigateAndUseStation(mc, pos);
                }
            }
        }
        return placeStationFromInventory(mc, "minecraft:crafting_table");
    }

    private boolean placeStationFromInventory(Minecraft mc, String stationBlockId) {
        int slot = findHotbarOrInventoryItem(mc, stationBlockId);
        if (slot < 0) {
            return false;
        }

        if (slot >= 9) {
            return false;
        }
        mc.player.getInventory().selected = slot;

        BlockPos placePos = findNearbyStationPlacement(mc);
        if (placePos == null) {
            return false;
        }
        BlockPos supportPos = placePos.below();
        mc.player.lookAt(net.minecraft.commands.arguments.EntityAnchorArgument.Anchor.EYES, Vec3.atCenterOf(supportPos));
        BlockHitResult hitResult = targetedBlockHit(mc, supportPos);
        if (hitResult == null) return true;
        if (hitResult.getDirection() != Direction.UP) return false;
        mc.gameMode.useItemOn(mc.player, InteractionHand.MAIN_HAND, hitResult);
        return true;
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

    private boolean openNearbyProcessingStation(Minecraft mc, String stationBlockId) {
        BlockPos remembered = PoiMemory.findNearest(mc, Set.of(stationBlockId), PoiMemory.INTERACTION_RADIUS);
        if (remembered != null) {
            String rememberedId = BuiltInRegistries.BLOCK.getKey(mc.level.getBlockState(remembered).getBlock()).toString();
            if (rememberedId.equals(stationBlockId)) {
                return navigateAndUseStation(mc, remembered);
            }
        }

        BlockPos playerPos = mc.player.blockPosition();
        for (int dx = -4; dx <= 4; dx++) {
            for (int dy = -2; dy <= 2; dy++) {
                for (int dz = -4; dz <= 4; dz++) {
                    BlockPos pos = playerPos.offset(dx, dy, dz);
                    String blockId = BuiltInRegistries.BLOCK.getKey(mc.level.getBlockState(pos).getBlock()).toString();
                    if (!blockId.equals(stationBlockId)) continue;
                    return navigateAndUseStation(mc, pos);
                }
            }
        }
        return placeStationFromInventory(mc, stationBlockId);
    }

    private boolean navigateAndUseStation(Minecraft mc, BlockPos stationPos) {
        if (!stationPos.equals(pendingCraftStationPos) || pendingCraftStationStandPos == null) {
            Vec3 standPos = Pathfinder.findReachableInteractionPosition(mc, stationPos, 4.25);
            if (standPos == null) return false;
            pendingCraftStationPos = stationPos.immutable();
            pendingCraftStationStandPos = standPos;
            pendingCraftStationUseAttempts = 0;
        }

        if (mc.player.position().distanceToSqr(pendingCraftStationStandPos) > 1.0) {
            Vec3 movementTarget = MovementSystem.getTarget();
            if (!MovementSystem.isMoving()
                || movementTarget == null
                || movementTarget.distanceToSqr(pendingCraftStationStandPos) > 0.25) {
                return MovementSystem.moveTo(
                    pendingCraftStationStandPos.x,
                    pendingCraftStationStandPos.y,
                    pendingCraftStationStandPos.z,
                    1.0f
                );
            }
            return true;
        }

        MovementSystem.stop();
        if (pendingCraftStationUseAttempts >= 3) return false;
        mc.player.lookAt(net.minecraft.commands.arguments.EntityAnchorArgument.Anchor.EYES, Vec3.atCenterOf(stationPos));
        BlockHitResult hitResult = targetedBlockHit(mc, stationPos);
        if (hitResult == null) return true;
        pendingCraftStationUseAttempts++;
        mc.gameMode.useItemOn(mc.player, InteractionHand.MAIN_HAND, hitResult);
        return true;
    }

    private void clearPendingCraftStation() {
        pendingCraftStationPos = null;
        pendingCraftStationStandPos = null;
        pendingCraftStationUseAttempts = 0;
    }

    private int findHotbarOrInventoryItem(Minecraft mc, String itemId) {
        int found = -1;
        for (int i = 0; i < mc.player.getInventory().getContainerSize(); i++) {
            var stack = mc.player.getInventory().getItem(i);
            if (stack.isEmpty()) continue;
            String stackId = BuiltInRegistries.ITEM.getKey(stack.getItem()).toString();
            if (!CraftingPlanner.matchesRegistryId(stackId, itemId)) continue;
            if (i < 9) {
                return i;
            }
            if (found < 0) {
                found = i;
            }
        }
        return found;
    }

    private BlockPos findNearbyStationPlacement(Minecraft mc) {
        BlockPos origin = mc.player.blockPosition();
        for (int radius = 1; radius <= 4; radius++) {
            for (int dx = -radius; dx <= radius; dx++) {
                for (int dz = -radius; dz <= radius; dz++) {
                    BlockPos pos = origin.offset(dx, 0, dz);
                    if (!mc.level.getBlockState(pos).isAir()) continue;
                    BlockPos supportPos = pos.below();
                    if (mc.level.getBlockState(supportPos).isAir()) continue;
                    return pos;
                }
            }
        }
        return null;
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
        mc.gameMode.handleInventoryMouseClick(mc.player.containerMenu.containerId, 2, 0, ClickType.QUICK_MOVE, mc.player);
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
        if (!fuelStack.isEmpty() && net.minecraft.world.level.block.entity.AbstractFurnaceBlockEntity.isFuel(fuelStack)) {
            return true;
        }

        int fuelSlot = findFuelInventoryMenuSlot(mc);
        if (fuelSlot < 0) {
            return false;
        }

        mc.gameMode.handleInventoryMouseClick(mc.player.containerMenu.containerId, fuelSlot, 0, ClickType.PICKUP, mc.player);
        mc.gameMode.handleInventoryMouseClick(mc.player.containerMenu.containerId, 1, 0, ClickType.PICKUP, mc.player);
        if (!mc.player.containerMenu.getCarried().isEmpty()) {
            mc.gameMode.handleInventoryMouseClick(mc.player.containerMenu.containerId, fuelSlot, 0, ClickType.PICKUP, mc.player);
        }
        return true;
    }

    private int findFuelInventoryMenuSlot(Minecraft mc) {
        if (mc.player.containerMenu == null) {
            return -1;
        }
        for (int slot = 3; slot < mc.player.containerMenu.slots.size(); slot++) {
            var stack = mc.player.containerMenu.slots.get(slot).getItem();
            if (!stack.isEmpty() && net.minecraft.world.level.block.entity.AbstractFurnaceBlockEntity.isFuel(stack)) {
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
            if (!net.minecraft.world.level.block.entity.AbstractFurnaceBlockEntity.isFuel(itemEntity.getItem())) continue;
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
                    if (!net.minecraft.world.level.block.entity.AbstractFurnaceBlockEntity.isFuel(blockStack)) continue;
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
        String normalized = switch (compact) {
            case "木剑" -> "minecraft:wooden_sword";
            case "木镐" -> "minecraft:wooden_pickaxe";
            case "木棍" -> "minecraft:stick";
            case "木板" -> "#minecraft:planks";
            case "原木" -> "#minecraft:logs";
            case "木头" -> "#lcu:wood";
            case "工作台" -> "minecraft:crafting_table";
            case "石剑" -> "minecraft:stone_sword";
            case "石镐" -> "minecraft:stone_pickaxe";
            default -> compact;
        };
        return normalized.contains(":") ? normalized : "minecraft:" + normalized;
    }

    private String normalizeCraftItemName(String itemName) {
        String normalized = normalizeItemName(itemName);
        return switch (normalized) {
            case "#minecraft:planks" -> "minecraft:oak_planks";
            case "#minecraft:logs", "#lcu:wood" -> "minecraft:oak_log";
            default -> normalized;
        };
    }

    private void tickPendingEat(Minecraft mc) {
        if (pendingEatReqId == null || mc.player == null || mc.gameMode == null) {
            return;
        }

        pendingEatTicks++;
        if (mc.player.isUsingItem()) {
            if (LCUMod.WIRE != null && pendingEatTicks % 10 == 0) {
                LCUMod.WIRE.sendProgress(pendingEatReqId, 0.4, "eating in progress");
            }
            return;
        }

        int hunger = mc.player.getFoodData().getFoodLevel();
        float health = mc.player.getHealth();
        if ((hunger > pendingEatStartHunger || health > pendingEatStartHealth) && pendingEatTicks > 5) {
            if (LCUMod.WIRE != null) {
                LCUMod.WIRE.sendProgress(pendingEatReqId, 1.0, "eat complete");
            }
            sendOperationOutcome(pendingEatReqId, "succeeded", "CONSUMED", "eat complete");
            pendingEatReqId = null;
            pendingEatTicks = 0;
            pendingEatAttempts = 0;
            clearTaskState();
            sendBehaviorSnapshot();
            return;
        }

        if (pendingEatTicks % 8 == 0 && pendingEatAttempts < 3) {
            pendingEatAttempts++;
            startEating(mc);
            return;
        }

        if (pendingEatAttempts >= 3 && pendingEatTicks > 30) {
            if (LCUMod.WIRE != null) {
                LCUMod.WIRE.sendProgress(pendingEatReqId, 0.0, "eat failed or cannot start use animation");
            }
            sendOperationOutcome(pendingEatReqId, "failed", "USE_FAILED", "eat failed or cannot start use animation");
            pendingEatReqId = null;
            pendingEatTicks = 0;
            pendingEatAttempts = 0;
            clearTaskState();
            sendBehaviorSnapshot();
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

    private int countInventoryItem(Minecraft mc, String itemId) {
        if (mc == null || mc.player == null || itemId == null) return 0;
        int total = 0;
        for (int i = 0; i < mc.player.getInventory().getContainerSize(); i++) {
            var stack = mc.player.getInventory().getItem(i);
            if (stack.isEmpty()) continue;
            String id = BuiltInRegistries.ITEM.getKey(stack.getItem()).toString();
            if (CraftingPlanner.matchesItemId(id, itemId)) {
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
        if (matchesCollectTargetId(itemFromBlock, itemId)) {
            return true;
        }
        return switch (itemId) {
            case "minecraft:raw_iron" -> blockId.equals("minecraft:iron_ore") || blockId.equals("minecraft:deepslate_iron_ore");
            case "minecraft:raw_gold" -> blockId.equals("minecraft:gold_ore") || blockId.equals("minecraft:deepslate_gold_ore");
            case "minecraft:raw_copper" -> blockId.equals("minecraft:copper_ore") || blockId.equals("minecraft:deepslate_copper_ore");
            case "minecraft:coal" -> blockId.equals("minecraft:coal_ore") || blockId.equals("minecraft:deepslate_coal_ore");
            case "minecraft:diamond" -> blockId.equals("minecraft:diamond_ore") || blockId.equals("minecraft:deepslate_diamond_ore");
            case "minecraft:emerald" -> blockId.equals("minecraft:emerald_ore") || blockId.equals("minecraft:deepslate_emerald_ore");
            case "minecraft:redstone" -> blockId.equals("minecraft:redstone_ore") || blockId.equals("minecraft:deepslate_redstone_ore");
            case "minecraft:lapis_lazuli" -> blockId.equals("minecraft:lapis_ore") || blockId.equals("minecraft:deepslate_lapis_ore");
            case "minecraft:quartz" -> blockId.equals("minecraft:nether_quartz_ore");
            default -> false;
        };
    }

    private boolean matchesCollectTargetId(String candidateId, String targetId) {
        return CraftingPlanner.matchesItemId(candidateId, targetId);
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
        if (bestSlot >= 0 && bestSlot != inv.selected) {
            inv.selected = bestSlot;
        }
    }

    private String resolveRootTaskKind() {
        if (pendingCraftReqId != null) return "craft";
        if (pendingCollectReqId != null) return "collect";
        if (pendingEatReqId != null) return "eat";
        if (followTargetName != null) return "follow";
        if (Pathfinder.isNavigating()) return "move";
        return "idle";
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

    private boolean hasFoodInHotbar(Minecraft mc) {
        var inv = mc.player.getInventory();
        for (int i = 0; i < 9; i++) {
            var stack = inv.getItem(i);
            if (!stack.isEmpty() && stack.getItem().getFoodProperties(stack, mc.player) != null) {
                return true;
            }
        }
        return false;
    }

    private void startEating(Minecraft mc) {
        var inv = mc.player.getInventory();
        for (int i = 0; i < 9; i++) {
            var stack = inv.getItem(i);
            if (stack.isEmpty()) continue;
            if (stack.getItem().getFoodProperties(stack, mc.player) != null) {
                inv.selected = i;
                mc.gameMode.useItem(mc.player, InteractionHand.MAIN_HAND);
                return;
            }
        }
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
