package com.lcu.lcumod.action;

import com.lcu.lcumod.chat.TextSanitizer;
import com.lcu.lcumod.client.ScreenClickPlan;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;

/**
 * 零依赖自检（接入 {@code check}）：本批"安全/防护"五件事。
 *
 * <ol>
 *   <li><b>出站文本清洗</b>：{@code //} / 全角空格 / 行首空白 / 全角斜杠被剥掉；正常文本原样不动。</li>
 *   <li><b>指令硬黑名单</b>：大小写、命名空间前缀、前导斜杠、多余空白都要命中；白名单指令不许误伤。</li>
 *   <li><b>区域保护</b>：球/长方体命中；保护优先；多区重叠取最保守；区域内无保护项不拦。</li>
 *   <li><b>控件级点击</b>：{@code no_screen} / 玩家自己的界面 / {@code no_match}（带候选文字）/ 精确优先匹配。</li>
 *   <li><b>导航不搭方块</b>：静态扫描"寻路/自救"模块里**没有任何放方块调用**；{@code place_block} 的
 *       物品不在背包 → 如实失败（不替换）；state 物品清单带 id + count。</li>
 * </ol>
 *
 * <p>断言信息用 ASCII（Windows 控制台 GBK）；失败非零退出。
 */
public final class GuardCheck {

    private static int passed = 0;
    private static int failed = 0;

    public static void main(String[] args) {
        sanitizerChecks();
        blacklistChecks();
        zoneChecks();
        screenClickChecks();
        placeItemChecks();
        noBridgeSourceChecks();
        routeExportSizeCheck();

        System.out.println("[GuardCheck] " + passed + " checks passed, " + failed + " failed");
        if (failed > 0) {
            System.exit(1);
        }
    }

    // ── 1. 出站清洗 ──────────────────────────────────────────────

    private static void sanitizerChecks() {
        TextSanitizer.Result plain = TextSanitizer.sanitize("hello world");
        check("sanitize: plain text unchanged", !plain.sanitized() && "hello world".equals(plain.text()));

        TextSanitizer.Result chinese = TextSanitizer.sanitize("你好，我在挖矿");
        check("sanitize: chinese unchanged", !chinese.sanitized() && "你好，我在挖矿".equals(chinese.text()));

        TextSanitizer.Result slash = TextSanitizer.sanitize("/kill");
        check("sanitize: leading slash stripped", "kill".equals(slash.text()) && slash.sanitized());
        check("sanitize: leading slash flagged", slash.hadLeadingSlash() && slash.removedChars() == 1);

        check("sanitize: double slash", "kill".equals(TextSanitizer.sanitize("//kill").text()));
        check("sanitize: triple slash", "kill".equals(TextSanitizer.sanitize("///kill").text()));
        check("sanitize: fullwidth slash", "kill".equals(TextSanitizer.sanitize("／kill").text()));
        check("sanitize: leading spaces", "kill".equals(TextSanitizer.sanitize("   /kill").text()));
        check("sanitize: leading tab", "kill".equals(TextSanitizer.sanitize("\t//kill").text()));

        // 全角空格（U+3000）与 NBSP（U+00A0）
        check("sanitize: ideographic space", "kill".equals(TextSanitizer.sanitize("\u3000/kill").text()));
        check("sanitize: nbsp", "tpa Bob".equals(TextSanitizer.sanitize("\u00A0/tpa Bob").text()));

        // 多行：每一行的行首都必须是干净的
        TextSanitizer.Result multiline = TextSanitizer.sanitize("hi there\n  /kill @a\n//stop");
        check("sanitize: multiline lines", "hi there\nkill @a\nstop".equals(multiline.text()));

        // 只剥行首，行内原样（不动正文里的斜杠，避免改坏正常句子）
        check("sanitize: inner slash kept",
                TextSanitizer.sanitize("走 A/B 路线").text().equals("走 A/B 路线"));

        check("sanitize: empty", !TextSanitizer.sanitize("").sanitized()
                && TextSanitizer.sanitize("").text().isEmpty());
        check("sanitize: null safe", !TextSanitizer.sanitize(null).sanitized()
                && TextSanitizer.sanitize(null).text().isEmpty());
        check("sanitize: only slashes becomes empty", TextSanitizer.sanitize(" //").text().isEmpty());
        check("sanitize: trailing space kept", "\u0020".equals(TextSanitizer.sanitize(" // ").text()));
    }

    // ── 2. 指令硬黑名单 ──────────────────────────────────────────

