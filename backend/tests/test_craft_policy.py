"""制作/取物的能力位与库存优先策略（`agent.permissions` 能力位 + `agent.craft`）。

用户口径（本文件的验收对象）：
  - 造东西只有**被允许**才能造；翻箱子要**另外**授权；
  - **没有自主开关**：没人指挥的轮次按 guest 处理（默认不许自造）；
  - 自己想造时**优先只用背包里的材料**；
  - 被授权翻箱时**用多少取多少**（不搬空）。

时间/IO 无关，全是纯函数与临时文件权限表，跑得飞快。
"""

import json
import tempfile
import unittest
from pathlib import Path

from agent.craft import (ARMOR_SLOT_ORDER, RECIPES, SLOT_ARMOR_END, SLOT_ARMOR_START,
                         SLOT_BACKPACK_END, SLOT_BACKPACK_START, SLOT_HOTBAR_END,
                         SLOT_HOTBAR_START, SLOT_OFFHAND, armor_in_containers, clamp_take,
                         describe_missing, evaluate_craft, inventory_counts, is_armor,
                         normalize_item, route_containers)
from agent.permissions import (CONTAINER_AUTHORITY, CRAFT_AUTHORITY,
                               TIER_FRIEND, TIER_GUEST, TIER_MASTER, CraftPolicy,
                               PermissionsStore)


class _StoreBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "permissions.json"
        self.store = PermissionsStore(self.path)


# ── 1. 能力位（craft / containers）────────────────────────────────

class CapabilityTableTests(_StoreBase):
    def test_defaults_are_deny_for_guest(self):
        craft = self.store.craft_authority()
        containers = self.store.container_authority()
        self.assertTrue(craft[TIER_MASTER])
        self.assertTrue(craft[TIER_FRIEND])
        self.assertFalse(craft[TIER_GUEST], "guest 默认不许让他造东西")
        self.assertTrue(containers[TIER_MASTER])
        self.assertTrue(containers[TIER_FRIEND])
        self.assertFalse(containers[TIER_GUEST], "guest 默认不许授权翻箱")

    def test_capability_defaults_are_exported_tables(self):
        self.assertEqual(CRAFT_AUTHORITY[TIER_GUEST], False)
        self.assertEqual(CONTAINER_AUTHORITY[TIER_FRIEND], True)

    def test_missing_file_keeps_capability_keys(self):
        data = self.store.get()
        self.assertIn("craft", data)
        self.assertIn("containers", data)
        # 自主开关已被删除：权限表里不再有 autonomy_craft 这个键。
        self.assertNotIn("autonomy_craft", data)

    def test_tier_policy_carries_capability_bits(self):
        master = self.store.policy(TIER_MASTER)
        guest = self.store.policy(TIER_GUEST)
        self.assertTrue(master.can_craft)
        self.assertTrue(master.can_use_containers)
        self.assertFalse(guest.can_craft)
        self.assertFalse(guest.can_use_containers)
        self.assertEqual(guest.can_craft, CRAFT_AUTHORITY[TIER_GUEST])

    def test_capability_list_form_grants_only_named_tiers(self):
        self.store.set({"craft": ["master"]})
        craft = self.store.craft_authority()
        self.assertTrue(craft[TIER_MASTER])
        self.assertFalse(craft[TIER_FRIEND], "名单里没写的一律不放行")
        self.assertFalse(craft[TIER_GUEST])

    def test_capability_partial_dict_keeps_other_cells(self):
        self.store.set({"containers": {"guest": True}})
        containers = self.store.container_authority()
        self.assertTrue(containers[TIER_GUEST])
        self.assertTrue(containers[TIER_MASTER], "没写的格回退默认")

    def test_capability_garbage_falls_back_to_defaults(self):
        self.store.set({"craft": "yes please", "containers": 123})
        self.assertEqual(self.store.craft_authority(), CRAFT_AUTHORITY)
        self.assertEqual(self.store.container_authority(), CONTAINER_AUTHORITY)

    def test_capability_survives_disk_roundtrip(self):
        self.store.set({"craft": ["friend"]})
        reopened = PermissionsStore(self.path)
        self.assertFalse(reopened.can_craft(TIER_MASTER))
        self.assertTrue(reopened.can_craft(TIER_FRIEND))
        on_disk = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertNotIn("autonomy_craft", on_disk, "自主开关不再落盘")
        self.assertEqual(on_disk["craft"][TIER_FRIEND], True)


# ── 2. craft_policy：一次请求的授权结论 ────────────────────────────

