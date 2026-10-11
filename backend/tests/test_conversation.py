"""对话记忆（ConversationStore）：分区 / 三层 / 摘要 / 死亡进出判定的回归测试。

全部离线：LLM 用 FakeLLM 注入，临时目录用 TemporaryDirectory，不碰真实 data/。
"""

import json
import tempfile
import unittest
from pathlib import Path

from agent.conversation import (
    EVENT_TYPES,
    SUMMARY_AGENT,
    UNKNOWN_KEY,
    ConversationStore,
    classify_system_message,
    sanitize_key,
    server_key,
)


# ── 假件 ────────────────────────────────────────────────────────

class FakeLLM:
    """按 agent 返回固定 content；`error` 用来模拟调用炸掉。"""

    def __init__(self, response=None, error=None):
        self._response = response
        self._error = error
        self.calls = []

    def chat(self, messages, agent=None, **kwargs):
        self.calls.append({"messages": messages, "agent": agent, "kwargs": kwargs})
        if self._error is not None:
            raise self._error
        return {"content": self._response}


class _StoreCase(unittest.TestCase):
    """给每个用例一个独立临时 root。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def store(self, **kwargs):
        kwargs.setdefault("root", self.root)
        return ConversationStore(**kwargs)


# ── 1. 分区键 / 文件名安全化 ────────────────────────────────────

class ServerKeyTests(unittest.TestCase):
    def test_server_wins_over_world_name(self):
        self.assertEqual(server_key("1.2.3.4", "myworld"), "1.2.3.4")

    def test_falls_back_to_world_name_for_singleplayer(self):
        self.assertEqual(server_key("", "myworld"), "myworld")
        self.assertEqual(server_key(None, "myworld"), "myworld")

    def test_unknown_when_both_empty(self):
        self.assertEqual(server_key("", ""), UNKNOWN_KEY)
        self.assertEqual(server_key("   ", ""), UNKNOWN_KEY)
        self.assertEqual(server_key(None, None), UNKNOWN_KEY)

    def test_strips_whitespace(self):
        self.assertEqual(server_key("  1.2.3.4  ", "x"), "1.2.3.4")
        self.assertEqual(server_key("", "  myworld "), "myworld")


class SanitizeKeyTests(unittest.TestCase):
    def test_ip_port_becomes_filename_safe(self):
        self.assertEqual(sanitize_key("127.0.0.1:25565"), "127.0.0.1_25565")

    def test_collapses_and_trims_underscores(self):
        self.assertEqual(sanitize_key("a::/ b"), "a_b")
        self.assertEqual(sanitize_key("__a__b__"), "a_b")

    def test_empty_result_falls_back_to_unknown(self):
        self.assertEqual(sanitize_key(""), UNKNOWN_KEY)
        self.assertEqual(sanitize_key("   "), UNKNOWN_KEY)
        self.assertEqual(sanitize_key("///"), UNKNOWN_KEY)

    def test_keeps_dots_dashes_and_alnum(self):
        self.assertEqual(sanitize_key("mc.example.com-2_world"), "mc.example.com-2_world")


# ── 2. 死亡 / 进出判定 ──────────────────────────────────────────

class ClassifySystemMessageTests(unittest.TestCase):
    def _type(self, text):
        result = classify_system_message(text)
        return result["type"] if result else None

    def test_english_join_and_leave(self):
        self.assertEqual(classify_system_message("Steve joined the game")["player"], "Steve")
        self.assertEqual(classify_system_message("Steve joined the game")["type"], "join")
        self.assertEqual(classify_system_message("Alex left the game")["type"], "leave")

    def test_english_deaths_with_and_without_killer(self):
        slain = classify_system_message("Steve was slain by Zombie")
        self.assertEqual(slain["type"], "death")
        self.assertEqual(slain["player"], "Steve")
        self.assertEqual(slain["cause"], "Zombie")

        drowned = classify_system_message("Alex drowned")
        self.assertEqual(drowned["type"], "death")
        self.assertEqual(drowned["cause"], "")

        self.assertEqual(classify_system_message("Bob fell from a high place")["type"], "death")
        self.assertEqual(classify_system_message("Steve_99 was shot by Skeleton")["player"], "Steve_99")

    def test_chinese_join_and_leave(self):
        self.assertEqual(classify_system_message("史蒂夫 加入了游戏")["type"], "join")
        self.assertEqual(classify_system_message("Steve 离开了游戏")["type"], "leave")
        self.assertEqual(classify_system_message("Steve 退出了游戏")["type"], "leave")

    def test_chinese_deaths_with_and_without_killer(self):
        killed = classify_system_message("Steve 被僵尸杀死了")
        self.assertEqual(killed["type"], "death")
        self.assertEqual(killed["player"], "Steve")
        self.assertEqual(killed["cause"], "僵尸")

        self.assertEqual(classify_system_message("Steve 淹死了")["type"], "death")
        self.assertEqual(classify_system_message("Steve 摔死了")["type"], "death")

        named = classify_system_message("刀锋战士_9 被骷髅射死了")
        self.assertEqual(named["player"], "刀锋战士_9")
        self.assertEqual(named["cause"], "骷髅")

    def test_ordinary_text_is_not_misclassified(self):
        for text in (
            "Steve killed a zombie",
            "The server was restarted by admin",
            "Welcome to the server",
            "Steve 打死了僵尸",
            "有人在吗",
            "Steve 死了吗",
            "",
        ):
            self.assertIsNone(classify_system_message(text), text)

    def test_returned_dict_keeps_original_text(self):
        result = classify_system_message("  Steve joined the game  ")
        self.assertEqual(result["text"], "  Steve joined the game  ")


# ── 3. 分区隔离 / 回灌 ──────────────────────────────────────────

class NamespaceTests(_StoreCase):
    def test_files_are_named_per_namespace(self):
        store = self.store()
        store.use_namespace("127.0.0.1:25565", "")
        store.record_chat("A", "hi")
        self.assertTrue((self.root / "127.0.0.1_25565.jsonl").exists())

    def test_partitions_are_isolated(self):
        store = self.store()
        store.use_namespace("1.2.3.4", "")
        store.record_chat("A", "from-a")
        store.use_namespace("5.6.7.8", "")
        store.record_chat("B", "from-b")

        self.assertEqual([e["message"] for e in store.recent()], ["from-b"])
        self.assertTrue((self.root / "1.2.3.4.jsonl").exists())
        self.assertTrue((self.root / "5.6.7.8.jsonl").exists())

        store.use_namespace("1.2.3.4", "")
        self.assertEqual([e["message"] for e in store.recent()], ["from-a"])

    def test_singleplayer_uses_world_name(self):
        store = self.store()
        ns = store.use_namespace("", "MyWorld")
        self.assertEqual(ns, "MyWorld")
        store.record_chat("A", "hi")
        self.assertTrue((self.root / "MyWorld.jsonl").exists())

    def test_repeat_use_namespace_does_not_rehydrate(self):
        # 重复切到同一分区若重复回灌，会把运行时 pending 清掉 → 断言 pending 仍在。
        store = self.store(single_window=3, multi_window=3)
        store.use_namespace("srv", "")
        for i in range(5):
            store.record_chat("A", f"m{i}")
        self.assertEqual(len(store.pending_summary()), 2)

        store.use_namespace("srv", "")
        self.assertEqual(len(store.pending_summary()), 2)


class RestartRehydrateTests(_StoreCase):
    def test_new_store_reloads_recent_events_from_l1(self):
        store = self.store()
        store.use_namespace("srv", "")
        for i in range(4):
            store.record_chat("A", f"m{i}")

        # 全新进程：不读任何运行时状态文件，只从 L1 回灌。
        fresh = self.store()
        fresh.use_namespace("srv", "")
        self.assertEqual([e["message"] for e in fresh.recent()], ["m0", "m1", "m2", "m3"])

    def test_corrupt_line_is_skipped(self):
        (self.root / "srv.jsonl").write_text(
            '{"ts": 1, "type": "chat", "sender": "A", "message": "ok"}\n'
            "not-json-at-all\n"
            '{"ts": 2, "type": "chat", "sender": "B", "message": "ok2"}\n',
            encoding="utf-8",
        )
        store = self.store()
        store.use_namespace("srv", "")
        self.assertEqual([e["message"] for e in store.recent()], ["ok", "ok2"])


# ── 4. 记录 / 窗口 ──────────────────────────────────────────────

class RecordTests(_StoreCase):
    def test_record_normalizes_and_omits_absent_fields(self):
        store = self.store(clock=lambda: 42.0)
        store.use_namespace("srv", "")
        entry = store.record_chat("A", "hi")
        self.assertEqual(entry["ts"], 42.0)
        self.assertEqual(entry["type"], "chat")
        self.assertEqual(set(entry.keys()), {"ts", "type", "sender", "message"})

    def test_coordinates_and_distance_are_kept(self):
        store = self.store()
        store.use_namespace("srv", "")
        entry = store.record_chat("A", "hi", x=1, y=64, z=-3, distance=4.5)
        for key, value in (("x", 1), ("y", 64), ("z", -3), ("distance", 4.5)):
            self.assertEqual(entry[key], value)

    def test_explicit_ts_wins(self):
        store = self.store(clock=lambda: 1.0)
        store.use_namespace("srv", "")
        self.assertEqual(store.record_chat("A", "hi", ts=999.0)["ts"], 999.0)

    def test_system_chat_is_classified_into_death(self):
        store = self.store()
        store.use_namespace("srv", "")
        entry = store.record_chat("[系统]", "Steve was slain by Zombie", is_system=True)
        self.assertEqual(entry["type"], "death")
        self.assertEqual(entry["sender"], "Steve")
        self.assertEqual(entry["cause"], "Zombie")

    def test_unclassified_system_stays_system(self):
        store = self.store()
        store.use_namespace("srv", "")
        entry = store.record_chat("[系统]", "Server restarting soon", is_system=True)
        self.assertEqual(entry["type"], "system")

    def test_record_death_and_record_system(self):
        store = self.store()
        store.use_namespace("srv", "")
        death = store.record_death("Steve", "Zombie", "Steve was slain by Zombie")
        self.assertEqual(death["type"], "death")
        self.assertEqual(death["cause"], "Zombie")
        system = store.record_system("hello world")
        self.assertEqual(system["type"], "system")
        self.assertIn(system["type"], EVENT_TYPES)

    def test_jsonl_is_written_as_utf8_without_escapes(self):
        store = self.store()
        store.use_namespace("srv", "")
        store.record_chat("A", "你好")
        raw = (self.root / "srv.jsonl").read_text(encoding="utf-8")
        self.assertIn("你好", raw)
        self.assertNotIn("\\u4f60", raw)


class WindowLimitTests(_StoreCase):
    def test_default_limits(self):
        store = self.store()
        self.assertEqual(store.window_limit(1), 60)
        self.assertEqual(store.window_limit(2), 40)
        self.assertEqual(store.window_limit(3), 40)

    def test_single_window_caps_window_and_feeds_pending(self):
        store = self.store(single_window=3, multi_window=3)
        store.use_namespace("srv", "")
        for i in range(5):
            store.record_chat("A", f"m{i}")
        self.assertEqual([e["message"] for e in store.recent()], ["m2", "m3", "m4"])
        self.assertEqual(len(store.pending_summary()), 2)

    def test_multi_window_trims_when_asking_for_three_players(self):
        store = self.store()
        store.use_namespace("srv", "")
        for i in range(45):
            store.record_chat("A", str(i))
        self.assertEqual(len(store.recent(players_count=1)), 45)
        self.assertEqual(len(store.recent(players_count=3)), 40)
        self.assertEqual(store.recent(players_count=3)[0]["message"], "5")

    def test_recent_n(self):
        store = self.store()
        store.use_namespace("srv", "")
        for i in range(5):
            store.record_chat("A", str(i))
        self.assertEqual([e["message"] for e in store.recent(2)], ["3", "4"])


# ── 5. 摘要（L3）──────────────────────────────────────────────

class SummaryTests(_StoreCase):
    def _overfilled(self, **kwargs):
        store = self.store(single_window=3, multi_window=3, **kwargs)
        store.use_namespace("srv", "")
        for i in range(5):
            store.record_chat("A", f"m{i}")
        return store

    def test_summarize_pending_persists_and_clears(self):
        store = self._overfilled()
        self.assertEqual(len(store.pending_summary()), 2)

        llm = FakeLLM('{"summary": "他们在闲聊", "keywords": ["闲聊", "m0"]}')
        result = store.summarize_pending(llm=llm)

        self.assertEqual(result, {"summary": "他们在闲聊", "keywords": ["闲聊", "m0"]})
        self.assertEqual(store.pending_summary(), [])
        self.assertEqual(store.latest_summary()["summary"], "他们在闲聊")
        self.assertEqual(store.latest_summary()["keywords"], ["闲聊", "m0"])
        self.assertIsNotNone(store.latest_summary()["ts"])
        self.assertTrue((self.root / "srv.summary.jsonl").exists())

        # 约定的调用参数
        call = llm.calls[0]
        self.assertEqual(call["agent"], SUMMARY_AGENT)
        self.assertEqual(call["kwargs"]["temperature"], 0.2)
        self.assertEqual(call["kwargs"]["max_tokens"], 200)
        self.assertEqual(call["kwargs"]["thinking"], False)
        self.assertEqual(call["kwargs"]["timeout"], 20.0)

    def test_summary_is_reloadable_by_a_new_store(self):
        store = self._overfilled()
        store.summarize_pending(llm=FakeLLM('{"summary": "旧事重提", "keywords": ["k"]}'))

        fresh = self.store()
        fresh.use_namespace("srv", "")
        self.assertEqual(fresh.latest_summary()["summary"], "旧事重提")
        self.assertEqual(fresh.latest_summary()["keywords"], ["k"])
        self.assertEqual(fresh.status()["summaries"], 1)

    def test_json_code_fence_is_tolerated(self):
        store = self._overfilled()
        fenced = '好的，如下：\n```json\n{"summary": "带围栏", "keywords": "a, b、c"}\n```\n'
        store.summarize_pending(llm=FakeLLM(fenced))
        self.assertEqual(store.latest_summary()["summary"], "带围栏")
        self.assertEqual(store.latest_summary()["keywords"], ["a", "b", "c"])

    def test_llm_error_keeps_pending(self):
        store = self._overfilled()
        self.assertIsNone(store.summarize_pending(llm=FakeLLM(error=RuntimeError("boom"))))
        self.assertEqual(len(store.pending_summary()), 2)
        self.assertEqual(store.latest_summary(), {})

    def test_missing_llm_keeps_pending(self):
        store = self._overfilled()
        self.assertIsNone(store.summarize_pending())
        self.assertEqual(len(store.pending_summary()), 2)

    def test_unparsable_reply_keeps_pending(self):
        store = self._overfilled()
        self.assertIsNone(store.summarize_pending(llm=FakeLLM("没有 JSON 的胡话")))
        self.assertEqual(len(store.pending_summary()), 2)

    def test_summarize_with_no_pending_is_noop(self):
        store = self.store()
        store.use_namespace("srv", "")
        self.assertIsNone(store.summarize_pending(llm=FakeLLM('{"summary": "x"}')))


# ── 6. 喂 prompt 的结构 / status ───────────────────────────────

class DescribeStatusTests(_StoreCase):
    def test_describe_shape(self):
        store = self.store(single_window=3, multi_window=3)
        store.use_namespace("srv", "")
        for i in range(5):
            store.record_chat("A", f"m{i}")
        store.summarize_pending(llm=FakeLLM('{"summary": "摘要", "keywords": ["k"]}'))

        described = store.describe()
        self.assertEqual(described["namespace"], "srv")
        self.assertEqual(described["summary"], "摘要")
        self.assertEqual(described["keywords"], ["k"])
        self.assertEqual(len(described["recent"]), 3)

    def test_status_shape(self):
        store = self.store(single_window=3, multi_window=3)
        store.use_namespace("srv", "")
        for i in range(4):
            store.record_chat("A", str(i))

        status = store.status()
        for key in ("namespace", "window", "window_limit", "events_logged",
                    "summaries", "last_summary"):
            self.assertIn(key, status)
        self.assertEqual(status["namespace"], "srv")
        self.assertEqual(status["window"], 3)
        self.assertEqual(status["window_limit"], 3)
        self.assertEqual(status["events_logged"], 4)
        self.assertEqual(status["summaries"], 0)
        self.assertEqual(status["last_summary"], {})


if __name__ == "__main__":
    unittest.main()
