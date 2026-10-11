package com.lcu.lcumod.action;

import net.minecraft.world.inventory.InventoryMenu;

import java.util.ArrayList;
import java.util.List;
import java.util.Locale;

/**
 * 容器/UI 驱动层的**纯逻辑**（零依赖自检 {@code ContainerCheck} 直接测这一层）。
 *
 * <p>为什么单独拆出来：{@code verify*} 自检的 classpath 只有 {@code main.output + test.output}，
 * 没有 Minecraft；所以凡是要被自检覆盖的逻辑，方法体/签名里都不得出现 Minecraft 类型。
 * 槽位常量直接引用 {@link InventoryMenu} 的**编译期常量**（javac 折叠成字面量），
 * 因此自检拿到的是原版真实数值，而不是手抄副本。
 *
 * <p>javap 实证（{@code net.minecraft.world.inventory.InventoryMenu}，26.3）：
 * <pre>
 * CONTAINER_ID=0 RESULT_SLOT=0 CRAFT_SLOT_START=1 CRAFT_SLOT_COUNT=4 CRAFT_SLOT_END=5
 * ARMOR_SLOT_START=5 ARMOR_SLOT_COUNT=4 ARMOR_SLOT_END=9
 * INV_SLOT_START=9 INV_SLOT_END=36 USE_ROW_SLOT_START=36 USE_ROW_SLOT_END=45 SHIELD_SLOT=45
 * static SLOT_IDS = {HEAD, CHEST, LEGS, FEET}   // → 盔甲菜单槽 5/6/7/8 = 头/胸/腿/脚
 * </pre>
 *
 * <p>唯一入口都是"计划"（点击序列 / 判定 / 原因码），不碰任何游戏对象：
 * 真正的发包与校验在 {@link ContainerController}。
 */
public final class ContainerPlan {

    // ── 槽位映射（自身背包 = 容器 0 = player.inventoryMenu）────────────────

    /** 自身背包的 containerId。 */
    public static final int INVENTORY_CONTAINER_ID = InventoryMenu.CONTAINER_ID;
    /** 合成结果槽（自身 2x2 与工作台 3x3 都是 0）。 */
    public static final int RESULT_SLOT = InventoryMenu.RESULT_SLOT;
    public static final int ARMOR_SLOT_START = InventoryMenu.ARMOR_SLOT_START;
    public static final int ARMOR_SLOT_END = InventoryMenu.ARMOR_SLOT_END;
    public static final int INV_SLOT_START = InventoryMenu.INV_SLOT_START;
    public static final int INV_SLOT_END = InventoryMenu.INV_SLOT_END;
    public static final int HOTBAR_SLOT_START = InventoryMenu.USE_ROW_SLOT_START;
    public static final int HOTBAR_SLOT_END = InventoryMenu.USE_ROW_SLOT_END;
    public static final int OFFHAND_SLOT = InventoryMenu.SHIELD_SLOT;
    /** 自身背包菜单的槽位总数（0..45）。 */
    public static final int INVENTORY_MENU_SLOTS = InventoryMenu.SHIELD_SLOT + 1;

    /** {@code AbstractContainerMenu.SLOT_CLICKED_OUTSIDE}（javap 实证 = -999）。 */
    public static final int CLICKED_OUTSIDE = -999;
    public static final int BTN_PRIMARY = 0;
    public static final int BTN_SECONDARY = 1;

    /** 单次命令允许的最大点击包数（有界：3 + 最多 64 次右键）。 */
    public static final int MAX_PARTIAL_ITEMS = 64;
    public static final int MAX_CLICKS_PER_COMMAND = 3 + MAX_PARTIAL_ITEMS;

    // ── 恒实失败原因码 ─────────────────────────────────────────────

