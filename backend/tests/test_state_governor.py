"""资源租约 + 状态治理的验收测试（``agent/resource_lease.py`` / ``agent/state_governor.py``）。

覆盖（用户口径）：

  ① 同资源互斥、异资源并行 —— 尤其「**边走边吃**」「边走边说」必须能真的同时跑；
  ② 卡死超时 ⇒ 取消 + **真实原因** + 审计；
  ③ 取消后 T 秒内**不许立刻重试**；
  ④ **P0 反射永不被取消**；
  ⑤ 总开关关掉 ⇒ 行为不变；
  ⑥ 优先级抢占顺序（P0 永远可抢；普通任务按优先级）。

以及接线：planner 派发前仲裁、skills 的 P0 占用走生存通道、session 的取消写进现有通道、
config.json 的 ``state_governor`` 键带默认值。
"""

import unittest
from types import SimpleNamespace

from agent.action_manager import ActionManager
from agent.config_store import ConfigStore, _default_config
from agent.planner import L0_PLANNER_CONTRACT, Planner
from agent.resource_lease import (AUDIT_MAX, LEASE_CANCEL, LEASE_GRANT, LEASE_PREEMPT,
                                  LEASE_QUEUE, OWNER_P0_REFLEX, PRIORITY_DEFAULT,
                                  PRIORITY_SURVIVAL, RESOURCE_ATTACK, RESOURCE_CHAT,
                                  RESOURCE_CONTAINER, RESOURCE_LOOK, RESOURCE_MOVE,
                                  RESOURCE_USE, ResourceLease, conflicts,
                                  register_survival_hold, resources_for_tool)
from agent.session import Session
from agent.skills import Skills
from agent.state_governor import (GOV_CANCEL, GovernorConfig, StateGovernor)
from agent.task_list import TaskList


class FakeClock:
    def __init__(self, now: float = 1000.0):
        self.now = float(now)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += float(seconds)


class DummyMemory:
    def build_context(self):
        return {}

    def get_recent_context(self, _count):
        return []


class RecordingSkills:
    """记录下发的技能调用（不碰 wire）。"""

    def __init__(self):
        self.calls: list[tuple] = []

    def move_to(self, *args, **kwargs):
        self.calls.append(("move_to",))

    def eat(self):
        self.calls.append(("eat",))

    def send_chat(self, message):
        self.calls.append(("send_chat", message))


class FakeControl:
    """Skills 控制流的假出口（只记录）。"""

    def __init__(self):
        self.sets: list[dict] = []
        self.released = 0

    def set(self, **kwargs):
        self.sets.append(dict(kwargs))

    def look(self, *a):
        pass

    def look_at(self, *a):
        pass

    def flush(self):
        return True

    def release(self):
        self.released += 1


# ── ① 资源集与冲突 ───────────────────────────────────────────

