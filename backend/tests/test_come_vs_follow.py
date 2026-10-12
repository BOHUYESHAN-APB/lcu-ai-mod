"""「过来/来我这」（一次性）与「跟着我」（长驻）的语义分野 + 周期日志坐标压缩。

用户实机口径（原话）："玩家说过来≠让ai跟着" —— mineflayer 的 `GoalNear` vs `GoalFollow` 分野：

  * 「过来 / 来我这」= 走到那个人**现在**站的位置，**一次性**，到了就结束 ⇒ `come_to_player`；
  * 「跟着我 / 别走开 / 跟我来」= 长驻跟随（目标会动）⇒ `follow_player`。

本文件测的是**真发出去什么**（不是读源码文本）：
  ① 注册表里有 `come_to_player`（args=player、与 move_to/follow 同口径权限）；
  ② `skills.come_to_player("X")` 真的发出 `{"cmd":"come_to_player","args":{"player":"X"}}`；
  ③ `planner._tool_come_to_player` 的接线：一次性动作**不**套 follow 的重复门（同格紧邻重复
     才压、挪了格/隔久了照发、解析不出目标格时不压）；
  ④ 周期状态日志：指纹仍用完整负载（灵敏度不减），打印内容压成摘要（数组只留条数 + 最近样本）；
     非周期事件一字不变。
"""

from __future__ import annotations

import json
import time
import types
import unittest

from agent.planner import COME_TO_PLAYER_REPEAT_WINDOW, Planner
from agent.skills import Skills
from agent.tool_registry import TOOL_BY_NAME
from protocol.wire_client import (
    WireClient, event_fingerprint, format_event_content, is_periodic_event,
)


# ── ① 注册表 ────────────────────────────────────────────────

class ComeToPlayerRegistryTests(unittest.TestCase):
    def test_tool_is_registered_with_player_arg(self):
        spec = TOOL_BY_NAME["come_to_player"]
        self.assertEqual([p.strip() for p in spec.args.split(",") if p.strip()], ["player"])
        self.assertEqual(spec.groups, ("move",))
        self.assertTrue(spec.job, "一次性走动也是长任务，要能查看/取消")
        self.assertTrue(callable(getattr(Planner, spec.handler, None)),
                        "登记的 handler 必须真在 Planner 上")

    def test_permission_matches_move_and_follow(self):
        """权限口径照 move_to/follow：无额外 tier/容器/区域门（就是走动）。"""
        for name in ("move_to", "follow", "come_to_player"):
            perm = TOOL_BY_NAME[name].permission
            self.assertEqual(perm.tier, "", name)
            self.assertFalse(perm.container, name)
            self.assertFalse(perm.area, name)

    def test_follow_description_points_at_come_to_player(self):
        """收紧 follow 描述：只在**明确要求跟随**时用；「过来」用 come_to_player。"""
        desc = TOOL_BY_NAME["follow"].description
        self.assertIn("come_to_player", desc)
        self.assertIn("跟随", desc)
        self.assertIn("一次性", desc)

    def test_where_is_and_find_entity_mention_come_to_player(self):
        for name in ("where_is", "find_entity"):
            self.assertIn("come_to_player", TOOL_BY_NAME[name].description, name)


# ── ② skills 接线（真的发出什么）──────────────────────────────

class ComeToPlayerSkillTests(unittest.TestCase):
    def _sent(self, **kwargs) -> dict:
        """不跑 `Skills.__init__`（它会去连 wire），只截住真正下发的那一条命令。"""
        skills = Skills.__new__(Skills)
        sent: dict = {}
        skills._send_cmd = lambda cmd, args: (sent.update(cmd=cmd, args=args), {})[1]
        skills.come_to_player(**kwargs)
        return sent

    def test_sends_the_frozen_contract(self):
        sent = self._sent(player_name="X")
        self.assertEqual(sent["cmd"], "come_to_player")
        self.assertEqual(sent["args"], {"player": "X"})

    def test_follow_shape_is_unchanged(self):
        skills = Skills.__new__(Skills)
        sent: dict = {}
        skills._send_cmd = lambda cmd, args: (sent.update(cmd=cmd, args=args), {})[1]
        skills.follow_player("Y")
        self.assertEqual(sent["cmd"], "follow_player")
        self.assertEqual(sent["args"], {"player": "Y"})


