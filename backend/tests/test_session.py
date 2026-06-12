import unittest

from agent.session import Session


class SessionTests(unittest.TestCase):
    def test_state_update_only_updates_runtime(self):
        session = Session.__new__(Session)
        session.runtime = {"player": {"x": 0}}

        session.handle_event("state_update", {"player": {"x": 12}, "world": {"time": 6000}})

        self.assertEqual(session.runtime["player"]["x"], 12)
        self.assertEqual(session.runtime["world"]["time"], 6000)

    def test_manual_behavior_active_detects_follow_task(self):
        session = Session.__new__(Session)
        session.runtime = {"behavior_state": {"follow_target": "BoHuYeShan"}}
        session._manual_task_kind = "follow_player"

        self.assertTrue(Session._manual_behavior_active(session))

    def test_task_state_event_is_stored_in_runtime(self):
        session = Session.__new__(Session)
        session.runtime = {}
        session._manual_task_kind = None
        session._manual_action_reqs = set()

        session.handle_event("task_state", {"kind": "craft", "status": "collecting", "target": "wooden_sword"})

        self.assertEqual(session.runtime["task_state"]["kind"], "craft")
        self.assertEqual(session.runtime["task_state"]["status"], "collecting")


if __name__ == "__main__":
    unittest.main()
