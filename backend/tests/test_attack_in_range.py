"""`attack_in_range`（站桩持续攻击）：注册 / 权限 / 契约 / 回执如实呈现。

用户口径（原话）：
  - "站在刷怪塔底部持续打怪" ⇒ 用 `attack_in_range`（站位交给 `go_afk`，它**不会移动**）；
  - 它会**持续出手**（等于把身体交出去打怪）⇒ 与 `go_afk` / `remember_station` 同档 **master-only**；
  - 模组侧对新任务 / `stop_all` 会自动退出 ⇒ 不需要额外的 stop 工具；
  - 结束时的 `attack_in_range_result` 回执（命中数 / 原因码）**必须如实呈现** ——
    "命令已 ack"不等于"打到了"，不许当成成功吞掉。

参数与语义逐字照 Java `ActionExecutor.handleAttackInRange`（radius 默认 8 钳 1..32、
kind ∈ hostile|passive|any 默认 hostile、max_seconds 默认 30 钳 1..300）。

全部离线：假 skills 记录下发 / 假 socket 数 wire 帧，不碰真实 `data/`。
"""

import json
import re
import unittest
from pathlib import Path
from types import SimpleNamespace

from agent import planner as planner_module
from agent.memory import WorldPoiMemory
from agent.permissions import TIER_GUEST, TIER_MASTER
from agent.planner import Planner
from agent.resource_lease import RESOURCE_ATTACK, resources_for_tool
from agent.session import Session
from agent.skills import Skills
from agent.tool_registry import TOOL_BY_NAME
from protocol.wire_client import WireClient

BACKEND = Path(__file__).resolve().parent.parent
AGENT_DIR = BACKEND / "agent"

RESULT = {"hits": 3, "reason": "no_target", "stopped": "max_seconds",
          "radius": 8.0, "kind": "hostile"}


def result_ctx(result=None, *, at=1234.0) -> dict:
    """L2 里那一份收尾回执的**存储形状**（与 session 存的 `{data, at}` 一致）。"""
    entry = {"data": dict(RESULT if result is None else result), "at": at}
    return {"attack_in_range_result": entry}


class DummyMemory:
    def build_context(self):
        return {}

    def get_recent_context(self, _count=5):
        return []


