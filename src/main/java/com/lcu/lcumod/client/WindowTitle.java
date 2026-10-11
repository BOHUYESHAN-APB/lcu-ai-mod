package com.lcu.lcumod.client;

import com.lcu.lcumod.LCUMod;
import com.lcu.lcumod.action.ActionExecutor;
import com.lcu.lcumod.action.CombatReflex;
import com.lcu.lcumod.action.InputIsolation;
import com.lcu.lcumod.action.JavaAutonomousBehavior;
import com.lcu.lcumod.action.MovementSystem;
import com.lcu.lcumod.action.Pathfinder;
import net.minecraft.SharedConstants;
import net.minecraft.client.Minecraft;
import net.minecraft.client.multiplayer.ServerData;
import net.neoforged.fml.ModList;

/**
 * 把「谁在操控 / 在干什么 / 哪个实例 / 在哪 / 什么版本」写进游戏窗口标题。
 *
 * 只在标题**真的变化**时才调用 {@code Window.setTitle}（字符串比较，代价可忽略），
 * 所以每 tick 调用是安全的；版本信息只解析一次并缓存。
 *
 * 格式化逻辑在同包的 {@link TitleFormat}（纯 Java，可离线自检）。
 */
public final class WindowTitle {

    private static String lastApplied = "";
    private static String mcVersion;
    private static String loaderVersion;

    private WindowTitle() {
    }

    /** 每客户端 tick 调用（由 ActionExecutor.onTick 驱动）。 */
    public static void tick(Minecraft mc) {
        if (mc == null || mc.getWindow() == null) {
            return;
        }
        resolveVersions();

        String title = TitleFormat.build(
            InputIsolation.isAiControlled() ? TitleFormat.AI_CONTROL : TitleFormat.USER_CONTROL,
            currentActivity(),
            instanceName(mc),
            currentWhere(mc),
            mcVersion,
            loaderVersion
        );

        if (!title.equals(lastApplied)) {
            mc.getWindow().setTitle(title);
            lastApplied = title;
            LCUMod.LOGGER.debug("[Title] {}", title);
        }
    }

    /** 当前活动（趣味词）。HUD 状态框与窗口标题**共用这一套说法**，避免两处不一致。 */
    public static String activity(Minecraft mc) {
        if (mc == null || mc.player == null) {
            return ActivityPhrase.IDLE;
        }
        return ActivityPhrase.of(
                mc.player.isOnFire() || mc.player.isInLava(),
                mc.player.isUnderWater(),
                CombatReflex.isEngaged(),
                ActionExecutor.isSleepingPending() || mc.player.isSleeping(),
                ActionExecutor.getActiveTaskKind(),
                Pathfinder.isNavigating(),
                MovementSystem.isMoving());
    }

    /** 兼容旧调用点。 */
    private static String currentActivity() {
        return activity(Minecraft.getInstance());
    }

    /** 实例/整合包目录名，例如 {@code Rapid Optimization-ai}。 */
    private static String instanceName(Minecraft mc) {
        return mc.gameDirectory == null ? "" : mc.gameDirectory.getName();
    }

    /** 在哪：服务器 > 单人世界 > 主菜单。 */
    private static String currentWhere(Minecraft mc) {
        ServerData server = mc.getCurrentServer();
        if (server != null) {
            String name = server.name == null ? "" : server.name.strip();
            String ip = server.ip == null ? "" : server.ip.strip();
            String label = TitleFormat.join(" ", name.isEmpty() ? "" : name,
                (ip.isEmpty() || ip.equals(name)) ? "" : "(" + ip + ")");
            return label.isEmpty() ? "服务器" : "服务器 " + label;
        }
        if (mc.hasSingleplayerServer() && mc.getSingleplayerServer() != null) {
            String level = mc.getSingleplayerServer().getWorldData().getLevelName();
            return level == null || level.isBlank() ? "单人世界" : "单人 " + level.strip();
        }
        return "主菜单";
    }

    /** MC / NeoForge 版本只解析一次（运行期不变）。 */
    private static void resolveVersions() {
        if (mcVersion != null) {
            return;
        }
        try {
            mcVersion = "MC " + SharedConstants.getCurrentVersion().name();
        } catch (Throwable t) {
            mcVersion = "";
        }
        try {
            loaderVersion = ModList.get().getModContainerById("neoforge")
                .map(container -> "NeoForge " + container.getModInfo().getVersion())
                .orElse("");
        } catch (Throwable t) {
            loaderVersion = "";
        }
    }
}
