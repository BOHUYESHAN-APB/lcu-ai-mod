"""AutoEatMode 的退避闸门 / 本能反射让路 / eat 失败冷却：用户实机日志的回归测试。

日志原文：
    [modes_engine] [Mode] auto_eat: ate minecraft:bread (hunger=15, hurt saturation-first)  ← 每 2 秒重复
    [action_manager] [ActionMgr] select_hotbar 正在退避（2.0s 后解禁），本次拒绝      ← 说了拒绝
    [skills] [Skills] Sent: select_hotbar (req=req_11)                                ← 却照样发出去了 ✗

三组用例：
  ① 模式发指令前必须过 action_manager 的退避闸门 → 退避期内**零 wire 帧**（且留 debug 日志）；
  ② `state.reflex_eat == True` 时 AutoEatMode 不激活/不发任何指令（Java 本地反射在按右键吃）；
  ③ 调用 Java `eat` 失败/超时 → 本次任务结束 + 10s 内不重试 + 原因如实写进 task_state。

本文件独立自足（自带假 socket / 假 skills），不改动别的测试文件。
"""

import json
import time
import unittest

from agent.action_manager import ActionManager
from agent.modes_engine import (AutoEatMode, ModesEngine, SelfDefenseMode,
                                _GatedSkills)
from agent.skills import Skills
from protocol.wire_client import WireClient


class FakeSocket:
    """只实现 sendall 的假 socket：让真实的编码/换行逻辑照常跑，用于数帧。"""

    def __init__(self):
        self.chunks: list[bytes] = []

    def sendall(self, data: bytes) -> None:
        self.chunks.append(data)

    def close(self) -> None:
        pass


class RecordingSkills:
    """记录下发的假 skills（控制流/命令都记），供纯模式用例使用。"""

    def __init__(self, eat_result=None):
        self.calls: list[tuple] = []
        self._eat_result = eat_result if eat_result is not None else {
            "success": True, "message": "eat sent", "req_id": "req_eat"}

    def look_at(self, x, y, z):
        self.calls.append(("look_at", x, y, z))

    def attack(self):
        self.calls.append(("attack",))

    def drive(self, **kwargs):
        self.calls.append(("drive", tuple(sorted(kwargs.items()))))
        return True

    def eat(self):
        self.calls.append(("eat",))
        return self._eat_result

    def count(self, name):
        return sum(1 for call in self.calls if call[0] == name)


def _state(player=None, **top_level):
    state = {"player": player or {}, "entities": [], "nearby_blocks": []}
    state.update(top_level)
    return state


def _hungry_state(**top_level):
    return _state({"health": 20, "max_health": 20, "hunger": 10},
                  inventory=[{"slot": 1, "name": "minecraft:cooked_beef"}],
                  **top_level)


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

    def frames(self):
        raw = b"".join(self.sock.chunks).decode("utf-8")
        return [json.loads(line) for line in raw.splitlines()]

    def sent_cmds(self):
        return [frame["cmd"] for frame in self.frames()]


# ── ① 退避闸门：退避期内不得产生 wire 帧 ─────────────────────────

class ModeBackoffGateTests(unittest.TestCase):
    def test_backed_off_eat_produces_no_wire_frame(self):
        h = _WireHarness()
        h.add_mode(AutoEatMode())
        h.manager._backoff["eat"] = (time.time() + 5.0, 1)

        with self.assertLogs("modes_engine", level="DEBUG") as logs:
            for _ in range(3):
                h.engine.tick(_hungry_state())

        self.assertEqual(h.sent_cmds(), [], "退避期内一个 wire 帧都不许发出")
        self.assertTrue(any("退避" in line for line in logs.output),
                        "被退避拦下时要留一条 debug 日志说明原因")

    def test_gate_is_per_label(self):
        """只有被退避的那个 label 被拦，其余命令照常（别把整个模式冻住）。"""
        h = _WireHarness()
        h.add_mode(SelfDefenseMode())
        h.manager._backoff["look_at"] = (time.time() + 5.0, 1)

        h.engine.tick(_state(entities=[_hostile()]))

        self.assertEqual(h.sent_cmds(), ["attack"], "look_at 被退避，attack 不该跟着陪葬")

    def test_backoff_window_expiry_allows_the_command_again(self):
        h = _WireHarness()
        h.add_mode(AutoEatMode())
        h.manager._backoff["eat"] = (time.time() - 1.0, 1)   # 已过期

        h.engine.tick(_hungry_state())

        self.assertEqual(h.sent_cmds(), ["eat"])

    def test_drive_control_stream_is_never_gated(self):
        """P0 逃生走控制流：命比节奏重要，退避永远不许挡住 drive。"""
        skills = RecordingSkills()
        gated = _GatedSkills(skills, ActionManager())
        gated.action_manager._backoff["drive"] = (time.time() + 5.0, 1)

        self.assertTrue(gated.drive(jump=True))
        self.assertEqual(skills.count("drive"), 1)


