package com.lcu.lcumod.action;

import java.util.ArrayList;
import java.util.Comparator;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;

/**
 * 区域（zone）声明与**硬保护**。
 *
 * <p>用户口径：「在农场里他想造东西，他**无法识别这里是不是农场**」→ 所以范围要能
 * **声明语义**（{@code kind}：farm / animal_pen / base / …）并**硬性保护**（不能靠提示词）。
 *
 * <p>不变的判定原则：
 * <ul>
 *   <li><b>保护优先于允许</b>：命中保护规则就是拒绝，没有"更允许"的例外；</li>
 *   <li><b>多区重叠取最保守</b>：任一命中区域声明了适用于该动作的保护规则 → 拦；报哪一条按
 *       {@link #RULE_PRIORITY} 的保守序（越靠前越保守）确定，同序时按区域名排序，结果稳定；</li>
 *   <li><b>区域内但没有保护项 → 不拦</b>：区域不是"禁止进入"，只是"这几类动作不许"；</li>
 *   <li>{@code containers} 不在这里拦（容器的取用策略另有出口），但保留在模型与状态里。</li>
 * </ul>
 *
 * <p>形状：{@code radius} = 以 {@code (x,y,z)} 为球心的**球**（三维欧氏距离 ≤ radius）；
 * {@code box} = 轴对齐长方体（端点含）。两者都给时按 {@code box} 判定（更明确）。
 *
 * <p>本类零依赖（不碰 Minecraft / gson），可被 {@code verifyGuard} 直接自检。
 */
public final class ZoneGuard {

    /** 会被硬拦的动作类别。 */
    public enum Action { ATTACK, MINE, PLACE }

    /** 允许出现在 {@code protect} 里的值（顺序 = 保守序）。 */
    public static final List<String> RULE_PRIORITY = List.of("animals", "crops", "blocks", "containers");

    /** 轴对齐长方体（端点含）。 */
    public record Box(double minX, double minY, double minZ, double maxX, double maxY, double maxZ) {

        public boolean contains(double x, double y, double z) {
            return x >= minX && x <= maxX && y >= minY && y <= maxY && z >= minZ && z <= maxZ;
        }

        public boolean valid() {
            return minX <= maxX && minY <= maxY && minZ <= maxZ;
        }
    }

    /** 一个区域声明。{@code protect} 已按 {@link #RULE_PRIORITY} 归一排序。 */
    public record Zone(String name, String kind, List<String> protect,
                       double x, double y, double z, Double radius, Box box) {

        public Zone {
            protect = List.copyOf(protect);
        }

        /** 点是否落在区域内（球或长方体）。 */
        public boolean contains(double px, double py, double pz) {
            if (box != null) {
                return box.contains(px, py, pz);
            }
            if (radius == null) {
                return false;
            }
            double dx = px - x;
            double dy = py - y;
            double dz = pz - z;
            return dx * dx + dy * dy + dz * dz <= radius * radius;
        }

        public String shape() {
            return box != null ? "box" : "radius";
        }
    }

    /** 拦住的结果：哪个区域、哪条规则（后端据此告诉 LLM"这里是农场，不能动"）。 */
    public record Block(String zone, String rule) {
    }

    /** 定义结果：成功带 {@code zone}，失败带稳定原因码。 */
    public record DefineResult(Zone zone, String reason) {
        public boolean ok() {
            return zone != null;
        }
    }

    private static final Map<String, Zone> ZONES = new LinkedHashMap<>();

    private ZoneGuard() {
    }

