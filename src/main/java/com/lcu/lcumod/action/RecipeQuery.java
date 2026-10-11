package com.lcu.lcumod.action;

import java.util.ArrayList;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Locale;

/**
 * 配方/物品查询的**纯逻辑**（零依赖自检 {@code RecipeCheck} 直接测这一层）。
 *
 * <p>与 {@link ContainerPlan} 同样的思路：{@code verify*} 自检的 classpath 只有
 * {@code main.output + test.output}，没有 Minecraft。所以凡是要被自检覆盖的判定
 * （物品身份对齐 / 歧义不猜 / 未知名候选 / 工作站归一 / 配方过滤与截断）都放在这里，
 * 方法体与签名里不得出现 Minecraft 类型。
 *
 * <p>真实注册表（物品 id / 中英文名 / tag / 服务端配方显示）由 {@link RecipeTools} 适配成
 * 本类的 {@link ItemEntry} / {@link TagEntry} / {@link RecipeRow} 后传入。
 *
 * <h2>用户最大的问题："合成表到底传 id 还是名字，怎么对齐？"</h2>
 * 答案：**两者都收，但收进来立刻对齐成 {@code minecraft:id}**。规则见 {@link #resolveItem}：
 * <ol>
 *   <li><b>id 精确匹配优先</b>（补全命名空间、忽略大小写）；</li>
 *   <li>否则按名字匹配，<b>中/英文与本地化名一起看</b>；</li>
 *   <li>同样高分命中多个 → <b>歧义不猜</b>，返回候选列表 + {@code ambiguous}；</li>
 *   <li>完全不匹配 → {@code unknown}，并带上最接近的候选（供大模型改口径）。</li>
 * </ol>
 * 上层（wire 命令 / AI）只要拿到 {@code match.item} 这一个稳定 id，就与名字无关了。
 */
public final class RecipeQuery {

    private RecipeQuery() {
    }

    // ── 原因码（机器可读，直接进 wire 回执的 reason 字段）──────────────

    public static final String OK = "ok";
    /** 命中真实注册表 id。 */
    public static final String EXACT_ID = "exact_id";
    /** 命中名字（中/英/本地化）。 */
    public static final String NAME = "name";
    /** 命中 tag（{@code #namespace:path}）。 */
    public static final String TAG = "tag";
    /** 名字歧义：多个同分候选，拒绝猜。 */
    public static final String AMBIGUOUS = "ambiguous";
    /** 查无此物（附最接近候选）。 */
    public static final String UNKNOWN = "unknown";
    /** 工作站不在原版支持范围（模组机器 / 无配方显示的工作站）。 */
    public static final String ERR_UNSUPPORTED_STATION = "unsupported_station";
    /** 过滤条件合法但一条都没搜到。 */
    public static final String ERR_NO_RECIPES = "no_recipes";
    /** 非集成服务端拿不到配方表。 */
    public static final String ERR_RECIPE_TABLE_UNAVAILABLE = "recipe_table_unavailable";
    public static final String ERR_AMBIGUOUS_OUTPUT = "ambiguous_output";
    public static final String ERR_UNKNOWN_OUTPUT = "unknown_output";
    public static final String ERR_AMBIGUOUS_INPUT = "ambiguous_input";
    public static final String ERR_UNKNOWN_INPUT = "unknown_input";
    /** 想拿 tag 去合成：tag 不是单个产物，拒结。 */
    public static final String ERR_TAG_NOT_CRAFTABLE = "tag_not_craftable";

    // ── 评分常量 ──────────────────────────────────────────────────

    /** 真实注册表 id 精确命中。 */
    public static final int SCORE_EXACT_ID = 100;
    /** 名字精确命中（中/英/本地化任一）。 */
    public static final int SCORE_NAME = 90;
    /** 候选列表上限（够大模型挑，不刷屏）。 */
    public static final int MAX_CANDIDATES = 12;
    /** 低于此分不算"接近候选"。 */
    public static final int FUZZY_MIN = 30;
    /** search_recipes 的默认条数上限。 */
    public static final int DEFAULT_LIMIT = 50;

