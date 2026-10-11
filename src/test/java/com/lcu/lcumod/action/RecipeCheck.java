package com.lcu.lcumod.action;

import java.util.ArrayList;
import java.util.List;

/**
 * Zero-dependency self-check for {@link RecipeQuery} (pure logic of item identity alignment and
 * recipe search). Same shape as the other verify* checks: plain Java + JavaExec (see
 * {@code verifyRecipe} in build.gradle), no third-party dependency, runs as part of
 * {@code build} / {@code check}. Assertion text is ASCII (Windows console codepage is GBK).
 *
 * <p>Covers the five contracts:
 * <ol>
 *   <li>id / name / tag alignment priority (id exact beats name; en + localized names both match);</li>
 *   <li>ambiguity is never guessed (candidates instead) and unknown names carry nearest candidates;</li>
 *   <li>workstation normalization + modded/unsupported stations refused with a reason;</li>
 *   <li>recipe row fields, id normalization, output/input/tag filtering, limit + truncation;</li>
 *   <li>craft target alignment refuses ambiguity / tags.</li>
 * </ol>
 */
public final class RecipeCheck {

    private static int passed = 0;
    private static int failed = 0;

    private RecipeCheck() {
    }

    public static void main(String[] args) {
        idNormalization();
        idExactPriority();
        nameMatchingEnAndLocalized();
        ambiguityIsNeverGuessed();
        unknownCarriesNearestCandidates();
        candidateListIsBounded();
        tagAlignment();
        craftTargetRefusesAmbiguityAndTags();
        stationNormalization();
        stationSupport();
        searchUnfilteredTotalAndLimit();
        searchByOutputIdAndName();
        searchInputMatchesTagMembersBothWays();
        searchStationFilterAndNormalization();
        searchUnsupportedStationsRefused();
        searchEmptyAndAmbiguousFilters();
        scoringSanity();

        System.out.println();
        System.out.println("verifyRecipe: " + passed + " passed, " + failed + " failed");
        if (failed > 0) {
            System.exit(1);
        }
    }

    // ── catalog ───────────────────────────────────────────────────

    private static List<RecipeQuery.ItemEntry> items() {
        List<RecipeQuery.ItemEntry> out = new ArrayList<>();
        out.add(item("minecraft:oak_planks", "oak_planks", "Oak Planks", "橡木木板"));
        out.add(item("minecraft:birch_planks", "birch_planks", "Birch Planks", "白桦木板"));
        out.add(item("minecraft:diamond_sword", "diamond_sword", "Diamond Sword", "钻石剑"));
        out.add(item("minecraft:coal", "coal", "Coal", "煤炭"));
        out.add(item("minecraft:charcoal", "charcoal", "Charcoal", "木炭"));
        out.add(item("minecraft:stone", "stone", "Stone", "石头"));
        out.add(item("minecraft:stick", "stick", "Stick", "木棍"));
        out.add(item("minecraft:iron_ingot", "iron_ingot", "Iron Ingot", "铁锭"));
        out.add(item("minecraft:bedrock", "bedrock", "Bedrock", "基岩"));
        // id-priority fixture: id "minecraft:foo" has name "Bar", and vice versa
        out.add(item("minecraft:foo", "foo", "Bar"));
        out.add(item("minecraft:bar", "bar", "Foo"));
        // same-name fixture for ambiguity
        out.add(item("mod:a", "a", "Planks"));
        out.add(item("mod:b", "b", "Planks"));
        return out;
    }

    private static RecipeQuery.ItemEntry item(String id, String path, String... names) {
        List<String> all = new ArrayList<>(List.of(names));
        all.add(path);
        all.add(path.replace('_', ' '));
        return new RecipeQuery.ItemEntry(id, path, List.copyOf(all));
    }

    private static List<RecipeQuery.TagEntry> tags() {
        return List.of(
                new RecipeQuery.TagEntry("#minecraft:planks",
                        List.of("minecraft:oak_planks", "minecraft:birch_planks")),
                new RecipeQuery.TagEntry("#minecraft:coals",
                        List.of("minecraft:coal", "minecraft:charcoal")));
    }

    private static RecipeQuery.Resolution resolve(String q) {
        return RecipeQuery.resolveItem(q, items(), tags());
    }

    // ── (1) alignment ─────────────────────────────────────────────

