package com.lcu.lcumod.proto;

import com.lcu.lcumod.network.WireAuth;
import java.lang.reflect.Method;
import java.lang.reflect.RecordComponent;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.regex.Pattern;

/**
 * 零依赖自检（接入 {@code check}）：本批"预留接口"四件事。
 *
 * <ol>
 *   <li><b>Body 抽象</b>：是接口；能力面方法齐全；签名里**不得**出现
 *       {@code net.minecraft} 类型（有则逐项报出）。</li>
 *   <li><b>WireMessage 前向兼容</b>：往返一致；未知字段忽略；未知 type 归一为
 *       {@code unknown} 且不报错；缺 {@code v} 记 0；坏 JSON 返回 {@code null}；
 *       超长 payload 上限生效。</li>
 *   <li><b>能力位</b>：非空、非空白、唯一、snake_case。</li>
 *   <li><b>版本常量稳定</b>：{@code PROTOCOL_VERSION} 字面值 + 能力位拼接串断言。</li>
 * </ol>
 *
 * <p>断言信息用 ASCII（Windows 控制台 GBK）；失败非零退出。
 */
public final class ProtoCheck {

    private static int passed = 0;
    private static int failed = 0;

    public static void main(String[] args) {
        bodyChecks();
        wireChecks();
        capabilityChecks();
        versionChecks();
        tokenChecks();

        System.out.println("[ProtoCheck] " + passed + " checks passed, " + failed + " failed");
        if (failed > 0) {
            System.exit(1);
        }
    }

    // ── 1. Body 抽象 ──────────────────────────────────────────────

    /** 能力面必须齐全的方法名（位置/朝向/潜行跳跑、按住与松开、背包与容器、看向/交互/攻击、聊天与指令）。 */
    private static final Set<String> REQUIRED_BODY_METHODS = Set.of(
            "profile",
            "x", "y", "z", "yaw", "pitch", "setRotation", "lookAt",
            "setSneaking", "setSprinting", "jump",
            "press", "release", "releaseAll",
            "inventory", "selectedHotbarSlot", "selectHotbarSlot",
            "openContainer", "clickSlot", "closeContainer",
            "useOn", "useItem", "attack",
            "sendChat", "runCommand");

    private static void bodyChecks() {
        check("body: is an interface", Body.class.isInterface());

        Set<String> names = new HashSet<>();
        List<String> leaks = new ArrayList<>();
        for (Method m : Body.class.getMethods()) {
            names.add(m.getName());
            collectLeak("method " + m.getName() + " return", m.getReturnType().getTypeName(), leaks);
            for (Class<?> p : m.getParameterTypes()) {
                collectLeak("method " + m.getName() + " param", p.getTypeName(), leaks);
            }
        }
        for (Class<?> nested : Body.class.getDeclaredClasses()) {
            if (nested.isRecord()) {
                for (RecordComponent rc : nested.getRecordComponents()) {
                    collectLeak("record " + nested.getSimpleName() + "." + rc.getName(),
                            rc.getType().getTypeName(), leaks);
                }
            }
        }

        List<String> missing = new ArrayList<>();
        for (String req : REQUIRED_BODY_METHODS) {
            if (!names.contains(req)) {
                missing.add(req);
            }
        }
        check("body: all capability methods present (missing=" + missing + ")", missing.isEmpty());

        // 报出来的泄露项要为空 —— 抽象层不许烙进 Minecraft 类型。
        for (String leak : leaks) {
            System.out.println("  FAIL minecraft type leaked into Body signature: " + leak);
        }
        check("body: no net.minecraft type in any signature (" + leaks.size() + " leaks)", leaks.isEmpty());

        System.out.println("  INFO Body exposes " + names.size() + " methods");
    }

    private static void collectLeak(String where, String typeName, List<String> leaks) {
        if (typeName.toLowerCase().contains("minecraft")) {
            leaks.add(where + " -> " + typeName);
        }
    }

    // ── 2. WireMessage 前向兼容 ───────────────────────────────────

