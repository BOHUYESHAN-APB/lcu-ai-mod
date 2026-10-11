"""模式抖动 / 动作循环：用户实机日志里的 three-way bug 的回归测试。

日志原文（每 ~2 秒一轮，一直到 #120）：
    self_defense: attacking 僵尸
    [Mode] Deactivated: self_defense        <-- 同一秒就关了
    [Mode] Activated: self_defense
    ...
    [ActionMgr] Loop warning #116: attack ran 3 times

三个根因各有一组用例：
  ① cooldown 被当成"继续运行"的门 → 模式一触发就把自己关掉；
  ② `state` 按阈值增量上报会闪 → 需要滞回（激活连续 N tick / 失活宽限 M tick）；
  ③ 每次激活都把同一目标重发一遍 → 需要防重复 + action_manager 侧退避/上报。
外加拿齐两条仲裁：`reflex_combat` / `reflex_swim` 为真时后端必须让路。

本文件独立自足（自带 skills 假件），不改动别的测试文件。
"""

import time
import unittest

from agent.action_manager import ActionManager
from agent.modes_engine import Mode, ModesEngine, SelfDefenseMode, SelfPreservationMode


class DummySkills:
    """记录所有下发（move_to/attack/look_at/drive/...）。"""

    def __init__(self):
        self.calls: list[tuple] = []

    def look_at(self, x, y, z):
        self.calls.append(("look_at", x, y, z))

    def attack(self):
        self.calls.append(("attack",))

    def move_to(self, x, y, z, speed=1.0):
        self.calls.append(("move_to", x, y, z))

    def drive(self, **kwargs):
        self.calls.append(("drive", tuple(sorted(kwargs.items()))))
        return True

    def release_control(self):
        self.calls.append(("release_control",))

    def select_hotbar(self, index):
        self.calls.append(("select_hotbar", index))

    def use_item(self):
        self.calls.append(("use_item",))

    def count(self, name):
        return sum(1 for call in self.calls if call[0] == name)


class FakeMode(Mode):
    """测试用模式：激活条件、动作、cooldown 都可控。"""

    def __init__(self, name="fake", priority=3, cooldown=2.0, activate=True,
                 action="fake: act"):
        super().__init__(name, priority, interrupts=True, cooldown=cooldown)
        self.wants_activation = activate
        self.ticks = 0
        self.action = action

    def should_activate(self, state: dict) -> bool:
        return self.wants_activation

    def tick(self, skills, state: dict):
        self.ticks += 1
        return self.action


def _state(player=None, **top_level):
    state = {"player": player or {}, "entities": [], "nearby_blocks": []}
    state.update(top_level)
    return state


def _hostile(distance=3.0, name="僵尸", entity_id=42):
    return {"type": "hostile", "name": name, "entity_id": entity_id,
            "distance": distance, "x": 1.0, "y": 64.0, "z": 2.0}


# ── ① cooldown 不该杀死正在运行的模式 ────────────────────────────

class SustainedActionSpamTests(unittest.TestCase):
    """cooldown 修好之后，模式会真的"持续活跃"——所以每个"动作型"模式都必须限速。

    历史 bug 的镜像：以前模式一触发就被 cooldown 关掉，恰好把"每 tick 重发"盖住了；
    现在必须由 `should_dispatch` 明确限速，否则 heal/eat/flee 会每 tick 发一次。
    """

    def _engine(self, mode):
        skills = DummySkills()
        engine = ModesEngine(skills)
        mode.activate_streak_required = 1
        engine.add_mode(mode)
        return engine, skills

    def test_self_heal_does_not_spam_use_item(self):
        from agent.modes_engine import SelfHealMode

        engine, skills = self._engine(SelfHealMode())
        state = _state({"health": 3, "max_health": 20})

        for _ in range(5):
            engine.tick(state)

        self.assertEqual(skills.count("use_item"), 1)
        self.assertEqual(engine.get_status()["active"], "self_heal")

    def test_cowardice_does_not_spam_move_to(self):
        from agent.modes_engine import CowardiceMode

        engine, skills = self._engine(CowardiceMode())
        state = _state({"health": 5, "x": 0, "y": 64, "z": 0}, entities=[_hostile()])

        for _ in range(5):
            engine.tick(state)

        self.assertEqual(skills.count("move_to"), 1)

    def test_auto_eat_does_not_spam_use_item(self):
        from agent.modes_engine import AutoEatMode

        engine, skills = self._engine(AutoEatMode())
        state = _state({"health": 20, "max_health": 20, "hunger": 10},
                       inventory=[{"slot": 1, "name": "minecraft:cooked_beef"}])

        for _ in range(5):
            engine.tick(state)

        self.assertEqual(skills.count("select_hotbar"), 1)
        self.assertEqual(skills.count("use_item"), 1)


