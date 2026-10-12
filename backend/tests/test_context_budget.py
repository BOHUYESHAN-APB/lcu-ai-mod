"""上下文预算 / 主动压缩的测试（`agent/context_budget.py` + session/conversation 接线）。

分三块：
1. **估算**：只断言量级/单调/自洽关系（CJK 明显比同长度 ASCII 贵；空串 = 0），不锁精确数字；
2. **阈值与迟滞**：过线才压、压完不许每轮都压、掉下去才重新武装、`reserve_output` 真的进预算；
3. **事件与接线**：真的压缩时**确实**调了既有摘要（假 LLM 记账），事件里"估算"和"实际"都在；
   低于触发线时**没有**任何模型调用（反例）。
"""

import contextlib
import logging
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from agent import context_budget as cb
from agent.config_store import ConfigStore
from agent.context_budget import Budget, describe_messages, estimate_messages, estimate_tokens
from agent.conversation import ConversationStore
from agent.session import Session

SUMMARY_JSON = '{"summary": "他们把钻石分了", "keywords": ["钻石", "分赃"]}'


class _FakeLLM:
    """假 LLM：记下每次调用，用来断言"到底有没有真的去摘要"。"""

    def __init__(self, reply: str = SUMMARY_JSON):
        self.calls: list[dict] = []
        self.reply = reply
        self.total_prompt_tokens = 0
        self.request_count = 0

    def chat(self, messages, **kwargs):
        self.calls.append({"messages": messages, "kwargs": kwargs})
        return {"content": self.reply}

    def get_usage(self):
        return {
            "total_prompt_tokens": self.total_prompt_tokens,
            "total_completion_tokens": 0,
            "total_tokens": self.total_prompt_tokens,
            "request_count": self.request_count,
        }


def _record_many(store: ConversationStore, count: int, text: str = "挖矿挖到了钻石" * 20,
                 sender: str = "张三") -> None:
    for index in range(count):
        store.record_chat(sender, f"{text}#{index}")


def _bare_session(store: ConversationStore, llm: _FakeLLM) -> Session:
    """只拼出 `_compress_context_if_needed` / `handle_chat` 需要的那些属性。"""
    session = Session.__new__(Session)
    session.id = "t"
    session.runtime = {}
    session.in_world = True
    session.conversation = store
    session.llm = llm
    session.memory = SimpleNamespace(add_interaction=lambda **kwargs: None)
    session.skills = SimpleNamespace(command_context=lambda _name: contextlib.nullcontext())
    session.planner = SimpleNamespace(plan_and_execute=lambda *a, **k: "嗯")
    session._runtime_context = lambda: {"conversation": store.describe(1)}
    session.persona_blocks = lambda scene=None: {}
    session._scene_for = lambda sender="", mentioned=False: "chat"
    session._is_mentioned = lambda message: False
    session._get_bot_name = lambda: "AI"
    return session


def _budget(**overrides) -> Budget:
    """窗口 1000、无输出预留：触发线 700 token、迟滞下限 600 token（好算）。"""
    config = {"context_window": 1000, "reserve_output": 0, "compress_at": 0.70,
              "hysteresis": 0.10, "keep_recent": 2}
    config.update(overrides)
    return Budget.from_config(config)


# ── 1. 估算 ───────────────────────────────────────────────────