    // ── 工作站（原版范围）─────────────────────────────────────────

    public static final String S_CRAFTING = "crafting";
    public static final String S_FURNACE = "furnace";
    public static final String S_BLAST_FURNACE = "blast_furnace";
    public static final String S_SMOKER = "smoker";
    public static final String S_STONECUTTER = "stonecutter";
    public static final String S_SMITHING = "smithing";
    public static final String S_BREWING = "brewing";

    // ── 数据结构 ──────────────────────────────────────────────────

    /** 真实注册表里的一个物品：id + 路径 + 可匹配的名字（中/英/本地化/path）。 */
    public record ItemEntry(String id, String path, List<String> names) {
    }

    /** 物品 tag：{@code #minecraft:planks} + 成员 id 列表。 */
    public record TagEntry(String id, List<String> memberIds) {
    }

    /** 对齐候选（{@code score} 越大越像；{@code reason} = exact_id/name/fuzzy/tag/tag_member）。 */
    public record Candidate(String item, String name, int score, String reason) {
    }

    /** 对齐结果（{@code tag=true} 时 item 形如 {@code #minecraft:planks}）。 */
    public record Match(String item, String name, int count, boolean tag) {
    }

    /** {@link #resolveItem} 的完整结果。 */
    public record Resolution(boolean ok, String reason, Match match, List<Candidate> candidates) {
    }

    /** craft_via_recipe 的目标对齐结果：唯一 id 才放行，否则拒结并回候选。 */
    public record CraftTarget(boolean ok, String itemId, String reason, List<Candidate> candidates) {
    }

    /** 配方里的一格输入/产物：一律 id 字符串（tag 用 {@code #ns:path}）。 */
    public record Stack(String item, int count) {
    }

    /**
     * 一条配方显示（来自服务端 RecipeManager 的 RecipeDisplay）。
     *
     * @param displayId 服务端分配的 {@code RecipeDisplayId#index()}（craft_via_recipe 的 display_id）
     * @param station   归一后的工作站名；不认识的原版类型保留原始 id（{@code supported=false}）
     * @param supported 该工作站是否在原版支持范围内
     */
    public record RecipeRow(String displayId, String station, boolean supported,
                            List<Stack> inputs, Stack output, String recipeId) {
    }

    /** {@link #search} 的结果。 */
    public record SearchResult(boolean ok, String reason, List<RecipeRow> rows, int total,
                               boolean truncated, List<Candidate> candidates) {
    }

    // ── 归一 ──────────────────────────────────────────────────────

    /** {@code planks} → {@code minecraft:planks}；带命名空间原样保留；空白返回空串。 */
    public static String normalizeId(String raw) {
        if (raw == null) {
            return "";
        }
        String s = raw.trim().toLowerCase(Locale.ROOT);
        if (s.isEmpty()) {
            return "";
        }
        return s.indexOf(':') >= 0 ? s : "minecraft:" + s;
    }

    /** id 的路径部分（去命名空间）；无冒号则原样。 */
    public static String pathOf(String id) {
        if (id == null) {
            return "";
        }
        int i = id.indexOf(':');
        return i >= 0 ? id.substring(i + 1) : id;
    }

    /**
     * 工作站别名归一：中文/英文/带命名空间/口语写法都收。
     *
     * @return 原版七个工作站之一，或 {@code null}（不认识 = 模组工作站/机器，如实不支持）
     */
    public static String canonicalStation(String raw) {
        if (raw == null) {
            return null;
        }
        String s = raw.trim().toLowerCase(Locale.ROOT);
        if (s.isEmpty()) {
            return null;
        }
        // 去掉命名空间前缀（minecraft: 或模组的 namespace:）后再看路径
        String path = pathOf(s);
        return switch (path) {
            case "crafting", "craft", "crafting_table", "workbench", "工作台" -> S_CRAFTING;
            case "furnace", "smelting", "smelt", "熔炉" -> S_FURNACE;
            case "blast_furnace", "blasting", "blast", "高炉" -> S_BLAST_FURNACE;
            case "smoker", "smoking", "smoke", "烟熏炉" -> S_SMOKER;
            case "stonecutter", "stonecutting", "切石机" -> S_STONECUTTER;
            case "smithing", "smithing_table", "锻造台" -> S_SMITHING;
            case "brewing", "brewing_stand", "酿造台" -> S_BREWING;
            default -> null;
        };
    }

