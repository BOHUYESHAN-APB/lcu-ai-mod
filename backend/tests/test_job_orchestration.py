"""job 句柄 + 编排轮次的验收测试（`agent/job_registry.py`，AGENT_ARCH §10.2 #2/#3）。

覆盖（用户口径 / 工单验收）：

  ① job 生命周期：running → done / failed / cancelled；
  ② 四样硬门槛（进度来源 / 终止条件 / 失败原因 code / 取消路径）**缺一即不许登记**；
  ③ **安静 ⇒ 零额外模型调用**（不是定时器）；
  ④ 事件（进度变化 / 出结果 / 卡住 / 玩家说话）⇒ **一轮**编排（带紧凑摘要）；
  ⑤ 摘要**不进 L0/L1**（哨兵值不得出现在 system / 静态前缀里）；
  ⑥ 取消路径（真副作用在回调里；卡死走**已有** state_governor，不另造）；
  ⑦ 关掉开关 ⇒ 行为回到现在（不再有编排轮）；
  ⑧ `set_goal` / `end_goal` 写的是**现有** `task_list`（不是第二套存储）。

外加接线：planner 派发长任务登记 job、config.json 的 `job_orchestration` 键带默认值。
"""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from agent.action_manager import ActionManager
from agent.config_store import ConfigStore, _default_config
from agent.job_registry import (DEFAULT_ENABLED, JOB_CANCELLED, JOB_DONE, JOB_FAILED,
                                JOB_RUNNING, ORCH_EVENT_OUTCOME, ORCH_EVENT_PLAYER,
                                ORCH_EVENT_PROGRESS, ORCH_EVENT_STUCK, JobRegistry,
                                JobRejected, OrchestrationTurns,
                                has_model_created_active_task, missing_requirements)
from agent.planner import L0_PLANNER_CONTRACT, Planner, build_tool_schemas
from agent.session import Session
from agent.state_governor import GovernorConfig, StateGovernor
from agent.task_list import STATUS_ACTIVE, TaskList

DIGEST_SENTINEL = "SENTINEL_ORCH_DIGEST"


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


