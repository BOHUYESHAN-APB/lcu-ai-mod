"""站点（挂机点）工具 + `drop_item` 硬底线：注册 / 权限 / 诚实拒绝 / L2 / 不自主调用。

用户口径（原话）：
  - "站到某个位置挂机，漂移了本能地移动回去" ⇒ 记站点（当帧位姿）+ `go_afk` 驻守；
  - "能力＝注册的工具；后台不许有自主开关/自动触发" ⇒ **只有模型显式调 go_afk 才动作**，
    没有任何一处后台/反射自己挂机（本文件有一条静态扫描钉住）；
  - 记站点 / 挂机是**主人专属**（一个写持久站点表、一个夺走身体），停止 `afk_stop` **不加门**
    （停下是安全方向，谁都能把它从挂机里拉出来）；
  - `drop_item` 有「宁死不许丢」硬底线（铁 / 钻石 / 下界合金 / 工具 / 盒类），且**先解析精确 id**
    再下发（模组侧是 `contains()` 子串匹配，给模糊词等于赌它挑中哪一件）。

全部离线：假 skills 记录下发 / 假 socket 数 wire 帧，不碰真实 `data/`。
"""

import copy
import json
import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from agent import planner as planner_module
from agent.memory import WorldPoiMemory
from agent.permissions import TIER_GUEST, TIER_MASTER
from agent.planner import Planner, is_protected_item, resolve_inventory_item
from agent.session import Session
from agent.skills import Skills
from agent.tool_registry import TOOL_BY_NAME, ToolPermission, _actions_for_phase
from protocol.wire_client import WireClient

BACKEND = Path(__file__).resolve().parent.parent
AGENT_DIR = BACKEND / "agent"
STATION_TOOLS = ("remember_station", "go_afk", "afk_stop")


# ── 假件 ────────────────────────────────────────────────────────


class DummyMemory:
    def build_context(self):
        return {}

    def get_recent_context(self, _count=5):
        return []


