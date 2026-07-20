import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent.agent_state import AgentStateDB
from agent.decision_scheduler import DecisionScheduler
from agent.orchestrator import Orchestrator
from agent.skill_registry import SkillRegistry
from agent.task_coordinator import TaskCoordinator
from protocol import BodyAdapter, BodyEvent
from tests.test_decision_scheduler import FakeLLM as DecisionLLM, ImmediateExecutor


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

    def test_response_progress_and_outcome_finalize_fake_body_command(self):
        body = FakeBody()
        with tempfile.TemporaryDirectory() as tmp:
            orchestrator = Orchestrator(body, storage_root=Path(tmp), legacy_root=None)
            orchestrator.session.register_external_command(
                "move_to", "fake-1", {"x": 1, "y": 64, "z": 2}, requester="sdk"
            )
            body.pending.extend([
                BodyEvent("response", {"id": "fake-1", "success": True}),
                BodyEvent("progress", {"id": "fake-1", "progress": 1.0, "message": "arrived"}),
                BodyEvent("outcome", {"id": "fake-1", "status": "succeeded", "message": "arrived"}),
            ])

            orchestrator.start()
            orchestrator.tick()

            outcome = orchestrator.session.memory.task_outcomes[-1]
            self.assertEqual(outcome["command"], "move_to")
            self.assertEqual(outcome["outcome"], "success")
            self.assertEqual(outcome["requester"], "sdk")
            orchestrator.session.stop()

    def test_malformed_state_event_does_not_block_following_valid_event(self):
        body = FakeBody()
        body.pending.extend([
            BodyEvent("event", {"event": "state_update", "data": "invalid"}),
            BodyEvent("event", {"event": "state_update", "data": {"player": {"health": 17}}}),
        ])
        with tempfile.TemporaryDirectory() as tmp:
            orchestrator = Orchestrator(body, storage_root=Path(tmp), legacy_root=None)
            orchestrator.start()

            orchestrator.tick()

            self.assertEqual(orchestrator.session.runtime["player"]["health"], 17)
            self.assertEqual(orchestrator.session.world_model.invalid_updates, 1)
            orchestrator.session.stop()

    def test_decision_scheduler_dispatches_allowlisted_skill_through_task_coordinator(self):
        body = FakeBody()
        body.pending.extend([
            BodyEvent("event", {"event": "state_update", "data": {
                "player": {"health": 20, "hunger": 8}, "control_state": {"ai_controlled": True},
            }}),
            BodyEvent("event", {"event": "state_update", "data": {
                "player": {"health": 3, "hunger": 5}, "control_state": {"ai_controlled": True},
            }}),
        ])
        with tempfile.TemporaryDirectory() as tmp:
            state = AgentStateDB(Path(tmp) / "agent_state.db")
            state.set_scheduler_clock(None, None, "default\0default")
            orchestrator = Orchestrator(body, storage_root=Path(tmp) / "session", legacy_root=None)
            coordinator = TaskCoordinator(state, SkillRegistry(), body, initial_scope="default\0default")
            orchestrator.set_task_coordinator(coordinator)
            orchestrator.session.llm.is_configured = lambda _agent=None: True
            orchestrator.decision_scheduler.close()
            orchestrator.decision_scheduler = DecisionScheduler(
                DecisionLLM('{"decision":"run_skill","skill_id":"general.eat","input":{},"reason":"critical health"}'),
                executor=ImmediateExecutor(),
            )
            orchestrator.start()

            orchestrator.tick()

            self.assertEqual(body.commands, [("eat", {})])
            self.assertEqual(state.list_runs(root_only=True)[0]["skill_id"], "general.eat")
            self.assertEqual(orchestrator.decision_scheduler.get_status()["history"][-1]["disposition"], "dispatched")
            self.assertEqual(orchestrator.session.pending_decision_triggers(), [])
            decision_events = [event["type"] for event in state.list_events() if event["aggregate_type"] == "decision"]
            self.assertEqual(decision_events, ["decision.requested", "decision.dispatched"])
            orchestrator.close()
            orchestrator.session.stop()
            state.close()

    def test_none_proposal_from_changed_control_epoch_is_resolved_stale(self):
        body = FakeBody()
        with tempfile.TemporaryDirectory() as tmp:
            state = AgentStateDB(Path(tmp) / "agent_state.db")
            orchestrator = Orchestrator(body, storage_root=Path(tmp) / "session", legacy_root=None)
            coordinator = TaskCoordinator(state, SkillRegistry(), body, initial_scope="default\0default")
            orchestrator.set_task_coordinator(coordinator)
            orchestrator.decision_scheduler.close()
            orchestrator.decision_scheduler = DecisionScheduler(
                DecisionLLM('{"decision":"none","reason":"stable"}'), executor=ImmediateExecutor(),
            )
            orchestrator.decision_scheduler.submit(
                [{"sequence": 1}], {}, scope_id="default\0default", body_epoch=0,
                observation_revision=1,
            )
            orchestrator.session.set_control_mode("external", fencing_token=1)

            orchestrator._apply_decision_result()

            history = orchestrator.decision_scheduler.get_status()["history"]
            self.assertEqual(history[-1]["disposition"], "stale")
            self.assertEqual(body.commands, [])
            orchestrator.close()
            orchestrator.session.stop()
            state.close()

    def test_chat_planner_proposal_creates_durable_run_through_coordinator(self):
        body = FakeBody()
        with tempfile.TemporaryDirectory() as tmp:
            state = AgentStateDB(Path(tmp) / "agent_state.db")
            orchestrator = Orchestrator(body, storage_root=Path(tmp) / "session", legacy_root=None)
            coordinator = TaskCoordinator(state, SkillRegistry(), body, initial_scope="default\0default")
            coordinator.set_body_armed(True)
            orchestrator.set_task_coordinator(coordinator)

            response = orchestrator.session.planner._execute_plan(
                'reply(马上做)\ntool(craft_item, {"item":"minecraft:torch","count":4})',
                sender="Alice", message="做四个火把", context={},
            )

            self.assertEqual(response, "马上做")
            self.assertEqual(body.commands, [("craft_item", {"item": "minecraft:torch", "count": 4})])
            run = state.list_runs(root_only=True)[0]
            self.assertEqual((run["skill_id"], run["status"]), ("general.craft_item", "dispatched"))
            self.assertIn("planner.proposal_admitted", [event["type"] for event in state.list_events()])
            orchestrator.close()
            orchestrator.session.stop()
            state.close()

    def test_chat_planner_rejects_non_durable_follow_without_body_command(self):
        body = FakeBody()
        with tempfile.TemporaryDirectory() as tmp:
            state = AgentStateDB(Path(tmp) / "agent_state.db")
            orchestrator = Orchestrator(body, storage_root=Path(tmp) / "session", legacy_root=None)
            coordinator = TaskCoordinator(state, SkillRegistry(), body, initial_scope="default\0default")
            coordinator.set_body_armed(True)
            orchestrator.set_task_coordinator(coordinator)

            orchestrator.session.planner._execute_plan(
                'tool(follow, {"player":"Alice"})', sender="Alice", message="跟着我", context={},
            )

            self.assertEqual(body.commands, [])
            self.assertEqual(state.list_runs(root_only=True), [])
            self.assertIn("durable", orchestrator.session.planner.get_status()["last_protocol_error"])
            self.assertEqual(
                [event["type"] for event in state.list_events() if event["aggregate_type"] == "planner"],
                ["planner.proposal_rejected"],
            )
            orchestrator.close()
            orchestrator.session.stop()
            state.close()

    def test_stop_intent_preempts_active_coordinator_run(self):
        body = FakeBody()
        with tempfile.TemporaryDirectory() as tmp:
            state = AgentStateDB(Path(tmp) / "agent_state.db")
            orchestrator = Orchestrator(body, storage_root=Path(tmp) / "session", legacy_root=None)
            coordinator = TaskCoordinator(state, SkillRegistry(), body, initial_scope="default\0default")
            coordinator.set_body_armed(True)
            orchestrator.set_task_coordinator(coordinator)
            coordinator.create_run("core.move_to", {"x": 1, "y": 64, "z": 2})

            accepted = orchestrator.session.dispatch_stop_intent()

            self.assertTrue(accepted)
            self.assertEqual([command for command, _ in body.commands], ["move_to", "stop_all"])
            self.assertIn("control.stop_requested", [event["type"] for event in state.list_events()])
            orchestrator.close()
            orchestrator.session.stop()
            state.close()

    def test_priority_body_chat_stop_is_admitted_before_tick_drain(self):
        body = FakeBody()
        with tempfile.TemporaryDirectory() as tmp:
            state = AgentStateDB(Path(tmp) / "agent_state.db")
            orchestrator = Orchestrator(body, storage_root=Path(tmp) / "session", legacy_root=None)
            coordinator = TaskCoordinator(state, SkillRegistry(), body, initial_scope="default\0default")
            coordinator.set_body_armed(True)
            orchestrator.set_task_coordinator(coordinator)
            coordinator.create_run("core.move_to", {"x": 1, "y": 64, "z": 2})
            event = BodyEvent("event", {
                "event": "player_chat",
                "data": {"sender": "Alice", "message": "先停下", "is_system": False},
            })

            with patch.object(orchestrator.session, "check_chat_skill_permission", return_value=True):
                accepted = orchestrator.handle_priority_body_event(event)

            self.assertTrue(accepted)
            self.assertEqual([command for command, _ in body.commands], ["move_to", "stop_all"])
            self.assertTrue(event.data["data"]["_lcu_priority_stop_admitted"])
            orchestrator.close()
            orchestrator.session.stop()
            state.close()

    def test_decision_scheduler_rejects_skill_outside_automatic_allowlist(self):
        body = FakeBody()
        with tempfile.TemporaryDirectory() as tmp:
            state = AgentStateDB(Path(tmp) / "agent_state.db")
            orchestrator = Orchestrator(body, storage_root=Path(tmp) / "session", legacy_root=None)
            coordinator = TaskCoordinator(state, SkillRegistry(), body, initial_scope="default\0default")
            orchestrator.set_task_coordinator(coordinator)
            orchestrator.decision_scheduler.close()
            orchestrator.decision_scheduler = DecisionScheduler(
                DecisionLLM('{"decision":"run_skill","skill_id":"general.craft_item","input":{"item":"minecraft:tnt","count":1}}'),
                executor=ImmediateExecutor(),
            )
            orchestrator.decision_scheduler.submit(
                [{"sequence": 1}], {}, scope_id="default\0default", body_epoch=0,
                observation_revision=1, submitted_at=10,
            )

            orchestrator._apply_decision_result()

            self.assertEqual(body.commands, [])
            self.assertEqual(orchestrator.decision_scheduler.get_status()["history"][-1]["disposition"], "rejected")
            orchestrator.close()
            orchestrator.session.stop()
            state.close()


if __name__ == "__main__":
    unittest.main()
