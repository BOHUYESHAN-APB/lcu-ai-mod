"""搬运（haul）/ 种田（farm）的验收测试（对应票据的验收清单）。

覆盖：
  ① 算量受三约束（目标侧剩余空间 / 本次需求 / 单趟上限 256）且**源箱留底不搬空**；
  ② 搬运两侧对账：搬前/搬后不符 → 如实报 `verify_failed`（不掩盖）；
  ③ `no_touch` 一律不碰、`borrow_only` 作为**源**必须走借用（haul 不许绕过）；
  ④ 失败回执逐字契约 `{ok:false, step, from, to, moved:{item,count}, reason}`；
  ⑤ 农活只在**授权地块**里干（没授权不干活、被 zone 硬拦就报"区域名 + 规则"、不绕过）；
  ⑥ 农活全走通用右键/挖（没有专门的 Java 命令、没有特殊交互分支）；缺种子/锄头如实报；
  ⑦ L1 静态前缀只降不抬（≤1700，实测值见断言）；坐标不进 server / person 层。
"""

import tempfile
import time
import unittest
from pathlib import Path

from agent.conversation import ConversationStore
from agent.farm import choose_action, crop_of, hold_index, is_hoe, missing_items
from agent.haul import (SOURCE_FLOOR, failure_receipt, plan_haul, plan_put_moves,
                        plan_take_moves, reconcile_sides, side_free_space, side_items)
from agent.permissions import PermissionsStore
from agent.person_memory import PersonMemoryStore
from agent.planner import Planner, build_tool_schemas
from agent.session import Session
from agent.territory import TerritoryStore

SERVER = "play.example:25565"
WORLD = "world-a"
OVERWORLD = "minecraft:overworld"
STEVE = "069a79f4-44e9-4726-a5be-fca90e38aaf5"

# 记的坐标用哨兵值：它们只该出现在 **world 层** 的领地文件里。
SENTINEL = (1234, 56, -9876)


# ── 夹具 ────────────────────────────────────────────────────────


def chest(title, items=None, *, cid=1, slots=63, start=27):
    """一份 `open_container` 快照：容器侧 0..start-1，玩家背包侧 start..slots-1。"""
    return {"id": cid, "title": title, "slots": slots, "player_inv_start": start,
            "items": [{"slot": slot, "item": item, "count": count}
                      for slot, item, count in (items or [])]}


def zone(name="pubfarm", *, kind="farm", protect=("animals", "crops"),
         x=0.0, y=64.0, z=0.0, radius=16.0):
    return {"name": name, "kind": kind, "protect": list(protect), "shape": "radius",
            "center": {"x": x, "y": y, "z": z}, "radius": radius, "box": None}


def blocks(*rows):
    return [{"block_id": block_id, "x": x, "y": y, "z": z, "distance": distance}
            for block_id, x, y, z, distance in rows]


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


def planner(*, store=None, permissions=None, skills=None) -> Planner:
    return Planner(llm_service=None, memory=DummyMemory(),
                   skills=skills if skills is not None else RecordingSkills(),
                   permissions=permissions, territory=store)


# ── ① 算量：留底 / 上限 / 目标空间 / 需求 ───────────────────────


