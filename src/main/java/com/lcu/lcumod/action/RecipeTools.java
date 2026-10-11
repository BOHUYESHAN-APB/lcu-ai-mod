package com.lcu.lcumod.action;

import net.minecraft.client.Minecraft;
import net.minecraft.core.Holder;
import net.minecraft.core.registries.BuiltInRegistries;
import net.minecraft.locale.Language;
import net.minecraft.resources.Identifier;
import net.minecraft.util.context.ContextMap;
import net.minecraft.world.item.Item;
import net.minecraft.world.item.ItemStack;
import net.minecraft.world.item.crafting.RecipeHolder;
import net.minecraft.world.item.crafting.display.FurnaceRecipeDisplay;
import net.minecraft.world.item.crafting.display.RecipeDisplay;
import net.minecraft.world.item.crafting.display.RecipeDisplayId;
import net.minecraft.world.item.crafting.display.ShapedCraftingRecipeDisplay;
import net.minecraft.world.item.crafting.display.ShapelessCraftingRecipeDisplay;
import net.minecraft.world.item.crafting.display.SlotDisplay;
import net.minecraft.world.item.crafting.display.SlotDisplayContext;
import net.minecraft.world.item.crafting.display.SmithingRecipeDisplay;
import net.minecraft.world.item.crafting.display.StonecutterRecipeDisplay;

import java.util.ArrayList;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Optional;

/**
 * {@link RecipeQuery} 的**实注册表适配层**：把真实物品注册表 / tag / 客户端语言名
 * / 服务端配方显示，映射成纯逻辑用的数据结构。
 *
 * <pre>
 * 物品身份对齐  ← BuiltInRegistries.ITEM（id / path）+ Item#getDescriptionId +
 *                Language.DEFAULT_INSTANCE（英文名）/ Language.getInstance()（本地化名，26.3 客户端）
 * tag           ← BuiltInRegistries.ITEM.getTags()（HolderSet.Named#key + 成员）
 * 配方显示      ← 集成服务端 RecipeManager#getRecipes + #listDisplaysForRecipe（26.3 客户端不再收到配方表）
 * </pre>
 *
 * <h2>工作站范围（原版先做全）</h2>
 * 工作台 / 熔炉 / 高炉 / 烟熏炉 / 切石机 / 锻造台：{@code RecipeType} 的注册 id 经
 * {@link RecipeQuery#canonicalStation} 归一到七个名字；酿造台（{@code minecraft:brewing}）
 * 在 26.3 <b>没有 RecipeDisplay</b>（{@code Recipe#display()} 默认返回空列表），因此可识别但
 * {@code stationHasDisplays=false}，如实回 {@code unsupported_station}。
 *
 * <h2>扩展点（以后的 JEI / 模组工作站，勿删）</h2>
 * 这里的数据源只有一种：原版 {@code RecipeDisplay}。要接 JEI 或模组机器（机械动力 / Mek 等），
 * 只需在 {@link #recipeRows} 与 {@link #recipeByOutput} 之外**追加一个数据源实现**
 * （把 JEI 的 {@code IRecipeManager} 结果同样映射成 {@link RecipeQuery.RecipeRow}），
 * 上层 wire 命令（search_recipes / craft_via_recipe）与纯逻辑 {@link RecipeQuery} 都不需要改。
 * 目前刻意不引入任何模组依赖。
 */
public final class RecipeTools {

    private RecipeTools() {
    }

    // ── 目录（真实注册表 → 纯逻辑结构）─────────────────────────────

    /** 物品目录：id + path + 可匹配名字（本地化名 / 英文名 / path / path 去下划线）。 */
    public static List<RecipeQuery.ItemEntry> itemCatalog() {
        Language english = Language.DEFAULT_INSTANCE;
        Language current = safeLanguage();
        List<RecipeQuery.ItemEntry> out = new ArrayList<>();
        for (Item item : BuiltInRegistries.ITEM) {
            Identifier key = BuiltInRegistries.ITEM.getKey(item);
            if (key == null) {
                continue;
            }
            String id = key.toString();
            String path = key.getPath();
            LinkedHashSet<String> names = new LinkedHashSet<>();
            String desc = item.getDescriptionId();
            if (desc != null && !desc.isBlank()) {
                String local = lookup(current, desc);
                if (local != null) {
                    names.add(local);
                }
                String en = lookup(english, desc);
                if (en != null) {
                    names.add(en);
                }
            }
            names.add(path);
            names.add(path.replace('_', ' '));
            out.add(new RecipeQuery.ItemEntry(id, path, List.copyOf(names)));
        }
        return out;
    }

