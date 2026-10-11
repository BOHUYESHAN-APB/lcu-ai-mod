package com.lcu.lcumod.action;

import com.google.gson.JsonArray;
import com.google.gson.JsonObject;
import com.lcu.lcumod.LCUMod;
import com.lcu.lcumod.state.StateCollector;
import net.minecraft.client.Minecraft;
import net.minecraft.client.gui.screens.inventory.AbstractContainerScreen;
import net.minecraft.core.BlockPos;
import net.minecraft.core.registries.BuiltInRegistries;
import net.minecraft.network.protocol.game.ServerboundMovePlayerPacket;
import net.minecraft.world.InteractionHand;
import net.minecraft.world.entity.Entity;
import net.minecraft.world.inventory.AbstractContainerMenu;
import net.minecraft.world.phys.EntityHitResult;
import net.minecraft.world.phys.Vec3;
import net.minecraft.world.inventory.ContainerInput;
import net.minecraft.world.inventory.Slot;
import net.minecraft.world.item.ItemStack;
import net.minecraft.world.item.crafting.Ingredient;
import net.minecraft.world.item.crafting.RecipeHolder;
import net.minecraft.world.item.crafting.RecipeType;
import net.minecraft.world.item.crafting.display.RecipeDisplayId;
import net.minecraft.world.item.crafting.display.ShapedCraftingRecipeDisplay;
import net.minecraft.world.item.crafting.display.SlotDisplayContext;
import net.minecraft.world.phys.Vec3;

import java.util.List;

/**
 * 容器 / UI 驱动层：**一律发窗口点击包，绝不打开客户端界面**。
 *
 * <h2>为什么"不抢鼠标"</h2>
 * 本层只做两件事：① {@code MultiPlayerGameMode.handleContainerInput(containerId, slot, button,
 * ContainerInput, player)} 发窗口点击包（同时把点击应用到客户端菜单，因此可**同步校验**）；
 * ② 关闭容器用 {@code player.closeContainer()}（发 {@code ServerboundContainerClosePacket}）。
 * 本类（以及整个 {@code src/main}）**没有任何界面打开调用** —— 这就是"不开界面 → 不抓鼠标 /
 * 不收键盘 → 玩家视角照旧"的硬证据（验收会用 grep 检查界面打开调用，结果为空）。
 *
 * <p>注意一个和 spec 的差异（以 javap/实读为准）：26.3 原版在收到
 * {@code ClientboundOpenScreenPacket} 时会**自己**弹出容器界面
 * （{@code ClientPacketListener.handleOpenScreen → MenuScreens.create → Gui 的界面设置入口}），
 * 即"玩家右键开箱"这件事原版会附带一个 GUI。为了真正满足"绝不打开客户端界面"，
 * {@link com.lcu.lcumod.client.ContainerScreenGuard} 会在**AI 自己发起开容器**的那一小段时间窗里
 * 取消该次界面打开（{@code ScreenEvent.Opening}，可取消事件）—— 菜单仍然正常建立
 * （原版是先 {@code player.containerMenu = menu} 再设置界面，见 {@code MenuScreens$ScreenConstructor.fromPacket} 字节码），
 * 玩家自己打开的界面一律照常显示。
 *
 * <h2>让路</h2>
 * {@code mc.gui.screen() instanceof AbstractContainerScreen}（26.3 的 {@code Minecraft} 已无
 * {@code screen} 字段/取值方法，界面挪到了 {@code Gui#screen()}）→ 不下任何点击，
 * 报 {@code reason="user_screen_open"}。
 *
 * <h2>幂等 / 有界 / 恒实失败</h2>
 * 每个命令都先校验再执行；点击序列由 {@link ContainerPlan} 生成（有界：≤ 3 + 64 次）；
 * 执行后立刻用客户端菜单状态校验（{@code handleContainerInput} 本地同步应用点击），
 * 没生效就如实报 {@code move_no_effect}，绝不谎报成功。合成走原版配方放置 API
 * （{@code handlePlaceRecipe}），拿不到配方表 / 配方不匹配 / 缺材料都如实失败。
 */
public final class ContainerController {

    /** AI 主动开容器后的"抑制界面"时间窗（毫秒）。 */
    private static final long INTENT_WINDOW_MS = 2000L;
    private static volatile long intentUntilMs = 0L;

    /** craft_via_recipe 的结果校验（诚实 outcome，超时恒实失败）。 */
    private static final int CRAFT_VERIFY_MAX_TICKS = 60;
    private static String craftReqId = null;
    private static String craftItem = null;
    private static int craftBaseline = 0;
    private static int craftGoal = 1;
    private static int craftElapsed = 0;

    private ContainerController() {
    }

