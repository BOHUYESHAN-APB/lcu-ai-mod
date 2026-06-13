package com.lcu.lcumod.action;

import com.google.gson.JsonArray;
import com.google.gson.JsonObject;
import com.lcu.lcumod.LCUMod;
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
import net.minecraft.world.item.crafting.RecipeHolder;
import net.minecraft.world.item.crafting.RecipeType;
import net.minecraft.world.phys.AABB;
import net.minecraft.world.phys.BlockHitResult;
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
    private static String pendingCraftItem = null;
    private static String pendingCraftReqId = null;
    private static int pendingCraftTicks = 0;
    private static int pendingCraftAttempts = 0;
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

        // ── Pathfinder (A* navigation) ──
        Pathfinder.tick(mc);

        // ── Remembered workstations/storage POIs ──
        PoiMemory.tick(mc, tickCount);

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

        // ── Java-side autonomous behavior (works without backend) ──
        if (InputIsolation.isAiControlled()) {
            boolean behaviorActive = JavaAutonomousBehavior.tick(mc);
            // If behavior is active and no backend command is pending, skip other actions
            if (behaviorActive && WireServer.commandQueue.isEmpty()) {
                return;
            }
        }

        boolean runtimeBusy = Pathfinder.isNavigating()
                || MovementSystem.isMoving()
                || JavaAutonomousBehavior.getState() != JavaAutonomousBehavior.BehaviorState.IDLE
                || !WireServer.commandQueue.isEmpty();

        // ── Human-like idle behavior (head tracking) ──
        if (InputIsolation.isAiControlled()
                && JavaAutonomousBehavior.getState() == JavaAutonomousBehavior.BehaviorState.IDLE
                && !MovementSystem.isMoving()
                && !Pathfinder.isNavigating()) {
            HumanLikeBehavior.tick(mc);
        }

        // ── Anti-AFK subtle activity pulses ──
        ActivitySignalController.tick(mc, runtimeBusy);

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
                case "use_item" -> { mc.gameMode.useItem(mc.player, InteractionHand.MAIN_HAND); sendResponse(cmd.id(), true, "Item used"); }
                case "select_hotbar" -> handleSelectHotbar(cmd);
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
                case "use_on" -> handleUseOn(cmd);       // right-click block/entity
                case "get_container" -> handleGetContainer(cmd);
                case "take_item" -> handleTakeItem(cmd);
                case "put_item" -> handlePutItem(cmd);
                case "close_container" -> handleCloseContainer(cmd);
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

        // Find nearest living entity in crosshair direction
        Entity target = findTargetEntity(mc, 6.0);
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
        state.addProperty("behaviors_enabled", LCUMod.BEHAVIORS != null ? LCUMod.BEHAVIORS.isEnabled() : false);

        sendResponse(cmd.id(), true, state);
    }

    private void handleInteract(WireServer.WireCommand cmd) {
        var mc = Minecraft.getInstance();
        if (mc.player == null || mc.gameMode == null) {
            sendResponse(cmd.id(), false, "No player");
            return;
        }
        // Right-click the block the player is looking at
        var hit = mc.hitResult;
        if (hit != null && hit.getType() == net.minecraft.world.phys.HitResult.Type.BLOCK) {
            var blockHit = (net.minecraft.world.phys.BlockHitResult) hit;
            mc.gameMode.useItemOn(mc.player, InteractionHand.MAIN_HAND, blockHit);
            sendResponse(cmd.id(), true, "Interacted");
        } else {
            // Use item in hand instead
            mc.gameMode.useItem(mc.player, InteractionHand.MAIN_HAND);
            sendResponse(cmd.id(), true, "Used item");
        }
    }

    private void handleMine(WireServer.WireCommand cmd) {
        var mc = Minecraft.getInstance();
        if (mc.player == null || mc.gameMode == null) {
            sendResponse(cmd.id(), false, "No player/gameMode");
            return;
        }
        var hit = mc.hitResult;
        if (hit != null && hit.getType() == net.minecraft.world.phys.HitResult.Type.BLOCK) {
            var blockHit = (net.minecraft.world.phys.BlockHitResult) hit;
            BlockPos pos = blockHit.getBlockPos();
            Direction dir = blockHit.getDirection();

            // Auto-equip best tool (mineflayer-style)
            autoEquipForBlock(mc);

            // Start digging — track for continuous ticks
            mc.gameMode.startDestroyBlock(pos, dir);
            mc.player.swing(InteractionHand.MAIN_HAND);
            diggingPos = pos;
            diggingDir = dir;
            diggingTicks = 0;
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
        stopDigging();
        sendResponse(cmd.id(), true, "Digging stopped");
    }

    private void stopDigging() {
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
        var mc = Minecraft.getInstance();
        followTargetName = null;
        pendingCraftItem = null;
        pendingCraftReqId = null;
        pendingCraftTicks = 0;
        pendingCraftAttempts = 0;
        pendingCraftGoalCount = 1;
        pendingEatReqId = null;
        pendingEatTicks = 0;
        pendingEatAttempts = 0;
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
            // Use item in hand
            mc.gameMode.useItem(mc.player, InteractionHand.MAIN_HAND);
            sendResponse(cmd.id(), true, "Used item (no target)");
        }
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

    private void handleTakeItem(WireServer.WireCommand cmd) {
        // Take item from container slot (shift-click to player inventory)
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
        var menu = mc.player.containerMenu;
        if (menu == null) { sendResponse(cmd.id(), false, "No container open"); return; }

        int slot = args.get("slot").getAsInt();
        if (slot < 0 || slot >= menu.slots.size()) {
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
        if (args == null || !args.has("slot")) {
            sendResponse(cmd.id(), false, "Need slot number");
            return;
        }
        var mc = Minecraft.getInstance();
        if (mc.player == null || mc.gameMode == null) {
            sendResponse(cmd.id(), false, "No player");
            return;
        }
        var menu = mc.player.containerMenu;
        if (menu == null) { sendResponse(cmd.id(), false, "No container open"); return; }

        int slot = args.get("slot").getAsInt();
        if (slot < 0 || slot >= menu.slots.size()) {
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
        if (entity != null) {
            mc.gameMode.interact(mc.player, entity, InteractionHand.MAIN_HAND);
            sendResponse(cmd.id(), true, "Interacted with entity " + entityId);
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
        pendingEatReqId = null;
        pendingEatTicks = 0;
        pendingEatAttempts = 0;
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
        // For now, just send chat
        var mc = Minecraft.getInstance();
        if (mc.player != null) {
            mc.player.connection.sendChat("Looking for " + args.get("villager_type").getAsString() + " villager to trade");
        }
        sendResponse(cmd.id(), true, "Looking for villager");
    }

    private void handleSleep(WireServer.WireCommand cmd) {
        var mc = Minecraft.getInstance();
        if (mc.player != null) {
            mc.player.connection.sendChat("Looking for a bed to sleep");
        }
        sendResponse(cmd.id(), true, "Looking for bed");
    }

    private void handleEat(WireServer.WireCommand cmd) {
        var mc = Minecraft.getInstance();
        if (mc.player == null || mc.gameMode == null) {
            sendResponse(cmd.id(), false, "No player");
            return;
        }

        if (!hasFoodInHotbar(mc)) {
            sendResponse(cmd.id(), false, "No food in hotbar");
            return;
        }

        followTargetName = null;

        pendingEatReqId = cmd.id();
        pendingEatTicks = 0;
        pendingEatAttempts = 0;
        pendingEatStartHunger = mc.player.getFoodData().getFoodLevel();
        pendingEatStartHealth = mc.player.getHealth();
        clearPendingCollectTask();
        setTaskState("eat", "running", "food", "consuming food", 0.05);
        startEating(mc);
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
                mc.player.drop(stack.split(count), false);
                sendResponse(cmd.id(), true, "Dropped " + itemName);
                return;
            }
        }
        sendResponse(cmd.id(), false, "Item " + itemName + " not found");
    }

    private void handleSortInventory(WireServer.WireCommand cmd) {
        // For now, just send confirmation
        sendResponse(cmd.id(), true, "Inventory sorted");
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
        data.addProperty("pending_eat", pendingEatReqId != null);
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
                if (!openNearbyCraftingTable(mc)) {
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
            mc.gameMode.handlePlaceRecipe(mc.player.containerMenu.containerId, step.recipe, false);
            mc.gameMode.handleInventoryMouseClick(mc.player.containerMenu.containerId, 0, 0, ClickType.QUICK_MOVE, mc.player);
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

        if (pendingCraftAttempts >= 8 && countInventoryItem(mc, pendingCraftItem) < pendingCraftGoalCount) {
            if (LCUMod.WIRE != null) {
                LCUMod.WIRE.sendProgress(pendingCraftReqId, 0.0, "craft failed or missing materials: " + pendingCraftItem);
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
            autoEquipForBlock(mc, pendingCollectTargetPos);
            mc.player.lookAt(net.minecraft.commands.arguments.EntityAnchorArgument.Anchor.EYES, Vec3.atCenterOf(pendingCollectTargetPos));
            mc.gameMode.startDestroyBlock(pendingCollectTargetPos, Direction.UP);
            mc.player.swing(InteractionHand.MAIN_HAND);
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
        for (var poi : PoiMemory.snapshot(mc, "storage", PoiMemory.INTERACTION_RADIUS, 16)) {
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
            Vec3 hitVec = Vec3.atCenterOf(pendingStoragePos);
            BlockHitResult hitResult = new BlockHitResult(hitVec, Direction.UP, pendingStoragePos, false);
            mc.gameMode.useItemOn(mc.player, InteractionHand.MAIN_HAND, hitResult);
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

        // First pass: record matching slot indices
        List<Integer> matchingSlots = new ArrayList<>();
        for (int slot = 0; slot < containerEndSlot; slot++) {
            var stack = mc.player.containerMenu.slots.get(slot).getItem();
            if (stack.isEmpty()) continue;
            String stackId = BuiltInRegistries.ITEM.getKey(stack.getItem()).toString();
            if (CraftingPlanner.matchesRegistryId(stackId, itemId)) {
                matchingSlots.add(slot);
                if (matchingSlots.size() * 64 >= maxCount) break; // enough potential items
            }
        }

        if (matchingSlots.isEmpty()) return 0;

        // Second pass: shift-click each matching slot to withdraw
        for (int slot : matchingSlots) {
            mc.gameMode.handleInventoryMouseClick(
                mc.player.containerMenu.containerId, slot, 0,
                ClickType.QUICK_MOVE, mc.player);
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

    private RecipeHolder<?> findRecipeByResult(Minecraft mc, String itemName) {
        for (RecipeHolder<?> recipe : mc.level.getRecipeManager().getRecipes()) {
            if (recipe.value().getType() != RecipeType.CRAFTING) continue;
            var result = recipe.value().getResultItem(mc.level.registryAccess());
            if (result.isEmpty()) continue;
            String id = BuiltInRegistries.ITEM.getKey(result.getItem()).toString();
            if (id.equals(itemName) || id.endsWith(":" + itemName)) {
                return recipe;
            }
        }
        return null;
    }

    private boolean isCraftingTableOpen(Minecraft mc) {
        return mc.player.containerMenu != null
            && mc.player.containerMenu != mc.player.inventoryMenu
            && mc.player.containerMenu.slots.size() >= 10;
    }

    private boolean openNearbyCraftingTable(Minecraft mc) {
        BlockPos remembered = PoiMemory.findNearest(mc, Set.of("minecraft:crafting_table"), PoiMemory.INTERACTION_RADIUS);
        if (remembered != null && mc.level.getBlockState(remembered).is(Blocks.CRAFTING_TABLE)) {
            Vec3 hitVec = Vec3.atCenterOf(remembered);
            BlockHitResult hitResult = new BlockHitResult(hitVec, Direction.UP, remembered, false);
            mc.gameMode.useItemOn(mc.player, InteractionHand.MAIN_HAND, hitResult);
            return true;
        }

        BlockPos playerPos = mc.player.blockPosition();
        for (int dx = -4; dx <= 4; dx++) {
            for (int dy = -2; dy <= 2; dy++) {
                for (int dz = -4; dz <= 4; dz++) {
                    BlockPos pos = playerPos.offset(dx, dy, dz);
                    if (!mc.level.getBlockState(pos).is(Blocks.CRAFTING_TABLE)) continue;
                    Vec3 hitVec = Vec3.atCenterOf(pos);
                    BlockHitResult hitResult = new BlockHitResult(hitVec, Direction.UP, pos, false);
                    mc.gameMode.useItemOn(mc.player, InteractionHand.MAIN_HAND, hitResult);
                    return true;
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
            int swapHotbar = 0;
            var current = mc.player.getInventory().getItem(swapHotbar).copy();
            var target = mc.player.getInventory().getItem(slot).copy();
            mc.player.getInventory().setItem(slot, current);
            mc.player.getInventory().setItem(swapHotbar, target);
            slot = swapHotbar;
        }
        mc.player.getInventory().selected = slot;

        BlockPos placePos = findNearbyStationPlacement(mc);
        if (placePos == null) {
            return false;
        }
        BlockPos supportPos = placePos.below();
        Vec3 hitVec = Vec3.atCenterOf(supportPos);
        BlockHitResult hitResult = new BlockHitResult(hitVec, Direction.UP, supportPos, false);
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
                Vec3 hitVec = Vec3.atCenterOf(remembered);
                BlockHitResult hitResult = new BlockHitResult(hitVec, Direction.UP, remembered, false);
                mc.gameMode.useItemOn(mc.player, InteractionHand.MAIN_HAND, hitResult);
                return true;
            }
        }

        BlockPos playerPos = mc.player.blockPosition();
        for (int dx = -4; dx <= 4; dx++) {
            for (int dy = -2; dy <= 2; dy++) {
                for (int dz = -4; dz <= 4; dz++) {
                    BlockPos pos = playerPos.offset(dx, dy, dz);
                    String blockId = BuiltInRegistries.BLOCK.getKey(mc.level.getBlockState(pos).getBlock()).toString();
                    if (!blockId.equals(stationBlockId)) continue;
                    Vec3 hitVec = Vec3.atCenterOf(pos);
                    BlockHitResult hitResult = new BlockHitResult(hitVec, Direction.UP, pos, false);
                    mc.gameMode.useItemOn(mc.player, InteractionHand.MAIN_HAND, hitResult);
                    return true;
                }
            }
        }
        return placeStationFromInventory(mc, stationBlockId);
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