# ── ③ planner handler ───────────────────────────────────────

def _planner(sent: list) -> Planner:
    planner = Planner.__new__(Planner)
    planner.skills = types.SimpleNamespace(
        come_to_player=lambda name: sent.append(name))
    return planner


def _ctx(player="Steve", x=11.0, y=64.0, z=-4.0) -> dict:
    return {"players": [{"name": player, "x": x, "y": y, "z": z, "distance": 1.0}]}


class ComeToPlayerHandlerTests(unittest.TestCase):
    def test_dispatches_to_skills(self):
        sent: list = []
        ok = _planner(sent)._tool_come_to_player({"player": "Steve"}, _ctx())
        self.assertTrue(ok)
        self.assertEqual(sent, ["Steve"])

    def test_accepts_player_name_alias(self):
        sent: list = []
        self.assertTrue(_planner(sent)._tool_come_to_player({"player_name": "Alex"}, {}))
        self.assertEqual(sent, ["Alex"])

    def test_missing_player_is_refused(self):
        sent: list = []
        planner = _planner(sent)
        self.assertFalse(planner._tool_come_to_player({}, _ctx()))
        self.assertFalse(planner._tool_come_to_player({"player": "  "}, _ctx()))
        self.assertEqual(sent, [])

    def test_same_cell_repeat_is_suppressed(self):
        """同一格上的紧邻重复请求才压掉（挡住一轮里 LLM 重发的同一条调用）。"""
        sent: list = []
        planner = _planner(sent)
        self.assertTrue(planner._tool_come_to_player({"player": "Steve"}, _ctx()))
        self.assertFalse(planner._tool_come_to_player({"player": "Steve"}, _ctx()),
                         "同一格、窗口内的紧邻重复当重复")
        self.assertEqual(sent, ["Steve"])

    def test_player_moving_to_a_new_cell_is_not_suppressed(self):
        """「先走到 A 又要求走到 B」：目标格变了 ⇒ 必须再发一次（不能被静默吞掉）。"""
        sent: list = []
        planner = _planner(sent)
        self.assertTrue(planner._tool_come_to_player({"player": "Steve"}, _ctx(z=0.0)))
        self.assertTrue(planner._tool_come_to_player({"player": "Steve"}, _ctx(z=-40.0)),
                        "玩家挪了格，新请求照发")
        self.assertEqual(sent, ["Steve", "Steve"])

    def test_repeat_after_the_window_is_not_suppressed(self):
        """「到不了之后再叫一次」：隔了几秒 ⇒ 同一格也照发。"""
        sent: list = []
        planner = _planner(sent)
        self.assertTrue(planner._tool_come_to_player({"player": "Steve"}, _ctx()))
        key = planner._come_to_player_last[0]
        planner._come_to_player_last = (key, time.monotonic() - COME_TO_PLAYER_REPEAT_WINDOW - 1.0)
        self.assertTrue(planner._tool_come_to_player({"player": "Steve"}, _ctx()))
        self.assertEqual(sent, ["Steve", "Steve"])

    def test_unknown_cell_never_suppresses(self):
        """快照里没有这个玩家（解析不出目标格）⇒ 绝不压，宁可多发。"""
        sent: list = []
        planner = _planner(sent)
        self.assertTrue(planner._tool_come_to_player({"player": "Ghost"}, {}))
        self.assertTrue(planner._tool_come_to_player({"player": "Ghost"}, {}))
        self.assertEqual(sent, ["Ghost", "Ghost"])


