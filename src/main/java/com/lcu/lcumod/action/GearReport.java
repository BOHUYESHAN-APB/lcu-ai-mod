package com.lcu.lcumod.action;

import com.google.gson.JsonArray;
import com.google.gson.JsonElement;
import com.google.gson.JsonNull;
import com.google.gson.JsonObject;
import com.lcu.lcumod.LCUMod;
import net.minecraft.client.Minecraft;
import net.minecraft.core.registries.BuiltInRegistries;
import net.minecraft.resources.Identifier;
import net.minecraft.server.packs.resources.Resource;
import net.minecraft.world.entity.EquipmentSlot;
import net.minecraft.world.entity.player.Player;
import net.minecraft.world.item.ItemStack;

import java.io.InputStream;
import java.util.ArrayList;
import java.util.Base64;
import java.util.HashSet;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Optional;
import java.util.Set;

/**
 * 自己身上的**装备与耐久事实** + 用得上的**原版贴图**（base64，运行期从用户自己的游戏里读）。
 *
 * <p>用户口径："OBS 叠加页要更明显地显示自己的装备、显示装备的耐久度、主要攻击类武器的耐久；
 * **如果能的话把原版材质贴图拿过来用**"。
 *
 * <p>两条诚实边界（别让人以为它什么都管）：
 * <ul>
 *   <li>贴图是 Mojang 资产，**不进仓库**：只在运行时从客户端资源管理器读
 *       {@code textures/item/<path>.png}，base64 发一次；**读不到就跳过这个 id**
 *       （不编、不占位），后端/web 退回文字显示；</li>
 *   <li>耐久三元组必须自洽：{@code durability = max_durability - damage}；不可损坏的东西
 *       （方块/食物）如实报 {@code damageable:false} 且三个数全 0，不编一个上限出来。</li>
 * </ul>
 *
 * <p>分两半：{@link Pure} 是**零依赖**的取值/自洽逻辑（自检直接测这一半，不带任何游戏类，
 * 所以能在零依赖的自检 JVM 里跑）；本类其余部分碰游戏（注册名、装备槽、客户端资源管理器）。
 */
public final class GearReport {

    private GearReport() {
    }

    // ── 零依赖的一半：取值与自洽（自检直接断言这一段的行为）────────────────────

    /**
     * 纯逻辑：不给任何游戏类，只有字符串与整数。
     *
     * <p>单独一个嵌套类是**故意**的：自检 JVM 的类路径上没有游戏类，只有这一半能被加载并直接测；
     * 跟游戏打交道的那一半用静态扫描钉住形态，不必把游戏拉进自检。
     */
    public static final class Pure {

        /** 装备槽显示顺序：**固定四槽，顺序 head, chest, legs, feet**（空槽也上报，不让 web 去猜）。 */
        public static final String[] ARMOR_SLOTS = {"head", "chest", "legs", "feet"};

        /** 一个槽位的耐久事实；空槽 = {@link #EMPTY_SLOT}（id/名字空、count 0、三个数全 0）。 */
        public record SlotFacts(String itemId, String name, int count, boolean damageable,
                                int durability, int maxDurability, int damage) {

            /** 槽是空的（没有物品）。 */
            public boolean isEmpty() {
                return itemId.isEmpty();
            }

            /** 三元组自洽：可损坏 ⇒ 剩余 = 上限 - 已损；不可损坏 ⇒ 三个数全 0。 */
            public boolean selfConsistent() {
                if (!damageable) {
                    return durability == 0 && maxDurability == 0 && damage == 0;
                }
                return maxDurability > 0 && damage >= 0 && damage <= maxDurability
                        && durability == maxDurability - damage;
            }
        }

        /** 空槽的规范值（**不是**缺字段：四槽永远在，空槽给零值对象）。 */
        public static final SlotFacts EMPTY_SLOT = new SlotFacts("", "", 0, false, 0, 0, 0);

        private Pure() {
        }

        /**
         * 取值/自洽：由"可损坏？已损？上限？"推出上报用的三元组。
         *
         * <p>不可损坏（方块/食物）**如实**报 {@code damageable:false} 且三个数全 0——不编上限；
         * 可损坏则钳到 {@code [0, max]}，保证 {@code durability = max_durability - damage} 永远成立
         * （游戏给的畸形值也不许漏出去）。
         */
        public static SlotFacts facts(String itemId, String name, int count, boolean damageable,
                                      int damage, int maxDamage) {
            String id = itemId == null ? "" : itemId;
            String label = name == null ? "" : name;
            int n = Math.max(0, count);
            if (!damageable || maxDamage <= 0) {
                // 上限为 0 的"可损坏物"是畸形的 ⇒ 如实按不可损坏报，而不是编一个上限。
                return new SlotFacts(id, label, n, false, 0, 0, 0);
            }
            int dmg = Math.min(Math.max(0, damage), maxDamage);
            return new SlotFacts(id, label, n, true, maxDamage - dmg, maxDamage, dmg);
        }