    private static void blacklistChecks() {
        check("blacklist: exact", CommandBlacklist.check("kill").blocked());
        check("blacklist: uppercase", CommandBlacklist.check("KILL").blocked());
        check("blacklist: mixed case", CommandBlacklist.check("KiCk").blocked());
        check("blacklist: leading slash", CommandBlacklist.check("/kick").blocked());
        check("blacklist: double slash", CommandBlacklist.check("//stop").blocked());
        check("blacklist: extra spaces", CommandBlacklist.check("   /   gamerule keepInventory true").blocked());
        check("blacklist: namespace", CommandBlacklist.check("minecraft:kill").blocked());
        check("blacklist: plugin namespace", CommandBlacklist.check("bukkit:ban Notch").blocked());
        check("blacklist: fullwidth slash", CommandBlacklist.check("／whitelist add Bob").blocked());
        check("blacklist: ban-ip", CommandBlacklist.check("/ban-ip 1.2.3.4").blocked());
        check("blacklist: save-off", CommandBlacklist.check("save-off").blocked());
        check("blacklist: deop", CommandBlacklist.check("/DEOp Bob").blocked());
        check("blacklist: reason code", CommandBlacklist.REASON.equals(CommandBlacklist.check("/kill").reason()));
        check("blacklist: family reported", "kill".equals(CommandBlacklist.check("//KILL @a").family()));

        // 白名单指令不许误伤
        check("blacklist: locate allowed", !CommandBlacklist.check("/locate structure minecraft:village").blocked());
        check("blacklist: tp allowed", !CommandBlacklist.check("/tp Bob").blocked());
        check("blacklist: tpa allowed", !CommandBlacklist.check("tpa Bob").blocked());
        check("blacklist: tpaccept allowed", !CommandBlacklist.check("/tpaccept").blocked());
        check("blacklist: home allowed", !CommandBlacklist.check("home base").blocked());
        check("blacklist: killall NOT matched", !CommandBlacklist.check("killall").blocked());
        check("blacklist: empty allowed", !CommandBlacklist.check("").blocked());
        check("blacklist: null allowed", !CommandBlacklist.check(null).blocked());
        check("blacklist: talk with word inside", !CommandBlacklist.check("hello killjoy").blocked());
    }

    // ── 3. 区域保护 ──────────────────────────────────────────────

