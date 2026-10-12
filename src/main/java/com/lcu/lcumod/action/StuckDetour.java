package com.lcu.lcumod.action;

/**
 * 卡住后的**有界绕行预算**（纯逻辑，不依赖任何 Minecraft 类型，便于零依赖自检 {@code WalkCheck}）。
 *
 * <p>用户口径：「卡住不要只停：先自动换路线（绕行）再试，试不动才如实报」。本类只负责两件事：
 *
 * <ul>
 *   <li><b>有界</b>：一个"卡住回合"最多 {@value #MAX_ATTEMPTS} 次绕行，用完即 {@link #exhausted()}
 *       —— 调用方（{@code ActionExecutor} 的卡住看门狗）据此决定"继续绕"还是"如实上报"，
 *       绝不会无限绕；</li>
 *   <li><b>每次换法真的不同</b>：第 n 次绕行的横向偏移按
 *       {@code 正负交替 * LATERAL_OFFSET * (1 + n/2)} 生成（+3.5 / -3.5 / +7.0 / -7.0 …）：
 *       方向交替、幅度递增 —— 不是把同一条路重算一遍。</li>
 * </ul>
 *
 * <p>"一个卡住回合" = 一段连续无位移；一旦重新探测到位移（{@link #reset()}）预算重置 ——
 * 与卡住看门狗的 {@code stuckTicks} 同一个生命周期（走出位移就重新开始计）。
 */
public final class StuckDetour {

    /** 一个卡住回合最多绕行几次（用户口径给的上界：试不动才如实报）。 */
    public static final int MAX_ATTEMPTS = 2;

    /** 单次绕行的横向偏移（格）：绕行落脚点落在"玩家→目标"连线的垂直方向上。 */
    public static final double LATERAL_OFFSET = 3.5;

    private final int maxAttempts;
    private int attempts = 0;

    public StuckDetour() {
        this(MAX_ATTEMPTS);
    }

    public StuckDetour(int maxAttempts) {
        if (maxAttempts < 1) {
            throw new IllegalArgumentException("maxAttempts must be >= 1");
        }
        this.maxAttempts = maxAttempts;
    }

    /** 还有额度可以再绕一次吗。 */
    public boolean canAttempt() {
        return attempts < maxAttempts;
    }

    /** 额度用完（调用方应如实上报，而不是继续绕）。 */
    public boolean exhausted() {
        return !canAttempt();
    }

    /** 本回合已经绕了几次。 */
    public int attempts() {
        return attempts;
    }

    public int maxAttempts() {
        return maxAttempts;
    }

    /**
     * 消耗一次绕行额度，并给出**这一次**的横向偏移（先正后负、幅度随次数递增 ⇒ 每次换法真的不同）。
     *
     * @throws IllegalStateException 预算已用完（调用方必须先问 {@link #canAttempt()}）
     */
    public double consumeOffset() {
        if (!canAttempt()) {
            throw new IllegalStateException("detour budget exhausted");
        }
        double magnitude = LATERAL_OFFSET * (1 + attempts / 2);
        double offset = (attempts % 2 == 0) ? magnitude : -magnitude;
        attempts++;
        return offset;
    }

    /** 走出位移 / 新的导航指令 ⇒ 新回合，额度重置。 */
    public void reset() {
        attempts = 0;
    }
}
