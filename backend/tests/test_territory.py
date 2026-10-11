"""领地 / 多基地 / 容器策略 / 区域保护 / 造床 的验收测试。

覆盖（对照票据的验收清单）：
  ① 坐标只落 **world 层**（换服 / 换世界 / 换维度不串号）；
  ② **保护优先于允许**（区域硬保护压过领地授权；多区重叠取最保守）；
  ③ friend 只能标**自己的**容器为可交互/传递，**默认只给传递权、不给任意取用权**；
  ④ 撤销授权**立即生效**且留审计；
  ⑤ "不在任何基地 = 野外"按**最低权限**；
  ⑥ 造床：缺料 / 不安全**如实报原因**（不硬凑）；
  ⑦ 被区域硬拦时**任务失败原因带区域名与规则**；
  ⑧ 区域回灌幂等 + 命令帧与 Java 契约逐字一致；
  ⑨ route_export 接线（拿得到就推、拿不到维持 no_geometry）；
  ⑩ L1 只多了几条规则（不抬预算）、L2 才带坐标。
"""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from agent.action_manager import ActionManager
from agent.permissions import PermissionsStore
from agent.planner import Planner, build_tool_schemas
from agent.session import Session
from agent.skills import Skills
from agent.task_list import TaskList
from agent.territory import (POLICY_BORROW_ONLY, TerritoryStore, bed_attempt, bed_note,
                             current_base, home_situation, may_act, nearest_base,
                             plan_zone_replay, protection_hit, zone_args, zones_match)

SERVER = "play.example:25565"
WORLD = "world-a"
OVERWORLD = "minecraft:overworld"
NETHER = "minecraft:the_nether"


def zone(name="farm", *, kind="farm", protect=("animals", "crops"), x=0.0, y=64.0, z=0.0,
         radius=16.0):
    return {"name": name, "kind": kind, "protect": list(protect), "shape": "radius",
            "center": {"x": x, "y": y, "z": z}, "radius": radius, "box": None}


def territory(name="yard", *, grants=("take", "place", "break"), x=0.0, y=64.0, z=0.0,
               radius=32.0, since=1.0):
    return {"name": name, "kind": "base", "grants": list(grants),
            "center": {"x": x, "y": y, "z": z}, "radius": radius, "since": since}


class DummyMemory:
    def build_context(self):
        return {"interaction_summary": "无"}

    def get_recent_context(self, _count=5):
        return []


class RecordingSkills:
    """记录下发的帧；只实现测试用得到的方法。"""

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


# ── ① world 层分区：坐标只在这里，换服/换世界/换维度不串号 ────────


class WorldLayerTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def store(self, *, server=SERVER, world=WORLD, dimension=OVERWORLD) -> TerritoryStore:
        return TerritoryStore(root=self.root / "territory", server=server,
                              world_name=world, dimension=dimension)

    def test_all_coordinate_objects_land_in_one_world_layer_file(self):
        store = self.store()
        store.set_territory("yard", grants=("take",), x=1, y=2, z=3, radius=8)
        store.set_base("home", anchor={"x": 1, "y": 2, "z": 3}, radius=32)
        store.remember_container("chest_a", x=4, y=5, z=6, policy="borrow_only")
        store.set_zone(zone())

        self.assertTrue(store.path.exists())
        self.assertEqual(store.path.name, f"{store.namespace}.json")
        self.assertIn("world-a", store.namespace)
        self.assertIn("overworld", store.namespace)
        # 三个对象 + 区域都在同一个 world 层文件里，没有散落到别处。
        reloaded = self.store()
        self.assertEqual(len(reloaded.territories()), 1)
        self.assertEqual(len(reloaded.bases()), 1)
        self.assertEqual(len(reloaded.containers()), 1)
        self.assertEqual(len(reloaded.zones()), 1)

    def test_switching_world_or_dimension_does_not_bleed(self):
        store = self.store()
        store.set_base("home", anchor={"x": 1, "y": 2, "z": 3}, radius=32)

        store.use_namespace(SERVER, WORLD, NETHER)
        self.assertEqual(store.bases(), [], "换维度必须是另一份（坐标不同维度不通用）")
        store.set_base("nether_camp", anchor={"x": 0, "y": 0, "z": 0}, radius=16)

        store.use_namespace("other.server:25565", WORLD, OVERWORLD)
        self.assertEqual(store.bases(), [], "换服必须是另一份（绝不串号）")

        store.use_namespace(SERVER, WORLD, OVERWORLD)
        self.assertEqual([row["name"] for row in store.bases()], ["home"])

    def test_absorbed_zones_survive_a_restart(self):
        store = self.store()
        store.absorb_state({"zones": [zone()]})

        reloaded = self.store()

        self.assertEqual(reloaded.zones()[0]["name"], "farm")
        self.assertEqual(reloaded.zones()[0]["protect"], ["animals", "crops"])

    def test_absorb_never_deletes_on_an_empty_report(self):
        """模组报空列表是"我这边是空的"，不是"删掉"（后端 = 持久真相）。"""
        store = self.store()
        store.absorb_state({"zones": [zone()]})

        store.absorb_state({"zones": []})

        self.assertEqual(len(store.zones()), 1)


