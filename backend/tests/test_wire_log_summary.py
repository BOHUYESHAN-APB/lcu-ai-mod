"""入站周期状态日志的**有界人读摘要**回归（与出站 `MAX_LOG_CONTENT_CHARS` 同一套口径）。

用户口径（原话，2026-10-12）："后端占用内存特别明显" / "state_update 和 token（用量/流）的
内容太多了"。实机那一行是**整段 JSON 截前 400 字**（实测 483 字/行、负载 3175 字）：

    ← state_update: {"afk":{…},"biome":"minecraft:plains","can_sleep":false,
                     "death_points":…,"entities":{…}…(+2775 字)

问题有两层：**太长**，而且**关键量被切掉** —— 键按字母序排，`player`（位置/血量）、
`task_state`、`inventory` 全排在后面，400 字里一个都看不见。现在改成
`format_event_summary` 的**人读摘要**：关键字段优先 + 结构上限 + 真被砍就如实写
"完整摘要 N 字符，已省略 M 字符；全量看 /api/status 或 state 文件"。

本文件锁定五条：
  ① 一行**有界**（`MAX_LOG_SUMMARY_CHARS` + 那句省略交代）；
  ② **可判定事实不丢**：位置 / 血量 / 饥饿 / 维度 / 任务 / 背包 / 实体与威胁 / 方块计数都在；
  ③ **不刷坐标**：只有"自己的位置"带坐标，远处实体/方块的坐标不进日志（模型照旧拿全量）；
  ④ **省略要如实**：省略了多少字符写清楚（不假装"什么都没少"）；
  ⑤ **去重仍走这一行**：内容没变不打行，真变化（如计数 27→23）照样打且带上新值。
"""
import json
import unittest

from protocol.wire_client import (
    MAX_LOG_SUMMARY_CHARS, WireClient, event_fingerprint, format_event_summary,
)

from tests.test_wire_log_dedupe import FakeRecvSocket, _event


def _payload(*, n_blocks=64, n_entities=37, health=20.0, far_threat=True):
    """**真实形状**的 state_update（逐字对齐 Java `StateCollector` 的键名，含胖数组）。"""
    return {
        "in_world": True,
        "ui_screen": "",
        "player": {"name": "AI", "health": health, "max_health": 20.0, "hunger": 18,
                   "saturation": 5.0, "x": 100.5, "y": 64.0, "z": 100.25,
                   "yaw": 90.0, "pitch": 3.5, "dimension": "minecraft:overworld",
                   "gamemode": "survival",
                   "equipment": {"head": {"name": "minecraft:iron_helmet", "count": 1}}},
        "world": {"time": 6200, "is_day": True, "is_raining": False, "light_level": 15,
                  "server": "localhost", "world_name": "world"},
        "inventory": [{"slot": i, "name": f"minecraft:item_{i}", "count": 64,
                       "display": f"物品{i}"} for i in range(10)],
        "entities": {
            "count": n_entities,
            "by_kind": {"dangerous": 0, "hostile": 2, "neutral": 3, "passive": 30,
                        "player": 1, "item": 1, "unknown": 0},
            "nearest": {"distance": 3.2, "name": "牛", "type": "passive"},
            "threats": [{"name": "僵尸", "type": "hostile", "tier": "hostile",
                         "x": 9999.0 if far_threat else 98.0, "y": 64.0, "z": 103.0,
                         "distance": 4.5}],
            "players": [{"name": "Steve", "x": 101.0, "y": 64.0, "z": 99.0,
                         "distance": 1.4, "health": 20.0}],
            "items": {"count": 1, "nearest": {"item_id": "minecraft:rotten_flesh",
                                              "distance": 6.0}},
        },
        "nearby_blocks": {
            "count": n_blocks,
            "by_block": {"minecraft:grass_block": 30, "minecraft:oak_log": 12},
            "nearest": {"block_id": "minecraft:oak_log", "distance": 4.0},
        },
        "remembered_pois": [{"name": "家", "category": "home", "x": -176, "y": 72,
                             "z": -80, "distance": 2100.0}],
        "stations": [{"name": "矿区", "x": 120, "y": 40, "z": 130}],
        "zones": [{"name": "家保护区", "kind": "protect", "radius": 16}],
        "task_state": {"kind": "idle", "status": "idle", "progress": 0.0},
        "biome": "minecraft:plains",
        "day_time": 6200,
        "is_night": False,
    }


def _line(payload):
    return format_event_summary({"type": "event", "event": "state_update", "data": payload})


