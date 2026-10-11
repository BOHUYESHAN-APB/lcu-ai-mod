"""进食闸门 BackendEatYield（让路 / 退避 / 冷却）+ wire 帧级回归。

背景：`auto_eat` 这条**自主触发**已删除（能力改为注册表里的 `eat` 工具，由 LLM 决定用不用）；
但它的三条安全约束一字不改地保留在 `EatYield` 里，并由 `eat` 工具下发前执行（用户实机日志）：
  ① 退避：action_manager 的退避窗口内 → **零 wire 帧**；
  ② 让路：`reflex_eat` 为真（Java 本地反射正在按右键吃）→ 后端一个字都不发；
  ③ 冷却：`eat` 失败/超时 → 10s 冷却 + 真实原因写进 `task_state`。
"""

import json
import time
import unittest

from agent.action_manager import ActionManager
from agent.modes_engine import EatYield, ModesEngine, SelfDefenseMode, _GatedSkills
from agent.planner import Planner
from agent.skills import Skills
from protocol.wire_client import WireClient


class DummyMemory:
    def build_context(self):
        return {}

    def get_recent_context(self, _count):
        return []


class FakeSocket:
    """只实现 sendall 的假 socket：让真实的编码/换行逻辑照常跑，用于数帧。"""

    def __init__(self):
        self.chunks: list[bytes] = []

    def sendall(self, data: bytes) -> None:
        self.chunks.append(data)

    def close(self):
        pass


def _state(player=None, **top_level):
    state = {"player": player or {}, "entities": [], "nearby_blocks": []}
    state.update(top_level)
    return state


def _hostile(distance=3.0, name="僵尸", entity_id=42):
    return {"type": "hostile", "name": name, "entity_id": entity_id,
            "distance": distance, "x": 1.0, "y": 64.0, "z": 2.0}


class _WireHarness:
    """按 Session 的真实接线来：`Skills` 的命令观察者 → `ActionManager.track`。"""

    def __init__(self):
        self.wire = WireClient("127.0.0.1", 25568)
        self.sock = FakeSocket()
        self.wire.sock = self.sock
        self.skills = Skills(self.wire)
        self.manager = ActionManager()
        # 跟 Session.LONG_TASK_CMDS 一致：eat 的完成靠 task_state 终态，wire 回执只表示"已排上"。
        self.skills.set_command_observer(
            lambda cmd, req_id, context, args: self.manager.track(
                cmd, req_id, complete_on_response=(cmd != "eat")))
        self.engine = ModesEngine(self.skills, action_manager=self.manager)

    def add_mode(self, mode):
        mode.activate_streak_required = 1
        self.engine.add_mode(mode)
        return mode

    def planner(self):
        return Planner(llm_service=None, memory=DummyMemory(), skills=self.skills,
                       action_manager=self.manager)

    def frames(self):
        raw = b"".join(self.sock.chunks).decode("utf-8")
        return [json.loads(line) for line in raw.splitlines()]

    def sent_cmds(self):
        return [frame["cmd"] for frame in self.frames()]


# ── ① 退避闸门：退避期内不得产生 wire 帧 ─────────────────────────

class EatBackoffGateTests(unittest.TestCase):
    def test_backed_off_eat_produces_no_wire_frame(self):
        h = _WireHarness()
        h.manager._backoff["eat"] = (time.time() + 5.0, 1)
        planner = h.planner()

        handled = planner._dispatch_tool_call("eat", {}, {})

        self.assertFalse(handled)
        self.assertEqual(h.sent_cmds(), [], "退避期内一个 wire 帧都不许发出")
        self.assertIn("退避", planner._action_note)

    def test_backoff_window_expiry_allows_the_command_again(self):
        h = _WireHarness()
        h.manager._backoff["eat"] = (time.time() - 1.0, 1)   # 已过期
        planner = h.planner()

        handled = planner._dispatch_tool_call("eat", {}, {})

        self.assertTrue(handled)
        self.assertEqual(h.sent_cmds(), ["eat"])

    def test_gate_is_per_label(self):
        """只有被退避的那个 label 被拦，其余命令照常（别把整个引擎冻住）。"""
        h = _WireHarness()
        h.add_mode(SelfDefenseMode())
        h.manager._backoff["look_at"] = (time.time() + 5.0, 1)

        h.engine.tick(_state(entities=[_hostile()]))

        self.assertEqual(h.sent_cmds(), ["attack"], "look_at 被退避，attack 不该跟着陪葬")

    def test_drive_control_stream_is_never_gated(self):
        """P0 逃生走控制流：命比节奏重要，退避永远不许挡住 drive。"""
        skills = _RecordingSkills()
        gated = _GatedSkills(skills, ActionManager())
        gated.action_manager._backoff["drive"] = (time.time() + 5.0, 1)

        self.assertTrue(gated.drive(jump=True))
        self.assertEqual(skills.count("drive"), 1)


