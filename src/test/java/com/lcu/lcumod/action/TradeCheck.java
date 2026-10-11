package com.lcu.lcumod.action;

import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;

/**
 * 村民交易的零依赖自检（接入 {@code check}，随 {@code build} 跑）。
 *
 * <p>两部分：① {@link TradePlan} 的纯逻辑（挑报价 / 成交判定 / 槽位常量）——它能被本自检
 * 加载，正是因为它**零依赖**（不碰 {@code net.minecraft}、不碰 Gson）；② 对主源码的静态扫描
 * （照 {@code WalkCheck} 的约定：cwd = 工程根）——把"不开界面""走原版那三步"这类不变量钉死。
 *
 * <p>断言信息用 ASCII（Windows 控制台 GBK）；失败非零退出。
 */
public final class TradeCheck {

    private static int passed = 0;
    private static int failed = 0;

    public static void main(String[] args) {
        slotChecks();
        selectionChecks();
        tradeChecks();
        sourceChecks();

        System.out.println("[TradeCheck] " + passed + " checks passed, " + failed + " failed");
        if (failed > 0) {
            System.exit(1);
        }
    }

    private static void slotChecks() {
        // 源码实证：MerchantMenu 的 PAYMENT1=0 / PAYMENT2=1 / RESULT=2。
        check("slots: payment1 is 0", TradePlan.PAYMENT1_SLOT == 0);
        check("slots: payment2 is 1", TradePlan.PAYMENT2_SLOT == 1);
        check("slots: result is 2", TradePlan.RESULT_SLOT == 2);
    }

    private static void selectionChecks() {
        List<TradePlan.Offer> offers = sampleOffers();

        TradePlan.Selection empty = TradePlan.select(List.of(), null, "");
        check("select: no offers is an honest no (code=" + empty.code() + ")",
                !empty.ok() && "no_offers".equals(empty.code()));

        TradePlan.Selection byIndex = TradePlan.select(offers, 1, "");
        check("select: by index picks that exact offer",
                byIndex.ok() && byIndex.offer().index() == 1);

        TradePlan.Selection badIndex = TradePlan.select(offers, 7, "");
        check("select: out-of-range index is refused (code=" + badIndex.code() + ")",
                !badIndex.ok() && "offer_index_out_of_range".equals(badIndex.code()));

        TradePlan.Selection byItem = TradePlan.select(offers, null, "EMERALD");
        check("select: item match is case-insensitive",
                byItem.ok() && byItem.offer().resultItem().contains("emerald"));

        TradePlan.Selection miss = TradePlan.select(offers, null, "netherite");
        check("select: unknown item is an honest no (code=" + miss.code() + ")",
                !miss.ok() && "offer_not_found".equals(miss.code()));

        TradePlan.Selection first = TradePlan.select(offers, null, "");
        check("select: no hint picks the first offer that still has stock",
                first.ok() && first.offer().index() == 0);

        List<TradePlan.Offer> soldOut = new ArrayList<>();
        for (TradePlan.Offer offer : offers) {
            soldOut.add(new TradePlan.Offer(offer.index(), offer.costAItem(), offer.costACount(),
                    offer.costBItem(), offer.costBCount(), offer.resultItem(), offer.resultCount(),
                    offer.maxUses(), offer.maxUses(), 0));
        }
        TradePlan.Selection none = TradePlan.select(soldOut, null, "");
        check("select: everything sold out is an honest no (code=" + none.code() + ")",
                !none.ok() && "all_sold_out".equals(none.code()));

        check("offer: sold-out detection", soldOut.get(0).soldOut() && !offers.get(0).soldOut());
        check("offer: second cost only when it is really there",
                offers.get(1).hasSecondCost() && !offers.get(0).hasSecondCost());
        check("offer: describe carries the numbers",
                offers.get(0).describe().contains("->") && offers.get(0).describe().contains("#0"));
    }

    private static void tradeChecks() {
        check("trade: result slot emptied means traded", TradePlan.tradeHappened(3, 2));
        check("trade: unchanged slot means NOT traded", !TradePlan.tradeHappened(3, 3));
        check("trade: empty-to-empty is NOT traded", !TradePlan.tradeHappened(0, 0));
    }

    private static void sourceChecks() {
        String plan = readSource("action/TradePlan.java");
        String executor = readSource("action/ActionExecutor.java");
        String controller = readSource("action/ContainerController.java");

        // 零依赖教训（token 那批）：纯逻辑类不许把 MC/Gson 拖进来，否则本自检加载不了它。
        // 判据看 **import**，不是"文中出现过这个字样"（javadoc 里说明"不碰 MC 类型"本身是好事 ✓）。
        check("scan: TradePlan stays dependency-free",
                !plan.isBlank() && !plan.contains("import net.minecraft")
                        && !plan.contains("import com.google.gson"));

        // 原版 MerchantScreen 选报价就这三件事（源码实证）——三件都得在。
        check("scan: executor selects the offer the vanilla way",
                executor.contains("setSelectionHint") && executor.contains("tryMoveItems")
                        && executor.contains("ServerboundSelectTradePacket"));

        // 交易全程不开界面（硬约束：全仓库 setScreen 必须为空，这里钉住交易这条路）。
        check("scan: trade never opens a screen",
                !executor.contains("setScreen") && !controller.contains("setScreen"));

        // 对实体做交互走的是容器层那一入口，不是自己造一套。
        check("scan: entity interact lives in the container controller",
                controller.contains("interactWithEntity") && controller.contains("gameMode.interact"));

        // 诚实失败：code 必须真的在源码里（不是只在文档里 ✗）。
        for (String code : List.of("no_villager", "out_of_range", "no_menu",
                "not_enough_items", "offer_unavailable", "trade_no_effect")) {
            check("scan: failure code present: " + code, executor.contains(code));
        }

        // 结果槽点击必须走窗口点击包（同一口径，可同步校验）。
        check("scan: result click goes through the container input packet",
                executor.contains("TradePlan.RESULT_SLOT, 0,")
                        && executor.contains("ContainerInput.PICKUP"));
    }

    private static List<TradePlan.Offer> sampleOffers() {
        List<TradePlan.Offer> offers = new ArrayList<>();
        offers.add(new TradePlan.Offer(0, "minecraft:wheat", 20, "", 0,
                "minecraft:emerald", 1, 3, 12, 0));
        offers.add(new TradePlan.Offer(1, "minecraft:paper", 24, "minecraft:book", 1,
                "minecraft:emerald", 1, 0, 16, 0));
        return offers;
    }

    /** 读主源码（cwd = 工程根，与 WalkCheck / ProtoCheck 同一约定）。 */
    private static String readSource(String relative) {
        try {
            return Files.readString(
                    Path.of("src", "main", "java", "com", "lcu", "lcumod", relative),
                    StandardCharsets.UTF_8);
        } catch (Exception e) {
            System.out.println("  INFO could not read " + relative + ": " + e.getMessage());
            return "";
        }
    }

    private static void check(String name, boolean ok) {
        if (ok) {
            passed++;
            System.out.println("  OK   " + name);
        } else {
            failed++;
            System.out.println("  FAIL " + name);
        }
    }
}
