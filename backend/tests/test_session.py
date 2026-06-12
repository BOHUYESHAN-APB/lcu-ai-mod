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


if __name__ == "__main__":
    unittest.main()
