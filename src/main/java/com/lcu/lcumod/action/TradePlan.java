package com.lcu.lcumod.action;

import java.util.List;
import java.util.Locale;

/**
 * 村民交易的<b>纯逻辑</b>（零依赖，可被零依赖自检直接加载）。
 *
 * <p>为什么不碰 {@code net.minecraft}：零依赖自检（{@code verifyTrade}）加载不了带 MC/Gson
 * 依赖的类 —— 这是 token 那批踩过的坑（{@code NoClassDefFoundError: gson/JsonElement}）。
 * 所以这里只放"挑哪一条报价 / 这一击算不算成交 / 槽位常量"这类判定；MC 类型到本类的转换
 * 放在 {@link ActionExecutor} 里（那一侧本来就有 MC 依赖）。
 *
 * <p>槽位常量来自源码实证：{@code MerchantMenu} 的 {@code PAYMENT1_SLOT = 0}、
 * {@code PAYMENT2_SLOT = 1}、{@code RESULT_SLOT = 2}。
 */
public final class TradePlan {

    public static final int PAYMENT1_SLOT = 0;
    public static final int PAYMENT2_SLOT = 1;
    public static final int RESULT_SLOT = 2;

    private TradePlan() {}

    /** 一条报价的**纯数据**形状（不含任何 MC 类型）。 */
    public record Offer(int index, String costAItem, int costACount,
                        String costBItem, int costBCount,
                        String resultItem, int resultCount,
                        int uses, int maxUses, int priceDiff) {

        public boolean hasSecondCost() {
            return costBItem != null && !costBItem.isBlank() && costBCount > 0;
        }

        public boolean soldOut() {
            return maxUses > 0 && uses >= maxUses;
        }

        /** 给模型看的一行（人类可读 ✓ 不隐藏"第几条/要什么/给什么/用了多少"✓）。 */
        public String describe() {
            String cost = costACount + "x " + costAItem
                    + (hasSecondCost() ? " + " + costBCount + "x " + costBItem : "");
            return "#" + index + " " + cost + " -> " + resultCount + "x " + resultItem
                    + (maxUses > 0 ? " (" + uses + "/" + maxUses + ")" : "");
        }
    }

    /** 挑报价的结果：要么挑中一条，要么**如实**说明为什么挑不中（{@code code} 机器可读）。 */
    public record Selection(Offer offer, String code, String message) {
        public boolean ok() {
            return offer != null;
        }
    }

    /**
     * 挑一条报价：给了 {@code index} 就按序号 ✓；否则按 {@code item} 模糊匹配**结果物** ✓；
     * 两个都没给 ⇒ 挑第一条**还有货**的 ✓。
     *
     * <p>挑不中一律给机器可读的 code，绝不编一条出来 ✗：{@code no_offers} /
     * {@code offer_index_out_of_range} / {@code offer_not_found} / {@code all_sold_out}。
     */
    public static Selection select(List<Offer> offers, Integer index, String item) {
        if (offers == null || offers.isEmpty()) {
            return new Selection(null, "no_offers", "这个村民没有可用报价");
        }
        if (index != null) {
            for (Offer offer : offers) {
                if (offer.index() == index) {
                    return new Selection(offer, null, null);
                }
            }
            return new Selection(null, "offer_index_out_of_range",
                    "报价序号 " + index + " 不存在（共 " + offers.size() + " 条）");
        }
        String want = item == null ? "" : item.trim().toLowerCase(Locale.ROOT);
        if (!want.isEmpty()) {
            for (Offer offer : offers) {
                if (offer.resultItem() != null
                        && offer.resultItem().toLowerCase(Locale.ROOT).contains(want)) {
                    return new Selection(offer, null, null);
                }
            }
            return new Selection(null, "offer_not_found", "没有结果物匹配 \"" + item + "\" 的报价");
        }
        for (Offer offer : offers) {
            if (!offer.soldOut()) {
                return new Selection(offer, null, null);
            }
        }
        return new Selection(null, "all_sold_out", "所有报价都用完了（等补货）");
    }

    /**
     * 成交判定：点完结果槽之后结果槽**被拿走**（数量变少）就算成交 ✓。
     *
     * <p>与容器点击同一口径 —— {@code handleContainerInput} 会把点击同步应用到客户端菜单，
     * 所以立刻就能看出来；看不出来 ⇒ 如实报 {@code trade_no_effect}，绝不谎报成功 ✗。
     */
    public static boolean tradeHappened(int resultCountBefore, int resultCountAfter) {
        return resultCountAfter < resultCountBefore;
    }
}
