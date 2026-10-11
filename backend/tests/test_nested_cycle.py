"""本批验收：搬运的**多物品 + 嵌套容器**、农场**复合链**、**别人家的田**。

对应票据验收清单：

* 不拆散（潜影盒连内容一起搬）/ 含内容物对账 / 递归 `no_touch` / 嵌套上限**如实拒结**；
* 多物品一次搬 + 单趟上限；
* 复合链每一步的产物/缺口**如实入账**；
* 喂动物只在**授权区域**内（被硬拦报区域名+规则）；
* `farm_others` 默认 `none`、`help|mischief` 才放行、且**每次动手都留审计**；
* L1 静态前缀**只减不加**（预算 1700）。
"""

import json
import tempfile
import unittest
from pathlib import Path

from agent.farm import (OTHERS_HELP, OTHERS_MISCHIEF, OTHERS_NONE, animal_kind,
                        cycle_step, feed_food, feed_targets, land_owner,
                        normalize_others, others_verdict)
from agent.haul import (MODE_INSIDE, MODE_WHOLE, NEST_MAX_DEPTH, NEST_MAX_TOTAL,
                        is_container_item, moved_dict, nested_limits, nested_stats,
                        normalize_items, normalize_mode, plan_box_take, plan_haul,
                        plan_inside_take, reconcile_haul, recursive_no_touch,
                        split_side_items)
from agent.permissions import PermissionsStore
from agent.planner import Planner, build_tool_schemas
from agent.territory import TerritoryStore

SERVER = "play.example:25565"
WORLD = "world-a"
OVERWORLD = "minecraft:overworld"
SENTINEL = (1234, 56, -9876)


# ── 夹具（沿用 test_haul_farm 的形状）────────────────────────────


def snap(title, items, *, cid=1, slots=63, start=27):
    return {"id": cid, "title": title, "slots": slots, "player_inv_start": start,
            "items": items}


def box(slot, contents, *, item="minecraft:shulker_box", count=1):
    return {"slot": slot, "item": item, "count": count, "contents": contents}


def zone(name="pen", *, kind="farm", protect=(), x=0.0, y=64.0, z=0.0, radius=16.0,
         granted_by=None):
    row = {"name": name, "kind": kind, "protect": list(protect), "shape": "radius",
           "center": {"x": x, "y": y, "z": z}, "radius": radius, "box": None}
    if granted_by is not None:
        row["granted_by"] = granted_by
    return row


def blocks(*rows):
    return [{"block_id": block_id, "x": x, "y": y, "z": z, "distance": distance}
            for block_id, x, y, z, distance in rows]


def entities(*rows):
    return [{"type": kind, "name": kind, "id": eid, "x": x, "y": y, "z": z, "distance": 2.0}
            for kind, eid, x, y, z in rows]


class DummyMemory:
    def build_context(self):
        return {"interaction_summary": "无"}

    def get_recent_context(self, _count=5):
        return []


class RecordingSkills:
    def __init__(self):
        self.calls = []

    def __getattr__(self, item):
        def recorder(*args, **kwargs):
            self.calls.append((item, args, kwargs))
            return {"success": True, "req_id": f"req-{len(self.calls)}"}
        return recorder

    def names(self):
        return [call[0] for call in self.calls]


def make_planner(*, store=None, permissions=None, skills=None) -> Planner:
    return Planner(llm_service=None, memory=DummyMemory(),
                   skills=skills if skills is not None else RecordingSkills(),
                   permissions=permissions, territory=store)


# ── ① 纯函数：多物品 / 嵌套读 / 上限 / 递归 no_touch ─────────────


