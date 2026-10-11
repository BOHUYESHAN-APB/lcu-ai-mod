package com.lcu.lcumod.action;

/**
 * 视角**单一写者**的仲裁核心（纯逻辑，不依赖任何 Minecraft 类型，便于零依赖自检
 * {@code verifyWalk}）。
 *
 * <p><b>为什么需要它</b>（用户实机 + 日志为证）：后端 wire 的 {@code look} 目标每 tick 被写进
 * 玩家朝向（{@code InputIsolation.applyAiControls}），**同时**寻路也在每 tick 写"往下一个路点
 * 转"（修复前在 {@code Pathfinder} 里直接写）——两个写者互相覆盖，寻路的 {@code yawDiff} 永不收敛
 * ⇒ 前进键永远是 false ⇒ "AI 在控制中、显示跟着我，但原地罚站"，并伴随每秒刷屏的
 * {@code Calculated path with 47 waypoints}。
 *
 * <p><b>规则（用户口径「同一时刻只有一个视角写者」）</b>：
 * <ol>
 *   <li>AI 不持控制 ⇒ 谁都不写（{@link Source#NONE}）——绝不抢玩家的视角；</li>
 *   <li><b>正在导航</b>（有路径）⇒ <b>以路径朝向为准</b>，后端的 {@code look} 绝对目标
 *       <b>不覆盖</b>它；</li>
 *   <li>没有导航 ⇒ 后端的 {@code look} 生效（绝对值、一次性目标，行为与修复前一致）；</li>
 *   <li>导航的朝向是**每 tick 逐步转**（限速 ±{@value #NAV_MAX_YAW_STEP}° 偏航 /
 *       ±{@value #NAV_MAX_PITCH_STEP}° 俯仰），**绝不**写成"瞬间对齐"（那会让 AI 抽风）。</li>
 * </ol>
 *
 * <p>本类只**计算**本 tick 应当写入什么，不真正写——真正的唯一写点在
 * {@code InputIsolation.tick}。
 */
public final class LookArbiter {

    /** 本 tick 视角的拥有者。 */
    public enum Source {
        /** 无人拥有：本 tick 什么都不写。 */
        NONE,
        /** 寻路（导航中的路径朝向）——优先级最高。 */
        NAVIGATION,
        /** 后端 wire 的 look 目标（绝对值、一次性）。 */
        BACKEND
    }

    /** 导航转身限速（与原版寻路同口径）：每 tick 最多 10°。 */
    public static final float NAV_MAX_YAW_STEP = 10.0f;
    /** 导航俯仰限速：每 tick 最多 6°。 */
    public static final float NAV_MAX_PITCH_STEP = 6.0f;
    /** 俯仰硬上限（避免写进非法角度）。 */
    public static final float PITCH_LIMIT = 90.0f;

    /**
     * 一 tick 的写入决策。
     *
     * @param source  谁拥有视角
     * @param wrote   本 tick 是否应当写入 yaw/pitch（{@code source==NONE} 时为 false）
     * @param yaw     应当写入的偏航
     * @param pitch   应当写入的俯仰
     * @param yawDiff 目标与当前偏航的带符号差（wrap 到 (-180,180]），用于对齐闸门
     */
    public record Frame(Source source, boolean wrote, float yaw, float pitch, float yawDiff) {}

    private boolean hasBackend = false;
    private float backendYaw = 0f;
    private float backendPitch = 0f;

    private boolean hasNavigation = false;
    private float navigationYaw = 0f;
    private float navigationPitch = 0f;

    // ── 目标设置 ──

    /** 后端 look：绝对视角目标（一次性），无导航时每 tick 会被写到该值（与修复前一致）。 */
    public void setBackendTarget(float yaw, float pitch) {
        hasBackend = true;
        backendYaw = yaw;
        backendPitch = clampPitch(pitch);
    }

    public void clearBackendTarget() {
        hasBackend = false;
    }

    public boolean hasBackendTarget() {
        return hasBackend;
    }

    /** 寻路目标：每 tick 由 {@code Pathfinder} 更新；导航期间优先级高于后端 look。 */
    public void setNavigationTarget(float yaw, float pitch) {
        hasNavigation = true;
        navigationYaw = yaw;
        navigationPitch = clampPitch(pitch);
    }

    public void clearNavigationTarget() {
        hasNavigation = false;
    }

    public boolean hasNavigationTarget() {
        return hasNavigation;
    }

    public void clearAll() {
        clearBackendTarget();
        clearNavigationTarget();
    }

    // ── 仲裁 ──

    /**
     * 本 tick 谁拥有视角。优先级：AI 不控制 ⇒ 无人；导航中 ⇒ 路径；否则 ⇒ 后端 look。
     */
    public Source resolveSource(boolean aiControlled, boolean navigating) {
        if (!aiControlled) {
            return Source.NONE;
        }
        if (navigating) {
            return hasNavigation ? Source.NAVIGATION : Source.NONE;
        }
        return hasBackend ? Source.BACKEND : Source.NONE;
    }

    /** 计算本 tick 应当写入的 yaw/pitch（纯计算，调用方负责真正写入）。 */
    public Frame plan(boolean aiControlled, boolean navigating, float currentYaw, float currentPitch) {
        Source source = resolveSource(aiControlled, navigating);
        switch (source) {
            case NAVIGATION -> {
                float yawDiff = yawDiff(currentYaw, navigationYaw);
                float pitchDiff = navigationPitch - currentPitch;
                float yaw = currentYaw + clamp(yawDiff, NAV_MAX_YAW_STEP);
                float pitch = clampPitch(currentPitch + clamp(pitchDiff, NAV_MAX_PITCH_STEP));
                return new Frame(Source.NAVIGATION, true, yaw, pitch, yawDiff);
            }
            case BACKEND -> {
                // 绝对值：一次性目标，直接对齐（不逐步转）。
                return new Frame(Source.BACKEND, true, backendYaw, clampPitch(backendPitch),
                        yawDiff(currentYaw, backendYaw));
            }
            default -> {
                return new Frame(Source.NONE, false, currentYaw, currentPitch, 0f);
            }
        }
    }

    // ── 纯函数 ──

    /** 目标与当前的带符号偏航差，wrap 到 (-180, 180]。 */
    public static float yawDiff(float currentYaw, float targetYaw) {
        float d = (targetYaw - currentYaw) % 360.0f;
        if (d > 180.0f) {
            d -= 360.0f;
        }
        if (d <= -180.0f) {
            d += 360.0f;
        }
        return d;
    }

    /** 把增量夹到 ±limit。 */
    public static float clamp(float value, float limit) {
        return Math.max(-limit, Math.min(limit, value));
    }

    /** 俯仰夹到 ±{@value #PITCH_LIMIT}。 */
    public static float clampPitch(float pitch) {
        return Math.max(-PITCH_LIMIT, Math.min(PITCH_LIMIT, pitch));
    }
}