# ── ② 保护优先于允许 ────────────────────────────────────────────


class ProtectionPrecedenceTests(unittest.TestCase):
    def test_zone_protection_overrides_a_territory_grant(self):
        hit = may_act([territory(grants=("break",))], [zone(protect=("crops",))],
                      0.0, 64.0, 0.0, "mine")

        self.assertFalse(hit["allowed"], "区域保护是硬拦，领地授权再多也不翻案")
        self.assertEqual(hit["reason"], "zone_protection")
        self.assertEqual(hit["zone"], "farm")
        self.assertEqual(hit["rule"], "crops")

    def test_territory_grant_allows_when_no_zone_hits(self):
        verdict = may_act([territory(grants=("break",))], [], 0.0, 64.0, 0.0, "mine")

        self.assertTrue(verdict["allowed"])
        self.assertEqual(verdict["reason"], "granted")

    def test_outside_every_territory_is_the_lowest_permission(self):
        verdict = may_act([territory()], [], 500.0, 64.0, 500.0, "take")

        self.assertFalse(verdict["allowed"])
        self.assertEqual(verdict["reason"], "outside_grant", "范围外一律回落借用台账")

    def test_missing_grant_is_not_within_grant(self):
        verdict = may_act([territory(grants=("take",))], [], 0.0, 64.0, 0.0, "mine")

        self.assertFalse(verdict["allowed"])
        self.assertEqual(verdict["reason"], "outside_grant")

    def test_overlapping_zones_pick_the_most_conservative_rule(self):
        """挖的动作同时命中 crops 与 blocks 两个区域 → 按保守序取 crops。"""
        zones = [zone("pen", kind="animal_pen", protect=("blocks",)),
                 zone("farm", kind="farm", protect=("crops",))]

        hit = protection_hit(zones, 0.0, 64.0, 0.0, "mine")

        self.assertEqual(hit["rule"], "crops", "保守序：crops 比 blocks 更保守")

    def test_attack_is_blocked_by_animals_but_mine_is_not(self):
        zones = [zone(protect=("animals",))]

        self.assertEqual(protection_hit(zones, 0.0, 64.0, 0.0, "attack")["rule"], "animals")
        self.assertIsNone(protection_hit(zones, 0.0, 64.0, 0.0, "mine"))

    def test_box_shape_is_inclusive_and_excludes_points_outside(self):
        row = {"name": "barn", "kind": "build", "protect": ["blocks"], "shape": "box",
               "center": {"x": 0, "y": 0, "z": 0}, "box": {"min_x": 0, "min_y": 0, "min_z": 0,
                                                           "max_x": 10, "max_y": 10, "max_z": 10}}
        self.assertEqual(protection_hit([row], 10, 10, 10, "place")["rule"], "blocks")
        self.assertIsNone(protection_hit([row], 11, 5, 5, "place"))

    def test_current_base_is_the_nearest_anchor_within_radius(self):
        bases = [{"name": "far", "kind": "base", "anchor": {"x": 100, "y": 64, "z": 100},
                  "radius": 32.0},
                 {"name": "near", "kind": "camp", "anchor": {"x": 0, "y": 64, "z": 0},
                  "radius": 32.0}]

        self.assertEqual(current_base(bases, 5.0, 64.0, 5.0)["name"], "near")
        self.assertIsNone(current_base(bases, 60.0, 64.0, 0.0), "不在任何基地 = 野外")
        self.assertEqual(nearest_base(bases, 60.0, 64.0, 0.0)["name"], "near")


# ── ③ / ④ 容器策略与撤销（权限查表 + 立即生效 + 审计）───────────


class ContainerPolicyTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.permissions = PermissionsStore(path=self.root / "permissions.json")
        self.permissions.set({"master": ["Steve"], "friends": ["Alex"]})
        self.store = TerritoryStore(root=self.root / "territory", server=SERVER,
                                    world_name=WORLD, dimension=OVERWORLD)

    def test_master_may_grant_any_policy(self):
        decision = self.permissions.container_grant_policy("master", actor="Steve",
                                                           owner="Alex", policy="free")

        self.assertTrue(decision["ok"])
        self.assertEqual(decision["policy"], "free")

    def test_friend_can_only_mark_their_own_and_free_is_clamped_to_transfer(self):
        mine = self.permissions.container_grant_policy("friend", actor="Alex", owner="Alex",
                                                      policy="free")
        theirs = self.permissions.container_grant_policy("friend", actor="Alex", owner="Steve",
                                                        policy="borrow_only")

        self.assertTrue(mine["ok"])
        self.assertEqual(mine["policy"], POLICY_BORROW_ONLY)
        self.assertTrue(mine["clamped"], "friend 不能给自己撑任意取用权")
        self.assertFalse(theirs["ok"])
        self.assertEqual(theirs["reason"], "not_owner")

    def test_guest_can_grant_nothing(self):
        decision = self.permissions.container_grant_policy("guest", actor="Bob", owner="Bob",
                                                           policy="borrow_only")

        self.assertFalse(decision["ok"])
        self.assertEqual(decision["reason"], "tier_not_allowed")

    def test_only_master_may_grant_territory(self):
        self.assertTrue(self.permissions.can_authorize_territory("master"))
        self.assertFalse(self.permissions.can_authorize_territory("friend"))
        self.assertFalse(self.permissions.can_authorize_territory("guest"))

    def test_planner_clamps_a_friends_free_request_and_says_so(self):
        p = planner(store=self.store, permissions=self.permissions)
        handled = p._dispatch_tool_call(
            "remember_container",
            {"name": "chest_a", "x": 1, "y": 64, "z": 2, "policy": "free"},
            {"actor_tier": "friend", "actor_name": "Alex"})

        self.assertTrue(handled)
        self.assertEqual(self.store.container("chest_a")["policy"], POLICY_BORROW_ONLY)
        self.assertIn("传递权", p._territory_note)

    def test_guest_cannot_remember_a_container(self):
        p = planner(store=self.store, permissions=self.permissions)
        handled = p._dispatch_tool_call(
            "remember_container", {"name": "chest_b", "x": 1, "y": 64, "z": 2},
            {"actor_tier": "guest", "actor_name": "Bob"})

        self.assertFalse(handled)
        self.assertIsNone(self.store.container("chest_b"))

    def test_revoke_takes_effect_immediately_and_is_audited(self):
        self.store.remember_container("chest_a", x=1, y=64, z=2, policy="free")

        removed = self.store.revoke_container("chest_a")

        self.assertIsNotNone(removed)
        self.assertIsNone(self.store.container("chest_a"), "撤销必须立即生效（没有缓存）")
        audited = self.store.audit_log(10)
        self.assertEqual(audited[0]["action"], "revoke_container")
        self.assertEqual(audited[0]["target"], "chest_a")

    def test_master_revoke_of_a_friend_grant_works(self):
        self.store.remember_container("chest_a", x=1, y=64, z=2, policy="borrow_only",
                                      owner="Alex", granted_by={"tier": "friend", "name": "Alex"})
        p = planner(store=self.store, permissions=self.permissions)

        handled = p._dispatch_tool_call("revoke_container", {"name": "chest_a"},
                                        {"actor_tier": "master", "actor_name": "Steve"})

        self.assertTrue(handled)
        self.assertIsNone(self.store.container("chest_a"))


class ContainerPolicyEnforcementTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.permissions = PermissionsStore(path=self.root / "permissions.json")
        self.permissions.set({"master": ["Steve"]})
        self.store = TerritoryStore(root=self.root / "territory", server=SERVER,
                                    world_name=WORLD, dimension=OVERWORLD)
        self.skills = RecordingSkills()

    def _open(self, name):
        return {"open_container": {"title": name, "slots": 63, "items": []}}

    def test_no_touch_container_is_refused(self):
        self.store.remember_container("vault", x=1, y=64, z=2, policy="no_touch")
        p = planner(store=self.store, permissions=self.permissions, skills=self.skills)

        handled = p._dispatch_tool_call(
            "container_move", {"from_slot": 0, "count": 1},
            {**self._open("vault"), "actor_tier": "master"})

        self.assertFalse(handled)
        self.assertEqual(self.skills.names(), [])
        self.assertIn("no_touch", p._craft_note)

    def test_free_container_can_be_taken_from_without_per_use_authority(self):
        """所有权已交给它（free）→ 随取；审计仍要留痕。"""
        self.store.remember_container("own_chest", x=1, y=64, z=2, policy="free")
        p = planner(store=self.store, permissions=self.permissions, skills=self.skills)

        handled = p._dispatch_tool_call(
            "container_move", {"from_slot": 0, "count": 2},
            {**self._open("own_chest"), "actor_tier": "guest", "actor_name": "Bob"})

        self.assertTrue(handled)
        self.assertEqual(self.skills.names(), ["container_move"])
        self.assertEqual(self.store.audit_log(1)[0]["action"], "take_free")

    def test_unknown_container_falls_back_to_the_borrow_path(self):
        p = planner(store=self.store, permissions=self.permissions, skills=self.skills)

        handled = p._dispatch_tool_call(
            "container_move", {"from_slot": 0, "count": 1},
            {**self._open("someone_else"), "actor_tier": "master"})

        self.assertTrue(handled, "master 授权下走原路（借用/持有链路）")
        self.assertEqual(self.skills.names(), ["container_move"])


# ── ⑥ 造床：缺料 / 不安全如实报 ─────────────────────────────────


class BedTests(unittest.TestCase):
    def test_missing_materials_are_reported_honestly(self):
        decision = bed_attempt([{"name": "minecraft:red_wool", "count": 1}], [])

        self.assertEqual(decision["action"], "missing")
        note = bed_note(decision)
        self.assertIn("羊毛 2", note)
        self.assertIn("木板 3", note)

    def test_hostiles_make_it_unsafe(self):
        decision = bed_attempt([{"name": "white_wool", "count": 3},
                                {"name": "oak_planks", "count": 3}],
                               [{"type": "mob", "name": "zombie"}])

        self.assertEqual(decision["action"], "unsafe")
        self.assertIn("zombie", bed_note(decision))

    def test_enough_materials_and_safe_means_craft(self):
        decision = bed_attempt([{"name": "white_wool", "count": 4},
                                {"name": "oak_planks", "count": 5}], [])

        self.assertEqual(decision["action"], "craft")


class HomeTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.permissions = PermissionsStore(path=self.root / "permissions.json")
        self.permissions.set({"master": ["Steve"]})
        self.store = TerritoryStore(root=self.root / "territory", server=SERVER,
                                    world_name=WORLD, dimension=OVERWORLD)
        self.skills = RecordingSkills()
        self.store.set_base("home", anchor={"x": 0, "y": 64, "z": 0}, radius=32,
                            granted_by={"tier": "master", "name": "Steve"})

    def test_set_home_records_the_bed_found_in_range(self):
        p = planner(store=self.store, permissions=self.permissions, skills=self.skills)
        context = {"actor_tier": "master", "player": {"x": 1, "y": 64, "z": 1},
                   "nearby_beds": [{"id": "minecraft:red_bed", "x": 5, "y": 64, "z": 5,
                                    "distance": 6.0}]}

        handled = p._dispatch_tool_call("set_home", {}, context)

        self.assertTrue(handled)
        self.assertEqual(self.store.base("home")["home_bed"], {"x": 5.0, "y": 64.0, "z": 5.0})

    def test_set_home_without_materials_reports_what_is_missing(self):
        p = planner(store=self.store, permissions=self.permissions, skills=self.skills)
        context = {"actor_tier": "master", "player": {"x": 1, "y": 64, "z": 1},
                   "nearby_beds": [], "entities": [],
                   "inventory": [{"name": "white_wool", "count": 3}]}

        handled = p._dispatch_tool_call("set_home", {}, context)

        self.assertFalse(handled)
        self.assertIn("木板 3", p._territory_note)
        self.assertEqual(self.skills.names(), [], "缺料时一帧都不该发")

    def test_set_home_crafts_when_materials_are_there_and_it_is_safe(self):
        p = planner(store=self.store, permissions=self.permissions, skills=self.skills)
        context = {"actor_tier": "master", "player": {"x": 1, "y": 64, "z": 1},
                   "nearby_beds": [], "entities": [],
                   "inventory": [{"name": "white_wool", "count": 3},
                                 {"name": "oak_planks", "count": 3}]}

        handled = p._dispatch_tool_call("set_home", {}, context)

        self.assertTrue(handled)
        self.assertEqual(self.skills.names(), ["craft_via_recipe"])

    def test_set_home_reports_unsafe_when_hostiles_are_close(self):
        p = planner(store=self.store, permissions=self.permissions, skills=self.skills)
        context = {"actor_tier": "master", "player": {"x": 1, "y": 64, "z": 1},
                   "nearby_beds": [], "entities": [{"type": "mob", "name": "creeper"}],
                   "inventory": [{"name": "white_wool", "count": 3},
                                 {"name": "oak_planks", "count": 3}]}

        handled = p._dispatch_tool_call("set_home", {}, context)

        self.assertFalse(handled)
        self.assertIn("creeper", p._territory_note)
        self.assertEqual(self.skills.names(), [])

    def test_go_home_walks_to_the_bed_then_the_anchor(self):
        p = planner(store=self.store, permissions=self.permissions, skills=self.skills)
        p._dispatch_tool_call("go_home", {}, {"player": {"x": 0, "y": 64, "z": 0}})
        self.assertEqual(self.skills.calls[-1][0], "move_to")
        self.assertEqual(self.skills.calls[-1][1], (0.0, 64.0, 0.0))

        self.store.set_home_bed("home", {"x": 5, "y": 64, "z": 5})
        p._dispatch_tool_call("go_home", {}, {"player": {"x": 0, "y": 64, "z": 0}})
        self.assertEqual(self.skills.calls[-1][1], (5.0, 64.0, 5.0))

    def test_being_away_too_long_is_only_reported(self):
        bases = self.store.bases()
        situation = home_situation(bases, 400.0, 64.0, 0.0, away_since=1000.0, now=1000.0 + 900)

        self.assertTrue(situation["away"])
        self.assertTrue(situation["over"], "超阈值只上报（over=True），绝不自己跑回去")
        p = planner(store=self.store, permissions=self.permissions, skills=self.skills)
        text = p._render_territory({"territory": self.store.snapshot(),
                                    "current_base": None, "home_status": situation})
        self.assertIn("只上报", text)