class NestedReadTests(unittest.TestCase):
    def test_container_items_are_recognised_by_id(self):
        self.assertTrue(is_container_item("minecraft:shulker_box"))
        self.assertTrue(is_container_item("minecraft:light_blue_shulker_box"))
        self.assertTrue(is_container_item("minecraft:bundle"))
        self.assertFalse(is_container_item("minecraft:iron_ingot"))

    def test_mode_and_items_normalisation_never_guess(self):
        self.assertEqual(normalize_mode("WHOLE"), MODE_WHOLE)
        self.assertEqual(normalize_mode("nonsense"), "item")
        self.assertEqual(normalize_items([{"item": "iron", "count": 3}, {"item": "wheat"}]),
                         {"iron": 3, "wheat": None})
        self.assertEqual(normalize_items("iron:8,wheat"), {"iron": 8, "wheat": None})

    def test_split_side_items_separates_outer_and_inner(self):
        oc = snap("chest", [box(0, {"minecraft:iron_ingot": 64}, item="minecraft:shulker_box"),
                            {"slot": 1, "item": "minecraft:wheat", "count": 5}])

        split = split_side_items(oc)

        self.assertEqual(split["outer"], {"shulker_box": 1, "wheat": 5})
        self.assertEqual(split["inner"], {"iron_ingot": 64})
        self.assertEqual(split["inner"].get("shulker_box"), None, "盒子本身算外层")

    def test_nested_stats_and_limits_refuse_instead_of_truncating(self):
        deep = snap("chest", [box(0, [box(100, {"minecraft:iron_ingot": 1})])])

        self.assertEqual(nested_stats(oc_rows(deep))["depth"], 3)
        report = nested_limits(oc_rows(deep))
        self.assertFalse(report["ok"])
        self.assertEqual(report["reason"], "nested_too_deep")

        huge = snap("chest", [box(0, {"minecraft:iron_ingot": NEST_MAX_TOTAL + 1})])
        report = nested_limits(oc_rows(huge))
        self.assertFalse(report["ok"])
        self.assertEqual(report["reason"], "nested_too_much")

    def test_recursive_no_touch_walks_into_the_box(self):
        oc = snap("chest", [box(0, [{"item": "minecraft:iron_ingot", "count": 8,
                                     "policy": "no_touch"}])])

        hit = recursive_no_touch(oc_rows(oc))

        self.assertIsNotNone(hit)
        self.assertEqual(hit["reason"], "nested_no_touch")
        self.assertEqual(hit["depth"], 2)

    def test_whole_box_moves_as_one_unit_and_reports_contents(self):
        oc = snap("chest", [box(0, {"minecraft:iron_ingot": 64}),
                            box(1, {"minecraft:iron_ingot": 64})])

        plan = plan_box_take(oc, "minecraft:shulker_box", 0)

        self.assertEqual(plan["reason"], "")
        self.assertEqual(plan["boxes"], 1, "源箱留底：两只盒子只搬一只")
        self.assertEqual(plan["contents"], {"iron_ingot": 64}, "内容物随盒走")
        self.assertEqual(plan["moves"], [{"from_slot": 0, "to_slot": 27, "count": 1}],
                         "一格一整只盒，不拆散")

    def test_inside_take_pulls_the_item_out_of_the_box(self):
        oc = snap("chest", [box(0, [{"slot": 100, "item": "minecraft:diamond", "count": 5}])])

        plan = plan_inside_take(oc, "minecraft:diamond", 0)

        self.assertEqual(plan["reason"], "")
        self.assertEqual(plan["available"], 5)
        self.assertEqual(plan["moves"], [{"from_slot": 100, "to_slot": 27, "count": 4,
                                          "box_item": "shulker_box"}],
                         "盒里也留底 1（不许拿空）")

    def test_inside_take_leaves_a_floor_inside_the_box(self):
        oc = snap("chest", [box(0, [{"slot": 100, "item": "minecraft:diamond", "count": 1}])])
        plan = plan_inside_take(oc, "minecraft:diamond", 0)
        self.assertEqual(plan["reason"], "source_floor")

    def test_inside_take_says_not_visible_when_snapshot_lacks_slots(self):
        oc = snap("chest", [box(0, {"minecraft:diamond": 5})])

        plan = plan_inside_take(oc, "minecraft:diamond", 0)

        self.assertEqual(plan["reason"], "nested_not_visible", "取不出来就如实说，不假装")
        self.assertEqual(plan["moves"], [])

    def test_multi_item_trip_is_capped_by_the_single_trip_limit(self):
        rows = [{"slot": s, "item": "minecraft:cobblestone", "count": 64} for s in range(10)]
        oc = snap("chest", rows)

        plan = plan_haul(oc, wants={"cobblestone": 0, "minecraft:iron_ingot": 0},
                         capacity={})

        self.assertEqual(plan["items"], {"cobblestone": 256}, "总量受单趟 256 上限")
        self.assertEqual(plan["blocked"].get("iron_ingot"), "item_absent")