# ── ② 本能反射让路：reflex_eat=true 时 auto_eat 一个字节都不发 ───

class ReflexEatArbitrationTests(unittest.TestCase):
    def test_should_activate_is_false_while_local_eat_reflex_is_engaged(self):
        mode = AutoEatMode()

        self.assertFalse(mode.should_activate(_hungry_state(reflex_eat=True)))
        self.assertTrue(mode.should_activate(_hungry_state()))

    def test_engine_does_not_activate_auto_eat_during_reflex(self):
        skills = RecordingSkills()
        engine = ModesEngine(skills)
        mode = AutoEatMode()
        mode.activate_streak_required = 1
        engine.add_mode(mode)

        engine.tick(_hungry_state(reflex_eat=True))

        self.assertIsNone(engine.get_status()["active"])
        self.assertEqual(skills.calls, [], "本地反射在吃，后端一个字节都不该发")

    def test_active_auto_eat_stops_dispatching_when_reflex_kicks_in(self):
        skills = RecordingSkills()
        engine = ModesEngine(skills)
        mode = AutoEatMode()
        mode.activate_streak_required = 1
        engine.add_mode(mode)

        engine.tick(_hungry_state())
        self.assertEqual(skills.count("eat"), 1)

        mode._last_dispatch_at = 0.0
        for _ in range(3):
            engine.tick(_hungry_state(reflex_eat=True))

        self.assertEqual(skills.count("eat"), 1, "反射接手后不许再发 eat")


# ── ③ eat 失败：本次任务结束 + 10s 冷却 + 真实原因进 task_state ──

class AutoEatFailureCooldownTests(unittest.TestCase):
    def test_failure_ends_the_task_and_cools_down(self):
        h = _WireHarness()
        mode = h.add_mode(AutoEatMode())
        state = _hungry_state()

        h.engine.tick(state)
        self.assertEqual(h.sent_cmds(), ["eat"])
        self.assertEqual(h.manager.current_label, "eat")

        # Java 侧回了个真实的失败原因（例如"热栏里没食物"）。
        h.manager.complete_current(False, "No food in hotbar")

        action = h.engine.tick(state)
        self.assertIn("failed", action)
        self.assertIn("No food in hotbar", action)
        self.assertGreater(mode._fail_until, time.time() + 9.0, "失败后要进 10s 冷却")

        # 冷却是"真的不重试"：再 tick 若干次都不该有新的 wire 帧。
        for _ in range(5):
            h.engine.tick(state)
        self.assertEqual(h.sent_cmds(), ["eat"], "冷却期内不许每 2 秒重试")

        # 失败原因如实写进 task_state。
        self.assertEqual(state["task_state"]["status"], "failed")
        self.assertEqual(state["task_state"]["detail"], "No food in hotbar")
        self.assertEqual(state["task_state"]["source"], "auto_eat")

    def test_timeout_without_receipt_also_cools_down(self):
        h = _WireHarness()
        mode = h.add_mode(AutoEatMode())
        state = _hungry_state()

        h.engine.tick(state)
        self.assertEqual(h.sent_cmds(), ["eat"])

        mode._pending_at = time.time() - (AutoEatMode.EAT_TIMEOUT + 1.0)  # 假装等了太久
        action = h.engine.tick(state)

        self.assertIn("timeout", action)
        self.assertIn("timeout", state["task_state"]["detail"])
        for _ in range(3):
            h.engine.tick(state)
        self.assertEqual(h.sent_cmds(), ["eat"])

    def test_successful_eat_does_not_cool_down(self):
        h = _WireHarness()
        mode = h.add_mode(AutoEatMode())
        state = _hungry_state()

        h.engine.tick(state)
        h.manager.complete_current(True, "done")
        self.assertEqual(mode._fail_until, 0.0)

        mode._last_dispatch_at = 0.0     # 等价于"重发窗口已过"
        h.engine.tick(state)

        self.assertEqual(h.sent_cmds(), ["eat", "eat"], "成功后照常按节奏再吃")


if __name__ == "__main__":
    unittest.main()
