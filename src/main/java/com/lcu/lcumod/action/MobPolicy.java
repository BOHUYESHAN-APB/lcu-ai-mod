package com.lcu.lcumod.action;

import java.util.Set;

/**
 * 生物对待策略表：**高危险 / 敌对 / 中立 / 被动 / 未知**，本地反射与后端模型**共用同一张表**
 * （纯字符串逻辑，**零依赖** —— 零依赖自检可以直接测它，不加载任何 Minecraft 类）。
 *
 * <p>用户口径（他拍的板）：
 * <ul>
 *   <li>**高危险的不主动打**（监守者 / 凋灵 / 末影龙）：连**还手都不还手**，如实交回模型 / 逃生 ——
 *       跟它们近战是拿命换血；</li>
 *   <li>**中立的不惹**（末影人 / 僵尸猪灵 / 猪灵 / 狼 / 蜜蜂 / 北极熊 / 铁傀儡 / 雪傀儡 / 羊驼 /
 *       海豚 …，**村民也不惹**）：只有**它先动手**才还手；</li>
 *   <li>**只有该处理的才处理**：被动（牛/猪/羊/鸡/兔/鱼…）是能猎的肉与资源；认不出的**不许**默认
 *       当成敌对 —— 表里没有就是 {@link Tier#UNKNOWN}。</li>
 * </ul>
 *
 * <p>参考项目结论（已侦察，直接可用）：
 * <ul>
 *   <li>mindcraft 只有二分（hostile / 7 名 huntable 白名单），**没有**危险等级表；它对
 *       **creeper/phantom 不贴脸**（保持 ≥4 格才出手）—— {@link #preferDistance} 就是这一条；</li>
 *   <li>minecraft-data 的分类有缺陷：`enderman`/`zombified_piglin`/`piglin` 被标成 hostile（会被主动打），
 *       `wolf`/`bee`/`polar_bear` 被标成 animal（永不敌对），`iron_golem`/`snow_golem` 标成 mob。
 *       所以这张表**手写显式**，不抄它的分类。</li>
 * </ul>
 *
 * <p>与既有硬约束的关系：本表只回答"**该不该打**"，不回答"打不打得中"。出手仍然**必须**过
 * {@link AttackRules}（视线 / 够得着 / 不误伤）与 {@link WeaponPolicy}（重锤 / 矛禁用）。
 *
 * <p>表里的名字是**注册名的 path**（`minecraft:ender_dragon` → `ender_dragon`），已用
 * {@code javap} 对着 26.3 的 {@code EntityTypes} 常量表逐个核过。
 */
public final class MobPolicy {

    /** 对待档位。**认不出 ⇒ UNKNOWN，绝不默认 HOSTILE**。 */
    public enum Tier {
        /** 高危险（监守者/凋灵/末影龙）：不主动打，也不还手 → 交回模型 / 逃生。 */
        DANGEROUS("dangerous"),
        /** 敌对（僵尸/骷髅/苦力怕…）：该处理的目标，可主动打、被打必还手。 */
        HOSTILE("hostile"),
        /** 中立（末影人/狼/铁傀儡/村民…）：不惹；**只有它先动手**才还手。 */
        NEUTRAL("neutral"),
        /** 被动（牛羊猪鸡兔鱼…）：能猎的肉与资源（"该处理的"），不还手也照样能打。 */
        PASSIVE("passive"),
        /** 表里没有（含自定义/新版本生物）：如实说"不认识"，**不许**当成敌对。 */
        UNKNOWN("unknown");

        /** 发给后端/进日志的**档位名**（`entities.by_kind` 的键、`scan_entities` 的 `kind` 过滤器）。 */
        private final String kind;

        Tier(String kind) {
            this.kind = kind;
        }

        /** 对外那套小写名字。**名字就长在这里**，省得别处再抄一份字符串。 */
        public String kind() {
            return kind;
        }

        /** 按名字（枚举名，大小写不敏感）找回档位；认不出 ⇒ {@link #UNKNOWN}，**不猜**。 */
        public static Tier named(String name) {
            if (name == null) {
                return UNKNOWN;
            }
            String wanted = name.trim();
            for (Tier tier : values()) {
                if (tier.name().equalsIgnoreCase(wanted)) {
                    return tier;
                }
            }
            return UNKNOWN;
        }
    }

    /**
     * 摘要（{@link PerceptionView}）与按需扫描（{@link EntityScan}）**共用**的归类。
     *
     * <p>玩家与掉落物优先（它们不是"档位"）；其余由 {@link Tier#named} 归，认不出 ⇒ {@code unknown}。
     * **只此一处** —— 两份拷贝迟早会漂移，弄出"摘要把这个怪算敌对、按需扫描算中立"这种自相矛盾的
     * 报告，而模型会照着其中一份动手。
     */
    public static String kindOf(String tierName, boolean player, boolean item) {
        if (player) {
            return "player";
        }
        if (item) {
            return "item";
        }
        return Tier.named(tierName).kind();
    }

    /**
     * creeper / phantom 的**保持距离**（mindcraft 规则）：不贴脸，至少 4 格。
     *
     * <p>近身苦力怕会炸、幻翼会俯冲 —— 这两类"打得到也要先站稳"。
     */
    public static final double STANDOFF_DISTANCE = 4.0;

    /** 高危险：不主动打、也不还手（`mayInitiate` / `mayRetaliate` 都 false）。 */
    private static final Set<String> DANGEROUS = Set.of(
            "warden", "wither", "ender_dragon");

