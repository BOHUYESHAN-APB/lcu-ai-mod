package com.lcu.lcumod.action;

import com.google.gson.JsonArray;
import com.google.gson.JsonObject;
import net.minecraft.client.Minecraft;
import net.minecraft.core.registries.BuiltInRegistries;
import net.minecraft.world.entity.Entity;
import net.minecraft.world.entity.LivingEntity;
import net.minecraft.world.entity.item.ItemEntity;
import net.minecraft.world.entity.player.Player;

import java.util.ArrayList;
import java.util.Comparator;
import java.util.List;
import java.util.Locale;

/**
 * 按需实体扫描（{@code scan_entities}）：把 {@code state_update} 里**默认屏蔽掉的坐标**在
 * **需要的时候**取回来。
 *
 * <p>用户口径："不要把周围生物的坐标动态全都传给他 —— 有些东西**需要的时候再获取坐标**，
 * 不需要的默认当**无关信息屏蔽**掉。" 于是 {@code state_update.entities} 改成了分级摘要
 * （{@link PerceptionView}：只有威胁/玩家/最近一件掉落物带坐标）。摘要删坐标**不丢能力**的前提就是
 * 这条按需回路：模型问"附近有没有牛""这附近有哪些方块"时，才用本命令把那一片的具体坐标取回来。
 *
 * <p><b>只读</b>：只扫、只回报（不移动、不点击、不改任何状态，不碰 {@link Pathfinder} /
 * {@link InputIsolation}）。
 *
 * <p>冻结的接口（参数）：{@code {radius?, kind?, limit?}}
 * <ul>
 *   <li>{@code radius}：默认 {@value #DEFAULT_RADIUS}，上限 {@value #MAX_RADIUS}（超过 ⇒
 *       **如实拒绝** {@code radius_too_large}，绝不偷偷放大）；</li>
 *   <li>{@code kind}：{@link Pure#KIND_FILTERS}（用既有 {@link MobPolicy#tier(String)} 分档；
 *       {@code player}/{@code item} 单独一类；认不出的实体归 {@code unknown}），认不出的档名 ⇒
 *       {@code bad_kind}；</li>
 *   <li>{@code limit}：默认 {@value #DEFAULT_LIMIT}，上限 {@value #MAX_LIMIT}（超过**钳到上限**，
 *       截断后 {@code total} 仍是**真实总数**）。</li>
 * </ul>
 *
 * <p>响应（{@code ok}/{@code reason} 照仓库既有 {@code sendResponse} 形状；不在世界里 ⇒
 * {@code ok:false + not_in_world}，**不编一份空列表冒充成功**）：
 * <pre>
 * {"ok":true,"radius":16,"kind":"passive","total":37,"returned":24,"truncated":true,
 *  "entities":[{"id":33227,"name":"牛","type":"passive","tier":"PASSIVE","distance":4.2,
 *               "x":851,"y":63,"z":-2189,"health":10.0}, …]}
 * </pre>
 *
 * <p>坐标一律是**方块坐标**（{@code Math.round}，与摘要里 threats/players 的口径一致，
 * 不一个用小数一个用整数）；排序按距离升序（同距离按实体 id 升序，稳定）。
 *
 * <p>分两半（与 {@link PerceptionView} / {@link GearReport} 同口径）：{@link Pure} 是**零依赖**的
 * 筛选/排序/截断纯逻辑（自检直接断言它的行为，不加载任何 Minecraft 类）；本类其余部分把
 * {@code Entity} 转成那些行并拼 JSON。
 */
public final class EntityScan {

    /** radius 默认 16（与摘要里"附近"的口径同量级）。 */
    public static final int DEFAULT_RADIUS = 16;
    /** radius 上限 32：超过**如实拒绝**，不偷偷放大。 */
    public static final int MAX_RADIUS = 32;
    /** limit 默认 24。 */
    public static final int DEFAULT_LIMIT = 24;
    /** limit 上限 48：超过**钳到上限**（截断后 total 仍是真实总数）。 */
    public static final int MAX_LIMIT = 48;

    private EntityScan() {
    }

    // ── 零依赖的一半：筛选 + 排序 + 截断（自检直接断言这一段的行为）──────────────

    /**
     * 纯逻辑：不给任何游戏类，只有字符串、布尔与数字。
     *
     * <p>单独一个嵌套类与 {@link PerceptionView.Pure} 同理：自检 JVM 的类路径上没有游戏类，
     * 只有这一半能被加载并直接测；碰游戏的那一半用静态扫描钉住形态。
     */
    public static final class Pure {

