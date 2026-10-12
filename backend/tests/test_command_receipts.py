"""模组**回执**必须变成可见事实（日志 + 进 L2），"发出去了" ≠ "做成了"。

真机黑洞（2026-10-12 21:23，`come_to_player id=req_2`）：模组日志里明明有
`[WireServer] Received command: come_to_player` + `[Action] Processing: …`，
后端日志却只有 `← response: req_2` —— **受理了还是被拒、为什么被拒，一个字都没有**
⇒ 用户原话"至于为什么没有调用成功，这件事情不好确定"。

本文件钉住这条**通用路径**（不是只修某一条命令）：
    ① 回执逐条落日志：命令名 + id + 成败 + 原因（原因取模组给的字段，超长如实截断）；
    ② 回执进 L2：模型下一轮就能看到"被拒了、为什么被拒"（每条只看一次，不刷屏）；
    ③ 命令名靠 `req_id → 命令名` 表对上（回执里只有 id）；对不上就如实写"未登记"；
    ④ 失败原因进在途动作的终态（不再只剩一个 `command failed: req_x`）。
"""

import time
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from agent.action_manager import ActionManager
from agent.planner import _command_receipts_section
from agent.session import Session
from agent.territory import TerritoryStore

SERVER = "play.example:25565"
WORLD = "world-a"
OVERWORLD = "minecraft:overworld"


class DummyMemory:
    def build_context(self):
        return {}

    def get_recent_context(self, _count=5):
        return []


class FakeWire:
    """假 wire：只管发号 + 记名字（真 WireClient 在发号点做的就是这件事）。"""

    def __init__(self):
        self._counter = 0
        self.names = {}

    def send_command(self, cmd, args=None):
        self._counter += 1
        req_id = f"req_{self._counter}"
        self.names[req_id] = cmd
        return req_id

    def command_name_for(self, req_id):
        return self.names.get(str(req_id), "")


def bare_session() -> Session:
    """只搭回执这条路要的那几样。"""
    session = Session.__new__(Session)
    session.wire = FakeWire()
    session.runtime = {"player": {}, "world": {}, "players": []}
    session._command_receipts = []
    return session


class ReceiptAbsorbTests(unittest.TestCase):
    def test_the_command_name_is_resolved_from_the_wire(self):
        session = bare_session()
        req = session.wire.send_command("come_to_player", {"player": "BoHuYeShan"})

        row = session._absorb_command_receipt(
            {"id": req, "success": False,
             "data": {"message": "Player BoHuYeShan not found"},
             "error": "Player BoHuYeShan not found"})

        self.assertEqual(row["command"], "come_to_player",
                         "回执里只有 id，命令名要从发号的那一端对上")
        self.assertEqual(row["detail"], "Player BoHuYeShan not found")

    def test_a_successful_receipt_is_kept_as_a_fact(self):
        session = bare_session()
        req = session.wire.send_command("move_to", {"x": 851.0})

        row = session._absorb_command_receipt(
            {"id": req, "success": True, "data": {"message": "Walking to 851,63,-2191"}})

        self.assertEqual(row["command"], "move_to")
        self.assertTrue(row["success"])
        self.assertIn("Walking to", row["detail"])

    def test_an_unknown_request_id_is_reported_honestly(self):
        session = bare_session()

        row = session._absorb_command_receipt({"id": "req_9", "success": False})

        self.assertEqual(row["command"], "未登记", "对不上命令名就如实说未登记，不猜")

    def test_a_command_sent_outside_skills_is_still_named(self):
        """网页 `/ws` 的 `{"type":"command"}` 也走 wire 发号 ⇒ 照样认得出名字。"""
        session = bare_session()
        req = session.wire.send_command("set_control_state", {"control": "forward"})

        row = session._absorb_command_receipt({"id": req, "success": True})

        self.assertEqual(row["command"], "set_control_state")

    def test_the_reason_falls_back_through_the_mod_fields(self):
        session = bare_session()
        req = session.wire.send_command("mine_block", {})

        for payload, expected in (
                ({"error": "硬拦：区域保护"}, "硬拦：区域保护"),
                ({"data": {"detail": "no path"}}, "no path"),
                ({"data": {"reason": "blocked_by_zone"}}, "blocked_by_zone"),
        ):
            row = session._absorb_command_receipt({"id": req, "success": False, **payload})
            self.assertEqual(row["detail"], expected)

    def test_a_long_reason_is_kept_but_marked_as_truncated(self):
        session = bare_session()
        req = session.wire.send_command("go_to_block", {})

        row = session._absorb_command_receipt(
            {"id": req, "success": False, "error": "x" * 500})

        self.assertLess(len(row["detail"]), 260)
        self.assertIn("已截断", row["detail"], "超长要如实说截断了，不许静默丢")

    def test_the_ledger_is_bounded(self):
        session = bare_session()
        for index in range(50):
            session._absorb_command_receipt({"id": f"req_{index}", "success": True})

        self.assertEqual(len(session._command_receipts), 12)
        self.assertEqual(session._command_receipts[-1]["id"], "req_49", "最新的一定留着")