    /** 该工作站是否有可搜索的配方显示。酿造台在 26.3 没有 RecipeDisplay（见 {@link RecipeRow}）。 */
    public static boolean stationHasDisplays(String canonical) {
        if (canonical == null) {
            return false;
        }
        return !S_BREWING.equals(canonical);
    }

    /** 是否为原版支持范围内的工作站。 */
    public static boolean isSupportedStation(String canonical) {
        return canonical != null && stationHasDisplays(canonical);
    }

    // ── 物品身份对齐 ──────────────────────────────────────────────

    /**
     * 把 query 对齐到真实注册表：{@code minecraft:id} / 中文名 / 英文名 / {@code #tag}。
     *
     * <p>规则：id 精确优先 → 名字（中/英/本地化）→ 歧义不猜（{@code ambiguous} + 候选）
     * → 未知名带最接近候选（{@code unknown} + 候选）。
     */
    public static Resolution resolveItem(String query, List<ItemEntry> items, List<TagEntry> tags) {
        List<ItemEntry> safeItems = items == null ? List.of() : items;
        List<TagEntry> safeTags = tags == null ? List.of() : tags;

        String raw = query == null ? "" : query.trim();
        if (raw.isEmpty()) {
            return new Resolution(false, UNKNOWN, null, List.of());
        }

        if (raw.charAt(0) == '#') {
            return resolveTag(raw, safeItems, safeTags);
        }

        String normalized = normalizeId(raw);
        ItemEntry exact = findItem(safeItems, normalized);
        if (exact != null) {
            List<Candidate> candidates = new ArrayList<>();
            candidates.add(new Candidate(exact.id(), displayName(exact), SCORE_EXACT_ID, EXACT_ID));
            appendNearest(candidates, raw, safeItems, safeTags);
            return new Resolution(true, EXACT_ID, new Match(exact.id(), displayName(exact), 0, false), candidates);
        }

        List<Candidate> scored = nearestItems(raw, safeItems);
        int top = scored.isEmpty() ? -1 : scored.get(0).score();

        if (top >= SCORE_NAME) {
            List<Candidate> ties = new ArrayList<>();
            for (Candidate c : scored) {
                if (c.score() == top) {
                    ties.add(c);
                }
            }
            if (ties.size() == 1) {
                Candidate winner = ties.get(0);
                List<Candidate> candidates = new ArrayList<>(ties);
                for (Candidate c : scored) {
                    if (c.score() < top && candidates.size() < MAX_CANDIDATES) {
                        candidates.add(c);
                    }
                }
                appendTagSuggestions(candidates, raw, safeTags);
                return new Resolution(true, NAME,
                        new Match(winner.item(), winner.name(), 0, false), candidates);
            }
            // 同分多命中：歧义不猜
            List<Candidate> candidates = new ArrayList<>(ties);
            appendTagSuggestions(candidates, raw, safeTags);
            return new Resolution(false, AMBIGUOUS, null, candidates);
        }

        // 未知名：带上最接近的候选
        List<Candidate> candidates = new ArrayList<>();
        for (Candidate c : scored) {
            if (c.score() < FUZZY_MIN || candidates.size() >= MAX_CANDIDATES) {
                break;
            }
            candidates.add(c);
        }
        appendTagSuggestions(candidates, raw, safeTags);
        return new Resolution(false, UNKNOWN, null, candidates);
    }