class RecordingSkills:
    """记录下发的技能调用（名字 + 位置参数 + 关键字参数）。"""

    def __init__(self):
        self.calls: list[tuple] = []

    def __getattr__(self, name):
        def _record(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            return {"success": True}

        return _record

    def names(self) -> list[str]:
        return [call[0] for call in self.calls]


class FakeSocket:
    def __init__(self):
        self.chunks: list[bytes] = []

    def sendall(self, data: bytes) -> None:
        self.chunks.append(data)

    def close(self):
        pass


def planner(*, skills=None, permissions=None, places=None) -> Planner:
    return Planner(llm_service=None, memory=DummyMemory(),
                   skills=skills if skills is not None else RecordingSkills(),
                   permissions=permissions, places=places)


def master_ctx(**extra) -> dict:
    ctx = {"actor_tier": TIER_MASTER, "actor_name": "Owner"}
    ctx.update(extra)
    return ctx


def station_row(name="铁塔", kind="iron_farm", x=10.0, y=64.0, z=-3.0, **over) -> dict:
    row = {"name": name, "kind": kind, "x": x, "y": y, "z": z,
           "yaw": 0.0, "pitch": 0.0, "radius": 1.0, "saved_at_tick": 120}
    row.update(over)
    return row


def make_session(root: Path) -> Session:
    """只搭 `_runtime_context` 这条链真正用到的东西（其余按生产缺省 / 缺就是没有）。"""
    session = Session.__new__(Session)
    session.runtime = {}
    session.in_world = True
    session.memory = DummyMemory()
    session.vision = SimpleNamespace(latest=lambda: "")
    session.poi = WorldPoiMemory(root=str(root / "poi"))
    session.people = None
    session.conversation = None
    session.borrow_rules = None
    session.territories = None
    session.last_speaker = {}
    session.chat_inbox = None
    session._present_person_facts = {}
    session._actor = {"name": "Owner", "tier": TIER_MASTER}
    session._home_away_since = None
    session._hud_note = lambda: ""
    session.persona_blocks = lambda *_a, **_k: {}
    session.llm_active = lambda: True
    return session


# ── ① 注册：三条工具的标准 ──────────────────────────────────────


class StationRegistryTests(unittest.TestCase):
    def test_the_three_station_tools_are_registered(self):
        for name in STATION_TOOLS:
            spec = TOOL_BY_NAME.get(name)
            self.assertIsNotNone(spec, f"{name} 必须登记在注册表里")
            self.assertIsInstance(spec.permission, ToolPermission)
            self.assertTrue(callable(getattr(Planner, spec.handler, None)),
                            f"{name} 的 handler {spec.handler} 不存在")

        self.assertEqual(TOOL_BY_NAME["remember_station"].args,
                         "name,kind,x,y,z,yaw,pitch,radius")
        self.assertEqual(TOOL_BY_NAME["go_afk"].args, "station,keepalive")
        self.assertEqual(TOOL_BY_NAME["afk_stop"].args, "")

    def test_station_tools_live_in_the_guard_group_and_reach_every_phase(self):
        """与 `guard` 同档（同一个动作组）⇒ 所有阶段都注入得到。"""
        for name in STATION_TOOLS:
            self.assertIn("guard", TOOL_BY_NAME[name].groups, name)
        for phase in ("idle", "travel", "gather", "craft", "survive"):
            names = [item[0] for item in _actions_for_phase(phase)]
            for name in STATION_TOOLS:
                self.assertIn(name, names, f"{phase} 阶段缺少 {name}")

    def test_descriptions_tell_when_to_use_me_and_who_may(self):
        """工具描述要写清"什么时候用我"，以及哪条是主人专属、哪条不加门。"""
        guard_desc = TOOL_BY_NAME["guard"].description
        self.assertIn("go_afk", guard_desc, "guard 要指路到 go_afk（半径尺度不同）")
        self.assertIn("精准站位", guard_desc)

        go_desc = TOOL_BY_NAME["go_afk"].description
        self.assertIn("精准站位", go_desc)
        self.assertIn("主人专属", go_desc)

        self.assertIn("主人专属", TOOL_BY_NAME["remember_station"].description)

        stop_desc = TOOL_BY_NAME["afk_stop"].description
        self.assertIn("不加门", stop_desc)
        self.assertIn("安全方向", stop_desc)

    def test_master_only_tools_do_not_reuse_unrelated_capability_tables(self):
        """两条主人专属门在 handler 里判（照 tpa_request 的先例），不借 craft/territory 的表。"""
        for name in ("remember_station", "go_afk"):
            self.assertEqual(TOOL_BY_NAME[name].permission.tier, "",
                             "别借能力表 —— 门由 handler 的 _actor_of 判")
        self.assertEqual(TOOL_BY_NAME["afk_stop"].permission.tier, "")

    def test_afk_stop_is_a_release_not_an_occupation(self):
        """停止驻守不占 move 资源（与 stop / guard_off 同一条口径）——别让「叫不停」成为可能。"""
        from agent.resource_lease import RESOURCE_MOVE, resources_for_tool

        self.assertEqual(resources_for_tool("afk_stop"), frozenset())
        self.assertEqual(resources_for_tool("go_afk"), frozenset({RESOURCE_MOVE}))


# ── ② skills 层：与 Java 契约逐字对齐 ───────────────────────────


class StationSkillsWireTests(unittest.TestCase):
    def _frames(self, skills: Skills, sock: FakeSocket) -> list[dict]:
        raw = b"".join(sock.chunks).decode("utf-8")
        return [json.loads(line) for line in raw.splitlines()]

    def test_go_afk_sends_name_and_keepalive(self):
        sock = FakeSocket()
        skills = Skills(WireClient("127.0.0.1", 25568))
        skills.wire.sock = sock

        skills.go_afk("铁塔", True)

        frame = self._frames(skills, sock)[-1]
        self.assertEqual(frame["cmd"], "go_afk")
        # Java `handleGoAfk` 读的是 `name`（不是 station）。
        self.assertEqual(frame["args"], {"name": "铁塔", "keepalive": True})

    def test_afk_stop_sends_an_empty_payload(self):
        sock = FakeSocket()
        skills = Skills(WireClient("127.0.0.1", 25568))
        skills.wire.sock = sock

        skills.afk_stop()

        frame = self._frames(skills, sock)[-1]
        self.assertEqual(frame["cmd"], "afk_stop")
        self.assertEqual(frame["args"], {})


# ── ③ 派发：master 门 + 诚实拒绝 ──────────────────────────────


class StationDispatchTests(unittest.TestCase):
    def test_master_can_remember_a_station_with_pose_fallback(self):
        skills = RecordingSkills()
        p = planner(skills=skills)

        handled = p._dispatch_tool_call(
            "remember_station", {"name": "铁塔", "kind": "iron_farm"}, master_ctx())

        self.assertTrue(handled)
        self.assertEqual(skills.names(), ["remember_station"])
        _name, args, kwargs = skills.calls[0]
        self.assertEqual(args, ("铁塔",))
        self.assertEqual(kwargs["kind"], "iron_farm")
        # 坐标/朝向缺省 ⇒ 传 None（模组侧回退当帧位姿），**不在这里编坐标**。
        for key in ("x", "y", "z", "yaw", "pitch", "radius"):
            self.assertIsNone(kwargs[key], key)

    def test_guest_cannot_remember_a_station(self):
        skills = RecordingSkills()
        p = planner(skills=skills)

        handled = p._dispatch_tool_call(
            "remember_station", {"name": "铁塔"}, {"actor_tier": TIER_GUEST})

        self.assertFalse(handled)
        self.assertEqual(skills.calls, [], "没资格时一帧都不发")
        self.assertIn("master_only", p._action_note)
        self.assertIn("remember_station", p._action_note)

    def test_guest_cannot_go_afk(self):
        skills = RecordingSkills()
        p = planner(skills=skills)
        ctx = {"actor_tier": TIER_GUEST, "stations": [station_row()]}

        handled = p._dispatch_tool_call("go_afk", {"station": "铁塔"}, ctx)

        self.assertFalse(handled)
        self.assertEqual(skills.calls, [])
        self.assertIn("master_only", p._action_note)

    def test_master_can_go_afk_to_a_known_station(self):
        skills = RecordingSkills()
        p = planner(skills=skills)

        handled = p._dispatch_tool_call(
            "go_afk", {"station": "铁塔", "keepalive": True},
            master_ctx(stations=[station_row()]))

        self.assertTrue(handled)
        self.assertEqual(skills.calls, [("go_afk", ("铁塔", True), {})])

    def test_unknown_station_is_refused_honestly(self):
        """站点不存在 ⇒ 一帧不发，且把模组会回的原话也透出来（不是静默 False）。"""
        skills = RecordingSkills()
        p = planner(skills=skills)

        handled = p._dispatch_tool_call(
            "go_afk", {"station": "没有这个塔"}, master_ctx(stations=[station_row()]))

        self.assertFalse(handled)
        self.assertEqual(skills.calls, [], "不存在的站点一帧都不发")
        self.assertIn("unknown_station", p._action_note)
        self.assertIn("Unknown station: 没有这个塔", p._action_note,
                      "模组的原话要透出去，不许吞")
        self.assertIn("没动", p._action_note)

    def test_go_afk_without_a_station_name_is_refused(self):
        skills = RecordingSkills()
        p = planner(skills=skills)

        handled = p._dispatch_tool_call("go_afk", {}, master_ctx(stations=[station_row()]))

        self.assertFalse(handled)
        self.assertEqual(skills.calls, [])
        self.assertIn("need_station", p._action_note)

    def test_afk_stop_has_no_gate_at_all(self):
        """停下是安全方向：guest 也能把 AI 从挂机里拉出来。"""
        for tier in (TIER_MASTER, "friend", TIER_GUEST):
            skills = RecordingSkills()
            p = planner(skills=skills)

            handled = p._dispatch_tool_call("afk_stop", {}, {"actor_tier": tier})

            self.assertTrue(handled, tier)
            self.assertEqual(skills.calls, [("afk_stop", (), {})], tier)

    def test_station_lookup_falls_back_to_the_world_memory_store(self):
        """context 里没有当帧 stations 时，退回后端持久化的那份（回灌的来源）。"""
        class Store:
            @staticmethod
            def stations():
                return [station_row(name="矿场")]

        skills = RecordingSkills()
        p = planner(skills=skills, places=Store())

        handled = p._dispatch_tool_call("go_afk", {"station": "矿场"}, master_ctx())

        self.assertTrue(handled)
        self.assertEqual(skills.names(), ["go_afk"])


# ── ④ 没有任何地方自主调用 go_afk（静态扫描钉住）───────────────


class NoAutonomousStationTests(unittest.TestCase):
    def test_only_the_planner_handler_calls_go_afk(self):
        call_re = re.compile(r"skills\.go_afk\s*\(")
        callers = {path.name for path in AGENT_DIR.glob("*.py")
                   if call_re.search(path.read_text(encoding="utf-8"))}

        self.assertEqual(callers, {"planner.py"},
                         "只有 planner 的 _dispatch_go_afk 能调 go_afk（禁止自主挂机）")
        planner_text = (AGENT_DIR / "planner.py").read_text(encoding="utf-8")
        self.assertEqual(planner_text.count("skills.go_afk("), 1)

    def test_the_modes_engine_knows_nothing_about_afk(self):
        """后台模式（含反射）里不许出现 go_afk / afk —— 挂机只能由模型显式调。"""
        modes_text = (AGENT_DIR / "modes_engine.py").read_text(encoding="utf-8")
        self.assertNotIn("go_afk", modes_text)
        self.assertNotIn("afk_stop", modes_text)

    def test_the_default_modes_never_include_a_station_mode(self):
        """默认装上的后台模式只有生存反射 P0-P3，**没有**任何挂机/站点模式。"""
        from agent.modes_engine import ModesEngine

        class _Skills:
            def __getattr__(self, _name):
                return lambda *a, **k: {"success": True}

        engine = ModesEngine(_Skills())
        engine.add_defaults()

        names = {mode.name for mode in engine.modes}
        self.assertEqual(names,
                         {"self_preservation", "self_heal", "cowardice", "self_defense"})
        self.assertFalse(any("afk" in name or "station" in name for name in names))


# ── ⑤ L2：模型必须知道自己在哪个塔、有没有在驻守 ────────────────


class StationL2Tests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def _l2(self, runtime: dict) -> tuple[str, dict]:
        session = make_session(self.root)
        session.runtime.update(runtime)
        context = session._runtime_context()
        p = planner()
        layers = p.build_prompt_layers("Owner", "你好", context, "LCU")
        return layers, context

    def test_stations_reach_l2_with_name_kind_and_coords(self):
        layers, context = self._l2({"stations": [station_row()]})

        self.assertEqual(context["stations"], [station_row()])
        self.assertIn("铁塔", layers.user)
        self.assertIn("iron_farm", layers.user)
        self.assertIn("(10,64,-3)", layers.user)
        self.assertIn("共 1 个", layers.user)

    def test_afk_state_reaches_l2(self):
        layers, _context = self._l2({
            "stations": [station_row()],
            "afk": {"active": True, "station": "铁塔", "at_target": False, "drift": 2.5,
                    "returning": True, "keepalive": False, "since_ticks": 300},
        })

        self.assertIn("挂机状态", layers.user)
        self.assertIn("正在站点「铁塔」驻守", layers.user)
        self.assertIn("漂移=2.5m", layers.user)
        self.assertIn("在容差内=否", layers.user)
        self.assertIn("走回中=是", layers.user)
        self.assertIn("since_ticks=300", layers.user)

    def test_afk_failure_reason_comes_from_the_task_state(self):
        layers, _context = self._l2({
            "afk": {"active": False, "station": "铁塔", "drift": 0.0},
            "task_state": {"kind": "afk", "status": "failed",
                           "detail": "returning failed after 3 attempts"},
        })

        self.assertIn("没在挂机", layers.user)
        self.assertIn("failed", layers.user)
        self.assertIn("returning failed after 3 attempts", layers.user)

    def test_no_afk_key_means_no_afk_section(self):
        layers, _context = self._l2({"stations": [station_row()]})

        self.assertNotIn("挂机状态", layers.user)

    def test_many_stations_are_capped_at_three_plus_a_count(self):
        rows = [station_row(name=f"塔{i}", saved_at_tick=i) for i in range(5)]

        layers, _context = self._l2({"stations": rows})

        self.assertIn("共 5 个", layers.user)
        self.assertIn("最近记的 3 个", layers.user)
        self.assertIn("塔4", layers.user)          # 最近记的
        self.assertNotIn("塔0", layers.user)       # 最老的被计数收掉
        self.assertIn("where_is", layers.user)     # 全量的出口指给模型

    def test_l1_is_untouched_by_station_state(self):
        """站点/挂机是**动态**的，只许进 L2 —— L1 一个字节都不能变。"""
        layers, _context = self._l2({"stations": [station_row()],
                                     "afk": {"active": True, "station": "铁塔"}})

        self.assertIn("铁塔", layers.user)
        self.assertNotIn("铁塔", layers.system)
        self.assertNotIn("挂机状态", layers.system)

        other, _ = self._l2({"stations": [station_row(name="别的塔", x=99.0)]})
        self.assertEqual(layers.system, other.system, "换一批站点 L1 必须逐字节不变")


# ── ⑥ drop_item 的「宁死不许丢」硬底线 + 精确 id 解析 ──────────


class ProtectedItemUnitTests(unittest.TestCase):
    def test_the_blocklist_is_a_negative_list(self):
        protected = [
            "iron_ingot", "raw_iron", "iron_ore", "deepslate_iron_ore", "iron_block",
            "diamond", "diamond_block",
            "netherite_ingot", "netherite_scrap", "netherite_block", "netherite_sword",
            "wooden_pickaxe", "stone_axe", "iron_shovel", "golden_hoe", "diamond_sword",
            "shears", "flint_and_steel", "elytra", "shulker_box", "red_shulker_box",
            "minecraft:iron_ingot", "minecraft:netherite_chestplate",
        ]
        for item_id in protected:
            self.assertTrue(is_protected_item(item_id), item_id)

    def test_ordinary_stuff_is_not_on_the_list(self):
        """只列负数清单：普通物品一概不管（什么该丢是模型的正数判断）。"""
        for item_id in ("dirt", "cobblestone", "wheat", "bone_meal", "oak_log",
                        "coal", "string", "gunpowder", "minecraft:gravel"):
            self.assertFalse(is_protected_item(item_id), item_id)

    def test_ambiguous_substring_is_not_resolved(self):
        inventory = [{"name": "minecraft:raw_iron", "count": 3},
                     {"name": "minecraft:iron_ingot", "count": 5}]

        item_id, candidates = resolve_inventory_item("iron", inventory)

        self.assertEqual(item_id, "")
        self.assertEqual(candidates, ["iron_ingot", "raw_iron"])

    def test_exact_and_unique_substring_resolve_to_the_snapshot_id(self):
        inventory = [{"name": "minecraft:oak_planks", "count": 12},
                     {"name": "minecraft:iron_ingot", "count": 5}]

        self.assertEqual(resolve_inventory_item("oak_planks", inventory),
                         ("minecraft:oak_planks", []))
        self.assertEqual(resolve_inventory_item("planks", inventory),
                         ("minecraft:oak_planks", []))
        self.assertEqual(resolve_inventory_item("iron_ingot", inventory),
                         ("minecraft:iron_ingot", []))


class DropItemGuardTests(unittest.TestCase):
    def _inventory(self) -> list[dict]:
        return [{"name": "minecraft:iron_ingot", "count": 5},
                {"name": "minecraft:raw_iron", "count": 3},
                {"name": "minecraft:oak_planks", "count": 12}]

    def test_a_protected_item_is_refused_and_nothing_is_dropped(self):
        skills = RecordingSkills()
        p = planner(skills=skills)
        inventory = self._inventory()
        before = copy.deepcopy(inventory)

        handled = p._dispatch_tool_call("drop_item", {"item": "iron_ingot", "count": 5},
                                        {"inventory": inventory})

        self.assertFalse(handled)
        self.assertEqual(skills.calls, [], "硬底线命中 ⇒ 一帧都不发")
        self.assertIn("protected_item", p._action_note)
        self.assertIn("宁死不许丢", p._action_note)
        self.assertEqual(inventory, before, "背包快照一个数都不许变")

    def test_an_ambiguous_name_is_refused_with_candidates(self):
        skills = RecordingSkills()
        p = planner(skills=skills)

        handled = p._dispatch_tool_call("drop_item", {"item": "iron"},
                                        {"inventory": self._inventory()})

        self.assertFalse(handled)
        self.assertEqual(skills.calls, [], "模糊词不解析出唯一一件就不许发")
        self.assertIn("ambiguous_item", p._action_note)
        self.assertIn("iron_ingot", p._action_note)
        self.assertIn("raw_iron", p._action_note)

    def test_an_item_that_is_not_in_the_inventory_is_refused(self):
        skills = RecordingSkills()
        p = planner(skills=skills)

        handled = p._dispatch_tool_call("drop_item", {"item": "dirt"},
                                        {"inventory": self._inventory()})

        self.assertFalse(handled)
        self.assertEqual(skills.calls, [])
        self.assertIn("not_in_inventory", p._action_note)

    def test_an_exact_id_reaches_the_mod_verbatim(self):
        skills = RecordingSkills()
        p = planner(skills=skills)

        handled = p._dispatch_tool_call("drop_item", {"item": "planks", "count": 2},
                                        {"inventory": self._inventory()})

        self.assertTrue(handled)
        self.assertEqual(skills.calls,
                         [("drop_item", ("minecraft:oak_planks", 2), {})],
                         "透传的必须是快照里的精确 id（不是模型给的模糊词）")

    def test_a_netherite_pickaxe_is_protected_by_prefix_and_suffix(self):
        skills = RecordingSkills()
        p = planner(skills=skills)

        handled = p._dispatch_tool_call(
            "drop_item", {"item": "netherite_pickaxe"},
            {"inventory": [{"name": "minecraft:netherite_pickaxe", "count": 1}]})

        self.assertFalse(handled)
        self.assertIn("protected_item", p._action_note)


# ── ⑦ 渲染件本身（纯函数）──────────────────────────────────────


class StationRenderHelpersTests(unittest.TestCase):
    def test_station_line_skips_missing_facts(self):
        line = planner_module._station_line({"name": "塔", "kind": "crop", "x": 1, "z": 2})
        self.assertIn("塔", line)
        self.assertIn("（crop）", line)
        self.assertNotIn("@(", line, "缺一个轴就不写坐标串（不编）")

    def test_afk_line_is_empty_when_the_mod_reports_nothing(self):
        self.assertEqual(planner_module._afk_line({}), "")
        self.assertEqual(planner_module._afk_line({"afk": {}}), "")


if __name__ == "__main__":
    unittest.main()