# ── ④ 周期日志压缩 ──────────────────────────────────────────

def _state_payload(**overrides) -> dict:
    payload = {
        "player": {"name": "AI", "health": 20.0, "max_health": 20.0, "hunger": 18,
                   "saturation": 5.0, "x": 10.5, "y": 64.0, "z": -3.2,
                   "dimension": "minecraft:overworld"},
        "world": {"time": 1000, "is_day": True, "server": "srv", "world_name": "w"},
        "players": [{"name": "Steve", "x": 11.0, "y": 64.0, "z": -4.0,
                     "distance": 1.2, "speaking": True}],
        "entities": [{"name": "Cow", "x": 9.0, "y": 64.0, "z": -2.0,
                      "distance": 2.0, "type": "animal"}],
        "nearby_blocks": [{"name": f"B{i}", "x": i, "y": 64, "z": 0,
                           "distance": float(32 - i)} for i in range(32)],
        "nearby_storage": [{"name": "chest", "x": 12.0, "y": 64.0, "z": -5.0, "distance": 3.0}],
        "nearby_beds": [{"x": 13.0, "y": 64.0, "z": -6.0, "distance": 4.0}],
        "remembered_pois": [{"name": "home", "x": 100.0, "y": 70.0, "z": 200.0,
                             "distance": 250.0}],
        "inventory": [{"slot": i, "name": "minecraft:stone", "count": i} for i in range(9)],
    }
    payload.update(overrides)
    return payload


def _full_text(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), default=str)


class PeriodicLogCompressionTests(unittest.TestCase):
    def test_fingerprint_follows_the_printed_view(self):
        """指纹 = **打印内容的哈希**：摘要没变就不打，摘要变了才打（用户实机："得限定"）。

        这取代了旧口径（指纹走完整负载 ⇒ 远端坐标抖一下也刷一行）。
        """
        payload = _state_payload()
        self.assertEqual(event_fingerprint(payload), event_fingerprint(_state_payload()))

        # ① 远处方块的一个坐标动了 ⇒ 摘要里没有它 ⇒ **不算变化**（这正是"限定"）。
        #    注意 B31 是**最近**那条（会被当作样本打印），所以这里动 index 0（远处）。
        far = _state_payload()
        far["nearby_blocks"][0]["z"] = 7
        self.assertEqual(event_fingerprint(payload), event_fingerprint(far),
                         "远端坐标抖动不该再刷日志")

        # ② 位置挪一点点、视角转 1 度 ⇒ 取整 / 剔除后不算变化。
        #    注意取整是"按格"：跨过 .5 边界才算变化，所以这里往下抖（10.5 → 10.45 → 仍是 10.0）。
        jitter = _state_payload()
        jitter["player"]["x"] = jitter["player"]["x"] - 0.05
        jitter["player"]["yaw"] = 42.0
        self.assertEqual(event_fingerprint(payload), event_fingerprint(jitter),
                         "零点几格与朝向抖动不该算变化")

        # ③ 真的变了 ⇒ 必须判得出来：血量、走过一格、附近东西变少。
        hurt = _state_payload()
        hurt["player"]["health"] = 12.0
        self.assertNotEqual(event_fingerprint(payload), event_fingerprint(hurt))
        walked = _state_payload()
        walked["player"]["x"] = 13.4
        self.assertNotEqual(event_fingerprint(payload), event_fingerprint(walked))
        self.assertNotEqual(event_fingerprint(payload),
                            event_fingerprint(_state_payload(
                                nearby_blocks=_state_payload()["nearby_blocks"][:5])))

    def test_printed_content_is_compact_and_keeps_key_fields(self):
        payload = _state_payload()
        obj = {"type": "event", "event": "state_update", "data": payload}
        compact = format_event_content(obj)          # 默认上限（= 实际日志里的那行）
        full = _full_text(payload)

        self.assertLess(len(compact), len(full) * 0.4, "满屏坐标必须被压掉")
        self.assertGreater(len(full) - len(compact), 1000, "压缩量要实打实")
        self.assertLess(len(compact), 500, "单行要短（用户实机：日志被坐标刷屏）")

        # 关键字段仍在（这里把上限调大，避免"被截断"干扰断言；空间标量按格取整 ⇒ 10.5 → 10.0）。
        untruncated = format_event_content(obj, limit=5000)
        for token in ('"health":20.0', '"hunger":18', "minecraft:overworld", '"x":10.0'):
            self.assertIn(token, untruncated)
        # 数组只留条数 + 空间类附最近一条样本（不再是 32 条全量），样本只留身份 + 距离。
        self.assertIn('"nearby_blocks":{"count":32,"nearest"', untruncated)
        self.assertIn('"inventory":{"count":9}', untruncated)
        self.assertIn('"entities":{"count":1,"nearest"', untruncated)
        # 最近一条方块 = distance 最小者（B31，distance=1.0）。
        self.assertIn('"B31"', untruncated)
        self.assertNotIn('"B0"', untruncated, "远的方块只留条数、不再逐条打印")
        self.assertNotIn('"x":11.0', untruncated, "样本里的坐标不再逐字打出来")

    def test_sample_is_the_nearest_when_no_distance_field(self):
        compact = format_event_content({"data": {"entities": [{"name": "A"}, {"name": "B"}]}})
        self.assertIn('"nearest":{"name":"A"', compact)

    def test_non_periodic_classification_is_unchanged(self):
        self.assertFalse(is_periodic_event("response", "state_update"))
        self.assertFalse(is_periodic_event("event", "player_chat"))


