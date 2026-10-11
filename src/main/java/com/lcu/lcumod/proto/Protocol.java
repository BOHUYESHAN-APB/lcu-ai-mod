package com.lcu.lcumod.proto;

import java.util.List;

/**
 * 线协议头常量（**与后端各持同一份定义**）。
 *
 * <p>这是双端同模组握手的"最小公共语言"：双方各报 {@link #PROTOCOL_VERSION} 与
 * {@link #CAPABILITIES}，版本兼容才启用富通道，否则静默退化到平台无关的最小通道
 * （聊天 / 命令，即今天的方式）——**任何一方缺失都不得失败**。
 *
 * <p>富通道只是**传输**：它让今天靠解析聊天 / 轮询 state 勉强做到的事变得直接，
 * 但**绝不承载权限语义**。服务端始终不信任客户端送来的字段，权限判定 / 区域保护 /
 * 物品归属一律服务端侧重新判定。
 *
 * <p>本批**只定常量**，握手 / 网络 / 通道实现留空（见 {@code docs/dev/SERVER_SIDE.md} §2、§4）。
 */
public final class Protocol {

    private Protocol() {
    }

    /** 协议版本。升版本 = 语义不兼容的改动；仅加字段/加能力位**不**升版本（前向兼容）。 */
    public static final int PROTOCOL_VERSION = 1;

    /**
     * 能力位表。每项都是"有它时能做什么"的开关，缺省（对端没报）＝按最小集理解。
     *
     * <ul>
     *   <li>{@code state_push} —— 服务端**主动推送** tick 级状态（精确位置 / 朝向 / 速度），
     *       上层不再轮询近似的 {@code state_update}。</li>
     *   <li>{@code structured_chat} —— 聊天以**结构化事件**下发（kind 由服务端给出），
     *       不再用翻译键猜 {@code chat_kind}。</li>
     *   <li>{@code container_view} —— 服务端**回传容器内容快照**，免去客户端开屏逐格读屏。</li>
     *   <li>{@code server_events} —— 服务端侧事件流（死亡 / 传送 / 维度切换等）直接下发，
     *       不必从聊天里嗅探。</li>
     *   <li>{@code entity_detail} —— 实体 / 方块**细节**（附魔 / 耐久 / NBT 级摘要），
     *       今天只能报名字与距离。</li>
     *   <li>{@code structured_request} —— 结构化**请求 / 响应**（如 TPA、{@code /locate}），
     *       服务端直接算，不再解析聊天文本或点聊天按钮。</li>
     * </ul>
     */
    public static final List<String> CAPABILITIES = List.of(
            "state_push",
            "structured_chat",
            "container_view",
            "server_events",
            "entity_detail",
            "structured_request");

    /** {@code profile} 取值：纯客户端。 */
    public static final String PROFILE_CLIENT = "client";
    /** {@code profile} 取值：纯服务端（假玩家所在侧）。 */
    public static final String PROFILE_SERVER = "server";
    /** {@code profile} 取值：两端都装了本模组（可同时是客户端与服务端）。 */
    public static final String PROFILE_BOTH = "both";
}