    private static void idNormalization() {
        check("planks -> minecraft:planks", "minecraft:planks".equals(RecipeQuery.normalizeId("planks")));
        check("case folded", "minecraft:oak_planks".equals(RecipeQuery.normalizeId("Oak_Planks")));
        check("namespaced kept", "minecraft:stone".equals(RecipeQuery.normalizeId("minecraft:stone")));
        check("blank -> empty", RecipeQuery.normalizeId("   ").isEmpty());
        check("null -> empty", RecipeQuery.normalizeId(null).isEmpty());
        check("pathOf drops namespace", "oak_planks".equals(RecipeQuery.pathOf("minecraft:oak_planks")));
        check("pathOf no namespace", "planks".equals(RecipeQuery.pathOf("planks")));
    }

    private static void idExactPriority() {
        RecipeQuery.Resolution r = resolve("stone");
        check("bare path aligns by id", r.ok() && RecipeQuery.EXACT_ID.equals(r.reason()));
        check("id match item", r.match() != null && "minecraft:stone".equals(r.match().item()));
        check("id match not a tag", r.match() != null && !r.match().tag());

        RecipeQuery.Resolution r2 = resolve("minecraft:coal");
        check("namespaced id", r2.ok() && "minecraft:coal".equals(r2.match().item()));
        check("id score = 100", r2.candidates().get(0).score() == RecipeQuery.SCORE_EXACT_ID);

        // id wins over a name that matches another item
        RecipeQuery.Resolution r3 = resolve("foo");
        check("id exact beats name", r3.ok() && RecipeQuery.EXACT_ID.equals(r3.reason()));
        check("id exact picks the id", "minecraft:foo".equals(r3.match().item()));
    }

    private static void nameMatchingEnAndLocalized() {
        RecipeQuery.Resolution en = resolve("Diamond Sword");
        check("english name", en.ok() && RecipeQuery.NAME.equals(en.reason()));
        check("english name item", "minecraft:diamond_sword".equals(en.match().item()));

        RecipeQuery.Resolution zh = resolve("钻石剑");
        check("localized name", zh.ok() && RecipeQuery.NAME.equals(zh.reason()));
        check("localized name item", "minecraft:diamond_sword".equals(zh.match().item()));

        RecipeQuery.Resolution spaced = resolve("oak planks");
        check("spaced name", spaced.ok() && "minecraft:oak_planks".equals(spaced.match().item()));

        RecipeQuery.Resolution ci = resolve("COAL");
        check("case-insensitive -> id", ci.ok() && "minecraft:coal".equals(ci.match().item()));

        check("name score = 90", RecipeQuery.nameScore("Diamond Sword", "Diamond Sword") == RecipeQuery.SCORE_NAME);
        check("name score ci = 90", RecipeQuery.nameScore("coal", "Coal") == RecipeQuery.SCORE_NAME);
    }

    private static void ambiguityIsNeverGuessed() {
        RecipeQuery.Resolution r = resolve("Planks");
        check("ambiguous not ok", !r.ok());
        check("ambiguous reason", RecipeQuery.AMBIGUOUS.equals(r.reason()));
        check("ambiguous has no match", r.match() == null);
        check("ambiguous lists both", containsItem(r.candidates(), "mod:a") && containsItem(r.candidates(), "mod:b"));
        boolean allTop90 = r.candidates().stream()
                .filter(c -> !"tag".equals(c.reason()))
                .allMatch(c -> c.score() == RecipeQuery.SCORE_NAME);
        check("ambiguous candidates share top score", allTop90);
    }

    private static void unknownCarriesNearestCandidates() {
        RecipeQuery.Resolution r = resolve("diamond swrod");
        check("typo -> unknown", !r.ok() && RecipeQuery.UNKNOWN.equals(r.reason()));
        check("typo has no match", r.match() == null);
        check("typo nearest includes target", containsItem(r.candidates(), "minecraft:diamond_sword"));
        check("nearest score > 0", r.candidates().get(0).score() > 0);

        RecipeQuery.Resolution garbage = resolve("qqqqqqqqqqzz");
        check("garbage -> unknown", !garbage.ok() && RecipeQuery.UNKNOWN.equals(garbage.reason()));

        RecipeQuery.Resolution blank = resolve("   ");
        check("blank -> unknown", !blank.ok() && RecipeQuery.UNKNOWN.equals(blank.reason()));
    }

