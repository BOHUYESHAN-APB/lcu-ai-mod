package com.lcu.lcumod.action;

import net.minecraft.core.BlockPos;
import net.minecraft.core.registries.BuiltInRegistries;
import net.minecraft.world.level.Level;
import net.minecraft.world.level.block.state.BlockState;
import net.minecraft.world.level.block.state.properties.Property;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.util.Comparator;

/** Stable identity for an observed block state in one dimension and position. */
final class BlockSnapshotToken {
    private BlockSnapshotToken() {}

    static String create(Level level, BlockPos pos, BlockState state) {
        StringBuilder source = new StringBuilder()
            .append(level.dimension().location()).append('|')
            .append(pos.getX()).append(',').append(pos.getY()).append(',').append(pos.getZ()).append('|')
            .append(BuiltInRegistries.BLOCK.getKey(state.getBlock()));
        state.getProperties().stream()
            .sorted(Comparator.comparing(Property::getName))
            .forEach(property -> source.append('|').append(property.getName()).append('=')
                .append(propertyValueName(state, property)));
        try {
            byte[] digest = MessageDigest.getInstance("SHA-256")
                .digest(source.toString().getBytes(StandardCharsets.UTF_8));
            return java.util.HexFormat.of().formatHex(digest);
        } catch (NoSuchAlgorithmException exception) {
            throw new IllegalStateException("SHA-256 is unavailable", exception);
        }
    }

    private static <T extends Comparable<T>> String propertyValueName(BlockState state, Property<T> property) {
        return property.getName(state.getValue(property));
    }
}