    public static final String OK = "ok";
    public static final String ERR_INVALID_SLOT = "invalid_slot";
    public static final String ERR_SAME_SLOT = "same_slot";
    public static final String ERR_EMPTY_SLOT = "empty_slot";
    public static final String ERR_INVALID_COUNT = "invalid_count";
    public static final String ERR_COUNT_TOO_LARGE = "count_too_large";
    public static final String ERR_UNKNOWN_CLICK = "unknown_click_type";
    public static final String ERR_USER_SCREEN = "user_screen_open";
    public static final String ERR_NO_EFFECT = "move_no_effect";
    public static final String ERR_TIMEOUT = "timeout";

    private ContainerPlan() {
    }

    // ── 点击类型 ──────────────────────────────────────────────────

    /** 与 {@code net.minecraft.world.inventory.ContainerInput} 一一对应的线协议取值。 */
    public enum ClickKind {
        PICKUP, QUICK_MOVE, SWAP, CLONE, THROW, QUICK_CRAFT, PICKUP_ALL;

        public String wireName() {
            return name().toLowerCase(Locale.ROOT);
        }

        /** 线协议字符串 → 枚举；不认识返回 {@code null}（调用方必须诚实失败）。 */
        public static ClickKind fromWire(String name) {
            if (name == null) {
                return null;
            }
            String n = name.trim().toLowerCase(Locale.ROOT);
            for (ClickKind k : values()) {
                if (k.wireName().equals(n)) {
                    return k;
                }
            }
            return null;
        }
    }

    /** 一次窗口点击：槽位 + 键位 + 语义。 */
    public record Click(int slot, int button, ClickKind kind) {
    }

    /** 一串有界点击（{@code error != null} 表示不能执行，必须如实失败）。 */
    public record Plan(List<Click> clicks, String error) {
        public boolean ok() {
            return error == null;
        }

        public int size() {
            return clicks.size();
        }
    }

    /** 结果判定（自检用：只有证据才算成功，超时恒实失败）。 */
    public record Outcome(boolean success, String reason) {
    }

    // ── 校验 ──────────────────────────────────────────────────────

    /** 槽位合法性；合法返回 {@code null}，否则返回原因码。 */
    public static String validateSlot(int slot, int totalSlots) {
        if (slot == CLICKED_OUTSIDE) {
            return null; // 原版"点界面外"是合法的（丢出/放回手上物品）
        }
        if (slot < 0 || slot >= totalSlots) {
            return ERR_INVALID_SLOT;
        }
        return null;
    }

    /** 缺省（{@code requested <= 0}）= 整组；否则夹到 [1, stackCount]。 */
    public static int normalizeCount(int requested, int stackCount) {
        if (stackCount <= 0) {
            return 0;
        }
        if (requested <= 0) {
            return stackCount;
        }
        return Math.min(requested, stackCount);
    }

    /**
     * 容器菜单的合法槽位号列表 {@code 0..totalSlots-1}。
     *
     * <p>这是**唯一**一套编号：{@code open_container.items[].slot} 与 {@code container_move} 的
     * {@code from_slot/to_slot} 都用它（自检直接断言两者一致，避免出现两套映射）。
     */
    public static List<Integer> menuSlotNumbers(int totalSlots) {
        List<Integer> out = new ArrayList<>(Math.max(0, totalSlots));
        for (int i = 0; i < totalSlots; i++) {
            out.add(i);
        }
        return out;
    }

    // ── 点击计划 ──────────────────────────────────────────────────