    private static void zoneChecks() {
        ZoneGuard.clearAll();

        // 形状校验
        check("zone: need name", !ZoneGuard.define("  ", "farm", 0, 0, 0, 10.0, null, List.of()).ok());
        check("zone: need shape", !ZoneGuard.define("z", "farm", 0, 0, 0, null, null, List.of()).ok());
        check("zone: bad radius", !ZoneGuard.define("z", "farm", 0, 0, 0, 0.0, null, List.of()).ok());
        check("zone: bad box",
                !ZoneGuard.define("z", "farm", 0, 0, 0, null,
                        new ZoneGuard.Box(5, 0, 0, 1, 1, 1), List.of()).ok());
        check("zone: bad protect token",
                !ZoneGuard.define("z", "farm", 0, 0, 0, 10.0, null, List.of("crops", "nukes")).ok());

        ZoneGuard.DefineResult farm = ZoneGuard.define(
                "home farm", "farm", 100, 64, 100, 20.0, null, List.of("crops", "blocks", "animals"));
        check("zone: define ok", farm.ok() && "home farm".equals(farm.zone().name()));
        check("zone: protect normalized order", farm.zone().protect().equals(List.of("animals", "crops", "blocks")));
        check("zone: registry size", ZoneGuard.size() == 1);

        // 球命中
        check("zone: sphere hit", ZoneGuard.block(105, 64, 100, ZoneGuard.Action.MINE, false) != null);
        check("zone: sphere miss", ZoneGuard.block(200, 64, 100, ZoneGuard.Action.MINE, false) == null);
        check("zone: sphere edge hit", ZoneGuard.block(120, 64, 100, ZoneGuard.Action.MINE, false) != null);
        check("zone: sphere just outside", ZoneGuard.block(120.1, 64, 100, ZoneGuard.Action.MINE, false) == null);

        // 规则命中：crops/blocks 拦挖与放
        check("zone: crops blocks mine",
                "crops".equals(ZoneGuard.block(100, 64, 100, ZoneGuard.Action.MINE, false).rule()));
        check("zone: blocks blocks place",
                ZoneGuard.block(100, 64, 100, ZoneGuard.Action.PLACE, false) != null);
        // animals 只拦"打动物"
        check("zone: animals blocks attack on animal",
                "animals".equals(ZoneGuard.block(100, 64, 100, ZoneGuard.Action.ATTACK, true).rule()));
        check("zone: animals does NOT block attack on player",
                ZoneGuard.block(100, 64, 100, ZoneGuard.Action.ATTACK, false) == null);

        // 区域内但没有该保护项 → 不拦
        ZoneGuard.clearAll();
        ZoneGuard.define("pen", "animal_pen", 0, 64, 0, 10.0, null, List.of("animals"));
        check("zone: no applicable rule -> not blocked",
                ZoneGuard.block(0, 64, 0, ZoneGuard.Action.MINE, false) == null);
        check("zone: containers-only zone does not block mining", block(0, 64, 0, ZoneGuard.Action.MINE, false,
                List.of("containers")) == null);

        // 多区重叠：取最保守（crops 比 blocks 保守 → 报 crops）
        ZoneGuard.clearAll();
        ZoneGuard.define("outer", "base", 0, 64, 0, 50.0, null, List.of("blocks"));
        ZoneGuard.define("inner", "farm", 0, 64, 0, 5.0, null, List.of("crops"));
        ZoneGuard.Block overlap = ZoneGuard.block(0, 64, 0, ZoneGuard.Action.MINE, false);
        check("zone: overlap most conservative", overlap != null && "crops".equals(overlap.rule()));
        check("zone: overlap zone named", overlap != null && "inner".equals(overlap.zone()));
        // 只在外圈 → blocks
        ZoneGuard.Block outerOnly = ZoneGuard.block(20, 64, 0, ZoneGuard.Action.MINE, false);
        check("zone: outer only", outerOnly != null && "blocks".equals(outerOnly.rule())
                && "outer".equals(outerOnly.zone()));

        // 长方体
        ZoneGuard.clearAll();
        ZoneGuard.DefineResult boxZone = ZoneGuard.define("silo", "storage", 0, 0, 0, null,
                new ZoneGuard.Box(10, 60, 10, 20, 70, 20), List.of("blocks"));
        check("zone: box define", boxZone.ok() && "box".equals(boxZone.zone().shape()));
        check("zone: box inside", ZoneGuard.block(15, 65, 15, ZoneGuard.Action.PLACE, false) != null);
        check("zone: box outside", ZoneGuard.block(25, 65, 15, ZoneGuard.Action.PLACE, false) == null);
        check("zone: box corner inclusive", ZoneGuard.block(10, 60, 10, ZoneGuard.Action.PLACE, false) != null);

        // 覆盖 / 删除
        ZoneGuard.define("silo", "storage", 0, 0, 0, 3.0, null, List.of("crops"));
        check("zone: same-name overwrite", ZoneGuard.size() == 1
                && "radius".equals(ZoneGuard.get("silo").shape()));
        check("zone: clear existing", ZoneGuard.clear("silo") && ZoneGuard.size() == 0);
        check("zone: clear unknown", !ZoneGuard.clear("nope"));
        check("zone: state fields shape", zoneFieldsOk());
        ZoneGuard.clearAll();
    }

    /** 用一条临时区域验证状态字段（name/kind/protect/center/radius/box）。 */
    private static boolean zoneFieldsOk() {
        ZoneGuard.clearAll();
        ZoneGuard.define("f", "farm", 1.5, 64.0, -2.5, 12.0, null, List.of("crops"));
        List<java.util.Map<String, Object>> fields = ZoneGuard.toFields();
        if (fields.size() != 1) {
            return false;
        }
        java.util.Map<String, Object> f = fields.get(0);
        Object center = f.get("center");
        return "f".equals(f.get("name")) && "farm".equals(f.get("kind"))
                && List.of("crops").equals(f.get("protect"))
                && "radius".equals(f.get("shape"))
                && Double.valueOf(12.0).equals(f.get("radius"))
                && center instanceof java.util.Map<?, ?> c && Double.valueOf(1.5).equals(c.get("x"));
    }

    /** 便捷：临时建一个单区并判定（用于"无保护项"这类小场景）。 */
    private static ZoneGuard.Block block(double x, double y, double z, ZoneGuard.Action action,
                                         boolean animal, List<String> protect) {
        ZoneGuard.clearAll();
        ZoneGuard.define("tmp", "base", x, y, z, 10.0, null, protect);
        return ZoneGuard.block(x, y, z, action, animal);
    }

    // ── 4. 控件级点击（纯逻辑部分）────────────────────────────────