    private static Language safeLanguage() {
        try {
            return Language.getInstance();
        } catch (Exception e) {
            return null;
        }
    }

    private static String lookup(Language lang, String key) {
        if (lang == null || key == null) {
            return null;
        }
        try {
            return lang.has(key) ? lang.getOrDefault(key) : null;
        } catch (Exception e) {
            return null;
        }
    }

    /** 物品 tag 目录：{@code #minecraft:planks} → 成员 id。 */
    public static List<RecipeQuery.TagEntry> tagCatalog() {
        List<RecipeQuery.TagEntry> out = new ArrayList<>();
        BuiltInRegistries.ITEM.getTags().forEach(named -> {
            String tagId = "#" + named.key().location();
            List<String> members = new ArrayList<>();
            for (Holder<Item> holder : named) {
                Identifier key = BuiltInRegistries.ITEM.getKey(holder.value());
                if (key != null) {
                    members.add(key.toString());
                }
            }
            out.add(new RecipeQuery.TagEntry(tagId, List.copyOf(members)));
        });
        return out;
    }

    /** 物品的本地化显示名；拿不到就回 id（绝不编）。 */
    public static String displayName(String itemId) {
        if (itemId == null || itemId.isBlank()) {
            return "";
        }
        if (itemId.charAt(0) == '#') {
            return itemId;
        }
        Identifier id = Identifier.tryParse(itemId);
        if (id == null) {
            return itemId;
        }
        Optional<Item> item = BuiltInRegistries.ITEM.getOptional(id);
        if (item.isEmpty()) {
            return itemId;
        }
        Language current = safeLanguage();
        String desc = item.get().getDescriptionId();
        String name = lookup(current, desc);
        return name != null ? name : itemId;
    }

    // ── 物品身份对齐（带背包数量）──────────────────────────────────

    /** {@code resolve_item}：对齐 query，并给命中项补上背包数量。 */
    public static RecipeQuery.Resolution resolveItem(Minecraft mc, String query) {
        RecipeQuery.Resolution r = RecipeQuery.resolveItem(query, itemCatalog(), tagCatalog());
        if (r.ok() && r.match() != null && !r.match().tag() && mc != null && mc.player != null) {
            int have = ActionExecutor.countInventoryItem(mc, r.match().item());
            RecipeQuery.Match m = r.match();
            return new RecipeQuery.Resolution(true, r.reason(),
                    new RecipeQuery.Match(m.item(), m.name(), have, false), r.candidates());
        }
        return r;
    }

    /** {@code craft_via_recipe} 的目标对齐：唯一 id 才放行。 */
    public static RecipeQuery.CraftTarget craftTarget(Minecraft mc, String query) {
        return RecipeQuery.craftTarget(query, itemCatalog(), tagCatalog());
    }

    // ── 配方搜索（服务端配方显示）──────────────────────────────────

    /** {@code search_recipes}：数据源 = 服务端发来的配方显示（同 {@code handlePlaceRecipe} 那套）。 */
    public static RecipeQuery.SearchResult searchRecipes(Minecraft mc, String output, String input,
                                                         String station, int limit) {
        if (mc == null || mc.getSingleplayerServer() == null) {
            return new RecipeQuery.SearchResult(false, RecipeQuery.ERR_RECIPE_TABLE_UNAVAILABLE,
                    List.of(), 0, false, List.of());
        }
        List<RecipeQuery.ItemEntry> items = List.of();
        List<RecipeQuery.TagEntry> tags = List.of();
        if (notBlank(output) || notBlank(input)) {
            items = itemCatalog();
            tags = tagCatalog();
        }
        return RecipeQuery.search(recipeRows(mc), output, input, station, limit, items, tags);
    }

