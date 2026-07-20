import unittest
from types import SimpleNamespace
from unittest.mock import patch

from agent.access_policy import default_access_policy
from agent.planner import SkillProposal
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

    def test_state_update_uses_authoritative_snapshot_collections(self):
        session = Session.__new__(Session)
        session.runtime = {
            "persona": {"name": "Maid"},
            "inventory": [{"slot": 0, "name": "minecraft:bread"}],
            "entities": [{"id": 1, "type": "item"}],
        }

        session.handle_event("state_update", {"player": {"health": 20}})

        self.assertEqual(session.runtime["inventory"], [])
        self.assertEqual(session.runtime["entities"], [])
        self.assertEqual(session.runtime["persona"]["name"], "Maid")

    def test_stop_intent_uses_explicit_words_not_substrings(self):
        self.assertTrue(Session.is_stop_intent("stop now"))
        self.assertTrue(Session.is_stop_intent("先停下"))
        self.assertFalse(Session.is_stop_intent("my stopwatch is ready"))
        self.assertFalse(Session.is_stop_intent("不要停下来"))
        self.assertFalse(Session.is_stop_intent("do not stop now"))

    def test_public_chat_cannot_dispatch_body_skill_without_grant(self):
        session = Session.__new__(Session)
        session.identity = SimpleNamespace(server_id="survival", companion_id="server-ai")
        session._current_requester = ("Bob", "uuid-b")
        session._current_request_channel = "chat.public"
        admitted = []
        session._planner_proposal_dispatcher = lambda proposal: admitted.append(proposal) or True

        with patch("agent.session.load_policy", return_value=default_access_policy()):
            result = session._dispatch_authorized_proposal(SkillProposal("core.move_to", {}, "test"))

        self.assertFalse(result)
        self.assertEqual(admitted, [])


if __name__ == "__main__":
    unittest.main()