class ResourceModelTests(unittest.TestCase):
    def test_conflict_matrix(self):
        self.assertFalse(conflicts({RESOURCE_MOVE}, {RESOURCE_USE}), "边走边吃：不冲突")
        self.assertFalse(conflicts({RESOURCE_MOVE}, {RESOURCE_CHAT}), "边走边说：不冲突")
        self.assertTrue(conflicts({RESOURCE_MOVE}, {RESOURCE_MOVE}), "同资源互斥")
        self.assertTrue(conflicts({RESOURCE_CONTAINER}, {RESOURCE_MOVE}), "容器独占：排斥一切")
        self.assertTrue(conflicts({RESOURCE_LOOK}, {RESOURCE_USE}), "视角独占：排斥一切")
        self.assertFalse(conflicts({RESOURCE_CHAT}, {RESOURCE_CHAT}), "chat 永远可并行")

    def test_walk_and_eat_run_in_parallel(self):
        lease = ResourceLease()
        walk = lease.request("task:walk", {RESOURCE_MOVE}, priority=50)
        eat = lease.request("task:eat", {RESOURCE_USE}, priority=50)

        self.assertTrue(walk["granted"])
        self.assertTrue(eat["granted"], "异资源必须能真的同时跑（边走边吃）")
        self.assertEqual({h["owner"] for h in lease.holders()}, {"task:walk", "task:eat"})

    def test_walk_and_chat_run_in_parallel(self):
        lease = ResourceLease()
        lease.request("task:walk", {RESOURCE_MOVE})
        chat = lease.request("task:talk", {RESOURCE_CHAT})

        self.assertTrue(chat["granted"], "chat 永远可并行（边走边说）")

    def test_same_resource_is_mutually_exclusive_and_queues(self):
        lease = ResourceLease()
        lease.request("task:a", {RESOURCE_MOVE}, priority=50)
        second = lease.request("task:b", {RESOURCE_MOVE}, priority=10)

        self.assertFalse(second["granted"])
        self.assertTrue(second["queued"])
        self.assertEqual(second["blocked_by"], ["task:a"])
        self.assertEqual([p["owner"] for p in lease.pending()], ["task:b"])

    def test_queued_request_is_granted_when_resource_frees(self):
        lease = ResourceLease()
        lease.request("task:a", {RESOURCE_MOVE}, priority=50)
        lease.request("task:b", {RESOURCE_MOVE}, priority=10)

        lease.release("task:a", "done")

        self.assertTrue(lease.is_held_by("task:b"), "资源空出后排队者应被放行")
        self.assertEqual(lease.pending(), [])

    def test_unknown_resource_is_rejected(self):
        lease = ResourceLease()
        with self.assertRaises(ValueError):
            lease.request("task:x", {"teleport"})

    def test_audit_records_who_what_why(self):
        lease = ResourceLease()
        lease.request("task:a", {RESOURCE_MOVE}, priority=50, reason="去挖矿")
        lease.request("task:b", {RESOURCE_MOVE}, priority=10, reason="去砍树")

        kinds = [e["kind"] for e in lease.events()]
        self.assertEqual(kinds, [LEASE_GRANT, LEASE_QUEUE])
        queue = lease.events()[-1]
        self.assertEqual(queue["owner"], "task:b")
        self.assertEqual(queue["reason"], "去砍树")
        self.assertEqual(queue["detail"]["blocked_by"], ["task:a"])

    def test_audit_is_bounded(self):
        lease = ResourceLease()
        for i in range(AUDIT_MAX + 5):
            lease.request(f"o{i}", {RESOURCE_MOVE})
            lease.release(f"o{i}")
        self.assertLessEqual(len(lease.events()), AUDIT_MAX)


# ── ⑥ 优先级抢占 ─────────────────────────────────────────────

class PreemptionTests(unittest.TestCase):
    def test_higher_priority_preempts_lower(self):
        lease = ResourceLease()
        lease.request("task:low", {RESOURCE_MOVE}, priority=10)
        result = lease.request("task:high", {RESOURCE_MOVE}, priority=80, reason="更急")

        self.assertTrue(result["granted"])
        self.assertEqual(result["preempted"], ["task:low"])
        self.assertFalse(lease.is_held_by("task:low"))
        preempt = next(e for e in lease.events() if e["kind"] == LEASE_PREEMPT)
        self.assertEqual(preempt["detail"]["victim"], "task:low")
        self.assertEqual(preempt["reason"], "更急")

    def test_equal_or_lower_priority_does_not_preempt(self):
        lease = ResourceLease()
        lease.request("task:a", {RESOURCE_MOVE}, priority=50)
        result = lease.request("task:b", {RESOURCE_MOVE}, priority=50)
        self.assertFalse(result["granted"], "同级不许抢占，只能排队")

    def test_p0_survival_always_preempts_and_is_never_queued(self):
        lease = ResourceLease()
        lease.request("task:a", {RESOURCE_MOVE}, priority=100)
        result = lease.request("p0", {RESOURCE_MOVE}, survival=True, reason="岩浆")

        self.assertTrue(result["granted"])
        self.assertEqual(result["preempted"], ["task:a"])
        self.assertTrue(lease.holders()[0]["survival"])

    def test_non_survival_cannot_preempt_survival(self):
        lease = ResourceLease()
        lease.request("p0", {RESOURCE_MOVE}, survival=True)
        result = lease.request("task:x", {RESOURCE_MOVE}, priority=100, reason="也想要")

        self.assertFalse(result["granted"], "P0 持有期间普通任务抢不走")
        self.assertTrue(lease.is_held_by("p0"))

    def test_priority_is_clamped(self):
        lease = ResourceLease()
        lease.request("task:x", {RESOURCE_MOVE}, priority=99999)
        self.assertEqual(lease.holders()[0]["priority"], 1000)