class RecordingSkills:
    def __init__(self):
        self.calls: list[tuple] = []

    def __getattr__(self, name):
        def _record(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            return {"success": True}

        return _record


class FakeSocket:
    def __init__(self):
        self.chunks: list[bytes] = []

    def sendall(self, data: bytes) -> None:
        self.chunks.append(data)

    def close(self):
        pass


def planner(*, skills=None) -> Planner:
    return Planner(llm_service=None, memory=DummyMemory(),
                   skills=skills if skills is not None else RecordingSkills())


def master_ctx() -> dict:
    return {"actor_tier": TIER_MASTER, "actor_name": "Owner"}


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


# ── ① 注册与契约 ────────────────────────────────────────────────


class AttackInRangeRegistryTests(unittest.TestCase):
    def test_the_tool_is_registered_with_the_java_contract(self):
        spec = TOOL_BY_NAME.get("attack_in_range")
        self.assertIsNotNone(spec)
        self.assertEqual(spec.args, "radius,kind,max_seconds", "参数名必须与 Java 逐字一致")
        self.assertIn("combat", spec.groups)
        self.assertTrue(callable(getattr(Planner, spec.handler, None)))
        # 攻击资源（它只出手、不移动）——不是 move。
        self.assertEqual(resources_for_tool("attack_in_range"), frozenset({RESOURCE_ATTACK}))

    def test_description_says_when_to_use_me(self):
        desc = TOOL_BY_NAME["attack_in_range"].description
        self.assertIn("刷怪塔底部", desc)
        self.assertIn("go_afk", desc, "站位是 go_afk 的事，说明里必须指路")
        self.assertIn("不会移动", desc)
        self.assertIn("主人专属", desc)
        self.assertIn("命中", desc, "回执口径（命中数）要写进说明")

    def test_defaults_and_clamps_match_java(self):
        self.assertEqual(planner_module.ATTACK_IN_RANGE_KINDS, ("hostile", "passive", "any"))
        self.assertEqual(planner_module.ATTACK_IN_RANGE_DEFAULT_RADIUS, 8.0)
        self.assertEqual(planner_module.ATTACK_IN_RANGE_MIN_RADIUS, 1.0)
        self.assertEqual(planner_module.ATTACK_IN_RANGE_MAX_RADIUS, 32.0)
        self.assertEqual(planner_module.ATTACK_IN_RANGE_DEFAULT_SECONDS, 30.0)
        self.assertEqual(planner_module.ATTACK_IN_RANGE_MAX_SECONDS, 300.0)

    def test_it_is_not_injected_outside_the_combat_phases(self):
        """与 `attack` 同一档：idle / travel 有，gather / craft / survive 没有（行为照旧）。"""
        from agent.tool_registry import _actions_for_phase

        for phase in ("idle", "travel"):
            self.assertIn("attack_in_range", [item[0] for item in _actions_for_phase(phase)])
        for phase in ("gather", "craft", "survive"):
            self.assertNotIn("attack_in_range", [item[0] for item in _actions_for_phase(phase)])


class AttackInRangeSkillsWireTests(unittest.TestCase):
    def test_the_frame_matches_the_java_arg_names(self):
        sock = FakeSocket()
        skills = Skills(WireClient("127.0.0.1", 25568))
        skills.wire.sock = sock

        skills.attack_in_range()

        raw = b"".join(sock.chunks).decode("utf-8")
        frame = [json.loads(line) for line in raw.splitlines()][-1]
        self.assertEqual(frame["cmd"], "attack_in_range")
        self.assertEqual(frame["args"],
                         {"radius": 8.0, "kind": "hostile", "max_seconds": 30.0})


# ── ② 派发：master 门 / 契约钳位 / 诚实拒绝 ────────────────────


class AttackInRangeDispatchTests(unittest.TestCase):
    def test_master_can_start_it_with_defaults(self):
        skills = RecordingSkills()
        p = planner(skills=skills)

        handled = p._dispatch_tool_call("attack_in_range", {}, master_ctx())

        self.assertTrue(handled)
        self.assertEqual(skills.calls, [("attack_in_range", (8.0, "hostile", 30.0), {})])
        self.assertIn("不移动", p._action_note)

    def test_values_are_clamped_like_the_mod(self):
        skills = RecordingSkills()
        p = planner(skills=skills)

        handled = p._dispatch_tool_call(
            "attack_in_range", {"radius": 999, "kind": "passive", "max_seconds": 0},
            master_ctx())

        self.assertTrue(handled)
        self.assertEqual(skills.calls, [("attack_in_range", (32.0, "passive", 1.0), {})])

    def test_guest_cannot_start_it(self):
        skills = RecordingSkills()
        p = planner(skills=skills)

        handled = p._dispatch_tool_call(
            "attack_in_range", {}, {"actor_tier": TIER_GUEST, "actor_name": "Bob"})

        self.assertFalse(handled)
        self.assertEqual(skills.calls, [], "没资格时一帧都不发")
        self.assertIn("master_only", p._action_note)
        self.assertIn("attack_in_range", p._action_note)

    def test_an_unknown_kind_is_refused_before_sending(self):
        skills = RecordingSkills()
        p = planner(skills=skills)

        handled = p._dispatch_tool_call("attack_in_range", {"kind": "cows"},
                                        master_ctx())

        self.assertFalse(handled)
        self.assertEqual(skills.calls, [], "注定被拒的命令不发")
        self.assertIn("unknown_kind", p._action_note)
        self.assertIn("Unknown kind: cows", p._action_note, "模组的原话要透出去")


# ── ③ 收尾回执：不许当成"成功"吞掉 ──────────────────────────────


class AttackInRangeResultTests(unittest.TestCase):
    def test_the_result_event_is_routed_and_kept(self):
        session = Session.__new__(Session)
        session._attack_result = None

        session.handle_event("attack_in_range_result", dict(RESULT))

        stored = session._attack_result
        self.assertEqual(stored["data"], RESULT, "事件的字段要原样留着（不加工、不丢）")
        self.assertIsInstance(stored["at"], float)

    def test_the_result_reaches_l2_with_hits_and_reason(self):
        line = planner_module._attack_result_line(result_ctx())

        self.assertIn("命中 3 次", line)
        self.assertIn("no_target", line)
        self.assertIn("半径8", line)
        self.assertIn("hostile", line)
        self.assertIn("max_seconds", line)

    def test_zero_hits_is_still_reported(self):
        """"发出去了"≠"打到了"：一次没打中也必须原样说出来。"""
        line = planner_module._attack_result_line(
            result_ctx(dict(RESULT, hits=0, reason="player_too_close")))

        self.assertIn("命中 0 次", line)
        self.assertIn("player_too_close", line)

    def test_no_result_means_no_section(self):
        self.assertEqual(planner_module._attack_result_line({}), "")
        self.assertEqual(planner_module._attack_result_line(
            {"attack_in_range_result": None}), "")
        self.assertEqual(planner_module._attack_result_line({"attack_in_range_result": {}}), "")

    def test_it_is_rendered_into_the_l2_prompt_only_when_there_is_a_result(self):
        p = planner()
        with_result = p._build_l2("Owner", "你好", result_ctx(), "LCU")
        without = p._build_l2("Owner", "你好", {}, "LCU")

        self.assertIn("站桩攻击", with_result)
        self.assertIn("命中 3 次", with_result)
        self.assertNotIn("站桩攻击", without)

    def test_the_dynamic_layer_never_leaks_into_system(self):
        p = planner()
        layers = p.build_prompt_layers("Owner", "你好", result_ctx(), "LCU")

        self.assertIn("命中 3 次", layers.user)
        self.assertNotIn("命中 3 次", layers.system)


class AttackInRangeNoAutonomyTests(unittest.TestCase):
    def test_only_the_planner_handler_starts_it(self):
        call_re = re.compile(r"skills\.attack_in_range\s*\(")
        callers = {path.name for path in AGENT_DIR.glob("*.py")
                   if call_re.search(path.read_text(encoding="utf-8"))}

        self.assertEqual(callers, {"planner.py"}, "只有 _dispatch_attack_in_range 能起它")
        modes_text = (AGENT_DIR / "modes_engine.py").read_text(encoding="utf-8")
        self.assertNotIn("attack_in_range", modes_text, "后台模式里不许有站桩攻击")


if __name__ == "__main__":
    unittest.main()
