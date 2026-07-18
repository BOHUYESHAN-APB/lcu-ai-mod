package com.lcu.lcumod.action;

import net.minecraft.client.Minecraft;
import net.minecraft.core.registries.BuiltInRegistries;
import net.minecraft.core.registries.Registries;
import net.minecraft.resources.ResourceLocation;
import net.minecraft.tags.ItemTags;
import net.minecraft.tags.TagKey;
import net.minecraft.world.item.Item;
import net.minecraft.world.item.ItemStack;
import net.minecraft.world.item.crafting.Ingredient;
import net.minecraft.world.item.crafting.RecipeHolder;
import net.minecraft.world.item.crafting.RecipeType;

import java.util.ArrayList;
import java.util.Comparator;
import java.util.HashMap;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;

/** Builds a deterministic, recursive production plan across crafting and furnace recipes. */
public final class CraftingPlanner {

    private CraftingPlanner() {
    }

    public static final class CraftStep {
        public final String mode;
        public final String itemId;
        public final int craftOperations;
        public final String stationBlockId;
        public final boolean needsCraftingTable;
        public final RecipeHolder<?> recipe;

        public CraftStep(String mode, String itemId, int craftOperations, String stationBlockId, boolean needsCraftingTable, RecipeHolder<?> recipe) {
            this.mode = mode;
            this.itemId = itemId;
            this.craftOperations = craftOperations;
            this.stationBlockId = stationBlockId;
            this.needsCraftingTable = needsCraftingTable;
            this.recipe = recipe;
        }
    }

    private record RecipeOption(
        RecipeHolder<?> recipe,
        String mode,
        String stationBlockId,
        boolean needsCraftingTable
    ) {
    }

    private static final class RecipeIndexes {
        final Map<String, List<RecipeOption>> byOutput = new HashMap<>();
    }

    private static final class PlanningBudget {
        int remaining = 4096;

        boolean claim() {
            return remaining-- > 0;
        }
    }

    private static final class Resolution {
        final boolean valid;
        final Map<String, Integer> available;
        final List<CraftStep> steps;
        final LinkedHashMap<String, Integer> missingRaw;
        final long cost;
        final String tieBreak;

        Resolution(
            boolean valid,
            Map<String, Integer> available,
            List<CraftStep> steps,
            LinkedHashMap<String, Integer> missingRaw,
            long cost,
            String tieBreak
        ) {
            this.valid = valid;
            this.available = available;
            this.steps = steps;
            this.missingRaw = missingRaw;
            this.cost = cost;
            this.tieBreak = tieBreak;
        }
    }

    public static final class CraftPlan {
        public final String targetItemId;
        public final int requestedCount;
        public final List<CraftStep> steps = new ArrayList<>();
        public final LinkedHashMap<String, Integer> missingRaw = new LinkedHashMap<>();
        public boolean success = false;
        public String failureReason = "";

        public CraftPlan(String targetItemId, int requestedCount) {
            this.targetItemId = targetItemId;
            this.requestedCount = requestedCount;
        }

        public String describe() {
            List<String> stepDescriptions = steps.stream()
                .limit(8)
                .map(step -> step.mode + ":" + step.recipe.id() + "->" + step.itemId + "x" + step.craftOperations)
                .toList();
            return "target=" + targetItemId + "x" + requestedCount
                + ", success=" + success
                + ", missing=" + missingRaw
                + ", steps=" + stepDescriptions
                + (failureReason.isBlank() ? "" : ", failure=" + failureReason);
        }
    }

    public static CraftPlan plan(Minecraft mc, String targetItemId, int requestedCount) {
        CraftPlan plan = new CraftPlan(targetItemId, requestedCount);
        if (mc == null || mc.player == null || mc.level == null || requestedCount <= 0) {
            plan.success = true;
            return plan;
        }

        Resolution resolution = resolve(
            mc,
            targetItemId,
            requestedCount,
            snapshotInventory(mc),
            buildRecipeIndexes(mc),
            Set.of(),
            new PlanningBudget()
        );
        if (!resolution.valid) {
            plan.failureReason = "no acyclic production path";
            return plan;
        }

        plan.steps.addAll(resolution.steps);
        plan.missingRaw.putAll(resolution.missingRaw);
        plan.success = plan.missingRaw.isEmpty();
        if (!plan.success) {
            plan.failureReason = "missing raw resources";
        }
        return plan;
    }