        /**
         * 六个槽（四件装备 + 主手 + 副手，顺序固定）的**状态指纹**：物品 id + 数量 + 已损都要体现，
         * 这样"耐久掉了一格"也会立刻触发一次状态上报（OBS 上的耐久条不许停在旧值）。
         */
        public static String slotKey(List<SlotFacts> slots) {
            if (slots == null) {
                return "";
            }
            StringBuilder sb = new StringBuilder();
            for (SlotFacts f : slots) {
                if (sb.length() > 0) {
                    sb.append('|');
                }
                if (f == null || f.isEmpty()) {
                    sb.append('-');
                    continue;
                }
                sb.append(f.itemId()).append('*').append(f.count()).append('@').append(f.damage());
            }
            return sb.toString();
        }
    }

    // ── Minecraft 侧：注册名/装备槽/贴图 ──────────────────────────────────────

    /** 本次会话里**已经成功发出去**的贴图 id（装备集合反复变也不重发）。 */
    private static final Set<String> sentIcons = new HashSet<>();

    /** 读过但资源里没有 / 读不动的 id：记住，别每次装备变化都再读一次盘。 */
    private static final Set<String> unavailableIcons = new HashSet<>();

    /** 上次检查贴图时的装备集合指纹（集合没变 ⇒ 连一个字节都不发）。 */
    private static String lastIconKey = "";

    /** 把一条槽位事实写成上报对象（web 只按 key 取值，顺序不影响）。 */
    public static JsonObject toJson(Pure.SlotFacts f) {
        JsonObject o = new JsonObject();
        o.addProperty("item_id", f.itemId());
        o.addProperty("name", f.name());
        o.addProperty("count", f.count());
        o.addProperty("durability", f.durability());
        o.addProperty("max_durability", f.maxDurability());
        o.addProperty("damage", f.damage());
        o.addProperty("damageable", f.damageable());
        return o;
    }

    /** 四件装备对应的装备槽，**顺序与 {@link Pure#ARMOR_SLOTS} 一一对应**。 */
    private static EquipmentSlot[] armorEquipment() {
        return new EquipmentSlot[]{EquipmentSlot.HEAD, EquipmentSlot.CHEST, EquipmentSlot.LEGS, EquipmentSlot.FEET};
    }

    /** 一个物品栈 → 槽位事实（空栈 ⇒ {@link Pure#EMPTY_SLOT}）。 */
    public static Pure.SlotFacts factsOf(ItemStack stack) {
        if (stack == null || stack.isEmpty()) {
            return Pure.EMPTY_SLOT;
        }
        boolean damageable = stack.isDamageableItem();
        String id = BuiltInRegistries.ITEM.getKey(stack.getItem()).toString();
        return Pure.facts(id, stack.getDisplayName().getString(), stack.getCount(), damageable,
                damageable ? stack.getDamageValue() : 0,
                damageable ? stack.getMaxDamage() : 0);
    }

    /** 现取六槽事实：四件装备（固定顺序）+ 主手 + 副手；玩家不在 ⇒ 六个空槽。 */
    private static List<Pure.SlotFacts> currentSlots(Player p) {
        List<Pure.SlotFacts> slots = new ArrayList<>(6);
        EquipmentSlot[] armor = armorEquipment();
        for (int i = 0; i < Pure.ARMOR_SLOTS.length; i++) {
            slots.add(p == null ? Pure.EMPTY_SLOT : factsOf(p.getItemBySlot(armor[i])));
        }
        slots.add(p == null ? Pure.EMPTY_SLOT : factsOf(p.getItemBySlot(EquipmentSlot.MAINHAND)));
        slots.add(p == null ? Pure.EMPTY_SLOT : factsOf(p.getItemBySlot(EquipmentSlot.OFFHAND)));
        return slots;
    }

    /** 六槽现状的状态指纹（耐久/数量变了也要立刻上报）——见 {@link Pure#slotKey(List)}。 */
    public static String slotKey(Player p) {
        return Pure.slotKey(currentSlots(p));
    }