# ── ②③④ 状态治理 ─────────────────────────────────────────────

def _collect_state(x=0.0, detail="1/8", status="collecting"):
    return {"player": {"x": x, "y": 64.0, "z": 0.0},
            "task_state": {"kind": "collect", "status": status, "detail": detail,
                           "progress": 0.1}}


class GovernorCancelTests(unittest.TestCase):
    def _governor(self, clock, **cfg):
        params = {"no_progress_seconds": 5.0, "retry_cooldown_seconds": 10.0}
        params.update(cfg)
        lease = ResourceLease(clock=clock)
        return StateGovernor(clock=clock, lease=lease,
                             config=GovernorConfig(**params)), lease

    def test_no_progress_timeout_cancels_with_reason_and_audit(self):
        clock = FakeClock()
        gov, lease = self._governor(clock)
        cancelled: list[dict] = []
        gov.register("action:collect_blocks", resources={RESOURCE_MOVE, RESOURCE_ATTACK},
                     priority=50, kind="collect", target="collect_blocks",
                     reason="在途动作", cancel=lambda p: cancelled.append(p))
        self.assertTrue(lease.is_held_by("action:collect_blocks"))

        clock.advance(4.0)
        self.assertEqual(gov.tick(_collect_state(x=3.0)), [], "有进展不该取消")
        clock.advance(6.0)
        out = gov.tick(_collect_state(x=3.0))

        self.assertEqual(len(out), 1)
        self.assertIn("无进展", out[0]["reason"])
        self.assertEqual(cancelled, out)
        self.assertFalse(lease.is_held_by("action:collect_blocks"),
                         "取消必须释放它占的资源")
        kinds = [e["kind"] for e in gov.events()]
        self.assertIn(GOV_CANCEL, kinds)
        self.assertEqual([e["kind"] for e in lease.events()], [LEASE_GRANT, LEASE_CANCEL])

    def test_repeated_tool_result_is_treated_as_no_progress(self):
        clock = FakeClock()
        gov, _lease = self._governor(clock)
        gov.register("action:craft_item", resources={RESOURCE_CONTAINER}, priority=50,
                     kind="craft", target="craft_item")
        state = {"player": {"x": 0, "y": 64, "z": 0},
                 "task_state": {"kind": "craft", "status": "crafting", "detail": "2/4"}}
        self.assertEqual(gov.tick(state), [])
        clock.advance(3.0)
        self.assertEqual(gov.tick(state), [])
        clock.advance(3.0)
        out = gov.tick(state)
        self.assertEqual(len(out), 1, "同一工具结果反复出现 = 无进展")

    def test_stuck_report_cancels_immediately_with_real_reason(self):
        clock = FakeClock()
        gov, _lease = self._governor(clock)
        gov.register("action:collect_blocks", resources={RESOURCE_MOVE}, kind="collect",
                     target="collect_blocks")
        state = {"player": {"x": 1, "y": 64, "z": 1},
                 "task_state": {"kind": "collect", "status": "collecting",
                                "detail": "[Stuck] 挖不动"}}
        out = gov.tick(state)
        self.assertEqual(len(out), 1)
        self.assertIn("已报告卡住", out[0]["reason"])

    def test_cancel_backs_off_the_target(self):
        clock = FakeClock()
        gov, _lease = self._governor(clock)
        gov.register("action:collect_blocks", resources={RESOURCE_MOVE}, kind="collect",
                     target="collect_blocks")
        clock.advance(6.0)
        gov.tick(_collect_state(x=1.0))

        self.assertTrue(gov.is_backed_off("collect_blocks"), "取消后 T 秒内退避")
        self.assertFalse(gov.register("action:collect_blocks", resources={RESOURCE_MOVE},
                                      target="collect_blocks"),
                         "退避窗口内不许重新注册（不许立刻重试）")
        clock.advance(11.0)
        self.assertFalse(gov.is_backed_off("collect_blocks"), "退避窗口过了就该允许")

    def test_p0_survival_is_never_cancelled(self):
        clock = FakeClock()
        gov, _lease = self._governor(clock)
        gov.register("p0:reflex", resources={RESOURCE_MOVE}, survival=True, kind="reflex")
        clock.advance(1000.0)
        self.assertEqual(gov.tick(_collect_state(x=0.0)), [], "P0 反射永不被取消")
        self.assertIn("p0:reflex", [t["key"] for t in gov.snapshot()["tracked"]])

    def test_cancel_hook_is_called(self):
        clock = FakeClock()
        calls: list[dict] = []
        gov = StateGovernor(clock=clock, config=GovernorConfig(no_progress_seconds=1.0),
                            cancel_hook=lambda p: calls.append(p))
        gov.register("action:x", resources={RESOURCE_MOVE}, kind="collect", target="x")
        clock.advance(2.0)
        gov.tick(_collect_state(x=0.0))
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["key"], "action:x")

    def test_max_tracked_never_evicts_survival(self):
        clock = FakeClock()
        gov = StateGovernor(clock=clock, config=GovernorConfig(max_tracked=2))
        gov.register("p0", resources={RESOURCE_MOVE}, survival=True)
        gov.register("a", resources={RESOURCE_USE})
        gov.register("b", resources={RESOURCE_ATTACK})
        gov.register("c", resources={RESOURCE_CHAT})
        keys = {t["key"] for t in gov.snapshot()["tracked"]}
        self.assertIn("p0", keys, "绝不为了腾地方摘除 P0")


