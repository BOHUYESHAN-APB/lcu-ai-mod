package com.lcu.lcumod.action;

import com.google.gson.JsonArray;
import com.google.gson.JsonObject;

import java.util.ArrayList;
import java.util.Comparator;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;

/**
 * 感知分层摘要：把"逐实体/逐方块**都带坐标**"改成**计数 + 最近 + 威胁（才带坐标）**。
 *
 * <p>用户口径（他拍的板）："不要把周围生物的坐标动态全都传给他 —— 在村庄/繁殖基地/农场附近会有大量
 * 生物在移动，就会大量刷屏。要设计一个**层级**：有些东西需要的时候再获取坐标，不需要的默认当
 * **无关信息屏蔽**掉。"
 *
 * <p>分层规则（冻结的接口）：
 * <ul>
 *   <li>{@code entities}：{@code count} + {@code by_kind}（七档，之和 == count）+ 全体 {@code nearest}
 *       （**不带坐标**）+ {@code threats}（**危险/敌对且 ≤16 格，最多 3 条，按距离升序**，这条才带 x/y/z）
 *       + {@code players}（**总是带坐标**，最多 8）+ {@code items}（只给聚合 + 最近一件，不逐件给坐标）；</li>
 *   <li>{@code nearby_blocks}：{@code count} + {@code by_block}（最多 12 种，按出现次数降序、同数按名字
 *       升序 —— 稳定）+ {@code nearest}（**不带坐标**）。</li>
 * </ul>
 *
 * <p><b>要具体坐标时走"按需"路径</b>：后端有 {@code find_entity} / {@code find_block} / {@code where_is}
 * 工具，需要哪一只的坐标再查，不必每帧把全场生物坐标灌进 prompt。这里删坐标**不丢能力**。
 *
 * <p>分两半（与 {@link GearReport} 同口径）：{@link Pure} 是**零依赖**的分级/聚合纯逻辑
 * （自检直接断言它的行为，不加载任何 Minecraft 类）；本类其余部分把摘要写成 {@code state_update} 的字段。
 */
public final class PerceptionView {

    private PerceptionView() {
    }

    // ── 零依赖的一半：分级与聚合（自检直接断言这一段的行为）──────────────

    /**
     * 纯逻辑：不给任何游戏类，只有字符串、布尔与数字。
     *
     * <p>单独一个嵌套类与 {@link GearReport.Pure} 同理：自检 JVM 的类路径上没有游戏类，
     * 只有这一半能被加载并直接测；碰游戏的那一半用静态扫描钉住形态。
     */
    public static final class Pure {

        /** {@code by_kind} 的七档：**顺序固定、永远都在**（即使为 0）—— 各档之和必须 == count。 */
        public static final String[] KINDS =
                {"dangerous", "hostile", "neutral", "passive", "player", "item", "unknown"};

        /** 威胁（dangerous/hostile）进 {@code threats} 的距离上限（格）。 */
        public static final double THREAT_RADIUS = 16.0;
        /** {@code threats} 最多几条（按距离升序取最近的）。 */
        public static final int MAX_THREATS = 3;
        /** {@code players} 最多几个（人最重要，永远带坐标）。 */
        public static final int MAX_PLAYERS = 8;
        /** {@code by_block} 最多列几种方块。 */
        public static final int MAX_BLOCK_KINDS = 12;

        /**
         * 一个待摘要的实体行（**零依赖**：没有游戏类，只有原始事实）。
         *
         * <p>玩家 / 掉落物 / 生物共用这一行：{@code player}、{@code item} 是显式类别，其余按
         * {@link MobPolicy.Tier} 的 {@code tier} 名字归类（{@link #kind()}）；认不出 ⇒ {@code unknown}。
         */
        public record Row(String name, String tier, boolean player, boolean item,
                          double x, double y, double z, double distance,
                          boolean living, double health,
                          String itemId, int itemCount, String owner, int age, int despawnIn) {

            /** 归到 {@code by_kind} 的哪一档（player/item 优先，其余由 tier 名推）。 */
            public String kind() {
                if (player) {
                    return "player";
                }
                if (item) {
                    return "item";
                }
                String t = tier == null ? "" : tier.trim().toUpperCase(Locale.ROOT);
                return switch (t) {
                    case "DANGEROUS" -> "dangerous";
                    case "HOSTILE" -> "hostile";
                    case "NEUTRAL" -> "neutral";
                    case "PASSIVE" -> "passive";
                    default -> "unknown";
                };
            }
        }