    private static Resolution resolve(
        Minecraft mc,
        String targetItemId,
        int neededCount,
        Map<String, Integer> available,
        RecipeIndexes recipeIndexes,
        Set<String> visiting,
        PlanningBudget budget
    ) {
        if (!budget.claim()) return invalid();
        Map<String, Integer> remainingInventory = new HashMap<>(available);
        int consumed = consumeAvailable(remainingInventory, targetItemId, neededCount);
        int remaining = neededCount - consumed;
        if (remaining <= 0) {
            return success(remainingInventory, List.of(), new LinkedHashMap<>(), 0, targetItemId);
        }

        String visitKey = canonicalVisitKey(targetItemId);
        if (visiting.contains(visitKey)) {
            return invalid();
        }

        Set<String> nextVisiting = new HashSet<>(visiting);
        nextVisiting.add(visitKey);
        Resolution best = null;
        for (RecipeOption option : findRecipeOptions(recipeIndexes, targetItemId).stream().limit(32).toList()) {
            Resolution candidate = resolveRecipe(
                mc,
                targetItemId,
                remaining,
                remainingInventory,
                recipeIndexes,
                nextVisiting,
                option,
                budget
            );
            best = better(best, candidate);
        }

        LinkedHashMap<String, Integer> rawMissing = new LinkedHashMap<>();
        rawMissing.put(targetItemId, remaining);
        Resolution rawFallback = success(
            remainingInventory,
            List.of(),
            rawMissing,
            (long) acquisitionCost(targetItemId) * remaining,
            "raw:" + targetItemId
        );
        return better(best, rawFallback);
    }

    private static Resolution resolveRecipe(
        Minecraft mc,
        String targetItemId,
        int remaining,
        Map<String, Integer> available,
        RecipeIndexes recipeIndexes,
        Set<String> visiting,
        RecipeOption option,
        PlanningBudget budget
    ) {
        ItemStack result = option.recipe.value().getResultItem(mc.level.registryAccess());
        if (result.isEmpty()) return invalid();

        String resultId = BuiltInRegistries.ITEM.getKey(result.getItem()).toString();
        int outputCount = Math.max(1, result.getCount());
        int operations = (remaining + outputCount - 1) / outputCount;
        Resolution branch = success(
            new HashMap<>(available),
            List.of(),
            new LinkedHashMap<>(),
            (long) operationCost(option.mode) * operations,
            option.recipe.id().toString()
        );

        for (Ingredient ingredient : option.recipe.value().getIngredients()) {
            if (ingredient == null || ingredient.isEmpty()) continue;

            Resolution bestIngredient = null;
            List<String> candidateIds = new ArrayList<>();
            for (ItemStack stack : ingredient.getItems()) {
                if (!stack.isEmpty()) {
                    candidateIds.add(BuiltInRegistries.ITEM.getKey(stack.getItem()).toString());
                }
            }
            Map<String, Integer> ingredientInventory = branch.available;
            candidateIds = candidateIds.stream()
                .distinct()
                .sorted(Comparator
                    .comparingInt((String id) -> ingredientInventory.getOrDefault(id, 0) > 0 ? 0 : 1)
                    .thenComparingInt(id -> id.startsWith("minecraft:") ? 0 : 1)
                    .thenComparing(id -> id))
                .limit(16)
                .toList();
            for (String candidateId : candidateIds) {
                Resolution candidate = resolve(
                    mc,
                    candidateId,
                    operations,
                    branch.available,
                    recipeIndexes,
                    visiting,
                    budget
                );
                bestIngredient = better(bestIngredient, candidate);
            }
            if (bestIngredient == null || !bestIngredient.valid) return invalid();
            branch = mergeCommitted(branch, bestIngredient);
        }

        List<CraftStep> steps = new ArrayList<>(branch.steps);
        steps.add(new CraftStep(
            option.mode,
            resultId,
            operations,
            option.stationBlockId,
            option.needsCraftingTable,
            option.recipe
        ));
        Map<String, Integer> after = new HashMap<>(branch.available);
        int leftover = operations * outputCount - remaining;
        if (leftover > 0) after.merge(resultId, leftover, Integer::sum);
        return success(after, steps, branch.missingRaw, branch.cost, branch.tieBreak + ">" + targetItemId);
    }

