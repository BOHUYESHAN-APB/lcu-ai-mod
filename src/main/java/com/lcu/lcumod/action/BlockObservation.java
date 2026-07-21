package com.lcu.lcumod.action;

import com.google.gson.JsonObject;
import com.google.gson.JsonArray;
import net.minecraft.client.Minecraft;
import net.minecraft.core.BlockPos;
import net.minecraft.core.registries.BuiltInRegistries;
import net.minecraft.world.level.block.state.BlockState;
import net.minecraft.world.level.block.state.properties.Property;

import java.util.ArrayList;
import java.util.Comparator;
import java.util.List;

/** Structured, read-only observation of one loaded block. */
final class BlockObservation {
    private static final double MAX_INSPECT_DISTANCE = 64.0;

    private BlockObservation() {}

    static JsonObject inspect(Minecraft mc, BlockPos pos) {
        if (mc.player == null || mc.level == null) {
            throw new IllegalStateException("No loaded player world");
        }
        if (!mc.level.hasChunkAt(pos)) {
            throw new IllegalArgumentException("Target block is not in a loaded chunk");
        }
        double distance = mc.player.position().distanceTo(net.minecraft.world.phys.Vec3.atCenterOf(pos));
        if (distance > MAX_INSPECT_DISTANCE) {
            throw new IllegalArgumentException("Target block is farther than 64 blocks");
        }

        BlockState state = mc.level.getBlockState(pos);
        String blockId = BuiltInRegistries.BLOCK.getKey(state.getBlock()).toString();
        JsonObject result = new JsonObject();
        result.addProperty("x", pos.getX());
        result.addProperty("y", pos.getY());
        result.addProperty("z", pos.getZ());
        result.addProperty("block_id", blockId);
        result.addProperty("dimension", mc.level.dimension().location().toString());
        result.addProperty("target_token", BlockSnapshotToken.create(mc.level, pos, state));
        result.addProperty("item_id", BuiltInRegistries.ITEM.getKey(state.getBlock().asItem()).toString());
        result.addProperty("distance", Math.round(distance * 100.0) / 100.0);
        result.addProperty("air", state.isAir());
        result.addProperty("player_tick", mc.player.tickCount);
        result.addProperty("game_time", mc.level.getGameTime());

        JsonObject properties = new JsonObject();
        Integer age = null;
        for (Property<?> property : state.getProperties()) {
            String value = propertyValueName(state, property);
            properties.addProperty(property.getName(), value);
            if ("age".equals(property.getName())) {
                try {
                    age = Integer.parseInt(value);
                } catch (NumberFormatException ignored) {
                    age = null;
                }
            }
        }
        result.add("properties", properties);

        CropPolicy.CropState crop = CropPolicy.inspect(blockId, age);
        JsonObject cropData = new JsonObject();
        cropData.addProperty("supported", crop.supported());
        cropData.addProperty("mature", crop.mature());
        if (crop.age() != null) cropData.addProperty("age", crop.age());
        if (crop.supported()) {
            cropData.addProperty("max_age", crop.maxAge());
            cropData.addProperty("seed_item_id", crop.seedItemId());
        }
        result.add("crop", cropData);
        return result;
    }

    static JsonObject scanCrops(Minecraft mc, int radius) {
        if (mc.player == null || mc.level == null) {
            throw new IllegalStateException("No loaded player world");
        }
        if (radius < 1 || radius > 16) {
            throw new IllegalArgumentException("Crop scan radius must be between 1 and 16");
        }

        BlockPos origin = mc.player.blockPosition();
        List<JsonObject> crops = new ArrayList<>();
        for (int dx = -radius; dx <= radius; dx++) {
            for (int dy = -4; dy <= 4; dy++) {
                for (int dz = -radius; dz <= radius; dz++) {
                    BlockPos pos = origin.offset(dx, dy, dz);
                    if (!mc.level.hasChunkAt(pos)) continue;
                    BlockState state = mc.level.getBlockState(pos);
                    String blockId = BuiltInRegistries.BLOCK.getKey(state.getBlock()).toString();
                    if (!CropPolicy.isSupported(blockId)) continue;
                    crops.add(inspect(mc, pos));
                }
            }
        }
        crops.sort(Comparator.comparingDouble(crop -> crop.get("distance").getAsDouble()));
        JsonArray items = new JsonArray();
        for (int i = 0; i < Math.min(256, crops.size()); i++) items.add(crops.get(i));

        JsonObject result = new JsonObject();
        result.addProperty("radius", radius);
        result.addProperty("count", items.size());
        result.addProperty("truncated", crops.size() > items.size());
        result.addProperty("player_tick", mc.player.tickCount);
        result.addProperty("game_time", mc.level.getGameTime());
        result.add("crops", items);
        return result;
    }

    private static <T extends Comparable<T>> String propertyValueName(BlockState state, Property<T> property) {
        return property.getName(state.getValue(property));
    }
}