    private static void candidateListIsBounded() {
        List<String> members = new ArrayList<>();
        for (int i = 0; i < 40; i++) {
            members.add("minecraft:m" + i);
        }
        List<RecipeQuery.TagEntry> many = List.of(new RecipeQuery.TagEntry("#minecraft:many", members));
        RecipeQuery.Resolution r = RecipeQuery.resolveItem("#minecraft:many", items(), many);
        check("tag match ok", r.ok() && RecipeQuery.TAG.equals(r.reason()));
        check("tag member count honest", r.match().count() == 40);
        check("tag candidates bounded", r.candidates().size() <= RecipeQuery.MAX_CANDIDATES);
    }

    private static void tagAlignment() {
        RecipeQuery.Resolution r = resolve("#minecraft:planks");
        check("tag ok", r.ok() && RecipeQuery.TAG.equals(r.reason()));
        check("tag match is the tag", r.match() != null && r.match().tag());
        check("tag id", "#minecraft:planks".equals(r.match().item()));
        check("tag member count", r.match().count() == 2);
        check("tag candidates are members", containsItem(r.candidates(), "minecraft:oak_planks")
                && containsItem(r.candidates(), "minecraft:birch_planks"));
        check("tag member reason", "tag_member".equals(r.candidates().get(0).reason()));

        RecipeQuery.Resolution shortTag = resolve("#planks");
        check("namespace-less tag completes", shortTag.ok() && "#minecraft:planks".equals(shortTag.match().item()));

        RecipeQuery.Resolution unknown = resolve("#minecraft:nope");
        check("unknown tag", !unknown.ok() && RecipeQuery.UNKNOWN.equals(unknown.reason()));

        // a bare tag path surfaces the tag as a suggestion without silently resolving
        RecipeQuery.Resolution bare = resolve("coals");
        check("bare tag path -> unknown", !bare.ok() && RecipeQuery.UNKNOWN.equals(bare.reason()));
        check("bare tag path suggests tag", bare.candidates().stream().anyMatch(c -> "tag".equals(c.reason())));
    }

    private static void craftTargetRefusesAmbiguityAndTags() {
        RecipeQuery.CraftTarget ok = RecipeQuery.craftTarget("Diamond Sword", items(), tags());
        check("craft target unique", ok.ok() && "minecraft:diamond_sword".equals(ok.itemId()));

        RecipeQuery.CraftTarget amb = RecipeQuery.craftTarget("Planks", items(), tags());
        check("craft ambiguous refused", !amb.ok() && RecipeQuery.AMBIGUOUS.equals(amb.reason()));
        check("craft ambiguous returns candidates", containsItem(amb.candidates(), "mod:a"));

        RecipeQuery.CraftTarget tag = RecipeQuery.craftTarget("#minecraft:planks", items(), tags());
        check("craft tag refused", !tag.ok() && RecipeQuery.ERR_TAG_NOT_CRAFTABLE.equals(tag.reason()));

        RecipeQuery.CraftTarget unknown = RecipeQuery.craftTarget("qqqqzz", items(), tags());
        check("craft unknown refused", !unknown.ok() && RecipeQuery.UNKNOWN.equals(unknown.reason()));
    }

    // ── (2) stations ──────────────────────────────────────────────

    private static void stationNormalization() {
        check("crafting", RecipeQuery.S_CRAFTING.equals(RecipeQuery.canonicalStation("crafting")));
        check("workbench alias", RecipeQuery.S_CRAFTING.equals(RecipeQuery.canonicalStation("workbench")));
        check("chinese crafting", RecipeQuery.S_CRAFTING.equals(RecipeQuery.canonicalStation("工作台")));
        check("smelting -> furnace", RecipeQuery.S_FURNACE.equals(RecipeQuery.canonicalStation("smelting")));
        check("chinese furnace", RecipeQuery.S_FURNACE.equals(RecipeQuery.canonicalStation("熔炉")));
        check("blast -> blast_furnace",
                RecipeQuery.S_BLAST_FURNACE.equals(RecipeQuery.canonicalStation("blast")));
        check("namespaced blast",
                RecipeQuery.S_BLAST_FURNACE.equals(RecipeQuery.canonicalStation("minecraft:blasting")));
        check("smoking -> smoker", RecipeQuery.S_SMOKER.equals(RecipeQuery.canonicalStation("smoking")));
        check("stonecutter", RecipeQuery.S_STONECUTTER.equals(RecipeQuery.canonicalStation("stonecutter")));
        check("stonecutting -> stonecutter",
                RecipeQuery.S_STONECUTTER.equals(RecipeQuery.canonicalStation("minecraft:stonecutting")));
        check("smithing", RecipeQuery.S_SMITHING.equals(RecipeQuery.canonicalStation("smithing_table")));
        check("brewing", RecipeQuery.S_BREWING.equals(RecipeQuery.canonicalStation("brewing_stand")));
        check("modded station -> null", RecipeQuery.canonicalStation("mekanism:crusher") == null);
        check("campfire -> null", RecipeQuery.canonicalStation("campfire_cooking") == null);
    }