class CooldownGateTests(unittest.TestCase):
    def test_running_mode_survives_its_own_cooldown(self):
        skills = DummySkills()
        engine = ModesEngine(skills)
        mode = FakeMode(cooldown=2.0)
        mode.activate_streak_required = 1          # 隔离变量：只看 cooldown
        engine.add_mode(mode)

        engine.tick(_state())
        self.assertEqual(engine.get_status()["active"], "fake")

        # 关键：刚触发即进入 cooldown；下一 tick 仍必须继续跑（历史 bug 在这里关掉它）
        engine.tick(_state())
        self.assertEqual(engine.get_status()["active"], "fake")
        self.assertTrue(mode.is_on_cooldown, "cooldown 仍应生效（只是不用于掐运行中的模式）")
        self.assertEqual(mode.ticks, 2)

    def test_cooldown_still_gates_reactivation(self):
        """cooldown 语义保留在"重新激活"上：模式被关掉后冷却期内不许马上重开。"""
        skills = DummySkills()
        engine = ModesEngine(skills)
        mode = FakeMode(cooldown=30.0)
        mode.activate_streak_required = 1
        mode.deactivate_grace_ticks = 1            # 条件一消失立刻关（便于测试）
        engine.add_mode(mode)

        engine.tick(_state())
        mode.wants_activation = False
        engine.tick(_state())
        self.assertIsNone(engine.get_status()["active"])

        mode.wants_activation = True
        engine.tick(_state())

        self.assertIsNone(engine.get_status()["active"], "冷却期内不该重新激活")
        self.assertEqual(mode.ticks, 1)


# ── ② 滞回：闪断不失活 / 单帧不激活 ─────────────────────────────

class HysteresisTests(unittest.TestCase):
    def _engine_with(self, **mode_kwargs):
        skills = DummySkills()
        engine = ModesEngine(skills)
        mode = FakeMode(**mode_kwargs)
        engine.add_mode(mode)
        return engine, mode, skills

    def test_single_tick_blip_does_not_activate(self):
        engine, mode, _ = self._engine_with()
        self.assertEqual(mode.activate_streak_required, 3)

        engine.tick(_state())                      # 满足 1 个 tick
        mode.wants_activation = False
        engine.tick(_state())                      # 闪断

        self.assertIsNone(engine.get_status()["active"])

    def test_activation_needs_three_consecutive_ticks(self):
        engine, mode, _ = self._engine_with()

        engine.tick(_state())
        engine.tick(_state())
        self.assertIsNone(engine.get_status()["active"], "两个 tick 还不够")

        engine.tick(_state())
        self.assertEqual(engine.get_status()["active"], "fake")

    def test_brief_flicker_does_not_deactivate(self):
        engine, mode, _ = self._engine_with()
        mode.activate_streak_required = 1
        engine.tick(_state())
        self.assertEqual(engine.get_status()["active"], "fake")

        # state 闪断 5 个 tick（< 20 的宽限）→ 必须还活着
        mode.wants_activation = False
        for _ in range(5):
            engine.tick(_state())
        self.assertEqual(engine.get_status()["active"], "fake", "宽限期内不该放手")

        # 持续不满足超过宽限 → 才真的失活
        for _ in range(mode.deactivate_grace_ticks):
            engine.tick(_state())
        self.assertIsNone(engine.get_status()["active"])

    def test_stale_streak_credit_does_not_auto_activate_lower_mode(self):
        """高优先级模式占着引擎时，低优先级模式不该攒"信用"（否则一放手就幽灵激活）。"""
        skills = DummySkills()
        engine = ModesEngine(skills)
        high = FakeMode(name="high", priority=1, cooldown=0.0, activate=False)
        low = FakeMode(name="low", priority=5, cooldown=0.0)
        high.activate_streak_required = 1
        low.activate_streak_required = 3
        engine.add_mode(high)
        engine.add_mode(low)

        # 两个 tick：没人激活，低优先级攒到 2/3 的"信用"
        engine.tick(_state())
        engine.tick(_state())
        self.assertIsNone(engine.get_status()["active"])

        # 高优先级插进来并占住引擎若干个 tick
        high.wants_activation = True
        high.deactivate_grace_ticks = 1
        engine.tick(_state())
        self.assertEqual(engine.get_status()["active"], "high")
        for _ in range(5):
            engine.tick(_state())

        # 高优先级放手：低优先级不许凭陈年信用立刻顶上，要从 1/3 重新数
        high.wants_activation = False
        engine.tick(_state())
        self.assertIsNone(engine.get_status()["active"], "陈旧信用不该让它立刻顶上")

        engine.tick(_state())
        self.assertIsNone(engine.get_status()["active"])
        engine.tick(_state())
        self.assertEqual(engine.get_status()["active"], "low")


