package com.lcu.lcumod.action;

/**
 * 零依赖自检：BlockFinder.matches 的通配语义。
 *
 * 重点防一个真 bug 类：裸词 `bed` 绝不能匹配 `minecraft:bedrock`（基岩）——
 * 否则"找床"会把基岩当成床，AI 会朝地心走。
 * 断言信息用 ASCII（Windows 控制台 GBK）；失败非零退出。
 */
public final class BlockFinderCheck {

    private static int passed = 0;
    private static int failed = 0;

    public static void main(String[] args) {
        // 精确 id
        check("exact id", BlockFinder.matches("minecraft:white_bed", "minecraft:white_bed"));
        check("id without namespace", BlockFinder.matches("white_bed", "minecraft:white_bed"));
        check("exact id negative", !BlockFinder.matches("minecraft:white_bed", "minecraft:red_bed"));

        // 后缀通配（床的正式用法）
        check("suffix all beds", BlockFinder.matches("*_bed", "minecraft:white_bed"));
        check("suffix all beds 2", BlockFinder.matches("*_bed", "minecraft:light_blue_bed"));
        check("suffix modded bed", BlockFinder.matches("*_bed", "somemod:fancy_bed"));
        check("suffix must NOT hit bedrock", !BlockFinder.matches("*_bed", "minecraft:bedrock"));

        // 裸词不得匹配（避免 bedrock 事故）
        check("bare 'bed' does NOT match bedrock", !BlockFinder.matches("bed", "minecraft:bedrock"));
        check("bare 'bed' does not match white_bed either", !BlockFinder.matches("bed", "minecraft:white_bed"));

        // 前缀 / 包含
        check("prefix", BlockFinder.matches("oak_*", "minecraft:oak_log"));
        check("prefix negative", !BlockFinder.matches("oak_*", "minecraft:birch_log"));
        check("contains", BlockFinder.matches("*chest*", "minecraft:trapped_chest"));
        check("contains negative", !BlockFinder.matches("*chest*", "minecraft:furnace"));

        // 大小写/空白
        check("case insensitive", BlockFinder.matches("  MINECRAFT:WHITE_BED ", "minecraft:white_bed"));
        check("blank query", !BlockFinder.matches("   ", "minecraft:white_bed"));
        check("null safe", !BlockFinder.matches(null, "minecraft:white_bed") && !BlockFinder.matches("x", null));

        // 扫描的默认值合理
        check("defaults sane", BlockFinder.DEFAULT_RADIUS >= 16 && BlockFinder.DEFAULT_LIMIT >= 1);
        check("reach distance sane", BlockFinder.REACH_DISTANCE >= 4.0 && BlockFinder.REACH_DISTANCE <= 6.0);

        System.out.println("[BlockFinderCheck] " + passed + " checks passed, " + failed + " failed");
        if (failed > 0) {
            System.exit(1);
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
