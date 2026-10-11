package com.lcu.lcumod.network;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;

/**
 * Wire-protocol token check — <b>pure logic, zero dependencies</b>.
 *
 * <p>Deliberately kept out of {@link WireServer}: that class drags in Gson and the mod's logger,
 * so the zero-dependency self-checks (see {@code ProtoCheck}) cannot even load it. This class
 * only uses the JDK, so the token rule can be tested directly.
 *
 * <p>Blank configured token ⇒ accept anything: that is what keeps existing loopback setups
 * working untouched (the mod's {@code wireToken} defaults to empty). Otherwise the comparison is
 * constant-time so a wrong token cannot be narrowed down by measuring the reply.
 */
public final class WireAuth {

    private WireAuth() {}

    public static boolean matches(String configured, String offered) {
        String want = configured == null ? "" : configured.trim();
        if (want.isEmpty()) return true;
        String got = offered == null ? "" : offered.trim();
        return MessageDigest.isEqual(
                want.getBytes(StandardCharsets.UTF_8), got.getBytes(StandardCharsets.UTF_8));
    }
}
