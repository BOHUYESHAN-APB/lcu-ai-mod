package com.lcu.lcumod.client;

/**
 * HUD 状态框与窗口标题共用的「当前在干什么」词汇表（**纯逻辑**，可被 verifyPhrase 自检）。
 *
 * 用户要求（原话）：「AI 控制中要是能显示当前的状态，跑路中/逃路中…还是不要写寻路了。
 * 赶路中/逃路中要写得有点趣味性。」→ 一律用玩家看得懂的话，不出现"寻路/导航"这类工程词。
 *
 * 优先级从"命"到"闲"：着火/岩浆 > 憋气 > 打架 > 睡觉 > 任务 > 赶路 > 遛弯 > 发呆。
 */
public final class ActivityPhrase {

    public static final String IDLE = "发呆中";

    private ActivityPhrase() {
    }

    /**
     * @param onFireOrLava 着火或在岩浆里（最高优先级——命比什么都重要）
     * @param underwater   头在水下
     * @param inCombat     正在还手
     * @param sleeping     正在走去睡 / 已在睡
     * @param taskKind     ActionExecutor 的 activeTaskKind（可为空）
     * @param navigating   Pathfinder 正在赶路
     * @param moving       有位移
     */
    public static String of(boolean onFireOrLava, boolean underwater, boolean inCombat,
                            boolean sleeping, String taskKind, boolean navigating, boolean moving) {
        if (onFireOrLava) {
            return "逃命中";
        }
        if (underwater) {
            return "憋气中";
        }
        if (inCombat) {
            return "跟它干上了";
        }
        if (sleeping) {
            return "找床睡觉中";
        }
        switch (taskKind == null ? "" : taskKind.trim().toLowerCase()) {
            case "sleep":
                return "找床睡觉中";
            case "collect":
                return "捡东西中";
            case "craft":
                return "造东西中";
            case "follow":
                return "跟着你呢";
            case "travel":
                return "赶路中";
            case "eat":
                return "吃东西中";
            case "trade":
                return "做买卖中";
            case "mine", "dig":
                return "挖矿中";
            case "guard":
                return "守着这里";
            case "build":
                return "盖房子中";
            default:
                break;
        }
        if (navigating) {
            return "赶路中";
        }
        if (moving) {
            return "遛弯中";
        }
        return IDLE;
    }
}