    private static Resolution mergeCommitted(Resolution base, Resolution child) {
        List<CraftStep> steps = new ArrayList<>(base.steps);
        steps.addAll(child.steps);
        LinkedHashMap<String, Integer> missing = new LinkedHashMap<>(base.missingRaw);
        child.missingRaw.forEach((item, count) -> missing.merge(item, count, Integer::sum));
        return success(
            new HashMap<>(child.available),
            steps,
            missing,
            base.cost + child.cost,
            base.tieBreak + ">" + child.tieBreak
        );
    }

    private static Resolution better(Resolution current, Resolution candidate) {
        if (candidate == null || !candidate.valid) return current;
        if (current == null || !current.valid) return candidate;
        int byCost = Long.compare(candidate.cost, current.cost);
        if (byCost != 0) return byCost < 0 ? candidate : current;
        int byMissingKinds = Integer.compare(candidate.missingRaw.size(), current.missingRaw.size());
        if (byMissingKinds != 0) return byMissingKinds < 0 ? candidate : current;
        int bySteps = Integer.compare(candidate.steps.size(), current.steps.size());
        if (bySteps != 0) return bySteps < 0 ? candidate : current;
        return candidate.tieBreak.compareTo(current.tieBreak) < 0 ? candidate : current;
    }

    private static Resolution success(
        Map<String, Integer> available,
        List<CraftStep> steps,
        LinkedHashMap<String, Integer> missing,
        long cost,
        String tieBreak
    ) {
        return new Resolution(true, available, new ArrayList<>(steps), new LinkedHashMap<>(missing), cost, tieBreak);
    }

    private static Resolution invalid() {
        return new Resolution(false, Map.of(), List.of(), new LinkedHashMap<>(), Long.MAX_VALUE, "");
    }

    private static int acquisitionCost(String itemId) {
        if (PoiMemory.getKnownStorageItemCount(itemId) > 0) return 0;
        String canonical = canonicalVisitKey(itemId);
        if (!canonical.startsWith("minecraft:")) return 30;
        String path = canonical.substring("minecraft:".length());
        if (path.endsWith("_log") || path.endsWith("_stem") || path.endsWith("_hyphae")
            || path.startsWith("raw_") || path.endsWith("_ore") || path.equals("coal") || path.equals("charcoal")) {
            return 1;
        }
        if (path.endsWith("_planks") || path.endsWith("_stick") || path.endsWith("_nugget")
            || path.endsWith("_ingot") || path.endsWith("_block")) {
            return 50;
        }
        if (path.endsWith("_pickaxe") || path.endsWith("_axe") || path.endsWith("_shovel")
            || path.endsWith("_hoe") || path.endsWith("_sword")) {
            return 200;
        }
        return 10;
    }

    private static int operationCost(String mode) {
        return switch (mode) {
            case "craft" -> 1;
            case "smelt" -> 2;
            case "blast", "smoke" -> 3;
            default -> 4;
        };
    }

    private static Map<String, Integer> snapshotInventory(Minecraft mc) {
        Map<String, Integer> available = new HashMap<>();
        for (int i = 0; i < mc.player.getInventory().getContainerSize(); i++) {
            ItemStack stack = mc.player.getInventory().getItem(i);
            if (stack.isEmpty()) continue;
            String id = BuiltInRegistries.ITEM.getKey(stack.getItem()).toString();
            available.merge(id, stack.getCount(), Integer::sum);
        }
        return available;
    }