class HaulPlanTests(unittest.TestCase):
    def src(self):
        return chest("warehouse", [(0, "minecraft:iron_ingot", 64),
                                  (1, "minecraft:wheat", 10)])

    def test_never_moves_the_last_item_out_of_the_source(self):
        """「不许整箱搬空」：源箱每件物品至少留 1 个（SOURCE_FLOOR）。"""
        plan = plan_haul(self.src(), item="iron_ingot", want=0)

        self.assertEqual(plan["items"], {"iron_ingot": 64 - SOURCE_FLOOR})
        self.assertGreaterEqual(SOURCE_FLOOR, 1)

    def test_single_trip_stays_under_256(self):
        big = chest("warehouse", [(slot, "minecraft:cobblestone", 64) for slot in range(10)])

        plan = plan_haul(big, item="cobblestone", want=0)

        self.assertEqual(plan["items"]["cobblestone"], 256)

    def test_capacity_and_demand_both_clamp_the_take(self):
        self.assertEqual(plan_haul(self.src(), item="iron_ingot", want=8,
                                  capacity={"iron_ingot": 4})["items"], {"iron_ingot": 4})
        self.assertEqual(plan_haul(self.src(), item="iron_ingot", want=8,
                                  capacity={"iron_ingot": 512})["items"], {"iron_ingot": 8})
        self.assertEqual(plan_haul(self.src(), item="iron_ingot", want=8,
                                  capacity={"iron_ingot": 0})["reason"], "target_full")

    def test_item_omitted_moves_everything_movable_within_the_trip_cap(self):
        plan = plan_haul(self.src(), capacity={"iron_ingot": 512, "wheat": 512})

        self.assertEqual(plan["items"], {"iron_ingot": 63, "wheat": 9})

    def test_source_floor_and_absent_item_are_reported_not_faked(self):
        self.assertEqual(plan_haul(chest("empty"), capacity={})["reason"], "source_empty")
        self.assertEqual(plan_haul(self.src(), item="sand")["reason"], "item_absent")
        self.assertEqual(plan_haul(chest("one", [(0, "minecraft:iron_ingot", 1)]),
                                  item="iron_ingot")["reason"], "source_floor")

    def test_moves_are_per_slot_and_never_whole_stacks_by_accident(self):
        oc = chest("warehouse", [(0, "minecraft:iron_ingot", 64)])

        moves = plan_take_moves(oc, "iron_ingot", 8)

        self.assertEqual(moves, [{"from_slot": 0, "to_slot": 27, "count": 8}])

    def test_put_moves_stack_into_existing_slots_first(self):
        oc = chest("outpost", [(0, "minecraft:iron_ingot", 60),
                               (27, "minecraft:iron_ingot", 8)])

        moves = plan_put_moves(oc, "iron_ingot", 8)

        self.assertEqual(moves, [{"from_slot": 27, "to_slot": 0, "count": 4},
                                 {"from_slot": 27, "to_slot": 1, "count": 4}])

    def test_side_reads_need_a_layout(self):
        blank = {"title": "x", "items": [{"slot": 0, "item": "minecraft:iron_ingot",
                                          "count": 3}]}
        self.assertEqual(side_items(blank), {})
        self.assertEqual(side_free_space(blank, "iron_ingot"), 0)


# ── ② 两侧对账：不符就如实报 ───────────────────────────────────


class ReconcileTests(unittest.TestCase):
    def before(self):
        return chest("warehouse", [(0, "minecraft:iron_ingot", 64)])

    def after(self, taken):
        return chest("warehouse", [(0, "minecraft:iron_ingot", 64 - taken),
                                   (27, "minecraft:iron_ingot", taken)])

    def test_matching_snapshot_passes_on_both_sides(self):
        result = reconcile_sides(self.before(), self.after(8), {"iron_ingot": 8},
                                 src_side="container", dst_side="player")

        self.assertTrue(result["ok"])
        self.assertEqual(result["checked"]["iron_ingot"]["source_delta"], 8)
        self.assertEqual(result["checked"]["iron_ingot"]["dest_delta"], 8)

    def test_mismatch_is_reported_as_verify_failed_never_hidden(self):
        result = reconcile_sides(self.before(), self.after(5), {"iron_ingot": 8},
                                 src_side="container", dst_side="player")

        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "verify_failed")
        self.assertIn("应少 8", result["detail"])

    def test_missing_snapshot_is_never_treated_as_ok(self):
        result = reconcile_sides({"title": "x"}, self.after(8), {"iron_ingot": 8})

        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "no_snapshot")

    def test_failure_receipt_matches_the_contract(self):
        receipt = failure_receipt("take", src="a", dst="b", moved={"iron_ingot": 3},
                                 reason="verify_failed")

        self.assertEqual(set(receipt), {"ok", "step", "from", "to", "moved", "reason"})
        self.assertEqual(receipt["ok"], False)
        self.assertEqual(receipt["step"], "take")
        self.assertEqual(receipt["from"], "a")
        self.assertEqual(receipt["to"], "b")
        self.assertEqual(receipt["moved"], {"item": "iron_ingot", "count": 3,
                                            "by_item": {"iron_ingot": 3}})
        self.assertEqual(receipt["reason"], "verify_failed")


# ── ③④ 搬运分发：硬约束 + 回执 + 跨轮对账 ──────────────────────


class HaulDispatchTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.permissions = PermissionsStore(path=self.root / "permissions.json")
        self.permissions.set({"master": ["Steve"], "friends": ["Alex"]})
        self.store = TerritoryStore(root=self.root / "territory", server=SERVER,
                                    world_name=WORLD, dimension=OVERWORLD)
        self.store.remember_container("warehouse", x=SENTINEL[0], y=SENTINEL[1],
                                      z=SENTINEL[2], policy="free")
        self.store.remember_container("outpost", x=10, y=64, z=10, policy="free")
        self.store.remember_container("vault", x=20, y=64, z=20, policy="no_touch")
        self.store.remember_container("lend", x=30, y=64, z=30, policy="borrow_only")
        self.skills = RecordingSkills()
        self.planner = planner(store=self.store, permissions=self.permissions,
                               skills=self.skills)

    def context(self, **extra):
        ctx = {"actor_tier": "master", "actor_name": "Steve",
               "player": {"x": SENTINEL[0], "y": SENTINEL[1], "z": SENTINEL[2]},
               "open_container": {}}
        ctx.update(extra)
        return ctx

    # ③ no_touch / borrow_only

    def test_no_touch_container_is_never_touched(self):
        dispatched = self.planner._dispatch_tool_call(
            "haul", {"from": "vault", "to": "self"}, self.context())

        self.assertFalse(dispatched)
        self.assertEqual(self.skills.calls, [], "no_touch 一律不碰（一帧都不发）")
        self.assertEqual(self.planner.haul_last()["reason"], "no_touch")
        self.assertEqual(self.planner.haul_last()["step"], "open_source")
        self.assertIn("no_touch", self.planner._haul_note)

    def test_borrow_only_source_must_go_through_the_borrow_tools(self):
        dispatched = self.planner._dispatch_tool_call(
            "haul", {"from": "lend", "to": "self"}, self.context())

        self.assertFalse(dispatched)
        self.assertEqual(self.skills.calls, [], "不许用 haul 绕过借还")
        receipt = self.planner.haul_last()
        self.assertEqual(receipt["reason"], "borrow_only_use_borrow_item")
        self.assertEqual(receipt["from"], "lend")

    def test_borrow_only_target_still_accepts_a_deposit(self):
        """borrow_only ＝「用于传递物品」：**收**的一侧是它本来的用途（取才必须走借用）。"""
        oc = chest("lend", [(27, "minecraft:iron_ingot", 8)])
        ctx = self.context(open_container=oc)

        dispatched = self.planner._dispatch_tool_call(
            "haul", {"from": "self", "to": "lend", "item": "iron_ingot", "count": 8}, ctx)

        self.assertTrue(dispatched)
        self.assertEqual(self.skills.calls[-1][0], "container_move")

    def test_unknown_container_is_refused_honestly(self):
        dispatched = self.planner._dispatch_tool_call(
            "haul", {"from": "nope", "to": "self"}, self.context())

        self.assertFalse(dispatched)
        self.assertEqual(self.skills.calls, [])
        self.assertEqual(self.planner.haul_last()["reason"], "unknown_container")
        self.assertEqual(self.planner.haul_last()["from"], "nope")

    # ①④ 容器 → 自己背包：取 + 两侧对账（同一份快照就能对两侧）

    def test_container_to_self_takes_then_verifies_both_sides(self):
        before = chest("warehouse", [(0, "minecraft:iron_ingot", 64)])
        after = chest("warehouse", [(0, "minecraft:iron_ingot", 56),
                                    (27, "minecraft:iron_ingot", 8)])
        ctx = self.context(open_container=before)

        self.assertTrue(self.planner._dispatch_tool_call(
            "haul", {"from": "warehouse", "to": "self", "item": "iron_ingot", "count": 8}, ctx))
        self.assertEqual(self.skills.calls,
                         [("container_move", (0, 27, 8), {})], "算量后按格取，不整箱搬")

        finished = self.planner.verify_haul(after, (SENTINEL[0], SENTINEL[1], SENTINEL[2]))

        self.assertTrue(finished)
        self.assertEqual(self.planner.haul_last()["ok"], True)
        self.assertEqual(self.planner.haul_last()["moved"]["by_item"], {"iron_ingot": 8})
        self.assertEqual(self.skills.names()[-1], "container_close")
        self.assertIn("搬好了", self.planner._haul_note)
        self.assertIn("源箱照旧留底", self.planner._haul_note)

    def test_mismatch_while_taking_is_reported_not_hidden(self):
        before = chest("warehouse", [(0, "minecraft:iron_ingot", 64)])
        after = chest("warehouse", [(0, "minecraft:iron_ingot", 59),
                                    (27, "minecraft:iron_ingot", 5)])
        ctx = self.context(open_container=before)
        self.planner._dispatch_tool_call(
            "haul", {"from": "warehouse", "to": "self", "item": "iron_ingot", "count": 8}, ctx)

        self.planner.verify_haul(after, (SENTINEL[0], SENTINEL[1], SENTINEL[2]))

        receipt = self.planner.haul_last()
        self.assertEqual(receipt["ok"], False)
        self.assertEqual(receipt["reason"], "verify_failed")
        self.assertEqual(receipt["step"], "verify")
        self.assertEqual(receipt["moved"], {"item": "", "count": 0, "by_item": {}},
                         "没对上的量不记成已搬")
        self.assertIn("对账不符", self.planner._haul_note)

    def test_waiting_for_a_snapshot_that_never_comes_is_reported(self):
        before = chest("warehouse", [(0, "minecraft:iron_ingot", 64)])
        ctx = self.context(open_container=before)
        self.planner._dispatch_tool_call(
            "haul", {"from": "warehouse", "to": "self", "item": "iron_ingot", "count": 8}, ctx)
        self.planner._haul_job.awaiting_since = time.time() - 60.0

        self.planner.haul_step(ctx)

        self.assertEqual(self.planner.haul_last()["reason"], "verify_no_snapshot")
        self.assertIn("不假装搬完了", self.planner._haul_note)

    # 自己背包 → 容器：放进目标箱（两侧都在同一份快照里）

    def test_self_to_container_puts_and_verifies_both_sides(self):
        before = chest("outpost", [(27, "minecraft:iron_ingot", 8)])
        after = chest("outpost", [(0, "minecraft:iron_ingot", 8)])
        ctx = self.context(open_container=before)

        self.assertTrue(self.planner._dispatch_tool_call(
            "haul", {"from": "self", "to": "outpost", "item": "iron_ingot", "count": 8}, ctx))
        self.assertEqual(self.skills.calls, [("container_move", (27, 0, 8), {})])

        self.planner.verify_haul(after, (SENTINEL[0], SENTINEL[1], SENTINEL[2]))

        self.assertEqual(self.planner.haul_last()["ok"], True)
        self.assertEqual(self.planner.haul_last()["moved"]["by_item"], {"iron_ingot": 8})

    def test_put_that_does_not_fit_is_reported_with_the_remainder(self):
        """目标箱在交付那一刻塞不下（别人塞满了）→ 背包里的余量要**如实报出来**。"""
        warehouse = chest("warehouse", [(0, "minecraft:iron_ingot", 64)], cid=1)
        outpost_empty = chest("outpost", [], cid=2)
        outpost_full = chest("outpost", [(slot, "minecraft:stone", 64) for slot in range(27)],
                             cid=2)
        ctx = self.context(open_container=outpost_empty)
        self.planner._dispatch_tool_call(
            "haul", {"from": "warehouse", "to": "outpost", "item": "iron_ingot", "count": 8},
            ctx)
        self.assertEqual(self.planner._haul_job.phase, "take")
        ctx["open_container"] = warehouse
        self.planner.haul_step(ctx)
        self.assertEqual(self.planner._haul_job.carried, {"iron_ingot": 8})
        after = chest("warehouse", [(0, "minecraft:iron_ingot", 56),
                                    (27, "minecraft:iron_ingot", 8)], cid=1)
        self.planner.verify_haul(after, (SENTINEL[0], SENTINEL[1], SENTINEL[2]))
        self.assertEqual(self.planner._haul_job.phase, "put")

        self.skills.calls.clear()
        ctx["open_container"] = outpost_full
        self.planner.haul_step(ctx)

        receipt = self.planner.haul_last()
        self.assertEqual(receipt["ok"], False)
        self.assertEqual(receipt["step"], "put")
        self.assertEqual(receipt["moved"], {"item": "", "count": 0, "by_item": {}},
                         "没放进去的量不算搬好")
        self.assertIn("放不下", self.planner._haul_note)
        self.assertIn("还留在我背包里", self.planner._haul_note)

    # 容器 → 容器：先探目标箱（算量受"目标箱剩余空间"约束）

    def test_container_to_container_probes_the_target_then_takes_within_its_space(self):
        warehouse = chest("warehouse", [(0, "minecraft:iron_ingot", 64)], cid=1)
        outpost = chest("outpost", [(slot, "minecraft:stone", 64) for slot in range(26)]
                        + [(26, "minecraft:iron_ingot", 60)], cid=2)
        ctx = self.context(open_container=outpost)

        self.planner._dispatch_tool_call(
            "haul", {"from": "warehouse", "to": "outpost", "item": "iron_ingot"}, ctx)
        self.assertEqual(self.skills.names(), ["container_close"],
                         "先探一眼目标箱（算量要看它剩多少空间），别急着取")
        self.assertEqual(self.planner._haul_job.wanted, "src")

        ctx["open_container"] = warehouse
        self.planner.haul_step(ctx)
        moves = [call for call in self.skills.calls if call[0] == "container_move"]

        # 目标箱里那叠只差 4 个满：取 4 个就够了（64-1 的可用量不是这里的关键约束）。
        self.assertEqual(moves, [("container_move", (0, 27, 4), {})])

    def test_walks_to_the_source_and_then_opens_it(self):
        """A 没开着 → 先走过去（普通寻路）；到了跟前 → 看向它并右键打开（通用原语）。"""
        ctx = self.context()
        self.planner._dispatch_tool_call(
            "haul", {"from": "warehouse", "to": "self", "item": "iron_ingot"}, ctx)

        self.assertEqual(self.skills.names(), ["go_to_block"])
        self.assertEqual(self.skills.calls[0][1][0], "minecraft:chest")
        self.assertIn("走去找", self.planner._haul_note)

        self.skills.calls.clear()
        ctx["nearby_storage"] = [{"title": "warehouse", "distance": 2.0}]
        self.planner.haul_step(ctx)

        self.assertEqual(self.skills.names(), ["look_at", "open_container"])
        self.assertEqual(self.planner._haul_job.wanted, "src")

    def test_being_unable_to_walk_there_is_reported_not_blocked_around(self):
        """走不到就如实报 —— **绝不靠放方块解决地形**（用户原话：它会乱搭方块）。"""
        ctx = self.context()
        self.planner._dispatch_tool_call(
            "haul", {"from": "warehouse", "to": "self", "item": "iron_ingot"}, ctx)
        self.planner._haul_job.walked_since = time.time() - 600.0

        self.planner.haul_step(ctx)

        receipt = self.planner.haul_last()
        self.assertEqual(receipt["reason"], "no_path")
        self.assertEqual(receipt["step"], "walk_to_source")
        self.assertIn("不搭方块硬凑", self.planner._haul_note)
        self.assertNotIn("place_block", self.skills.names())

    def test_session_hooks_drive_the_haul_job(self):
        """session 的两个钩子：空闲推进一步 + 新快照到了就对账（没钩子就永远卡着）。"""
        calls = []

        class FakePlanner:
            def haul_step(self, context):
                calls.append(("step", dict(context)))
                return True

            def verify_haul(self, snapshot, player_pos=None):
                calls.append(("verify", snapshot, player_pos))
                return True

        session = Session.__new__(Session)
        session.planner = FakePlanner()
        session.runtime = {"open_container": {}, "inventory": [],
                           "nearby_storage": [], "player": {"x": 1.0, "y": 2.0, "z": 3.0}}
        session.poi = None
        session._actor = {"tier": "master", "name": "Steve"}

        self.assertTrue(session._haul_step())
        session._verify_haul({"id": 1, "title": "warehouse", "items": []})

        self.assertEqual([row[0] for row in calls], ["step", "verify"])
        self.assertEqual(calls[1][2], (1.0, 2.0, 3.0), "对账要带上玩家位置（判是不是那个箱子）")
        self.assertEqual(calls[1][1]["title"], "warehouse")

    def test_audit_and_status_carry_no_coordinates(self):
        before = chest("warehouse", [(0, "minecraft:iron_ingot", 64)])
        ctx = self.context(open_container=before)
        self.planner._dispatch_tool_call(
            "haul", {"from": "warehouse", "to": "self", "item": "iron_ingot", "count": 8}, ctx)

        rows = self.planner.haul_audit(5)
        status = self.planner.haul_status()

        self.assertEqual(rows[-1]["result"], "started")
        self.assertEqual(set(rows[-1]), {"ts", "from", "to", "item", "want", "moved",
                                         "result", "reason"})
        self.assertEqual(status["from"], "warehouse")
        self.assertNotIn(str(SENTINEL[0]), str(rows) + str(status))