class FakeRecvSocket:
    def __init__(self, chunks):
        self._chunks = list(chunks)

    def recv(self, _n):
        return self._chunks.pop(0) if self._chunks else b""

    def close(self):
        pass


class WireLoopCompressionTests(unittest.TestCase):
    """走真实 `_read_loop`：坐标负载重复 → 压掉；关键非周期事件一字不变。"""

    def _run(self, lines):
        wire = WireClient("127.0.0.1", 25568)
        # 本类锁的是"**变了才打** / 压缩"的语义本身，把**最小间隔**关掉（默认 5s）；
        # 最小间隔另有 `tests/test_wire_log_dedupe.py::LogMinIntervalTests` 用假时钟精确验证。
        wire.configure_log_dedupe(min_interval_seconds=0)
        wire.sock = FakeRecvSocket([(line + "\n").encode("utf-8") for line in lines])
        wire._running = True
        with self.assertLogs("wire", level="DEBUG") as logs:
            wire._read_loop()
        return wire, [record.getMessage() for record in logs.records]

    def test_identical_coordinate_state_is_suppressed_then_change_is_compact(self):
        payload = _state_payload()
        moved = _state_payload()
        moved["player"]["z"] = 10.0              # 位置变了（关键字段）→ 该出一行
        lines = [
            json.dumps({"type": "event", "event": "state_update", "data": payload}),
            json.dumps({"type": "event", "event": "state_update", "data": payload}),
            json.dumps({"type": "event", "event": "state_update", "data": moved}),
        ]

        wire, out = self._run(lines)

        self.assertEqual(len(out), 2, "重复的坐标负载只出 1 行，变化再 1 行")
        self.assertLess(len(out[0]), 1500)
        self.assertIn("与上一条相同 ×1", out[1], "被压掉的那条要在下一行交代")
        self.assertEqual(wire._log_deduper.total_events(), 3)

    def test_non_periodic_lines_are_verbatim(self):
        lines = [json.dumps({"type": "response", "id": "req_1", "data": {}}),
                 json.dumps({"type": "event", "event": "player_chat",
                             "data": {"message": "hi"}})]
        _wire, out = self._run(lines)
        self.assertEqual(out, ["← response: req_1", "← event: player_chat"])


if __name__ == "__main__":
    unittest.main()
