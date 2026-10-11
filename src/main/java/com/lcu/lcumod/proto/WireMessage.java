package com.lcu.lcumod.proto;

import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.Set;

/**
 * 前向兼容的消息信封（纯逻辑，可零依赖自检）。
 *
 * <p>线形状（与 {@code docs/dev/SERVER_SIDE.md} §2 一致）：
 * <pre>{@code
 * { "v": 1, "type": "<string>", "seq": 7, "ts": 1750000000000, "payload": { ... } }
 * }</pre>
 *
 * <h2>前向兼容铁律</h2>
 * <ul>
 *   <li><b>未知字段一律忽略</b> —— 老代码遇到新字段不许炸。</li>
 *   <li><b>未知 {@code type} 一律不报错</b> —— 归一为 {@link #UNKNOWN_TYPE}，
 *       交上层忽略；原始串仍留在 {@link #declaredType} 里，回写时原样带出。</li>
 *   <li><b>缺 {@code v} 按 0 处理</b>（可据此判断对端太老 / 非本协议）。</li>
 *   <li><b>坏 JSON 返回 {@code null} 而不是抛</b>；超过 {@link #MAX_MESSAGE_CHARS} 的
 *       消息同样按 {@code null} 处理（大小上限）。</li>
 * </ul>
 *
 * <p>这只是**传输信封**，不承载权限语义：服务端绝不信任客户端送来的字段。
 * 本批不实现任何握手 / 网络 / 通道。
 */
public final class WireMessage {

    /** {@code type} 未在 {@link #KNOWN_TYPES} 中时的归一取值。 */
    public static final String UNKNOWN_TYPE = "unknown";

    /** 本协议当前认识的 {@code type}（与 {@code WireServer} 现有类型保持同形）。 */
    public static final Set<String> KNOWN_TYPES = Set.of(
            "hello", "state", "event", "response", "progress", "request");

    /** 单条消息（字符数）上限：超过即视为不可用，序列化/解析都返回 {@code null}。 */
    public static final int MAX_MESSAGE_CHARS = 1 << 20; // 1 MiB

    /** 协议版本号，缺省 0。 */
    public final int v;
    /** 归一后的类型；未知类型 → {@link #UNKNOWN_TYPE}。 */
    public final String type;
    /** 线上原始类型串（未知类型时保留，便于回写与上层诊断）；缺失为 {@code null}。 */
    public final String declaredType;
    /** 序号，缺省 0。 */
    public final int seq;
    /** 时间戳（毫秒），缺省 0。 */
    public final long ts;
    /** 载荷；永不 {@code null}（缺失即空表，只读）。 */
    public final Map<String, Object> payload;

    private WireMessage(int v, String type, String declaredType, int seq, long ts, Map<String, Object> payload) {
        this.v = v;
        this.type = type;
        this.declaredType = declaredType;
        this.seq = seq;
        this.ts = ts;
        this.payload = payload == null
                ? Collections.emptyMap()
                : Collections.unmodifiableMap(new LinkedHashMap<>(payload));
    }

    /** 用当前协议版本组装一条消息（{@code ts} 取当前时间）。 */
    public static WireMessage of(String type, int seq, Map<String, Object> payload) {
        String t = type == null ? UNKNOWN_TYPE : type;
        String normalized = KNOWN_TYPES.contains(t) ? t : UNKNOWN_TYPE;
        return new WireMessage(Protocol.PROTOCOL_VERSION, normalized, t, seq, System.currentTimeMillis(), payload);
    }

    /** 是否是本协议当前不认识的类型（上层可据此忽略）。 */
    public boolean isUnknown() {
        return UNKNOWN_TYPE.equals(type);
    }

    /** 序列化；超过 {@link #MAX_MESSAGE_CHARS} 或含不可序列化值时返回 {@code null}（不抛）。 */
    public String toJson() {
        try {
            Map<String, Object> root = new LinkedHashMap<>();
            root.put("v", v);
            root.put("type", declaredType != null ? declaredType : type);
            root.put("seq", seq);
            root.put("ts", ts);
            root.put("payload", payload);
            String json = Json.stringify(root);
            if (json.length() > MAX_MESSAGE_CHARS) {
                return null;
            }
            return json;
        } catch (RuntimeException e) {
            return null;
        }
    }

    /**
     * 解析一行 JSON。坏 JSON / 非对象 / 超长 → {@code null}；未知字段忽略；缺 {@code v} 记 0。
     */
    public static WireMessage fromJson(String json) {
        if (json == null || json.isEmpty() || json.length() > MAX_MESSAGE_CHARS) {
            return null;
        }
        Object parsed;
        try {
            parsed = Json.parse(json);
        } catch (RuntimeException e) {
            return null;
        }
        if (!(parsed instanceof Map<?, ?> rawRoot)) {
            return null;
        }

        int v = intOf(rawRoot.get("v"));
        int seq = intOf(rawRoot.get("seq"));
        long ts = longOf(rawRoot.get("ts"));

        String declared = rawRoot.get("type") instanceof String s ? s : null;
        String normalized = declared != null && KNOWN_TYPES.contains(declared) ? declared : UNKNOWN_TYPE;

        Map<String, Object> payload = new LinkedHashMap<>();
        if (rawRoot.get("payload") instanceof Map<?, ?> p) {
            for (Map.Entry<?, ?> e : p.entrySet()) {
                if (e.getKey() instanceof String key) {
                    payload.put(key, e.getValue());
                }
            }
        }
        // 其余未知顶层字段一律忽略。
        return new WireMessage(v, normalized, declared, seq, ts, payload);
    }

    private static int intOf(Object o) {
        return o instanceof Number n ? n.intValue() : 0;
    }

    private static long longOf(Object o) {
        return o instanceof Number n ? n.longValue() : 0L;
    }

    @Override
    public String toString() {
        return "WireMessage{v=" + v + ", type=" + type + ", seq=" + seq + ", ts=" + ts + ", payload=" + payload + '}';
    }
}
