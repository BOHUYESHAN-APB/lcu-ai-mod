package com.lcu.lcumod.action;

import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.List;

/**
 * 建造的零依赖自检（接入 {@code check}，随 {@code build} 跑）。
 *
 * <p>两部分：① {@link BuildPlan} 的纯逻辑（结构 → 有序清单 / 材料核算 / 够得着判定）——
 * 它能被本自检加载，正是因为它**零依赖**（不碰 {@code net.minecraft}、不碰 Gson ✓）；
 * ② 对主源码的静态扫描：把"复用同一条放置路径""不开界面""失败带 code"钉死。
 *
 * <p>断言信息用 ASCII（Windows 控制台 GBK）；失败非零退出。
 */
public final class BuildCheck {

    private static int passed = 0;
    private static int failed = 0;

    public static void main(String[] args) {
        planChecks();
        reachChecks();
        sourceChecks();

        System.out.println("[BuildCheck] " + passed + " checks passed, " + failed + " failed");
        if (failed > 0) {
            System.exit(1);
        }
    }

    private static void planChecks() {
        BuildPlan.Plan wall = BuildPlan.plan("wall", 3, 2);
        check("plan: wall 3x2 is 6 blocks", wall.ok() && wall.size() == 6);

        BuildPlan.Plan pen = BuildPlan.plan("pen", 3, 1);
        check("plan: pen 3x1 is a 3x3 ring minus one gate (7)", pen.ok() && pen.size() == 7);

        BuildPlan.Plan platform = BuildPlan.plan("platform", 2, 1);
        check("plan: platform 2x1 is 4 blocks", platform.ok() && platform.size() == 4);

        BuildPlan.Plan pillar = BuildPlan.plan("pillar", 1, 3);
        check("plan: pillar 1x3 is 3 blocks", pillar.ok() && pillar.size() == 3);

        // 自下而上：层号不许回退（先放脚下的 ✓）
        boolean ordered = true;
        int last = -1;
        for (BuildPlan.Block block : wall.blocks()) {
            if (block.dy() < last) {
                ordered = false;
            }
            last = block.dy();
        }
        check("plan: ordered bottom-up", ordered);

        BuildPlan.Plan unknown = BuildPlan.plan("castle", 3, 1);
        check("plan: unknown structure is an honest no (code=" + unknown.code() + ")",
                !unknown.ok() && "unknown_structure".equals(unknown.code()));

        BuildPlan.Plan empty = BuildPlan.plan("", 3, 1);
        check("plan: empty structure name is refused too",
                !empty.ok() && "unknown_structure".equals(empty.code()));

        BuildPlan.Plan huge = BuildPlan.plan("platform", 9, 1);
        check("plan: over the per-call cap is refused (81 > " + BuildPlan.MAX_BLOCKS + ")",
                !huge.ok() && "too_many_blocks".equals(huge.code()));

        check("plan: describe carries size and block count",
                BuildPlan.describe(wall, 3, 2).contains("blocks=6"));
    }

    private static void reachChecks() {
        BuildPlan.Plan platform = BuildPlan.plan("platform", 2, 1);
        // 站在基准点正上方 ⇒ 4 块都够得着（3x3 以内 ✓）
        List<BuildPlan.Block> near = BuildPlan.withinReach(platform.blocks(), 100, 64, 100,
                100.5, 64.5, 100.5, BuildPlan.DEFAULT_REACH);
        check("reach: standing at the base reaches every block", near.size() == 4);

        // 离 40 格 ⇒ 一块都够不着 ✓（这时要如实说"先走过去"，不许假装远程放 ✗）
        List<BuildPlan.Block> far = BuildPlan.withinReach(platform.blocks(), 100, 64, 100,
                140.5, 64.5, 100.5, BuildPlan.DEFAULT_REACH);
        check("reach: 40 blocks away reaches nothing", far.isEmpty());

        List<BuildPlan.Block> beyond = BuildPlan.beyondReach(platform.blocks(), 100, 64, 100,
                140.5, 64.5, 100.5, BuildPlan.DEFAULT_REACH);
        check("reach: beyondReach is the complement", beyond.size() == 4);

        check("reach: a block 3 up is still reachable",
                BuildPlan.inReach(0, 64, 0, new BuildPlan.Block(0, 3, 0),
                        0.5, 64.5, 0.5, BuildPlan.DEFAULT_REACH));
    }

    private static void sourceChecks() {
        String plan = readSource("action/BuildPlan.java");
        String executor = readSource("action/ActionExecutor.java");

        // 零依赖教训（token 那批）：纯逻辑类不许把 MC/Gson 拖进来（判据看 import ✓）。
        check("scan: BuildPlan stays dependency-free",
                !plan.isBlank() && !plan.contains("import net.minecraft")
                        && !plan.contains("import com.google.gson"));

        // 放置只有**一条**路径：place_block 与 build 都走 beginPlacement ✓（不许各写一套 ✗）
        check("scan: build reuses the shared placement core",
                executor.contains("beginPlacement(mc, targetPos, dir, requested, cmd.id())")
                        && executor.contains("beginPlacement(mc, abs.below(), Direction.UP"));

        // 全程不开界面（硬约束：全仓库 setScreen 必须为空 ✓）
        check("scan: build never opens a screen", !executor.contains("setScreen"));

        // 诚实失败：code 必须真在源码里 ✓
        for (String code : List.of("need_material", "out_of_reach", "not_enough_material",
                "block_not_placed", "place_timeout")) {
            check("scan: failure code present: " + code, executor.contains(code));
        }

        // 逐块进度 + 汇总都要有 ✓（job 层就靠 task_state + progress ✓）
        check("scan: build reports per-block progress",
                executor.contains("built \" + pendingBuildPlaced + \"/\"")
                        && executor.contains("setTaskState(\"build\", \"failed\""));
    }

    /** 读主源码（cwd = 工程根，与 WalkCheck / ProtoCheck / TradeCheck 同一约定）。 */
    private static String readSource(String relative) {
        try {
            return Files.readString(
                    Path.of("src", "main", "java", "com", "lcu", "lcumod", relative),
                    StandardCharsets.UTF_8);
        } catch (Exception e) {
            System.out.println("  INFO could not read " + relative + ": " + e.getMessage());
            return "";
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