    // ── 结果 ──────────────────────────────────────────────────────

    /** {@code ok} = 命令已生效；{@code reason} = 机器可读原因（成功时也可带信息性原因）。 */
    public record Result(boolean ok, String reason, JsonObject data) {
        public static Result ok(JsonObject data) {
            return new Result(true, ContainerPlan.OK, data);
        }

        public static Result info(String reason, JsonObject data) {
            return new Result(true, reason, data);
        }

        public static Result fail(String reason, JsonObject data) {
            return new Result(false, reason, data);
        }
    }

    // ── AI 开容器意图（供 ContainerScreenGuard 抑制界面）──────────────

    /** 通用右键前的意图登记：接下来 ~2s 内若原版要弹容器界面，就抑制它。 */
    public static void markAiContainerIntent() {
        intentUntilMs = System.currentTimeMillis() + INTENT_WINDOW_MS;
    }

    public static boolean aiContainerIntentPending() {
        return System.currentTimeMillis() < intentUntilMs;
    }

    public static void clearAiContainerIntent() {
        intentUntilMs = 0L;
    }

    // ── 通用右键（本地层只负责"看向 + 按住右键"）─────────────────────

    /**
     * 通用右键：**不判断命中的是方块/实体，也不判断手上是什么** —— 直接按住原版 use 键，
     * 由原版自己分派（开方块 / 喂动物 / 放种子 / 吃东西…语义完全交给后端 + 原版）。
     * 之前散落在 {@code ActionExecutor} 里的 {@code useItemOn/useItem/interact} 直调都收敛到这里。
     */
    public static void genericRightClick(Minecraft mc, int holdTicks) {
        if (mc == null || mc.player == null || mc.gameMode == null) {
            return;
        }
        markAiContainerIntent();
        ControlStream.hold("use", Math.max(1, holdTicks));
    }

