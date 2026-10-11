package com.lcu.lcumod.action;

import java.util.ArrayList;
import java.util.Comparator;
import java.util.List;
import java.util.Locale;

/**
 * 建造的<b>纯逻辑</b>（零依赖，可被零依赖自检直接加载）：结构 → **有序**方块清单 + 材料核算 +
 * "够得着"的子集。
 *
 * <p>设计口径（与用户一致 ✓）：
 * <ul>
 *   <li><b>一次调用只做一批</b>：同一次里所有方块必须落在伸手可及范围内；更远的**如实说**
 *       "还差 N 块、先走过去再叫一次" ✓ —— 不假装能远程放 ✗（移动与重规划是模型自己的事，
 *       它才是编排者 ✓）。</li>
 *   <li>顺序**自下而上**、同层按到基准点的距离由近到远 ✓（先放脚下的，避免"放一半悬空"）。</li>
 *   <li>**不做**本地"用什么材料"的启发式 ✗：材料由后端大模型点名 ✓（与 `place_block` 同一条口径 ✓）。</li>
 * </ul>
 */
public final class BuildPlan {

    /** 单次调用最多放多少块（超了如实拒绝 ✓ 而不是默默只放一半 ✗）。 */
    public static final int MAX_BLOCKS = 64;
    /** 默认伸手可及半径（格）；超出的一次调用直接如实拒绝 ✓。 */
    public static final double DEFAULT_REACH = 4.5;

    /** 相对基准点的偏移。 */
    public record Block(int dx, int dy, int dz) {}

    /** 计划：要么给出一串方块，要么**如实**给出 why（code 机器可读）。 */
    public record Plan(String structure, List<Block> blocks, String code, String message) {
        public boolean ok() {
            return code == null;
        }

        public int size() {
            return blocks == null ? 0 : blocks.size();
        }
    }

    private BuildPlan() {}

    /**
     * 生成结构：`wall`（长 size × 高 height × 厚 1）、`pen`（size×size 的空心围栏，留 1 格门）、
     * `platform`（size×size 地板）、`pillar`（1×1×高 height）。
     *
     * <p>认不出的结构名**如实拒绝**（code=`unknown_structure` ✓），不猜 ✗。
     */
    public static Plan plan(String structure, int size, int height) {
        String name = structure == null ? "" : structure.trim().toLowerCase(Locale.ROOT);
        if (name.isEmpty()) {
            return new Plan(name, List.of(), "unknown_structure", "没给结构名（wall / pen / platform / pillar）");
        }
        int s = Math.max(1, size);
        int h = Math.max(1, height);
        List<Block> blocks = new ArrayList<>();
        switch (name) {
            case "wall" -> {
                for (int y = 0; y < h; y++) {
                    for (int x = 0; x < s; x++) {
                        blocks.add(new Block(x, y, 0));
                    }
                }
            }
            case "pen" -> {
                // 空心围栏 + 留一格门（在 z = s-1 的中点），否则动物进不去 ✓
                int gate = s / 2;
                for (int x = 0; x < s; x++) {
                    for (int z = 0; z < s; z++) {
                        boolean edge = x == 0 || z == 0 || x == s - 1 || z == s - 1;
                        if (!edge) {
                            continue;
                        }
                        if (z == s - 1 && x == gate) {
                            continue; // 门
                        }
                        blocks.add(new Block(x, 0, z));
                    }
                }
                for (int y = 1; y < h; y++) {
                    for (int x = 0; x < s; x++) {
                        for (int z = 0; z < s; z++) {
                            boolean edge = x == 0 || z == 0 || x == s - 1 || z == s - 1;
                            if (!edge || (z == s - 1 && x == gate)) {
                                continue;
                            }
                            blocks.add(new Block(x, y, z));
                        }
                    }
                }
            }
            case "platform" -> {
                for (int x = 0; x < s; x++) {
                    for (int z = 0; z < s; z++) {
                        blocks.add(new Block(x, 0, z));
                    }
                }
            }
            case "pillar" -> {
                for (int y = 0; y < h; y++) {
                    blocks.add(new Block(0, y, 0));
                }
            }
            default -> {
                return new Plan(name, List.of(), "unknown_structure",
                        "认不出结构 \"" + structure + "\"（只有 wall / pen / platform / pillar）");
            }
        }
        blocks.sort(Comparator
                .comparingInt(Block::dy)
                .thenComparingInt(b -> b.dx() * b.dx() + b.dz() * b.dz())
                .thenComparingInt(Block::dx)
                .thenComparingInt(Block::dz));
        if (blocks.size() > MAX_BLOCKS) {
            return new Plan(name, blocks, "too_many_blocks",
                    "这个结构要 " + blocks.size() + " 块，超过单次上限 " + MAX_BLOCKS
                            + "（分成几次、或缩小尺寸）");
        }
        return new Plan(name, blocks, null, null);
    }

    /** 绝对坐标下的"够得着"判定（纯数学 ✓ 不碰世界）。 */
    public static boolean inReach(int baseX, int baseY, int baseZ, Block block,
                                  double playerX, double playerY, double playerZ, double reach) {
        double dx = (baseX + block.dx()) + 0.5 - playerX;
        double dy = (baseY + block.dy()) + 0.5 - playerY;
        double dz = (baseZ + block.dz()) + 0.5 - playerZ;
        return dx * dx + dy * dy + dz * dz <= reach * reach;
    }

    /** 够得着的那批（保持原顺序 ✓）。 */
    public static List<Block> withinReach(List<Block> blocks, int baseX, int baseY, int baseZ,
                                          double playerX, double playerY, double playerZ, double reach) {
        List<Block> out = new ArrayList<>();
        for (Block block : blocks) {
            if (inReach(baseX, baseY, baseZ, block, playerX, playerY, playerZ, reach)) {
                out.add(block);
            }
        }
        return out;
    }

    /** 够不着的那批（用来**如实**告诉模型"还差多少、先走过去"✓）。 */
    public static List<Block> beyondReach(List<Block> blocks, int baseX, int baseY, int baseZ,
                                          double playerX, double playerY, double playerZ, double reach) {
        List<Block> out = new ArrayList<>();
        for (Block block : blocks) {
            if (!inReach(baseX, baseY, baseZ, block, playerX, playerY, playerZ, reach)) {
                out.add(block);
            }
        }
        return out;
    }

    /** 给模型看的一行（不藏"要多少块/什么尺寸"✓）。 */
    public static String describe(Plan plan, int size, int height) {
        if (!plan.ok()) {
            return plan.code() + ": " + plan.message();
        }
        return plan.structure() + " size=" + Math.max(1, size) + " height=" + Math.max(1, height)
                + " blocks=" + plan.size();
    }
}
