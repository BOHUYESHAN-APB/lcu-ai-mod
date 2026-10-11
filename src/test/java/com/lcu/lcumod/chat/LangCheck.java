package com.lcu.lcumod.chat;

import java.io.InputStream;
import java.nio.charset.StandardCharsets;
import java.util.LinkedHashSet;
import java.util.Set;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * 语言文件对齐自检 —— 零第三方依赖，由 build.gradle 的 verifyLang 任务执行，并接入 check。
 *
 * 为什么需要它：26.3 曾**整文件缺失** zh_cn.json；而 26.3 与 master 的键集合不同
 * （master 有 18 个 conversation/runtimeRole 键，26.3 没有对应代码）。抄 master 会造出
 * "有翻译无来源"的孤儿键，漏键又会让界面露英文。这里把 zh_cn 的键集合**钉死**成与
 * en_us 逐一对应，防止将来漂移。
 *
 * 断言信息刻意用 ASCII：Windows 控制台默认 GBK，避免非必要的中文输出。
 */
public final class LangCheck {

    private static final Pattern KEY = Pattern.compile("\"([^\"]+)\"\\s*:");

    private static int checks = 0;
    private static int failed = 0;

    private LangCheck() {
    }

    public static void main(String[] args) throws Exception {
        Set<String> en = keys("/assets/lcumod/lang/en_us.json");
        Set<String> zh = keys("/assets/lcumod/lang/zh_cn.json");

        check(!en.isEmpty(), "en_us.json has keys (" + en.size() + ")");
        check(!zh.isEmpty(), "zh_cn.json exists and has keys (" + zh.size() + ")");

        Set<String> missing = new LinkedHashSet<>(en);
        missing.removeAll(zh);
        Set<String> extra = new LinkedHashSet<>(zh);
        extra.removeAll(en);

        check(missing.isEmpty(), "every en_us key has a zh_cn translation; missing=" + missing);
        check(extra.isEmpty(), "zh_cn has no key without an en_us source; extra=" + extra);
        check(en.equals(zh), "key sets are identical (en=" + en.size() + ", zh=" + zh.size() + ")");

        // 反向保护：绝不把 master 的 conversation/runtimeRole 键抄进来（26.3 没有这些代码）。
        for (String key : zh) {
            check(!key.contains("conversation") && !key.contains("runtimeRole"),
                "zh_cn key is not a master-only conversation/runtimeRole key: " + key);
        }

        System.out.println("[verifyLang] LangAlignment: " + (checks - failed) + " checks passed"
            + (failed == 0 ? "" : ", " + failed + " FAILED"));
        if (failed > 0) {
            System.exit(1);
        }
    }

    private static Set<String> keys(String path) throws Exception {
        try (InputStream in = LangCheck.class.getResourceAsStream(path)) {
            if (in == null) {
                return new LinkedHashSet<>();
            }
            String body = new String(in.readAllBytes(), StandardCharsets.UTF_8);
            Set<String> out = new LinkedHashSet<>();
            Matcher m = KEY.matcher(body);
            while (m.find()) {
                out.add(m.group(1));
            }
            return out;
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