def oc_rows(container):
    from agent.haul import container_rows
    return container_rows(container)


# ── ② 含内容物对账 ─────────────────────────────────────────────


class NestedReconcileTests(unittest.TestCase):
    def before(self):
        return snap("chest", [box(0, {"minecraft:iron_ingot": 64}),
                              box(1, {"minecraft:iron_ingot": 64})])

    def after_ok(self):
        return snap("chest", [box(1, {"minecraft:iron_ingot": 64}),
                              box(27, {"minecraft:iron_ingot": 64})])

    def test_contents_are_part_of_the_reconciliation(self):
        moved = {"outer": {"shulker_box": 1}, "inner": {"iron_ingot": 64}}

        result = reconcile_haul(MODE_WHOLE, "take", self.before(), self.after_ok(), moved,
                                src_side="container", dst_side="player")

        self.assertTrue(result["ok"], result)

    def test_missing_contents_are_reported_with_the_layer(self):
        # 盒子过去了，但**盒里的东西不见了**（源侧内层只该少 64，实际少 128）→ 如实报"内层"。
        after = snap("chest", [box(1, {})])
        moved = {"outer": {"shulker_box": 1}, "inner": {"iron_ingot": 64}}

        result = reconcile_haul(MODE_WHOLE, "take", self.before(), after, moved,
                                src_side="container", dst_side=None)

        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "verify_failed")
        self.assertIn("内层", result["detail"])
        self.assertIn("iron_ingot", result["detail"])

    def test_put_leg_checks_the_destination_contents(self):
        # 放进目标箱时，**目标侧内层**也要涨 —— 少一件就报"内层"。
        before = snap("outpost", [])
        after = snap("outpost", [box(0, {})])
        moved = {"outer": {"shulker_box": 1}, "inner": {"iron_ingot": 64}}

        result = reconcile_haul(MODE_WHOLE, "put", before, after, moved,
                                src_side=None, dst_side="container")

        self.assertFalse(result["ok"])
        self.assertIn("内层", result["detail"])


# ── ③ 分发：整盒搬 / 盒内取 / 上限拒结 / 多物品 ─────────────────


class NestedDispatchTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.permissions = PermissionsStore(path=root / "permissions.json")
        self.permissions.set({"master": ["Steve"]})
        self.store = TerritoryStore(root=root / "territory", server=SERVER,
                                    world_name=WORLD, dimension=OVERWORLD)
        self.store.remember_container("warehouse", x=SENTINEL[0], y=SENTINEL[1],
                                      z=SENTINEL[2], policy="free")
        self.skills = RecordingSkills()
        self.planner = make_planner(store=self.store, permissions=self.permissions,
                                    skills=self.skills)

    def context(self, **extra):
        ctx = {"actor_tier": "master", "actor_name": "Steve",
               "player": {"x": SENTINEL[0], "y": SENTINEL[1], "z": SENTINEL[2]},
               "open_container": {}}
        ctx.update(extra)
        return ctx

    def test_whole_box_hauls_the_box_and_its_contents_in_one_piece(self):
        before = snap("warehouse", [box(0, {"minecraft:iron_ingot": 64}),
                                    box(1, {"minecraft:iron_ingot": 64})])
        ctx = self.context(open_container=before)

        self.assertTrue(self.planner._dispatch_tool_call(
            "haul", {"from": "warehouse", "to": "self", "mode": "whole",
                     "item": "shulker_box"}, ctx))
        self.assertEqual(self.skills.calls, [("container_move", (0, 27, 1), {})],
                         "整盒搬：一格一整只盒，绝不把盒与内容拆开")

        after = snap("warehouse", [box(1, {"minecraft:iron_ingot": 64}),
                                   box(27, {"minecraft:iron_ingot": 64})])
        self.planner.verify_haul(after, (SENTINEL[0], SENTINEL[1], SENTINEL[2]))

        receipt = self.planner.haul_last()
        self.assertTrue(receipt["ok"], receipt)
        self.assertEqual(receipt["moved"]["by_item"], {"shulker_box": 1})
        self.assertEqual(receipt["nested"]["inner"], {"iron_ingot": 64},
                         "回执要含内容物，不只数外层")
        self.assertIn("内容物", self.planner._haul_note)

    def test_recursive_no_touch_stops_the_whole_box(self):
        before = snap("warehouse", [box(0, [{"item": "minecraft:iron_ingot", "count": 8,
                                            "policy": "no_touch"}])])
        ctx = self.context(open_container=before)

        dispatched = self.planner._dispatch_tool_call(
            "haul", {"from": "warehouse", "to": "self", "mode": "whole",
                     "item": "shulker_box"}, ctx)

        self.assertFalse(dispatched)
        self.assertEqual(self.skills.calls, [], "盒里有 no_touch 的东西 → 连盒带物都不动")
        self.assertEqual(self.planner.haul_last()["reason"], "nested_no_touch")

    def test_nested_over_the_limit_is_refused_not_truncated(self):
        before = snap("warehouse", [box(0, [box(100, {"minecraft:iron_ingot": 1}),
                                            box(101, {"minecraft:iron_ingot": 1}),
                                            box(102, {"minecraft:iron_ingot": 1})])])
        ctx = self.context(open_container=before)

        dispatched = self.planner._dispatch_tool_call(
            "haul", {"from": "warehouse", "to": "self", "mode": "whole",
                     "item": "shulker_box"}, ctx)

        self.assertFalse(dispatched)
        self.assertEqual(self.skills.calls, [], "超上限如实拒结，绝不静默截断")
        self.assertEqual(self.planner.haul_last()["reason"], "nested_too_deep")

    def test_multi_item_haul_moves_several_kinds_in_one_trip(self):
        before = snap("warehouse", [{"slot": 0, "item": "minecraft:iron_ingot", "count": 64},
                                    {"slot": 1, "item": "minecraft:wheat", "count": 10}])
        ctx = self.context(open_container=before)

        self.assertTrue(self.planner._dispatch_tool_call(
            "haul", {"from": "warehouse", "to": "self",
                     "items": [{"item": "iron_ingot", "count": 8},
                               {"item": "wheat", "count": 3}]}, ctx))

        moves = [call for call in self.skills.calls if call[0] == "container_move"]
        self.assertEqual(len(moves), 2, "两种物品各一条移动")
        self.assertEqual(self.planner.haul_status()["items"], {"iron_ingot": 8, "wheat": 3})

    def test_whole_box_from_player_inventory_into_a_chest_still_verifies(self):
        before = snap("warehouse", [{"slot": 27, "item": "minecraft:shulker_box", "count": 1}])
        ctx = self.context(open_container=before)

        self.assertTrue(self.planner._dispatch_tool_call(
            "haul", {"from": "self", "to": "warehouse", "mode": "whole",
                     "item": "shulker_box"}, ctx))
        self.assertEqual(self.skills.calls[-1], ("container_move", (27, 0, 1), {}))

        after = snap("warehouse", [{"slot": 0, "item": "minecraft:shulker_box", "count": 1}])
        self.planner.verify_haul(after, (SENTINEL[0], SENTINEL[1], SENTINEL[2]))

        self.assertTrue(self.planner.haul_last()["ok"], self.planner.haul_last())

    def test_inside_mode_takes_only_what_is_in_the_box(self):
        # 盒内取：盒里的钻石有内容槽位 → 从内容槽位取，不搬整盒。
        before = snap("warehouse", [box(0, [{"slot": 100, "item": "minecraft:diamond",
                                             "count": 5}])])
        ctx = self.context(open_container=before)

        self.assertTrue(self.planner._dispatch_tool_call(
            "haul", {"from": "warehouse", "to": "self", "mode": "inside",
                     "item": "diamond"}, ctx))
        self.assertEqual(self.skills.calls, [("container_move", (100, 27, 4), {})],
                         "盒内取：从盒的内容槽位取，不搬整盒（盒里也留底 1）")