# ── ③ 同一目标的动作不重复下发 ──────────────────────────────────

class RepeatDispatchTests(unittest.TestCase):
    def _engine_with_defense(self, **top_level):
        skills = DummySkills()
        engine = ModesEngine(skills)
        mode = SelfDefenseMode()
        mode.activate_streak_required = 1
        engine.add_mode(mode)
        return engine, mode, skills

    def test_same_hostile_is_not_attacked_every_tick(self):
        engine, mode, skills = self._engine_with_defense()
        hostile = _hostile()

        for _ in range(5):
            engine.tick(_state(entities=[hostile]))

        self.assertEqual(skills.count("attack"), 1, "同一目标不该每 tick 重发")
        self.assertEqual(skills.count("look_at"), 1)
        self.assertEqual(engine.get_status()["active"], "self_defense", "模式应保持激活")

    def test_same_hostile_is_attacked_again_after_the_interval(self):
        engine, mode, skills = self._engine_with_defense()
        hostile = _hostile()

        engine.tick(_state(entities=[hostile]))
        mode._last_dispatch_at = 0.0               # 把上次下发时间推远（等价于"过了一秒"）
        engine.tick(_state(entities=[hostile]))

        self.assertEqual(skills.count("attack"), 2)

    def test_a_different_hostile_is_attacked_immediately(self):
        engine, mode, skills = self._engine_with_defense()

        engine.tick(_state(entities=[_hostile()]))
        engine.tick(_state(entities=[_hostile(name="骷髅", entity_id=7)]))

        self.assertEqual(skills.count("attack"), 2)

    def test_should_dispatch_window_semantics(self):
        mode = FakeMode()

        self.assertTrue(mode.should_dispatch("zombie:1"))
        self.assertFalse(mode.should_dispatch("zombie:1"), "窗口内同目标 → 不发")
        self.assertTrue(mode.should_dispatch("zombie:2"), "换了目标 → 发")
        self.assertTrue(mode.should_dispatch("zombie:2", interval=0.0), "interval=0 表示不设限")

    def test_continuous_mode_opts_out_of_dedupe(self):
        """P0 逃生是"按住"，必须每 tick 重申 —— 不能被防重复吃掉。"""
        mode = SelfPreservationMode()
        self.assertEqual(mode.redispatch_interval, 0.0)


# ── ④ 本地反射优先：reflex_combat / reflex_swim ─────────────────

