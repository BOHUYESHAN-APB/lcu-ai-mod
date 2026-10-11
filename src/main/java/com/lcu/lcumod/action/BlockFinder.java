package com.lcu.lcumod.action;

import net.minecraft.client.Minecraft;
import net.minecraft.core.BlockPos;
import net.minecraft.core.registries.BuiltInRegistries;

import java.util.ArrayList;
import java.util.Comparator;
import java.util.List;

/**
 * 「获得周围方块位置」的底座：按 id / 通配符在玩家附近**已加载区块**里找方块。
 *
 * 为什么用通配符而不是 TagKey：26.3 的标签 API 尚未在本工程验证过签名（本项目纪律：不确定的
 * API 一律 javap 验证，不许猜）。通配符表达力足够覆盖实际需求：
 *   - 精确   `minecraft:white_bed`（也接受省略命名空间的 `white_bed`）
 *   - 后缀   `*_bed`   → 16 色床 + 模组床
 *   - 前缀   `oak_*`
 *   - 包含   `*chest*`
 * 刻意**不支持**裸词 `bed`（会误匹配 `minecraft:bedrock` 基岩）。
 *
 * 纯匹配逻辑与 Minecraft 无关，可被 verifyFinder 自检；扫描部分沿用 PoiMemory 已验证的写法。
 */
public final class BlockFinder {

    public static final int DEFAULT_RADIUS = 32;
    public static final int DEFAULT_LIMIT = 8;
    /** 实际能交互到的距离（原版方块交互 4.5，留点余量）。 */
    public static final double REACH_DISTANCE = 4.0;

    private BlockFinder() {
    }

    public record Hit(int x, int y, int z, String id, double distance) {
    }

    /** 通配匹配（纯函数，见类注释的语义表）。 */
    public static boolean matches(String query, String blockId) {
        if (query == null || blockId == null) {
            return false;
        }
        String q = query.trim().toLowerCase();
        if (q.isEmpty()) {
            return false;
        }
        String id = blockId.toLowerCase();
        // 通配只针对"路径部分"（冒号之后）：`oak_*` 应该能匹配 `minecraft:oak_log`
        // （用户/LLM 想的是方块名，不是命名空间）。
        String path = id.contains(":") ? id.substring(id.indexOf(':') + 1) : id;
        if (q.length() > 2 && q.startsWith("*") && q.endsWith("*")) {
            return path.contains(q.substring(1, q.length() - 1));
        }
        if (q.length() > 1 && q.startsWith("*")) {
            return path.endsWith(q.substring(1));
        }
        if (q.length() > 1 && q.endsWith("*")) {
            return path.startsWith(q.substring(0, q.length() - 1));
        }
        return id.equals(q) || id.endsWith(":" + q) || path.equals(q);
    }

    /**
     * 在玩家周围正方体范围内扫描匹配方块，按距离升序返回前 limit 个。
     * 一次调用扫 (2r+1)^3 个方块（r=32 → 27 万次 getBlockState，几十毫秒量级）；
     * 只应在**命令**里调用，不要放进每个 tick 的热路径。
     */
    public static List<Hit> scan(Minecraft mc, String query, int radius, int limit) {
        List<Hit> hits = new ArrayList<>();
        if (mc == null || mc.player == null || mc.level == null || query == null || query.isBlank()) {
            return hits;
        }
        int r = Math.max(1, Math.min(radius, 64));
        int cap = limit > 0 ? limit : DEFAULT_LIMIT;
        BlockPos base = mc.player.blockPosition();
        double px = mc.player.getX();
        double py = mc.player.getY();
        double pz = mc.player.getZ();

        for (BlockPos pos : BlockPos.betweenClosed(base.offset(-r, -r, -r), base.offset(r, r, r))) {
            var state = mc.level.getBlockState(pos);
            if (state.isAir()) {
                continue;
            }
            String id = BuiltInRegistries.BLOCK.getKey(state.getBlock()).toString();
            if (!matches(query, id)) {
                continue;
            }
            double dx = pos.getX() + 0.5 - px;
            double dy = pos.getY() + 0.5 - py;
            double dz = pos.getZ() + 0.5 - pz;
            hits.add(new Hit(pos.getX(), pos.getY(), pos.getZ(), id, Math.sqrt(dx * dx + dy * dy + dz * dz)));
        }

        hits.sort(Comparator.comparingDouble(Hit::distance));
        return hits.size() <= cap ? hits : new ArrayList<>(hits.subList(0, cap));
    }
}
