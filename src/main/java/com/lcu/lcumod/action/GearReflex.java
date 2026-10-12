package com.lcu.lcumod.action;

import com.lcu.lcumod.LCUMod;
import net.minecraft.client.Minecraft;

/**
 * P0 装备反射：**别人丢过来的盔甲，自己穿上**（用户实机："丢装备给它不会自动穿"）。
 *
 * <p>形态照抄 {@code ActionExecutor.tickHungerReflex}（同一个 tick 点、同样的门）：
 * 冷却 + 可被后端 {@code reflex_override} 关掉 + **只在 AI 持控制权时动手**。
 *
 * <p>为什么复用 {@link ContainerController#equipArmor}：那是唯一一条"穿上"的路径
 * （问原版 {@code Slot#mayPlace} 判断部位、穿完**交叉核对**装备槽、失败给机器可读原因），
 * 反射里再写一套 = 两条路径迟早不一致。这里只做"什么时候值得问一次"。
 *
 * <p>边界（诚实写清，别让人以为它什么都管）：
 * <ul>
 *   <li>只处理**空着的装备槽**（= 丢过来的新装备），**不抢**已经穿在身上的：换更好的那件是
 *       模型该做的决定（它看得到 {@code armor} 与 {@code inventory}，有 {@code equip_armor} 工具）；</li>
 *   <li>不动手上的武器：{@code equipBestWeapon} 每 40 tick 自动跑一遍会把模型特意拿在手里的
 *       方块/食物换成武器（放方块、吃东西就废了），所以武器只由模型与出手路径自己切；</li>
 *   <li>玩家控制时不跑（别动人家的装备）。</li>
 * </ul>
 */
public final class GearReflex {

    /** 检查间隔：2 秒（穿脱都是包操作，没必要每 tick 问一次）。 */
    private static final int GEAR_REFLEX_COOLDOWN_TICKS = 40;

    /** 反射名（后端可用 {@code reflex_override {name:"gear"}} 临时关掉）。 */
    public static final String GATE = "gear";

    private static int cooldown = 0;

    private GearReflex() {
    }

    /** 每客户端 tick 调一次（与进食反射同一处）。 */
    public static void tick(Minecraft mc) {
        if (cooldown > 0) {
            cooldown--;
            return;
        }
        cooldown = GEAR_REFLEX_COOLDOWN_TICKS;

        if (mc == null || mc.player == null || mc.level == null || mc.gameMode == null) {
            return;
        }
        if (!InputIsolation.isAiControlled()) {
            // 玩家控制：人家的装备人家自己穿。
            return;
        }
        if (ReflexGate.isSuppressed(GATE)) {
            return;
        }

        // 战斗中也照穿：空槽补上装备只会变强、且是一步包操作；这里刻意**不像进食反射那样让路**。
        ContainerController.Result result = ContainerController.equipArmor(mc);
        if (result == null || !result.ok() || !ContainerPlan.OK.equals(result.reason())) {
            // already_equipped / no_armor / 门没过：都是"没什么可穿的"，安静返回。
            return;
        }
        String item = result.data() != null && result.data().has("item")
                ? result.data().get("item").getAsString() : "?";
        String part = result.data() != null && result.data().has("part")
                ? result.data().get("part").getAsString() : "?";
        LCUMod.LOGGER.info("[Gear] 自己把丢过来的装备穿上了: {} -> {}", item, part);
        // 状态刷新由 equipArmor 成功路径发起；这里不重复刷。
    }

    /** 重连/测试用：清掉冷却。 */
    public static void reset() {
        cooldown = 0;
    }
}