# ── ⑦ 被区域拦下 → 任务失败原因带区域名与规则 ────────────────────


def bare_session():
    s = Session.__new__(Session)
    s.action_manager = ActionManager()
    s.runtime = {}
    s.tasks = TaskList()
    s._manual_action_reqs = set()
    s._manual_task_kind = "mine_block"
    s._manual_command_until = 0.0
    s._task_idle_since = None
    s._zone_blocks = []
    # 审计只留内存，别在测试里往仓库 data/ 落盘。
    s._security_log_failed = True
    return s


class ZoneBlockTests(unittest.TestCase):
    def test_blocked_receipt_becomes_the_task_failure_reason(self):
        s = bare_session()
        task = s.tasks.add("mine_block", owner="Steve", owner_tier="master", status="active")

        s.handle_event("command_response", {
            "id": "req_1", "success": False,
            "data": {"blocked_by_zone": "farm", "rule": "crops", "action": "mine"},
            "error": "blocked_by_zone:farm rule:crops"})

        stored = s.tasks.get(task.id)
        self.assertEqual(stored.status, "failed")
        self.assertIn("farm", stored.failure_reason)
        self.assertIn("crops", stored.failure_reason)

    def test_recent_blocks_are_kept_for_the_next_context(self):
        s = bare_session()
        s.handle_event("command_response", {
            "id": "req_2", "success": False,
            "data": {"blocked_by_zone": "pen", "rule": "animals", "action": "attack"}})

        self.assertEqual(s._zone_block_context()[0]["zone"], "pen")

    def test_block_is_rendered_in_the_l2_context(self):
        p = planner()
        text = p._render_territory({"zone_blocks": [{"zone": "farm", "rule": "crops",
                                                     "action": "mine"}]})

        self.assertIn("farm", text)
        self.assertIn("crops", text)


# ── ⑧ 区域回灌 + 命令帧与 Java 契约逐字一致 ─────────────────────