    private static void stationSupport() {
        check("crafting supported", RecipeQuery.isSupportedStation(RecipeQuery.S_CRAFTING));
        check("furnace supported", RecipeQuery.isSupportedStation(RecipeQuery.S_FURNACE));
        check("blast supported", RecipeQuery.isSupportedStation(RecipeQuery.S_BLAST_FURNACE));
        check("smoker supported", RecipeQuery.isSupportedStation(RecipeQuery.S_SMOKER));
        check("stonecutter supported", RecipeQuery.isSupportedStation(RecipeQuery.S_STONECUTTER));
        check("smithing supported", RecipeQuery.isSupportedStation(RecipeQuery.S_SMITHING));
        check("brewing recognized but no displays",
                !RecipeQuery.isSupportedStation(RecipeQuery.S_BREWING));
        check("null unsupported", !RecipeQuery.isSupportedStation(null));
    }

    // ── (3) search ────────────────────────────────────────────────

    private static List<RecipeQuery.RecipeRow> rows() {
        return List.of(
                new RecipeQuery.RecipeRow("0", "crafting", true,
                        List.of(stack("#minecraft:planks", 1)), stack("minecraft:stick", 4), "minecraft:stick"),
                new RecipeQuery.RecipeRow("1", "minecraft:crafting", true,
                        List.of(stack("minecraft:oak_planks", 4)), stack("minecraft:crafting_table", 1),
                        "minecraft:crafting_table"),
                new RecipeQuery.RecipeRow("2", "furnace", true,
                        List.of(stack("minecraft:coal", 1)), stack("Minecraft:Iron_Ingot", 1), "minecraft:iron_ingot"),
                new RecipeQuery.RecipeRow("3", "modid:crusher", false,
                        List.of(stack("minecraft:stone", 1)), stack("minecraft:gravel", 1), "modid:crush_stone"),
                new RecipeQuery.RecipeRow("4", "smoker", true,
                        List.of(stack("minecraft:coal", 1)), stack("minecraft:cooked_beef", 1), "minecraft:cook"));
    }

    private static RecipeQuery.Stack stack(String item, int count) {
        return new RecipeQuery.Stack(item, count);
    }

    private static RecipeQuery.SearchResult search(String output, String input, String station, int limit) {
        return RecipeQuery.search(rows(), output, input, station, limit, items(), tags());
    }

    private static void searchUnfilteredTotalAndLimit() {
        RecipeQuery.SearchResult all = search(null, null, null, 0);
        check("unfiltered ok", all.ok() && RecipeQuery.OK.equals(all.reason()));
        check("unfiltered total", all.total() == 5);
        check("unfiltered count", all.rows().size() == 5);
        check("unfiltered not truncated", !all.truncated());

        RecipeQuery.SearchResult limited = search(null, null, null, 2);
        check("limit caps rows", limited.rows().size() == 2);
        check("limit keeps total", limited.total() == 5);
        check("limit marks truncated", limited.truncated());

        check("row fields present", !all.rows().get(0).displayId().isEmpty()
                && all.rows().get(0).output() != null && all.rows().get(0).inputs() != null);
        check("unsupported row flagged", all.rows().stream().anyMatch(r -> !r.supported()));
    }

    private static void searchByOutputIdAndName() {
        RecipeQuery.SearchResult byId = search("minecraft:stick", null, null, 0);
        check("output by id", byId.total() == 1 && "0".equals(byId.rows().get(0).displayId()));

        RecipeQuery.SearchResult byName = search("Stick", null, null, 0);
        check("output by name resolves", byName.total() == 1 && "0".equals(byName.rows().get(0).displayId()));

        // row output stored as "Minecraft:Iron_Ingot" still matched by the normalized id
        RecipeQuery.SearchResult normalized = search("minecraft:iron_ingot", null, null, 0);
        check("search normalizes row ids", normalized.total() == 1 && "2".equals(normalized.rows().get(0).displayId()));

        RecipeQuery.SearchResult zh = search("钻石剑", null, null, 0);
        check("output name resolves but none produce it", zh.ok()
                && RecipeQuery.ERR_NO_RECIPES.equals(zh.reason()) && zh.total() == 0);
    }

