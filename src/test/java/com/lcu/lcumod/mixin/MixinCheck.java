package com.lcu.lcumod.mixin;

import java.io.InputStream;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.List;

/**
 * {@code lcumod.mixins.json} 的纯逻辑自检 —— 零第三方依赖，由 build.gradle 的 verifyMixin 任务执行，
 * 并接入 check（随 build 一起跑）。
 *
 * 为什么需要它：mixin 配置一旦漏登记，后果**只在客户端启动时才暴露**（mixin 类是死代码，
 * 编译期/单测期都毫无征兆）。26.3 就出过这个事故——两个数组全空、MouseHandlerMixin 根本没生效。
 *
 * 断言：
 *   (a) 配置里声明的每个 mixin 类都存在、能被 JVM 实际加载；
 *   (b) 配置不再"死掉"（client 数组非空，两个数组不再同时为空）；
 *   (c) MouseHandlerMixin 的守卫条件真的编译进去了：注入方法名是 grabMouse，
 *       且方法体调用了 com.lcu.lcumod.action.InputIsolation.isAiControlled()（只读字节码常量池，不加载 MC）。
 *
 * 断言信息刻意用 ASCII：Windows 控制台默认 GBK，避免非必要的中文输出。
 */
public final class MixinCheck {

    private static int checks = 0;
    private static int failed = 0;

    private MixinCheck() {
    }

    public static void main(String[] args) throws Exception {
        String json = readResource("/lcumod.mixins.json");
        check(json != null, "lcumod.mixins.json is on the main resources classpath");

        List<String> common = arrayEntries(json, "mixins");
        List<String> client = arrayEntries(json, "client");

        // (b) 配置不再死掉
        check(!client.isEmpty(), "client mixin array is not empty (was empty in the broken 26.3 config)");
        check(!(common.isEmpty() && client.isEmpty()),
            "the mixin config declares at least one mixin (not both arrays empty)");
        // 两端通用（common）数组在本模组里**没有合法目标**：MouseHandler 是纯客户端类。
        // 因此这里只做"声明了就不得是坏名字"的检查，不强行塞一个假 mixin。
        for (String name : common) {
            check(!name.isBlank(), "common array entry is a concrete class name");
        }

        // (a) 每个声明的类都存在且能加载
        ClassLoader loader = MixinCheck.class.getClassLoader();
        for (String simpleName : client) {
            String fqcn = "com.lcu.lcumod.mixin." + simpleName;
            String resource = "/com/lcu/lcumod/mixin/" + simpleName + ".class";
            Class<?> loaded = null;
            try {
                loaded = Class.forName(fqcn, false, loader);
            } catch (Throwable t) {
                failed++;
                System.out.println("  FAIL: " + fqcn + " could not be loaded: " + t);
            }
            check(loaded != null, simpleName + " loads as a real compiled class");
            check(MixinCheck.class.getResource(resource) != null,
                simpleName + " is registered in the client array AND compiled into the artifact");
            if (loaded != null) {
                String bytes = readResourceAsLatin1(resource);
                check(bytes != null && bytes.contains("org/spongepowered/asm/mixin/Mixin"),
                    simpleName + " carries the @Mixin annotation reference in its bytecode");
            }
        }

        // (c) 守卫条件真的存在于字节码里
        String mouseBytes = readResourceAsLatin1("/com/lcu/lcumod/mixin/MouseHandlerMixin.class");
        check(mouseBytes != null, "MouseHandlerMixin.class is on the classpath");
        check(mouseBytes.contains("grabMouse"), "MouseHandlerMixin injects into grabMouse");
        check(mouseBytes.contains("InputIsolation"), "MouseHandlerMixin references the InputIsolation guard");
        check(mouseBytes.contains("isAiControlled"), "MouseHandlerMixin calls isAiControlled() (guarded cancel)");

        String isolationBytes = readResourceAsLatin1("/com/lcu/lcumod/action/InputIsolation.class");
        check(isolationBytes != null, "InputIsolation.class is on the classpath");
        check(isolationBytes.contains("isAiControlled"),
            "InputIsolation exposes the isAiControlled guard used by the mixin");
        check(isolationBytes.contains("()Z"), "the isAiControlled guard returns boolean");

        // (d) 键盘屏蔽：AI 控制中物理键盘不起作用，但必须留出控制开关与安全出口
        String keyboardFileName = "/com/lcu/lcumod/mixin/KeyboardHandlerMixin.class";
        String keyboardBytes = readResourceAsLatin1(keyboardFileName);
        check(keyboardBytes != null, "KeyboardHandlerMixin.class is on the classpath");
        check(keyboardBytes.contains("keyPress"), "KeyboardHandlerMixin injects into keyPress");
        check(keyboardBytes.contains("isAiControlled"),
            "KeyboardHandlerMixin is guarded by isAiControlled() (never unconditional)");
        check(keyboardBytes.contains("lcuBlockKeysWhileAiPlays"),
            "KeyboardHandlerMixin carries the blocking hook");
        // 放行清单看**源码**：SDL 常量是编译期内联的，字节码里只剩数字，断言不到名字。
        String keyboardSource = readSource("src/main/java/com/lcu/lcumod/mixin/KeyboardHandlerMixin.java");
        check(keyboardSource != null, "KeyboardHandlerMixin.java is readable from the project dir");
        check(keyboardSource != null && keyboardSource.contains("SDL_SCANCODE_F12"),
            "KeyboardHandlerMixin keeps the F12 control switch working");
        check(keyboardSource != null && keyboardSource.contains("SDL_SCANCODE_ESCAPE"),
            "KeyboardHandlerMixin keeps ESC (menu / quit) as a safety exit");
        check(keyboardSource != null && keyboardSource.contains("isAiControlled()"),
            "the blocking path stays guarded by isAiControlled()");
        check(keyboardSource != null && keyboardSource.contains("gui.screen() != null"),
            "an open screen keeps its own keys (the AI steps aside)");

        System.out.println("[verifyMixin] MixinRegistration: " + (checks - failed) + " checks passed"
            + (failed == 0 ? "" : ", " + failed + " FAILED"));
        if (failed > 0) {
            System.exit(1);
        }
    }