        /** 一个待摘要的方块行（**零依赖**：注册名 + 距离）。 */
        public record BlockRow(String blockId, double distance) {
        }

        /** 全体里最近的那一个（**刻意不带坐标** —— 要看具体在哪请按需查询）。 */
        public record Nearest(String name, String type, double distance, Double health) {
        }

        /** 一条威胁：dangerous/hostile 且 ≤ {@link #THREAT_RADIUS}；**带坐标**（要处理它就得知道它在哪）。 */
        public record Threat(String name, String type, String tier, int x, int y, int z, double distance) {
        }

        /** 一个玩家：**总是带坐标**（人最重要）。 */
        public record PlayerRow(String name, int x, int y, int z, double distance, double health) {
        }

        /** 最近一件掉落物（**不带坐标**）：谁丢的 / 掉多久了 / 还有多久消失。 */
        public record ItemNearest(String itemId, int itemCount, double distance,
                                  String owner, int age, int despawnIn) {
        }

        /** 掉落物只给聚合 + 最近一件。 */
        public record ItemSummary(int count, ItemNearest nearest) {
        }

        /** 实体的分级摘要。 */
        public record EntitySummary(int count, Map<String, Integer> byKind, Nearest nearest,
                                    List<Threat> threats, List<PlayerRow> players, ItemSummary items) {
        }

        /** 最近的一种方块（**不带坐标**）。 */
        public record BlockNearest(String blockId, double distance) {
        }

        /** 方块的聚合摘要。 */
        public record BlockSummary(int count, Map<String, Integer> byBlock, BlockNearest nearest) {
        }

        /** 家的那张床 + "绑上了没"：{@code haveBed}=没记过床；{@code bound}=原版重生点就是这张床。 */
        public record HomeBed(boolean haveBed, int x, int y, int z, boolean bound) {
        }

        private Pure() {
        }

        /** 一位小数（距离口径与既有 players/beds 一致）。 */
        static double round1(double v) {
            return Math.round(v * 10.0) / 10.0;
        }

        /**
         * 实体列表 → 分级摘要。**坐标只出现在 threats 与 players 上**；nearest/items 一律不带坐标。
         *
         * <p>{@code count} == 输入行数，且 == {@code by_kind} 各档之和（自洽，不让 web 去加）。
         */
        public static EntitySummary summarizeEntities(List<Row> rows) {
            List<Row> all = rows == null ? List.of() : rows;

            Map<String, Integer> byKind = new LinkedHashMap<>();
            for (String k : KINDS) {
                byKind.put(k, 0);
            }
            for (Row r : all) {
                byKind.merge(r.kind(), 1, Integer::sum);
            }

            Row nearest = null;
            List<Row> threatRows = new ArrayList<>();
            List<Row> playerRows = new ArrayList<>();
            List<Row> itemRows = new ArrayList<>();
            for (Row r : all) {
                if (nearest == null || r.distance() < nearest.distance()) {
                    nearest = r;
                }
                String k = r.kind();
                if (k.equals("player")) {
                    playerRows.add(r);
                } else if (k.equals("item")) {
                    itemRows.add(r);
                } else if ((k.equals("dangerous") || k.equals("hostile")) && r.distance() <= THREAT_RADIUS) {
                    threatRows.add(r);
                }
            }
            threatRows.sort(Comparator.comparingDouble(Row::distance));
            playerRows.sort(Comparator.comparingDouble(Row::distance));
            itemRows.sort(Comparator.comparingDouble(Row::distance));

            List<Threat> threats = new ArrayList<>();
            for (int i = 0; i < Math.min(MAX_THREATS, threatRows.size()); i++) {
                Row r = threatRows.get(i);
                threats.add(new Threat(r.name(), r.kind(), r.tier(),
                        (int) Math.round(r.x()), (int) Math.round(r.y()), (int) Math.round(r.z()),
                        round1(r.distance())));
            }

            List<PlayerRow> players = new ArrayList<>();
            for (int i = 0; i < Math.min(MAX_PLAYERS, playerRows.size()); i++) {
                Row r = playerRows.get(i);
                players.add(new PlayerRow(r.name(), (int) Math.round(r.x()), (int) Math.round(r.y()),
                        (int) Math.round(r.z()), round1(r.distance()), r.health()));
            }

            ItemNearest nearestItem = itemRows.isEmpty() ? null
                    : new ItemNearest(itemRows.get(0).itemId(), itemRows.get(0).itemCount(),
                            round1(itemRows.get(0).distance()), itemRows.get(0).owner(),
                            itemRows.get(0).age(), itemRows.get(0).despawnIn());

            Nearest nearestOut = nearest == null ? null
                    : new Nearest(nearest.name(), nearest.kind(), round1(nearest.distance()),
                            nearest.living() ? nearest.health() : null);

            return new EntitySummary(all.size(), byKind, nearestOut, List.copyOf(threats),
                    List.copyOf(players), new ItemSummary(itemRows.size(), nearestItem));
        }