class ZoneReplayTests(unittest.TestCase):
    def test_identical_zones_are_not_pushed(self):
        report = plan_zone_replay([zone()], [zone()])

        self.assertEqual(report["zones"], [])
        self.assertEqual(report["skipped"][0]["reason"], "identical")

    def test_missing_zone_is_pushed_with_java_arg_names(self):
        report = plan_zone_replay([zone()], [])

        self.assertEqual(report["zones"], [{"name": "farm", "kind": "farm",
                                            "x": 0.0, "y": 64.0, "z": 0.0,
                                            "radius": 16.0,
                                            "protect": ["animals", "crops"]}])

    def test_mod_only_zones_are_adopted_not_dropped(self):
        report = plan_zone_replay([], [zone()])

        self.assertEqual([row["name"] for row in report["adopted"]], ["farm"])

    def test_shape_less_zone_is_blocked_never_pushed(self):
        broken = {"name": "nowhere", "kind": "farm", "protect": ["crops"],
                  "center": {"x": 0, "y": 0, "z": 0}, "radius": None, "box": None}

        report = plan_zone_replay([broken], [])

        self.assertEqual(report["zones"], [])
        self.assertEqual(report["blocked"][0]["reason"], "no_shape")

    def test_zone_args_matches_the_java_field_names(self):
        boxed = {"name": "barn", "kind": "build", "protect": ["blocks"],
                 "center": {"x": 1, "y": 2, "z": 3},
                 "box": {"min_x": 0, "min_y": 0, "min_z": 0,
                         "max_x": 4, "max_y": 4, "max_z": 4}}

        self.assertEqual(zone_args(boxed),
                         {"name": "barn", "kind": "build", "x": 1.0, "y": 2.0, "z": 3.0,
                          "box": {"min_x": 0.0, "min_y": 0.0, "min_z": 0.0,
                                  "max_x": 4.0, "max_y": 4.0, "max_z": 4.0},
                          "protect": ["blocks"]})
        self.assertTrue(zones_match(boxed, boxed))
        self.assertFalse(zones_match(boxed, zone()))


class WireContractTests(unittest.TestCase):
    def setUp(self):
        self.wire = RecordingWire()

    def test_define_zone_sends_exactly_the_java_arg_names(self):
        skills = Skills(wire=self.wire)
        skills.define_zone("farm", "farm", 1.0, 64.0, 2.0, radius=16.0,
                           protect=["animals", "crops"])

        self.assertEqual(self.wire.sent, [(
            "define_zone",
            {"name": "farm", "kind": "farm", "x": 1.0, "y": 64.0, "z": 2.0,
             "radius": 16.0, "protect": ["animals", "crops"]})])

    def test_clear_zone_and_route_export_frames(self):
        skills = Skills(wire=self.wire)
        skills.clear_zone("farm")
        skills.route_export("tunnel")

        self.assertEqual(self.wire.sent, [("clear_zone", {"name": "farm"}),
                                          ("route_export", {"name": "tunnel"})])


class RecordingWire:
    def __init__(self):
        self.sent = []

    def send_command(self, cmd, args):
        self.sent.append((cmd, args))
        return f"req-{len(self.sent)}"


# ── ⑨ route_export 接线（按需取点列，拿不到维持 no_geometry）────


class RouteExportWiringTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def session(self, *, wire, places):
        s = Session.__new__(Session)
        s.skills = Skills(wire=wire)
        s.places = places
        s._route_exports = {}
        return s

    def test_blocked_geometry_less_route_asks_the_mod_to_export(self):
        wire = RecordingWire()
        s = self.session(wire=wire, places=SimpleNamespace(namespace="ns"))
        blocked = [{"kind": "route", "name": "tunnel", "reason": "no_geometry"},
                   {"kind": "station", "name": "home", "reason": "no_coords"}]

        requested = s._request_route_exports(blocked, s.places)

        self.assertEqual(requested, ["tunnel"])
        self.assertEqual(wire.sent, [("route_export", {"name": "tunnel"})])
        self.assertEqual(len(s._route_exports), 1)

    def test_successful_export_is_stored_and_pushed_back(self):
        from agent.world_memory import WorldMemoryStore
        wire = RecordingWire()
        places = WorldMemoryStore(stations_root=self.root / "stations",
                                  routes_root=self.root / "routes")
        places.use_namespace(SERVER, WORLD, OVERWORLD)
        s = self.session(wire=wire, places=places)
        points = [{"x": 1.0, "y": 64.0, "z": 2.0, "yaw": 0.0},
                  {"x": 9.0, "y": 66.0, "z": 2.0, "yaw": 90.0}]
        s._route_exports["req-1"] = {"name": "tunnel", "namespace": places.namespace}

        absorbed = s._absorb_route_export({"id": "req-1", "success": True,
                                           "data": {"name": "tunnel", "points": points}})

        self.assertTrue(absorbed)
        self.assertEqual(len(places.route("tunnel")["points"]), 2)
        self.assertEqual(wire.sent[-1][0], "remember_route")
        self.assertEqual(wire.sent[-1][1]["name"], "tunnel")

    def test_failed_export_keeps_no_geometry_and_pushes_nothing(self):
        wire = RecordingWire()
        s = self.session(wire=wire, places=SimpleNamespace(namespace="ns"))
        s._route_exports["req-2"] = {"name": "tunnel", "namespace": "ns"}

        absorbed = s._absorb_route_export({"id": "req-2", "success": False,
                                           "data": {"reason": "no_geometry"}})

        self.assertFalse(absorbed)
        self.assertEqual(wire.sent, [], "拿不到点列就维持 no_geometry，绝不编")