    /** 读项目目录下的源码文件（verifyMixin 的 CWD = 项目根）。 */
    static String readSource(String relative) {
        try {
            java.nio.file.Path path = java.nio.file.Path.of(relative);
            if (!path.toFile().isFile()) {
                return null;
            }
            return new String(java.nio.file.Files.readAllBytes(path),
                java.nio.charset.StandardCharsets.UTF_8);
        } catch (Exception e) {
            return null;
        }
    }

    /** 取 JSON 里某个数组键的字符串元素（配置极简，手工扫描即可，避免引入 Gson 依赖）。 */
    static List<String> arrayEntries(String json, String key) {
        List<String> out = new ArrayList<>();
        int k = json.indexOf("\"" + key + "\"");
        if (k < 0) {
            return out;
        }
        int open = json.indexOf('[', k);
        int close = json.indexOf(']', open);
        if (open < 0 || close < 0) {
            return out;
        }
        for (String part : json.substring(open + 1, close).split(",")) {
            String p = part.trim();
            if (p.length() >= 2 && p.charAt(0) == '"') {
                out.add(p.substring(1, p.length() - 1));
            }
        }
        return out;
    }

    private static String readResource(String path) throws Exception {
        try (InputStream in = MixinCheck.class.getResourceAsStream(path)) {
            if (in == null) {
                return null;
            }
            return new String(in.readAllBytes(), StandardCharsets.UTF_8);
        }
    }

    /** 读字节码并用 ISO-8859-1 解码：每个字节都能映射成字符，便于直接搜常量池里的 ASCII 串。 */
    private static String readResourceAsLatin1(String path) throws Exception {
        try (InputStream in = MixinCheck.class.getResourceAsStream(path)) {
            if (in == null) {
                return null;
            }
            return new String(in.readAllBytes(), StandardCharsets.ISO_8859_1);
        }
    }

    private static void check(boolean ok, String what) {
        checks++;
        if (!ok) {
            failed++;
            System.out.println("  FAIL: " + what);
        }
    }
}