# ── ⑤⑥ 种田：授权 / 保护 / 缺料 / 交互方式 ────────────────────


class FarmDispatchTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.permissions = PermissionsStore(path=self.root / "permissions.json")
        self.permissions.set({"master": ["Steve"], "friends": ["Alex"]})
        self.store = TerritoryStore(root=self.root / "territory", server=SERVER,
                                    world_name=WORLD, dimension=OVERWORLD)
        self.skills = RecordingSkills()
        self.planner = planner(store=self.store, permissions=self.permissions,
                               skills=self.skills)
        self.crops = blocks(("minecraft:wheat", 1, 64, 1, 1.4),
                            ("minecraft:dirt", 2, 64, 1, 2.0),
                            ("minecraft:farmland", 3, 64, 1, 3.0))

    def context(self, *, inventory=None, nearby=None, player=None):
        return {"actor_tier": "master", "actor_name": "Steve",
                "player": player or {"x": 0.0, "y": 64.0, "z": 0.0},
                "inventory": inventory or [], "nearby_blocks": nearby or []}

    def grant(self, *, grants=("farm", "take", "store"), x=0.0, y=64.0, z=0.0, radius=32.0):
        self.store.set_territory("yard", kind="base", x=x, y=y, z=z, radius=radius,
                                 grants=grants)

    def test_without_any_granted_plot_the_ai_refuses_to_farm(self):
        dispatched = self.planner._dispatch_tool_call(
            "farm", {"action": "harvest"}, self.context(nearby=self.crops))

        self.assertFalse(dispatched)
        self.assertEqual(self.skills.calls, [], "没有任何授权地块 → 一帧都不发（不偷干）")
        self.assertIn("没有授权给我的农田", self.planner._farm_note)

    def test_grant_without_farm_permission_is_still_not_a_farm(self):
        self.grant(grants=("take", "place"))

        dispatched = self.planner._dispatch_tool_call(
            "farm", {"action": "harvest"}, self.context(nearby=self.crops))

        self.assertFalse(dispatched)
        self.assertEqual(self.skills.calls, [])
        self.assertIn("没有授权给我的农田", self.planner._farm_note)

    def test_protected_zone_is_reported_with_its_name_and_rule(self):
        """§11：带 `protect:[\"crops\"]` 的是**保护区** —— 不绕过，如实报区域名与规则。"""
        self.grant()
        self.store.set_zone(zone("pubfarm", protect=["animals", "crops"]))

        dispatched = self.planner._dispatch_tool_call(
            "farm", {"action": "harvest"}, self.context(nearby=self.crops))

        self.assertFalse(dispatched)
        self.assertEqual(self.skills.calls, [], "被硬拦就停手，绝不绕过去挖")
        self.assertIn("pubfarm", self.planner._farm_note)
        self.assertIn("crops", self.planner._farm_note)

    def test_a_crop_inside_someone_elses_protected_field_is_not_touched(self):
        self.grant(radius=64)          # 我在自己的地里
        self.store.set_zone(zone("neighbour", protect=["crops"], x=40.0, radius=8.0))
        crops = blocks(("minecraft:wheat", 40, 64, 0, 40.0))   # 别人的保护区里

        dispatched = self.planner._dispatch_tool_call(
            "farm", {"action": "harvest"}, self.context(nearby=crops))

        self.assertFalse(dispatched)
        self.assertEqual(self.skills.calls, [], "最近的作物在别人保护区里 → 不碰")
        self.assertIn("neighbour", self.planner._farm_note)

    def test_own_field_with_no_protection_harvests_with_the_generic_dig(self):
        self.grant()
        self.store.set_zone(zone("own_field", protect=[]))

        dispatched = self.planner._dispatch_tool_call(
            "farm", {"action": "harvest"}, self.context(nearby=self.crops))

        self.assertTrue(dispatched)
        self.assertEqual(self.skills.names(), ["look_at", "mine_block"],
                         "收割＝通用挖（没有专门的收割命令）")
        self.assertIn("收割", self.planner._farm_note)

    def test_tilling_uses_the_generic_use_and_the_hotbar(self):
        """锄地＝手持锄头的**通用右键**（`use_item` 按住语义），不新增交互分支。"""
        self.grant()
        inventory = [{"slot": 3, "name": "minecraft:iron_hoe", "count": 1}]

        dispatched = self.planner._dispatch_tool_call(
            "farm", {"action": "till"}, self.context(inventory=inventory, nearby=self.crops))

        self.assertTrue(dispatched)
        self.assertEqual(self.skills.names(), ["select_hotbar", "look_at", "use_item"])
        self.assertEqual(self.skills.calls[0], ("select_hotbar", (3,), {}))
        self.assertIn("锄地", self.planner._farm_note)

    def test_missing_hoe_is_reported_plainly(self):
        self.grant()

        dispatched = self.planner._dispatch_tool_call(
            "farm", {"action": "till"}, self.context(nearby=self.crops))

        self.assertFalse(dispatched)
        self.assertEqual(self.skills.calls, [])
        self.assertIn("锄头", self.planner._farm_note)

    def test_missing_seed_is_reported_plainly(self):
        self.grant()

        dispatched = self.planner._dispatch_tool_call(
            "farm", {"action": "plant"}, self.context(nearby=self.crops))

        self.assertFalse(dispatched)
        self.assertEqual(self.skills.calls, [])
        self.assertIn("种子", self.planner._farm_note)

    def test_planting_sows_the_same_seed_from_the_backpack(self):
        self.grant()
        inventory = [{"slot": 1, "name": "minecraft:wheat_seeds", "count": 3}]

        dispatched = self.planner._dispatch_tool_call(
            "farm", {"action": "plant"}, self.context(inventory=inventory, nearby=self.crops))

        self.assertTrue(dispatched)
        self.assertEqual(self.skills.names(), ["select_hotbar", "look_at", "use_item"])
        self.assertEqual(self.skills.calls[0], ("select_hotbar", (1,), {}))

    def test_replant_keeps_the_same_crop_and_waits_for_farmland(self):
        """补种＝收割后**同一作物**的种子等耕地出现再播（缺种子就先不收）。"""
        self.grant()
        inventory = [{"slot": 2, "name": "minecraft:wheat_seeds", "count": 2}]
        ctx = self.context(inventory=inventory, nearby=self.crops)

        self.assertTrue(self.planner._dispatch_tool_call("farm", {"action": "replant"}, ctx))
        self.assertEqual(self.skills.names(), ["look_at", "mine_block"])
        self.assertEqual(self.planner._farm_pending["seed"], "wheat_seeds")

        self.skills.calls.clear()
        no_farmland = blocks(("minecraft:wheat", 1, 64, 1, 1.4))
        self.planner._claim_pending_replant(self.context(inventory=inventory,
                                                        nearby=no_farmland))
        self.assertEqual(self.skills.calls, [], "没看到耕地就先不种")

        ctx = self.context(inventory=inventory,
                           nearby=blocks(("minecraft:farmland", 3, 64, 1, 1.0)))
        self.planner._claim_pending_replant(ctx)

        self.assertEqual(self.skills.names(), ["select_hotbar", "look_at", "use_item"])
        self.assertEqual(self.skills.calls[0], ("select_hotbar", (2,), {}))

    def test_replant_without_the_seed_does_not_harvest(self):
        self.grant()

        dispatched = self.planner._dispatch_tool_call(
            "farm", {"action": "replant"}, self.context(nearby=self.crops))

        self.assertFalse(dispatched)
        self.assertEqual(self.skills.calls, [], "缺种子就别收（收了补不上）")
        self.assertIn("种子", self.planner._farm_note)

    def test_farm_helpers_know_their_crops(self):
        self.assertEqual(crop_of("minecraft:wheat"), "wheat")
        self.assertEqual(crop_of("minecraft:oak_log"), "")
        self.assertTrue(is_hoe("minecraft:iron_hoe"))
        self.assertEqual(hold_index([{"slot": 12, "name": "wheat_seeds"}], "wheat_seeds"), None)
        self.assertEqual(missing_items("plant", "wheat_seeds", {}), ["种子（wheat_seeds）"])
        self.assertEqual(choose_action("nonsense", self.crops, {})["reason"], "unknown_action")

    def test_plot_named_but_unknown_is_refused(self):
        dispatched = self.planner._dispatch_tool_call(
            "farm", {"plot": "nowhere", "action": "harvest"}, self.context(nearby=self.crops))

        self.assertFalse(dispatched)
        self.assertEqual(self.skills.calls, [])
        self.assertIn("nowhere", self.planner._farm_note)

    def test_plot_can_name_a_known_field(self):
        """`plot` 给了名字就按那块地的中心判授权（区域名或领地名的中心都认）。"""
        self.grant()
        self.store.set_zone(zone("field", protect=[]))

        dispatched = self.planner._dispatch_tool_call(
            "farm", {"plot": "field", "action": "harvest"}, self.context(nearby=self.crops))

        self.assertTrue(dispatched)
        self.assertEqual(self.skills.names(), ["look_at", "mine_block"])


