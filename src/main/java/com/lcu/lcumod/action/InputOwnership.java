package com.lcu.lcumod.action;

import java.util.LinkedHashMap;
import java.util.Map;

/**
 * AI 控制位的**所有权**：谁写的，谁才能清。
 *
 * <p>真机事故（用户："老问题不移动"）：`Pathfinder` 每 tick 把 `forward` 置 true，而 {@code CombatReflex}
 * **没在交战时也每 tick** 调 {@code releaseMovement()} 把它置 false —— 两者写的是同一个字段，
 * 执行顺序又是 Pathfinder 在前、CombatReflex 在后，于是"有路、navigating=true、却一步不动"。
 * 同类隐患还有 {@code SwimReflex} 收尾时清 `forward`（它可能刚被导航写过）。
 *
 * <p>所以把规则收进**一个零依赖类**，让"清掉别人的位移"在结构上做不到：
 * <ul>
 *   <li>写 {@code true} = 声明所有权（我是这个键当前的写入者）；</li>
 *   <li>写 {@code false} = **只有当前写入者才放行**；别人写的位移，一格都不许动。</li>
 * </ul>
 *
 * <p>零依赖（不引用 net.minecraft）⇒ `verifyWalk` 可以直接对它的行为做断言，而不是只做源码扫描。
 */
public final class InputOwnership {

    private static final Map<String, String> OWNER = new LinkedHashMap<>();

    private InputOwnership() {
    }

    /**
     * 这次写入该不该落到实处？
     *
     * @param writer 写入者名字（"navigation" / "combat" / "swim" / …）
     * @param key    控制位（"forward" / "back" / "jump" / "sprint" / …）
     * @param state  想写成的值
     * @return true = 允许写（写 true 会接管所有权；写 false 会把所有权交还）
     */
    public static boolean allows(String writer, String key, boolean state) {
        if (writer == null || key == null || writer.isBlank() || key.isBlank()) {
            return false;
        }
        if (state) {
            OWNER.put(key, writer);
            return true;
        }
        return writer.equals(OWNER.get(key)) && OWNER.remove(key) != null;
    }

    public static String ownerOf(String key) {
        return OWNER.get(key);
    }

    public static boolean owns(String writer, String key) {
        return writer != null && writer.equals(OWNER.get(key));
    }

    /** 用户接管 / 清 AI 控制 / 换世界时一并清掉（所有权不跨回合）。 */
    public static void clear() {
        OWNER.clear();
    }

    /** 排错与自检用的只读快照。 */
    public static Map<String, String> snapshot() {
        return new LinkedHashMap<>(OWNER);
    }
}