    private static void screenClickChecks() {
        check("screen: no screen -> no_screen",
                ScreenClickPlan.decide(false, false, false, false) == ScreenClickPlan.Decision.NO_SCREEN);
        check("screen: pause menu -> user_screen",
                ScreenClickPlan.decide(true, true, false, false) == ScreenClickPlan.Decision.USER_SCREEN);
        check("screen: player container -> user_screen",
                ScreenClickPlan.decide(true, false, true, false) == ScreenClickPlan.Decision.USER_SCREEN);
        check("screen: ai container -> ok",
                ScreenClickPlan.decide(true, false, true, true) == ScreenClickPlan.Decision.OK);
        check("screen: server dialog -> ok",
                ScreenClickPlan.decide(true, false, false, false) == ScreenClickPlan.Decision.OK);
        check("screen: pause beats container",
                ScreenClickPlan.decide(true, true, true, true) == ScreenClickPlan.Decision.USER_SCREEN);

        List<ScreenClickPlan.Widget> widgets = List.of(
                new ScreenClickPlan.Widget(0, "Accept", "Button"),
                new ScreenClickPlan.Widget(1, "Deny", "Button"),
                new ScreenClickPlan.Widget(2, "Accept all requests", "Button"),
                new ScreenClickPlan.Widget(3, "", "ScrollableList"));

        ScreenClickPlan.Match exact = ScreenClickPlan.match(widgets, "Accept");
        check("screen: exact match wins", exact.found() && exact.index() == 0 && "Accept".equals(exact.label()));
        check("screen: exact match is case-insensitive",
                ScreenClickPlan.match(widgets, "  accept ").index() == 0);
        ScreenClickPlan.Match substring = ScreenClickPlan.match(widgets, "all requests");
        check("screen: substring match", substring.found() && substring.index() == 2);
        ScreenClickPlan.Match miss = ScreenClickPlan.match(widgets, "同意");
        check("screen: no_match path", !miss.found() && miss.index() < 0);
        check("screen: no_match lists candidates", miss.candidates().equals(List.of("Accept", "Deny", "Accept all requests")));
        check("screen: blank query -> no match", !ScreenClickPlan.match(widgets, "   ").found());
        check("screen: null query -> no match", !ScreenClickPlan.match(widgets, null).found());
        check("screen: empty widget list -> no match", !ScreenClickPlan.match(List.of(), "accept").found());
        check("screen: candidates capped", ScreenClickPlan.candidates(widgets).size() <= ScreenClickPlan.MAX_CANDIDATES);
    }

    // ── 5. place_block 的物品解析（不替换）────────────────────────

    private static void placeItemChecks() {
        List<ItemPick.Slot> inv = new ArrayList<>();
        inv.add(new ItemPick.Slot(0, "minecraft:oak_planks", 32));
        inv.add(new ItemPick.Slot(3, "minecraft:cobblestone", 64));
        inv.add(new ItemPick.Slot(12, "minecraft:dirt", 16));
        inv.add(new ItemPick.Slot(20, "minecraft:oak_planks", 8));

        check("place: not requested", ItemPick.resolve("", inv, "minecraft:oak_planks", 0)
                .outcome() == ItemPick.Outcome.NOT_REQUESTED);
        check("place: in hand", ItemPick.resolve("oak_planks", inv, "minecraft:oak_planks", 0)
                .outcome() == ItemPick.Outcome.IN_HAND);
        ItemPick.Result hotbar = ItemPick.resolve("minecraft:cobblestone", inv, "minecraft:oak_planks", 0);
        check("place: pick hotbar slot", hotbar.outcome() == ItemPick.Outcome.SELECT_HOTBAR && hotbar.slot() == 3);
        check("place: needs hotbar swap", ItemPick.resolve("dirt", inv, "oak_planks", 0)
                .outcome() == ItemPick.Outcome.SWAP_INTO_HOTBAR);
        ItemPick.Result missing = ItemPick.resolve("minecraft:diamond_block", inv, "minecraft:oak_planks", 0);
        check("place: not in inventory -> NOT_FOUND", missing.outcome() == ItemPick.Outcome.NOT_FOUND);
        check("place: NOT_FOUND is not ok", !missing.ok());
        check("place: NOT_FOUND reports id", "diamond_block".equals(missing.id()));
        check("place: no substitution (nothing selected)", missing.slot() < 0);
        check("place: empty inventory -> NOT_FOUND",
                ItemPick.resolve("stone", List.of(), null, 0).outcome() == ItemPick.Outcome.NOT_FOUND);
        check("place: namespace optional matching",
                ItemPick.normalize("minecraft:oak_planks").equals(ItemPick.normalize("OAK_PLANKS")));
        check("place: hotbar size", ItemPick.HOTBAR_SIZE == 9);
    }

