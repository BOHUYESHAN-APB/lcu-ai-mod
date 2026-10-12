"""周期性入站事件的"变化才打行"日志回归。

用户口径（原话）："日志这边，我希望做一个高级一点的内容，就是没有状态刷新，那就不刷新日志。"
实机现象：`← event: behavior_state` / `← event: control_state` 每秒刷 2~3 行、内容却基本不变，
把真正要看的 `→ command` / 错误 / 决策全淹掉。

本文件锁定四条硬约束：
  ① 周期状态事件内容没变 ⇒ 不打行，只在**变化**时打一行并交代"上一条相同了多久"；
     抖动（`afk.drift`、最近样本距离在 1 格内来回）**单独**出现不算"变了"；
  ② 可配置：`log_changes_only`（默认开）、`log_heartbeat_seconds`（默认 0 = 关）、
     `log_min_interval_seconds`（默认 5s = 同类事件两行最小间隔）；
     关掉开关 ⇒ 逐字回到旧行为（`← type: name`，每个事件一行）；
  ③ **不许静音关键信息**：命令/回执/错误/决策/聊天一律照旧、不去重、也不受限流；
  ④ **信息没丢**：抑制计数可复原见过的总条数（emitted + suppressed + merged = 总事件数），
     间隔内被合并的变化条数会写进下一行（"期间另有 N 条变化被合并"）。
"""
import json
import tempfile
import unittest
from pathlib import Path

