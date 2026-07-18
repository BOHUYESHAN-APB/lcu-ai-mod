package com.lcu.lcumod.network;

import com.google.gson.JsonArray;
import com.google.gson.JsonObject;

import java.util.List;

/** Machine-readable deterministic actuator contracts advertised during wire auth. */
public final class ToolCatalog {
    private ToolCatalog() {}

    public static JsonArray describe() {
        JsonArray tools = new JsonArray();
        tools.add(tool("move_to", "operation", true, "progress", List.of("body.move"),
            numbers("x", "y", "z"), List.of("x", "y", "z")));
        tools.add(tool("look_at", "immediate", false, "response", List.of("camera.move"),
            numbers("x", "y", "z"), List.of("x", "y", "z")));
        tools.add(tool("jump", "immediate", false, "response", List.of("body.move"), emptyProperties(), List.of()));
        tools.add(tool("attack", "immediate", false, "response", List.of("entity.attack"), emptyProperties(), List.of()));
        tools.add(tool("mine_block", "operation", true, "progress", List.of("body.move", "world.break"), emptyProperties(), List.of()));
        tools.add(tool("use_on", "immediate", false, "response", List.of("world.interact"), emptyProperties(), List.of()));
        tools.add(tool("send_chat", "immediate", false, "response", List.of("chat.send"),
            strings("message"), List.of("message")));
        tools.add(tool("stop_all", "immediate", false, "response", List.of("body.move", "inventory.ui"), emptyProperties(), List.of()));
        tools.add(tool("follow_player", "operation", true, "state", List.of("body.move"),
            strings("player"), List.of("player")));
        tools.add(tool("collect_blocks", "operation", true, "progress", List.of("body.move", "world.break", "inventory.produce"),
            stringAndCount("block_type"), List.of("block_type", "count")));
        tools.add(tool("craft_item", "operation", true, "progress", List.of("body.move", "inventory.ui", "inventory.consume", "inventory.produce", "world.interact"),
            stringAndCount("item"), List.of("item", "count")));
        tools.add(tool("eat", "operation", true, "progress", List.of("inventory.consume"), emptyProperties(), List.of()));
        tools.add(tool("get_inventory", "immediate", false, "response", List.of("inventory.read"), emptyProperties(), List.of()));
        tools.add(tool("get_container", "immediate", false, "response", List.of("inventory.read"), emptyProperties(), List.of()));
        tools.add(tool("take_item", "immediate", false, "response", List.of("inventory.transfer"),
            slotAndContainer(), List.of("container_id", "slot")));
        tools.add(tool("put_item", "immediate", false, "response", List.of("inventory.transfer"),
            slotAndContainer(), List.of("container_id", "slot")));
        tools.add(tool("close_container", "immediate", false, "response", List.of("inventory.ui"), emptyProperties(), List.of()));
        tools.add(tool("drop_item", "immediate", false, "response", List.of("inventory.drop"),
            stringAndCount("item"), List.of("item", "count")));
        return tools;
    }

    private static JsonObject tool(String command, String execution, boolean cancellable, String completion,
                                   List<String> effects, JsonObject properties, List<String> required) {
        JsonObject descriptor = new JsonObject();
        descriptor.addProperty("command", command);
        descriptor.addProperty("version", command.equals("craft_item") ? "1.1.0" : "1.0.0");
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
        return descriptor;
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
        for (String name : List.of("container_id", "slot")) {
            JsonObject value = new JsonObject();
            value.addProperty("type", "integer");
            value.addProperty("minimum", 0);
            properties.add(name, value);
        }
        return properties;
    }
}