        /**
         * 方块列表 → 聚合摘要。**不给逐方块坐标**；{@code by_block} 按出现次数降序、同数按名字升序
         * （**稳定**：同一份输入永远同一份输出），最多 {@link #MAX_BLOCK_KINDS} 种。
         */
        public static BlockSummary summarizeBlocks(List<BlockRow> rows) {
            List<BlockRow> all = rows == null ? List.of() : rows;

            Map<String, Integer> counts = new HashMap<>();
            BlockRow nearest = null;
            for (BlockRow r : all) {
                if (r == null || r.blockId() == null || r.blockId().isEmpty()) {
                    continue;
                }
                counts.merge(r.blockId(), 1, Integer::sum);
                if (nearest == null || r.distance() < nearest.distance()) {
                    nearest = r;
                }
            }

            List<String> ids = new ArrayList<>(counts.keySet());
            ids.sort((a, b) -> {
                int cmp = Integer.compare(counts.get(b), counts.get(a));
                return cmp != 0 ? cmp : a.compareTo(b);
            });
            Map<String, Integer> byBlock = new LinkedHashMap<>();
            for (int i = 0; i < Math.min(MAX_BLOCK_KINDS, ids.size()); i++) {
                byBlock.put(ids.get(i), counts.get(ids.get(i)));
            }

            BlockNearest nearestOut = nearest == null ? null
                    : new BlockNearest(nearest.blockId(), round1(nearest.distance()));
            return new BlockSummary(all.size(), byBlock, nearestOut);
        }

        /**
         * "家的床绑上没绑"的**唯一判据**：记着床 **且** 原版重生点就是它 ⇒ bound。
         *
         * <p>**唯一**：没记过床 / 拿不到重生点 / 重生点在别处 ⇒ false —— **不许编**。
         * 注意 {bound=false} 只代表"**没绑上**"，**不代表床没了**（床没了的判据是"那个位置已经不是
         * 床方块"，见 {@link PoiMemory#invalidateBedIfGone}：它会把 {@code bed} 标签摘掉）。
         */
        public static HomeBed homeBed(boolean haveBed, int x, int y, int z,
                                      boolean haveRespawn, int respawnX, int respawnY, int respawnZ) {
            boolean bound = haveBed && haveRespawn && x == respawnX && y == respawnY && z == respawnZ;
            return new HomeBed(haveBed, x, y, z, bound);
        }
    }

    // ── Minecraft 侧：把摘要写成 state_update 的字段 ───────────────────────

    /** 实体行 → 分级摘要对象（{@code state_update.entities}）。 */
    public static JsonObject entities(List<Pure.Row> rows) {
        return entitiesJson(Pure.summarizeEntities(rows));
    }

