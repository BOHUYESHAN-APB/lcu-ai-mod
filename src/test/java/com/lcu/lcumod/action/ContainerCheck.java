package com.lcu.lcumod.action;

import java.util.List;
import java.util.Locale;

/**
 * Zero-dependency self-check for {@link ContainerPlan} (pure logic of the container/UI driving layer).
 *
 * <p>Same shape as the other verify* checks: plain Java + JavaExec (see {@code verifyContainer} in
 * build.gradle), no third-party dependency, runs as part of {@code build} / {@code check}.
 * Assertion text is ASCII (Windows console codepage is GBK).
 *
 * <p>Covers the five contracts:
 * <ol>
 *   <li>slot mapping (own inventory menu: armor 5-8 / inventory 9-35 / hotbar 36-44 / offhand 45 / container 0);</li>
 *   <li>QUICK_MOVE arguments (to_slot &lt; 0 sends one shift-click; full move = three PICKUP; bounded);</li>
 *   <li>give-way condition (player has a container screen open -&gt; refuse, reason="user_screen_open");</li>
 *   <li>idempotency (same input -&gt; same click plan; close with nothing open = success no-op);</li>
 *   <li>invalid slots / timeout always fail honestly (never report success without evidence).</li>
 * </ol>
 */
public final class ContainerCheck {

    private static int passed = 0;
    private static int failed = 0;

    private ContainerCheck() {
    }

    public static void main(String[] args) {
        slotMappingMatchesVanilla();
        armorSlotMapping();
        clickTypesMatchWireNames();
        quickMovePlanIsSingleShiftClick();
        fullMoveIsThreePickups();
        partialMoveIsBounded();
        moveRejectsInvalidSlots();
        countNormalizationAndIdempotency();
        gateBlocksWhenUserScreenOpen();
        suppressOnlyAiInitiated();
        itemSlotsShareMoveNumbering();
        noEvidenceNeverSucceeds();
        classifyMenuKinds();
        timeoutReasonIsHonest();

        System.out.println();
        System.out.println("verifyContainer: " + passed + " passed, " + failed + " failed");
        if (failed > 0) {
            System.exit(1);
        }
    }

    // (1) slot mapping: compared against the real vanilla InventoryMenu compile-time constants
    private static void slotMappingMatchesVanilla() {
        check("container id = 0 (own inventory)", ContainerPlan.INVENTORY_CONTAINER_ID == 0);
        check("result slot = 0", ContainerPlan.RESULT_SLOT == 0);
        check("armor slots 5..9", ContainerPlan.ARMOR_SLOT_START == 5 && ContainerPlan.ARMOR_SLOT_END == 9);
        check("inventory slots 9..36", ContainerPlan.INV_SLOT_START == 9 && ContainerPlan.INV_SLOT_END == 36);
        check("hotbar slots 36..45", ContainerPlan.HOTBAR_SLOT_START == 36 && ContainerPlan.HOTBAR_SLOT_END == 45);
        check("offhand slot = 45", ContainerPlan.OFFHAND_SLOT == 45);
        check("inventory menu = 46 slots", ContainerPlan.INVENTORY_MENU_SLOTS == 46);
        check("clicked-outside = -999", ContainerPlan.CLICKED_OUTSIDE == -999);
    }

    // (1) armor menu slots: javap-verified SLOT_IDS = {HEAD,CHEST,LEGS,FEET} -> 5/6/7/8
    private static void armorSlotMapping() {
        check("head -> 5", ContainerPlan.armorMenuSlot("head") == 5);
        check("chest -> 6", ContainerPlan.armorMenuSlot("chest") == 6);
        check("legs -> 7", ContainerPlan.armorMenuSlot("legs") == 7);
        check("feet -> 8", ContainerPlan.armorMenuSlot("feet") == 8);
        check("body (not a player armor slot) -> -1", ContainerPlan.armorMenuSlot("body") == -1);
        check("slot 5 name = head", "head".equals(ContainerPlan.armorSlotName(5)));
        check("slot 8 name = feet", "feet".equals(ContainerPlan.armorSlotName(8)));
        check("slot 9 is not an armor slot", ContainerPlan.armorSlotName(9).isEmpty());
    }