class ReflexArbitrationTests(unittest.TestCase):
    def test_self_defense_yields_when_local_combat_reflex_is_engaged(self):
        skills = DummySkills()
        engine = ModesEngine(skills)
        mode = SelfDefenseMode()
        mode.activate_streak_required = 1
        engine.add_mode(mode)

        engine.tick(_state(entities=[_hostile()], reflex_combat=True))

        self.assertIsNone(engine.get_status()["active"])
        self.assertEqual(skills.calls, [], "本地反射在打，后端一个字节都不该发")

    def test_active_self_defense_stops_dispatching_when_reflex_kicks_in(self):
        skills = DummySkills()
        engine = ModesEngine(skills)
        mode = SelfDefenseMode()
        mode.activate_streak_required = 1
        engine.add_mode(mode)

        engine.tick(_state(entities=[_hostile()]))
        self.assertEqual(skills.count("attack"), 1)

        mode._last_dispatch_at = 0.0
        for _ in range(3):
            engine.tick(_state(entities=[_hostile()], reflex_combat=True))

        self.assertEqual(skills.count("attack"), 1, "反射接手后不许再发 attack")
        self.assertEqual(skills.count("look_at"), 1)

    def test_self_preservation_yields_drowning_to_swim_reflex(self):
        skills = DummySkills()
        engine = ModesEngine(skills)
        mode = SelfPreservationMode()
        engine.add_mode(mode)

        engine.tick(_state({"air_ticks": 10, "x": 0, "z": 0}, reflex_swim=True))

        self.assertIsNone(engine.get_status()["active"])
        self.assertEqual(skills.calls, [], "本地上浮反射在处理，别再按跳")

    def test_self_preservation_still_handles_drowning_without_the_reflex(self):
        skills = DummySkills()
        engine = ModesEngine(skills)
        engine.add_mode(SelfPreservationMode())

        engine.tick(_state({"air_ticks": 10, "x": 0, "z": 0}))

        self.assertEqual(engine.get_status()["active"], "self_preservation")
        self.assertTrue(any(call[0] == "drive" for call in skills.calls))

    def test_self_preservation_does_not_press_keys_when_threat_vanishes_mid_tick(self):
        """tick 里威胁消失（如反射接手）时不能掉进"虚空兜底"乱按。"""
        mode = SelfPreservationMode()
        skills = DummySkills()
        mode._satisfied_streak = 5                 # 引擎认为它还该活跃

        mode.tick(skills, _state({"air_ticks": 10}, reflex_swim=True))

        self.assertEqual(skills.calls, [])


# ── ⑤ 循环检测：退避 + 标失败 + 上报（不再无声抛异常）───────────

class LoopBackoffTests(unittest.TestCase):
    def _manager(self, base=2.0):
        manager = ActionManager()
        manager._backoff_base = base
        return manager

    def test_repeats_of_unfinished_action_warn_once_then_back_off(self):
        manager = self._manager()

        with self.assertLogs("action_manager", level="WARNING") as logs:
            manager.track("attack", "req_1")
            manager.track("attack", "req_2")
            manager.track("attack", "req_3")       # 第 3 次 → 退避
            rejected = manager.track("attack", "req_4")   # 退避窗口内 → 拒
            manager.track("attack", "req_5")

        warnings = [line for line in logs.output if "Loop warning" in line]
        self.assertEqual(len(warnings), 1, "退避期内不再刷日志")
        self.assertFalse(rejected, "退避期内应向调用方明说被拒")
        self.assertIn("attack", manager.get_status()["backoff"])

    def test_in_flight_repeat_does_not_churn_the_action(self):
        """同一动作还在跟踪中时只更新 req_id：不该再打一条 Stopped + Tracking。"""
        manager = self._manager()
        manager.track("attack", "req_1")
        first = manager.current

        manager.track("attack", "req_2")

        self.assertIs(manager.current, first)
        self.assertEqual(manager.current.req_id, "req_2")
        self.assertEqual(manager.current.elapsed < 1.0, True)

    def test_kill_marks_task_failed_and_reports_instead_of_raising(self):
        manager = self._manager(base=0.0)          # 退避瞬间过期，便于快速走完 5 次

        for index in range(1, manager._max_kills + 1):
            manager.track("look_at", f"req_{index}")   # 不该抛异常

        report = manager.pop_loop_failure()
        self.assertIsNotNone(report)
        self.assertEqual(report["label"], "look_at")
        self.assertEqual(report["count"], manager._max_kills)
        self.assertFalse(manager.is_busy, "被判失败的任务必须让出跟踪位")
        self.assertIsNone(manager.pop_loop_failure(), "上报是拉取式的，取一次就没了")

    def test_successful_repeats_are_not_a_loop(self):
        """打架时每秒攻击一次、每次都有回执 → 绝不能判成死循环。"""
        manager = self._manager()

        for index in range(6):
            manager.track("attack", f"req_{index}")
            manager.handle_response(f"req_{index}", True)

        self.assertEqual(manager._warnings, 0)
        self.assertEqual(manager.get_status()["backoff"], {})
        self.assertIsNone(manager.last_loop_failure)

    def test_failed_action_then_retry_counts_towards_the_loop(self):
        manager = self._manager()

        manager.track("move_to", "req_1")
        manager.handle_response("req_1", False)    # 失败 → 计数保留
        manager.track("move_to", "req_2")
        manager.handle_response("req_2", False)

        self.assertEqual(manager._repeat_streak.get("move_to"), 2)

    def test_backoff_expiry_allows_a_retry(self):
        manager = self._manager()
        manager.track("attack", "req_1")
        manager.track("attack", "req_2")
        manager.track("attack", "req_3")
        manager._backoff["attack"] = (time.time() - 1.0, 1)   # 退避已过期

        self.assertTrue(manager.track("attack", "req_4"))

    def test_run_action_also_respects_backoff(self):
        manager = self._manager()
        manager._backoff["patrol"] = (time.time() + 5.0, 1)

        self.assertFalse(manager.run_action("patrol", lambda: None))
        self.assertIsNone(manager.current)

    def test_status_reports_loop_bookkeeping(self):
        manager = self._manager()
        manager.track("attack", "req_1")

        status = manager.get_status()

        self.assertEqual(status["label"], "attack")
        self.assertEqual(status["loop_warnings"], 0)
        self.assertIn("last_loop_failure", status)