# ── ④ 复合链：每一步如实入账 / 喂食只在授权区 ────────────────────


class FarmCycleTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.permissions = PermissionsStore(path=root / "permissions.json")
        self.permissions.set({"master": ["Steve"]})
        self.store = TerritoryStore(root=root / "territory", server=SERVER,
                                    world_name=WORLD, dimension=OVERWORLD)
        self.store.set_territory("yard", kind="base", x=0.0, y=64.0, z=0.0, radius=32.0,
                                 grants=("farm", "take", "store"))
        self.skills = RecordingSkills()
        self.planner = make_planner(store=self.store, permissions=self.permissions,
                                    skills=self.skills)

    def context(self, **extra):
        ctx = {"actor_tier": "master", "actor_name": "Steve",
               "player": {"x": 0.0, "y": 64.0, "z": 0.0},
               "task_state": {"kind": "idle", "status": "idle"}}
        ctx.update(extra)
        return ctx

    def test_cycle_harvests_replants_and_feeds_in_one_chain(self):
        ctx = self.context(
            nearby_blocks=blocks(("minecraft:wheat", 1, 64, 1, 1.2)),
            inventory=[{"slot": 1, "name": "wheat_seeds", "count": 4},
                       {"slot": 2, "name": "wheat", "count": 6}],
            entities=entities(("minecraft:cow", 7, 2.0, 64.0, 2.0)))

        self.assertTrue(self.planner._dispatch_tool_call(
            "farm_cycle", {"feed": True}, ctx))

        steps = {row["step"]: row for row in self.planner.cycle_status()["steps"]}
        self.assertTrue(steps["harvest"]["ok"], steps)
        self.assertTrue(steps["replant"]["ok"], "收割后挂上同一作物的补种")
        self.assertTrue(steps["feed"]["ok"], steps)
        self.assertEqual(steps["feed"]["items"], {"cow": 1})
        # 收割走挖、喂食走通用右键（select_hotbar + look_at_entity + use_item）。
        names = self.skills.names()
        self.assertIn("mine_block", names)
        self.assertIn("look_at_entity", names)
        self.assertIn("use_item", names)

    def test_feed_only_happens_in_an_authorised_pen(self):
        other = TerritoryStore(root=Path(self._tmp.name) / "t2", server=SERVER,
                               world_name=WORLD, dimension=OVERWORLD)
        planner = make_planner(store=other, permissions=self.permissions,
                               skills=self.skills)
        ctx = self.context(
            inventory=[{"slot": 1, "name": "wheat", "count": 4}],
            entities=entities(("minecraft:cow", 7, 2.0, 64.0, 2.0)))

        planner._dispatch_tool_call("farm_cycle", {"feed": True}, ctx)

        steps = {row["step"]: row for row in planner.cycle_status()["steps"]}
        self.assertFalse(steps["feed"]["ok"], "没授权的畜栏不喂")
        self.assertEqual(self.skills.calls, [], "一帧都不发")
        self.assertIn("畜栏", steps["feed"]["detail"])

    def test_blocked_pen_is_reported_with_its_zone_and_rule(self):
        self.store.set_zone(zone("pubfarm", kind="farm", protect=["crops", "blocks"]))
        ctx = self.context(
            inventory=[{"slot": 1, "name": "wheat", "count": 4}],
            entities=entities(("minecraft:cow", 7, 2.0, 64.0, 2.0)))

        self.planner._dispatch_tool_call("farm_cycle", {"feed": True}, ctx)

        steps = {row["step"]: row for row in self.planner.cycle_status()["steps"]}
        self.assertIn("pubfarm", steps["feed"]["detail"])
        self.assertIn("crops", steps["feed"]["detail"])

    def test_missing_food_is_reported_not_faked(self):
        ctx = self.context(
            nearby_blocks=blocks(("minecraft:wheat", 1, 64, 1, 1.2)),
            inventory=[{"slot": 1, "name": "wheat_seeds", "count": 4}],
            entities=entities(("minecraft:pig", 9, 2.0, 64.0, 2.0)))

        self.planner._dispatch_tool_call("farm_cycle", {"feed": True}, ctx)

        steps = {row["step"]: row for row in self.planner.cycle_status()["steps"]}
        self.assertFalse(steps["feed"]["ok"])
        self.assertIn("pig", steps["feed"]["detail"])
        self.assertEqual(self.skills.names(), ["look_at", "mine_block"],
                         "缺料就不喂：没有 use_item / select_hotbar")