    // (2) click_type wire values = lowercased ContainerInput enum names
    private static void clickTypesMatchWireNames() {
        for (ContainerPlan.ClickKind k : ContainerPlan.ClickKind.values()) {
            check("click_type roundtrip " + k.wireName(), ContainerPlan.ClickKind.fromWire(k.wireName()) == k);
        }
        check("uppercase parses", ContainerPlan.ClickKind.fromWire("QUICK_MOVE") == ContainerPlan.ClickKind.QUICK_MOVE);
        check("unknown click_type -> null", ContainerPlan.ClickKind.fromWire("nope") == null);
        check("null click_type -> null", ContainerPlan.ClickKind.fromWire(null) == null);
        check("7 click semantics", ContainerPlan.ClickKind.values().length == 7);
    }

    // (2) QUICK_MOVE arguments: exactly one click, button 0, kind QUICK_MOVE
    private static void quickMovePlanIsSingleShiftClick() {
        ContainerPlan.Plan p = ContainerPlan.quickMovePlan(9, ContainerPlan.INVENTORY_MENU_SLOTS);
        check("quick_move plan is executable", p.ok());
        check("quick_move = exactly 1 click", p.size() == 1);
        if (p.ok() && p.size() == 1) {
            ContainerPlan.Click c = p.clicks().get(0);
            check("quick_move slot = 9", c.slot() == 9);
            check("quick_move button = 0", c.button() == ContainerPlan.BTN_PRIMARY);
            check("quick_move kind = QUICK_MOVE", c.kind() == ContainerPlan.ClickKind.QUICK_MOVE);
        }
        check("quick_move invalid slot fails honestly",
                ContainerPlan.ERR_INVALID_SLOT.equals(ContainerPlan.quickMovePlan(99, 46).error()));
    }

    // (2) full-stack move = pickup / place / give-back, three PICKUP clicks in order
    private static void fullMoveIsThreePickups() {
        ContainerPlan.Plan p = ContainerPlan.movePlan(10, 40, 0, 64, ContainerPlan.INVENTORY_MENU_SLOTS);
        check("full move is executable", p.ok());
        check("full move = 3 clicks", p.size() == 3);
        if (p.ok() && p.size() == 3) {
            List<ContainerPlan.Click> c = p.clicks();
            check("click 1 picks up from", c.get(0).slot() == 10 && c.get(0).kind() == ContainerPlan.ClickKind.PICKUP);
            check("click 2 places into to", c.get(1).slot() == 40 && c.get(1).button() == ContainerPlan.BTN_PRIMARY);
            check("click 3 gives back to from", c.get(2).slot() == 10);
        }
        check("count == stack size is a full move", ContainerPlan.movePlan(10, 40, 64, 64, 46).size() == 3);
    }

    // (2) partial move = pickup + N right-clicks + give-back, and the click count stays bounded
    private static void partialMoveIsBounded() {
        ContainerPlan.Plan p = ContainerPlan.movePlan(10, 40, 3, 64, ContainerPlan.INVENTORY_MENU_SLOTS);
        check("partial move of 3 = 5 clicks", p.ok() && p.size() == 5);
        if (p.ok() && p.size() == 5) {
            check("each partial right-click moves one", p.clicks().get(1).button() == ContainerPlan.BTN_SECONDARY
                    && p.clicks().get(1).kind() == ContainerPlan.ClickKind.PICKUP);
        }
        ContainerPlan.Plan almost = ContainerPlan.movePlan(10, 40, 63, 64, 46);
        check("partial move 63 = 65 clicks", almost.size() == 65);
        boolean bounded = true;
        for (int n = 1; n <= 64; n++) {
            if (ContainerPlan.movePlan(10, 40, n, 64, 46).size() > ContainerPlan.MAX_CLICKS_PER_COMMAND) {
                bounded = false;
                break;
            }
        }
        check("every count 1..64 stays within MAX_CLICKS_PER_COMMAND", bounded);
        check("bounded cap = 3 + 64", ContainerPlan.MAX_CLICKS_PER_COMMAND == 67);
        check("oversized partial fails honestly",
                ContainerPlan.ERR_COUNT_TOO_LARGE.equals(
                        ContainerPlan.movePlan(10, 40, ContainerPlan.MAX_PARTIAL_ITEMS + 1, 128, 46).error()));
    }

