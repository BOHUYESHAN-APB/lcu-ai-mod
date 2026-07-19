package com.lcu.lcumod.network;

import com.google.gson.JsonArray;
import com.google.gson.JsonObject;

import java.util.List;
import java.util.Map;

/** Machine-readable deterministic actuator contracts advertised during wire auth. */
public final class ToolCatalog {
    private ToolCatalog() {}

    public static JsonArray describe() {
        return describe(Map.of());
    }

    public static JsonArray describe(Map<String, Boolean> currentPolicy) {
        JsonArray tools = new JsonArray();
        tools.add(tool("move_to", "operation", true, "outcome", List.of("body.move"),
            numbers("x", "y", "z"), List.of("x", "y", "z")));
        tools.add(tool("look_at", "immediate", false, "response", List.of("camera.move"),
            numbers("x", "y", "z"), List.of("x", "y", "z")));
        tools.add(tool("look_at_entity", "immediate", false, "response", List.of("camera.move"),
            integerProperties("id"), List.of("id")));
        tools.add(tool("jump", "immediate", false, "response", List.of("body.move"), emptyProperties(), List.of()));
        tools.add(policyTool("attack", "immediate", false, "response", List.of("entity.attack"),
            emptyProperties(), List.of(), "allowAutomatedCombat"));
        tools.add(policyTool("attack_entity", "immediate", false, "response", List.of("entity.attack"),
            integerProperties("entity_id"), List.of("entity_id"), "allowAutomatedCombat"));
        tools.add(tool("mine_block", "operation", true, "outcome", List.of("body.move", "world.break"), emptyProperties(), List.of()));
        tools.add(tool("mine_block_at", "operation", true, "outcome", List.of("world.break"),
            blockTargetProperties(), List.of("x", "y", "z")));
        tools.add(tool("use_on", "immediate", false, "response", List.of("world.interact"), emptyProperties(), List.of()));
        tools.add(tool("use_item", "immediate", false, "response", List.of("inventory.use"), emptyProperties(), List.of()));
        tools.add(tool("use_on_entity", "immediate", false, "response", List.of("entity.interact"),
            integerProperties("id"), List.of("id")));
        tools.add(tool("interact_block_at", "immediate", false, "response", List.of("world.interact"),
            blockTargetProperties(), List.of("x", "y", "z")));
        tools.add(tool("equip_item", "immediate", false, "response", List.of("inventory.equip"),
            strings("item"), List.of("item")));
        tools.add(tool("send_chat", "immediate", false, "response", List.of("chat.send"),
            strings("message"), List.of("message")));
        tools.add(tool("stop_all", "immediate", false, "response", List.of("body.move", "inventory.ui"), emptyProperties(), List.of()));
        tools.add(tool("cancel_operation", "immediate", false, "response", List.of("operation.cancel"),
            strings("operation_id"), List.of("operation_id")));
        tools.add(tool("follow_player", "operation", true, "outcome", List.of("body.move"),
            strings("player"), List.of("player")));
        tools.add(tool("collect_blocks", "operation", true, "outcome", List.of("body.move", "world.break", "inventory.produce"),
            stringAndCount("block_type"), List.of("block_type", "count")));
        tools.add(tool("craft_item", "operation", true, "outcome", List.of("body.move", "inventory.ui", "inventory.consume", "inventory.produce", "world.interact"),
            stringAndCount("item"), List.of("item", "count")));
        tools.add(tool("eat", "operation", true, "outcome", List.of("inventory.consume"), emptyProperties(), List.of()));
        tools.add(tool("get_inventory", "immediate", false, "response", List.of("inventory.read"), emptyProperties(), List.of()));
        tools.add(tool("get_recipes", "immediate", false, "response", List.of("recipes.read"),
            strings("item"), List.of("item")));
        tools.add(tool("get_state", "immediate", false, "response", List.of("state.read"), emptyProperties(), List.of()));
        tools.add(tool("select_hotbar", "immediate", false, "response", List.of("inventory.equip"),
            integerProperties("index"), List.of("index")));
        tools.add(tool("get_container", "immediate", false, "response", List.of("inventory.read"), emptyProperties(), List.of()));
        tools.add(tool("inventory_click", "immediate", false, "response", List.of("inventory.transfer"),
            inventoryClickProperties(), List.of("container_id", "expected_state_id", "slot", "click_type")));
        tools.add(tool("container_button", "immediate", false, "response", List.of("inventory.ui"),
            integerProperties("container_id", "expected_state_id", "button_id"), List.of("container_id", "expected_state_id", "button_id")));
        tools.add(tool("place_recipe", "immediate", false, "response", List.of("inventory.ui", "inventory.transfer"),
            recipeProperties(), List.of("container_id", "expected_state_id", "recipe_id")));
        tools.add(tool("take_item", "immediate", false, "response", List.of("inventory.transfer"),
            slotAndContainer(), List.of("container_id", "expected_state_id", "slot")));
        tools.add(tool("put_item", "immediate", false, "response", List.of("inventory.transfer"),
            slotAndContainer(), List.of("container_id", "expected_state_id", "slot")));
        tools.add(tool("close_container", "immediate", false, "response", List.of("inventory.ui"), emptyProperties(), List.of()));
        if (currentPolicy != null && !currentPolicy.isEmpty()) {
            for (var element : tools) {
                JsonObject descriptor = element.getAsJsonObject();
                if (!descriptor.has("policy_options")) continue;
                JsonArray options = descriptor.getAsJsonArray("policy_options");
                boolean complete = options.asList().stream()
                    .allMatch(option -> currentPolicy.containsKey(option.getAsString()));
                if (!complete) continue;
                boolean enabled = options.asList().stream()
                    .allMatch(option -> Boolean.TRUE.equals(currentPolicy.get(option.getAsString())));
                descriptor.addProperty("available", enabled);
                descriptor.addProperty("policy_enabled", enabled);
            }
        }
        return tools;
    }

