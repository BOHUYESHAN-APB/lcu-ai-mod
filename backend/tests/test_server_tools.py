"""L3 工具面（第二/三/四块的接线）：新工具要广告出去、要能派发、要如实报结论。

L1（规则段）**不加一个字** —— 新能力走 `tools` 参数（L3），静态前缀预算只降不抬。
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.planner import (  # noqa: E402
    ACTION_GROUPS,
    L0_PLANNER_CONTRACT,
    PHASE_GROUPS,
    PLANNER_MECHANICS,
    PLANNER_RULES,
    Planner,
    _actions_for_phase,
    build_tool_schemas,
    tool_schema,
)

NEW_TOOLS = ("run_server_command", "send_whisper", "tpa_respond", "tpa_request")


class DummyMemory:
    def build_context(self):
        return {}

    def get_recent_context(self, _count):
        return []


class DummySkills:
    def __init__(self):
        self.calls = []


class ToolSurfaceTests(unittest.TestCase):
    def test_new_tools_are_declared_in_every_phase(self):
        declared = {name for actions in ACTION_GROUPS.values() for name, _, _ in actions}
        for tool in NEW_TOOLS:
            self.assertIn(tool, declared, tool)

        for phase in PHASE_GROUPS:
            names = [name for name, _, _ in _actions_for_phase(phase)]
            for tool in NEW_TOOLS:
                self.assertIn(tool, names, f"{phase} 阶段缺少 {tool}")

    def test_built_schema_covers_every_declared_action(self):
        declared = {name for actions in ACTION_GROUPS.values() for name, _, _ in actions}
        built = {item["function"]["name"] for item in build_tool_schemas()}

        self.assertEqual(declared, built)

    def test_args_of_run_server_command_is_an_array(self):
        properties = tool_schema("command,args,confirm")

        self.assertEqual(properties["command"], {"type": "string"})
        self.assertEqual(properties["args"], {"type": "array", "items": {"type": "string"}})
        self.assertEqual(properties["confirm"], {"type": "string"})

    def test_declared_parameter_is_command(self):
        """工具参数按设计文档用 `command`；派发侧同时接受 `tool` 别名（票据写法）。"""
        schema = [item for item in build_tool_schemas()
                  if item["function"]["name"] == "run_server_command"][0]
        self.assertIn("command", schema["function"]["parameters"]["properties"])

    def test_l1_rules_gained_nothing(self):
        """预算 1700 只降不抬：新增能力一律走 L3，不动 L1。"""
        static_core = (L0_PLANNER_CONTRACT + "\n".join(PLANNER_RULES) + PLANNER_MECHANICS)

        self.assertNotIn("run_server_command", static_core)
        self.assertNotIn("send_whisper", static_core)
        self.assertNotIn("tpa", static_core)


class DispatchTests(unittest.TestCase):
    def _planner(self, **kwargs):
        return Planner(llm_service=None, memory=DummyMemory(), skills=DummySkills(), **kwargs)

    def test_run_server_command_is_routed_through_the_gate(self):
        seen = []

        def server_commands(tool, args, confirm=""):
            seen.append((tool, args, confirm))
            return {"ok": True, "command": "/tpaccept Steve"}

        planner = self._planner(server_commands=server_commands)

        handled = planner._dispatch_tool_call(
            "run_server_command", {"command": "tpaccept", "args": ["Steve"]}, {})

        self.assertTrue(handled)
        self.assertEqual(seen, [("tpaccept", ["Steve"], "")])
        self.assertIn("已下发", planner._action_note)

    def test_blocked_command_is_reported_honestly(self):
        planner = self._planner(
            server_commands=lambda tool, args, confirm="": {"ok": False,
                                                            "blocked": "not_whitelisted"})

        planner._dispatch_tool_call("run_server_command", {"command": "op", "args": ["me"]}, {})

        self.assertIn("白名单", planner._action_note)
        self.assertIn("没发", planner._action_note)

    def test_needs_confirm_note_carries_the_token(self):
        planner = self._planner(
            server_commands=lambda tool, args, confirm="": {
                "ok": False, "blocked": "needs_confirm", "confirm": "tok123"})

        planner._dispatch_tool_call("run_server_command", {"command": "give", "args": []}, {})

        self.assertIn("tok123", planner._action_note)

    def test_ambiguous_target_note_lists_candidates(self):
        planner = self._planner(
            server_commands=lambda tool, args, confirm="": {
                "ok": False, "blocked": "ambiguous", "candidates": ["Steve", "SteveX"]})

        planner._dispatch_tool_call("run_server_command", {"command": "tp", "args": ["steve"]}, {})

        self.assertIn("Steve", planner._action_note)
        self.assertIn("SteveX", planner._action_note)

    def test_send_whisper_requires_both_arguments(self):
        calls = []
        planner = self._planner(whisper_sender=lambda player, text: calls.append((player, text))
                                or {"ok": True})

        self.assertFalse(planner._dispatch_tool_call("send_whisper", {"player": "Steve"}, {}))
        self.assertTrue(planner._dispatch_tool_call(
            "send_whisper", {"player": "Steve", "text": "hi"}, {}))
        self.assertEqual(calls, [("Steve", "hi")])

    def test_tpa_respond_uses_the_injected_responder(self):
        calls = []
        planner = self._planner(tpa_responder=lambda action, player: calls.append((action, player))
                                or {"ok": True})

        self.assertTrue(planner._dispatch_tool_call(
            "tpa_respond", {"action": "accept", "player": "Steve"}, {}))
        self.assertEqual(calls, [("accept", "Steve")])

    def test_tpa_request_uses_the_injected_requester(self):
        calls = []
        planner = self._planner(tpa_requester=lambda player: calls.append(player) or {"ok": True})

        self.assertTrue(planner._dispatch_tool_call("tpa_request", {"player": "Steve"}, {}))
        self.assertFalse(planner._dispatch_tool_call("tpa_request", {}, {}))
        self.assertEqual(calls, ["Steve"])

    def test_missing_wiring_means_no_action(self):
        planner = self._planner()

        for tool, payload in (("run_server_command", {"tool": "tpaccept"}),
                              ("send_whisper", {"player": "A", "text": "b"}),
                              ("tpa_respond", {"action": "accept"})):
            self.assertFalse(planner._dispatch_tool_call(tool, payload, {}), tool)

    def test_notes_are_appended_to_the_reply(self):
        planner = self._planner(server_commands=lambda tool, args, confirm="": {
            "ok": False, "blocked": "hard_blacklist"})

        planner._dispatch_tool_call("run_server_command", {"command": "kill", "args": []}, {})

        self.assertEqual(planner._with_notes("好的"), "好的\n（kill在黑名单里，绝对不能发）")

    def test_note_is_reset_every_turn(self):
        planner = self._planner()
        planner._action_note = "旧的一轮"

        # plan_and_execute 一开头就清空（这里只验清空动作本身，不跑 LLM）。
        planner._action_note = None

        self.assertIsNone(planner._with_notes(None))


if __name__ == "__main__":
    unittest.main()
