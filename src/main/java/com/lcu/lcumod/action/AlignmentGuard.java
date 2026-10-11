package com.lcu.lcumod.action;

/**
 * 寻路"对齐闸门"的**收敛保护**（纯逻辑，不依赖任何 Minecraft 类型，便于零依赖自检
 * {@code verifyWalk}）。
 *
 * <p>背景：{@code moveToward} 只有在视角与路点方向对准（{@code |yawDiff| < 65°}）时才按下前进键，
 * 保留这个意图是对的；但如果因为任何原因**一直转不过去**（玩家的鼠标、碰撞体、其它反射抢写视角
 * ——修复前就是后端 look 每 tick 覆盖寻路的转身），原实现会**原地无限转**且**永远不前进**。
 *
 * <p>用户口径：转不过去就**如实上报**（"转不过去，可能被玩家操作/碰撞体卡住"）并**停步**，
 * 不许原地无限转，也不许假装在走。本类只做计数：连续 {@code timeoutTicks} 次未对准之后
 * 返回"超时"。
 */
public final class AlignmentGuard {

    private final int timeoutTicks;
    private int stalledTicks = 0;

    public AlignmentGuard(int timeoutTicks) {
        if (timeoutTicks < 1) {
            throw new IllegalArgumentException("timeoutTicks must be >= 1");
        }
        this.timeoutTicks = timeoutTicks;
    }

    /**
     * 记录本 tick 的对齐结果。
     *
     * @param aligned 本 tick 是否已对准（可以按前进键）
     * @return true 表示"超过 {@code timeoutTicks} 仍未对准"——调用方应如实停步并上报原因
     */
    public boolean tick(boolean aligned) {
        if (aligned) {
            stalledTicks = 0;
            return false;
        }
        stalledTicks++;
        return stalledTicks > timeoutTicks;
    }

    /** 已经连续多少 tick 未对准。 */
    public int stalledTicks() {
        return stalledTicks;
    }

    public int timeoutTicks() {
        return timeoutTicks;
    }

    public void reset() {
        stalledTicks = 0;
    }
}
