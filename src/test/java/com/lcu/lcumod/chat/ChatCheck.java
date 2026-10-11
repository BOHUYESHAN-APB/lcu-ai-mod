package com.lcu.lcumod.chat;

/**
 * {@code verifyChat} 的统一入口（零依赖自检，替代 JUnit）：
 * 依次跑「聊天回声去重」({@link OutboundChatLogCheck}) 与「聊天分类」({@link ChatKindCheck})，
 * 汇总条数，任一失败即非零退出。
 */
public final class ChatCheck {

    private ChatCheck() {
    }

    public static void main(String[] args) {
        int[] a = OutboundChatLogCheck.run();
        int[] b = ChatKindCheck.run();
        int passed = a[0] + b[0];
        int failed = a[1] + b[1];
        System.out.println("[ChatCheck] TOTAL " + passed + " checks passed, " + failed + " failed");
        if (failed > 0) {
            System.exit(1);
        }
    }
}
