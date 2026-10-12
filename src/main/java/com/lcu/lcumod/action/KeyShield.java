package com.lcu.lcumod.action;

import java.util.HashSet;
import java.util.List;
import java.util.Set;
import java.util.TreeSet;

/**
 * 键盘屏蔽的**策略**（零依赖，可被自检直接做行为断言）。
 *
 * <p>真机事故（用户："按 F12 和 ESC 都是无限效果" = 完全没反应）：上一版用的是"**放行清单**"——
 * 只放行它认得的 F12/ESC 编码，其余一律吞掉。结果编码没匹配上（键码/扫描码单位与推断不一致），
 * 于是**连控制开关和人唯一的安全出口都吞掉了，等于把人锁在世界里**。
 *
 * <p>所以策略反过来，并且**默认安全**：
 * <ol>
 *   <li><b>fail-open</b>：只有**明确在"要拦的操作键"名单里**的才拦；名单外的键（功能键、聊天、
 *       截图、全屏…）一律放行。名单算错了最多是"少拦了几个键"，**绝不会再把人锁住**；</li>
 *   <li><b>豁免优先</b>：安全出口（ESC）与控制开关（F12）**永远**放行，即使它们同时出现在操作键名单里；</li>
 *   <li>名单不是硬编码的键码表，而是**从玩家自己的按键绑定**里读出来的（见 mixin），
 *       跟着玩家改键走，也不依赖"vanilla 到底用哪个字段"这种推断。</li>
 * </ol>
 */
public final class KeyShield {

    private KeyShield() {
    }

    /**
     * 这一按该不该被吞掉？
     *
     * @param key              {@code KeyEvent.key()}
     * @param keycode          {@code KeyEvent.keycode()}（两个字段都比对，避免单位判断错误）
     * @param gameplayKeys     要拦的操作键（来自玩家的按键绑定）
     * @param alwaysAllowed    永不拦的键（ESC / F12，含多种可能编码）
     * @return true = 吞掉这一按
     */
    public static boolean blocks(int key, int keycode,
                                 Set<Integer> gameplayKeys, Set<Integer> alwaysAllowed) {
        if (alwaysAllowed != null && (alwaysAllowed.contains(key) || alwaysAllowed.contains(keycode))) {
            return false;
        }
        if (gameplayKeys == null || gameplayKeys.isEmpty()) {
            // 拿不到绑定就什么都别拦（fail-open）。
            return false;
        }
        return gameplayKeys.contains(key) || gameplayKeys.contains(keycode);
    }

    /**
     * 把若干"永不拦"的键码折成一个集合 —— **绝不允许因为常量撞车而炸**。
     *
     * <p>真机事故（2026-10-12，用户实例启动即崩）：mixin 里写的是
     * {@code static final Set<Integer> ALWAYS_ALLOWED = Set.of(InputConstants.KEY_ESCAPE,
     * SDLScancode.SDL_SCANCODE_ESCAPE, SDLKeycode.SDLK_ESCAPE, …)}，六个常量里有两个**恰好等值**
     * （崩溃报告：{@code IllegalArgumentException: duplicate element: 41}）。{@code Set.of} 对重复元素
     * 是**抛异常**的，而 mixin 的静态初始化会**合并进目标类的 {@code <clinit>}**，于是异常出在
     * {@code KeyboardHandler.<clinit>} ⇒ **游戏在 {@code Minecraft.<init>} 阶段直接打不开**。
     *
     * <p>所以这里**去重**而不是 {@code Set.of}：多一个同义编码最多是"少拦一个键"，
     * 而抛异常是"玩家连世界都进不去"。重复值用 {@link #duplicateCodes(int...)} 报出来（自检断言）。
     */
    public static Set<Integer> allowed(int... codes) {
        Set<Integer> unique = new HashSet<>();
        if (codes != null) {
            for (int code : codes) {
                unique.add(code);
            }
        }
        return Set.copyOf(unique);
    }

    /** 哪些编码重复了（升序、不重复；给自检与排障用）。 */
    public static List<Integer> duplicateCodes(int... codes) {
        Set<Integer> seen = new HashSet<>();
        Set<Integer> dup = new TreeSet<>();
        if (codes != null) {
            for (int code : codes) {
                if (!seen.add(code)) {
                    dup.add(code);
                }
            }
        }
        return List.copyOf(dup);
    }
}