        /**
         * {@code kind} 参数的**全部合法取值**（顺序固定）：{@code all} + 七档实体类别
         * （{@link PerceptionView.Pure#KINDS} 那七档，外加 {@code all} = 不筛档）。
         */
        public static final String[] KIND_FILTERS =
                {"all", "dangerous", "hostile", "neutral", "passive", "player", "item", "unknown"};

        /**
         * 一条待扫描的实体行（**零依赖**：没有游戏类，只有原始事实）。
         *
         * <p>玩家 / 掉落物 / 生物共用这一行；{@link #kind()} 的归类口径与
         * {@link PerceptionView.Pure.Row#kind()} **完全一致**（player/item 优先，其余由 tier 名推，
         * 认不出 ⇒ {@code unknown}）。**档位判断本身不在这里**：那来自 {@link MobPolicy#tier(String)}
         * 这张唯一的表（游戏侧填 {@code tier} 字段）。
         */
        public record Row(int id, String name, String tier, boolean player, boolean item,
                          double x, double y, double z, double distance,
                          boolean living, double health,
                          String itemId, int itemCount, String owner, int age, int despawnIn) {

            /** 归到 {@code by_kind} 的哪一档（player/item 优先，其余由 tier 名推；认不出 ⇒ unknown）。 */
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

        /** 扫描结果：截断后的行 + **截断前的真实总数** + 是否被 limit 截掉过。 */
        public record Result(List<Row> rows, int total, boolean truncated) {
        }

        private Pure() {
        }

        /** 一位小数（距离口径与既有摘要一致）。 */
        static double round1(double v) {
            return Math.round(v * 10.0) / 10.0;
        }

        /**
         * {@code kind} 规范化：缺省/空白 ⇒ {@code all}；认不出的档名 ⇒ **null**
         * （handler 据此如实报 {@code bad_kind}，不猜一个档出来）。
         */
        public static String canonicalKind(String raw) {
            if (raw == null || raw.isBlank()) {
                return "all";
            }
            String k = raw.trim().toLowerCase(Locale.ROOT);
            for (String allowed : KIND_FILTERS) {
                if (allowed.equals(k)) {
                    return k;
                }
            }
            return null;
        }

        /**
         * {@code radius} 校验：{@code <1 ⇒ bad_radius}；{@code >}{@link #MAX_RADIUS}
         * ⇒ {@code radius_too_large}（**如实拒绝，绝不偷偷放大**）；其余 ⇒ null（通过）。
         */
        public static String radiusErrorCode(int radius) {
            if (radius < 1) {
                return "bad_radius";
            }
            return radius > MAX_RADIUS ? "radius_too_large" : null;
        }

        /**
         * {@code limit} 的**生效值**：超过上限一律**钳到** {@link #MAX_LIMIT}（截断后
         * {@code total} 仍是真实总数）；{@code <1} 的入参由 handler 先用 {@code bad_limit} 如实拒掉，
         * 这里兜底成 1，绝不返回 0 个。
         */
        public static int clampLimit(int limit) {
            return Math.min(Math.max(limit, 1), MAX_LIMIT);
        }

        /**
         * 这一行符不符合 {@code kind}（{@code null}/空白 ⇒ all）。
         *
         * <p>{@code all} 不筛档；{@code player}/{@code item} 是**独立类别**；其余按 {@link Row#kind()}
         * 精确相等（所以 {@code hostile} 只含 HOSTILE —— {@code dangerous} 是**单独一档**，不算 hostile）。
         */
        public static boolean matches(Row row, String kind) {
            if (row == null) {
                return false;
            }
            String k = kind == null || kind.isBlank() ? "all" : kind.trim().toLowerCase(Locale.ROOT);
            return switch (k) {
                case "all" -> true;
                case "player" -> row.player();
                case "item" -> row.item();
                default -> !row.player() && !row.item() && row.kind().equals(k);
            };
        }

        /**
         * 筛选（kind + radius）→ 距离升序（同距离按 id 升序，稳定）→ 按 limit 截断。
         *
         * <p>{@code total} 是**截断前**符合条件的真实总数；{@code truncated} = 是否被 limit 截断。
         * 半径**边界包含**（距离刚好等于 radius 算在内）。
         */
        public static Result scan(List<Row> rows, String kind, double radius, int limit) {
            List<Row> matched = new ArrayList<>();
            if (rows != null) {
                for (Row row : rows) {
                    if (matches(row, kind) && row.distance() <= radius) {
                        matched.add(row);
                    }
                }
            }
            matched.sort(Comparator.comparingDouble(Row::distance).thenComparingInt(Row::id));

            int total = matched.size();
            int keep = Math.min(clampLimit(limit), total);
            return new Result(List.copyOf(matched.subList(0, keep)), total, total > keep);
        }
    }