    // (5) invalid slots: out of range / same slot / empty slot all yield machine-readable reasons
    private static void moveRejectsInvalidSlots() {
        check("from out of range -> invalid_slot",
                ContainerPlan.ERR_INVALID_SLOT.equals(ContainerPlan.movePlan(46, 10, 0, 64, 46).error()));
        check("to out of range -> invalid_slot",
                ContainerPlan.ERR_INVALID_SLOT.equals(ContainerPlan.movePlan(10, -5, 0, 64, 46).error()));
        check("from == to -> same_slot",
                ContainerPlan.ERR_SAME_SLOT.equals(ContainerPlan.movePlan(10, 10, 0, 64, 46).error()));
        check("empty source -> empty_slot",
                ContainerPlan.ERR_EMPTY_SLOT.equals(ContainerPlan.movePlan(10, 40, 0, 0, 46).error()));
        check("click allows -999 (clicked outside)",
                ContainerPlan.validateSlot(ContainerPlan.CLICKED_OUTSIDE, 46) == null);
        check("click out of range -> invalid_slot",
                ContainerPlan.ERR_INVALID_SLOT.equals(ContainerPlan.validateSlot(46, 46)));
        check("reason literal = invalid_slot", "invalid_slot".equals(ContainerPlan.ERR_INVALID_SLOT));
    }

    // (4) idempotency: identical inputs -> identical plan (repeating a command never stacks up)
    private static void countNormalizationAndIdempotency() {
        check("missing count = full stack", ContainerPlan.normalizeCount(0, 32) == 32);
        check("negative count = full stack", ContainerPlan.normalizeCount(-7, 32) == 32);
        check("count clamped to stack size", ContainerPlan.normalizeCount(999, 32) == 32);
        check("empty slot normalizes to 0", ContainerPlan.normalizeCount(5, 0) == 0);

        ContainerPlan.Plan a = ContainerPlan.movePlan(10, 40, 2, 32, 46);
        ContainerPlan.Plan b = ContainerPlan.movePlan(10, 40, 2, 32, 46);
        check("same input -> same plan (idempotent)", a.clicks().equals(b.clicks()) && a.error() == b.error());
        ContainerPlan.Plan q1 = ContainerPlan.quickMovePlan(9, 46);
        ContainerPlan.Plan q2 = ContainerPlan.quickMovePlan(9, 46);
        check("quick_move is idempotent", q1.clicks().equals(q2.clicks()));
    }

    // (3) give-way: a container screen opened by the player blocks every click
    private static void gateBlocksWhenUserScreenOpen() {
        check("no screen -> allowed", ContainerPlan.gateReason(false, false, false) == null);
        check("player screen open -> user_screen_open",
                ContainerPlan.ERR_USER_SCREEN.equals(ContainerPlan.gateReason(true, false, false)));
        check("player screen open, no AI intent -> still refused",
                ContainerPlan.ERR_USER_SCREEN.equals(ContainerPlan.gateReason(true, false, true)));
        check("AI intent + screen is the AI's own container -> allowed (fallback)",
                ContainerPlan.gateReason(true, true, true) == null);
        check("AI intent but screen is another menu -> refused",
                ContainerPlan.ERR_USER_SCREEN.equals(ContainerPlan.gateReason(true, true, false)));
        check("reason literal = user_screen_open", "user_screen_open".equals(ContainerPlan.ERR_USER_SCREEN));
    }

    // (3) screen suppression only for AI-initiated opens of container screens
    private static void suppressOnlyAiInitiated() {
        check("AI initiated + container screen -> suppress", ContainerPlan.suppressScreen(true, true));
        check("AI initiated + non-container screen -> keep", !ContainerPlan.suppressScreen(true, false));
        check("player initiated -> keep", !ContainerPlan.suppressScreen(false, true));
        check("nothing pending -> keep", !ContainerPlan.suppressScreen(false, false));
    }