    private static void wireChecks() {
        // 2.1 往返一致
        Map<String, Object> payload = new LinkedHashMap<>();
        payload.put("name", "cave_to_base");
        payload.put("count", 42L);
        payload.put("ratio", 0.5d);
        payload.put("ok", Boolean.TRUE);
        payload.put("note", null);
        payload.put("tags", List.of("a", "b"));
        Map<String, Object> nested = new LinkedHashMap<>();
        nested.put("x", 1L);
        payload.put("pos", nested);

        WireMessage msg = WireMessage.of("state", 7, payload);
        check("wire: v equals PROTOCOL_VERSION", msg.v == Protocol.PROTOCOL_VERSION);
        String json = msg.toJson();
        check("wire: toJson not null", json != null);
        WireMessage back = WireMessage.fromJson(json);
        check("wire: roundtrip not null", back != null);
        check("wire: roundtrip type", back != null && "state".equals(back.type));
        check("wire: roundtrip seq", back != null && back.seq == 7);
        check("wire: roundtrip v", back != null && back.v == Protocol.PROTOCOL_VERSION);
        check("wire: roundtrip string", back != null && "cave_to_base".equals(back.payload.get("name")));
        check("wire: roundtrip long", back != null && Long.valueOf(42L).equals(back.payload.get("count")));
        check("wire: roundtrip double", back != null && Double.valueOf(0.5d).equals(back.payload.get("ratio")));
        check("wire: roundtrip bool", back != null && Boolean.TRUE.equals(back.payload.get("ok")));
        check("wire: roundtrip explicit null", back != null && back.payload.containsKey("note")
                && back.payload.get("note") == null);
        check("wire: roundtrip list", back != null && List.of("a", "b").equals(back.payload.get("tags")));
        check("wire: roundtrip nested map", back != null && back.payload.get("pos") instanceof Map<?, ?> p
                && Long.valueOf(1L).equals(p.get("x")));

        // 2.2 未知字段忽略
        WireMessage extra = WireMessage.fromJson(
                "{\"v\":1,\"type\":\"event\",\"seq\":1,\"ts\":2,\"payload\":{\"a\":1},\"future\":{\"deep\":true}}");
        check("wire: unknown top-level field ignored", extra != null && extra.v == 1
                && "event".equals(extra.type) && Long.valueOf(1L).equals(extra.payload.get("a")));

        // 2.3 未知 type 不报错、归一 unknown、保留原始串
        WireMessage unknown = WireMessage.fromJson(
                "{\"v\":1,\"type\":\"future_thing\",\"seq\":3,\"ts\":4,\"payload\":{}}");
        check("wire: unknown type not an error", unknown != null);
        check("wire: unknown type normalized", unknown != null && unknown.isUnknown()
                && WireMessage.UNKNOWN_TYPE.equals(unknown.type));
        check("wire: unknown type declared preserved", unknown != null
                && "future_thing".equals(unknown.declaredType));
        String unknownJson = unknown == null ? null : unknown.toJson();
        check("wire: unknown type re-emits declared", unknownJson != null && unknownJson.contains("future_thing"));

        // 2.4 缺 v 按 0
        WireMessage nov = WireMessage.fromJson("{\"type\":\"event\",\"seq\":1,\"payload\":{}}");
        check("wire: missing v treated as 0", nov != null && nov.v == 0);

        // 2.5 缺 payload / type 也不炸
        WireMessage bare = WireMessage.fromJson("{\"v\":1}");
        check("wire: bare message ok", bare != null && bare.payload.isEmpty()
                && WireMessage.UNKNOWN_TYPE.equals(bare.type));

        // 2.6 坏 JSON → null（不抛）
        String[] bad = {"", "   ", "{", "}", "not json", "[1,2]", "{\"v\":}", "{\"a\":1,}", "\"just a string\"", "42"};
        int badOk = 0;
        for (String b : bad) {
            try {
                if (WireMessage.fromJson(b) == null) {
                    badOk++;
                }
            } catch (RuntimeException e) {
                // 抛 = 失败（计入 badOk 之外）
            }
        }
        check("wire: bad json returns null, never throws (" + badOk + "/" + bad.length + ")", badOk == bad.length);

        // 2.7 超长 payload 上限
        Map<String, Object> huge = new LinkedHashMap<>();
        huge.put("blob", "x".repeat(WireMessage.MAX_MESSAGE_CHARS + 8));
        WireMessage hugeMsg = WireMessage.of("event", 0, huge);
        check("wire: oversized toJson returns null", hugeMsg.toJson() == null);
        String hugeRaw = "{\"v\":1,\"type\":\"event\",\"payload\":{\"blob\":\"" + "x".repeat(WireMessage.MAX_MESSAGE_CHARS + 8) + "\"}}";
        check("wire: oversized fromJson returns null", WireMessage.fromJson(hugeRaw) == null);

        // 2.8 UTF-8 往返（显式 UTF-8 语义）
        Map<String, Object> cn = new LinkedHashMap<>();
        cn.put("text", "你好，我在挖矿 ✓");
        WireMessage cnBack = WireMessage.fromJson(WireMessage.of("event", 1, cn).toJson());
        check("wire: utf-8 roundtrip", cnBack != null && "你好，我在挖矿 ✓".equals(cnBack.payload.get("text")));

        // 2.9 已知 type 不被归一
        WireMessage known = WireMessage.fromJson("{\"type\":\"progress\"}");
        check("wire: known types untouched", known != null && "progress".equals(known.type));
    }