    /**
     * 定义/覆盖一个区域（同名覆盖）。
     *
     * <p>失败原因码：{@code need_name} / {@code need_shape} / {@code bad_radius} / {@code bad_box} /
     * {@code bad_protect:<值>}。
     */
    public static DefineResult define(String name, String kind, double x, double y, double z,
                                      Double radius, Box box, List<String> protect) {
        String clean = name == null ? "" : name.strip();
        if (clean.isEmpty()) {
            return new DefineResult(null, "need_name");
        }
        if (radius == null && box == null) {
            return new DefineResult(null, "need_shape");
        }
        if (radius != null && radius <= 0) {
            return new DefineResult(null, "bad_radius");
        }
        if (box != null && !box.valid()) {
            return new DefineResult(null, "bad_box");
        }
        List<String> rules = new ArrayList<>();
        if (protect != null) {
            for (String raw : protect) {
                if (raw == null) {
                    continue;
                }
                String v = raw.strip().toLowerCase(Locale.ROOT);
                if (v.isEmpty()) {
                    continue;
                }
                if (!RULE_PRIORITY.contains(v)) {
                    return new DefineResult(null, "bad_protect:" + v);
                }
                if (!rules.contains(v)) {
                    rules.add(v);
                }
            }
        }
        rules.sort(Comparator.comparingInt(RULE_PRIORITY::indexOf));
        Zone zone = new Zone(clean,
                kind == null ? "" : kind.strip().toLowerCase(Locale.ROOT),
                rules, x, y, z, radius, box);
        ZONES.put(clean, zone);
        return new DefineResult(zone, "");
    }

    public static Zone get(String name) {
        return name == null ? null : ZONES.get(name.strip());
    }

    public static boolean clear(String name) {
        return name != null && ZONES.remove(name.strip()) != null;
    }

    /** 所有区域（按名字排序，保证判定与状态输出稳定）。 */
    public static List<Zone> all() {
        List<Zone> out = new ArrayList<>(ZONES.values());
        out.sort(Comparator.comparing(Zone::name));
        return out;
    }

    public static void clearAll() {
        ZONES.clear();
    }

    public static int size() {
        return ZONES.size();
    }

    /** 该规则是否适用于该动作（{@code animals} 只拦"打动物"，交互/喂食不拦）。 */
    public static boolean applies(String rule, Action action, boolean targetIsAnimal) {
        if (rule == null || action == null) {
            return false;
        }
        return switch (action) {
            case ATTACK -> "animals".equals(rule) && targetIsAnimal;
            case MINE, PLACE -> "crops".equals(rule) || "blocks".equals(rule);
        };
    }

    /**
     * 硬拦判定：点 {@code (px,py,pz)} 处做 {@code action} 是否被某个区域拦住。
     *
     * @return {@code null} = 不拦（含"区域内但没有该保护项"）
     */
    public static Block block(double px, double py, double pz, Action action, boolean targetIsAnimal) {
        Block best = null;
        int bestPri = Integer.MAX_VALUE;
        for (Zone zone : all()) {
            if (!zone.contains(px, py, pz)) {
                continue;
            }
            for (String rule : zone.protect()) {
                if (!applies(rule, action, targetIsAnimal)) {
                    continue;
                }
                int pri = RULE_PRIORITY.indexOf(rule);
                if (best == null || pri < bestPri) {
                    best = new Block(zone.name(), rule);
                    bestPri = pri;
                }
                break; // 同一区域内 protect 已按保守序排好，取第一条即可
            }
        }
        return best;
    }

    /** 状态输出（纯字段表；{@code null} 值表示该字段不适用）。 */
    public static List<Map<String, Object>> toFields() {
        List<Map<String, Object>> out = new ArrayList<>();
        for (Zone z : all()) {
            Map<String, Object> m = new LinkedHashMap<>();
            m.put("name", z.name());
            m.put("kind", z.kind());
            m.put("protect", z.protect());
            Map<String, Object> center = new LinkedHashMap<>();
            center.put("x", z.x());
            center.put("y", z.y());
            center.put("z", z.z());
            m.put("center", center);
            m.put("radius", z.radius());
            m.put("shape", z.shape());
            if (z.box() != null) {
                Box b = z.box();
                Map<String, Object> boxFields = new LinkedHashMap<>();
                boxFields.put("min_x", b.minX());
                boxFields.put("min_y", b.minY());
                boxFields.put("min_z", b.minZ());
                boxFields.put("max_x", b.maxX());
                boxFields.put("max_y", b.maxY());
                boxFields.put("max_z", b.maxZ());
                m.put("box", boxFields);
            } else {
                m.put("box", null);
            }
            out.add(m);
        }
        return out;
    }
}