# ── ⑤ 总开关 ─────────────────────────────────────────────────

class SwitchTests(unittest.TestCase):
    def test_disabled_governor_does_nothing(self):
        gov = StateGovernor(config=GovernorConfig(enabled=False))
        self.assertFalse(gov.enabled())
        self.assertFalse(gov.register("action:x", resources={RESOURCE_MOVE}))
        self.assertEqual(gov.tick(_collect_state()), [])
        self.assertEqual(gov.snapshot()["tracked"], [])

    def test_default_config_is_enabled(self):
        self.assertTrue(_default_config()["state_governor"]["enabled"])

    def test_config_store_roundtrip(self):
        import tempfile
        import os
        with tempfile.TemporaryDirectory() as tmp:
            store = ConfigStore(os.path.join(tmp, "config.json"))
            cfg = store.get_state_governor_config()
            self.assertTrue(cfg["enabled"])
            self.assertEqual(cfg["no_progress_seconds"], 20.0)
            self.assertEqual(cfg["retry_cooldown_seconds"], 30.0)

            store.set_app_config({"state_governor": {"enabled": False,
                                                     "no_progress_seconds": 7}})
            cfg2 = store.get_state_governor_config()
            self.assertFalse(cfg2["enabled"])
            self.assertEqual(cfg2["no_progress_seconds"], 7)

    def test_disabled_governor_leaves_planner_dispatch_intact(self):
        lease = ResourceLease()
        register_survival_hold(lease, resources={RESOURCE_MOVE})
        gov = StateGovernor(config=GovernorConfig(enabled=False))
        skills = RecordingSkills()
        planner = Planner(llm_service=None, memory=DummyMemory(), skills=skills,
                          resource_lease=lease, state_governor=gov)

        self.assertTrue(planner._dispatch_tool_call("move_to", {"x": 1, "y": 2, "z": 3}, {}))
        self.assertEqual(skills.calls, [("move_to",)])
        self.assertIsNone(planner._action_note)