    private static JsonObject tool(String command, String execution, boolean cancellable, String completion,
                                   List<String> effects, JsonObject properties, List<String> required) {
        JsonObject descriptor = new JsonObject();
        descriptor.addProperty("command", command);
        descriptor.addProperty("version", versionFor(command));
        descriptor.addProperty("execution", execution);
        descriptor.addProperty("cancellable", cancellable);
        descriptor.addProperty("completion", completion);
        descriptor.addProperty("available", true);
        JsonArray effectArray = new JsonArray();
        effects.forEach(effectArray::add);
        descriptor.add("effects", effectArray);
        JsonObject schema = new JsonObject();
        schema.addProperty("type", "object");
        schema.add("properties", properties);
        JsonArray requiredArray = new JsonArray();
        required.forEach(requiredArray::add);
        schema.add("required", requiredArray);
        schema.addProperty("additionalProperties", false);
        descriptor.add("input_schema", schema);
        List<String> policyOptions = policyOptionsFor(command);
        if (!policyOptions.isEmpty()) {
            JsonArray options = new JsonArray();
            policyOptions.forEach(options::add);
            descriptor.add("policy_options", options);
            descriptor.addProperty("policy_default", policyDefaultsEnabled(command) ? "enabled" : "disabled");
        }
        return descriptor;
    }

    private static JsonObject policyTool(String command, String execution, boolean cancellable, String completion,
                                         List<String> effects, JsonObject properties, List<String> required,
                                         String policyOption) {
        JsonObject descriptor = tool(command, execution, cancellable, completion, effects, properties, required);
        descriptor.addProperty("policy_default", "disabled");
        descriptor.addProperty("policy_option", policyOption);
        return descriptor;
    }

    private static List<String> policyOptionsFor(String command) {
        return switch (command) {
            case "move_to", "jump", "follow_player" -> List.of("allowMovementAutomation");
            case "mine_block", "mine_block_at", "use_on", "use_on_entity", "interact_block_at" ->
                List.of("allowWorldAutomation");
            case "collect_blocks" -> List.of("allowMovementAutomation", "allowWorldAutomation");
            case "craft_item" -> List.of(
                "allowMovementAutomation", "allowWorldAutomation", "allowInventoryAutomation");
            case "use_item", "equip_item", "select_hotbar", "inventory_click", "container_button",
                 "place_recipe", "take_item", "put_item", "eat" -> List.of("allowInventoryAutomation");
            case "attack", "attack_entity" -> List.of("allowAutomatedCombat");
            case "send_chat" -> List.of("allowChatAutomation");
            default -> List.of();
        };
    }

    private static boolean policyDefaultsEnabled(String command) {
        return switch (command) {
            case "move_to", "jump", "follow_player", "send_chat" -> true;
            default -> false;
        };
    }

    private static String versionFor(String command) {
        return switch (command) {
            case "move_to", "mine_block", "follow_player", "collect_blocks", "craft_item", "eat",
                 "get_container", "inventory_click", "container_button", "place_recipe", "take_item", "put_item" -> "2.0.0";
            default -> "1.0.0";
        };
    }

    private static JsonObject emptyProperties() {
        return new JsonObject();
    }

    private static JsonObject numbers(String... names) {
        JsonObject properties = new JsonObject();
        for (String name : names) {
            JsonObject value = new JsonObject();
            value.addProperty("type", "number");
            properties.add(name, value);
        }
        return properties;
    }

    private static JsonObject strings(String... names) {
        JsonObject properties = new JsonObject();
        for (String name : names) {
            JsonObject value = new JsonObject();
            value.addProperty("type", "string");
            value.addProperty("minLength", 1);
            properties.add(name, value);
        }
        return properties;
    }

    private static JsonObject stringAndCount(String stringName) {
        JsonObject properties = strings(stringName);
        JsonObject count = new JsonObject();
        count.addProperty("type", "integer");
        count.addProperty("minimum", 1);
        count.addProperty("maximum", 2304);
        properties.add("count", count);
        return properties;
    }

    private static JsonObject slotAndContainer() {
        JsonObject properties = new JsonObject();
        for (String name : List.of("container_id", "expected_state_id", "slot")) {
            JsonObject value = new JsonObject();
            value.addProperty("type", "integer");
            value.addProperty("minimum", 0);
            properties.add(name, value);
        }
        return properties;
    }

    private static JsonObject integerProperties(String... names) {
        JsonObject properties = new JsonObject();
        for (String name : names) {
            JsonObject value = new JsonObject();
            value.addProperty("type", "integer");
            properties.add(name, value);
        }
        return properties;
    }

    private static JsonObject blockTargetProperties() {
        JsonObject properties = integerProperties("x", "y", "z");
        JsonObject face = new JsonObject();
        face.addProperty("type", "string");
        JsonArray values = new JsonArray();
        for (String value : List.of("down", "up", "north", "south", "west", "east")) values.add(value);
        face.add("enum", values);
        properties.add("face", face);
        return properties;
    }

    private static JsonObject inventoryClickProperties() {
        JsonObject properties = integerProperties("container_id", "expected_state_id", "slot", "button");
        JsonObject clickType = new JsonObject();
        clickType.addProperty("type", "string");
        properties.add("click_type", clickType);
        return properties;
    }

    private static JsonObject recipeProperties() {
        JsonObject properties = integerProperties("container_id", "expected_state_id");
        properties.add("recipe_id", strings("recipe_id").get("recipe_id"));
        JsonObject craftAll = new JsonObject();
        craftAll.addProperty("type", "boolean");
        properties.add("craft_all", craftAll);
        return properties;
    }
}