    /** craft 目标对齐：tag 不可合成、歧义/未知一律拒结。 */
    public static CraftTarget craftTarget(String query, List<ItemEntry> items, List<TagEntry> tags) {
        Resolution r = resolveItem(query, items, tags);
        if (r.ok() && r.match() != null) {
            if (r.match().tag()) {
                return new CraftTarget(false, null, ERR_TAG_NOT_CRAFTABLE, r.candidates());
            }
            return new CraftTarget(true, r.match().item(), OK, List.of());
        }
        return new CraftTarget(false, null, r.reason(), r.candidates());
    }

    private static Resolution resolveTag(String raw, List<ItemEntry> items, List<TagEntry> tags) {
        String wanted = normalizeId(raw.substring(1));
        for (TagEntry t : tags) {
            if (t.id() != null && normalTagId(t.id()).equals(wanted)) {
                List<Candidate> candidates = new ArrayList<>();
                for (String member : t.memberIds()) {
                    if (candidates.size() >= MAX_CANDIDATES) {
                        break;
                    }
                    candidates.add(new Candidate(member, nameOf(items, member), SCORE_EXACT_ID, "tag_member"));
                }
                Match match = new Match(t.id(), t.id(), t.memberIds().size(), true);
                return new Resolution(true, TAG, match, candidates);
            }
        }
        // 未知 tag：拿 tag 主体去物品里找最接近的
        String body = raw.substring(1);
        List<Candidate> candidates = new ArrayList<>(nearestItems(body, items));
        if (candidates.size() > MAX_CANDIDATES) {
            candidates = new ArrayList<>(candidates.subList(0, MAX_CANDIDATES));
        }
        appendTagSuggestions(candidates, body, tags);
        return new Resolution(false, UNKNOWN, null, candidates);
    }

    private static String normalTagId(String tagId) {
        return normalizeId(tagId == null ? "" : tagId.replace("#", ""));
    }

    private static ItemEntry findItem(List<ItemEntry> items, String normalizedId) {
        if (normalizedId.isEmpty()) {
            return null;
        }
        for (ItemEntry it : items) {
            if (it.id() != null && it.id().equalsIgnoreCase(normalizedId)) {
                return it;
            }
        }
        return null;
    }

    private static String displayName(ItemEntry entry) {
        if (entry == null) {
            return "";
        }
        if (entry.names() != null) {
            for (String n : entry.names()) {
                if (n != null && !n.isBlank()) {
                    return n;
                }
            }
        }
        return entry.id() == null ? "" : entry.id();
    }

    private static String nameOf(List<ItemEntry> items, String id) {
        ItemEntry e = findItem(items, normalizeId(id));
        return e == null ? (id == null ? "" : id) : displayName(e);
    }

    /** 名字打分的候选（按分数降序、分数相同按 id 升序，保证确定性）。 */
    private static List<Candidate> nearestItems(String query, List<ItemEntry> items) {
        List<Candidate> out = new ArrayList<>();
        for (ItemEntry it : items) {
            int best = 0;
            if (it.names() != null) {
                for (String n : it.names()) {
                    best = Math.max(best, nameScore(query, n));
                }
            }
            if (it.path() != null) {
                best = Math.max(best, nameScore(query, it.path()));
            }
            if (best > 0) {
                out.add(new Candidate(it.id(), displayName(it), best, best >= SCORE_NAME ? NAME : "fuzzy"));
            }
        }
        out.sort((a, b) -> a.score() != b.score()
                ? Integer.compare(b.score(), a.score())
                : String.valueOf(a.item()).compareTo(String.valueOf(b.item())));
        return out;
    }

    private static void appendNearest(List<Candidate> out, String query, List<ItemEntry> items,
                                      List<TagEntry> tags) {
        for (Candidate c : nearestItems(query, items)) {
            if (out.size() >= MAX_CANDIDATES) {
                return;
            }
            boolean dup = out.stream().anyMatch(x -> x.item() != null && x.item().equalsIgnoreCase(c.item()));
            if (!dup) {
                out.add(c);
            }
        }
        appendTagSuggestions(out, query, tags);
    }

