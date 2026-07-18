import tempfile
import unittest
from pathlib import Path

from agent.orchestrator import Orchestrator
from protocol import BodyAdapter, BodyEvent


class FakeBody:
    def __init__(self):
        self.is_connected = True
        self.pending: list[BodyEvent] = []
        self.commands: list[tuple[str, dict]] = []

    def connect(self):
        self.is_connected = True
        return True

    def disconnect(self):
        self.is_connected = False

    def send_command(self, command, args=None, request_id=None):
        self.commands.append((command, args or {}))
        return request_id or f"fake-{len(self.commands)}"

    def drain(self):
        events, self.pending = self.pending, []
        return events


class OrchestratorBodyTests(unittest.TestCase):
    def test_fake_body_satisfies_runtime_contract_and_updates_session(self):
        body = FakeBody()
        self.assertIsInstance(body, BodyAdapter)
        body.pending.append(BodyEvent("event", {
            "event": "state_update",
            "data": {
                "player": {"name": "Companion", "health": 18},
                "control_state": {"ai_controlled": False},
            },
        }))

        with tempfile.TemporaryDirectory() as tmp:
            orchestrator = Orchestrator(body, storage_root=Path(tmp), legacy_root=None)
            published = []
            orchestrator.on_event = lambda event_type, data, occurred_at: published.append((event_type, data))
            orchestrator.start()
            orchestrator.tick()

            self.assertEqual(orchestrator.session.runtime["player"]["health"], 18)
            self.assertEqual(body.pending, [])
            self.assertEqual([event[0] for event in published], ["body.connection", "state_update"])
            self.assertTrue(orchestrator.session.get_status()["body"]["connected"])
            orchestrator.on_body_disconnect()
            self.assertFalse(orchestrator.session.get_status()["body"]["connected"])
            orchestrator.session.stop()

    def test_response_and_progress_finalize_fake_body_command(self):
        body = FakeBody()
        with tempfile.TemporaryDirectory() as tmp:
            orchestrator = Orchestrator(body, storage_root=Path(tmp), legacy_root=None)
            orchestrator.session.register_external_command(
                "move_to", "fake-1", {"x": 1, "y": 64, "z": 2}, requester="sdk"
            )
            body.pending.extend([
                BodyEvent("response", {"id": "fake-1", "success": True}),
                BodyEvent("progress", {"id": "fake-1", "progress": 1.0, "message": "arrived"}),
            ])

            orchestrator.start()
            orchestrator.tick()

            outcome = orchestrator.session.memory.task_outcomes[-1]
            self.assertEqual(outcome["command"], "move_to")
            self.assertEqual(outcome["outcome"], "success")
            self.assertEqual(outcome["requester"], "sdk")
            orchestrator.session.stop()


if __name__ == "__main__":
    unittest.main()