class EventSummaryBoundsTests(unittest.TestCase):

    def test_line_is_bounded(self):
        line = _line(_payload())
        # 硬上限 + 那句"（完整摘要 N 字符，已省略 M 字符；全量看 …）"的交代本身
        self.assertLessEqual(len(line), MAX_LOG_SUMMARY_CHARS + 120, msg=f"长度 {len(line)}")

    def test_decidable_facts_survive(self):
        """用户点名的关键量：位置 / 血量 / 饥饿 / 任务 / 背包 / 威胁 —— 一个都不许少。"""
        line = _line(_payload(health=17.5))
        for need in ("位置(100,64,100)", "血17.5/20", "饿18", "overworld",
                     "任务(类=idle", "背包(共10)", "实体(共37", "威胁1[僵尸 4格]",
                     "附近方块(共64"):
            self.assertIn(need, line, msg=f"少了 {need!r}：{line}")

    def test_own_position_is_kept_but_far_coordinates_are_not(self):
        near = _line(_payload(far_threat=False))
        far = _line(_payload(far_threat=True))
        # 自己的位置在（可判定事实）
        self.assertIn("位置(100,64,100)", near)
        # 远处那只（9999 格）的坐标**不进日志** —— 只留身份与距离；两条行的摘要都是"僵尸 4格"
        self.assertNotIn("9999", far)
        self.assertNotIn("98", far.split("威胁")[1][:20])
        self.assertIn("威胁1[僵尸 4格]", far)

    def test_elision_is_reported_honestly(self):
        """真被砍就如实写"完整摘要 N 字符，已省略 M 字符"（不假装什么都没少）。"""
        payload = _payload()
        payload.update({f"extra_{i}": [{"name": f"东西{i}", "x": i} for _ in range(4)]
                        for i in range(20)})
        line = _line(payload)
        self.assertIn("已省略", line)
        self.assertIn("完整摘要", line)
        self.assertIn("全量看 /api/status 或 state 文件", line)
        full = format_event_summary({"type": "event", "event": "state_update", "data": payload},
                                    limit=10 ** 9)
        self.assertGreater(len(full), MAX_LOG_SUMMARY_CHARS)
        # 报的"省略了多少"必须等于"完整摘要 - 打出来的正文"（不含那句交代）
        body = line[:line.index("…（完整摘要")]
        omitted = int(line.split("已省略 ", 1)[1].split(" 字符", 1)[0])
        self.assertEqual(omitted, len(full) - len(body))

    def test_no_elision_when_it_fits(self):
        """小负载（behavior_state / control_state）不该出现省交代 —— 一行装得下就全给。"""
        line = format_event_summary({"type": "event", "event": "control_state",
                                     "data": {"ai_controlled": True, "behaviors_enabled": False}})
        self.assertNotIn("已省略", line)
        self.assertIn("ai_controlled=是", line)
        self.assertIn("behaviors_enabled=否", line)

    def test_empty_values_are_skipped(self):
        line = format_event_summary({"type": "event", "event": "task_state",
                                     "data": {"ui_screen": "", "kind": "idle"}})
        self.assertNotIn("界面=", line)
        self.assertIn("类=idle", line)

    def test_unknown_keys_are_not_hidden(self):
        line = format_event_summary({"type": "event", "event": "task_state",
                                     "data": {"brand_new_thing": [1, 2, 3],
                                              "another": "x"}})
        self.assertIn("brand_new_thing3", line)
        self.assertIn("another=x", line)


class EventSummaryDedupeTests(unittest.TestCase):
    """摘要行与去重**同源**：走真实 `_read_loop`，只抓 `wire` 实际打出的行。"""

    def _run(self, payloads):
        wire = WireClient("127.0.0.1", 25568)
        wire.configure_log_dedupe(min_interval_seconds=0)
        wire.sock = FakeRecvSocket(
            [(json.dumps({"type": "event", "event": "state_update", "ts": 1, "data": payload})
              + "\n").encode("utf-8") for payload in payloads])
        wire._running = True
        with self.assertLogs("wire", level="DEBUG") as logs:
            wire._read_loop()
        return [record.getMessage() for record in logs.records]

    def test_identical_payload_is_one_line(self):
        out = self._run([_payload() for _ in range(6)])
        self.assertEqual(len(out), 1, "内容没变 = 只有第 1 行")
        self.assertIn("血20/20", out[0])

    def test_real_change_emits_and_carries_the_new_value(self):
        out = self._run([_payload(), _payload(n_blocks=64), _payload(n_blocks=23)])
        self.assertEqual(len(out), 2, "方块 64→23 是真变化，必须打一行")
        self.assertIn("附近方块(共23", out[1])

    def test_far_only_change_does_not_emit_a_coordinate_line(self):
        """远处那只挪了位置：摘要里的"僵尸 4格"没变 ⇒ 不该因为坐标而多打一行。"""
        first = _payload(far_threat=True)
        second = _payload(far_threat=True)
        second["entities"]["threats"][0]["x"] = 12000.0
        out = self._run([first, second])
        self.assertEqual(len(out), 1)

    def test_fingerprint_matches_the_printed_line(self):
        """指纹 = 打印摘要行的哈希（同一份负载 ⇒ 同一行 ⇒ 同一指纹）。"""
        payload = _payload()
        event = {"type": "event", "event": "state_update", "data": payload}
        self.assertEqual(format_event_summary(event), format_event_summary(event))
        self.assertEqual(event_fingerprint(event), event_fingerprint(
            json.loads(json.dumps(event))))
        changed = _payload(n_blocks=12)
        self.assertNotEqual(event_fingerprint(event),
                            event_fingerprint({"type": "event", "event": "state_update",
                                               "data": changed}))


if __name__ == "__main__":
    unittest.main()
