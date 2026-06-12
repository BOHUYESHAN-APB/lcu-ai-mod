package com.lcu.lcumod.action;

import net.minecraft.client.Minecraft;
import net.minecraft.core.registries.BuiltInRegistries;
import net.minecraft.world.item.ItemStack;
import net.minecraft.world.item.crafting.Ingredient;
import net.minecraft.world.item.crafting.RecipeHolder;
import net.minecraft.world.item.crafting.RecipeType;

import java.util.ArrayList;
import java.util.HashMap;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;

/**
 * Generic recursive crafting dependency planner.
 *
 * Produces a post-order craft step list (leaf craft first, root craft last)
 * plus any raw resource gaps that cannot be satisfied from current inventory
 * or sub-recipes.
 */
public final class CraftingPlanner {

    private CraftingPlanner() {
    }

    public static final class CraftStep {
        public final String itemId;
        public final int craftOperations;
        public final boolean needsCraftingTable;
        public final RecipeHolder<?> recipe;

        public CraftStep(String itemId, int craftOperations, boolean needsCraftingTable, RecipeHolder<?> recipe) {
            this.itemId = itemId;
            this.craftOperations = craftOperations;
            this.needsCraftingTable = needsCraftingTable;
            this.recipe = recipe;
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
    }

    public static CraftPlan plan(Minecraft mc, String targetItemId, int requestedCount) {
        CraftPlan plan = new CraftPlan(targetItemId, requestedCount);
        if (mc == null || mc.player == null || mc.level == null || requestedCount <= 0) {
            plan.success = true;
            return plan;
        }

        Map<String, Integer> available = snapshotInventory(mc);
        Map<String, RecipeHolder<?>> recipeIndex = buildRecipeIndex(mc);
        Set<String> visiting = new HashSet<>();
        boolean resolved = resolve(mc, targetItemId, requestedCount, available, recipeIndex, visiting, plan);
        plan.success = resolved && plan.missingRaw.isEmpty();
        if (!plan.success && plan.failureReason.isBlank() && !plan.missingRaw.isEmpty()) {
            plan.failureReason = "missing raw resources";
        }
        return plan;
    }

    private static boolean resolve(
        Minecraft mc,
        String targetItemId,
        int neededCount,
        Map<String, Integer> available,
        Map<String, RecipeHolder<?>> recipeIndex,
        Set<String> visiting,
        CraftPlan plan
    ) {
        int consumed = consumeAvailable(available, targetItemId, neededCount);
        int remaining = neededCount - consumed;
        if (remaining <= 0) {
            return true;
        }

        String visitKey = canonicalVisitKey(targetItemId);
        if (!visiting.add(visitKey)) {
            plan.failureReason = "recipe cycle detected for " + targetItemId;
            plan.missingRaw.merge(targetItemId, remaining, Integer::sum);
            return false;
        }

        RecipeHolder<?> recipe = findRecipe(recipeIndex, targetItemId);
        if (recipe == null || recipe.value().getType() != RecipeType.CRAFTING) {
            visiting.remove(visitKey);
            plan.missingRaw.merge(targetItemId, remaining, Integer::sum);
            return false;
        }

        ItemStack result = recipe.value().getResultItem(mc.level.registryAccess());
        if (result.isEmpty()) {
            visiting.remove(visitKey);
            plan.failureReason = "recipe has empty result for " + targetItemId;
            plan.missingRaw.merge(targetItemId, remaining, Integer::sum);
            return false;
        }

        String resolvedResultId = BuiltInRegistries.ITEM.getKey(result.getItem()).toString();
        int outputCount = Math.max(1, result.getCount());
        int craftOperations = (remaining + outputCount - 1) / outputCount;

        boolean dependenciesResolved = true;
        for (Ingredient ingredient : recipe.value().getIngredients()) {
            if (ingredient == null || ingredient.isEmpty()) {
                continue;
            }

            String candidateId = chooseIngredientCandidate(ingredient, available, recipeIndex);
            if (candidateId == null || candidateId.isBlank()) {
                plan.failureReason = "recipe ingredient unresolved for " + targetItemId;
                dependenciesResolved = false;
                continue;
            }

            if (!resolve(mc, candidateId, craftOperations, available, recipeIndex, visiting, plan)) {
                dependenciesResolved = false;
            }
        }

        visiting.remove(visitKey);
        if (!dependenciesResolved) {
            return false;
        }

        plan.steps.add(new CraftStep(
            resolvedResultId,
            craftOperations,
            !recipe.value().canCraftInDimensions(2, 2),
            recipe
        ));

        int produced = craftOperations * outputCount;
        int leftover = produced - remaining;
        if (leftover > 0) {
            available.merge(resolvedResultId, leftover, Integer::sum);
        }
        return true;
    }