    /** 方块行 → 聚合摘要对象（{@code state_update.nearby_blocks}）。 */
    public static JsonObject blocks(List<Pure.BlockRow> rows) {
        return blocksJson(Pure.summarizeBlocks(rows));
    }

    /** 当前重生点是不是记着的那张床（`state_update.home_bed_bound`）。 */
    public static boolean homeBedBound(Pure.HomeBed bed) {
        return bed != null && bed.bound();
    }

    private static JsonObject entitiesJson(Pure.EntitySummary s) {
        JsonObject out = new JsonObject();
        out.addProperty("count", s.count());

        JsonObject byKind = new JsonObject();
        for (String k : Pure.KINDS) {
            byKind.addProperty(k, s.byKind().getOrDefault(k, 0));
        }
        out.add("by_kind", byKind);

        if (s.nearest() != null) {
            out.add("nearest", nearestJson(s.nearest()));
        }

        JsonArray threats = new JsonArray();
        for (Pure.Threat t : s.threats()) {
            threats.add(threatJson(t));
        }
        out.add("threats", threats);

        JsonArray players = new JsonArray();
        for (Pure.PlayerRow pl : s.players()) {
            players.add(playerJson(pl));
        }
        out.add("players", players);

        JsonObject items = new JsonObject();
        items.addProperty("count", s.items().count());
        if (s.items().nearest() != null) {
            items.add("nearest", itemJson(s.items().nearest()));
        }
        out.add("items", items);

        return out;
    }

    /** 全体最近的一个：**故意没有 x/y/z**（要坐标请按需查询）。 */
    private static JsonObject nearestJson(Pure.Nearest n) {
        JsonObject o = new JsonObject();
        o.addProperty("distance", n.distance());
        o.addProperty("name", n.name());
        o.addProperty("type", n.type());
        if (n.health() != null) {
            o.addProperty("health", n.health());
        }
        return o;
    }

    /** 威胁：**这条才带坐标**（要处理它就得知道它在哪），外加档位名。 */
    private static JsonObject threatJson(Pure.Threat t) {
        JsonObject o = new JsonObject();
        o.addProperty("name", t.name());
        o.addProperty("type", t.type());
        o.addProperty("tier", t.tier());
        o.addProperty("x", t.x());
        o.addProperty("y", t.y());
        o.addProperty("z", t.z());
        o.addProperty("distance", t.distance());
        return o;
    }

    /** 玩家：**永远带坐标**（人最重要）。 */
    private static JsonObject playerJson(Pure.PlayerRow p) {
        JsonObject o = new JsonObject();
        o.addProperty("name", p.name());
        o.addProperty("x", p.x());
        o.addProperty("y", p.y());
        o.addProperty("z", p.z());
        o.addProperty("distance", p.distance());
        o.addProperty("health", p.health());
        return o;
    }

    /** 最近一件掉落物：**不带坐标**，只给事实（谁丢的 / 掉多久了 / 还有多久消失）。 */
    private static JsonObject itemJson(Pure.ItemNearest i) {
        JsonObject o = new JsonObject();
        o.addProperty("item_id", i.itemId());
        o.addProperty("item_count", i.itemCount());
        o.addProperty("distance", i.distance());
        o.addProperty("owner", i.owner());
        o.addProperty("age", i.age());
        o.addProperty("despawn_in", i.despawnIn());
        return o;
    }

    private static JsonObject blocksJson(Pure.BlockSummary s) {
        JsonObject out = new JsonObject();
        out.addProperty("count", s.count());

        JsonObject byBlock = new JsonObject();
        for (Map.Entry<String, Integer> e : s.byBlock().entrySet()) {
            byBlock.addProperty(e.getKey(), e.getValue());
        }
        out.add("by_block", byBlock);

        if (s.nearest() != null) {
            JsonObject nearest = new JsonObject();
            nearest.addProperty("block_id", s.nearest().blockId());
            nearest.addProperty("distance", s.nearest().distance());
            out.add("nearest", nearest);
        }
        return out;
    }
}