    private static boolean notBlank(String s) {
        return s != null && !s.isBlank();
    }

    /** 枚举服务端全部配方显示行（{@code display_id} = 服务端分配的 RecipeDisplayId）。 */
    public static List<RecipeQuery.RecipeRow> recipeRows(Minecraft mc) {
        if (mc == null || mc.getSingleplayerServer() == null || mc.level == null) {
            return List.of();
        }
        var manager = mc.getSingleplayerServer().getRecipeManager();
        ContextMap ctx = SlotDisplayContext.fromLevel(mc.level);
        List<RecipeQuery.RecipeRow> rows = new ArrayList<>();
        for (RecipeHolder<?> recipe : manager.getRecipes()) {
            String typeId = typeIdOf(recipe);
            String canonical = RecipeQuery.canonicalStation(typeId);
            String station = canonical != null ? canonical : typeId;
            boolean supported = RecipeQuery.isSupportedStation(canonical);
            String recipeId = recipe.id().identifier().toString();
            manager.listDisplaysForRecipe(recipe.id(), entry -> {
                RecipeDisplay display = entry.display();
                RecipeQuery.Stack output = outputOf(display, ctx);
                if (output == null) {
                    return; // 无产物显示的行不可用（如部分 special 配方）
                }
                List<RecipeQuery.Stack> inputs = inputsOf(display, ctx);
                rows.add(new RecipeQuery.RecipeRow(
                        String.valueOf(entry.id().index()), station, supported, inputs, output, recipeId));
            });
        }
        return rows;
    }

    // ── craft_via_recipe 需要的单条配方查询 ────────────────────────

    /** 由 display_id 反查配方（{@code RecipeManager#getRecipeFromDisplay}）。 */
    public static RecipeHolder<?> recipeByDisplayId(Minecraft mc, int displayId) {
        if (mc == null || mc.getSingleplayerServer() == null) {
            return null;
        }
        var info = mc.getSingleplayerServer().getRecipeManager()
                .getRecipeFromDisplay(new RecipeDisplayId(displayId));
        return info == null ? null : info.parent();
    }

    /** display_id 对应显示的产物 id；拿不到返回空串。 */
    public static String outputIdOfDisplay(Minecraft mc, int displayId) {
        if (mc == null || mc.getSingleplayerServer() == null || mc.level == null) {
            return "";
        }
        var info = mc.getSingleplayerServer().getRecipeManager()
                .getRecipeFromDisplay(new RecipeDisplayId(displayId));
        if (info == null) {
            return "";
        }
        RecipeQuery.Stack out = outputOf(info.display().display(), SlotDisplayContext.fromLevel(mc.level));
        return out == null ? "" : out.item();
    }

    /** 产物 id → 优先级最高的配方（工作台优先，其次切石/锻造，最后熔炼类）。 */
    public static RecipeHolder<?> recipeByOutput(Minecraft mc, String itemId) {
        if (mc == null || mc.getSingleplayerServer() == null || mc.level == null || itemId == null) {
            return null;
        }
        String want = RecipeQuery.normalizeId(itemId);
        ContextMap ctx = SlotDisplayContext.fromLevel(mc.level);
        RecipeHolder<?> best = null;
        int bestRank = Integer.MAX_VALUE;
        for (RecipeHolder<?> recipe : mc.getSingleplayerServer().getRecipeManager().getRecipes()) {
            for (RecipeDisplay display : recipe.value().display()) {
                RecipeQuery.Stack out = outputOf(display, ctx);
                if (out == null || !out.item().equalsIgnoreCase(want)) {
                    continue;
                }
                int rank = stationRank(RecipeQuery.canonicalStation(typeIdOf(recipe)));
                if (rank < bestRank) {
                    bestRank = rank;
                    best = recipe;
                }
                break;
            }
        }
        return best;
    }

