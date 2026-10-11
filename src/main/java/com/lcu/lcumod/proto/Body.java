package com.lcu.lcumod.proto;

import java.util.List;

/**
 * 体（Body）抽象 —— 决定"控制回路跑在哪"，是客户端本地玩家与服务端假玩家之间**唯一**的接缝。
 *
 * <p>同一套上层逻辑（后端协议、记忆分层、工具集、行为树）应当能原封不动地驱动任意一种体：
 * 今天跑在客户端本地玩家上（{@code ClientBody}），将来跑在服务端假玩家上（{@code ServerBody}）。
 * 本接口只描述**体的能力面**，不含任何渲染 / 输入设备 / 网络假设。
 *
 * <h2>三条规矩（现在就守，老代码不必一次性搬）</h2>
 * <ol>
 *   <li><b>新写的动作只准调 {@code Body}</b>，不得再直接 {@code Minecraft.getInstance()}
 *       —— 老代码可暂留，但**不许再新增**这种直连。</li>
 *   <li><b>P0 生存反射、区域硬拦、出站清洗一律在 {@code Body} 之上</b>，由两种体共同经过；
 *       <b>绝不</b>把它们塞进 {@code Body} 的实现里（实现只负责"把动作做出去"）。</li>
 *   <li><b>出口在进入 {@code Body} 之前就已清洗</b>：{@link #sendChat(String)} /
 *       {@link #runCommand(String)} 的调用方负责先跑出站清洗与指令黑名单；实现只做传输，
 *       不承担策略（与规矩 ② 一致 —— 清洗属于 {@code Body} 之上的层）。</li>
 * </ol>
 *
 * <h2>两种实现</h2>
 * <ul>
 *   <li>{@code ClientBody} —— 本地玩家。动作走客户端 tick / 按键语义（**今天的方式**）。</li>
 *   <li>{@code ServerBody} —— 服务端假玩家。无渲染、无输入设备，动作直接改实体状态
 *       （**本批不实现**，仅预留本接口）。</li>
 * </ul>
 *
 * <p><b>本批只建接口 + 文档</b>：现有代码**不**改成用 {@code Body}。
 * 签名里刻意只用 {@code java.*} 与 {@link BlockTarget} 等本包值类型，
 * 不出现任何 {@code net.minecraft} 类型，以免把 Minecraft 依赖烙进抽象层。
 */
public interface Body {

    // ── 身份 ──────────────────────────────────────────────────────

    /** 体类型：{@code "client"}（本地玩家）或 {@code "server"}（服务端假玩家）。 */
    String profile();

    // ── 位置 / 朝向 ───────────────────────────────────────────────

    double x();

    double y();

    double z();

    /** 水平朝向（度）。 */
    float yaw();

    /** 俯仰（度）。 */
    float pitch();

    /** 直接把朝向设到给定值（等价今天的 look 指令）。 */
    void setRotation(float yaw, float pitch);

    /** 朝世界坐标点看（含俯仰）。 */
    void lookAt(double x, double y, double z);

    // ── 移动姿态 ─────────────────────────────────────────────────

    void setSneaking(boolean sneaking);

    void setSprinting(boolean sprinting);

    /** 起跳一次。 */
    void jump();

    // ── 按住 / 松开语义（复用现有 hold 语义）──────────────────────

    /**
     * 按住某个控制若干 tick。
     *
     * @param control 逻辑控制名，形如 {@code "forward"} / {@code "attack"} / {@code "use"}。
     * @param ticks   按住时长；实现到期后自行松开。
     */
    void press(String control, int ticks);

    /** 松开某个控制。 */
    void release(String control);

    /** 松开全部控制（安全释放）。 */
    void releaseAll();

    // ── 背包与容器 ───────────────────────────────────────────────

    /** 背包物品快照（实现只读返回，调用方不得回写）。 */
    List<ItemStack> inventory();

    /** 当前选中热键栏槽位（0..8）。 */
    int selectedHotbarSlot();

    void selectHotbarSlot(int slot);

    /** 打开世界坐标处的容器并返回其内容视图；失败返回 {@code null}。 */
    ContainerView openContainer(int x, int y, int z);

    /** 对已打开的容器执行一次点击。 */
    void clickSlot(int slot, int button, ClickMode mode);

    /** 关闭当前容器界面（若有）。 */
    void closeContainer();

    // ── 看向 / 交互 / 攻击 ────────────────────────────────────────

    /** 对目标方块面使用当前手持物。 */
    void useOn(BlockTarget target);

    /** 使用当前手持物（右键空气语义）。 */
    void useItem();

    /** 攻击实体（按后端可见的实体 id 定位）。 */
    void attack(long entityId);

    // ── 出口（调用方在 Body 之上完成出站清洗）─────────────────────

    /** 发送聊天文本（调用方须先过出站清洗；实现只做传输）。 */
    void sendChat(String text);

    /** 发送指令（调用方须先过清洗 + 指令黑名单；实现只做传输）。 */
    void runCommand(String command);

    // ── 值类型（刻意不含 Minecraft 类型）─────────────────────────

    /** 背包/容器中的一格。 */
    record ItemStack(String id, int count) {
    }

    /** 容器内容视图（附其世界坐标，便于回溯）。 */
    record ContainerView(int x, int y, int z, List<ItemStack> slots) {
    }

    /** 交互目标：方块坐标 + 面（{@code "up"/"down"/"north"/...}）。 */
    record BlockTarget(int x, int y, int z, String face) {
    }

    /** 容器点击模式，对应原版三种点法 + 丢弃。 */
    enum ClickMode {
        PICKUP,
        QUICK_MOVE,
        SWAP,
        THROW
    }
}
