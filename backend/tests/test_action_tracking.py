"""Closed action-loop tracking: commands sent to the mod must occupy the
action manager until a real completion signal arrives (wire response for
instant commands, task_state terminal status for long-running tasks)."""

import time
import unittest
from types import SimpleNamespace

from agent.action_manager import ActionManager
from agent.session import Session


def _bare_session() -> Session:
    s = Session.__new__(Session)
    s.action_manager = ActionManager()
    s.self_prompter = SimpleNamespace(mark_action=lambda: None)
    s.runtime = {}
    s._manual_action_reqs = set()
    s._manual_task_kind = None
    s._manual_command_until = 0.0
    s._task_idle_since = None
    return s


class ActionTrackingTests(unittest.TestCase):
    def test_instant_command_completes_on_response(self):
        s = _bare_session()
        s._on_skill_command("jump", "req_1", "mode", {})
        self.assertTrue(s.action_manager.is_busy)
        self.assertEqual(s.action_manager.current_label, "jump")

        s.handle_event("command_response", {"id": "req_1", "success": True})
        self.assertFalse(s.action_manager.is_busy)

    def test_long_command_ignores_queued_response(self):
        s = _bare_session()
        s._on_skill_command("craft_item", "req_2", "mode", {})
        # The "Crafting queued" success response must NOT release the slot
        s.handle_event("command_response", {"id": "req_2", "success": True})
        self.assertTrue(s.action_manager.is_busy)

    def test_long_command_completes_via_task_state_grace(self):
        s = _bare_session()
        s._on_skill_command("craft_item", "req_3", "mode", {})
        s.handle_event("task_state", {"kind": "craft", "status": "collecting"})
        self.assertTrue(s.action_manager.is_busy)

        # Intermediate clear between craft steps must not complete either
        s.handle_event("task_state", {"kind": "craft", "status": "idle"})
        s._complete_long_task_if_idle()
        self.assertTrue(s.action_manager.is_busy)

        # Terminal idle sustained past the grace window → done
        s._task_idle_since = time.time() - 5.0
        s._complete_long_task_if_idle()
        self.assertFalse(s.action_manager.is_busy)

    def test_second_long_command_ignored_while_busy(self):
        s = _bare_session()
        s._on_skill_command("craft_item", "req_a", "mode", {})
        first = s.action_manager.current

        s._on_skill_command("collect_blocks", "req_b", "mode", {})

        self.assertIs(s.action_manager.current, first)
        self.assertEqual(first.req_id, "req_a")

    def test_stop_all_frees_running_action(self):
        s = _bare_session()
        s._on_skill_command("move_to", "req_c", "mode", {})
        self.assertTrue(s.action_manager.is_busy)

        s._on_skill_command("stop_all", "req_d", "mode", {})

        self.assertEqual(s.action_manager.current_label, "stop_all")


if __name__ == "__main__":
    unittest.main()