class DummySkills:
    def __init__(self):
        self.calls: list[tuple] = []

    def __getattr__(self, name):
        def _record(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            return {"success": True}

        return _record


class TrackingSkills:
    """把技能调用转成 action_manager.track（模拟真实下发链路，不碰 wire）。"""

    def __init__(self, action_manager: ActionManager):
        self.action_manager = action_manager

    def move_to(self, *_args, **_kwargs):
        self.action_manager.track("move_to", "req-1", timeout=120.0,
                                  complete_on_response=False)
        return {"req_id": "req-1"}


def _planner(**kwargs) -> Planner:
    kwargs.setdefault("memory", DummyMemory())
    kwargs.setdefault("skills", DummySkills())
    return Planner(llm_service=None, **kwargs)


# ── ① job 生命周期 ──────────────────────────────────────────────

class JobLifecycleTests(unittest.TestCase):
    def _registry(self):
        return JobRegistry(clock=FakeClock())

    def _register(self, registry, **overrides):
        kwargs = dict(progress=lambda: 0.0, terminal=lambda: None,
                      failure="tool_failed", cancel=lambda reason="": None)
        kwargs.update(overrides)
        return registry.register("move_to", **kwargs)

    def test_running_to_done(self):
        registry = self._registry()
        job = self._register(registry)

        self.assertEqual(job.status, JOB_RUNNING)
        registry.set_progress(job.id, 0.5)
        self.assertEqual(registry.get(job.id).progress, 0.5)
        registry.finish(job.id, result={"detail": "ok"})

        self.assertEqual(registry.get(job.id).status, JOB_DONE)
        self.assertEqual(registry.get(job.id).progress, 1.0)
        self.assertTrue(registry.get(job.id).terminal)

    def test_running_to_failed_carries_a_code(self):
        registry = self._registry()
        job = self._register(registry)

        registry.fail(job.id, code="tool_failed", message="卡在第一步")

        closed = registry.get(job.id)
        self.assertEqual(closed.status, JOB_FAILED)
        self.assertEqual(closed.failure_code, "tool_failed")
        self.assertIn("卡在第一步", str(closed.result))

    def test_running_to_cancelled_runs_the_cancel_path(self):
        registry = self._registry()
        fired: list[str] = []
        job = self._register(registry, cancel=lambda reason="": fired.append(reason))

        ok, why = registry.cancel(job.id, reason="player stop")

        self.assertTrue(ok)
        self.assertEqual(why, "cancelled")
        self.assertEqual(fired, ["player stop"], "取消路径必须真的执行")
        self.assertEqual(registry.get(job.id).status, JOB_CANCELLED)

    def test_terminal_job_cannot_be_cancelled_twice(self):
        registry = self._registry()
        job = self._register(registry)
        registry.finish(job.id)

        self.assertEqual(registry.cancel(job.id), (False, "already_terminal"))
        self.assertEqual(registry.cancel("nope"), (False, "not_found"))

    def test_refresh_drives_lifecycle_from_the_declared_sources(self):
        registry = self._registry()
        progress = {"value": 0.25}
        done = {"value": False}
        job = self._register(registry, progress=lambda: progress["value"],
                             terminal=lambda: ({"status": JOB_DONE} if done["value"] else None))

        registry.refresh()
        self.assertEqual(registry.get(job.id).progress, 0.25)

        done["value"] = True
        registry.refresh()
        self.assertEqual(registry.get(job.id).status, JOB_DONE)

    def test_snapshot_is_json_friendly(self):
        import json

        registry = self._registry()
        job = self._register(registry)
        snapshot = registry.snapshot()

        self.assertEqual(snapshot["count"], 1)
        self.assertEqual(snapshot["active"], 1)
        self.assertEqual(snapshot["jobs"][0]["id"], job.id)
        self.assertTrue(snapshot["jobs"][0]["has_cancel_path"])
        json.dumps(snapshot, ensure_ascii=False)


# ── ② 四样硬门槛 ────────────────────────────────────────────────

class JobRequirementGateTests(unittest.TestCase):
    def test_missing_requirements_are_named(self):
        self.assertEqual(missing_requirements(progress=lambda: 0, terminal="t",
                                              failure="c", cancel=lambda r="": None), [])
        self.assertEqual(
            missing_requirements(progress=None, terminal=None, failure="", cancel=None),
            ["progress", "terminal", "failure", "cancel"])

    def test_each_missing_requirement_blocks_registration(self):
        registry = JobRegistry()
        complete = dict(progress=lambda: 0.0, terminal=lambda: None,
                        failure="tool_failed", cancel=lambda reason="": None)
        for missing in ("progress", "terminal", "failure", "cancel"):
            kwargs = dict(complete)
            kwargs[missing] = None if missing != "failure" else ""
            with self.assertRaises(JobRejected) as ctx:
                registry.register("move_to", **kwargs)
            self.assertIn(missing, ctx.exception.missing)
        self.assertEqual(registry.jobs(), [], "四样不全的一个都不许登记")

    def test_complete_job_is_accepted(self):
        registry = JobRegistry()
        job = registry.register("move_to", progress=lambda: 0.0, terminal=lambda: None,
                                failure="tool_failed", cancel=lambda reason="": None)
        self.assertEqual(len(registry.jobs()), 1)
        self.assertEqual(job.status, JOB_RUNNING)


# ── ⑥ 卡死走已有 state_governor（不另造）─────────────────────────

class JobGovernorReuseTests(unittest.TestCase):
    def test_stuck_job_is_cancelled_by_the_existing_governor(self):
        clock = FakeClock()
        governor = StateGovernor(
            clock=clock,
            config=GovernorConfig(no_progress_seconds=5.0, retry_cooldown_seconds=10.0))
        fired: list[str] = []
        registry = JobRegistry(governor=governor, clock=clock)
        job = registry.register("move_to", progress=lambda: 0.0, terminal=lambda: None,
                                failure="tool_failed",
                                cancel=lambda reason="": fired.append(reason))

        governor.tick({})              # 首次观测＝基线
        clock.advance(6.0)
        cancelled = governor.tick({})  # 无进展 ⇒ 摘掉

        self.assertTrue(cancelled, "治理器必须判定卡死并取消")
        self.assertEqual(registry.get(job.id).status, JOB_CANCELLED)
        self.assertTrue(fired and "无进展" in fired[0], "取消必须带真实原因")
        self.assertTrue(governor.is_backed_off(f"job:{job.id}"), "取消后必须退避")
        self.assertTrue(any(e["kind"] == "cancel" for e in governor.events()),
                        "取消必须留审计")
        self.assertTrue(any(e["kind"] == JOB_CANCELLED for e in registry.events()))


# ── ③④⑤⑦ 编排轮次 ──────────────────────────────────────────────

class FakePlanner:
    def __init__(self, response=None):
        self.calls: list[dict] = []
        self.response = response

    def plan_and_execute(self, sender, message, context, bot_name="AI"):
        self.calls.append({"sender": sender, "message": message, "context": dict(context)})
        return self.response


class _NullCtx:
    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


def _session(*, enabled=True, tasks=None, planner=None, min_interval=0.0) -> Session:
    """一个只装了编排轮所需依赖的 session 替身（不碰 wire / LLM）。"""
    session = Session.__new__(Session)
    session.tasks = tasks if tasks is not None else TaskList()
    session.jobs = JobRegistry()
    session.orchestration = OrchestrationTurns(enabled_provider=lambda: enabled,
                                               min_interval=min_interval)
    session._orch_task_signature = None
    session._event_audit = []
    session._actor = {"name": "", "tier": "guest"}
    session._reply_whisper_to = ""
    session.memory = SimpleNamespace(add_interaction=lambda **_k: None)
    session.conversation = None
    session.planner = planner if planner is not None else FakePlanner()
    session.skills = SimpleNamespace(command_context=lambda _ctx: _NullCtx(),
                                     send_chat=lambda _text: None)
    session._runtime_context = lambda: {}
    session.llm_active = lambda: True
    session._is_mentioned = lambda _m: False
    session.persona_blocks = lambda _scene: {}
    session._scene_for = lambda *_a: ""
    session._get_bot_name = lambda: "AI"
    return session


def _goal_task_list(detail: str = "把这片林子砍完") -> TaskList:
    tasks = TaskList()
    tasks.add("goal", owner="Steve", owner_tier="friend", detail=detail,
              status=STATUS_ACTIVE, model_created=True)
    return tasks


class OrchestrationQuietTests(unittest.TestCase):
    """**安静 ⇒ 零额外模型调用**（证明它由事件驱动，不是定时器）。"""

    def test_no_events_means_no_model_call(self):
        planner = FakePlanner()
        session = _session(tasks=_goal_task_list(), planner=planner)

        self.assertIsNone(session._tick_orchestration())
        self.assertEqual(planner.calls, [], "没有事件时一次模型调用都不许有")

    def test_repeated_quiet_ticks_stay_silent(self):
        planner = FakePlanner()
        session = _session(tasks=_goal_task_list(), planner=planner)

        for _ in range(5):
            session._tick_orchestration()

        self.assertEqual(planner.calls, [])

    def test_events_without_a_model_created_task_do_not_trigger(self):
        planner = FakePlanner()
        tasks = TaskList()
        tasks.add("mine", owner="Steve", detail="自主挖矿", status=STATUS_ACTIVE)
        session = _session(tasks=tasks, planner=planner)
        session.orchestration.note_event(ORCH_EVENT_PROGRESS, {"tasks": 1})

        self.assertIsNone(session._tick_orchestration())
        self.assertEqual(planner.calls, [], "没有'模型自己创建的'活跃任务 ⇒ 不请模型")

    def test_disabled_switch_restores_current_behaviour(self):
        planner = FakePlanner()
        session = _session(enabled=False, tasks=_goal_task_list(), planner=planner)
        session.orchestration.note_event(ORCH_EVENT_PROGRESS, {"tasks": 1})

        self.assertIsNone(session._tick_orchestration())
        self.assertEqual(planner.calls, [], "关掉开关 ⇒ 行为回到现在")


class OrchestrationEventTests(unittest.TestCase):
    def test_one_event_produces_exactly_one_turn_with_a_digest(self):
        planner = FakePlanner()
        session = _session(tasks=_goal_task_list(), planner=planner)
        session.orchestration.note_event(ORCH_EVENT_PROGRESS, {"tasks": 1})

        session._tick_orchestration()

        self.assertEqual(len(planner.calls), 1, "一个事件 ⇒ 恰好一轮")
        call = planner.calls[0]
        self.assertIn("编排进展摘要", call["message"])
        self.assertIn("把这片林子砍完", call["message"], "摘要要带活跃目标的进展")
        self.assertEqual(call["context"].get("orchestration"), call["message"])
        self.assertFalse(session.orchestration.has_event(), "事件必须被消费，不许反复触发")

    def test_all_four_event_kinds_are_accepted(self):
        orchestration = OrchestrationTurns(enabled_provider=lambda: True)
        for kind in (ORCH_EVENT_PROGRESS, ORCH_EVENT_OUTCOME, ORCH_EVENT_STUCK,
                     ORCH_EVENT_PLAYER):
            self.assertTrue(orchestration.note_event(kind, {}))
        self.assertEqual(len(orchestration.pending()), 4)
        self.assertFalse(orchestration.note_event("bogus", {}))

    def test_stuck_event_surfaces_the_reason_in_the_digest(self):
        planner = FakePlanner()
        session = _session(tasks=_goal_task_list(), planner=planner)
        session.orchestration.note_event(ORCH_EVENT_STUCK, {"target": "move_to",
                                                            "reason": "20s 无进展"})

        session._tick_orchestration()

        self.assertIn("卡住", planner.calls[0]["message"])
        self.assertIn("20s 无进展", planner.calls[0]["message"])

    def test_outcome_and_progress_from_task_state_are_noted(self):
        session = _session(tasks=_goal_task_list())
        session.tasks.add("mine", owner="", owner_tier="guest", status=STATUS_ACTIVE)
        session._absorb_task_state({"kind": "mine", "status": "collecting", "progress": 0.4})
        kinds = [e["kind"] for e in session.orchestration.pending()]
        self.assertIn(ORCH_EVENT_PROGRESS, kinds)

        session._absorb_task_state({"kind": "mine", "status": "failed", "reason": "stuck"})
        kinds = [e["kind"] for e in session.orchestration.pending()]
        self.assertIn(ORCH_EVENT_OUTCOME, kinds)

    def test_player_speech_event_is_consumed_by_the_chat_turn(self):
        session = _session(tasks=_goal_task_list())
        session.orchestration.note_event(ORCH_EVENT_PLAYER, {"sender": "Steve"})

        session.orchestration.consume_kind(ORCH_EVENT_PLAYER)

        self.assertFalse(session.orchestration.has_event(),
                         "对话那一轮已处理这次发言 ⇒ 不该再跑编排轮")

    def test_min_interval_prevents_a_self_exciting_loop(self):
        orchestration = OrchestrationTurns(enabled_provider=lambda: True, min_interval=30.0)
        orchestration.note_event(ORCH_EVENT_PROGRESS, {})
        self.assertTrue(orchestration.should_run(True))
        orchestration.consume()

        orchestration.note_event(ORCH_EVENT_PROGRESS, {})
        self.assertFalse(orchestration.should_run(True), "最小间隔内不许连跑两轮")


class DigestLayeringTests(unittest.TestCase):
    """⑤ 编排摘要**只进 L2**（当轮 user），绝不进 L0/L1 静态前缀。"""

    def test_digest_never_lands_in_the_system_prefix(self):
        planner = _planner()
        layers = planner.build_prompt_layers(
            "Steve", "hi", {"orchestration": DIGEST_SENTINEL}, "AI")

        self.assertNotIn(DIGEST_SENTINEL, layers.system)
        self.assertIn(DIGEST_SENTINEL, layers.user)
        self.assertTrue(layers.system.startswith(L0_PLANNER_CONTRACT))

    def test_digest_does_not_change_the_static_prefix_length(self):
        planner = _planner()
        without = planner.build_prompt_layers("Steve", "hi", {}, "AI").system
        with_digest = planner.build_prompt_layers(
            "Steve", "hi", {"orchestration": DIGEST_SENTINEL}, "AI").system

        self.assertEqual(without, with_digest, "摘要不得改变 system（前缀缓存友好）")

    def test_tools_still_go_through_the_tools_argument(self):
        planner = _planner()
        layers = planner.build_prompt_layers("Steve", "hi", {"orchestration": DIGEST_SENTINEL}, "AI")
        self.assertTrue(layers.tools)
        self.assertNotIn("可用动作（", layers.system + layers.user)


# ── ⑧ set_goal / end_goal 写现有 task_list ──────────────────────

class GoalToolTests(unittest.TestCase):
    def test_both_goal_tools_are_registered_and_l3_only(self):
        names = {item["function"]["name"] for item in build_tool_schemas()}
        self.assertIn("set_goal", names)
        self.assertIn("end_goal", names)

        planner = _planner()
        layers = planner.build_prompt_layers("Steve", "hi", {}, "AI")
        self.assertNotIn("set_goal", layers.system, "工具 schema 只走 L3，不进静态前缀")

    def test_set_goal_writes_into_the_existing_task_list(self):
        tasks = TaskList()
        planner = _planner(task_list=tasks)

        handled = planner._dispatch_tool_call(
            "set_goal", {"text": "把这片林子砍完"},
            {"actor_name": "Steve", "actor_tier": "friend"})

        self.assertTrue(handled)
        self.assertIs(planner.task_list, tasks, "写的是同一份 task_list，不是第二套存储")
        items = tasks.items()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].kind, "goal")
        self.assertEqual(items[0].detail, "把这片林子砍完")
        self.assertEqual(items[0].owner, "Steve")
        self.assertEqual(items[0].owner_tier, "friend")
        self.assertTrue(items[0].model_created, "目标要标成'模型自己创建'（编排触发条件）")

    def test_end_goal_ends_the_active_goal(self):
        tasks = TaskList()
        planner = _planner(task_list=tasks)
        planner._dispatch_tool_call("set_goal", {"text": "砍完这片林子"}, {})
        self.assertTrue(has_model_created_active_task(tasks))

        handled = planner._dispatch_tool_call("end_goal", {}, {})

        self.assertTrue(handled)
        self.assertEqual(tasks.items()[0].status, "done")
        self.assertFalse(has_model_created_active_task(tasks),
                         "显式结束后不再占着（编排轮次随之停下）")

    def test_end_goal_without_a_goal_is_a_honest_no(self):
        planner = _planner(task_list=TaskList())
        self.assertFalse(planner._dispatch_tool_call("end_goal", {}, {}))

    def test_set_goal_without_a_text_is_refused(self):
        planner = _planner(task_list=TaskList())
        self.assertFalse(planner._dispatch_tool_call("set_goal", {"text": "  "}, {}))
        self.assertEqual(planner.task_list.items(), [])

    def test_set_goal_refreshes_an_existing_active_goal(self):
        tasks = TaskList()
        planner = _planner(task_list=tasks)
        planner._dispatch_tool_call("set_goal", {"text": "旧目标"}, {})
        planner._dispatch_tool_call("set_goal", {"text": "新目标"}, {})

        items = tasks.items()
        self.assertEqual(len(items), 1, "同一条活跃目标不重复入队")
        self.assertEqual(items[0].detail, "新目标")