class CraftPolicyTests(_StoreBase):
    def test_master_may_craft_and_open_containers(self):
        policy = self.store.craft_policy(TIER_MASTER)
        self.assertIsInstance(policy, CraftPolicy)
        self.assertEqual(policy.tier, TIER_MASTER)
        self.assertTrue(policy.may_craft)
        self.assertTrue(policy.may_open_containers)

    def test_guest_is_denied(self):
        policy = self.store.craft_policy(TIER_GUEST)
        self.assertFalse(policy.may_craft)
        self.assertFalse(policy.may_open_containers)
        self.assertEqual(policy.reason, "tier_not_allowed")

    def test_unknown_tier_degrades_to_guest_deny(self):
        policy = self.store.craft_policy("admin")
        self.assertEqual(policy.tier, TIER_GUEST)
        self.assertFalse(policy.may_craft)

    def test_no_commander_is_treated_as_guest_and_never_opens_containers(self):
        """没人指挥（传空 tier）→ 按 guest 处理：不许造、绝不许翻箱（没有自主开关）。"""
        policy = self.store.craft_policy("")
        self.assertEqual(policy.tier, TIER_GUEST)
        self.assertFalse(policy.may_craft)
        self.assertEqual(policy.reason, "tier_not_allowed")
        self.assertFalse(policy.may_open_containers, "没人指挥时绝不许翻箱")


# ── 3. 库存优先：材料评估 ──────────────────────────────────────────

class InventoryEvaluationTests(unittest.TestCase):
    def test_normalize_item_strips_namespace_and_case(self):
        self.assertEqual(normalize_item("minecraft:Oak_Log"), "oak_log")
        self.assertEqual(normalize_item("  IRON_INGOT "), "iron_ingot")
        self.assertEqual(normalize_item(None), "")

    def test_inventory_counts_reads_name_and_item_id(self):
        counts = inventory_counts([
            {"name": "minecraft:oak_log", "count": 3},
            {"item_id": "oak_log", "count": 2},
            {"name": "stick", "count": "4"},
            {"name": "junk"},          # 没数量 → 忽略
            {"count": 9},              # 没名字 → 忽略
            "not a dict",
        ])
        self.assertEqual(counts["oak_log"], 5)
        self.assertEqual(counts["stick"], 4)
        self.assertEqual(len(counts), 2)

    def test_sufficient_materials_are_detected(self):
        plan = evaluate_craft("wooden_sword", 1, [{"name": "oak_planks", "count": 2},
                                                  {"name": "stick", "count": 4}])
        self.assertTrue(plan.known)
        self.assertTrue(plan.sufficient)
        self.assertEqual(plan.missing, {})
        self.assertEqual(plan.required, {"oak_planks": 2, "stick": 1})

    def test_count_scales_the_requirement(self):
        plan = evaluate_craft("stick", 4, [{"name": "oak_planks", "count": 5}])
        self.assertEqual(plan.required, {"oak_planks": 8})
        self.assertEqual(plan.missing, {"oak_planks": 3})
        self.assertFalse(plan.sufficient)
        self.assertEqual(describe_missing(plan), "oak_planks x3")

    def test_missing_uses_total_inventory_including_hotbar(self):
        # Java 的 inventory 快照本来就是整背包（含快捷栏），这里验证多格会累加。
        plan = evaluate_craft("iron_pickaxe", 1, [{"name": "iron_ingot", "count": 1},
                                                  {"name": "minecraft:iron_ingot", "count": 1},
                                                  {"name": "stick", "count": 2}])
        self.assertEqual(plan.missing, {"iron_ingot": 1})

    def test_unknown_recipe_is_reported_as_unknown_not_guessed(self):
        plan = evaluate_craft("beacon", 1, [{"name": "nether_star", "count": 1}])
        self.assertFalse(plan.known)
        self.assertEqual(plan.required, {})
        self.assertEqual(plan.missing, {})
        self.assertTrue(plan.sufficient)  # 没配方可评估 → 交给 Java 的原版配方判断

    def test_bad_count_and_inventory_never_raise(self):
        plan = evaluate_craft("stick", "很多", "not a list")
        self.assertEqual(plan.count, 1)
        self.assertEqual(plan.missing, {"oak_planks": 2})

    def test_armor_recipes_use_the_right_materials(self):
        self.assertEqual(RECIPES["iron_chestplate"], {"iron_ingot": 8})
        self.assertEqual(RECIPES["diamond_helmet"], {"diamond": 5})

    def test_slots_match_the_java_container_zero_layout(self):
        self.assertEqual((SLOT_ARMOR_START, SLOT_ARMOR_END), (5, 8))
        self.assertEqual((SLOT_BACKPACK_START, SLOT_BACKPACK_END), (9, 35))
        self.assertEqual((SLOT_HOTBAR_START, SLOT_HOTBAR_END), (36, 44))
        self.assertEqual(SLOT_OFFHAND, 45)
        self.assertEqual(ARMOR_SLOT_ORDER, (5, 6, 7, 8))


