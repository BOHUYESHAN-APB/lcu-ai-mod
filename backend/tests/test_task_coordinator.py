import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent.agent_state import AgentStateDB
from agent.skill_registry import SkillRegistry
from agent.task_coordinator import TaskCoordinator
from protocol import BodyEvent


class FakeBody:
    def __init__(self):
        self.is_connected = True
        self.commands = []

    def connect(self):
        self.is_connected = True
        return True

    def disconnect(self):
        self.is_connected = False

    def send_command(self, command, args=None, request_id=None):
        self.commands.append((command, args or {}))
        return request_id or f"req-{len(self.commands)}"

    def drain(self):
        return []


class TaskCoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = AgentStateDB(Path(self.tmp.name) / "agent_state.db")
        self.state.set_scheduler_clock(None, None, "default")
        self.registry = SkillRegistry()
        self.body = FakeBody()
        self.coordinator = TaskCoordinator(self.state, self.registry, self.body)
        self.coordinator.set_body_armed(True)

    def tearDown(self):
        self.state.close()
        self.tmp.cleanup()

    def test_response_and_progress_produce_durable_run_events(self):
        run = self.coordinator.create_run(
            "general.craft_item", {"item": "minecraft:torch", "count": 4}
        )

        self.assertEqual(run["status"], "dispatched")
        self.coordinator.handle_body_message(BodyEvent("response", {
            "id": run["request_id"], "success": True, "data": {"message": "queued"},
        }))
        self.assertEqual(self.state.get_run(run["id"])["status"], "running")

        self.coordinator.handle_body_message(BodyEvent("progress", {
            "id": run["request_id"], "progress": 1.0, "message": "crafted",
        }))

        restored = self.state.get_run(run["id"])
        self.assertEqual(restored["status"], "succeeded")
        self.assertEqual(restored["progress"], 1.0)
        event_types = [event["type"] for event in self.state.list_events()]
        self.assertEqual(event_types[-4:], ["run.created", "run.dispatched", "run.started", "run.succeeded"])

    def test_disconnect_marks_inflight_run_unknown_without_replay(self):
        run = self.coordinator.create_run("core.move_to", {"x": 1, "y": 64, "z": 2})

        changed = self.coordinator.on_disconnect()

        self.assertEqual(changed, 1)
        self.assertEqual(self.state.get_run(run["id"])["status"], "unknown")
        self.assertEqual(len(self.body.commands), 1)
        queued = self.coordinator.create_run("core.jump", {})
        self.assertEqual(queued["status"], "queued")

    def test_restart_marks_persisted_inflight_run_unknown(self):
        run = self.coordinator.create_run("core.move_to", {"x": 1, "y": 64, "z": 2})

        TaskCoordinator(self.state, self.registry, self.body)

        self.assertEqual(self.state.get_run(run["id"])["status"], "unknown")

    def test_second_run_stays_queued_until_first_is_terminal(self):
        first = self.coordinator.create_run("core.move_to", {"x": 1, "y": 64, "z": 2})
        second = self.coordinator.create_run("core.jump", {})

        self.assertEqual(first["status"], "dispatched")
        self.assertEqual(second["status"], "queued")
        self.assertEqual(len(self.body.commands), 1)

    def test_raw_progress_command_blocks_durable_dispatch_until_terminal_progress(self):
        with self.coordinator.raw_command_guard():
            request_id = self.coordinator.dispatch_raw_command(
                "craft_item", {"item": "minecraft:torch", "count": 4},
            )
        run = self.coordinator.create_run("core.jump", {})

        self.assertTrue(self.coordinator.is_busy())
        self.assertEqual(run["status"], "queued")
        self.coordinator.handle_body_message(BodyEvent("response", {
            "id": request_id, "success": True, "data": {"message": "queued"},
        }))
        self.assertTrue(self.coordinator.is_busy())

        self.coordinator.handle_body_message(BodyEvent("progress", {
            "id": request_id, "progress": 1.0, "message": "crafted",
        }))
        self.coordinator.resume(run["id"])

        self.assertEqual(self.state.get_run(run["id"])["status"], "dispatched")
        self.assertEqual([command for command, _ in self.body.commands], ["craft_item", "jump"])

    def test_reconnect_does_not_implicitly_resume_queued_run(self):
        self.coordinator.set_body_armed(False)
        run = self.coordinator.create_run("core.jump", {})
        self.coordinator.set_body_armed(True)

        with patch("agent.task_coordinator.time.monotonic", return_value=10.0):
            self.coordinator.tick({"world": {}}, "builtin")

        self.assertEqual(self.state.get_run(run["id"])["status"], "queued")
        self.assertEqual(self.body.commands, [])
        self.coordinator.resume(run["id"])
        self.assertEqual(self.state.get_run(run["id"])["status"], "dispatched")

    def test_raw_command_guard_rejects_concurrent_command(self):
        with self.coordinator.raw_command_guard():
            self.coordinator.dispatch_raw_command("jump", {})

        with self.assertRaisesRegex(ValueError, "already executing"):
            with self.coordinator.raw_command_guard():
                pass

    def test_failed_raw_send_releases_admission_and_pending_registration(self):
        registered = []
        removed = []
        with patch.object(self.body, "send_command", side_effect=ConnectionError("offline")):
            with self.assertRaisesRegex(ConnectionError, "offline"):
                with self.coordinator.raw_command_guard():
                    self.coordinator.dispatch_raw_command(
                        "jump", {}, on_reserved=registered.append, on_failed=removed.append,
                    )

        self.assertFalse(self.coordinator.is_busy())
        self.assertEqual(removed, registered)

    def test_wall_interval_schedule_creates_and_dispatches_run(self):
        manifest = self.registry.get("core.jump")
        schedule = self.state.create_schedule({
            "name": "jump check",
            "skill_id": manifest.id,
            "skill_version": manifest.version,
            "input": {},
            "enabled": True,
            "clock": "wall",
            "trigger_type": "interval",
            "misfire_policy": "fire_once",
            "wall_run_at": 100.0,
            "wall_interval_seconds": 60.0,
            "game_interval_ticks": None,
            "time_of_day_tick": None,
            "next_wall_at": 100.0,
            "next_game_tick": None,
        })
        self.coordinator._last_scheduler_tick = 0.0

        with patch("agent.task_coordinator.time.monotonic", return_value=10.0), \
                patch("agent.task_coordinator.time.time", return_value=100.0):
            self.coordinator.tick({"world": {}}, "builtin")

        runs = self.state.list_runs()
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["schedule_id"], schedule["id"])
        self.assertEqual(runs[0]["status"], "dispatched")
        self.assertEqual(self.state.get_schedule(schedule["id"])["next_wall_at"], 160.0)

    def test_schedule_from_another_scope_does_not_fire(self):
        manifest = self.registry.get("core.jump")
        self.state.create_schedule({
            "name": "other world", "skill_id": manifest.id, "skill_version": manifest.version,
            "input": {}, "enabled": True, "clock": "wall", "trigger_type": "interval",
            "misfire_policy": "fire_once", "wall_run_at": 100.0, "wall_interval_seconds": 60.0,
            "game_interval_ticks": None, "time_of_day_tick": None,
            "next_wall_at": 100.0, "next_game_tick": None, "scope_id": "other",
        })

        with patch("agent.task_coordinator.time.monotonic", return_value=10.0), \
                patch("agent.task_coordinator.time.time", return_value=100.0):
            self.coordinator.tick({"world": {}}, "builtin", "default")

        self.assertEqual(self.state.list_runs(), [])

    def test_game_time_of_day_schedule_initializes_then_fires_once(self):
        manifest = self.registry.get("core.jump")
        schedule = self.state.create_schedule({
            "name": "morning jump",
            "skill_id": manifest.id,
            "skill_version": manifest.version,
            "input": {},
            "enabled": True,
            "clock": "game",
            "trigger_type": "time_of_day",
            "misfire_policy": "fire_once",
            "wall_run_at": None,
            "wall_interval_seconds": None,
            "game_interval_ticks": None,
            "time_of_day_tick": 1000,
            "next_wall_at": None,
            "next_game_tick": None,
        })

        with patch("agent.task_coordinator.time.monotonic", side_effect=[10.0, 11.0]):
            self.coordinator.tick({"world": {"game_time": 500, "day_time": 500}}, "builtin")
            self.coordinator.tick({"world": {"game_time": 1000, "day_time": 1000}}, "builtin")

        self.assertEqual(len(self.state.list_runs()), 1)
        self.assertEqual(self.state.get_schedule(schedule["id"])["next_game_tick"], 25000)

    def test_skip_schedule_advances_while_body_is_disconnected(self):
        manifest = self.registry.get("core.jump")
        schedule = self.state.create_schedule({
            "name": "skip offline",
            "skill_id": manifest.id,
            "skill_version": manifest.version,
            "input": {}, "enabled": True, "clock": "wall", "trigger_type": "interval",
            "misfire_policy": "skip", "wall_run_at": 100.0, "wall_interval_seconds": 60.0,
            "game_interval_ticks": None, "time_of_day_tick": None,
            "next_wall_at": 100.0, "next_game_tick": None,
        })
        self.body.is_connected = False

        with patch("agent.task_coordinator.time.monotonic", return_value=10.0), \
                patch("agent.task_coordinator.time.time", return_value=130.0):
            self.coordinator.tick({"world": {}}, "builtin")

        self.assertEqual(self.state.list_runs(), [])
        self.assertEqual(self.state.get_schedule(schedule["id"])["next_wall_at"], 160.0)
        self.assertIn("schedule.misfired", [event["type"] for event in self.state.list_events()])

    def test_skipped_one_shot_is_disabled_atomically(self):
        manifest = self.registry.get("core.jump")
        schedule = self.state.create_schedule({
            "name": "missed once", "skill_id": manifest.id, "skill_version": manifest.version,
            "input": {}, "enabled": True, "clock": "wall", "trigger_type": "once",
            "misfire_policy": "skip", "wall_run_at": 100.0, "wall_interval_seconds": None,
            "game_interval_ticks": None, "time_of_day_tick": None,
            "next_wall_at": 100.0, "next_game_tick": None,
        })
        self.body.is_connected = False

        with patch("agent.task_coordinator.time.monotonic", return_value=10.0), \
                patch("agent.task_coordinator.time.time", return_value=130.0):
            self.coordinator.tick({"world": {}}, "builtin")

        restored = self.state.get_schedule(schedule["id"])
        self.assertFalse(restored["enabled"])
        self.assertIsNone(restored["next_wall_at"])


if __name__ == "__main__":
    unittest.main()