    private static RecipeIndexes buildRecipeIndexes(Minecraft mc) {
        RecipeIndexes indexes = new RecipeIndexes();
        for (RecipeHolder<?> recipe : mc.level.getRecipeManager().getRecipes()) {
            RecipeOption option = toRecipeOption(recipe);
            if (option == null) continue;
            ItemStack result = recipe.value().getResultItem(mc.level.registryAccess());
            if (result.isEmpty()) continue;
            String id = BuiltInRegistries.ITEM.getKey(result.getItem()).toString();
            indexes.byOutput.computeIfAbsent(id, ignored -> new ArrayList<>()).add(option);
        }
        indexes.byOutput.values().forEach(options -> options.sort(Comparator
            .comparingInt((RecipeOption option) -> option.recipe.id().getNamespace().equals("minecraft") ? 0 : 1)
            .thenComparing(option -> option.recipe.id().toString())));
        return indexes;
    }

    private static RecipeOption toRecipeOption(RecipeHolder<?> recipe) {
        if (recipe.value().getType() == RecipeType.CRAFTING) {
            return new RecipeOption(
                recipe,
                "craft",
                "minecraft:crafting_table",
                !recipe.value().canCraftInDimensions(2, 2)
            );
        }
        if (recipe.value().getType() == RecipeType.SMELTING) {
            return new RecipeOption(recipe, "smelt", "minecraft:furnace", false);
        }
        if (recipe.value().getType() == RecipeType.BLASTING) {
            return new RecipeOption(recipe, "blast", "minecraft:blast_furnace", false);
        }
        if (recipe.value().getType() == RecipeType.SMOKING) {
            return new RecipeOption(recipe, "smoke", "minecraft:smoker", false);
        }
        return null;
    }

    private static List<RecipeOption> findRecipeOptions(RecipeIndexes indexes, String targetItemId) {
        List<RecipeOption> exact = indexes.byOutput.get(targetItemId);
        if (exact != null) return exact;
        for (Map.Entry<String, List<RecipeOption>> entry : indexes.byOutput.entrySet()) {
            if (matchesRegistryId(entry.getKey(), targetItemId)) return entry.getValue();
        }
        return List.of();
    }

    private static int consumeAvailable(Map<String, Integer> available, String targetItemId, int neededCount) {
        int remaining = neededCount;
        for (String key : new ArrayList<>(available.keySet())) {
            if (!matchesRegistryId(key, targetItemId) || remaining <= 0) continue;
            int have = available.getOrDefault(key, 0);
            int take = Math.min(have, remaining);
            if (have > take) available.put(key, have - take);
            else available.remove(key);
            remaining -= take;
        }
        return neededCount - remaining;
    }

    private static String canonicalVisitKey(String itemId) {
        return itemId.contains(":") ? itemId : "minecraft:" + itemId;
    }

    public static boolean matchesRegistryId(String actualId, String targetId) {
        if (actualId == null || targetId == null) return false;
        return canonicalVisitKey(actualId).equals(canonicalVisitKey(targetId));
    }

    public static boolean matchesItemId(String actualId, String targetId) {
        if (matchesRegistryId(actualId, targetId)) return true;
        if (actualId == null || targetId == null || !targetId.startsWith("#")) return false;
        ResourceLocation actualLocation = ResourceLocation.tryParse(actualId);
        if (actualLocation == null) return false;
        Item item = BuiltInRegistries.ITEM.get(actualLocation);
        if (targetId.equals("#lcu:wood")) {
            return item.builtInRegistryHolder().is(ItemTags.LOGS)
                || item.builtInRegistryHolder().is(ItemTags.PLANKS);
        }
        ResourceLocation tagLocation = ResourceLocation.tryParse(targetId.substring(1));
        if (tagLocation == null) return false;
        return item.builtInRegistryHolder().is(TagKey.create(Registries.ITEM, tagLocation));
    }
}