    // ── 3. 能力位 ─────────────────────────────────────────────────

    private static final Pattern SNAKE = Pattern.compile("[a-z][a-z0-9_]*");

    private static void capabilityChecks() {
        List<String> caps = Protocol.CAPABILITIES;
        check("caps: non-empty", !caps.isEmpty());

        int blanks = 0;
        Set<String> seen = new HashSet<>();
        int dup = 0;
        int malformed = 0;
        for (String c : caps) {
            if (c == null || c.isBlank()) {
                blanks++;
                continue;
            }
            if (!seen.add(c)) {
                dup++;
            }
            if (!SNAKE.matcher(c).matches()) {
                malformed++;
            }
        }
        check("caps: no blank entries (" + blanks + ")", blanks == 0);
        check("caps: all unique (dups=" + dup + ")", dup == 0);
        check("caps: all snake_case (" + malformed + " malformed)", malformed == 0);
        System.out.println("  INFO capabilities = " + caps);
    }

    // ── 4. 版本常量稳定 ───────────────────────────────────────────

    private static void versionChecks() {
        check("version: PROTOCOL_VERSION == 1", Protocol.PROTOCOL_VERSION == 1);
        String joined = String.join(",", Protocol.CAPABILITIES);
        check("version: capability list pinned",
                "state_push,structured_chat,container_view,server_events,entity_detail,structured_request"
                        .equals(joined));
        check("version: known types non-empty", !WireMessage.KNOWN_TYPES.isEmpty());
        check("version: known types pinned count", WireMessage.KNOWN_TYPES.size() == 6);
        check("version: profile tokens distinct",
                !Protocol.PROFILE_CLIENT.equals(Protocol.PROFILE_SERVER)
                        && !Protocol.PROFILE_SERVER.equals(Protocol.PROFILE_BOTH));
    }

    // ── helpers ──────────────────────────────────────────────────

    // ── 5. 线协议 token 鉴权（可选握手）────────────────────────────

    private static void tokenChecks() {
        // 空 token = 不鉴权 ⇒ 老链路一字不变（这就是向后兼容的根据）。
        check("auth: blank configured token accepts anything",
                WireAuth.matches("", "whatever") && WireAuth.matches(null, "x")
                        && WireAuth.matches("   ", "x"));
        check("auth: matching token accepted", WireAuth.matches("s3cret", "s3cret"));
        check("auth: surrounding whitespace tolerated", WireAuth.matches(" s3cret ", "s3cret"));
        check("auth: wrong token rejected", !WireAuth.matches("s3cret", "wrong"));
        check("auth: empty offer rejected when a token is set", !WireAuth.matches("s3cret", ""));
        check("auth: null offer rejected when a token is set", !WireAuth.matches("s3cret", null));

        String wire = readSource("network/WireServer.java");
        String config = readSource("config/ModConfig.java");
        String auth = readSource("network/WireAuth.java");
        check("auth: WireServer delegates to WireAuth", wire.contains("WireAuth.matches"));
        check("auth: WireAuth is dependency-free (no gson / no minecraft / no mod logger)",
                !auth.isBlank() && !auth.contains("gson") && !auth.contains("net.minecraft")
                        && !auth.contains("LCUMod"));
        check("auth: WireServer gates on a hello frame", wire.contains("\"hello\".equals(type)"));
        int gate = wire.indexOf("!authed");
        int dispatch = wire.indexOf("\"command\".equals(type)");
        check("auth: hello gate runs before command dispatch",
                gate >= 0 && dispatch >= 0 && gate < dispatch);
        check("auth: unauthenticated connections time out", wire.contains("setSoTimeout(HELLO_TIMEOUT_MS)"));
        check("auth: config key wireToken defaults to empty",
                config.contains("\"wireToken\"") && config.contains(".define(\"wireToken\", \"\")"));
    }

    /** 读主源码（cwd = 工程根，与 WalkCheck 同一约定）。 */
    private static String readSource(String relative) {
        try {
            return Files.readString(
                    Path.of("src", "main", "java", "com", "lcu", "lcumod", relative),
                    StandardCharsets.UTF_8);
        } catch (Exception e) {
            System.out.println("  INFO could not read " + relative + ": " + e.getMessage());
            return "";
        }
    }

    private static void check(String name, boolean ok) {
        if (ok) {
            passed++;
            System.out.println("  OK   " + name);
        } else {
            failed++;
            System.out.println("  FAIL " + name);
        }
    }

    private ProtoCheck() {
    }
}