    /**
     * 把 {@code from} 槽的 {@code count} 个物品移到 {@code to} 槽。
     *
     * <p>整组：抓取 → 放到目标 → 归还（目标为空时最后一下是 no-op）；
     * 目标已有不同物品时原版会**交换**（与原版拖拽一致，报告里写明）。
     * 部分：抓取 → {@code count} 次右键各放 1 个 → 归还余量。
     */
    public static Plan movePlan(int from, int to, int count, int stackCount, int totalSlots) {
        String bad = validateSlot(from, totalSlots);
        if (bad != null) {
            return new Plan(List.of(), bad);
        }
        bad = validateSlot(to, totalSlots);
        if (bad != null) {
            return new Plan(List.of(), bad);
        }
        if (from == to) {
            return new Plan(List.of(), ERR_SAME_SLOT);
        }
        if (stackCount <= 0) {
            return new Plan(List.of(), ERR_EMPTY_SLOT);
        }
        int n = normalizeCount(count, stackCount);
        if (n <= 0) {
            return new Plan(List.of(), ERR_INVALID_COUNT);
        }

        List<Click> clicks = new ArrayList<>(MAX_CLICKS_PER_COMMAND);
        if (n >= stackCount) {
            clicks.add(new Click(from, BTN_PRIMARY, ClickKind.PICKUP));
            clicks.add(new Click(to, BTN_PRIMARY, ClickKind.PICKUP));
            clicks.add(new Click(from, BTN_PRIMARY, ClickKind.PICKUP));
        } else {
            if (n > MAX_PARTIAL_ITEMS) {
                return new Plan(List.of(), ERR_COUNT_TOO_LARGE);
            }
            clicks.add(new Click(from, BTN_PRIMARY, ClickKind.PICKUP));
            for (int i = 0; i < n; i++) {
                clicks.add(new Click(to, BTN_SECONDARY, ClickKind.PICKUP));
            }
            clicks.add(new Click(from, BTN_PRIMARY, ClickKind.PICKUP));
        }
        return new Plan(clicks, null);
    }

    /**
     * {@code to_slot < 0} 的移动 = 原版 shift 点击（{@code QUICK_MOVE}）：
     * 自身背包里就是"背包 ↔ 快捷栏"的原版语义，箱子/工作台里是"输入 ↔ 玩家背包"。
     */
    public static Plan quickMovePlan(int from, int totalSlots) {
        String bad = validateSlot(from, totalSlots);
        if (bad != null) {
            return new Plan(List.of(), bad);
        }
        return new Plan(List.of(new Click(from, BTN_PRIMARY, ClickKind.QUICK_MOVE)), null);
    }

    // ── 盔甲槽映射 ────────────────────────────────────────────────

    /** {@code head/chest/legs/feet} → 菜单槽偏移；未知（如 body/saddle）返回 -1。 */
    public static int armorOffset(String equipmentSlotName) {
        if (equipmentSlotName == null) {
            return -1;
        }
        return switch (equipmentSlotName.trim().toLowerCase(Locale.ROOT)) {
            case "head" -> 0;
            case "chest" -> 1;
            case "legs" -> 2;
            case "feet" -> 3;
            default -> -1;
        };
    }

    /** {@code head/chest/legs/feet} → 自身背包菜单里的盔甲槽号（5/6/7/8）；未知 -1。 */
    public static int armorMenuSlot(String equipmentSlotName) {
        int off = armorOffset(equipmentSlotName);
        return off < 0 ? -1 : ARMOR_SLOT_START + off;
    }

    /** 菜单槽号 → 盔甲部位名；不是盔甲槽返回空串。 */
    public static String armorSlotName(int menuSlot) {
        int off = menuSlot - ARMOR_SLOT_START;
        if (off < 0 || off > 3) {
            return "";
        }
        return switch (off) {
            case 0 -> "head";
            case 1 -> "chest";
            case 2 -> "legs";
            default -> "feet";
        };
    }

    // ── 容器种类 ──────────────────────────────────────────────────