# ── 4. 获授权翻箱：去哪、拿多少 ───────────────────────────────────

class ContainerRoutingTests(unittest.TestCase):
    def test_route_picks_nearest_and_takes_only_the_shortfall(self):
        containers = [
            {"block_id": "minecraft:chest", "x": 40, "y": 64, "z": 0, "distance": 40,
             "contents": [{"item_id": "minecraft:iron_ingot", "count": 64}]},
            {"block_id": "minecraft:barrel", "x": 4, "y": 64, "z": 4, "distance": 5,
             "contents": [{"item_id": "minecraft:iron_ingot", "count": 64}]},
        ]
        picks = route_containers({"iron_ingot": 5}, containers)

        self.assertEqual(len(picks), 1)
        self.assertEqual(picks[0].block_id, "minecraft:barrel")  # 最近的优先
        self.assertEqual(picks[0].take, {"iron_ingot": 5})       # 只要缺的 5 个，不搬空

    def test_route_splits_across_containers_and_never_exceeds_need(self):
        containers = [
            {"block_id": "chest", "x": 1, "y": 64, "z": 1, "distance": 2,
             "contents": [{"item_id": "iron_ingot", "count": 2}]},
            {"block_id": "barrel", "x": 9, "y": 64, "z": 9, "distance": 9,
             "contents": [{"item_id": "iron_ingot", "count": 30},
                          {"item_id": "stick", "count": 8}]},
        ]
        picks = route_containers({"iron_ingot": 5, "stick": 2}, containers)

        self.assertEqual([p.block_id for p in picks], ["chest", "barrel"])
        self.assertEqual(picks[0].take, {"iron_ingot": 2})
        self.assertEqual(picks[1].take, {"iron_ingot": 3, "stick": 2})
        total = {}
        for pick in picks:
            for item, amount in pick.take.items():
                total[item] = total.get(item, 0) + amount
        self.assertLessEqual(total["iron_ingot"], 5)
        self.assertLessEqual(total["stick"], 2)

    def test_route_ignores_containers_that_cannot_help(self):
        picks = route_containers({"iron_ingot": 3}, [
            {"block_id": "chest", "x": 1, "y": 1, "z": 1,
             "contents": [{"item_id": "dirt", "count": 64}]},
        ])
        self.assertEqual(picks, [])
        self.assertEqual(route_containers({}, [{"contents": [{"item_id": "iron_ingot", "count": 9}]}]), [])

    def test_route_dedupes_the_same_container_seen_twice(self):
        same = {"block_id": "minecraft:chest", "x": 3, "y": 64, "z": 3,
                "contents": [{"item_id": "iron_ingot", "count": 4}]}
        nearby = dict(same, distance=2.0)
        picks = route_containers({"iron_ingot": 4}, [nearby, dict(same)])
        self.assertEqual(len(picks), 1)
        self.assertEqual(picks[0].take, {"iron_ingot": 4})

    def test_route_tolerates_garbage(self):
        self.assertEqual(route_containers(None, None), [])
        self.assertEqual(route_containers({"iron_ingot": 1}, ["nope", 3]), [])

    def test_clamp_take_never_exceeds_the_shortfall(self):
        need = {"iron_ingot": 5}
        self.assertEqual(clamp_take("iron_ingot", 64, need), 5)
        self.assertEqual(clamp_take("minecraft:iron_ingot", 3, need), 3)
        self.assertEqual(clamp_take("iron_ingot", None, need), 5)   # 整组 → 按缺口封顶
        self.assertEqual(clamp_take("iron_ingot", 0, need), 5)
        self.assertEqual(clamp_take("stick", 7, need), 7)           # 没登记缺口 → 按调用方给的
        self.assertEqual(clamp_take("iron_ingot", 3, {}), 3)

    def test_armor_lookup_finds_pieces_in_inventory_and_containers(self):
        self.assertTrue(is_armor("minecraft:iron_helmet"))
        self.assertTrue(is_armor("diamond_boots"))
        self.assertFalse(is_armor("iron_ingot"))
        self.assertFalse(is_armor(""))

        picks = armor_in_containers([
            {"block_id": "minecraft:chest", "x": 7, "y": 64, "z": 7, "distance": 3,
             "contents": [{"item_id": "iron_chestplate", "count": 1},
                          {"item_id": "dirt", "count": 12}]},
        ])
        self.assertEqual(len(picks), 1)
        self.assertEqual(picks[0].take, {"iron_chestplate": 1})
        self.assertEqual((picks[0].x, picks[0].z), (7.0, 7.0))
        self.assertEqual(armor_in_containers([{"contents": [{"item_id": "stone", "count": 1}]}]), [])


if __name__ == "__main__":
    unittest.main()