# ── 接线：planner 派发前仲裁 ─────────────────────────────────

class PlannerArbitrationTests(unittest.TestCase):
    def _planner(self, **kwargs):
        skills = RecordingSkills()
        planner = Planner(llm_service=None, memory=DummyMemory(), skills=skills, **kwargs)
        return planner, skills

    def test_move_denied_while_p0_reflex_holds_move(self):
        planner, skills = self._planner()
        register_survival_hold(planner.resource_lease, resources={RESOURCE_MOVE})

        handled = planner._dispatch_tool_call("move_to", {"x": 1, "y": 2, "z": 3}, {})

        self.assertFalse(handled, "P0 逃生占着 move 时模型不许抢")
        self.assertEqual(skills.calls, [], "被挡必须一帧都不发")
        self.assertIn("排队", planner._action_note)

    def test_eat_still_allowed_while_moving(self):
        planner, skills = self._planner()
        planner.resource_lease.request("task:walk", {RESOURCE_MOVE}, priority=50)

        handled = planner._dispatch_tool_call("eat", {}, {})

        self.assertTrue(handled, "走路占 move，吃占 use —— 异资源必须能并行（边走边吃）")
        self.assertEqual(skills.calls, [("eat",)])

    def test_lease_is_released_after_dispatch(self):
        planner, _skills = self._planner()
        planner._dispatch_tool_call("move_to", {"x": 1, "y": 2, "z": 3}, {})
        self.assertEqual(planner.resource_lease.holders(), [],
                         "工具是一次性下发，派发结束即释放")

    def test_backed_off_tool_is_not_dispatched(self):
        clock = FakeClock()
        gov = StateGovernor(clock=clock, config=GovernorConfig(retry_cooldown_seconds=30))
        gov.note_backoff("move_to", 30)
        planner, skills = self._planner(state_governor=gov)

        handled = planner._dispatch_tool_call("move_to", {"x": 1, "y": 2, "z": 3}, {})

        self.assertFalse(handled)
        self.assertEqual(skills.calls, [])
        self.assertIn("退避", planner._action_note)

    def test_tool_resource_declarations_are_present(self):
        self.assertEqual(resources_for_tool("move_to"), frozenset({RESOURCE_MOVE}))
        self.assertEqual(resources_for_tool("eat"), frozenset({RESOURCE_USE}))
        self.assertEqual(resources_for_tool("attack"), frozenset({RESOURCE_ATTACK}))
        self.assertEqual(resources_for_tool("container_move"), frozenset({RESOURCE_CONTAINER}))
        self.assertEqual(resources_for_tool("send_whisper"), frozenset({RESOURCE_CHAT}))
        self.assertEqual(resources_for_tool("stop"), frozenset())

    def test_static_prefix_is_byte_identical(self):
        planner, _skills = self._planner()
        system = planner.build_prompt_layers("Steve", "hi", {}, "AI").system
        self.assertIn(L0_PLANNER_CONTRACT[:16], system)
        self.assertEqual(len(system), 1648, "本批零 prompt 改动")
        for token in ("资源租约", "resource_lease", "state_governor", "独占"):
            self.assertNotIn(token, system, f"新接口不许进 L0/L1：{token}")


# ── 接线：skills 的 P0 占用走生存通道 ────────────────────────