    /** 敌对：原版会主动追着玩家打的怪（`Enemy` 那一路），可主动处理。 */
    private static final Set<String> HOSTILE = Set.of(
            "blaze", "bogged", "breeze", "cave_spider", "creaking", "creeper", "drowned",
            "elder_guardian", "endermite", "evoker", "ghast", "giant", "guardian", "hoglin",
            "husk", "illusioner", "magma_cube", "parched", "phantom", "piglin_brute",
            "pillager", "ravager", "shulker", "silverfish", "skeleton", "slime", "spider",
            "stray", "sulfur_cube", "vex", "vindicator", "witch", "wither_skeleton", "zoglin",
            "zombie", "zombie_villager");

    /**
     * 中立：**不惹**。要么是被打了才反击（末影人/僵尸猪灵/蜜蜂/北极熊/羊驼/山羊/熊猫/河豚…），
     * 要么是"打了会给玩家惹麻烦"的（铁傀儡/雪傀儡/铜傀儡是村庄守卫，狼是宠物）。
     *
     * <p>**村民与流浪商人也在这一档 —— 村民不惹**（交易对象，且打了会激怒铁傀儡/刷袭击）；
     * 他们本来就不会还手，放中立等于"永久不许作为目标"。
     */
    private static final Set<String> NEUTRAL = Set.of(
            "bee", "copper_golem", "dolphin", "enderman", "goat", "iron_golem", "llama",
            "panda", "piglin", "polar_bear", "pufferfish", "snow_golem", "trader_llama",
            "villager", "wandering_trader", "wolf", "zombified_piglin");

    /** 被动：能猎的（肉/皮革/羽毛…）。"该处理的"里最没争议的一档。 */
    private static final Set<String> PASSIVE = Set.of(
            "allay", "armadillo", "axolotl", "bat", "camel", "camel_husk", "cat", "chicken",
            "cod", "cow", "donkey", "fox", "frog", "glow_squid", "happy_ghast", "horse",
            "mooshroom", "mule", "nautilus", "ocelot", "parrot", "pig", "rabbit", "salmon",
            "sheep", "skeleton_horse", "sniffer", "squid", "strider", "tadpole",
            "tropical_fish", "turtle", "zombie_horse");

    /** 需要保持距离的（{@link #preferDistance} 用）。 */
    private static final Set<String> STANDOFF = Set.of("creeper", "phantom");

    private MobPolicy() {
    }

    /**
     * 取注册名的"路径"部分并小写（`minecraft:creeper` → `creeper`；`CREEPER` → `creeper`）。
     *
     * <p>与 {@link WeaponPolicy} 同口径：剥命名空间是为了"带不带 `minecraft:` 都认"，而**大小写
     * 无关**是因为上报来的可能是注册名、也可能是显示名派生出来的写法。
     */
    private static String path(String entityTypeId) {
        String id = entityTypeId == null ? "" : entityTypeId.trim().toLowerCase();
        int colon = id.indexOf(':');
        return colon >= 0 ? id.substring(colon + 1) : id;
    }

    /**
     * 这个生物属于哪一档（`minecraft:enderman` / `enderman` / `ENDERMAN` 等价）。
     *
     * <p>认不出就是 {@link Tier#UNKNOWN} —— **绝不默认 HOSTILE**（表里没写 ≠ 会打我们）。
     */
    public static Tier tier(String entityTypeId) {
        String p = path(entityTypeId);
        if (p.isEmpty()) {
            return Tier.UNKNOWN;
        }
        if (DANGEROUS.contains(p)) {
            return Tier.DANGEROUS;
        }
        if (HOSTILE.contains(p)) {
            return Tier.HOSTILE;
        }
        if (NEUTRAL.contains(p)) {
            return Tier.NEUTRAL;
        }
        if (PASSIVE.contains(p)) {
            return Tier.PASSIVE;
        }
        return Tier.UNKNOWN;
    }

    /**
     * 能不能**主动**开打（没有任何人先动手）。
     *
     * <p>高危险 / 中立 ⇒ **false**（不主动打 / 不惹）；敌对 / 被动 / 未知 ⇒ true。
     *
     * <p>被动为 true 是口径本身："牛是能猎的"；未知为 true 只是"表里没写、不预先禁止"的如实说法
     * （**不是**把未知当敌对）—— 生存反射另外只挑**明确敌对**档下手，见
     * {@code JavaAutonomousBehavior#worthFighting}。
     */
    public static boolean mayInitiate(Tier tier) {
        if (tier == null) {
            return false;
        }
        return tier != Tier.DANGEROUS && tier != Tier.NEUTRAL;
    }

    /**
     * 能不能**还手**（`itHitUsFirst` = 这一下是它打的）。
     *
     * <p>高危险 ⇒ **如实 false**（连还手都不还手，交回模型 / 逃生）；敌对 / 未知 ⇒ true；
     * 中立 / 被动 ⇒ 只有**它先动手**才还手（"中立的不惹"）。
     */
    public static boolean mayRetaliate(Tier tier, boolean itHitUsFirst) {
        if (tier == null) {
            return false;
        }
        if (tier == Tier.DANGEROUS) {
            return false;
        }
        if (tier == Tier.HOSTILE || tier == Tier.UNKNOWN) {
            return true;
        }
        return itHitUsFirst;
    }

    /**
     * 出手前要先把距离拉到多少（0 = 没要求）。
     *
     * <p>creeper / phantom ⇒ {@value #STANDOFF_DISTANCE} 格（mindcraft 规则）：贴脸会被炸 / 被俯冲，
     * 所以反射**不往它身上贴**，只站在这个距离外等它进攻击距离。
     */
    public static double preferDistance(String entityTypeId) {
        return STANDOFF.contains(path(entityTypeId)) ? STANDOFF_DISTANCE : 0.0;
    }
}