class FakeSocket:
    """只实现 sendall 的假 socket：让真实的编码/换行逻辑照常跑，用于数帧。"""

    def __init__(self):
        self.chunks: list[bytes] = []

    def sendall(self, data: bytes) -> None:
        self.chunks.append(data)

    def close(self) -> None:
        pass


class EndToEndThrashRegressionTests(unittest.TestCase):
    """把日志里那几行**原样重放**：模式不再抖、动作不再重发、不再有 loop warning。

    这里按 Session 的真实接线来：`Skills` 的命令观察者 → `ActionManager.track`。
    """

    def test_combat_scenario_stays_stable(self):
        import json
        import logging

        from agent.skills import Skills
        from protocol.wire_client import WireClient

        wire = WireClient("127.0.0.1", 25568)
        fake = FakeSocket()
        wire.sock = fake
        skills = Skills(wire)
        manager = ActionManager()
        skills.set_command_observer(
            lambda cmd, req_id, context, args: manager.track(cmd, req_id))

        engine = ModesEngine(skills)
        mode = SelfDefenseMode()
        mode.activate_streak_required = 1
        engine.add_mode(mode)
        state = _state(entities=[_hostile()])

        records = []
        handler = logging.Handler()
        handler.emit = records.append
        action_logger = logging.getLogger("action_manager")
        action_logger.addHandler(handler)
        try:
            active = []
            for _ in range(12):
                engine.tick(state)
                active.append(engine.get_status()["active"])
        finally:
            action_logger.removeHandler(handler)

        # 12 个 tick 全程保持激活（历史 bug：每个 tick 都 Activated→Deactivated）
        self.assertEqual(active, ["self_defense"] * 12)
        # 一个警告都不该有
        noisy = [r.getMessage() for r in records if r.levelno >= logging.WARNING]
        self.assertEqual(noisy, [])
        # 同一目标的动作只下发一次（12 个 tick 在 1 秒内）
        frames = [json.loads(chunk.decode("utf-8"))
                  for chunk in fake.chunks for line in [chunk.splitlines()[0]] if line]
        sent = [frame["cmd"] for frame in frames]
        self.assertEqual(sent, ["look_at", "attack"])
        self.assertEqual(manager.current_label, "attack")


if __name__ == "__main__":
    unittest.main()