    /** 配方的产物 id（取第一个非空产物显示）；拿不到返回空串。 */
    public static String outputIdOf(Minecraft mc, RecipeHolder<?> recipe) {
        if (mc == null || mc.level == null || recipe == null) {
            return "";
        }
        ContextMap ctx = SlotDisplayContext.fromLevel(mc.level);
        for (RecipeDisplay display : recipe.value().display()) {
            RecipeQuery.Stack out = outputOf(display, ctx);
            if (out != null) {
                return out.item();
            }
        }
        return "";
    }

    /** 配方归一后的工作站名（原版七个之一，模组类型保留原始 id）。 */
    public static String stationOf(RecipeHolder<?> recipe) {
        if (recipe == null) {
            return "";
        }
        String typeId = typeIdOf(recipe);
        String canonical = RecipeQuery.canonicalStation(typeId);
        return canonical != null ? canonical : typeId;
    }

    private static int stationRank(String canonical) {
        if (canonical == null) {
            return 99;
        }
        return switch (canonical) {
            case RecipeQuery.S_CRAFTING -> 0;
            case RecipeQuery.S_STONECUTTER -> 1;
            case RecipeQuery.S_SMITHING -> 2;
            case RecipeQuery.S_FURNACE -> 3;
            case RecipeQuery.S_BLAST_FURNACE -> 4;
            case RecipeQuery.S_SMOKER -> 5;
            default -> 99;
        };
    }

    private static String typeIdOf(RecipeHolder<?> recipe) {
        if (recipe == null || recipe.value() == null || recipe.value().getType() == null) {
            return "";
        }
        Identifier key = BuiltInRegistries.RECIPE_TYPE.getKey(recipe.value().getType());
        return key == null ? "" : key.toString();
    }

    // ── 显示 → 输入/产物 ──────────────────────────────────────────

    private static RecipeQuery.Stack outputOf(RecipeDisplay display, ContextMap ctx) {
        if (display == null) {
            return null;
        }
        ItemStack first = display.result().resolveForFirstStack(ctx);
        if (first == null || first.isEmpty()) {
            return null;
        }
        return new RecipeQuery.Stack(itemId(first.getItem()), Math.max(1, first.getCount()));
    }

    private static List<RecipeQuery.Stack> inputsOf(RecipeDisplay display, ContextMap ctx) {
        List<SlotDisplay> slots = new ArrayList<>();
        if (display instanceof ShapedCraftingRecipeDisplay shaped) {
            slots.addAll(shaped.ingredients());
        } else if (display instanceof ShapelessCraftingRecipeDisplay shapeless) {
            slots.addAll(shapeless.ingredients());
        } else if (display instanceof FurnaceRecipeDisplay furnace) {
            slots.add(furnace.ingredient());
        } else if (display instanceof StonecutterRecipeDisplay stonecutter) {
            slots.add(stonecutter.input());
        } else if (display instanceof SmithingRecipeDisplay smithing) {
            slots.add(smithing.template());
            slots.add(smithing.base());
            slots.add(smithing.addition());
        }
        List<RecipeQuery.Stack> out = new ArrayList<>();
        for (SlotDisplay slot : slots) {
            RecipeQuery.Stack stack = slotStack(slot, ctx);
            if (stack != null) {
                out.add(stack);
            }
        }
        return out;
    }

    /** 一格显示 → 规范字符串：tag 用 {@code #ns:path}，否则用物品 id。 */
    private static RecipeQuery.Stack slotStack(SlotDisplay slot, ContextMap ctx) {
        if (slot instanceof SlotDisplay.TagSlotDisplay tagDisplay) {
            String tagId = tagDisplay.tag().unwrapKey()
                    .map(k -> "#" + k.location())
                    .orElse(null);
            if (tagId != null) {
                return new RecipeQuery.Stack(tagId, 1);
            }
        }
        ItemStack first = slot.resolveForFirstStack(ctx);
        if (first == null || first.isEmpty()) {
            return null;
        }
        return new RecipeQuery.Stack(itemId(first.getItem()), Math.max(1, first.getCount()));
    }

    private static String itemId(Item item) {
        Identifier key = BuiltInRegistries.ITEM.getKey(item);
        return key == null ? "" : key.toString();
    }
}
