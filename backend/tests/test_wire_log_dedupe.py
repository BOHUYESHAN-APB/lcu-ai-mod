"""周期性入站事件的"变化才打行"日志回归。

用户口径（原话）："日志这边，我希望做一个高级一点的内容，就是没有状态刷新，那就不刷新日志。"
实机现象：`← event: behavior_state` / `← event: control_state` 每秒刷 2~3 行、内容却基本不变，
把真正要看的 `→ command` / 错误 / 决策全淹掉。

本文件锁定四条硬约束：
  ① 周期状态事件内容没变 ⇒ 不打行，只在**变化**时打一行并交代"上一条相同了多久"；
  ② 可配置：`log_changes_only`（默认开）、`log_heartbeat_seconds`（默认 0 = 关）；
     关掉开关 ⇒ 逐字回到旧行为（`← type: name`，每个事件一行）；
  ③ **不许静音关键信息**：命令/回执/错误/决策/聊天一律照旧、不去重；
  ④ **信息没丢**：抑制计数可复原见过的总条数（emitted + suppressed = 总事件数）。
"""
import json
import tempfile
import unittest
from pathlib import Path

from agent.config_store import ConfigStore
from protocol.wire_client import (
    WireClient, _LogDeduper, event_fingerprint, is_periodic_event,
)


class FakeRecvSocket:
    """按块返回预置字节；耗尽后返回 b""（触发读循环收尾）。"""

    def __init__(self, chunks):
        self._chunks = list(chunks)

    def recv(self, _n):
        return self._chunks.pop(0) if self._chunks else b""

    def close(self):
        pass


def _event(name, data=None, ts=1):
    return json.dumps({"type": "event", "event": name, "ts": ts,
                       "data": {} if data is None else data})


class _Clock:
    def __init__(self, value=0.0):
        self.value = value

    def __call__(self):
        return self.value