# ── ⑦ 提示层与分层纪律 ─────────────────────────────────────────


class PromptAndLayerTests(unittest.TestCase):
    def test_l1_gains_haul_and_farm_rules_within_the_budget(self):
        p = planner()
        system = p.build_prompt_layers(
            "Steve", "hi", {"task_state": {"kind": "idle", "status": "idle"}}, "AI").system

        self.assertIn("搬运 haul", system)
        self.assertIn("种田用 farm", system)
        self.assertIn("源箱留底", system)
        self.assertLessEqual(len(system), 1700, "静态前缀预算只降不抬")

    def test_tools_are_advertised_and_phases_include_them(self):
        from agent.planner import _actions_for_phase

        names = {item["function"]["name"] for item in build_tool_schemas("idle")}
        self.assertIn("haul", names)
        self.assertIn("farm", names)
        for phase in ("idle", "travel", "gather", "craft", "survive"):
            available = [name for name, _, _ in _actions_for_phase(phase)]
            self.assertIn("haul", available, f"{phase} 阶段缺少 haul")
            self.assertIn("farm", available, f"{phase} 阶段缺少 farm")

    def test_coordinates_stay_out_of_the_server_and_person_layers(self):
        """坐标只该待在 **world 层**（领地文件）：server 层与 person 层一点都不进。"""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        session = Session.__new__(Session)
        session.runtime = {"world": {"server": SERVER, "world_name": WORLD},
                           "player": {"x": 0.0, "y": 64.0, "z": 0.0}}
        session.people = PersonMemoryStore(root=root / "people")
        session.conversation = ConversationStore(root=root / "conversations")
        session.conversation.use_namespace(SERVER, WORLD)
        session.poi = None
        session.places = None
        session._people_seen = {}
        session._present_person_facts = {}
        session._replay_layers = set()
        session.people.identify(STEVE, "Steve", server_key=SERVER)
        session._note_person({"sender": "Steve", "uuid": STEVE, "message": "把这箱铁搬到前哨"})
        session._record_conversation_chat({"sender": "Steve", "uuid": STEVE,
                                           "message": "把这箱铁搬到前哨"})
        store = TerritoryStore(root=root / "territory", server=SERVER, world_name=WORLD,
                               dimension=OVERWORLD)
        store.remember_container("warehouse", x=SENTINEL[0], y=SENTINEL[1], z=SENTINEL[2],
                                 policy="free")
        skills = RecordingSkills()
        p = planner(store=store, skills=skills)
        context = {"actor_tier": "master", "player": {"x": 0.0, "y": 64.0, "z": 0.0},
                   "open_container": {}}

        p._dispatch_tool_call("haul", {"from": "warehouse", "to": "self"}, context)
        p._dispatch_tool_call("farm", {"action": "harvest"}, context)

        person_file = (root / "people" / f"uuid_{STEVE}.json").read_text(encoding="utf-8")
        chat_log = session.conversation.events_path.read_text(encoding="utf-8")
        world_file = store.path.read_text(encoding="utf-8")

        self.assertNotIn(str(SENTINEL[0]), person_file)
        self.assertNotIn(str(SENTINEL[2]), chat_log)
        self.assertIn("前哨", chat_log, "server 层照旧留着一次性上下文（这里不是坐标）")
        self.assertIn(str(SENTINEL[0]), world_file, "坐标只在 world 层（领地文件）里")
        self.assertNotIn(str(SENTINEL[0]), str(p.haul_status()))
        self.assertNotIn(str(SENTINEL[0]), str(p.get_status()["haul_audit"]))


if __name__ == "__main__":
    unittest.main()
