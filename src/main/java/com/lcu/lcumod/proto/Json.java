package com.lcu.lcumod.proto;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 极简 JSON 编解码（**零第三方依赖**，显式 UTF-8 语义）。
 *
 * <p>只覆盖线协议真实用到的子集：object / array / string / number / boolean / null。
 * 不做 pretty-print，不做注释，不做 NaN/Infinity —— 与 {@code WireServer} 的
 * "一行一个 JSON 对象" 风格一致，但不引入 Gson，从而 {@code verifyProto}
 * 可以在只有 {@code main.output + test.output} 的类路径上零依赖跑起来。
 *
 * <p>数字只产出 {@link Long}（无小数）或 {@link Double}（有小数/指数）。
 * 字符串按 UTF-8 原样输出，仅转义控制字符与 {@code "} {@code \}。
 */
final class Json {

    private Json() {
    }

    /** 序列化任意支持的值；不支持的类型直接抛 {@link IllegalArgumentException}（调用方转 null）。 */
    static String stringify(Object value) {
        StringBuilder sb = new StringBuilder();
        write(value, sb);
        return sb.toString();
    }

    static void write(Object value, StringBuilder sb) {
        if (value == null) {
            sb.append("null");
        } else if (value instanceof String s) {
            writeString(s, sb);
        } else if (value instanceof Boolean b) {
            sb.append(b ? "true" : "false");
        } else if (value instanceof Number n) {
            writeNumber(n, sb);
        } else if (value instanceof Map<?, ?> m) {
            sb.append('{');
            boolean first = true;
            for (Map.Entry<?, ?> e : m.entrySet()) {
                if (!(e.getKey() instanceof String key)) {
                    throw new IllegalArgumentException("non-string key: " + e.getKey());
                }
                if (!first) {
                    sb.append(',');
                }
                first = false;
                writeString(key, sb);
                sb.append(':');
                write(e.getValue(), sb);
            }
            sb.append('}');
        } else if (value instanceof List<?> list) {
            sb.append('[');
            boolean first = true;
            for (Object item : list) {
                if (!first) {
                    sb.append(',');
                }
                first = false;
                write(item, sb);
            }
            sb.append(']');
        } else {
            throw new IllegalArgumentException("unsupported type: " + value.getClass().getName());
        }
    }

    private static void writeNumber(Number n, StringBuilder sb) {
        if (n instanceof Double || n instanceof Float) {
            double d = n.doubleValue();
            if (Double.isNaN(d) || Double.isInfinite(d)) {
                throw new IllegalArgumentException("non-finite number");
            }
            sb.append(d);
        } else {
            sb.append(n.longValue());
        }
    }

    private static void writeString(String s, StringBuilder sb) {
        sb.append('"');
        for (int i = 0; i < s.length(); i++) {
            char c = s.charAt(i);
            switch (c) {
                case '"' -> sb.append("\\\"");
                case '\\' -> sb.append("\\\\");
                case '\n' -> sb.append("\\n");
                case '\r' -> sb.append("\\r");
                case '\t' -> sb.append("\\t");
                case '\b' -> sb.append("\\b");
                case '\f' -> sb.append("\\f");
                default -> {
                    if (c < 0x20) {
                        sb.append("\\u").append(String.format("%04x", (int) c));
                    } else {
                        sb.append(c);
                    }
                }
            }
        }
        sb.append('"');
    }

    /** 解析一个 JSON 文档；任何结构错误抛 {@link IllegalArgumentException}（由 {@code WireMessage} 转 null）。 */
    static Object parse(String text) {
        if (text == null) {
            throw new IllegalArgumentException("null input");
        }
        Parser p = new Parser(text);
        Object value = p.value();
        p.ws();
        if (!p.eof()) {
            throw p.err("trailing content");
        }
        return value;
    }

    private static final class Parser {
        private final String s;
        private int i;

        Parser(String s) {
            this.s = s;
        }

        boolean eof() {
            return i >= s.length();
        }

        void ws() {
            while (i < s.length()) {
                char c = s.charAt(i);
                if (c == ' ' || c == '\t' || c == '\n' || c == '\r') {
                    i++;
                } else {
                    break;
                }
            }
        }

        IllegalArgumentException err(String msg) {
            return new IllegalArgumentException(msg + " at " + i);
        }

        char next() {
            if (eof()) {
                throw err("unexpected end");
            }
            return s.charAt(i++);
        }

        char peek() {
            if (eof()) {
                throw err("unexpected end");
            }
            return s.charAt(i);
        }

        Object value() {
            ws();
            char c = peek();
            switch (c) {
                case '{':
                    return object();
                case '[':
                    return array();
                case '"':
                    return string();
                case 't':
                    expect("true");
                    return Boolean.TRUE;
                case 'f':
                    expect("false");
                    return Boolean.FALSE;
                case 'n':
                    expect("null");
                    return null;
                default:
                    return number();
            }
        }

        void expect(String word) {
            if (!s.startsWith(word, i)) {
                throw err("expected " + word);
            }
            i += word.length();
        }

        Map<String, Object> object() {
            i++; // '{'
            Map<String, Object> m = new LinkedHashMap<>();
            ws();
            if (peek() == '}') {
                i++;
                return m;
            }
            while (true) {
                ws();
                String key = string();
                ws();
                if (next() != ':') {
                    throw err("expected ':'");
                }
                m.put(key, value());
                ws();
                char d = next();
                if (d == '}') {
                    return m;
                }
                if (d != ',') {
                    throw err("expected ',' or '}'");
                }
            }
        }

        List<Object> array() {
            i++; // '['
            List<Object> list = new ArrayList<>();
            ws();
            if (peek() == ']') {
                i++;
                return list;
            }
            while (true) {
                list.add(value());
                ws();
                char d = next();
                if (d == ']') {
                    return list;
                }
                if (d != ',') {
                    throw err("expected ',' or ']'");
                }
            }
        }

        String string() {
            if (next() != '"') {
                throw err("expected string");
            }
            StringBuilder sb = new StringBuilder();
            while (true) {
                char c = next();
                if (c == '"') {
                    return sb.toString();
                }
                if (c != '\\') {
                    sb.append(c);
                    continue;
                }
                char esc = next();
                switch (esc) {
                    case '"' -> sb.append('"');
                    case '\\' -> sb.append('\\');
                    case '/' -> sb.append('/');
                    case 'n' -> sb.append('\n');
                    case 'r' -> sb.append('\r');
                    case 't' -> sb.append('\t');
                    case 'b' -> sb.append('\b');
                    case 'f' -> sb.append('\f');
                    case 'u' -> {
                        if (i + 4 > s.length()) {
                            throw err("bad \\u escape");
                        }
                        int cp = Integer.parseInt(s.substring(i, i + 4), 16);
                        i += 4;
                        sb.append((char) cp);
                    }
                    default -> throw err("bad escape");
                }
            }
        }

        Number number() {
            int start = i;
            if (peek() == '-') {
                i++;
            }
            boolean dot = false;
            while (i < s.length()) {
                char c = s.charAt(i);
                if (c >= '0' && c <= '9') {
                    i++;
                } else if (c == '.' || c == 'e' || c == 'E' || c == '+' || c == '-') {
                    if (!dot && (c == '.' || c == 'e' || c == 'E')) {
                        dot = true;
                    }
                    i++;
                } else {
                    break;
                }
            }
            String raw = s.substring(start, i);
            if (raw.isEmpty() || "-".equals(raw)) {
                throw err("bad number");
            }
            try {
                return dot ? (Number) Double.valueOf(raw) : (Number) Long.valueOf(raw);
            } catch (NumberFormatException e) {
                throw err("bad number: " + raw);
            }
        }
    }
}