class WireLogDedupeTests(unittest.TestCase):
    """走真实 `_read_loop`，只抓 `wire` logger 里实际打出的行。"""

    def _run(self, lines, configure=None):
        wire = WireClient("127.0.0.1", 25568)
        if configure is not None:
            configure(wire)
        wire.sock = FakeRecvSocket([(line + "\n").encode("utf-8") for line in lines])
        wire._running = True
        with self.assertLogs("wire", level="DEBUG") as logs:
            wire._read_loop()
        return wire, [record.getMessage() for record in logs.records]

    # ── ① 变化才打行 ──────────────────────────────────────────────

    def test_identical_run_is_suppressed_and_counted_on_next_change(self):
        lines = [_event("behavior_state", {"navigating": False}) for _ in range(5)]
        lines.append(_event("behavior_state", {"navigating": True}))

        _wire, out = self._run(lines)

        self.assertEqual(len(out), 2, "5 条相同只该 1 行；内容变了再 1 行")
        self.assertEqual(out[0], '← behavior_state: {"navigating":false}')
        self.assertIn("与上一条相同 ×4", out[1], "被压掉的 4 条要在下一行里交代清楚")
        self.assertIn("持续", out[1])
        self.assertIn('{"navigating":true}', out[1], "变化行要带新内容")

    def test_each_content_change_emits_immediately(self):
        lines = [_event("control_state", {"ai_controlled": v}) for v in (True, False, True)]

        _wire, out = self._run(lines)

        self.assertEqual(len(out), 3, "每次内容变化都要立刻一行")
        self.assertIn('{"ai_controlled":true}', out[0])
        self.assertIn('{"ai_controlled":false}', out[1])
        self.assertIn('{"ai_controlled":true}', out[2])

    def test_two_event_types_keep_independent_counts(self):
        lines = []
        for _ in range(4):                       # A/B 交替各 4 条 → 各 1 行
            lines.append(_event("behavior_state", {"navigating": False}))
            lines.append(_event("control_state", {"ai_controlled": True}))
        lines.append(_event("behavior_state", {"navigating": True}))  # A 变了

        _wire, out = self._run(lines)

        self.assertEqual(len(out), 3, "两类事件各自计数，互不串味")
        self.assertEqual(out[0], '← behavior_state: {"navigating":false}')
        self.assertEqual(out[1], '← control_state: {"ai_controlled":true}')
        self.assertIn("与上一条相同 ×3", out[2], "behavior_state 自己的第 3 次相同")
        self.assertNotIn("×3", out[1])

    def test_timestamps_and_counters_do_not_defeat_dedupe(self):
        """信封 ts / 内嵌 timestamp / 计数器（since_ticks、day_time、*_ms）每次都变也不能算"变了"。"""
        lines = []
        for i in range(6):
            lines.append(_event("state_update", {
                "in_world": False,
                "timestamp": 1_000_000 + i,
                "ui_screen": "GuiConnecting",
                "afk": {"since_ticks": i},
                "day_time": 100 + i,
                "stale_ms": i * 7,
            }, ts=1_000_000 + i))

        _wire, out = self._run(lines)

        self.assertEqual(len(out), 1, "只有时间戳/计时器在变 → 必须判为未变")

    # ── ② 可配置 ─────────────────────────────────────────────────

    def test_switch_off_restores_old_lines_verbatim(self):
        lines = [_event("behavior_state", {"navigating": False}) for _ in range(3)]

        _wire, out = self._run(lines, configure=lambda w: w.configure_log_dedupe(enabled=False))

        self.assertEqual(out, ["← event: behavior_state"] * 3,
                         "关掉开关 → 逐字回到旧行为，一行不少")

    def test_switch_off_does_not_touch_non_periodic_either(self):
        lines = [json.dumps({"type": "response", "id": "req_1", "data": {}}) for _ in range(2)]

        _wire, out = self._run(lines, configure=lambda w: w.configure_log_dedupe(enabled=False))

        self.assertEqual(out, ["← response: req_1"] * 2)

    # ── ③ 不许静音关键信息 ───────────────────────────────────────

    def test_non_periodic_messages_are_never_deduped(self):
        lines = [json.dumps({"type": "response", "id": "req_1", "data": {}}),
                 json.dumps({"type": "response", "id": "req_1", "data": {}}),
                 _event("player_death", {}),
                 json.dumps({"type": "event", "event": "player_chat", "data": {}})]

        _wire, out = self._run(lines)

        self.assertEqual(len(out), 4, "命令回执/死亡/聊天一条都不能被压掉")
        self.assertEqual(out[0], "← response: req_1")

    # ── ④ 信息没丢 ───────────────────────────────────────────────

    def test_suppressed_counts_reconstruct_the_total(self):
        lines = []
        for _ in range(6):
            lines.append(_event("behavior_state", {"navigating": False}))
        for _ in range(3):
            lines.append(_event("control_state", {"ai_controlled": True}))
        lines.append(_event("behavior_state", {"navigating": True}))   # 触发变化行

        wire, out = self._run(lines)

        deduper = wire._log_deduper
        self.assertEqual(deduper.total_events(), len(lines),
                         "emitted + suppressed 必须等于见过的总事件数")
        self.assertEqual(deduper.emitted + deduper.suppressed, len(lines))

        # 变化行里的 ×N 也能把"被压掉的那一段"复原出来。
        last = out[-1]
        self.assertIn("与上一条相同 ×5", last)
        self.assertEqual(split_count(last), 5, "×N 就是那一段被压掉的行数")

    # ── 心跳（默认关） ───────────────────────────────────────────

    def test_heartbeat_line_surfaces_liveness_when_enabled(self):
        lines = [_event("behavior_state", {"navigating": False}) for _ in range(4)]

        def _configure(wire):
            clock = _Clock(0.0)
            deduper = _LogDeduper(heartbeat_seconds=1.0, clock=clock)
            wire._log_deduper = deduper
            original = deduper.feed

            def _feed(name, fp, content, now=None):
                line = original(name, fp, content)
                clock.value += 0.6      # 每条间隔 0.6s → 第 3 条起触发 1s 心跳
                return line

            deduper.feed = _feed

        _wire, out = self._run(lines, configure=_configure)

        heartbeats = [line for line in out if "未变" in line]
        self.assertTrue(heartbeats, "开了心跳就该出现「未变」行（区分链路活着 vs 断了）")
        self.assertIn("持续", heartbeats[0])