class _RecordingSkills:
    def __init__(self):
        self.calls: list[tuple] = []

    def drive(self, **kwargs):
        self.calls.append(("drive", tuple(sorted(kwargs.items()))))
        return True

    def count(self, name):
        return sum(1 for call in self.calls if call[0] == name)


# ── ② 本能反射让路：reflex_eat=true 时后端一个字节都不发 ──────────

class ReflexEatArbitrationTests(unittest.TestCase):
    def test_eat_tool_yields_to_the_local_reflex(self):
        h = _WireHarness()
        planner = h.planner()

        handled = planner._dispatch_tool_call("eat", {}, {"reflex_eat": True})

        self.assertFalse(handled)
        self.assertEqual(h.sent_cmds(), [], "本地反射在吃时后端不许抢")
        self.assertIn("不抢", planner._action_note)

    def test_eat_tool_dispatches_when_no_reflex(self):
        h = _WireHarness()
        planner = h.planner()

        handled = planner._dispatch_tool_call("eat", {}, {})

        self.assertTrue(handled)
        self.assertEqual(h.sent_cmds(), ["eat"])


# ── ③ eat 失败：10s 冷却 + 真实原因进 task_state ────────────────

class EatFailureCooldownTests(unittest.TestCase):
    def test_failure_starts_cooldown_and_records_the_reason(self):
        manager = ActionManager()
        guard = EatYield(manager)
        guard.mark_sent()
        manager.track("eat", "req_eat", complete_on_response=False)
        manager.complete_current(False, "eat failed: no food")

        state = {}
        guard.absorb(state)

        allowed, note = guard.allow(state)
        self.assertFalse(allowed)
        self.assertIn("冷却", note)
        self.assertEqual(state["task_state"]["kind"], "eat")
        self.assertEqual(state["task_state"]["status"], "failed")
        self.assertEqual(state["task_state"]["source"], "eat_yield")
        self.assertIn("no food", state["task_state"]["detail"])

    def test_timeout_without_any_outcome_also_cools_down(self):
        guard = EatYield()
        guard.mark_sent()
        guard._pending_at -= (EatYield.EAT_TIMEOUT + 1.0)
        state = {}

        guard.absorb(state)

        self.assertEqual(state["task_state"]["status"], "failed")
        self.assertIn("timeout", state["task_state"]["detail"])

    def test_success_clears_the_cooldown(self):
        manager = ActionManager()
        guard = EatYield(manager)
        guard.mark_sent()
        manager.track("eat", "req_eat", complete_on_response=False)
        manager.complete_current(True)

        guard.absorb({})

        allowed, _ = guard.allow({})
        self.assertTrue(allowed)


# ── ④ 自主触发已删除：AutoEatMode 不复存在，默认引擎只装生存反射 ──

class AutoEatRemovedTests(unittest.TestCase):
    def test_autonomous_modes_are_not_defined(self):
        """五条非生存的自主触发模式全部删除（能力改为注册表里的工具）。"""
        import agent.modes_engine as modes

        for gone in ("AutoEatMode", "ItemCollectMode", "TorchPlaceMode",
                     "HuntingMode", "FollowMasterMode"):
            self.assertFalse(hasattr(modes, gone), f"{gone} 应已删除")

    def test_eat_yield_replaces_auto_eat(self):
        import agent.modes_engine as modes

        self.assertTrue(hasattr(modes, "EatYield"))

    def test_default_engine_only_installs_survival_modes(self):
        engine = ModesEngine(_RecordingSkills())
        engine.add_defaults()

        names = [mode.name for mode in engine.modes]
        self.assertEqual(
            names,
            ["self_preservation", "self_heal", "cowardice", "self_defense"],
            "只留 P0-P3 生存反射；auto_eat/item_collect/torch_place/hunting/follow_master 全删")


if __name__ == "__main__":
    unittest.main()