    /** 由菜单类名（{@code AbstractContainerMenu#getClass().getSimpleName()}）判定 kind。 */
    public static String classifyMenu(String simpleClassName) {
        if (simpleClassName == null || simpleClassName.isBlank()) {
            return "unknown";
        }
        return switch (simpleClassName) {
            case "InventoryMenu" -> "inventory";
            case "CraftingMenu", "AbstractCraftingMenu" -> "crafting";
            case "FurnaceMenu" -> "furnace";
            case "BlastFurnaceMenu" -> "blast_furnace";
            case "SmokerMenu" -> "smoker";
            case "ChestMenu" -> "chest";
            case "ShulkerBoxMenu" -> "shulker_box";
            case "HopperMenu" -> "hopper";
            case "DispenserMenu" -> "dispenser";
            case "BrewingStandMenu" -> "brewing_stand";
            case "MerchantMenu" -> "merchant";
            case "BeaconMenu" -> "beacon";
            case "EnchantmentMenu" -> "enchanting";
            case "AnvilMenu" -> "anvil";
            case "SmithingMenu" -> "smithing";
            case "GrindstoneMenu" -> "grindstone";
            case "CartographyTableMenu" -> "cartography_table";
            case "LoomMenu" -> "loom";
            case "StonecutterMenu" -> "stonecutter";
            case "CrafterMenu" -> "crafter";
            case "LecternMenu" -> "lectern";
            case "HorseInventoryMenu", "NautilusInventoryMenu", "AbstractMountInventoryMenu" -> "mount_inventory";
            default -> toSnake(simpleClassName);
        };
    }

    /** {@code FooBarMenu} → {@code foo_bar}；纯字符串处理，便于自检。 */
    public static String toSnake(String simpleClassName) {
        if (simpleClassName == null || simpleClassName.isBlank()) {
            return "unknown";
        }
        String base = simpleClassName.endsWith("Menu")
                ? simpleClassName.substring(0, simpleClassName.length() - 4)
                : simpleClassName;
        StringBuilder sb = new StringBuilder(base.length() + 4);
        for (int i = 0; i < base.length(); i++) {
            char c = base.charAt(i);
            if (Character.isUpperCase(c)) {
                if (i > 0) {
                    sb.append('_');
                }
                sb.append(Character.toLowerCase(c));
            } else {
                sb.append(c);
            }
        }
        return sb.length() == 0 ? "unknown" : sb.toString();
    }

    // ── 让路 / 抑制界面 ────────────────────────────────────────────

    /**
     * 让路条件：玩家自己开着容器界面时，绝不下任何点击。
     *
     * <p>唯一的例外是"这个界面就是 AI 自己刚发起要开的容器"（{@code aiIntentPending} 且
     * 界面挂的菜单正是当前 {@code containerMenu}）—— 那是抑制界面失败时的兜底，保证容器命令仍然可用。
     * 返回原因码或 {@code null}（可以操作）。
     */
    public static String gateReason(boolean containerScreenOpen, boolean aiIntentPending,
                                    boolean screenIsCurrentContainer) {
        if (!containerScreenOpen) {
            return null;
        }
        if (aiIntentPending && screenIsCurrentContainer) {
            return null;
        }
        return ERR_USER_SCREEN;
    }

    /**
     * 是否要抑制这次界面打开：**只有 AI 自己主动右键开容器**时才抑制，
     * 目的是"不开界面 → 不抓鼠标/不收键盘"；玩家自己打开的界面一律照常显示。
     */
    public static boolean suppressScreen(boolean aiIntentPending, boolean containerScreen) {
        return aiIntentPending && containerScreen;
    }

    // ── 结论（超时恒实失败）────────────────────────────────────────

    /**
     * 有证据 → 成功；到点没证据 → 失败（timeout）；都没到 → {@code null}（继续等）。
     * 不存在"没证据也算成功"的分支。
     */
    public static Outcome conclude(boolean evidence, int elapsedTicks, int maxTicks) {
        if (evidence) {
            return new Outcome(true, OK);
        }
        if (elapsedTicks >= maxTicks) {
            return new Outcome(false, ERR_TIMEOUT);
        }
        return null;
    }

    /** 超时原因码（带操作名与上限，便于后端区分）。 */
    public static String timeoutReason(String op, int maxTicks) {
        return (op == null || op.isBlank() ? "op" : op) + ":" + ERR_TIMEOUT + "(" + maxTicks + "t)";
    }
}
