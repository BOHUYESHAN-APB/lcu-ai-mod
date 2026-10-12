"""慢模型（国外服务商）不许被误判成"卡住"：超时可配、等待可见、失败不刷屏。

用户口径（原话）："模型**超时**还是有可能的，所以说得等有些时候不得不等后台，不得不等**大**
模型，因为现在我配的服务商是国外的，所以说肯定第一，一方面有网络延迟，另外一方面每轮的
**首字延迟**肯定也高。"

实机现象：`llm_service` 报 `ReadTimeout` → `session` 每轮刷一条"语义门调用失败" WARNING；
同时"在等模型"的那段时间里，`state_governor` 会按"位置未变"把在途任务当卡死掐掉 ——
等于**自己把正在做的事打断**。

本文件锁定四件事：
  (1) 读模型期间 `state_governor` 的**进展判定暂停**（超过 `no_progress_seconds` 也不取消），
      调用结束后按实际等待时长补平、恢复判定；这期间若真取消，理由必须如实写"在等模型"，
      绝不写"位置未变"；
  (2) 语义门 / 规划 / decider 的**读超时可配**（`config.json → llm_timeouts`），
      语义门默认放宽到 15s（扛国外首字延迟）；
  (3) 语义门失败/超时仍是 fail-open（continue），但 WARNING **只打一次**，恢复时打一行；
  (4) 等待不静默：调用前打"在等模型（语义门）…"；"模型在飞"在 LLMService 层统一通知。
"""
import logging
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent.config_store import LLM_TIMEOUT_DEFAULTS, ConfigStore
from agent.llm_service import DEFAULT_REQUEST_TIMEOUT, LLMService
from agent.session import Session
from agent.state_governor import GovernorConfig, StateGovernor


# ── 共用小工具 ────────────────────────────────────────────────

class FakeClock:
    def __init__(self, now: float = 1000.0):
        self.now = float(now)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += float(seconds)


def _collect_state(x: float = 0.0, status: str = "collecting"):
    return {"player": {"x": x, "y": 64.0, "z": 0.0},
            "task_state": {"kind": "collect", "status": status, "detail": "1/8"}}


class _FakeLLM:
    """最小 LLM 替身：记录 kwargs，可选地每次都超时失败。"""

    def __init__(self, fail: bool = False):
        self.api_key = "test-key"
        self.calls: list[dict] = []
        self.fail = fail

    def chat(self, messages, **kwargs):
        self.calls.append(kwargs)
        if self.fail:
            raise TimeoutError("The read operation timed out")
        return {"content": '{"decision": "continue"}'}


def _session(llm) -> Session:
    session = Session.__new__(Session)
    session.llm = llm
    session.runtime = {"player": {"name": "AI"}}
    session.persona = None
    session._semantic_gate_failed = False
    session._semantic_gate_enabled = lambda: True      # type: ignore[method-assign]
    session._conversation_recent_lines = lambda n=6: ""  # type: ignore[method-assign]
    return session


def _store(case: unittest.TestCase, **cfg) -> ConfigStore:
    tmp = tempfile.TemporaryDirectory()
    case.addCleanup(tmp.cleanup)
    store = ConfigStore(Path(tmp.name) / "config.json")
    if cfg:
        store.set_app_config(cfg)
    return store


# ── (1) 读模型期间暂停进展判定 ────────────────────────────────

