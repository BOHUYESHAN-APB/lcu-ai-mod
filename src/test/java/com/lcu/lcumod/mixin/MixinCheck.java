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

        // (e) 注入模拟：把 Mixin 在**加载时**才做的检查提前到 CI（用户口径："就得先模拟这个注入逻辑"）
        injectionSimulationChecks(client);

        // (f) 加载器一致性：声明的 NeoForge 下限必须等于构建版本（否则允许跑在没验过的 loader 上）
        loaderVersionChecks();

        System.out.println("[verifyMixin] MixinRegistration: " + (checks - failed) + " checks passed"
            + (failed == 0 ? "" : ", " + failed + " FAILED"));
        if (failed > 0) {
            System.exit(1);
        }
    }

    /**
     * 注入模拟 —— 用户口径："CI 就得先模拟这个注入逻辑，尽可能在测试阶段发现这类显著性问题。"
     *
     * <p>背景（真机事故，2026-10-12）：`KeyboardHandlerMixin` 在客户端启动时直接崩：
     * {@code InvalidMixinException: … contains non-private static method isAlwaysAllowed(…)}。
     * 原因是 mixin 类里有一个**包级可见的 static 方法** —— Mixin 的这条结构规则只在游戏启动
     * 那一刻才检查，编译和单测都毫无征兆。所以这里把 Mixin 应用阶段会做的检查搬进自检：
     * <ol>
     *   <li>结构规则：mixin 类里的 static 方法必须 private；static 字段必须 private 或 static final
     *       （非 private 的会被"合并"进目标类，Mixin 直接拒绝整个 mixin）；</li>
     *   <li>注入点规则：`@Inject` 处理函数必须 private、非 static、返回 void、末位是 CallbackInfo；</li>
     *   <li>**目标存在性**：`@Mixin(X.class)` 的 X 能在 classpath 上加载，且 `@Inject(method="m")` 的 m
     *       在 X 上真的存在、参数类型与处理函数前面的参数**逐一相同** —— 这正是 Mixin 在应用时
     *       会去找的东西（版本不一致/签名写错，就是在这一步炸）。</li>
     * </ol>
     *
     * <p>为什么解析**源码**而不是读注解：Mixin 的注解是 CLASS retention，反射读不到；
     * 反射只用来在 classpath 上验证"目标类和目标方法确实存在"。
     */
    private static void injectionSimulationChecks(List<String> declared) throws Exception {
        ClassLoader targetLoader = MixinCheck.class.getClassLoader();
        for (String simpleName : declared) {
            String source = readSource("src/main/java/com/lcu/lcumod/mixin/" + simpleName + ".java");
            check(source != null, "simulate: " + simpleName + ".java is readable from the project dir");
            if (source == null) {
                continue;
            }

            String declaredTarget = mixinTarget(source);
            String targetName = declaredTarget == null ? null : resolveType(source, declaredTarget);
            check(targetName != null, "simulate: " + simpleName + " declares @Mixin(<Target>.class)");
            Class<?> targetClass = null;
            if (targetName != null) {
                try {
                    targetClass = Class.forName(targetName, false, targetLoader);
                } catch (Throwable t) {
                    System.out.println("  FAIL: cannot load mixin target " + targetName + ": " + t);
                }
                check(targetClass != null, "simulate: mixin target " + targetName
                    + " is on the classpath we compile against");
            }

            // ① 结构规则（真机事故就是这个）：mixin 里的 static 方法必须 private。
            //    解析**源码**而不是反射：反射要 Mixin 在运行 classpath 上（Handler 的参数是 CallbackInfo），
            //    而 verifyMixin 的运行 classpath 只有本仓输出 —— 那正是"看起来验了、其实没验"的坑。
            List<String[]> methods = mixinMethods(source);
            check(!methods.isEmpty(), "simulate: " + simpleName + " has parseable method declarations");
            for (String[] method : methods) {
                String mods = method[0];
                String name = method[2];
                if (mods.contains("static")) {
                    check(mods.contains("private"), "simulate: " + simpleName + "." + name
                        + "() is a non-private static method (Mixin rejects the whole class at load time)");
                }
            }

            // ②③ 注入点规则 + 目标签名
            for (String[] injection : injections(source)) {
                String targetMethod = injection[0];
                String handlerName = injection[1];
                String[] handler = null;
                for (String[] method : methods) {
                    if (method[2].equals(handlerName)) {
                        handler = method;
                        break;
                    }
                }
                check(handler != null, "simulate: " + simpleName + " has the handler " + handlerName);
                if (handler == null) {
                    continue;
                }
                check(handler[0].contains("private"), "simulate: handler " + handlerName + " must be private");
                check(!handler[0].contains("static"), "simulate: handler " + handlerName + " must not be static");
                check(handler[1].equals("void"), "simulate: handler " + handlerName + " must return void");

                String[] handlerParams = splitParams(handler[3]);
                check(handlerParams.length > 0 && simpleType(handlerParams[handlerParams.length - 1])
                        .endsWith("CallbackInfo"),
                    "simulate: handler " + handlerName + " ends with a CallbackInfo parameter");

                if (targetClass == null) {
                    continue;
                }
                String[] expectedNames = java.util.Arrays.copyOf(handlerParams,
                    Math.max(0, handlerParams.length - 1));
                Class<?>[] expected = new Class<?>[expectedNames.length];
                boolean resolved = true;
                for (int i = 0; i < expectedNames.length; i++) {
                    expected[i] = resolveClass(source, targetLoader, simpleType(expectedNames[i]));
                    if (expected[i] == null) {
                        resolved = false;
                        check(false, "simulate: cannot resolve parameter type " + simpleType(expectedNames[i])
                            + " of handler " + handlerName);
                    }
                }
                if (!resolved) {
                    continue;
                }
                StringBuilder signature = new StringBuilder(targetMethod).append('(');
                for (int i = 0; i < expected.length; i++) {
                    signature.append(i == 0 ? "" : ", ").append(expected[i].getSimpleName());
                }
                signature.append(')');
                java.lang.reflect.Method target = null;
                try {
                    target = targetClass.getDeclaredMethod(targetMethod, expected);
                } catch (NoSuchMethodException e) {
                    // 就是 Mixin 应用时会报的那种"找不到注入目标"
                } catch (Throwable t) {
                    System.out.println("  FAIL: cannot inspect " + targetName + ": " + t);
                }
                check(target != null, "simulate: " + targetName + "." + signature
                    + " exists with exactly that signature (that is what Mixin resolves at apply time)");
            }
        }
    }

    /** 粗略解析 mixin 源码里的方法声明：{修饰符, 返回类型, 名字, 参数原文}（只看签名独占一行的写法）。 */
    static List<String[]> mixinMethods(String source) {
        List<String[]> out = new ArrayList<>();
        for (String raw : source.split("\\R")) {
            String line = raw.trim();
            if (line.isEmpty() || line.startsWith("*") || line.startsWith("//") || line.startsWith("/**")
                    || line.startsWith("@") || line.startsWith("import") || line.startsWith("package")
                    || line.startsWith("private ") && line.endsWith("()") // 构造函数之类
                    || !line.endsWith("{")) {
                continue;
            }
            int paren = line.indexOf('(');
            int close = line.lastIndexOf(')');
            if (paren < 0 || close < paren) {
                continue;
            }
            String head = line.substring(0, paren).trim();
            String[] parts = head.split("\\s+");
            if (parts.length < 2) {
                continue;
            }
            StringBuilder mods = new StringBuilder();
            for (int i = 0; i < parts.length - 2; i++) {
                mods.append(parts[i]).append(' ');
            }
            out.add(new String[] {
                mods.toString().trim(),
                parts[parts.length - 2],
                parts[parts.length - 1],
                line.substring(paren + 1, close).trim(),
            });
        }
        return out;
    }

    /** `long window, int action, KeyEvent event, CallbackInfo ci` → {"long window", ...}。 */
    static String[] splitParams(String params) {
        if (params.isEmpty()) {
            return new String[0];
        }
        List<String> out = new ArrayList<>();
        int depth = 0;
        int start = 0;
        for (int i = 0; i < params.length(); i++) {
            char c = params.charAt(i);
            if (c == '<') {
                depth++;
            } else if (c == '>') {
                depth--;
            } else if (c == ',' && depth == 0) {
                out.add(params.substring(start, i).trim());
                start = i + 1;
            }
        }
        out.add(params.substring(start).trim());
        return out.toArray(new String[0]);
    }

    /** `KeyEvent event` → `KeyEvent`。 */
    static String simpleType(String parameter) {
        String[] parts = parameter.trim().split("\\s+");
        return parts.length == 0 ? "" : parts[0];
    }

    /** 解析参数类型为 Class：原语走表，其余走源码 import 再在目标 classpath 上加载。 */
    static Class<?> resolveClass(String source, ClassLoader loader, String simpleName) {
        switch (simpleName) {
            case "long": return long.class;
            case "int": return int.class;
            case "float": return float.class;
            case "double": return double.class;
            case "boolean": return boolean.class;
            case "byte": return byte.class;
            case "short": return short.class;
            case "char": return char.class;
            default:
                break;
        }
        try {
            String resolved = resolveType(source, simpleName);
            if (resolved != null && resolved.equals(simpleName) && !simpleName.contains(".")) {
                return Class.forName(simpleName, false, loader);
            }
            return Class.forName(resolved, false, loader);
        } catch (Throwable t) {
            return null;
        }
    }

    /**
     * 加载器一致性：`neo_version_range` 的下限必须是**构建用的那个版本**。
     *
     * <p>理由：把下限放宽到更旧的 loader（例如为了让某个测试实例能加载而写成 0.42，代码却按 0.57
     * 编译）等于对外承诺"能跑在没验过的组合上" —— 真机上就是这么炸的，而且报出来的是 Mixin 内部异常，
     * 完全看不出根因。宁可让加载器给出"需要更高版本"，也不要让 Mixin 在启动中途炸掉。
     */
    private static void loaderVersionChecks() {
        String props = readSource("gradle.properties");
        check(props != null, "loader: gradle.properties is readable");
        if (props == null) {
            return;
        }
        String build = property(props, "neo_version");
        String range = property(props, "neo_version_range");
        check(build != null && !build.isBlank(), "loader: neo_version is set");
        check(range != null && !range.isBlank(), "loader: neo_version_range is set");
        if (build == null || range == null) {
            return;
        }
        String lower = range.trim();
        if (lower.startsWith("[")) {
            lower = lower.substring(1);
        }
        int comma = lower.indexOf(',');
        lower = (comma >= 0 ? lower.substring(0, comma) : lower).trim();
        check(lower.equals(build.trim()), "loader: the declared NeoForge lower bound (" + lower
            + ") must equal the version we build against (" + build.trim()
            + ") - we do not claim to run on a loader we never compiled or tested against");

        String template = readSource("src/main/templates/META-INF/neoforge.mods.toml");
        check(template != null && template.contains("${neo_version_range}"),
            "loader: the mod metadata actually uses neo_version_range");
    }

    /** 取 `key=value`（gradle.properties 极简解析，忽略注释行）。 */
    private static String property(String properties, String key) {
        for (String line : properties.split("\\R")) {
            String trimmed = line.trim();
            if (trimmed.startsWith("#") || !trimmed.startsWith(key + "=")) {
                continue;
            }
            return trimmed.substring(key.length() + 1).trim();
        }
        return null;
    }

    /** 把源码里的简名补成全名（走 import）；已经是全名就不动。 */
    static String resolveType(String source, String name) {
        if (name == null || name.contains(".")) {
            return name;
        }
        for (String line : source.split("\\R")) {
            String trimmed = line.trim();
            if (trimmed.startsWith("import ") && trimmed.endsWith("." + name + ";")) {
                return trimmed.substring("import ".length(), trimmed.length() - 1).trim();
            }
        }
        return name;
    }

    /** `@Mixin(com.foo.Bar.class)` → `com.foo.Bar`（源码里通常是简名，再用 {@link #resolveType} 补包名）。 */
    static String mixinTarget(String source) {
        int at = source.indexOf("@Mixin(");
        if (at < 0) {
            return null;
        }
        int end = source.indexOf(".class", at);
        if (end < 0) {
            return null;
        }
        String name = source.substring(at + "@Mixin(".length(), end).trim();
        return name.isEmpty() ? null : name;
    }

    /**
     * 解析 `@Inject(...)` 与它紧跟着的处理函数：返回 {目标方法名, 处理函数名} 列表。
     *
     * 只认 {@code method = "..."} 的写法（本仓的 mixin 都是这种），认不出就跳过 —— 不做花哨推断。
     */
    static List<String[]> injections(String source) {
        List<String[]> out = new ArrayList<>();
        int cursor = 0;
        while (true) {
            int at = source.indexOf("@Inject(", cursor);
            if (at < 0) {
                return out;
            }
            int close = source.indexOf(')', at);
            if (close < 0) {
                return out;
            }
            String annotation = source.substring(at, close);
            cursor = close + 1;
            int m = annotation.indexOf("method");
            if (m < 0) {
                continue;
            }
            int quote = annotation.indexOf('"', m);
            int quoteEnd = quote < 0 ? -1 : annotation.indexOf('"', quote + 1);
            if (quote < 0 || quoteEnd < 0) {
                continue;
            }
            String targetMethod = annotation.substring(quote + 1, quoteEnd);

            int handler = source.indexOf("void ", cursor);
            if (handler < 0) {
                continue;
            }
            int paren = source.indexOf('(', handler);
            if (paren < 0) {
                continue;
            }
            String signature = source.substring(handler + "void ".length(), paren).trim();
            int space = signature.lastIndexOf(' ');
            String handlerName = (space < 0 ? signature : signature.substring(space + 1)).trim();
            if (!handlerName.isEmpty() && !targetMethod.isEmpty()) {
                out.add(new String[] {targetMethod, handlerName});
            }
        }
    }

    private static java.lang.reflect.Method findByName(Class<?> owner, String name) {
        for (java.lang.reflect.Method method : owner.getDeclaredMethods()) {
            if (method.getName().equals(name)) {
                return method;
            }
        }
        return null;
    }

    private static java.lang.reflect.Method findMethod(Class<?> owner, String name, Class<?>[] params) {
        for (java.lang.reflect.Method method : owner.getDeclaredMethods()) {
            if (method.getName().equals(name) && java.util.Arrays.equals(method.getParameterTypes(), params)) {
                return method;
            }
        }
        return null;
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
