package com.lcu.lcumod.client;

/**
 * {@link TitleFormat} 的纯逻辑自检 —— 零第三方依赖，由 build.gradle 的 verifyTitle 任务执行，
 * 并接入 check（随 build 一起跑）。
 *
 * 断言信息刻意用 ASCII：Windows 控制台默认 GBK，避免非必要的中文输出。
 */
public final class TitleFormatCheck {

    private static int checks = 0;
    private static int failed = 0;

    private TitleFormatCheck() {
    }

    public static void main(String[] args) {
        expect("[AI 操控中] 挖掘中 · Rapid Optimization-ai · 服务器 127.0.0.1:25565 · MC 26.3 / NeoForge 26.3.0.42-beta",
            TitleFormat.build(TitleFormat.AI_CONTROL, "挖掘中", "Rapid Optimization-ai",
                "服务器 127.0.0.1:25565", "MC 26.3", "NeoForge 26.3.0.42-beta"),
            "all fields present");

        expect("[你操控中] Rapid Optimization-ai · 主菜单 · MC 26.3 / NeoForge 26.3.0.57-beta",
            TitleFormat.build(TitleFormat.USER_CONTROL, "", "Rapid Optimization-ai",
                "主菜单", "MC 26.3", "NeoForge 26.3.0.57-beta"),
            "empty activity omitted");

        expect("[AI 操控中] MC 26.3 / NeoForge 26.3.0.57-beta",
            TitleFormat.build(TitleFormat.AI_CONTROL, null, null, null, "MC 26.3", "NeoForge 26.3.0.57-beta"),
            "null fields dropped");

        expect("[AI 操控中] lcutest · MC 26.3",
            TitleFormat.build(TitleFormat.AI_CONTROL, "", "  lcutest  ", "", "MC 26.3", ""),
            "blank loader dropped, fields trimmed");

        expect("[AI 操控中] 单人 world · NeoForge x",
            TitleFormat.build(TitleFormat.AI_CONTROL, "", "", "单人 world", "", "NeoForge x"),
            "empty mc version keeps loader");

        expect("[?] MC 26.3",
            TitleFormat.build("", "", "", "", "MC 26.3", ""),
            "blank state falls back to ?");

        expect("[AI 操控中]", TitleFormat.build(TitleFormat.AI_CONTROL, "", "", "", "", ""),
            "nothing but the state marker");

        String compact = TitleFormat.build(TitleFormat.AI_CONTROL, "", "inst", "主菜单", "MC 26.3", "NeoForge v");
        check(!compact.contains("· ·"), "no doubled separator: " + compact);
        check(!compact.contains("  "), "no doubled space: " + compact);

        check("b".equals(TitleFormat.join("/", null, "b", "   ")), "join drops blanks");
        check("a/b".equals(TitleFormat.join("/", "a", "b")), "join keeps order");

        System.out.println("[verifyTitle] TitleFormat: " + (checks - failed) + " checks passed"
            + (failed == 0 ? "" : ", " + failed + " FAILED"));
        if (failed > 0) {
            System.exit(1);
        }
    }

    private static void expect(String expected, String actual, String what) {
        checks++;
        if (!expected.equals(actual)) {
            failed++;
            System.out.println("[verifyTitle] FAIL " + what);
            System.out.println("             expected: " + expected);
            System.out.println("             actual  : " + actual);
        }
    }

    private static void check(boolean condition, String what) {
        checks++;
        if (!condition) {
            failed++;
            System.out.println("[verifyTitle] FAIL " + what);
        }
    }
}