class WireRegistryTests(unittest.TestCase):
    def test_the_wire_remembers_which_command_a_request_id_was(self):
        from protocol.wire_client import MAX_REMEMBERED_COMMANDS, WireClient

        wire = WireClient.__new__(WireClient)
        wire._req_commands = {}
        wire._id_counter = 0
        wire._send_lock = None
        wire._send_line = lambda _line: None

        req = wire.send_command("move_to", {"x": 1.0})

        self.assertEqual(wire.command_name_for(req), "move_to")
        self.assertEqual(wire.command_name_for("req_404"), "", "不知道就空串，绝不猜")

        for index in range(MAX_REMEMBERED_COMMANDS * 2):
            wire.send_command("send_chat", {"message": str(index)})
        self.assertLessEqual(len(wire._req_commands), MAX_REMEMBERED_COMMANDS,
                             "名字表必须有界")
        self.assertEqual(wire.command_name_for(req), "", "最早的那批被如实淘汰（不留假记录）")


class ReceiptContextTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def session(self) -> Session:
        """够 `_runtime_context` 用的最小接线（照仓库里 `Session.__new__` 的测试惯例）。"""
        session = Session.__new__(Session)
        session.runtime = {"player": {}, "world": {}, "players": []}
        session.memory = DummyMemory()
        session.vision = SimpleNamespace(latest=lambda: "")
        session.poi = SimpleNamespace(describe=lambda *a, **k: "", find=lambda *a, **k: [])
        session.territories = TerritoryStore(root=self.root / "territory", server=SERVER,
                                             world_name=WORLD, dimension=OVERWORLD)
        session.last_speaker = {}
        session.chat_inbox = None
        session.conversation = None
        session.llm_active = lambda: True
        session._actor = {"name": "Owner", "tier": "master"}
        session._zone_blocks = []
        session._home_away_since = None
        session.wire = FakeWire()
        session._command_receipts = []
        return session

    def test_the_receipts_reach_the_model_context(self):
        session = self.session()
        req = session.wire.send_command("come_to_player", {"player": "BoHuYeShan"})
        session._absorb_command_receipt({"id": req, "success": False,
                                         "error": "Player BoHuYeShan not found"})

        context = session._runtime_context()

        self.assertEqual(len(context["command_receipts"]), 1)
        row = context["command_receipts"][0]
        self.assertEqual(row["command"], "come_to_player")
        self.assertFalse(row["success"])
        self.assertIn("not found", row["detail"])
        self.assertNotIn("command_receipts", session.runtime, "只在上下文里露一次，不污染 runtime")