class EstimateTests(unittest.TestCase):
    def test_empty_and_whitespace_are_zero(self):
        self.assertEqual(estimate_tokens(""), 0)
        self.assertEqual(estimate_tokens(None), 0)
        self.assertEqual(estimate_tokens("   \n\t  "), 0)
        self.assertEqual(estimate_tokens("　　"), 0)  # 全角空格也是空白

    def test_cjk_is_much_more_expensive_than_ascii_of_same_length(self):
        cjk = "中文接口文档说明测试样例内容"
        ascii_text = "a" * len(cjk)
        self.assertGreater(estimate_tokens(cjk), estimate_tokens(ascii_text) * 2)
        # 数量级：CJK ≈ 1 token/字，不加精确断言，只要求"至少一字一 token 的一半"
        self.assertGreaterEqual(estimate_tokens(cjk), len(cjk) // 2)

    def test_monotonic_under_appending(self):
        base = "挖矿挖到了钻石"
        previous = 0
        for length in range(1, len(base) + 1):
            current = estimate_tokens(base[:length])
            self.assertGreaterEqual(current, previous)
            previous = current
        # ASCII 侧同样单调
        previous = 0
        for length in range(1, 40):
            current = estimate_tokens("x" * length)
            self.assertGreaterEqual(current, previous)
            previous = current

    def test_mixed_text_is_between_the_two_pure_cases(self):
        mixed = "你好" + "abc"
        self.assertGreaterEqual(estimate_tokens(mixed), estimate_tokens("你好"))
        self.assertGreaterEqual(estimate_tokens(mixed), estimate_tokens("abc"))

    def test_estimate_messages_is_the_sum_of_its_parts(self):
        messages = {
            "summary": "他们挖了钻石",
            "keywords": ["钻石", "mine"],
            "recent": [{"type": "chat", "sender": "张三", "message": "一起去挖矿吧"},
                       {"type": "death", "sender": "李四", "message": "李四 被僵尸杀死了",
                        "cause": "僵尸"}],
        }
        described = describe_messages(messages)
        self.assertEqual(sum(item["tokens"] for item in described["items"]),
                         described["estimated"])
        self.assertEqual(estimate_messages(messages), described["estimated"])
        self.assertEqual(estimate_messages([]), 0)
        self.assertEqual(estimate_messages({"summary": "", "keywords": [], "recent": []}), 0)

    def test_heaviest_points_at_the_most_expensive_item(self):
        described = describe_messages({
            "recent": [{"type": "chat", "sender": "张三", "message": "短"},
                       {"type": "chat", "sender": "李四", "message": "这是一条非常非常长的消息" * 3}],
        })
        self.assertIsNotNone(described["heaviest"])
        self.assertEqual(described["heaviest"]["index"], 1)
        self.assertIn("李四", described["heaviest"]["label"])
        self.assertGreater(described["heaviest"]["share"], 0.5)


# ── 2. 阈值 / 迟滞 / 预算 ──────────────────────────────────────

class BudgetTests(unittest.TestCase):
    def test_available_subtracts_the_output_reserve(self):
        rules = Budget(context_window=1000, reserve_output=250)
        self.assertEqual(rules.available(), 750)
        # 预留比窗口还大 → 夹到 0，而不是负数（available() 绝不返回负值）
        weird = Budget.from_config({"context_window": 100, "reserve_output": 9999})
        self.assertEqual(weird.available(), 0)
        self.assertEqual(weird.reserve_output, 100)

    def test_below_threshold_does_not_compress(self):
        rules = _budget()
        decision = cb.plan(699, rules)
        self.assertFalse(decision["compress"])
        self.assertEqual(decision["next_marker"], None)

    def test_above_threshold_compresses(self):
        rules = _budget()
        decision = cb.plan(700, rules)
        self.assertTrue(decision["compress"])
        self.assertAlmostEqual(decision["next_marker"], 0.70)

    def test_reserve_output_shrinks_the_budget_and_can_block_everything(self):
        rules = Budget.from_config({"context_window": 1000, "reserve_output": 900,
                                    "compress_at": 0.70, "keep_recent": 2})
        self.assertEqual(rules.available(), 100)
        self.assertEqual(rules.high_line(), 70)          # 触发线按**可用输入**算，不是按窗口
        self.assertFalse(cb.plan(69, rules)["compress"])
        self.assertTrue(cb.plan(70, rules)["compress"])  # reserve_output 真的参与了判断
        # 可用为 0（预留 ≥ 窗口）这种自相矛盾配置：永不触发，并如实说明原因
        blocked = Budget.from_config({"context_window": 100, "reserve_output": 100})
        self.assertEqual(blocked.available(), 0)
        self.assertFalse(cb.plan(999999, blocked)["compress"])
        self.assertIn("自相矛盾", cb.plan(999999, blocked)["reason"])

    def test_hysteresis_blocks_a_second_compression_until_usage_drops(self):
        rules = _budget()
        first = cb.plan(900, rules)
        self.assertTrue(first["compress"])
        marker = first["next_marker"]
        self.assertIsNotNone(marker)

        # 压完立刻再判（占用还高）⇒ **不压**（否则就是"每轮都压"）
        again = cb.plan(900, rules, already_compressed_at=marker)
        self.assertFalse(again["compress"])
        self.assertEqual(again["next_marker"], marker)
        self.assertIn("迟滞", again["reason"])
        # 纯函数口径也要一致
        self.assertFalse(rules.should_compress(900, already_compressed_at=marker))

        # 掉到迟滞下限（600）以下 ⇒ 重新武装
        dropped = cb.plan(500, rules, already_compressed_at=marker)
        self.assertFalse(dropped["compress"])
        self.assertIsNone(dropped["next_marker"])

        # 重新武装后再涨回触发线以上 ⇒ 又能压
        self.assertTrue(cb.plan(900, rules, already_compressed_at=dropped["next_marker"])["compress"])

    def test_invalid_or_low_marker_does_not_block(self):
        rules = _budget()
        # 低于触发线的标记不可能来自一次真的过线压缩 → 当没有，别拿它挡路
        self.assertTrue(rules.should_compress(900, already_compressed_at=0.10))
        self.assertTrue(rules.should_compress(900, already_compressed_at="坏值"))

    def test_disabled_budget_never_compresses(self):
        rules = _budget(enabled=False)
        decision = cb.plan(10 ** 6, rules)
        self.assertFalse(decision["compress"])
        self.assertIn("已关闭", decision["reason"])

    def test_plan_reason_carries_estimate_window_and_threshold(self):
        rules = _budget()
        for estimate in (10, 800):
            reason = cb.plan(estimate, rules)["reason"]
            self.assertIn(f"{estimate:,}", reason)
            self.assertIn(f"{rules.context_window:,}", reason)
            self.assertIn(f"{rules.compress_at:.2f}", reason)

    def test_from_config_clamps_bad_values(self):
        rules = Budget.from_config({"context_window": "没写", "compress_at": 5,
                                    "hysteresis": 9, "keep_recent": -3, "reserve_output": -1})
        self.assertEqual(rules.context_window, 128000)  # 解析不出 → 默认假设值
        self.assertEqual(rules.compress_at, 1.0)
        self.assertEqual(rules.hysteresis, 1.0)
        self.assertEqual(rules.keep_recent, 0)
        self.assertEqual(rules.reserve_output, 0)


# ── 3. 配置 / 事件 / 接线 ─────────────────────────────────────

class CompressionTests(unittest.TestCase):
    def test_compact_window_reuses_the_existing_summarizer(self):
        with tempfile.TemporaryDirectory() as tmp:
            llm = _FakeLLM()
            store = ConversationStore(root=tmp, llm=llm)
            store.use_namespace("srv", "world")
            _record_many(store, 10)

            result = store.compact_window(keep_recent=4)
            self.assertTrue(result["summarized"])
            self.assertEqual(result["removed"], 6)
            self.assertEqual(result["kept"], 4)
            self.assertEqual(len(store.recent()), 4)
            self.assertEqual(store.pending_summary(), [])
            # **确实**走的是既有摘要（agent=summarizer），不是另写一套
            self.assertEqual(len(llm.calls), 1)
            self.assertEqual(llm.calls[0]["kwargs"].get("agent"), "summarizer")
            self.assertEqual(store.latest_summary()["summary"], "他们把钻石分了")

    def test_compact_window_with_nothing_to_move_does_not_call_the_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            llm = _FakeLLM()
            store = ConversationStore(root=tmp, llm=llm)
            store.use_namespace("srv", "world")
            _record_many(store, 2)

            result = store.compact_window(keep_recent=6)
            self.assertEqual(result["removed"], 0)
            self.assertFalse(result["summarized"])
            self.assertEqual(llm.calls, [])

    def test_failed_summary_degrades_visibly_and_keeps_pending(self):
        with tempfile.TemporaryDirectory() as tmp:
            llm = _FakeLLM(reply="这不是 JSON")
            store = ConversationStore(root=tmp, llm=llm)
            store.use_namespace("srv", "world")
            _record_many(store, 8)

            result = store.compact_window(keep_recent=2)
            self.assertFalse(result["summarized"])       # 如实降级
            self.assertEqual(result["removed"], 6)
            self.assertEqual(len(store.recent()), 2)
            self.assertEqual(len(store.pending_summary()), 6)  # pending 保留等下次

    def test_compression_event_has_estimate_and_actual_slots(self):
        event = cb.format_compression_event(
            before=12480, after=3100, budget=Budget(), removed=8, kept=6,
            summarized=True, heaviest={"label": "[chat] 张三", "tokens": 620},
            actual_prompt=11900, agent="planner")
        self.assertIn("估算", event)
        self.assertIn("实际", event)
        self.assertIn("12,480", event)
        self.assertIn("3,100", event)
        self.assertIn(f"{Budget().context_window:,}", event)
        self.assertIn("0.70", event)
        self.assertIn("未经该模型证实", event)
        # 拿不到 usage 时如实说未知，不拿估算冒充
        unknown = cb.format_compression_event(
            before=1000, after=500, budget=Budget(), removed=3, kept=6, summarized=False,
            actual_prompt=None)
        self.assertIn("实际用量：未知", unknown)
        self.assertIn("降级", unknown)

    def test_estimate_vs_actual_line_labels_both_sources(self):
        line = cb.format_estimate_vs_actual(4200, 3980)
        self.assertIn("估算", line)
        self.assertIn("实际", line)
        unknown = cb.format_estimate_vs_actual(4200, None, requests=2)
        self.assertIn("实际未知", unknown)


class ConfigTests(unittest.TestCase):
    def test_defaults_are_an_assumption_and_are_configurable(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ConfigStore(f"{tmp}/config.json")
            defaults = store.get_context_budget_config("planner")
            self.assertEqual(defaults["context_window"], 128000)
            self.assertEqual(defaults["reserve_output"], 16384)
            self.assertEqual(defaults["compress_at"], 0.70)
            self.assertEqual(defaults["keep_recent"], 6)
            self.assertTrue(defaults["enabled"])

            store.set_app_config({"context_budget": {"context_window": 32000,
                                                     "compress_at": 0.5,
                                                     "keep_recent": 8}})
            changed = store.get_context_budget_config("planner")
            self.assertEqual(changed["context_window"], 32000)
            self.assertEqual(changed["compress_at"], 0.5)
            self.assertEqual(changed["keep_recent"], 8)

    def test_per_model_window_override_wins_and_default_agent_is_the_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ConfigStore(f"{tmp}/config.json")
            # 单模型模式（默认）：所有角色一个模型 → 窗口只留一份（default 角色上的覆盖）
            store.set_agent_llm_config("default", {"context_window": 200000})
            self.assertEqual(store.get_context_budget_config("planner")["context_window"], 200000)

            # 关掉单模型 → 角色可以各自覆盖窗口
            store.set_agent_llm_config("default", {"single_model": False})
            store.set_agent_llm_config("planner", {"context_window": 64000})
            self.assertEqual(store.get_context_budget_config("planner")["context_window"], 64000)
            self.assertEqual(store.get_context_budget_config("decider")["context_window"], 200000)

    def test_single_model_mode_drops_per_role_windows(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ConfigStore(f"{tmp}/config.json")
            store.set_agent_llm_config("default", {"single_model": False})
            store.set_agent_llm_config("planner", {"context_window": 64000})
            store.set_agent_llm_config("default", {"single_model": True})
            self.assertEqual(store.get_context_budget_config("planner")["context_window"], 128000)


class SessionIntegrationTests(unittest.TestCase):
    """session 里的接线：调模型**之前**判预算；要压就压；不压就一个模型调用都没有。"""

    def _patch_config(self, config: dict):
        stub = SimpleNamespace(get_context_budget_config=lambda agent=None: dict(config))
        return unittest.mock.patch("agent.session.get_config_store", lambda *a, **k: stub)

    def test_over_threshold_compresses_and_logs_an_event(self):
        with tempfile.TemporaryDirectory() as tmp:
            llm = _FakeLLM()
            store = ConversationStore(root=tmp, llm=llm)
            store.use_namespace("srv", "world")
            _record_many(store, 10)
            session = _bare_session(store, llm)
            context = {"conversation": store.describe(1)}

            with self._patch_config(_budget().as_config()), \
                    self.assertLogs("session", level=logging.INFO) as captured:
                decision = session._compress_context_if_needed(context, "在吗")

            self.assertTrue(decision["compress"])
            self.assertEqual(len(llm.calls), 1)                       # 真的去摘要了
            self.assertEqual(llm.calls[0]["kwargs"].get("agent"), "summarizer")
            self.assertEqual(len(store.recent()), 2)                  # keep_recent=2
            self.assertEqual(len(context["conversation"]["recent"]), 2)  # 这一轮吃压缩后的
            event = session._last_context_compression["event"]
            self.assertEqual(session._last_context_compression["removed"], 8)
            self.assertTrue(any("估算" in line and "实际" in line for line in captured.output))
            self.assertIn("估算", event)
            self.assertIn("实际", event)

    def test_below_threshold_never_touches_the_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            llm = _FakeLLM()
            store = ConversationStore(root=tmp, llm=llm)
            store.use_namespace("srv", "world")
            _record_many(store, 2)
            session = _bare_session(store, llm)

            with self._patch_config(_budget().as_config()):
                decision = session._compress_context_if_needed(
                    {"conversation": store.describe(1)}, "在吗")

            self.assertFalse(decision["compress"])                    # 反例：低于触发线不压
            self.assertEqual(llm.calls, [])                           # 一个模型调用都没有
            self.assertIsNone(getattr(session, "_last_context_compression", None))

    def test_hysteresis_at_session_level(self):
        with tempfile.TemporaryDirectory() as tmp:
            llm = _FakeLLM()
            store = ConversationStore(root=tmp, llm=llm)
            store.use_namespace("srv", "world")
            _record_many(store, 10)
            session = _bare_session(store, llm)

            with self._patch_config(_budget().as_config()):
                session._compress_context_if_needed({"conversation": store.describe(1)}, "在吗")
                self.assertEqual(len(llm.calls), 1)
                # 立刻再判：占用还高（900 > 触发线 700，且没掉到迟滞下限 600 以下）
                # → 迟滞拦住，不许每轮都压
                second = session._compress_context_if_needed(
                    {"conversation": {"recent": [{"type": "chat", "sender": "张三",
                                                  "message": "矿" * 900}]}}, "在吗")
                self.assertFalse(second["compress"])
                self.assertIn("迟滞", second["reason"])
                self.assertEqual(len(llm.calls), 1)
                # 掉到迟滞下限以下 → 重新武装；窗口里再攒够可挪的旧内容 → 又能压
                session._compress_context_if_needed({"conversation": {"recent": []}}, "在吗")
                _record_many(store, 6)
                third = session._compress_context_if_needed({"conversation": store.describe(1)},
                                                            "在吗")
                self.assertTrue(third["compress"])
                self.assertEqual(len(llm.calls), 2)

    def test_over_line_with_nothing_to_move_does_not_pretend_to_compress(self):
        with tempfile.TemporaryDirectory() as tmp:
            llm = _FakeLLM()
            store = ConversationStore(root=tmp, llm=llm)
            store.use_namespace("srv", "world")
            _record_many(store, 2)        # 窗口里只有 2 条（== keep_recent）→ 没有可挪的
            session = _bare_session(store, llm)

            with self._patch_config(_budget().as_config()):
                decision = session._compress_context_if_needed(
                    {"conversation": {"recent": [{"type": "chat", "sender": "张三",
                                                  "message": "矿" * 900}]}}, "在吗")

            self.assertFalse(decision["compress"])     # 过线但没得压 → 如实说"没压"
            self.assertIn("没有可挪的旧内容", decision["reason"])
            self.assertEqual(llm.calls, [])            # 也不会去花一次摘要调用
            self.assertIsNone(session._context_compress_marker)  # 没压成就不进迟滞带

    def test_handle_chat_checks_the_budget_before_calling_the_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            llm = _FakeLLM()
            store = ConversationStore(root=tmp, llm=llm)
            store.use_namespace("srv", "world")
            _record_many(store, 10)
            session = _bare_session(store, llm)

            with self._patch_config(_budget().as_config()):
                response = session.handle_chat("张三", "在吗")

            self.assertEqual(response, "嗯")
            self.assertEqual(len(llm.calls), 1)          # 就是压缩那一次（planner 是假的）
            self.assertEqual(session._last_context_compression["agent"], "planner")

    def test_estimate_vs_actual_comparison_uses_api_usage(self):
        with tempfile.TemporaryDirectory() as tmp:
            llm = _FakeLLM()
            store = ConversationStore(root=tmp, llm=llm)
            store.use_namespace("srv", "world")
            session = _bare_session(store, llm)
            before = session._usage_snapshot(llm)

            llm.total_prompt_tokens += 3980   # 假装这一轮 API 回了 usage
            llm.request_count += 1
            with self.assertLogs("session", level=logging.INFO) as captured:
                session._note_actual_usage(before, 4200)

            line = session._last_usage_comparison["line"]
            self.assertEqual(session._last_usage_comparison["actual_prompt_tokens"], 3980)
            self.assertIn("估算", line)
            self.assertIn("实际", line)
            self.assertTrue(any("4,200" in entry and "3,980" in entry for entry in captured.output))

    def test_usage_status_says_the_window_is_unverified_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            llm = _FakeLLM()
            store = ConversationStore(root=tmp, llm=llm)
            session = _bare_session(store, llm)
            with self._patch_config(_budget().as_config()):
                status = session._context_budget_status()
            self.assertEqual(status["context_window"], 1000)
            self.assertIn("未经该模型证实", status["window_source"])
            self.assertIn("估算", status["note"])


if __name__ == "__main__":
    unittest.main()