class GovernorModelWaitTests(unittest.TestCase):

    def _governor(self, clock: FakeClock, **cfg) -> StateGovernor:
        params = {"no_progress_seconds": 5.0, "retry_cooldown_seconds": 10.0}
        params.update(cfg)
        return StateGovernor(clock=clock, config=GovernorConfig(**params))

    def test_inflight_call_pauses_no_progress_and_resumes_after(self):
        clock = FakeClock()
        gov = self._governor(clock)
        gov.register("action:collect_blocks", kind="collect", target="collect_blocks")
        state = _collect_state(x=0.0)
        self.assertEqual(gov.tick(state), [], "baseline tick")

        gov.begin_model_call("planner")
        clock.advance(120.0)          # 用户在等大模型：网络 + 首字延迟都高
        self.assertEqual(gov.tick(state), [],
                         "MUST NOT cancel while a model call is in flight")
        self.assertTrue(gov.snapshot()["model_in_flight"])
        self.assertEqual(gov.snapshot()["model_waiting_for"], "planner")

        gov.end_model_call()          # 模型回来了：等待时长被补平
        self.assertFalse(gov.snapshot()["model_in_flight"])
        clock.advance(3.0)
        self.assertEqual(gov.tick(state), [], "fresh window: 3s < 5s")
        clock.advance(3.0)
        out = gov.tick(state)
        self.assertEqual(len(out), 1, "judgement resumes once the model is back")
        self.assertIn("无进展", out[0]["reason"])

    def test_wait_is_compensated_even_without_any_tick_in_between(self):
        """等待期间主循环一拍都没跑（就是被这次调用挡住了）也一样不许误判。"""
        clock = FakeClock()
        gov = self._governor(clock)
        gov.register("action:eat", kind="collect", target="eat")
        gov.tick(_collect_state())

        gov.begin_model_call("gate")
        clock.advance(90.0)
        gov.end_model_call()

        clock.advance(4.0)
        self.assertEqual(gov.tick(_collect_state()), [], "waited time must not count")
        clock.advance(2.0)
        self.assertEqual(len(gov.tick(_collect_state())), 1)

    def test_repeated_waits_do_not_accumulate(self):
        clock = FakeClock()
        gov = self._governor(clock)
        gov.register("action:collect_blocks", kind="collect", target="collect_blocks")
        gov.tick(_collect_state())
        for _ in range(5):
            gov.begin_model_call("planner")
            clock.advance(30.0)
            gov.end_model_call()
            clock.advance(0.5)
        self.assertEqual(gov.tick(_collect_state()), [],
                         "5 x (30s wait + 0.5s idle) must still be under the window")

    def test_cancel_during_the_wait_says_waiting_for_the_model(self):
        clock = FakeClock()
        gov = self._governor(clock)
        gov.register("action:collect_blocks", kind="collect", target="collect_blocks")
        gov.begin_model_call("planner")
        failed = {"player": {"x": 0, "y": 64, "z": 0},
                  "task_state": {"kind": "collect", "status": "failed", "reason": "cannot dig"}}

        out = gov.tick(failed)

        self.assertEqual(len(out), 1, "an explicit upstream failure still cancels")
        reason = out[0]["reason"]
        self.assertIn("在等模型", reason)
        self.assertIn("planner", reason)
        self.assertNotIn("无进展", reason, "must not claim 'no progress' while waiting")
        self.assertNotIn("位置", reason, "must not claim 'position unchanged' while waiting")

    def test_governor_freezes_only_while_a_call_is_in_flight(self):
        clock = FakeClock()
        gov = self._governor(clock)
        gov.begin_model_call("planner")
        gov.begin_model_call("gate")
        gov.end_model_call()
        self.assertTrue(gov.model_in_flight(), "nested calls keep the pause")
        self.assertIn("planner", gov.model_wait_label())
        gov.end_model_call()
        self.assertFalse(gov.model_in_flight())
        gov.end_model_call()               # 多余的收尾不许把计数打负
        self.assertFalse(gov.model_in_flight())

    def test_llm_service_inflight_notice_reaches_the_governor(self):
        clock = FakeClock()
        gov = self._governor(clock)
        gov.register("action:collect_blocks", kind="collect", target="collect_blocks")
        gov.tick(_collect_state())
        service = LLMService()
        self.addCleanup(service.close)
        service.set_inflight_callbacks(gov.begin_model_call, gov.end_model_call)
        service._client = mock.Mock()

        def _slow_post(*args, **kwargs):
            clock.advance(60.0)            # 请求本身很慢（超时/首字延迟）
            raise TimeoutError("The read operation timed out")

        service._client.post.side_effect = _slow_post
        with self.assertRaises(TimeoutError):
            service._post("chat/completions", {"model": "m"}, agent="planner",
                          config={"base_url": "http://example.invalid/v1", "api_key": "k"})

        self.assertFalse(gov.snapshot()["model_in_flight"], "the in-flight notice must be closed")
        self.assertEqual(gov.tick(_collect_state()), [],
                         "a 60s slow call must not count as 'no progress'")