    private static Map<String, Integer> snapshotInventory(Minecraft mc) {
        Map<String, Integer> available = new HashMap<>();
        for (int i = 0; i < mc.player.getInventory().getContainerSize(); i++) {
            ItemStack stack = mc.player.getInventory().getItem(i);
            if (stack.isEmpty()) {
                continue;
            }
            String id = BuiltInRegistries.ITEM.getKey(stack.getItem()).toString();
            available.merge(id, stack.getCount(), Integer::sum);
        }
        return available;
    }

    private static Map<String, RecipeHolder<?>> buildRecipeIndex(Minecraft mc) {
        Map<String, RecipeHolder<?>> index = new HashMap<>();
        for (RecipeHolder<?> recipe : mc.level.getRecipeManager().getRecipes()) {
            if (recipe.value().getType() != RecipeType.CRAFTING) {
                continue;
            }
            ItemStack result = recipe.value().getResultItem(mc.level.registryAccess());
            if (result.isEmpty()) {
                continue;
            }
            String id = BuiltInRegistries.ITEM.getKey(result.getItem()).toString();
            index.putIfAbsent(id, recipe);
        }
        return index;
    }

    private static RecipeHolder<?> findRecipe(Map<String, RecipeHolder<?>> recipeIndex, String targetItemId) {
        if (recipeIndex.containsKey(targetItemId)) {
            return recipeIndex.get(targetItemId);
        }
        for (Map.Entry<String, RecipeHolder<?>> entry : recipeIndex.entrySet()) {
            if (matchesRegistryId(entry.getKey(), targetItemId)) {
                return entry.getValue();
            }
        }
        return null;
    }

    private static String chooseIngredientCandidate(
        Ingredient ingredient,
        Map<String, Integer> available,
        Map<String, RecipeHolder<?>> recipeIndex
    ) {
        String bestCandidate = null;
        int bestScore = Integer.MIN_VALUE;
        for (ItemStack stack : ingredient.getItems()) {
            if (stack.isEmpty()) {
                continue;
            }
            String candidateId = BuiltInRegistries.ITEM.getKey(stack.getItem()).toString();
            int score = Math.min(available.getOrDefault(candidateId, 0), 64) * 100;
            if (recipeIndex.containsKey(candidateId)) {
                score += 20;
            }
            if (bestCandidate == null || score > bestScore) {
                bestCandidate = candidateId;
                bestScore = score;
            }
        }
        return bestCandidate;
    }

    private static int consumeAvailable(Map<String, Integer> available, String targetItemId, int neededCount) {
        int remaining = neededCount;
        List<String> keys = new ArrayList<>(available.keySet());
        for (String key : keys) {
            if (!matchesRegistryId(key, targetItemId) || remaining <= 0) {
                continue;
            }
            int have = available.getOrDefault(key, 0);
            int take = Math.min(have, remaining);
            int leftover = have - take;
            if (leftover > 0) {
                available.put(key, leftover);
            } else {
                available.remove(key);
            }
            remaining -= take;
        }
        return neededCount - remaining;
    }

    private static String canonicalVisitKey(String itemId) {
        int idx = itemId.indexOf(':');
        return idx >= 0 ? itemId.substring(idx + 1) : itemId;
    }

    public static boolean matchesRegistryId(String actualId, String targetId) {
        if (actualId == null || targetId == null) {
            return false;
        }
        if (actualId.equals(targetId)) {
            return true;
        }
        String actualPath = canonicalVisitKey(actualId);
        String targetPath = canonicalVisitKey(targetId);
        return actualPath.equals(targetPath);
    }
}
