package com.lcu.lcumod.action;

import java.util.EnumMap;
import java.util.Map;

/** Arbitrates all body effect channels for one client tick. */
public final class BodyKernel {
    private record Claim(String owner, BodyPriority priority) {}

    private static final Map<BodyChannel, Claim> CLAIMS = new EnumMap<>(BodyChannel.class);

    private BodyKernel() {}

    public static synchronized void beginTick() {
        CLAIMS.clear();
    }

    public static synchronized boolean claim(String owner, BodyPriority priority, BodyChannel... channels) {
        if (owner == null || owner.isBlank() || priority == null || channels == null || channels.length == 0) {
            return false;
        }
        for (BodyChannel channel : channels) {
            Claim current = CLAIMS.get(channel);
            if (current != null && !current.owner().equals(owner)
                    && current.priority().value() >= priority.value()) {
                return false;
            }
        }
        Claim next = new Claim(owner, priority);
        for (BodyChannel channel : channels) CLAIMS.put(channel, next);
        return true;
    }

    public static synchronized boolean owns(String owner, BodyChannel channel) {
        Claim claim = CLAIMS.get(channel);
        return claim != null && claim.owner().equals(owner);
    }

    public static synchronized void release(String owner) {
        if (owner == null) return;
        CLAIMS.entrySet().removeIf(entry -> entry.getValue().owner().equals(owner));
    }

    static synchronized String ownerOf(BodyChannel channel) {
        Claim claim = CLAIMS.get(channel);
        return claim == null ? null : claim.owner();
    }
}
