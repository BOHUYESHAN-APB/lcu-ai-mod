package com.lcu.lcumod;

import com.lcu.lcumod.config.RuntimeRole;
import com.lcu.lcumod.config.ServerConfig;
import net.neoforged.api.distmarker.Dist;
import net.neoforged.bus.api.SubscribeEvent;
import net.neoforged.fml.ModContainer;
import net.neoforged.fml.common.EventBusSubscriber;
import net.neoforged.fml.common.Mod;
import net.neoforged.neoforge.event.server.ServerAboutToStartEvent;

@Mod(value = LCUMod.MODID, dist = Dist.DEDICATED_SERVER)
@EventBusSubscriber(modid = LCUMod.MODID, value = Dist.DEDICATED_SERVER)
public final class LCUModServer {
    public LCUModServer(ModContainer container) {}

    @SubscribeEvent
    public static void onServerAboutToStart(ServerAboutToStartEvent event) {
        if (!RuntimeRole.current().activatesServerFakePlayers()) {
            LCUMod.LOGGER.info("[LCUMod] Dedicated server loaded without fake-player body activation");
            return;
        }
        if (!ServerConfig.FAKE_PLAYER_ENABLED.get()) {
            LCUMod.LOGGER.warn("[LCUMod] server_fake_player role selected but fakePlayerEnabled=false");
            return;
        }
        LCUMod.LOGGER.error(
                "[LCUMod] Server fake-player body is configured for profile {} but its executor is not implemented yet; no wire listener was started",
                ServerConfig.FAKE_PLAYER_NAME.get()
        );
        throw new IllegalStateException(
                "server_fake_player is unavailable in this build; disable fakePlayerEnabled");
    }
}