# ── (2) 读超时可配 ────────────────────────────────────────────

class LLMTimeoutConfigTests(unittest.TestCase):

    def test_defaults_tolerate_a_foreign_provider(self):
        store = _store(self)
        cfg = store.get_llm_timeouts()
        self.assertEqual(cfg["semantic_gate"], 15.0, "semantic gate default must survive slow first token")
        self.assertEqual(cfg["planner"], 120.0)
        self.assertEqual(cfg["decider"], 20.0, "the old hard-coded 8s decider timeout was too tight")
        self.assertEqual(cfg, {k: float(v) for k, v in LLM_TIMEOUT_DEFAULTS.items()})

    def test_round_trip_clamps_and_persists(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "config.json"
        store = ConfigStore(path)

        store.set_app_config({"llm_timeouts": {"semantic_gate": 20, "planner": 300,
                                              "decider": -1, "junk": 5}})
        cfg = store.get_llm_timeouts()
        self.assertEqual(cfg["semantic_gate"], 20.0)
        self.assertEqual(cfg["planner"], 300.0)
        self.assertEqual(cfg["decider"], 20.0, "non-positive -> default")
        self.assertNotIn("junk", cfg, "unknown roles are ignored")
        self.assertEqual(ConfigStore(path).get_llm_timeouts()["semantic_gate"], 20.0,
                         "must be persisted")

        store.set_app_config({"llm_timeouts": {"planner": "nonsense"}})
        self.assertEqual(store.get_llm_timeouts()["planner"], 300.0, "unreadable -> keep current")


class LLMServiceRoleTimeoutTests(unittest.TestCase):

    def test_resolution_order_provider_fixed_table_call_site_default(self):
        service = LLMService()
        self.addCleanup(service.close)
        self.assertEqual(service.role_timeout("planner"), DEFAULT_REQUEST_TIMEOUT)

        service.set_role_timeout("planner", 45)
        self.assertEqual(service.role_timeout("planner"), 45.0)

        service.set_timeout_provider(lambda: {"planner": 90, "semantic_gate": 12})
        self.assertEqual(service.role_timeout("planner"), 90.0,
                         "the config provider wins over the fixed table")
        self.assertEqual(service.role_timeout("gate"), 12.0,
                         "the gate role reads the `semantic_gate` config key")

        service.set_timeout_provider(lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        self.assertEqual(service.role_timeout("planner"), 45.0,
                         "a broken provider falls back to the fixed table")

    def test_call_site_timeout_is_used_when_the_role_has_no_config(self):
        service = LLMService()
        self.addCleanup(service.close)
        self.assertEqual(service.role_timeout("vision", 33), 33.0)
        self.assertEqual(service.role_timeout("vision"), DEFAULT_REQUEST_TIMEOUT)
        self.assertEqual(service.role_timeout("vision", -1), DEFAULT_REQUEST_TIMEOUT,
                         "a non-positive call-site value is ignored")

    def test_request_carries_the_role_timeout_and_closes_the_inflight_notice(self):
        service = LLMService()
        self.addCleanup(service.close)
        service.set_timeout_provider(lambda: {"planner": 77})
        events: list[tuple] = []
        service.set_inflight_callbacks(lambda role: events.append(("begin", role)),
                                       lambda: events.append(("end",)))
        service._client = mock.Mock()
        service._client.post.side_effect = RuntimeError("read timed out")

        with self.assertRaises(RuntimeError):
            service._post("chat/completions", {"model": "m"}, agent="planner",
                          config={"base_url": "http://example.invalid/v1", "api_key": "k"})

        self.assertEqual(service._client.post.call_args.kwargs["timeout"], 77.0,
                         "the configured role timeout must reach the HTTP client")
        self.assertEqual(events, [("begin", "planner"), ("end",)],
                         "the in-flight notice must have both ends")

    def test_decider_call_site_value_yields_to_the_configured_role_timeout(self):
        """`planner.py` 里写死的 decider 8s 必须能被配置顶掉（用户口径：等得起多久由配置算）。"""
        service = LLMService()
        self.addCleanup(service.close)
        service.set_timeout_provider(lambda: {"decider": 25})
        service._client = mock.Mock()
        service._client.post.side_effect = RuntimeError("boom")

        with self.assertRaises(RuntimeError):
            service._post("chat/completions", {"model": "m"}, agent="decider", timeout=8.0,
                          config={"base_url": "http://example.invalid/v1", "api_key": "k"})

        self.assertEqual(service._client.post.call_args.kwargs["timeout"], 25.0)


# ── (3)(4) 语义门：超时可配、失败不刷屏、等待可见 ─────────────

class SemanticGateSlowModelTests(unittest.TestCase):

    def test_gate_timeout_comes_from_config(self):
        llm = _FakeLLM()
        session = _session(llm)
        store = _store(self, llm_timeouts={"semantic_gate": 18.0})
        with mock.patch("agent.session.get_config_store", return_value=store):
            session._semantic_gate("Owner", "在吗")

        self.assertEqual(llm.calls[0]["timeout"], 18.0)

    def test_gate_timeout_falls_back_when_config_is_unreadable(self):
        llm = _FakeLLM()
        session = _session(llm)
        with mock.patch("agent.session.get_config_store", side_effect=RuntimeError("no store")):
            session._semantic_gate("Owner", "在吗")

        self.assertEqual(llm.calls[0]["timeout"], Session.SEMANTIC_GATE_TIMEOUT)
        self.assertEqual(Session.SEMANTIC_GATE_TIMEOUT, 15.0,
                         "fallback default must be foreign-provider friendly")

    def test_gate_failure_is_fail_open_and_warned_only_once(self):
        llm = _FakeLLM(fail=True)
        session = _session(llm)

        with self.assertLogs("session", level="WARNING") as logs:
            for _ in range(3):
                self.assertEqual(session._semantic_gate("Owner", "在吗"), "continue")
        warnings = [r for r in logs.records if r.levelno >= logging.WARNING]
        self.assertEqual(len(warnings), 1, "the same failure must not spam every round")

    def test_gate_recovery_is_logged_and_resets_the_warning_latch(self):
        llm = _FakeLLM(fail=True)
        session = _session(llm)
        session._semantic_gate("Owner", "在吗")           # 第一次失败 → 打一次 WARNING

        llm.fail = False
        with self.assertLogs("session", level="INFO") as logs:
            self.assertEqual(session._semantic_gate("Owner", "在吗"), "continue")
        self.assertTrue(any("恢复" in r.getMessage() for r in logs.records),
                        "a successful call after failures must be reported")

        llm.fail = True
        with self.assertLogs("session", level="WARNING") as logs:
            session._semantic_gate("Owner", "在吗")
        self.assertEqual(len([r for r in logs.records if r.levelno >= logging.WARNING]), 1,
                         "after recovery the next failure warns again")

    def test_waiting_for_the_gate_is_never_silent(self):
        llm = _FakeLLM()
        session = _session(llm)

        with self.assertLogs("session", level="INFO") as logs:
            session._semantic_gate("Owner", "在吗")

        self.assertTrue(any("在等模型" in r.getMessage() for r in logs.records),
                        "long waits must say why nothing else is happening")

    def test_role_timeout_map_uses_the_semantic_gate_key(self):
        session = _session(_FakeLLM())
        store = _store(self, llm_timeouts={"semantic_gate": 21, "planner": 130, "decider": 31})

        with mock.patch("agent.session.get_config_store", return_value=store):
            mapping = session._llm_role_timeouts()

        self.assertEqual(mapping, {"gate": 21.0, "planner": 130.0, "decider": 31.0})

    def test_dead_llm_keeps_failing_open_without_calling_the_model(self):
        session = _session(_FakeLLM())
        session.llm.api_key = None
        self.assertEqual(session._semantic_gate("Owner", "在吗"), "continue")
        self.assertEqual(session.llm.calls, [], "no key -> no call, fail open")


if __name__ == "__main__":
    unittest.main()