    /**
     * `state_update` 里的 `gear`：
     * <pre>
     * {"armor":[{slot,item_id,name,count,durability,max_durability,damage,damageable} × 四槽],
     *  "main_hand":{…}|null, "off_hand":{…}|null}
     * </pre>
     * 空装备槽**照样给**零值对象（不让 web 去猜）；空手/空副手给 JSON null。
     */
    public static JsonObject build(Minecraft mc) {
        List<Pure.SlotFacts> slots = currentSlots(mc == null ? null : mc.player);
        JsonObject gear = new JsonObject();

        JsonArray armor = new JsonArray();
        for (int i = 0; i < Pure.ARMOR_SLOTS.length; i++) {
            JsonObject o = new JsonObject();
            o.addProperty("slot", Pure.ARMOR_SLOTS[i]);
            JsonObject facts = toJson(slots.get(i));
            for (String key : facts.keySet()) {
                o.add(key, facts.get(key));
            }
            armor.add(o);
        }
        gear.add("armor", armor);
        gear.add("main_hand", handJson(slots.get(4)));
        gear.add("off_hand", handJson(slots.get(5)));
        return gear;
    }

    /** 手上没东西 ⇒ JSON null（不是空对象，也不是字符串 "null"）。 */
    private static JsonElement handJson(Pure.SlotFacts f) {
        return f.isEmpty() ? JsonNull.INSTANCE : toJson(f);
    }

    // ── 原版贴图（运行期从用户自己的客户端资源里读，不进仓库）──────────────────

    /**
     * 只发**当前装备槽 + 主/副手**里出现的物品；装备集合变了才检查一次，
     * 已经发过的 id（{@link #sentIcons}）不重发，读不到的直接跳过（不编、不占位）。
     */
    public static void publishIcons(Minecraft mc, Player p) {
        if (mc == null || p == null || LCUMod.WIRE == null || !LCUMod.WIRE.isConnected()) {
            return;
        }
        List<String> ids = equippedIds(p);
        String key = String.join(";", ids);
        if (key.equals(lastIconKey)) {
            return;   // 装备集合没变 ⇒ 一个字节都不发（不是每帧发）
        }
        lastIconKey = key;

        JsonObject icons = new JsonObject();
        for (String id : ids) {
            if (sentIcons.contains(id) || unavailableIcons.contains(id)) {
                continue;
            }
            String base64 = readIconBase64(mc, id);
            if (base64 == null) {
                // 读不到（资源包里没这张图 / 读失败）：**跳过这个 id**，web 退回文字显示。
                unavailableIcons.add(id);
                continue;
            }
            icons.addProperty(id, base64);
            sentIcons.add(id);
        }
        if (icons.size() == 0) {
            return;   // 没有新贴图 ⇒ 不发空事件
        }
        JsonObject data = new JsonObject();
        data.add("icons", icons);
        LCUMod.WIRE.sendEvent("item_icons", data);
    }

    /** 当前装备槽 + 主/副手里的物品 id（固定顺序、去重——同一件装备出现在两处也只发一次）。 */
    private static List<String> equippedIds(Player p) {
        Set<String> ids = new LinkedHashSet<>();
        for (Pure.SlotFacts f : currentSlots(p)) {
            if (!f.isEmpty()) {
                ids.add(f.itemId());
            }
        }
        return new ArrayList<>(ids);
    }

    /**
     * 从客户端资源管理器读 {@code <namespace>:textures/item/<path>.png} 并 base64。
     *
     * <p>读不到一律返回 {@code null}（调用方跳过这个 id）：**不编、不占位**。
     * 只用原版 asset 路径的朴素映射 {@code item id → textures/item/<id path>.png}；
     * 特殊渲染的物品（盾牌/旗帜/药水等）与方块类物品本来就不走这条路径，如实缺席。
     */
    private static String readIconBase64(Minecraft mc, String itemId) {
        int colon = itemId.indexOf(':');
        if (colon <= 0 || colon == itemId.length() - 1) {
            return null;
        }
        String namespace = itemId.substring(0, colon);
        String path = itemId.substring(colon + 1);
        try {
            Identifier texture = Identifier.fromNamespaceAndPath(namespace, "textures/item/" + path + ".png");
            Optional<Resource> resource = mc.getResourceManager().getResource(texture);
            if (resource.isEmpty()) {
                return null;
            }
            try (InputStream in = resource.get().open()) {
                byte[] png = in.readAllBytes();
                // 只把真 PNG 当贴图发出去（魔数核对），别把别的东西塞进 icons 里。
                if (png.length < 8
                        || (png[0] & 0xFF) != 0x89 || png[1] != 'P' || png[2] != 'N' || png[3] != 'G') {
                    return null;
                }
                return Base64.getEncoder().encodeToString(png);
            }
        } catch (Exception e) {
            return null;
        }
    }
}