def split_count(line):
    """从 `…（与上一条相同 ×N，持续 T 秒）…` 里取出 N。"""
    return int(line.split("×")[1].split("，")[0])


class LogDeduperUnitTests(unittest.TestCase):
    def test_heartbeat_emits_when_unchanged(self):
        clock = _Clock()
        deduper = _LogDeduper(heartbeat_seconds=2.0, clock=clock)

        self.assertIsNotNone(deduper.feed("behavior_state", "fp", '{"x":1}', now=0.0))
        self.assertIsNone(deduper.feed("behavior_state", "fp", '{"x":1}', now=1.0))

        line = deduper.feed("behavior_state", "fp", '{"x":1}', now=3.0)
        self.assertIsNotNone(line)
        self.assertIn("未变", line)
        self.assertIn("已持续 3.0 秒", line)
        self.assertIn("×2", line)
        self.assertEqual(deduper.heartbeats, 1)

    def test_heartbeat_is_off_by_default(self):
        deduper = _LogDeduper(clock=_Clock())

        deduper.feed("behavior_state", "fp", "c", now=0.0)
        self.assertIsNone(deduper.feed("behavior_state", "fp", "c", now=10_000.0),
                          "默认（0）不许心跳 —— 用户口径就是「没刷新就不刷新」")
        self.assertEqual(deduper.heartbeats, 0)

    def test_memory_is_bounded(self):
        deduper = _LogDeduper()
        for i in range(_LogDeduper.MAX_KEYS + 20):
            deduper.feed(f"thing_{i}_state", f"fp{i}", "{}", now=float(i))

        self.assertLessEqual(len(deduper._states), _LogDeduper.MAX_KEYS,
                             "只保留最近 N 类，内存不许无限涨")

    def test_periodic_event_classification(self):
        self.assertTrue(is_periodic_event("event", "behavior_state"))
        self.assertTrue(is_periodic_event("event", "state_update"))
        self.assertTrue(is_periodic_event("event", "future_widget_state"))
        self.assertFalse(is_periodic_event("event", "player_chat"))
        self.assertFalse(is_periodic_event("response", "behavior_state"),
                         "非 event 类型一律不去重")
        self.assertFalse(is_periodic_event("progress", "1"))

    def test_fingerprint_is_key_order_independent(self):
        self.assertEqual(event_fingerprint({"a": 1, "b": 2}),
                         event_fingerprint({"b": 2, "a": 1}))
        self.assertNotEqual(event_fingerprint({"a": 1}),
                            event_fingerprint({"a": 2}))


class WireLogConfigTests(unittest.TestCase):
    def test_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ConfigStore(Path(tmp) / "config.json")
            self.assertTrue(store.get_log_changes_only(), "默认变化驱动")
            self.assertEqual(store.get_log_heartbeat_seconds(), 0.0, "默认不心跳")

    def test_set_app_config_round_trip_and_clamps(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            store = ConfigStore(path)

            store.set_app_config({"log_changes_only": False, "log_heartbeat_seconds": 5})
            self.assertFalse(store.get_log_changes_only())
            self.assertEqual(store.get_log_heartbeat_seconds(), 5.0)
            self.assertFalse(ConfigStore(path).get_log_changes_only(), "要落盘")

            store.set_app_config({"log_heartbeat_seconds": -3})
            self.assertEqual(store.get_log_heartbeat_seconds(), 0.0, "负值回退 0（关）")

            store.set_app_config({"log_changes_only": "看不懂"})
            self.assertFalse(store.get_log_changes_only(), "认不出 → 保持原值")


if __name__ == "__main__":
    unittest.main()
