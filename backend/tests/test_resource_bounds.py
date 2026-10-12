"""长期运行时的**内存边界**回归（用户口径："后端占用内存特别明显"）。

三处实测会无界长的地方（改前 → 改后，数字见交付报告）：

1. `LLMService._usage_history`：每请求一条明细，**只加不减**（累计请求数用的是
   `len(history)`，所以既涨内存、也被截断影响不了口径）。现在明细留最近
   `USAGE_HISTORY_LIMIT` 条，累计 token / 累计请求数走独立计数器（**精确**）。
2. `AuditLog.list()`：以前 `read_text().splitlines()` —— **整份审计文件读进内存**
   （审计是 append-only，只增不减），跑到几万条后查一次就吃掉几十 MB。现在从尾部按块
   反向读、有扫描上限，并且"类别过滤命中在很旧的位置"时照旧能找出来（语义不缩水）。
3. `WireClient._event_queue`：生产端是读线程（`state_update` 实测 2Hz、每条约 3KB），
   消费端是接线循环 —— 而循环里跑 `session.tick()`（可能有秒级 LLM 调用）。这期间队列
   只进不出。现在**周期快照**在队列里最多留 `MAX_QUEUED_STATE_EVENTS` 条（latest-wins，
   丢了如实计数）；命令 / 回执 / 进度 / 聊天**一条都不丢**。
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.audit_log import AuditLog  # noqa: E402
from agent.llm_service import USAGE_HISTORY_LIMIT, LLMService  # noqa: E402
from protocol.wire_client import (  # noqa: E402
    MAX_QUEUED_STATE_EVENTS, WireClient,
)

from tests.test_wire_log_dedupe import FakeRecvSocket  # noqa: E402


class UsageHistoryBoundTests(unittest.TestCase):

    def test_history_is_bounded_but_the_totals_stay_exact(self):
        service = LLMService()
        for _ in range(USAGE_HISTORY_LIMIT + 200):
            service._track_usage({"usage": {"prompt_tokens": 10, "completion_tokens": 2}})

        usage = service.get_usage()
        self.assertEqual(usage["history_entries"], USAGE_HISTORY_LIMIT, "明细有上限")
        self.assertEqual(usage["request_count"], USAGE_HISTORY_LIMIT + 200,
                         "累计请求数不许被截断影响（session 用它算本轮增量）")
        self.assertEqual(usage["total_prompt_tokens"], (USAGE_HISTORY_LIMIT + 200) * 10)
        self.assertEqual(usage["total_completion_tokens"], (USAGE_HISTORY_LIMIT + 200) * 2)
        self.assertEqual(usage["history_limit"], USAGE_HISTORY_LIMIT)

    def test_history_keeps_the_newest_entries(self):
        service = LLMService()
        for value in range(USAGE_HISTORY_LIMIT + 5):
            service._track_usage({"usage": {"prompt_tokens": value + 1}})
        self.assertEqual(service._usage_history[-1]["prompt"], USAGE_HISTORY_LIMIT + 5)
        self.assertEqual(service._usage_history[0]["prompt"], 6, "留下的是最近的那一段")


class AuditLogReadBoundTests(unittest.TestCase):

    def _log_with(self, tmp: str, count: int, *, category: str = "config") -> AuditLog:
        log = AuditLog(Path(tmp) / "audit.jsonl")
        with log.path.open("w", encoding="utf-8") as handle:
            for i in range(count):
                handle.write(json.dumps({
                    "id": f"id-{i}", "occurred_at": 1000 + i,
                    "category": "old_other" if i < 5 else category,
                    "action": f"action-{i}", "target": "", "actor": "operator",
                    "outcome": "succeeded", "details": {"i": i, "pad": "x" * 120},
                }, ensure_ascii=False) + "\n")
        return log

    def test_list_returns_the_newest_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = self._log_with(tmp, 3000)
            rows = log.list(limit=100)
            self.assertEqual(len(rows), 100)
            self.assertEqual(rows[0]["id"], "id-2999", "最新的在最前")
            self.assertEqual(rows[-1]["id"], "id-2900")

    def test_read_is_bounded_not_the_whole_file(self):
        """只从尾部读：行的总量远大于要的结果数时，读进来的行数必须远小于文件行数。"""
        with tempfile.TemporaryDirectory() as tmp:
            log = self._log_with(tmp, 3000)
            tail = log._tail_lines(100)
            self.assertLess(len(tail), 1000, f"读了 {len(tail)} 行，应该只读尾部一小段")
            self.assertGreaterEqual(len(tail), 100)

    def test_category_filter_still_finds_older_matches(self):
        """类别命中在很旧的位置时也要找得到（语义不许因为"只读尾部"而缩水）。"""
        with tempfile.TemporaryDirectory() as tmp:
            log = self._log_with(tmp, 3000, category="config")
            rows = log.list(category="old_other", limit=100)
            self.assertEqual([row["id"] for row in rows], [f"id-{i}" for i in reversed(range(5))])

    def test_limit_is_clamped_and_missing_file_is_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = AuditLog(Path(tmp) / "nope.jsonl")
            self.assertEqual(log.list(limit=100), [])
            log2 = self._log_with(tmp, 3)
            self.assertEqual(len(log2.list(limit=10 ** 6)), 3)


class WireQueueBoundTests(unittest.TestCase):

    @staticmethod
    def _state_line(i):
        return json.dumps({"type": "event", "event": "state_update", "ts": i,
                           "data": {"player": {"x": i, "y": 64, "z": 0, "health": 20},
                                    "entities": {"count": i, "by_kind": {"hostile": 1}}}})

    @staticmethod
    def _response_line(i):
        return json.dumps({"type": "response", "id": f"req_{i}", "success": True,
                           "result": {"i": i}})

    def _feed(self, lines):
        wire = WireClient("127.0.0.1", 25568)
        wire.sock = FakeRecvSocket([(line + "\n").encode("utf-8") for line in lines])
        wire._running = True
        with self.assertLogs("wire", level="DEBUG"):
            wire._read_loop()
        return wire

    def test_state_snapshots_are_bounded_latest_wins(self):
        wire = self._feed([self._state_line(i) for i in range(MAX_QUEUED_STATE_EVENTS * 4)])
        stats = wire.queue_stats()
        self.assertLessEqual(stats["queued_state_events"], MAX_QUEUED_STATE_EVENTS)
        self.assertEqual(stats["state_event_limit"], MAX_QUEUED_STATE_EVENTS)
        self.assertEqual(stats["dropped_state_events"],
                         MAX_QUEUED_STATE_EVENTS * 4 - stats["queued_state_events"],
                         "丢了多少要如实计数")

    def test_the_newest_state_survives(self):
        total = MAX_QUEUED_STATE_EVENTS * 3
        wire = self._feed([self._state_line(i) for i in range(total)])
        queued = list(wire._event_queue.queue)
        self.assertEqual(queued[-1].data["data"]["entities"]["count"], total - 1,
                         "最新的状态必须在（丢的是最旧的）")
        self.assertEqual(queued[0].data["data"]["entities"]["count"], total - MAX_QUEUED_STATE_EVENTS)

    def test_commands_and_responses_are_never_dropped(self):
        lines = []
        for i in range(40):
            lines.append(self._response_line(i))
            for j in range(6):
                lines.append(self._state_line(i * 10 + j))
        wire = self._feed(lines)
        responses = [msg for msg in wire._event_queue.queue if msg.type == "response"]
        self.assertEqual(len(responses), 40, "回执一条都不许丢（丢了动作就永远等不到结果）")
        self.assertGreater(wire.queue_stats()["dropped_state_events"], 0)

    def test_trimming_does_not_disturb_the_remaining_messages(self):
        """丢的是"队列里最旧的那条快照"，其余消息顺序不变（消费者照旧按序拿到）。"""
        lines = [self._state_line(i) for i in range(MAX_QUEUED_STATE_EVENTS + 5)]
        lines.insert(3, self._response_line(999))
        wire = self._feed(lines)
        queued = [msg for msg in wire._event_queue.queue]
        counts = [msg.data["data"]["entities"]["count"] for msg in queued if msg.type == "event"]
        self.assertEqual(counts, sorted(counts), "剩下的快照仍按原顺序")
        self.assertEqual(len([m for m in queued if m.type == "response"]), 1)


if __name__ == "__main__":
    unittest.main()