    private static void searchInputMatchesTagMembersBothWays() {
        // input is a real item; one row lists that item, the other lists the tag containing it
        RecipeQuery.SearchResult byItem = search(null, "minecraft:oak_planks", null, 0);
        check("input item matches direct + tag member", byItem.total() == 2);

        // input is a tag; matches the row with the tag and the row with a member item
        RecipeQuery.SearchResult byTag = search(null, "#minecraft:planks", null, 0);
        check("input tag matches tag + members", byTag.total() == 2);

        RecipeQuery.SearchResult absent = search(null, "minecraft:bedrock", null, 0);
        check("input absent -> empty", absent.ok() && absent.total() == 0
                && RecipeQuery.ERR_NO_RECIPES.equals(absent.reason()));
    }

    private static void searchStationFilterAndNormalization() {
        RecipeQuery.SearchResult furnace = search(null, null, "熔炉", 0);
        check("station zh alias filters", furnace.total() == 1 && "2".equals(furnace.rows().get(0).displayId()));

        // rows "crafting" and "minecraft:crafting" both normalize to crafting
        RecipeQuery.SearchResult crafting = search(null, null, "工作台", 0);
        check("station normalizes rows", crafting.total() == 2);

        RecipeQuery.SearchResult smoker = search(null, null, "smoking", 0);
        check("smoking alias -> smoker", smoker.total() == 1 && "4".equals(smoker.rows().get(0).displayId()));

        RecipeQuery.SearchResult none = search(null, null, "stonecutter", 0);
        check("supported station with no rows -> empty", none.ok()
                && RecipeQuery.ERR_NO_RECIPES.equals(none.reason()) && none.total() == 0);
    }

    private static void searchUnsupportedStationsRefused() {
        RecipeQuery.SearchResult modded = search(null, null, "mekanism:crusher", 0);
        check("modded station refused", !modded.ok()
                && RecipeQuery.ERR_UNSUPPORTED_STATION.equals(modded.reason()));
        check("modded refusal is empty", modded.rows().isEmpty() && modded.total() == 0);

        RecipeQuery.SearchResult brewing = search(null, null, "酿造台", 0);
        check("brewing refused (no display)", !brewing.ok()
                && RecipeQuery.ERR_UNSUPPORTED_STATION.equals(brewing.reason()));
    }

    private static void searchEmptyAndAmbiguousFilters() {
        RecipeQuery.SearchResult amb = search("Planks", null, null, 0);
        check("ambiguous output refused", !amb.ok()
                && RecipeQuery.ERR_AMBIGUOUS_OUTPUT.equals(amb.reason()));
        check("ambiguous output returns candidates", containsItem(amb.candidates(), "mod:a"));

        RecipeQuery.SearchResult unknown = search("qqqqzz", null, null, 0);
        check("unknown output refused", !unknown.ok()
                && RecipeQuery.ERR_UNKNOWN_OUTPUT.equals(unknown.reason()));

        RecipeQuery.SearchResult ambIn = search(null, "Planks", null, 0);
        check("ambiguous input refused", !ambIn.ok()
                && RecipeQuery.ERR_AMBIGUOUS_INPUT.equals(ambIn.reason()));
    }

    private static void scoringSanity() {
        check("similarity identical = 100", RecipeQuery.similarity("stone", "stone") == 100);
        check("similarity prefix >= 70", RecipeQuery.similarity("stone", "stone_bricks") >= 70);
        check("similarity disjoint low", RecipeQuery.similarity("abc", "xyz") < 40);
        check("nameScore exact = 90", RecipeQuery.nameScore("Stone", "stone") == 90);
        check("nameScore substring < 90", RecipeQuery.nameScore("stone", "stone sword") < 90);
        check("nameScore empty = 0", RecipeQuery.nameScore("", "stone") == 0);
    }

    // ── helpers ───────────────────────────────────────────────────

    private static boolean containsItem(List<RecipeQuery.Candidate> candidates, String id) {
        return candidates != null && candidates.stream().anyMatch(c -> id.equalsIgnoreCase(c.item()));
    }

    private static void check(String name, boolean ok) {
        if (ok) {
            passed++;
        } else {
            failed++;
            System.out.println("FAIL: " + name);
        }
    }
}