# ── 接线：planner 派发长任务登记 job ─────────────────────────────

class JobDispatchWiringTests(unittest.TestCase):
    def test_job_tool_dispatch_registers_a_job_with_all_four_requirements(self):
        action_manager = ActionManager()
        registry = JobRegistry()
        planner = _planner(skills=TrackingSkills(action_manager),
                           action_manager=action_manager, job_registry=registry)

        self.assertTrue(planner._dispatch_tool_call("move_to", {"x": 1, "y": 2, "z": 3}, {}))

        jobs = registry.jobs()
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].tool, "move_to")
        self.assertEqual(jobs[0].status, JOB_RUNNING)
        as_dict = jobs[0].as_dict()
        self.assertTrue(as_dict["progress_source"])
        self.assertTrue(as_dict["terminal_condition"])
        self.assertTrue(as_dict["failure_code"])
        self.assertTrue(as_dict["has_cancel_path"])

    def test_cancelling_a_dispatched_job_runs_the_real_cancel_path(self):
        action_manager = ActionManager()
        registry = JobRegistry()
        planner = _planner(skills=TrackingSkills(action_manager),
                           action_manager=action_manager, job_registry=registry)
        planner._dispatch_tool_call("move_to", {"x": 1, "y": 2, "z": 3}, {})
        job = registry.jobs()[0]
        self.assertTrue(action_manager.is_busy)

        ok, _why = registry.cancel(job.id, reason="player stop")

        self.assertTrue(ok)
        self.assertFalse(action_manager.is_busy, "取消路径必须真的停下在途动作")

    def test_non_job_tool_dispatch_registers_no_job(self):
        action_manager = ActionManager()
        registry = JobRegistry()
        planner = _planner(action_manager=action_manager, job_registry=registry)

        planner._dispatch_tool_call("get_inventory", {}, {})

        self.assertEqual(registry.jobs(), [])

    def test_no_job_registry_keeps_dispatch_unchanged(self):
        planner = _planner()
        self.assertTrue(planner._dispatch_tool_call("move_to", {"x": 1, "y": 2, "z": 3}, {}))


# ── 配置键 ──────────────────────────────────────────────────────

class ConfigKeyTests(unittest.TestCase):
    def test_default_is_on(self):
        self.assertIs(_default_config()["job_orchestration"], True)
        self.assertTrue(DEFAULT_ENABLED)

    def test_store_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ConfigStore(Path(tmp) / "config.json")
            self.assertTrue(store.get_job_orchestration())

            store.set_app_config({"job_orchestration": False})
            self.assertFalse(store.get_job_orchestration())

            store.set_app_config({"job_orchestration": True})
            self.assertTrue(store.get_job_orchestration())


if __name__ == "__main__":
    unittest.main()