    // (4-idempotency/) open_container.items[].slot must use the SAME numbering as container_move
    private static void itemSlotsShareMoveNumbering() {
        for (int total : new int[]{ContainerPlan.INVENTORY_MENU_SLOTS, 63, 90}) {
            List<Integer> numbers = ContainerPlan.menuSlotNumbers(total);
            boolean allAccepted = numbers.size() == total;
            for (int s : numbers) {
                if (ContainerPlan.validateSlot(s, total) != null) {
                    allAccepted = false;
                    break;
                }
            }
            check("items slot numbers == move slot numbers (total=" + total + ")", allAccepted);
            check("slot " + total + " out of range for total=" + total,
                    ContainerPlan.ERR_INVALID_SLOT.equals(ContainerPlan.validateSlot(total, total)));
        }
        List<Integer> own = ContainerPlan.menuSlotNumbers(ContainerPlan.INVENTORY_MENU_SLOTS);
        check("own inventory numbering covers armor 5..8, inventory 9..35, hotbar 36..44, offhand 45",
                own.contains(ContainerPlan.ARMOR_SLOT_START) && own.contains(ContainerPlan.ARMOR_SLOT_END - 1)
                        && own.contains(ContainerPlan.INV_SLOT_START) && own.contains(ContainerPlan.INV_SLOT_END - 1)
                        && own.contains(ContainerPlan.HOTBAR_SLOT_START) && own.contains(ContainerPlan.OFFHAND_SLOT));
        check("menu slot numbers are contiguous and zero-based",
                own.get(0) == 0 && own.get(own.size() - 1) == ContainerPlan.INVENTORY_MENU_SLOTS - 1);
        check("empty menu has no slots", ContainerPlan.menuSlotNumbers(0).isEmpty());
    }

    // (5) timeout always fails honestly: no evidence can never become success
    private static void noEvidenceNeverSucceeds() {
        ContainerPlan.Outcome pending = ContainerPlan.conclude(false, 0, 60);
        check("no evidence, not expired -> keep waiting (null)", pending == null);
        check("repeated call still does not claim success", ContainerPlan.conclude(false, 0, 60) == null);
        ContainerPlan.Outcome late = ContainerPlan.conclude(false, 60, 60);
        check("expired without evidence -> failed/timeout",
                late != null && !late.success() && ContainerPlan.ERR_TIMEOUT.equals(late.reason()));
        ContainerPlan.Outcome later = ContainerPlan.conclude(false, 999, 60);
        check("still failing after the deadline", later != null && !later.success());
        ContainerPlan.Outcome good = ContainerPlan.conclude(true, 1, 60);
        check("evidence -> success", good != null && good.success());
        check("evidence beats the deadline", ContainerPlan.conclude(true, 999, 60).success());
        check("reason literal = timeout", "timeout".equals(ContainerPlan.ERR_TIMEOUT));
    }

    // open_container.kind classification
    private static void classifyMenuKinds() {
        check("InventoryMenu -> inventory", "inventory".equals(ContainerPlan.classifyMenu("InventoryMenu")));
        check("ChestMenu -> chest", "chest".equals(ContainerPlan.classifyMenu("ChestMenu")));
        check("CraftingMenu -> crafting", "crafting".equals(ContainerPlan.classifyMenu("CraftingMenu")));
        check("FurnaceMenu -> furnace", "furnace".equals(ContainerPlan.classifyMenu("FurnaceMenu")));
        check("BlastFurnaceMenu -> blast_furnace",
                "blast_furnace".equals(ContainerPlan.classifyMenu("BlastFurnaceMenu")));
        check("SmokerMenu -> smoker", "smoker".equals(ContainerPlan.classifyMenu("SmokerMenu")));
        check("BrewingStandMenu -> brewing_stand",
                "brewing_stand".equals(ContainerPlan.classifyMenu("BrewingStandMenu")));
        check("ShulkerBoxMenu -> shulker_box", "shulker_box".equals(ContainerPlan.classifyMenu("ShulkerBoxMenu")));
        check("unknown menu falls back to snake_case", "foo_bar".equals(ContainerPlan.classifyMenu("FooBarMenu")));
        check("blank name -> unknown", "unknown".equals(ContainerPlan.classifyMenu(null)));
        check("toSnake is pure", "abc_xyz".equals(ContainerPlan.toSnake("AbcXyz")));
    }

    private static void timeoutReasonIsHonest() {
        String r = ContainerPlan.timeoutReason("craft", 60);
        check("timeout reason carries the op name", r.startsWith("craft"));
        check("timeout reason carries 'timeout'", r.toLowerCase(Locale.ROOT).contains("timeout"));
        check("timeout reason carries the limit", r.contains("60"));
    }

    private static void check(String name, boolean ok) {
        if (ok) {
            passed++;
            System.out.println("  [ok]   " + name);
        } else {
            failed++;
            System.out.println("  [FAIL] " + name);
        }
    }
}