from agent.config_store import ConfigStore
from protocol.wire_client import (
    PERIODIC_MIN_INTERVAL_SECONDS, WireClient, _LogDeduper, event_fingerprint,
    format_event_content, is_periodic_event,
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

    def advance(self, seconds):
        self.value += float(seconds)


class WireLogDedupeTests(unittest.TestCase):
    """走真实 `_read_loop`，只抓 `wire` logger 里实际打出的行。"""

    def _run(self, lines, configure=None, min_interval=0.0):
        wire = WireClient("127.0.0.1", 25568)
        # 本类锁的是"**变了才打**"的语义本身，所以把**最小间隔**关掉（默认 5s）；
        # 最小间隔的语义另有 `LogMinIntervalTests`，用假时钟精确控制时间。
        wire.configure_log_dedupe(min_interval_seconds=min_interval)
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
        # 摘要行（2026-10-12 起是**人读摘要**，不再是整段 JSON）：内容仍逐字可判
        self.assertEqual(out[0], '← behavior_state: navigating=否')
        self.assertIn("与上一条相同 ×4", out[1], "被压掉的 4 条要在下一行里交代清楚")
        self.assertIn("持续", out[1])
        self.assertIn("navigating=是", out[1], "变化行要带新内容")

    def test_each_content_change_emits_immediately(self):
        lines = [_event("control_state", {"ai_controlled": v}) for v in (True, False, True)]

        _wire, out = self._run(lines)

        self.assertEqual(len(out), 3, "每次内容变化都要立刻一行")
        self.assertIn("ai_controlled=是", out[0])
        self.assertIn("ai_controlled=否", out[1])
        self.assertIn("ai_controlled=是", out[2])

    def test_two_event_types_keep_independent_counts(self):
        lines = []
        for _ in range(4):                       # A/B 交替各 4 条 → 各 1 行
            lines.append(_event("behavior_state", {"navigating": False}))
            lines.append(_event("control_state", {"ai_controlled": True}))
        lines.append(_event("behavior_state", {"navigating": True}))  # A 变了

        _wire, out = self._run(lines)

        self.assertEqual(len(out), 3, "两类事件各自计数，互不串味")
        # 摘要行（2026-10-12 起是**人读摘要**，不再是整段 JSON）：内容仍逐字可判
        self.assertEqual(out[0], '← behavior_state: navigating=否')
        self.assertEqual(out[1], '← control_state: ai_controlled=是')
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

        self.assertEqual(len(out), 2, "关掉开关 → 非周期消息照旧一条一行")
        self.assertTrue(all(line.startswith("← response: ") and "id=req_1" in line
                            for line in out), out)

    # ── ③ 不许静音关键信息 ───────────────────────────────────────

    def test_non_periodic_messages_are_never_deduped(self):
        lines = [json.dumps({"type": "response", "id": "req_1", "data": {}}),
                 json.dumps({"type": "response", "id": "req_1", "data": {}}),
                 _event("player_death", {}),
                 json.dumps({"type": "event", "event": "player_chat", "data": {}})]

        _wire, out = self._run(lines)

        self.assertEqual(len(out), 4, "命令回执/死亡/聊天一条都不能被压掉")
        self.assertTrue(out[0].startswith("← response: "), out[0])
        self.assertIn("id=req_1", out[0])

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
                         "emitted + suppressed + merged must equal the total event count")
        self.assertEqual(deduper.emitted + deduper.suppressed + deduper.merged, len(lines))
        self.assertEqual(deduper.merged, 0, "min interval is off in this test")

        # 变化行里的 ×N 也能把"被压掉的那一段"复原出来。
        last = out[-1]
        self.assertIn("与上一条相同 ×5", last)
        self.assertEqual(split_count(last), 5, "×N 就是那一段被压掉的行数")

    # ── 心跳（默认关） ───────────────────────────────────────────

    def test_heartbeat_line_surfaces_liveness_when_enabled(self):
        lines = [_event("behavior_state", {"navigating": False}) for _ in range(4)]

        def _configure(wire):
            clock = _Clock(0.0)
            # 心跳也遵守最小间隔（默认 5s 会把 1s 心跳全挡住）⇒ 这条用例专门关心心跳，
            # 所以把间隔关掉。
            deduper = _LogDeduper(heartbeat_seconds=1.0, min_interval_seconds=0, clock=clock)
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
        deduper = _LogDeduper(heartbeat_seconds=2.0, min_interval_seconds=0, clock=clock)

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
            self.assertEqual(store.get_log_min_interval_seconds(), 5.0,
                             "默认同类周期事件两行至少隔 5 秒")

    def test_set_app_config_round_trip_and_clamps(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            store = ConfigStore(path)

            store.set_app_config({"log_changes_only": False, "log_heartbeat_seconds": 5,
                                  "log_min_interval_seconds": 2.5})
            self.assertFalse(store.get_log_changes_only())
            self.assertEqual(store.get_log_heartbeat_seconds(), 5.0)
            self.assertEqual(store.get_log_min_interval_seconds(), 2.5)
            self.assertFalse(ConfigStore(path).get_log_changes_only(), "要落盘")

            store.set_app_config({"log_heartbeat_seconds": -3})
            self.assertEqual(store.get_log_heartbeat_seconds(), 0.0, "负值回退 0（关）")

            store.set_app_config({"log_min_interval_seconds": 0})
            self.assertEqual(store.get_log_min_interval_seconds(), 0.0, "0 = 关最小间隔")
            store.set_app_config({"log_min_interval_seconds": "看不懂"})
            self.assertEqual(store.get_log_min_interval_seconds(), 0.0, "认不出 → 保持原值")

            store.set_app_config({"log_changes_only": "看不懂"})
            self.assertFalse(store.get_log_changes_only(), "认不出 → 保持原值")


class LogJitterTests(unittest.TestCase):
    """抖动**单独**出现不构成"内容变了"（用户实机：`afk.drift` 与最近样本距离在 1 格内来回）。"""

    @staticmethod
    def _state_update(drift=0.0, distance=1.1, since_ticks=0, ts=1):
        return {"type": "event", "event": "state_update", "ts": ts,
                "data": {"afk": {"drift": drift, "since_ticks": since_ticks},
                         "entities": [{"name": "绵羊", "distance": distance, "health": 8.0}]}}

    def test_drift_and_sample_distance_jitter_do_not_emit(self):
        clock = _Clock()
        # 最小间隔关掉：证明"不打行"是**抖动判据**的结果，不是间隔挡的。
        deduper = _LogDeduper(min_interval_seconds=0, clock=clock)
        lines = []
        for i in range(8):
            event = self._state_update(drift=0.2 if i % 2 else 0.0,
                                       distance=1.4 if i % 2 else 1.1,
                                       since_ticks=i, ts=1000 + i)
            lines.append(deduper.feed("state_update", event_fingerprint(event),
                                      format_event_content(event)))
            clock.advance(0.5)

        printed = [line for line in lines if line]
        self.assertEqual(len(printed), 1, "只有 drift / 最近样本距离在 1 格内来回 -> 不该再打行")
        self.assertEqual(deduper.total_events(), 8, "8 条事件一条不少地被数到")
        self.assertEqual(deduper.merged, 0, "抖动不是'变化'，不该计入被合并的变化数")

    def test_the_jitter_is_still_printed_not_hidden(self):
        """抖动不触发打行，但**打印内容照旧带原值** —— 信息不许被藏掉。"""
        quiet = format_event_content(self._state_update(drift=0.0, distance=1.1))
        jittered = format_event_content(self._state_update(drift=0.2, distance=1.4))

        self.assertNotEqual(quiet, jittered, "打印出来的确实是两个不同的值")
        self.assertIn('"drift":0.2', jittered)
        self.assertIn('"distance":1', jittered, "距离按'取整到格'打印")
        self.assertEqual(event_fingerprint(self._state_update(drift=0.0, distance=1.1)),
                         event_fingerprint(self._state_update(drift=0.2, distance=1.4)),
                         "但指纹把它们当同一个状态（同一格）")

    def test_real_count_change_emits_even_though_jitter_field_also_moved(self):
        """计数 27→23 是真变化 ⇒ 必须打行（抖动字段同时在动也不许掩盖它）。"""

        def state(n_blocks, drift):
            return {"type": "event", "event": "state_update", "ts": 1,
                    "data": {"afk": {"drift": drift},
                             "nearby_blocks": [{"block_id": "stone", "x": i, "y": 64, "z": 0}
                                               for i in range(n_blocks)]}}

        deduper = _LogDeduper(min_interval_seconds=0, clock=_Clock(0.0))
        first = state(27, 0.0)
        second = state(23, 0.4)
        line1 = deduper.feed("state_update", event_fingerprint(first), format_event_content(first))
        line2 = deduper.feed("state_update", event_fingerprint(second), format_event_content(second))

        self.assertIsNotNone(line1)
        self.assertIsNotNone(line2, "计数 27->23 是真变化，必须打一行")
        self.assertIn('"count":23', line2)


class LogMinIntervalTests(unittest.TestCase):
    """最小间隔：同类周期事件两行至少隔 `PERIODIC_MIN_INTERVAL_SECONDS`，间隔内的变化被合并。"""

    def test_default_interval_constant_and_deduper_default(self):
        self.assertEqual(PERIODIC_MIN_INTERVAL_SECONDS, 5.0)
        self.assertEqual(_LogDeduper(clock=_Clock()).min_interval_seconds, 5.0)

    def test_changes_inside_the_interval_are_merged_and_reported(self):
        clock = _Clock()
        deduper = _LogDeduper(min_interval_seconds=5.0, clock=clock)
        lines = []
        lines.append(deduper.feed("state_update", "fp:27", '{"count":27}', now=0.0))
        for i, fp in enumerate(("fp:26", "fp:25", "fp:24"), start=1):
            lines.append(deduper.feed("state_update", fp, '{"count":%d}' % (27 - i),
                                      now=0.5 * i))
        lines.append(deduper.feed("state_update", "fp:23", '{"count":23}', now=5.1))

        printed = [line for line in lines if line]
        self.assertEqual(len(printed), 2, "间隔内一行都不打；到点才打一行")
        self.assertIn("期间另有 3 条变化被合并", printed[1], "被合并的变化条数要如实交代")
        self.assertIn('"count":23', printed[1], "打出来的是**最新**内容")
        self.assertEqual(deduper.merged, 3)
        self.assertEqual(deduper.total_events(), 5,
                         "打出来的 + 被合并的 = 见过的总数（信息没丢）")

    def test_jitter_inside_the_interval_never_counts_as_merged_change(self):
        clock = _Clock()
        deduper = _LogDeduper(min_interval_seconds=5.0, clock=clock)

        def state(drift, distance):
            return {"type": "event", "event": "state_update", "ts": 1,
                    "data": {"afk": {"drift": drift},
                             "entities": [{"name": "羊", "distance": distance}]}}

        deduper.feed("state_update", event_fingerprint(state(0.0, 1.1)),
                     format_event_content(state(0.0, 1.1)), now=0.0)
        for i in range(1, 8):
            event = state(0.2 if i % 2 else 0.0, 1.4 if i % 2 else 1.1)
            self.assertIsNone(deduper.feed("state_update", event_fingerprint(event),
                                           format_event_content(event), now=0.5 * i))
        # 间隔到了：期间没有**变化**被合并 ⇒ 不该凭空打一行（心跳默认关）。
        self.assertIsNone(deduper.feed("state_update", event_fingerprint(state(0.0, 1.1)),
                                       format_event_content(state(0.0, 1.1)), now=5.2))
        self.assertEqual(deduper.merged, 0, "抖动被合并进'变化'计数就是虚报")
        self.assertEqual(deduper.total_events(), 9)

    def test_unchanged_stream_stays_silent_after_the_interval(self):
        clock = _Clock()
        deduper = _LogDeduper(min_interval_seconds=5.0, clock=clock)
        printed = [line for line in (
            deduper.feed("state_update", "fp", "{}", now=0.0),
            deduper.feed("state_update", "fp", "{}", now=0.5),
            deduper.feed("state_update", "fp", "{}", now=5.5),
        ) if line]
        self.assertEqual(len(printed), 1, "内容一直没变 -> 只有第一行")
        self.assertEqual(deduper.suppressed, 2)
        self.assertEqual(deduper.merged, 0)

    def test_repeats_after_a_merged_change_are_not_counted_as_extra_changes(self):
        clock = _Clock()
        deduper = _LogDeduper(min_interval_seconds=5.0, clock=clock)
        self.assertIsNotNone(deduper.feed("state_update", "A", "A", now=0.0))
        self.assertIsNone(deduper.feed("state_update", "B", "B", now=0.5))
        for t in (1.0, 1.5, 2.0):
            self.assertIsNone(deduper.feed("state_update", "B", "B", now=t))

        line = deduper.feed("state_update", "C", "C", now=5.2)

        self.assertIn("期间另有 1 条变化被合并", line, "A->B is one change; the trailing B's are not")
        self.assertIn("与上一条相同 ×3", line)
        self.assertEqual(deduper.merged, 1)
        self.assertEqual(deduper.total_events(), 6, "2 printed + 3 repeats + 1 change = 6 events")

    def test_interval_zero_restores_change_driven_behaviour(self):
        clock = _Clock()
        deduper = _LogDeduper(min_interval_seconds=0, clock=clock)
        printed = [line for line in (
            deduper.feed("state_update", "a", "A", now=0.0),
            deduper.feed("state_update", "b", "B", now=0.1),
            deduper.feed("state_update", "c", "C", now=0.2),
        ) if line]
        self.assertEqual(len(printed), 3, "0 = 关最小间隔 -> 变了就打（旧行为）")
        self.assertEqual(deduper.merged, 0)

    def test_a_line_at_t0_does_not_delay_other_event_types(self):
        """间隔是**按事件类型**各算各的：一类被限住，不许连累另一类。"""
        clock = _Clock()
        deduper = _LogDeduper(min_interval_seconds=5.0, clock=clock)
        first = deduper.feed("state_update", "a", "A", now=0.0)
        other = deduper.feed("behavior_state", "b", "B", now=0.5)
        self.assertIsNotNone(first)
        self.assertIsNotNone(other, "第一次见的类型立刻打行（没有'上一条'可比）")


class PeriodicThrottleSafetyTests(unittest.TestCase):
    """限流只针对**周期状态**：命令 / 回执 / 错误 / 聊天一条都不许被卷进来。"""

    def test_non_periodic_lines_survive_inside_the_interval(self):
        wire = WireClient("127.0.0.1", 25568)
        wire.configure_log_dedupe(min_interval_seconds=5.0)
        lines = [
            _event("state_update", {"count": 1}),
            _event("state_update", {"count": 2}),
            json.dumps({"type": "response", "id": "req_1", "data": {}}),
            json.dumps({"type": "event", "event": "player_chat", "data": {}}),
            json.dumps({"type": "event", "event": "error", "data": {}}),
            json.dumps({"type": "response", "id": "req_2", "data": {}}),
        ]
        wire.sock = FakeRecvSocket([(line + "\n").encode("utf-8") for line in lines])
        wire._running = True
        with self.assertLogs("wire", level="DEBUG") as logs:
            wire._read_loop()
        out = [record.getMessage() for record in logs.records]

        joined = "\n".join(out)
        for keep in ("id=req_1", "id=req_2", "← event: player_chat"):
            self.assertIn(keep, joined, "关键信息一条都不能被限流吃掉")
        self.assertEqual(out.count("← event: state_update"), 0,
                         "周期类走变化行，不走旧的逐条行")


if __name__ == "__main__":
    unittest.main()