    // ── 6. 导航不搭方块（静态扫描）────────────────────────────────

    private static void noBridgeSourceChecks() {
        Path root = Path.of("src", "main", "java", "com", "lcu", "lcumod");
        if (!Files.isDirectory(root)) {
            check("no-bridge: source root found (cwd=" + Path.of(".").toAbsolutePath() + ")", false);
            return;
        }
        // "寻路 / 自救 / 自主行为"这些模块里绝不许出现放方块/右键放置的调用
        String[] navModules = {
                "action/Pathfinder.java",
                "action/MovementSystem.java",
                "action/RouteFollower.java",
                "action/AfkController.java",
                "action/JavaAutonomousBehavior.java",
                "action/HumanLikeBehavior.java",
                "action/SwimReflex.java",
                "action/CombatReflex.java",
        };
        String[] forbidden = {"place_block", "handlePlace", "useItemOn", "rightClickBlock", "BlockItem"};
        for (String module : navModules) {
            Path file = root.resolve(module);
            if (!Files.isRegularFile(file)) {
                check("no-bridge: " + module + " exists", false);
                continue;
            }
            String src = read(file);
            boolean clean = true;
            String hit = "";
            for (String token : forbidden) {
                if (src.contains(token)) {
                    clean = false;
                    hit = token;
                }
            }
            check("no-bridge: " + module + " has no placement call" + (hit.isEmpty() ? "" : " (found " + hit + ")"),
                    clean);
        }

        // 放方块只有一个入口：wire 分发里的 `case "place_block"`（且只有一处）
        String executor = read(root.resolve("action/ActionExecutor.java"));
        check("no-bridge: single place dispatch", countOf(executor, "case \"place_block\"") == 1);
        check("no-bridge: single place hold (2 ticks, only in handlePlace)",
                countOf(executor, "ControlStream.hold(\"use\", 2)") == 1
                        && countOf(executor, "handlePlace(cmd)") == 1);
        check("no-bridge: pathfinder reports no-rescue policy",
                read(root.resolve("action/Pathfinder.java")).contains("NO_RESCUE_POLICY"));

        // 区域硬保护必须在**所有**破坏/放置入口生效：attack / mine_block / place_block / collect_blocks
        check("zone: enforced at attack+mine+place+collect", countOf(executor, "ZoneGuard.block(") == 4);

        // state 的物品清单必须带 id + count（后端据此自己决定用哪种方块）
        String stateCollector = read(root.resolve("state/StateCollector.java"));
        check("state: inventory has id+count",
                stateCollector.contains("item.addProperty(\"name\", stack.getItem().toString())")
                        && stateCollector.contains("item.addProperty(\"count\", stack.getCount())"));
    }

    // ── 7. route_export 帧大小实测（2000 点）─────────────────────

    private static void routeExportSizeCheck() {
        // 与 ActionExecutor#handleRouteExport 同一形状的 JSON；用 2000 点（上限）量一行有多长。
        StringBuilder sb = new StringBuilder();
        sb.append("{\"type\":\"response\",\"id\":\"route-export-1\",\"success\":true,\"data\":{\"ok\":true,")
                .append("\"name\":\"cave_to_base\",\"points\":[");
        int points = RouteMemory.MAX_POINTS;
        for (int i = 0; i < points; i++) {
            if (i > 0) {
                sb.append(',');
            }
            sb.append("{\"x\":").append(1234 + (i % 97)).append(".456789012,")
                    .append("\"y\":").append(63 + (i % 40)).append(".0,")
                    .append("\"z\":").append(-987).append(".654321098,")
                    .append("\"yaw\":").append(-137).append(".4375}");
        }
        sb.append("],\"count\":").append(points).append(",\"saved_at_tick\":123456},\"ts\":1750000000000}");
        int bytes = sb.toString().getBytes(StandardCharsets.UTF_8).length;
        System.out.println("  INFO route_export frame for " + points + " points = " + bytes + " bytes (one line)");
        check("route_export: 2000-point frame is a sane single line", bytes > 0 && bytes < 4 * 1024 * 1024);
    }

    // ── helpers ──────────────────────────────────────────────────

    private static String read(Path file) {
        try {
            return Files.readString(file, StandardCharsets.UTF_8);
        } catch (IOException e) {
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