    // ── Minecraft 侧：把实体转成行并拼 JSON ─────────────────────────────────

    /**
     * 扫玩家周围 {@code radius} 格内的实体，转成零依赖的行。
     *
     * <p>距离用 {@code player.distanceTo(entity)}（3D，与摘要里的 distance 同口径）；包围盒只是
     * **粗筛**，精确边界由这里与 {@link Pure#scan} 的 {@code distance <= radius} 裁。
     * 档位走**唯一那张表** {@link MobPolicy#tier(String)}（与 {@code StateCollector} / 摘要口径一致），
     * 这里不另写一份判断。
     */
    public static List<Pure.Row> collect(Minecraft mc, int radius) {
        List<Pure.Row> rows = new ArrayList<>();
        Player self = mc == null ? null : mc.player;
        if (self == null || mc.level == null) {
            return rows;
        }
        for (Entity entity : mc.level.getEntities(self, self.getBoundingBox().inflate(radius))) {
            if (entity == self) {
                continue;
            }
            double distance = self.distanceTo(entity);
            if (distance > radius) {
                continue;
            }
            if (entity instanceof Player other) {
                rows.add(new Pure.Row(other.getId(), other.getName().getString(), "", true, false,
                        other.getX(), other.getY(), other.getZ(), distance,
                        true, other.getHealth(), "", 0, "", 0, 0));
            } else if (entity instanceof ItemEntity item) {
                var stack = item.getItem();
                Entity owner = item.getOwner();
                int age = item.getAge();
                // 掉落物的事实与摘要同口径：谁丢的（方块/发射器掉的 ⇒ 空）/ 掉了多久 / 还有多久消失。
                rows.add(new Pure.Row(item.getId(), item.getName().getString(), "", false, true,
                        item.getX(), item.getY(), item.getZ(), distance, false, 0.0,
                        BuiltInRegistries.ITEM.getKey(stack.getItem()).toString(), stack.getCount(),
                        owner == null ? "" : owner.getName().getString(), age,
                        Math.max(0, item.lifespan - age)));
            } else {
                MobPolicy.Tier tier = MobPolicy.tier(AttackRules.entityTypeId(entity));
                boolean living = entity instanceof LivingEntity;
                rows.add(new Pure.Row(entity.getId(), entity.getName().getString(), tier.name(),
                        false, false, entity.getX(), entity.getY(), entity.getZ(), distance,
                        living, living ? ((LivingEntity) entity).getHealth() : 0.0,
                        "", 0, "", 0, 0));
            }
        }
        return rows;
    }

    /**
     * 扫描 + 筛选 + 截断 + 拼 JSON（{@code scan_entities} 的 data 载荷）。
     *
     * <p>{@code radius}/{@code kind}/{@code limit} 由 handler 校验与钳制后再进来（{@code kind} 已是
     * {@link Pure#canonicalKind} 的规范值）。
     */
    public static JsonObject scan(Minecraft mc, int radius, String kind, int limit) {
        Pure.Result result = Pure.scan(collect(mc, radius), kind, radius, limit);

        JsonObject data = new JsonObject();
        data.addProperty("ok", true);
        data.addProperty("radius", radius);
        data.addProperty("kind", kind);
        data.addProperty("total", result.total());
        data.addProperty("returned", result.rows().size());
        data.addProperty("truncated", result.truncated());
        JsonArray entities = new JsonArray();
        for (Pure.Row row : result.rows()) {
            entities.add(rowJson(row));
        }
        data.add("entities", entities);
        return data;
    }

    /** 一条实体行 → JSON（方块坐标取整；掉落物带物品事实；生物/玩家带血量）。 */
    private static JsonObject rowJson(Pure.Row row) {
        JsonObject o = new JsonObject();
        o.addProperty("id", row.id());
        o.addProperty("name", row.name());
        o.addProperty("type", row.kind());
        if (!row.player() && !row.item()) {
            // 档位名（与摘要 threats 里那条 tier 同口径、同一个枚举来源）。
            o.addProperty("tier", row.tier());
        }
        o.addProperty("distance", Pure.round1(row.distance()));
        o.addProperty("x", (int) Math.round(row.x()));
        o.addProperty("y", (int) Math.round(row.y()));
        o.addProperty("z", (int) Math.round(row.z()));
        if (row.player() || (row.living() && !row.item())) {
            o.addProperty("health", row.health());
        }
        if (row.item()) {
            o.addProperty("item_id", row.itemId());
            o.addProperty("item_count", row.itemCount());
            o.addProperty("owner", row.owner());
            o.addProperty("age", row.age());
            o.addProperty("despawn_in", row.despawnIn());
        }
        return o;
    }
}
