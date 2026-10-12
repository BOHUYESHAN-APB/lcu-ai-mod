package com.lcu.lcumod.action;

/**
 * 武器选择与**禁用名单**（纯字符串逻辑，**零依赖** —— 零依赖自检可以直接测它，不加载任何
 * Minecraft 类，避免 {@code NoClassDefFoundError}）。
 *
 * <p>用户口径（2026-10-12 实机）：
 * <ul>
 *   <li>**重锤（{@code _mace}）与矛（{@code _spear}）禁止 AI 使用**：误伤更严重（重锤落地范围伤害、
 *       矛突刺范围长）⇒ 既不许主动切过去，也不许拿它们出手；</li>
 *   <li>旁边有玩家时**斧子可以用**（原版只有**剑**触发横扫范围伤害，斧子误伤面小）；</li>
 *   <li>平时偏好不变：剑 &gt; 三叉戟 &gt; 斧，材质 netherite &gt; diamond &gt; iron &gt; stone &gt; gold &gt; 其他。</li>
 * </ul>
 *
 * <p>物品 id 已用 {@code javap} 实证（26.3）：存在 {@code MACE} 与
 * {@code WOODEN/STONE/COPPER/IRON/GOLDEN/DIAMOND/NETHERITE_SPEAR}。
 */
public final class WeaponPolicy {

    /** 禁用后缀：AI 永远不许用它们出手，也不许主动切过去。 */
    private static final String[] BANNED_SUFFIXES = {"_mace", "_spear"};

    private WeaponPolicy() {
    }

    /**
     * 取注册名的"路径"部分并小写（`minecraft:netherite_axe` → `netherite_axe`）。
     *
     * <p>为什么要剥命名空间：**重锤与三叉戟没有材质前缀** —— 注册名就是 `minecraft:mace` /
     * `minecraft:trident`，用 `_mace`/`_trident` 这种后缀匹配**永远匹配不上**（旧代码就有这个 bug：
     * 三叉戟从没被当成武器、重锤也没被禁掉）。矛有材质前缀（`wooden_spear`…），两种都要覆盖。
     */
    private static String path(String itemId) {
        String id = itemId == null ? "" : itemId.trim().toLowerCase();
        int colon = id.indexOf(':');
        return colon >= 0 ? id.substring(colon + 1) : id;
    }

    /** 是否属于**禁止 AI 使用**的武器（重锤 / 矛，含全部材质）。 */
    public static boolean isBanned(String itemId) {
        String p = path(itemId);
        return p.equals("mace") || p.endsWith("_mace")
                || p.equals("spear") || p.endsWith("_spear");
    }

    /**
     * 旁边有玩家时是否**安全**（不会横扫到旁人）：斧子与空手安全，剑/其他武器不安全。
     *
     * <p>依据：原版只有**剑**会触发横扫（sweep）范围伤害 —— 所以"旁边有人就用斧子"。
     */
    public static boolean isSweepSafe(String itemId) {
        String p = path(itemId);
        if (p.isEmpty()) {
            return true; // 空手：不造成伤害，也不会横扫
        }
        return p.equals("axe") || p.endsWith("_axe");
    }

    /** 武器评分（0 = 不是武器**或已被禁用** ⇒ 永不主动切过去）；数值越大越优先。 */
    public static int score(String itemId) {
        String p = path(itemId);
        if (p.isEmpty() || isBanned(p)) {
            return 0;
        }
        int kind;
        if (p.equals("sword") || p.endsWith("_sword")) {
            kind = 40;
        } else if (p.equals("trident")) {
            kind = 35;
        } else if (p.equals("axe") || p.endsWith("_axe")) {
            kind = 30;
        } else {
            return 0;
        }
        int tier;
        if (p.contains("netherite")) {
            tier = 6;
        } else if (p.contains("diamond")) {
            tier = 5;
        } else if (p.contains("iron")) {
            tier = 4;
        } else if (p.contains("stone")) {
            tier = 3;
        } else if (p.contains("gold")) {
            tier = 2;
        } else {
            tier = 1;
        }
        return kind + tier;
    }
}