# ── ⑤ 别人家的田：默认关 / help|mischief 才放行 / 留审计 ──────────


class FarmOthersTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.permissions = PermissionsStore(path=root / "permissions.json")
        self.permissions.set({"master": ["Steve"], "friends": ["Alex"]})
        self.store = TerritoryStore(root=root / "territory", server=SERVER,
                                    world_name=WORLD, dimension=OVERWORLD)
        # 一块**别人家**的田：`granted_by` 是 friend，protect 为空（不是保护区）。
        self.store.set_zone(zone("alex_farm", kind="farm", protect=[],
                                 granted_by={"tier": "friend", "name": "Alex"}))
        self.skills = RecordingSkills()
        self.planner = make_planner(store=self.store, permissions=self.permissions,
                                    skills=self.skills)

    def set_level(self, level):
        self.permissions.set({"farm_others": {"friend": level}})

    def context(self, **extra):
        ctx = {"actor_tier": "master", "actor_name": "Steve",
               "player": {"x": 0.0, "y": 64.0, "z": 0.0},
               "task_state": {"kind": "idle", "status": "idle"},
               "nearby_blocks": blocks(("minecraft:wheat", 1, 64, 1, 1.2)),
               "inventory": [{"slot": 3, "name": "wheat_seeds", "count": 2}]}
        ctx.update(extra)
        return ctx

    def test_default_is_none_and_nothing_is_touched(self):
        dispatched = self.planner._dispatch_tool_call("farm", {"action": "harvest"},
                                                      self.context())

        self.assertFalse(dispatched)
        self.assertEqual(self.skills.calls, [], "默认关：一帧都不发")
        self.assertEqual(self.permissions.farm_others("friend"), OTHERS_NONE)
        self.assertIn("没放开采别人家的田", self.planner._farm_note)
        audit = self.planner.farm_others_audit()
        self.assertEqual(audit[-1]["allowed"], False)
        self.assertEqual(audit[-1]["mode"], OTHERS_NONE)
        self.assertEqual(audit[-1]["owner"], "Alex")

    def test_help_lets_it_lend_a_hand_and_leaves_an_audit_row(self):
        self.set_level(OTHERS_HELP)

        dispatched = self.planner._dispatch_tool_call("farm", {"action": "harvest"},
                                                      self.context())

        self.assertTrue(dispatched)
        self.assertEqual(self.skills.names(), ["look_at", "mine_block"])
        audit = self.planner.farm_others_audit()
        self.assertEqual(audit[-1]["allowed"], True)
        self.assertEqual(audit[-1]["mischief"], False)
        self.assertIn("帮Alex家的田", self.planner._farm_note)

    def test_mischief_needs_both_the_level_and_a_harmless_action(self):
        self.set_level(OTHERS_MISCHIEF)

        # 无伤大雅（种东西）→ 放行，且标成捣乱。
        planted = self.planner._dispatch_tool_call(
            "farm", {"action": "plant", "mischief": True},
            self.context(nearby_blocks=blocks(("minecraft:farmland", 1, 64, 1, 1.0))))
        self.assertTrue(planted)
        self.assertTrue(self.planner.farm_others_audit()[-1]["mischief"])

        # 破坏/伤害类的动作（收割）就算戴着 mischief 也不许。
        harmful = self.planner._dispatch_tool_call(
            "farm", {"action": "harvest", "mischief": True}, self.context())
        self.assertFalse(harmful)
        self.assertEqual(self.planner.farm_others_audit()[-1]["allowed"], False)
        self.assertEqual(self.planner.farm_others_audit()[-1]["reason"],
                         "mischief_not_harmless")

    def test_mischief_is_refused_when_only_help_is_allowed(self):
        self.set_level(OTHERS_HELP)

        dispatched = self.planner._dispatch_tool_call(
            "farm", {"action": "plant", "mischief": True},
            self.context(nearby_blocks=blocks(("minecraft:farmland", 1, 64, 1, 1.0))))

        self.assertFalse(dispatched)
        self.assertIn("只许帮忙种", self.planner._farm_note)