    private static void appendTagSuggestions(List<Candidate> out, String query, List<TagEntry> tags) {
        for (TagEntry t : tags) {
            if (out.size() >= MAX_CANDIDATES) {
                return;
            }
            int score = tagScore(query, t.id());
            if (score < FUZZY_MIN) {
                continue;
            }
            boolean dup = out.stream().anyMatch(x -> x.item() != null && x.item().equalsIgnoreCase(t.id()));
            if (!dup) {
                out.add(new Candidate(t.id(), t.id(), score, "tag"));
            }
        }
    }

    private static int tagScore(String query, String tagId) {
        if (tagId == null) {
            return 0;
        }
        String a = normText(query);
        String b = normText(pathOf(normalTagId(tagId)));
        if (a.isEmpty() || b.isEmpty()) {
            return 0;
        }
        if (a.equals(b)) {
            return 95;
        }
        String a2 = a.replace('_', ' ');
        String b2 = b.replace('_', ' ');
        if (a2.equals(b2)) {
            return 94;
        }
        return Math.min(similarity(a2, b2), 88);
    }

    /** 0..100：完全相等 100；否则 Levenshtein 相似 + 前后缀/包含加成（不区分大小写）。 */
    public static int similarity(String a, String b) {
        String x = normText(a);
        String y = normText(b);
        if (x.isEmpty() || y.isEmpty()) {
            return 0;
        }
        if (x.equals(y)) {
            return 100;
        }
        int max = Math.max(x.length(), y.length());
        int dist = levenshtein(x, y);
        int score = (int) Math.round(100.0 * (max - dist) / max);
        if (x.startsWith(y) || y.startsWith(x)) {
            score = Math.max(score, 70);
        }
        if (x.contains(y) || y.contains(x)) {
            score = Math.max(score, 60);
        }
        return clamp(score, 0, 99);
    }

    /** 单个名字与 query 的匹配分：相等 90；下划线/空格等价 89；其余 ≤88。 */
    public static int nameScore(String query, String name) {
        String a = normText(query);
        String b = normText(name);
        if (a.isEmpty() || b.isEmpty()) {
            return 0;
        }
        if (a.equals(b)) {
            return SCORE_NAME;
        }
        if (a.replace('_', ' ').equals(b.replace('_', ' '))) {
            return SCORE_NAME - 1;
        }
        return Math.min(similarity(a, b), SCORE_NAME - 2);
    }

    private static String normText(String s) {
        if (s == null) {
            return "";
        }
        return s.trim().toLowerCase(Locale.ROOT).replaceAll("\\s+", " ");
    }

    private static int levenshtein(String a, String b) {
        int[] prev = new int[b.length() + 1];
        int[] cur = new int[b.length() + 1];
        for (int j = 0; j <= b.length(); j++) {
            prev[j] = j;
        }
        for (int i = 1; i <= a.length(); i++) {
            cur[0] = i;
            for (int j = 1; j <= b.length(); j++) {
                int cost = a.charAt(i - 1) == b.charAt(j - 1) ? 0 : 1;
                cur[j] = Math.min(Math.min(cur[j - 1] + 1, prev[j] + 1), prev[j - 1] + cost);
            }
            int[] swap = prev;
            prev = cur;
            cur = swap;
        }
        return prev[b.length()];
    }

    private static int clamp(int v, int lo, int hi) {
        return Math.max(lo, Math.min(hi, v));
    }

    // ── 配方搜索（纯过滤）──────────────────────────────────────────