# ── ⑩ L1 只多几条规则、L2 才带坐标 ──────────────────────────────


class PromptLayerTests(unittest.TestCase):
    def test_l1_gains_the_territory_rules_and_stays_within_budget(self):
        layers = planner().build_prompt_layers(
            "Steve", "hi", {"task_state": {"kind": "idle", "status": "idle"}}, "AI")

        self.assertIn("只在已授权的领地/基地范围内自由动手", layers.system)
        self.assertIn("保护区", layers.system)
        self.assertIn("自己造一张", layers.system)
        self.assertLess(len(layers.system), 1700, "静态前缀预算只降不抬")

    def test_l1_l2_and_tools_are_all_wired_for_the_new_surface(self):
        tools = {item["function"]["name"] for item in build_tool_schemas("idle")}

        for name in ("list_territory", "set_base", "del_base", "remember_container",
                     "grant_container", "revoke_container", "define_zone", "clear_zone",
                     "set_home", "go_home"):
            self.assertIn(name, tools)

    def test_coordinates_and_saves_show_up_in_l2_not_in_l1(self):
        snapshot = {"territories": [], "bases": [{"name": "home", "kind": "base",
                                                  "anchor": {"x": 7, "y": 64, "z": -9876},
                                                  "radius": 32, "home_bed": None}],
                    "containers": [{"name": "chest_a", "x": 1, "y": 64, "z": 2,
                                    "policy": "borrow_only"}],
                    "zones": [zone()]}
        p = planner()
        context = {"territory": snapshot, "current_base": None}
        layers = p.build_prompt_layers("Steve", "hi", context, "AI")
        text = p._render_territory(context)

        self.assertIn("home", text)
        self.assertIn("野外", text)
        self.assertIn("chest_a", text)
        self.assertIn("farm", text)
        self.assertNotIn("-9876", layers.system, "坐标绝不进 L1 静态前缀")
        self.assertNotIn("chest_a", layers.system)

    def test_list_territory_reports_what_is_known(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = TerritoryStore(root=Path(tmp.name) / "territory", server=SERVER,
                               world_name=WORLD, dimension=OVERWORLD)
        store.set_base("home", anchor={"x": 0, "y": 64, "z": 0}, radius=32)
        store.remember_container("chest_a", x=1, y=64, z=2, policy="free")
        store.set_zone(zone())
        p = planner(store=store)

        handled = p._dispatch_tool_call("list_territory", {}, {})

        self.assertTrue(handled)
        self.assertIn("home", p._territory_note)
        self.assertIn("chest_a", p._territory_note)
        self.assertIn("farm", p._territory_note)


class TerritoryGrantDispatchTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.permissions = PermissionsStore(path=self.root / "permissions.json")
        self.permissions.set({"master": ["Steve"], "friends": ["Alex"]})
        self.store = TerritoryStore(root=self.root / "territory", server=SERVER,
                                    world_name=WORLD, dimension=OVERWORLD)
        self.skills = RecordingSkills()

    def test_master_can_grant_and_revoke_a_territory(self):
        p = planner(store=self.store, permissions=self.permissions, skills=self.skills)
        context = {"actor_tier": "master", "actor_name": "Steve",
                   "player": {"x": 10.0, "y": 64.0, "z": 10.0}}

        granted = p._dispatch_tool_call(
            "grant_territory", {"name": "yard", "radius": 32, "grants": "take,break"}, context)

        self.assertTrue(granted)
        self.assertEqual(self.store.territory("yard")["grants"], ["take", "break"])

        revoked = p._dispatch_tool_call("revoke_territory", {"name": "yard"}, context)

        self.assertTrue(revoked)
        self.assertIsNone(self.store.territory("yard"), "撤销立即生效")

    def test_friend_cannot_grant_a_territory(self):
        p = planner(store=self.store, permissions=self.permissions, skills=self.skills)

        handled = p._dispatch_tool_call(
            "grant_territory", {"name": "yard", "radius": 32, "grants": "take"},
            {"actor_tier": "friend", "actor_name": "Alex",
             "player": {"x": 0.0, "y": 64.0, "z": 0.0}})

        self.assertFalse(handled)
        self.assertIsNone(self.store.territory("yard"))

    def test_master_define_zone_persists_and_calls_java_with_java_arg_names(self):
        p = planner(store=self.store, permissions=self.permissions, skills=self.skills)
        context = {"actor_tier": "master", "actor_name": "Steve",
                   "player": {"x": 0.0, "y": 64.0, "z": 0.0}}

        handled = p._dispatch_tool_call(
            "define_zone", {"name": "farm", "kind": "farm", "x": 0.0, "y": 64.0, "z": 0.0,
                            "radius": 16.0, "protect": "animals,crops"}, context)

        self.assertTrue(handled)
        self.assertEqual(self.store.zone("farm")["protect"], ["animals", "crops"])
        call = [entry for entry in self.skills.calls if entry[0] == "define_zone"][0]
        self.assertEqual(call[2]["name"], "farm")
        self.assertEqual(call[2]["protect"], ["animals", "crops"])

    def test_friend_cannot_define_a_zone(self):
        p = planner(store=self.store, permissions=self.permissions, skills=self.skills)

        handled = p._dispatch_tool_call(
            "define_zone", {"name": "farm", "radius": 16.0, "protect": "crops"},
            {"actor_tier": "friend", "actor_name": "Alex",
             "player": {"x": 0.0, "y": 64.0, "z": 0.0}})

        self.assertFalse(handled)
        self.assertIsNone(self.store.zone("farm"))
        self.assertEqual(self.skills.names(), [])

    def test_clear_zone_removes_locally_and_tells_java(self):
        self.store.set_zone(zone())
        p = planner(store=self.store, permissions=self.permissions, skills=self.skills)

        handled = p._dispatch_tool_call("clear_zone", {"name": "farm"},
                                        {"actor_tier": "master", "actor_name": "Steve"})

        self.assertTrue(handled)
        self.assertIsNone(self.store.zone("farm"))
        self.assertEqual(self.skills.names(), ["clear_zone"])


class TerritorySessionTests(unittest.TestCase):
    """会话侧接线：按 world 分区吸收区域 + 进世界时幂等回灌 + 基地/回家事实进上下文。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def session(self):
        s = Session.__new__(Session)
        s.territories = TerritoryStore(root=self.root / "territory")
        s.skills = RecordingSkills()
        s._replay_layers = set()
        s._zone_blocks = []
        s._home_away_since = None
        s.in_world = True
        s._actor = {"name": "", "tier": "guest"}
        return s

    @staticmethod
    def state(*, server=SERVER, world=WORLD, dimension=OVERWORLD, zones=None):
        data = {"world": {"server": server, "world_name": world},
                "player": {"dimension": dimension, "x": 0.0, "y": 64.0, "z": 0.0}}
        if zones is not None:
            data["zones"] = zones
        return data

    def test_zones_absorb_into_the_world_partition_and_never_bleed_across_servers(self):
        s = self.session()
        s._absorb_territory(self.state(zones=[zone()]))
        self.assertEqual([row["name"] for row in s.territories.zones()], ["farm"])

        s._absorb_territory(self.state(server="other.server:25565", zones=[]))

        self.assertEqual(s.territories.zones(), [], "换服是另一份分区（绝不串号）")

    def test_zone_replay_pushes_saved_zones_once(self):
        s = self.session()
        s.territories.use_namespace(SERVER, WORLD, OVERWORLD)
        s.territories.set_zone(zone())

        first = s._absorb_territory(self.state(zones=[]))
        second = s._replay_zones(self.state(zones=[]), s.territories)

        self.assertEqual([call[0] for call in s.skills.calls].count("define_zone"), 1)
        define_call = [call for call in s.skills.calls if call[0] == "define_zone"][0]
        self.assertEqual(define_call[2]["name"], "farm")
        self.assertFalse(second["replayed"], "同一 world 只推一次（幂等标记）")
        self.assertIsNotNone(first)

    def test_context_carries_the_base_and_home_facts(self):
        s = self.session()
        s.territories.use_namespace(SERVER, WORLD, OVERWORLD)
        s.territories.set_base("home", anchor={"x": 0, "y": 64, "z": 0}, radius=32)
        context = {"player": {"x": 1000.0, "y": 64.0, "z": 0.0}}

        s._fill_territory_context(context)

        self.assertIsNone(context["current_base"], "在基地外就是野外")
        self.assertTrue(context["home_status"]["away"])
        self.assertIn("home", [row["name"] for row in context["territory"]["bases"]])


if __name__ == "__main__":
    unittest.main()