class ReceiptL2Tests(unittest.TestCase):
    def test_only_receipts_the_model_has_not_seen_are_shown(self):
        rows = [{"command": "move_to", "id": "req_1", "success": False,
                 "detail": "no path", "at": 100.0}]

        text, cursor = _command_receipts_section({"command_receipts": rows}, 0.0)

        self.assertIn("move_to", text)
        self.assertIn("no path", text)
        self.assertIn("**被拒/失败**", text)
        self.assertIn("不等于", text, "必须带上「发出去了≠做成了」的口径")
        self.assertEqual(cursor, 100.0)

        again, _ = _command_receipts_section({"command_receipts": rows}, cursor)
        self.assertEqual(again, "", "给过一次就不再刷屏")

    def test_a_success_is_not_dressed_up_as_a_failure(self):
        text, _ = _command_receipts_section(
            {"command_receipts": [{"command": "use_on", "id": "req_3", "success": True,
                                   "detail": "", "at": time.time()}]}, 0.0)

        self.assertIn("模组回执**成功**", text)
        self.assertNotIn("**被拒/失败**", text)
        self.assertIn("刚刚", text)

    def test_a_missing_reason_is_said_out_loud(self):
        text, _ = _command_receipts_section(
            {"command_receipts": [{"command": "move_to", "id": "req_4", "success": False,
                                   "detail": "", "at": time.time()}]}, 0.0)

        self.assertIn("模组没给原因", text, "没原因就如实说没原因")

    def test_no_receipts_means_no_section_at_all(self):
        self.assertEqual(_command_receipts_section({}, 0.0), ("", 0.0))
        self.assertEqual(_command_receipts_section({"command_receipts": []}, 0.0), ("", 0.0))


class WireLogTests(unittest.TestCase):
    """`← response:` 那一行本身（模组原始回执）—— 以前只有 `← response: req_2`。"""

    def test_the_raw_receipt_line_carries_the_command_name_and_the_reason(self):
        from protocol.wire_client import format_response_summary

        line = format_response_summary(
            {"type": "response", "id": "req_2", "success": False,
             "data": {"message": "Player BoHuYeShan not found"},
             "error": "Player BoHuYeShan not found", "ts": 1},
            "come_to_player")

        self.assertIn("come_to_player", line, "没命令名就只剩一个 req id（这就是老黑洞）")
        self.assertIn("id=req_2", line)
        self.assertIn("success=否", line)
        self.assertIn("BoHuYeShan not found", line)

    def test_an_unknown_command_is_labelled_as_such(self):
        from protocol.wire_client import format_response_summary

        line = format_response_summary({"id": "req_8", "success": True, "data": {}}, "")

        self.assertTrue(line.startswith("未登记"), "认不出就如实写未登记，不猜")

    def test_a_refusal_without_any_reason_says_so(self):
        from protocol.wire_client import format_response_summary

        line = format_response_summary({"id": "req_5", "success": False, "data": {}}, "move_to")

        self.assertIn("模组没给", line)

    def test_extra_payload_fields_are_kept_but_bounded(self):
        from protocol.wire_client import format_response_summary

        line = format_response_summary(
            {"id": "req_6", "success": False, "data": {"detail": "no path",
                                                        "blocked_by_zone": "农场" * 400}},
            "move_to")

        self.assertIn("no path", line)
        self.assertIn("blocked_by_zone", line)
        self.assertLess(len(line), 600)


class ActionOutcomeTests(unittest.TestCase):
    def test_the_mods_reason_lands_in_the_action_outcome(self):
        manager = ActionManager()
        manager.track("come_to_player", "req_2", timeout=20.0, complete_on_response=False)

        manager.handle_response("req_2", False, detail="Player not found")

        self.assertEqual(manager.last_outcome["come_to_player"]["error"], "Player not found",
                         "失败原因不能用 `command failed: req_2` 糊过去")

    def test_a_response_for_an_untracked_command_still_does_not_crash(self):
        manager = ActionManager()

        manager.handle_response("req_9", False, detail="whatever")

        self.assertIsNone(manager.current)


if __name__ == "__main__":
    unittest.main()