    /**
     * 在配方显示行上做过滤：output? / input? / station? / limit?。
     *
     * <p>output/input 既收 id 也收名字/tag，先经 {@link #resolveItem} 对齐；对齐歧义/未知直接拒结
     * （reason = ambiguous_output / unknown_output / …）。搜不到返回空数组 + {@code no_recipes}。
     */
    public static SearchResult search(List<RecipeRow> rows, String outputQuery, String inputQuery,
                                      String stationQuery, int limit, List<ItemEntry> items,
                                      List<TagEntry> tags) {
        List<RecipeRow> all = rows == null ? List.of() : rows;

        String station = null;
        if (stationQuery != null && !stationQuery.isBlank()) {
            station = canonicalStation(stationQuery);
            if (!isSupportedStation(station)) {
                return new SearchResult(false, ERR_UNSUPPORTED_STATION, List.of(), 0, false, List.of());
            }
        }

        String outputId = null;
        boolean outputIsTag = false;
        if (outputQuery != null && !outputQuery.isBlank()) {
            Resolution r = resolveItem(outputQuery, items, tags);
            if (!r.ok() || r.match() == null) {
                String reason = AMBIGUOUS.equals(r.reason()) ? ERR_AMBIGUOUS_OUTPUT : ERR_UNKNOWN_OUTPUT;
                return new SearchResult(false, reason, List.of(), 0, false, r.candidates());
            }
            outputId = r.match().item();
            outputIsTag = r.match().tag();
        }

        String inputId = null;
        boolean inputIsTag = false;
        if (inputQuery != null && !inputQuery.isBlank()) {
            Resolution r = resolveItem(inputQuery, items, tags);
            if (!r.ok() || r.match() == null) {
                String reason = AMBIGUOUS.equals(r.reason()) ? ERR_AMBIGUOUS_INPUT : ERR_UNKNOWN_INPUT;
                return new SearchResult(false, reason, List.of(), 0, false, r.candidates());
            }
            inputId = r.match().item();
            inputIsTag = r.match().tag();
        }

        List<RecipeRow> matched = new ArrayList<>();
        for (RecipeRow row : all) {
            if (station != null && !station.equals(rowStationKey(row))) {
                continue;
            }
            if (outputId != null && !stackMatches(row.output(), outputId, outputIsTag, tags)) {
                continue;
            }
            if (inputId != null && !anyInputMatches(row.inputs(), inputId, inputIsTag, tags)) {
                continue;
            }
            matched.add(row);
        }

        int total = matched.size();
        int cap = limit <= 0 ? DEFAULT_LIMIT : limit;
        List<RecipeRow> page = total <= cap ? matched : new ArrayList<>(matched.subList(0, cap));
        boolean truncated = total > page.size();
        String reason = total == 0 ? ERR_NO_RECIPES : OK;
        return new SearchResult(true, reason, page, total, truncated, List.of());
    }

    private static String rowStationKey(RecipeRow row) {
        String canon = canonicalStation(row.station());
        return canon != null ? canon : (row.station() == null ? "" : row.station());
    }

    private static boolean stackMatches(Stack stack, String id, boolean filterIsTag, List<TagEntry> tags) {
        if (stack == null || stack.item() == null || id == null) {
            return false;
        }
        String item = stack.item();
        boolean stackIsTag = item.startsWith("#");
        if (filterIsTag) {
            // 过滤的是 tag：#同名 tag 直接命中，或该物品属于这个 tag
            if (item.equalsIgnoreCase(id)) {
                return true;
            }
            return tagMembers(id, tags).contains(item.toLowerCase(Locale.ROOT));
        }
        // 过滤的是具体物品：配方那格是 tag 时，看该物品是否属于这个 tag
        if (stackIsTag) {
            return tagMembers(item, tags).contains(id.toLowerCase(Locale.ROOT));
        }
        return item.equalsIgnoreCase(id);
    }

    private static boolean anyInputMatches(List<Stack> inputs, String id, boolean isTag, List<TagEntry> tags) {
        if (inputs == null) {
            return false;
        }
        for (Stack s : inputs) {
            if (stackMatches(s, id, isTag, tags)) {
                return true;
            }
        }
        return false;
    }

    private static LinkedHashSet<String> tagMembers(String tagId, List<TagEntry> tags) {
        LinkedHashSet<String> out = new LinkedHashSet<>();
        String want = normalTagId(tagId);
        if (tags != null) {
            for (TagEntry t : tags) {
                if (t.id() != null && normalTagId(t.id()).equals(want) && t.memberIds() != null) {
                    for (String m : t.memberIds()) {
                        if (m != null) {
                            out.add(m.toLowerCase(Locale.ROOT));
                        }
                    }
                }
            }
        }
        return out;
    }
}