class SkillsSurvivalHoldTests(unittest.TestCase):
    def _skills(self, lease):
        skills = Skills(wire=None)
        skills.set_control_stream(FakeControl())
        skills.set_resource_lease(lease, lambda: True)
        return skills

    def test_drive_registers_survival_hold(self):
        lease = ResourceLease()
        skills = self._skills(lease)
        skills.drive(forward=True)

        holders = lease.holders()
        self.assertEqual(len(holders), 1)
        self.assertEqual(holders[0]["owner"], OWNER_P0_REFLEX)
        self.assertTrue(holders[0]["survival"])
        self.assertEqual(holders[0]["priority"], PRIORITY_SURVIVAL)
        self.assertEqual(holders[0]["resources"], [RESOURCE_MOVE])

    def test_release_control_frees_the_hold(self):
        lease = ResourceLease()
        skills = self._skills(lease)
        skills.drive(forward=True)
        skills.release_control()
        self.assertEqual(lease.holders(), [])

    def test_p0_hold_never_blocks_drive_itself(self):
        lease = ResourceLease()
        # 模型任务先占了 move
        lease.request("task:walk", {RESOURCE_MOVE}, priority=90)
        skills = self._skills(lease)
        skills.drive(jump=True)   # P0 永远可抢
        self.assertTrue(lease.is_held_by(OWNER_P0_REFLEX))
        self.assertFalse(lease.is_held_by("task:walk"), "P0 抢走后模型任务被清掉")

    def test_without_lease_drive_is_unchanged(self):
        skills = Skills(wire=None)
        skills.set_control_stream(FakeControl())
        self.assertTrue(skills.drive(forward=True))  # 没接线 ⇒ 行为照旧


# ── 接线：session 的取消写进现有通道 ─────────────────────────

class SessionGovernorWiringTests(unittest.TestCase):
    def _session(self):
        session = Session.__new__(Session)
        session.id = "t"
        session.action_manager = ActionManager()
        session.runtime = {}
        session.tasks = TaskList()
        session._manual_task_kind = None
        return session

    def test_governor_cancel_stops_action_and_records_reason(self):
        session = self._session()
        session.action_manager.track("collect_blocks", "req_1", timeout=600)
        session.tasks.add("collect", owner="Owner", owner_tier="master", status="active")

        session._governor_cancel({"key": "action:collect_blocks", "kind": "collect_blocks",
                                  "target": "collect_blocks",
                                  "reason": "位置未变 + 已报告卡住"})

        self.assertFalse(session.action_manager.is_busy, "取消 = 直接打断在途动作")
        item = session.tasks.items()[0]
        self.assertEqual(item.status, "failed")
        self.assertIn("stuck", item.failure_reason)
        self.assertIn("位置未变", item.failure_reason)
        self.assertEqual(session.runtime["task_state"]["status"], "failed")
        self.assertEqual(session.runtime["task_state"]["source"], "state_governor")

    def test_governor_cancel_is_null_safe(self):
        session = self._session()
        session._governor_cancel({})  # 不该抛

    def test_status_exposes_governor_and_lease(self):
        session = Session.__new__(Session)
        session.id = "t"
        session.action_manager = SimpleNamespace(get_status=lambda: {"label": None})
        session.planner = SimpleNamespace(get_status=lambda: {"last_decision": {}})
        session.modes_engine = SimpleNamespace(get_status=lambda: {})
        session.self_prompter = SimpleNamespace(get_status=lambda: {})
        session.control = SimpleNamespace(holding=False)
        session.memory = SimpleNamespace(interaction_count=0)
        session.message_db = SimpleNamespace(get_stats=lambda: {})
        session.runtime = {}
        session.vision = SimpleNamespace(get_status=lambda: {})
        session.poi = SimpleNamespace(namespace="srv__world", find=lambda _kind: [])
        session.llm = SimpleNamespace(get_usage=lambda: {})
        session.resource_lease = ResourceLease()
        session.state_governor = StateGovernor()

        status = session.get_status()

        self.assertIn("holders", status["resource_lease"])
        self.assertTrue(status["state_governor"]["enabled"])


if __name__ == "__main__":
    unittest.main()