class FarmOthersPureTests(unittest.TestCase):
    def test_verdicts_are_table_driven(self):
        self.assertEqual(others_verdict(OTHERS_NONE, "harvest")["reason"], "farm_others_off")
        self.assertTrue(others_verdict(OTHERS_HELP, "harvest")["allowed"])
        self.assertFalse(others_verdict(OTHERS_HELP, "till", mischievous=True)["allowed"])
        self.assertTrue(others_verdict(OTHERS_MISCHIEF, "plant",
                                       mischievous=True)["mischief"])

    def test_land_owner_defaults_to_ours_when_nobody_else_claimed_it(self):
        self.assertTrue(land_owner({"name": "yard"})["mine"])
        self.assertTrue(land_owner({"granted_by": {"tier": "master"}})["mine"])
        self.assertFalse(land_owner({"granted_by": {"tier": "friend",
                                                    "name": "Alex"}})["mine"])

    def test_permissions_level_normalisation_defaults_to_none(self):
        self.assertEqual(normalize_others("nonsense"), OTHERS_NONE)
        level = PermissionsStore(path=Path(tempfile.mkdtemp()) / "p.json")
        self.assertEqual(level.farm_others("friend"), "none")
        level.set({"farm_others": {"friend": "mischief"}})
        self.assertEqual(level.farm_others("friend"), "mischief")
        level.set({"farm_others": {"friend": "bogus"}})
        self.assertEqual(level.farm_others("friend"), "none", "认不出的回默认关")


class FeedHelpersTests(unittest.TestCase):
    def test_animal_food_table_and_authorised_targets(self):
        self.assertEqual(animal_kind({"type": "minecraft:cow"}), "cow")
        self.assertEqual(feed_food({"wheat": 1}, {"type": "minecraft:cow"}), "wheat")
        self.assertEqual(feed_food({}, {"type": "minecraft:cow"}), "")

        granted = [{"name": "pen", "grants": ["farm"], "granted_by": {"tier": "master"},
                    "shape": "radius", "center": {"x": 0.0, "y": 64.0, "z": 0.0},
                    "radius": 16.0, "box": None}]
        targets = feed_targets(entities(("minecraft:cow", 1, 1.0, 64.0, 1.0),
                                        ("minecraft:pig", 2, 40.0, 64.0, 40.0)),
                               [], granted)
        self.assertEqual([row["kind"] for row in targets["feedable"]], ["cow"])
        self.assertEqual(len(targets["unauthorized"]), 1)


# ── ⑥ 工具注册 + L1 预算 ────────────────────────────────────────


class ToolsAndPromptTests(unittest.TestCase):
    def test_new_tools_and_args_are_registered(self):
        schemas = {item["function"]["name"]: item for item in build_tool_schemas("idle")}

        self.assertIn("farm_cycle", schemas)
        props = schemas["farm_cycle"]["function"]["parameters"]["properties"]
        self.assertEqual(props["feed"]["type"], "boolean")
        self.assertEqual(props["craft"]["type"], "boolean")
        haul = schemas["haul"]["function"]["parameters"]["properties"]
        self.assertEqual(haul["items"]["type"], "array")
        self.assertEqual(haul["mode"]["type"], "string")
        farm = schemas["farm"]["function"]["parameters"]["properties"]
        self.assertEqual(farm["mischief"]["type"], "boolean")

    def test_l1_static_prefix_only_shrinks(self):
        p = make_planner()
        system = p.build_prompt_layers(
            "Steve", "hi", {"task_state": {"kind": "idle", "status": "idle"}}, "AI").system

        self.assertLess(len(system), 1690, "本批只许减不许加：静态前缀必须 < 上一批的 1690")
        for needle in ("搬运 haul", "种田用 farm", "源箱留底", "farm_cycle", "farm_others"):
            self.assertIn(needle, system)


if __name__ == "__main__":
    unittest.main()