    /** 看向世界坐标点（并立刻同步一次旋转包，避免服务端用旧朝向裁决）。 */
    public static void aimAt(Minecraft mc, Vec3 target) {
        if (mc == null || mc.player == null || target == null) {
            return;
        }
        var p = mc.player;
        double dx = target.x - p.getX();
        double dy = target.y - p.getEyeY();
        double dz = target.z - p.getZ();
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

    /**
     * 对**实体**做一次通用右键（村民/流浪商人 ⇒ 由服务端开交易菜单）。
     *
     * <p>与方块容器同一口径：**不开任何客户端界面**，只看向 + 发交互包，然后等
     * {@code player.containerMenu} 自己变成 {@code MerchantMenu}（那一步在 tick 里等）。
     */
    public static boolean interactWithEntity(Minecraft mc, Entity entity) {
        if (mc == null || mc.player == null || mc.gameMode == null || entity == null) {
            return false;
        }
        aimAt(mc, entity.position().add(0.0, entity.getBbHeight() * 0.5, 0.0));
        mc.gameMode.interact(mc.player, entity, new EntityHitResult(entity), InteractionHand.MAIN_HAND);
        return true;
    }

    /**
     * 通用右键打开某个方块（箱子 / 工作台 / 酿酒台…都走这一条，没有按方块 id 分支）：看向方块中心
     * → 按住 use。容器由**服务端**打开，客户端这边只等 {@code player.containerMenu} 变化。
     */
    public static boolean openContainerAt(Minecraft mc, BlockPos pos, int holdTicks) {
        if (mc == null || mc.player == null || pos == null) {
            return false;
        }
        aimAt(mc, Vec3.atCenterOf(pos));
        genericRightClick(mc, holdTicks);
        return true;
    }

    /**
     * 打开就近的某个方块容器（箱子 / 工作台 / 炉子…）：先查记忆点，再就近扫描。
     * 这是**唯一**的开容器入口，blockId 是参数而非"按方块类型分派的语义分支"；
     * 打开动作本身仍然是通用右键（看向 + 按住右键）。
     */
    public static boolean openNearestContainer(Minecraft mc, String blockId) {
        if (mc == null || mc.player == null || mc.level == null || blockId == null || blockId.isBlank()) {
            return false;
        }
        BlockPos remembered = PoiMemory.findNearest(mc, java.util.Set.of(blockId), PoiMemory.INTERACTION_RADIUS);
        if (remembered != null && blockId.equals(
                BuiltInRegistries.BLOCK.getKey(mc.level.getBlockState(remembered).getBlock()).toString())) {
            return openContainerAt(mc, remembered, 6);
        }
        for (BlockFinder.Hit hit : BlockFinder.scan(mc, blockId, 4, 1)) {
            return openContainerAt(mc, new BlockPos(hit.x(), hit.y(), hit.z()), 6);
        }
        return false;
    }

    // ── 让路 ──────────────────────────────────────────────────────

    /**
     * 让路判定（所有命令入口统一调用）：玩家自己开着容器界面 → 不下任何点击。
     *
     * <p>26.3 的 {@code Minecraft} 已无 screen 字段/取值方法，界面挪到了 {@code Gui#screen()}。
     * 例外：界面挂的菜单正好就是 AI 刚发起要开的那个容器（抑制界面失败时的兜底），
     * 那不算"玩家的界面"，命令仍然可用。
     */
    /** 让路判定的公开只读版本：非 null ⇒ 现在不该下任何点击（交易等入口共用同一条口径 ✓）。 */
    public static String gateReason(Minecraft mc) {
        return gate(mc);
    }

    private static String gate(Minecraft mc) {
        boolean containerScreen = false;
        boolean ours = false;
        if (mc != null && mc.gui != null && mc.gui.screen() instanceof AbstractContainerScreen<?> screen) {
            containerScreen = true;
            ours = aiContainerIntentPending()
                    && mc.player != null && mc.player.containerMenu != null
                    && mc.player.containerMenu != mc.player.inventoryMenu
                    && screen.getMenu() == mc.player.containerMenu;
        }
        return ContainerPlan.gateReason(containerScreen, aiContainerIntentPending(), ours);
    }

    /** 当前操作的容器；没开外部容器时回落到自身背包菜单（容器 0）。 */
    public static AbstractContainerMenu activeMenu(Minecraft mc) {
        if (mc == null || mc.player == null) {
            return null;
        }
        return mc.player.containerMenu != null ? mc.player.containerMenu : mc.player.inventoryMenu;
    }

    private static boolean isInventoryMenu(Minecraft mc, AbstractContainerMenu menu) {
        return menu != null && mc.player != null && menu == mc.player.inventoryMenu;
    }

    // ── 命令实现 ──────────────────────────────────────────────────

    /** {@code container_move {from_slot,to_slot,count?}}；{@code to_slot < 0} = QUICK_MOVE。 */
    public static Result move(Minecraft mc, int fromSlot, int toSlot, int count) {
        if (mc == null || mc.player == null || mc.gameMode == null) {
            return failSimple("no_player");
        }
        String gate = gate(mc);
        if (gate != null) {
            return failSimple(gate);
        }
        AbstractContainerMenu menu = activeMenu(mc);
        if (menu == null) {
            return failSimple("no_container_open");
        }
        int total = menu.slots.size();
        if (total <= 0) {
            return failSimple("empty_menu");
        }

        if (fromSlot == ContainerPlan.CLICKED_OUTSIDE) {
            return failSimple(ContainerPlan.ERR_INVALID_SLOT); // 移动必须有真实源槽
        }
        if (toSlot == ContainerPlan.CLICKED_OUTSIDE) {
            return failSimple(ContainerPlan.ERR_INVALID_SLOT); // 移动必须有真实目标槽
        }

        ContainerPlan.Plan plan;
        if (toSlot < 0) {
            plan = ContainerPlan.quickMovePlan(fromSlot, total);
        } else {
            String slotErr = ContainerPlan.validateSlot(toSlot, total);
            if (slotErr != null) {
                return failSimple(slotErr);
            }
            ItemStack fromStack = menu.slots.get(fromSlot).getItem();
            plan = ContainerPlan.movePlan(fromSlot, toSlot, count, fromStack.getCount(), total);
        }
        if (!plan.ok()) {
            return failSimple(plan.error());
        }

        ItemStack before = menu.slots.get(fromSlot).getItem().copy();
        if (before.isEmpty()) {
            return failSimple(ContainerPlan.ERR_EMPTY_SLOT);
        }

        int clicks = 0;
        for (ContainerPlan.Click click : plan.clicks()) {
            if (clicks++ >= ContainerPlan.MAX_CLICKS_PER_COMMAND) {
                break; // 有界兜底（理论上 plan 已经保证）
            }
            applyClick(mc, menu, click.slot(), click.button(), click.kind());
        }

        // 同步校验：handleContainerInput 会把点击应用到客户端菜单，立即就能看出有没有生效
        ItemStack after = menu.slots.get(fromSlot).getItem();
        boolean moved = !sameItem(before, after) || after.getCount() != before.getCount();
        JsonObject data = new JsonObject();
        data.addProperty("container_id", menu.containerId);
        data.addProperty("from_slot", fromSlot);
        data.addProperty("to_slot", toSlot);
        data.addProperty("clicks", clicks);
        if (!moved) {
            data.addProperty("reason", ContainerPlan.ERR_NO_EFFECT);
            return Result.fail(ContainerPlan.ERR_NO_EFFECT, data);
        }
        data.addProperty("from_left", after.getCount());
        data.addProperty("reason", ContainerPlan.OK);
        StateCollector.requestRefresh();
        return Result.ok(data);
    }

    /** {@code container_click {slot,button?,click_type?}}。 */
    public static Result click(Minecraft mc, int slot, int button, String clickType) {
        if (mc == null || mc.player == null || mc.gameMode == null) {
            return failSimple("no_player");
        }
        String gate = gate(mc);
        if (gate != null) {
            return failSimple(gate);
        }
        ContainerPlan.ClickKind kind = ContainerPlan.ClickKind.fromWire(clickType);
        if (kind == null) {
            return failSimple(ContainerPlan.ERR_UNKNOWN_CLICK);
        }
        AbstractContainerMenu menu = activeMenu(mc);
        if (menu == null) {
            return failSimple("no_container_open");
        }
        String slotErr = ContainerPlan.validateSlot(slot, menu.slots.size());
        if (slotErr != null) {
            return failSimple(slotErr);
        }
        if (button != ContainerPlan.BTN_PRIMARY && button != ContainerPlan.BTN_SECONDARY) {
            return failSimple("invalid_button");
        }
        applyClick(mc, menu, slot, button, kind);
        JsonObject data = new JsonObject();
        data.addProperty("container_id", menu.containerId);
        data.addProperty("slot", slot);
        data.addProperty("button", button);
        data.addProperty("click_type", kind.wireName());
        data.addProperty("reason", ContainerPlan.OK);
        StateCollector.requestRefresh();
        return Result.ok(data);
    }

    /**
     * {@code equip_armor {}}：在自身背包（含快捷栏）里找盔甲 → 移动到对应装备槽 → **校验真的穿上了**。
     * 槽位接受规则直接问原版（{@code Slot#mayPlace}，即 {@code ItemStack#canEquip}），
     * 不手抄"哪个物品属于哪个部位"的清单；已经穿着的槽不覆盖。
     */
    public static Result equipArmor(Minecraft mc) {
        if (mc == null || mc.player == null || mc.gameMode == null) {
            return failSimple("no_player");
        }
        String gate = gate(mc);
        if (gate != null) {
            return failSimple(gate);
        }
        AbstractContainerMenu menu = activeContainer(mc);
        if (!isInventoryMenu(mc, menu)) {
            return failSimple("armor_slots_not_visible");
        }

        int src = -1;
        int target = -1;
        for (int armorSlot = ContainerPlan.ARMOR_SLOT_START; armorSlot < ContainerPlan.ARMOR_SLOT_END; armorSlot++) {
            Slot as = menu.slots.get(armorSlot);
            if (as.hasItem()) {
                continue; // 已经穿了，不覆盖
            }
            for (int i = ContainerPlan.INV_SLOT_START; i < ContainerPlan.HOTBAR_SLOT_END; i++) {
                ItemStack stack = menu.slots.get(i).getItem();
                if (stack.isEmpty() || !as.mayPlace(stack)) {
                    continue;
                }
                src = i;
                target = armorSlot;
                break;
            }
            if (src >= 0) {
                break;
            }
        }
        if (src < 0 || target < 0) {
            // 幂等：盔甲已经穿在身上（背包里没有再可穿的）→ 报 already_equipped，不重复点
            for (int armorSlot = ContainerPlan.ARMOR_SLOT_START; armorSlot < ContainerPlan.ARMOR_SLOT_END; armorSlot++) {
                ItemStack worn = menu.slots.get(armorSlot).getItem();
                if (!worn.isEmpty()) {
                    JsonObject data = new JsonObject();
                    data.addProperty("equipped", true);
                    data.addProperty("part", ContainerPlan.armorSlotName(armorSlot));
                    data.addProperty("item", BuiltInRegistries.ITEM.getKey(worn.getItem()).toString());
                    data.addProperty("reason", "already_equipped");
                    return Result.info("already_equipped", data);
                }
            }
            return failSimple("no_armor");
        }

        ItemStack wanted = menu.slots.get(src).getItem().copy();
        ContainerPlan.Plan plan = ContainerPlan.movePlan(src, target, wanted.getCount(), wanted.getCount(),
                menu.slots.size());
        if (!plan.ok()) {
            return failSimple(plan.error());
        }
        for (ContainerPlan.Click click : plan.clicks()) {
            applyClick(mc, menu, click.slot(), click.button(), click.kind());
        }

        // 校验：装备槽真的穿上了（客户端菜单 + 实体装备槽两处交叉核对）
        ItemStack equipped = menu.slots.get(target).getItem();
        boolean worn = !equipped.isEmpty() && sameItem(wanted, equipped);
        String part = ContainerPlan.armorSlotName(target);
        JsonObject data = new JsonObject();
        data.addProperty("container_id", menu.containerId);
        data.addProperty("from_slot", src);
        data.addProperty("armor_slot", target);
        data.addProperty("part", part);
        data.addProperty("item", BuiltInRegistries.ITEM.getKey(wanted.getItem()).toString());
        data.addProperty("equipped", worn);
        data.addProperty("entity_slot", worn ? equippedEntitySlot(mc, target).toString() : "");
        if (!worn) {
            data.addProperty("reason", ContainerPlan.ERR_NO_EFFECT);
            return Result.fail(ContainerPlan.ERR_NO_EFFECT, data);
        }
        data.addProperty("reason", ContainerPlan.OK);
        StateCollector.requestRefresh();
        return Result.ok(data);
    }

    /**
     * {@code craft_via_recipe {item,count?}}（按产物 id 入口）：跨原版工作站找配方（工作台优先）
     * → 原版**配方放置** API {@code MultiPlayerGameMode.handlePlaceRecipe(containerId, RecipeDisplayId, useMaxItems)}
     * → 结果槽 QUICK_MOVE 收进背包。**不自己摆动合成格子。**
     * 拿不到配方表（非单人）/ 无配方 / 工作站不符 / 缺材料 → 如实失败。
     * 另一个入口是 {@link #craftViaDisplay(Minecraft, int, int)}（search_recipes 的 display_id）。
     */
    public static Result craftViaRecipe(Minecraft mc, String item, int count) {
        if (mc == null || mc.player == null || mc.gameMode == null) {
            return failSimple("no_player");
        }
        if (item == null || item.isBlank()) {
            return failSimple("missing_item");
        }
        if (mc.getSingleplayerServer() == null) {
            // 配方表只在集成服务端可解析（ServerboundPlaceRecipePacket 要的是服务端分配的 display id）
            return failSimple("recipe_table_unavailable");
        }
        // 跨工作站找配方（工作台优先），产物用归一后的 id 记账
        RecipeHolder<?> recipe = RecipeTools.recipeByOutput(mc, item);
        if (recipe == null) {
            return failSimple("no_recipe");
        }
        RecipeDisplayId displayId = resolveRecipeDisplayId(recipe);
        if (displayId == null) {
            return failSimple("no_recipe_display");
        }
        return placeAndCollect(mc, recipe, displayId.index(), RecipeTools.outputIdOf(mc, recipe), count);
    }

    /** {@code craft_via_recipe {display_id,count?}}：用 search_recipes 给出的精确 display_id 直接放置。 */
    public static Result craftViaDisplay(Minecraft mc, int displayId, int count) {
        if (mc == null || mc.player == null || mc.gameMode == null) {
            return failSimple("no_player");
        }
        if (mc.getSingleplayerServer() == null) {
            return failSimple("recipe_table_unavailable");
        }
        RecipeHolder<?> recipe = RecipeTools.recipeByDisplayId(mc, displayId);
        if (recipe == null) {
            return failSimple("unknown_display_id");
        }
        String output = RecipeTools.outputIdOfDisplay(mc, displayId);
        if (output == null || output.isBlank()) {
            output = RecipeTools.outputIdOf(mc, recipe);
        }
        return placeAndCollect(mc, recipe, displayId, output, count);
    }

    /**
     * 放置 + 收结果（craft_via_recipe 两条入口共用）：让路判定 → 工作站与已开容器匹配
     * （不匹配如实拒结，绝不盲目发包）→ 材料核对 → 原版 {@code handlePlaceRecipe} →
     * 结果槽 QUICK_MOVE → 登记诚实 outcome（背包真多出物品才算成功）。
     */
    private static Result placeAndCollect(Minecraft mc, RecipeHolder<?> recipe, int displayId,
                                          String outputItem, int count) {
        String gate = gate(mc);
        if (gate != null) {
            return failSimple(gate);
        }
        AbstractContainerMenu menu = activeMenu(mc);
        if (menu == null) {
            return failSimple("no_container_open");
        }
        int want = count <= 0 ? 1 : count;

        String station = RecipeTools.stationOf(recipe);
        String stationError = stationGate(mc, menu, station, recipe);
        if (stationError != null) {
            JsonObject data = new JsonObject();
            data.addProperty("station", station);
            data.addProperty("open_menu", ContainerPlan.classifyMenu(menu.getClass().getSimpleName()));
            data.addProperty("reason", stationError);
            return Result.fail(stationError, data);
        }
        if (!hasIngredients(mc, recipe)) {
            return failSimple("missing_ingredients");
        }
        int resultSlot = findResultSlot(menu);
        if (resultSlot < 0) {
            return failSimple("no_result_slot");
        }
        if (outputItem == null || outputItem.isBlank()) {
            outputItem = RecipeTools.outputIdOf(mc, recipe);
        }

        int before = ActionExecutor.countInventoryItem(mc, outputItem);
        mc.gameMode.handlePlaceRecipe(menu.containerId, new RecipeDisplayId(displayId), want > 1);
        // 收集产物（原版"合成全部"语义：shift 点击结果槽；结果槽按 ResultContainer 动态定位）
        mc.gameMode.handleContainerInput(menu.containerId, resultSlot, ContainerPlan.BTN_PRIMARY,
                ContainerInput.QUICK_MOVE, mc.player);

        craftItem = outputItem;
        craftBaseline = before;
        craftGoal = want;
        craftElapsed = 0;

        JsonObject data = new JsonObject();
        data.addProperty("container_id", menu.containerId);
        data.addProperty("item", outputItem);
        data.addProperty("count", want);
        data.addProperty("station", station);
        data.addProperty("result_slot", resultSlot);
        data.addProperty("recipe", recipe.id().identifier().toString());
        data.addProperty("display_index", displayId);
        data.addProperty("use_max_items", want > 1);
        data.addProperty("baseline", before);
        data.addProperty("reason", ContainerPlan.OK);
        return Result.ok(data);
    }

    /**
     * 工作站匹配（原版先做全：工作台 / 熔炉 / 高炉 / 烟熏炉 / 切石机 / 锻造台）：
     * 已开容器与配方工作站不符 → 如实拒结，不盲目发包。模组工作站（未归一）→ unsupported_station。
     */
    private static String stationGate(Minecraft mc, AbstractContainerMenu menu, String station,
                                      RecipeHolder<?> recipe) {
        boolean inventory = isInventoryMenu(mc, menu);
        String kind = inventory ? "inventory"
                : ContainerPlan.classifyMenu(menu.getClass().getSimpleName());
        if (RecipeQuery.S_CRAFTING.equals(station)) {
            if ("crafting".equals(kind)) {
                return null;
            }
            if (inventory) {
                return needsCraftingTable(recipe) ? "needs_crafting_table" : null;
            }
            return "wrong_station";
        }
        if (station == null || station.isBlank() || !RecipeQuery.isSupportedStation(station)) {
            return "unsupported_station";
        }
        return kind.equals(station) ? null : "wrong_station";
    }

    /** 结果槽定位：合成/熔炉/切石/锻造的结果槽底层都是 {@code ResultContainer}（javap 实证）。 */
    private static int findResultSlot(AbstractContainerMenu menu) {
        for (int i = 0; i < menu.slots.size(); i++) {
            if (menu.slots.get(i).container instanceof net.minecraft.world.inventory.ResultContainer) {
                return i;
            }
        }
        return -1;
    }

    /** 登记 craft_via_recipe 的 outcome 请求 id（由命令层设置，用于诚实回执）。 */
    public static void trackCraftOutcome(String reqId) {
        craftReqId = reqId;
    }

    /**
     * craft_via_recipe 的收尾校验（每个客户端 tick 调用）：
     * 只有"背包里真的多出目标物品"才算成功，到点没证据 → 诚实 timeout。
     */
    public static void tickCraftVerification(Minecraft mc) {
        if (craftItem == null || mc == null || mc.player == null) {
            return;
        }
        craftElapsed++;
        int now = ActionExecutor.countInventoryItem(mc, craftItem);
        boolean evidence = now >= craftBaseline + craftGoal;
        ContainerPlan.Outcome outcome = ContainerPlan.conclude(evidence, craftElapsed, CRAFT_VERIFY_MAX_TICKS);
        if (outcome == null) {
            return;
        }
        String item = craftItem;
        String reqId = craftReqId;
        craftItem = null;
        craftReqId = null;
        craftElapsed = 0;
        if (LCUMod.WIRE == null || reqId == null) {
            return;
        }
        if (outcome.success()) {
            LCUMod.WIRE.sendOutcome(reqId, "succeeded",
                    "crafted " + item + " (inventory " + now + ")", null);
        } else {
            LCUMod.WIRE.sendOutcome(reqId, "failed",
                    ContainerPlan.timeoutReason("craft", CRAFT_VERIFY_MAX_TICKS) + " item=" + item,
                    ContainerPlan.ERR_TIMEOUT);
        }
    }

    /** {@code container_close {}}：发关闭包（{@code LocalPlayer#closeContainer} → ServerboundContainerClosePacket）。 */
    public static Result close(Minecraft mc) {
        if (mc == null || mc.player == null) {
            return failSimple("no_player");
        }
        AbstractContainerMenu menu = mc.player.containerMenu;
        if (menu == null || isInventoryMenu(mc, menu)) {
            JsonObject data = new JsonObject();
            data.addProperty("closed", false);
            data.addProperty("reason", "no_container_open");
            return Result.info("no_container_open", data); // 幂等：没开容器也算成功
        }
        int id = menu.containerId;
        mc.player.closeContainer();
        JsonObject data = new JsonObject();
        data.addProperty("closed", true);
        data.addProperty("container_id", id);
        data.addProperty("reason", ContainerPlan.OK);
        StateCollector.requestRefresh();
        return Result.ok(data);
    }

    // ── state ─────────────────────────────────────────────────────

    /** state 里的 {@code open_container}：没开外部容器时是空对象 {@code {}}。 */
    public static JsonObject describeState(Minecraft mc) {
        JsonObject o = new JsonObject();
        if (mc == null || mc.player == null) {
            return o;
        }
        AbstractContainerMenu menu = mc.player.containerMenu;
        if (menu == null || isInventoryMenu(mc, menu)) {
            return o;
        }
        o.addProperty("id", menu.containerId);
        o.addProperty("title", menuTitle(menu));
        o.addProperty("slots", menu.slots.size());
        o.addProperty("kind", ContainerPlan.classifyMenu(menu.getClass().getSimpleName()));
        o.addProperty("player_inv_start", playerInvStart(mc, menu));
        // 每格物品（**只报非空槽**）：slot 用菜单槽位号 = container_move 的 from_slot/to_slot 同一套编号
        JsonArray items = new JsonArray();
        for (int i = 0; i < menu.slots.size(); i++) {
            ItemStack stack = menu.slots.get(i).getItem();
            if (stack.isEmpty()) {
                continue;
            }
            JsonObject row = new JsonObject();
            row.addProperty("slot", i);
            row.addProperty("item", BuiltInRegistries.ITEM.getKey(stack.getItem()).toString());
            row.addProperty("count", stack.getCount());
            row.addProperty("name", displayName(stack));
            items.add(row);
        }
        o.add("items", items);
        return o;
    }

    /**
     * 容器内容指纹（containerId + 每个非空槽的物品与数量），没开外部容器返回 -1。
     * state 变化检测用它：点击后 / 被玩家动过 / 关闭时都会立刻上报（关闭 → -1，items 变空）。
     */
    public static int containerFingerprint(Minecraft mc) {
        if (mc == null || mc.player == null) {
            return -1;
        }
        AbstractContainerMenu menu = mc.player.containerMenu;
        if (menu == null || isInventoryMenu(mc, menu)) {
            return -1;
        }
        int h = menu.containerId * 31 + menu.slots.size();
        for (int i = 0; i < menu.slots.size(); i++) {
            ItemStack stack = menu.slots.get(i).getItem();
            if (stack.isEmpty()) {
                continue;
            }
            h = h * 31 + i;
            h = h * 31 + stack.getCount();
            h = h * 31 + System.identityHashCode(stack.getItem());
        }
        return h;
    }

    /** 本地化显示名；拿不到就空串（绝不编）。 */
    private static String displayName(ItemStack stack) {
        try {
            var name = stack.getDisplayName();
            return name == null ? "" : name.getString();
        } catch (Exception e) {
            return "";
        }
    }

    // ── 内部 ──────────────────────────────────────────────────────

    private static AbstractContainerMenu activeContainer(Minecraft mc) {
        return mc.player.containerMenu;
    }

    private static void applyClick(Minecraft mc, AbstractContainerMenu menu, int slot, int button,
                                   ContainerPlan.ClickKind kind) {
        mc.gameMode.handleContainerInput(menu.containerId, slot, button, toContainerInput(kind), mc.player);
    }

    private static ContainerInput toContainerInput(ContainerPlan.ClickKind kind) {
        return switch (kind) {
            case PICKUP -> ContainerInput.PICKUP;
            case QUICK_MOVE -> ContainerInput.QUICK_MOVE;
            case SWAP -> ContainerInput.SWAP;
            case CLONE -> ContainerInput.CLONE;
            case THROW -> ContainerInput.THROW;
            case QUICK_CRAFT -> ContainerInput.QUICK_CRAFT;
            case PICKUP_ALL -> ContainerInput.PICKUP_ALL;
        };
    }

    private static Result failSimple(String reason) {
        JsonObject data = new JsonObject();
        data.addProperty("reason", reason);
        return Result.fail(reason, data);
    }

    private static boolean sameItem(ItemStack a, ItemStack b) {
        if (a.isEmpty() || b.isEmpty()) {
            return a.isEmpty() && b.isEmpty();
        }
        return a.getItem() == b.getItem() && ItemStack.isSameItemSameComponents(a, b);
    }

    /** 已经穿上的部位在实体上的 EquipmentSlot（交叉核对用）。 */
    private static net.minecraft.world.entity.EquipmentSlot equippedEntitySlot(Minecraft mc, int menuSlot) {
        return switch (menuSlot - ContainerPlan.ARMOR_SLOT_START) {
            case 0 -> net.minecraft.world.entity.EquipmentSlot.HEAD;
            case 1 -> net.minecraft.world.entity.EquipmentSlot.CHEST;
            case 2 -> net.minecraft.world.entity.EquipmentSlot.LEGS;
            case 3 -> net.minecraft.world.entity.EquipmentSlot.FEET;
            default -> net.minecraft.world.entity.EquipmentSlot.MAINHAND;
        };
    }

    private static String menuTitle(AbstractContainerMenu menu) {
        try {
            var key = BuiltInRegistries.MENU.getKey(menu.getType());
            return key == null ? "unknown" : key.toString();
        } catch (Exception e) {
            return "unknown";
        }
    }

    /** 玩家背包区在菜单里的起始槽位号（箱子 9x3 → 27），后端据此换算 from/to。 */
    private static int playerInvStart(Minecraft mc, AbstractContainerMenu menu) {
        var inv = mc.player.getInventory();
        for (int i = 0; i < menu.slots.size(); i++) {
            if (menu.slots.get(i).container == inv) {
                return i;
            }
        }
        return Math.max(0, menu.slots.size() - 36);
    }

    // ── 配方解析（只在集成服务端可用）──────────────────────────────

    public static RecipeHolder<?> findRecipeByResult(Minecraft mc, String itemName) {
        var server = mc.getSingleplayerServer();
        if (server == null) {
            return null;
        }
        net.minecraft.util.context.ContextMap context = SlotDisplayContext.fromLevel(mc.level);
        for (RecipeHolder<?> recipe : server.getRecipeManager().getRecipes()) {
            if (recipe.value().getType() != RecipeType.CRAFTING) {
                continue;
            }
            for (var display : recipe.value().display()) {
                var result = display.result().resolveForFirstStack(context);
                if (result == null || result.isEmpty()) {
                    continue;
                }
                String id = BuiltInRegistries.ITEM.getKey(result.getItem()).toString();
                if (id.equals(itemName) || id.endsWith(":" + itemName)) {
                    return recipe;
                }
            }
        }
        return null;
    }

    /**
     * 26.3 的配方放置包（{@code ServerboundPlaceRecipePacket}）要的是服务端给配方 display 分配的
     * {@link RecipeDisplayId}（不是 {@code RecipeHolder}），只能在集成服务端的 RecipeManager 里解析。
     */
    public static RecipeDisplayId resolveRecipeDisplayId(RecipeHolder<?> recipe) {
        var server = Minecraft.getInstance().getSingleplayerServer();
        if (server == null || recipe == null) {
            return null;
        }
        java.util.List<RecipeDisplayId> ids = new java.util.ArrayList<>();
        server.getRecipeManager().listDisplaysForRecipe(recipe.id(), entry -> ids.add(entry.id()));
        return ids.isEmpty() ? null : ids.get(0);
    }

    /** 3x3 配方需要工作台（自身背包只有 2x2）。 */
    public static boolean needsCraftingTable(RecipeHolder<?> recipe) {
        if (recipe == null) {
            return false;
        }
        for (var display : recipe.value().display()) {
            if (display instanceof ShapedCraftingRecipeDisplay shaped) {
                return shaped.width() > 2 || shaped.height() > 2;
            }
            return recipe.value().placementInfo().ingredients().size() > 4;
        }
        return false;
    }

    /** 材料是否齐（按配方声明的 ingredients 逐项核对背包数量）。 */
    public static boolean hasIngredients(Minecraft mc, RecipeHolder<?> recipe) {
        if (recipe == null) {
            return false;
        }
        List<Ingredient> ingredients = recipe.value().placementInfo().ingredients();
        for (Ingredient ingredient : ingredients) {
            if (ingredient == null || ingredient.isEmpty()) {
                continue;
            }
            boolean ok = false;
            for (var holder : ingredient.items().toList()) {
                ItemStack stack = holder.value().getDefaultInstance();
                if (stack.isEmpty()) {
                    continue;
                }
                String id = BuiltInRegistries.ITEM.getKey(stack.getItem()).toString();
                if (ActionExecutor.countInventoryItem(mc, id) > 0) {
                    ok = true;
                    break;
                }
            }
            if (!ok) {
                return false;
            }
        }
        return true;
    }
}
