import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent.memory import Memory
from agent.session import Session


class FakeBody:
    is_connected = False

    def connect(self):
        return False

    def disconnect(self):
        pass

    def send_command(self, command, args=None, request_id=None):
        return request_id or f"req-{command}"

    def drain(self):
        return []


class MemoryTests(unittest.TestCase):
    def test_old_schema_loads_with_structured_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "memory.json"
            path.write_text(json.dumps({"recent_messages": [{"message": "legacy"}]}), encoding="utf-8")

            memory = Memory(path)
            memory.save()
            saved = json.loads(path.read_text(encoding="utf-8"))

            self.assertEqual(saved["schema_version"], 5)
            self.assertEqual(memory.recent_messages[0]["message"], "legacy")
            self.assertEqual(memory.player_relationships, {})
            self.assertEqual(memory.task_outcomes, [])

    def test_invalid_structured_fields_fall_back_to_safe_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "memory.json"
            path.write_text(json.dumps({"schema_version": 4, "experiences": None}), encoding="utf-8")

            memory = Memory(path)
            memory.observe_world({"player": {}, "entities": []})
            original = path.read_text(encoding="utf-8")
            memory.save()

            self.assertIsInstance(memory.experiences, dict)
            self.assertIn("worlds", memory.experiences)
            self.assertEqual(path.read_text(encoding="utf-8"), original)

    def test_future_schema_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "memory.json"
            original = json.dumps({"schema_version": 99, "future_data": {"keep": True}})
            path.write_text(original, encoding="utf-8")

            memory = Memory(path)
            memory.add_interaction("Alice", "runtime only")
            memory.save()

            self.assertEqual(path.read_text(encoding="utf-8"), original)

    def test_world_observation_aggregates_without_creating_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            memory = Memory(Path(tmp) / "memory.json", server_id="server-a", world_id="world-a")
            state = {
                "player": {"x": 1, "y": 64, "z": 2, "dimension": "minecraft:overworld"},
                "entities": [{"type": "player", "name": "Alice"}],
            }

            memory.observe_world(state)
            memory.observe_world(state)

            world = memory.experiences["worlds"]["server-a\u0000world-a"]
            self.assertEqual(world["last_position"]["x"], 1)
            self.assertEqual(memory.experiences["servers"]["server-a"]["known_players"], ["Alice"])
            self.assertEqual(memory.events, [])

    def test_relationship_and_task_outcome_are_persistent(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "memory.json"
            memory = Memory(path, server_id="server-a", world_id="world-a")
            memory.observe_player("Alice", "uuid-a", "hello")
            memory.record_task_outcome(
                "craft_item", "success", target="stone_pickaxe", requester="Alice", requester_id="uuid-a",
            )
            memory.save()

            restored = Memory(path, server_id="server-a", world_id="world-a")
            relationship = restored.player_relationships["uuid:uuid-a"]
            self.assertEqual(relationship["message_count"], 1)
            self.assertEqual(relationship["task_outcomes"]["success"], 1)
            self.assertEqual(restored.task_outcomes[-1]["target"], "stone_pickaxe")

    def test_dirty_memory_is_flushed_once_after_bounded_interval(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "memory.json"
            memory = Memory(path)
            memory.observe_player("Alice", "uuid-a", "hello")

            self.assertTrue(memory.is_dirty)
            self.assertFalse(memory.flush_if_due(interval=60.0))
            self.assertFalse(path.exists())

            memory._last_save -= 61.0
            self.assertTrue(memory.flush_if_due(interval=60.0))
            self.assertFalse(memory.is_dirty)
            first_contents = path.read_text(encoding="utf-8")

            self.assertFalse(memory.flush_if_due(interval=0.0))
            self.assertEqual(path.read_text(encoding="utf-8"), first_contents)

    def test_high_value_memory_shortens_world_flush_deadline(self):
        with tempfile.TemporaryDirectory() as tmp:
            memory = Memory(Path(tmp) / "memory.json")
            memory.observe_world({"player": {}, "entities": []})
            world_deadline = memory._flush_deadline

            memory.observe_player("Alice", "uuid-a", "hello")

            self.assertIsNotNone(world_deadline)
            self.assertLess(memory._flush_deadline, world_deadline)
            self.assertLessEqual(memory._flush_deadline - memory._last_save, memory.FLUSH_INTERVAL + 0.1)

    def test_blocked_memory_does_not_retry_scheduled_flush(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "memory.json"
            path.write_text(json.dumps({"schema_version": 99}), encoding="utf-8")
            memory = Memory(path)
            memory.observe_player("Alice", "uuid-a", "hello")
            memory._flush_deadline = 0.0

            with patch("agent.memory.logger.error") as error_log:
                self.assertFalse(memory.flush_if_due())
                self.assertFalse(memory.flush_if_due())

            error_log.assert_not_called()

    def test_session_tick_flushes_terminal_task_outcome_when_due(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session = Session(FakeBody(), companion_id="memory-test", storage_root=root, legacy_root=None)
            session.runtime["control_state"] = {"ai_controlled": False}
            session.register_external_command("jump", "req-sdk", {}, requester="sdk")
            session.handle_event("command_response", {"id": "req-sdk", "success": True})
            session.memory._flush_deadline = 0.0

            session.tick()

            restored = Memory(session.memory.path)
            self.assertEqual(restored.task_outcomes[-1]["command"], "jump")
            self.assertEqual(restored.task_outcomes[-1]["outcome"], "success")
            session.stop()

    def test_context_is_deterministic_bounded_and_prioritizes_current_player(self):
        with tempfile.TemporaryDirectory() as tmp:
            memory = Memory(Path(tmp) / "memory.json")
            memory.observe_player("Bob", "uuid-b", "b" * 200)
            memory.observe_player("Alice", "uuid-a", "a" * 200)

            first = memory.build_context(current_player="Alice", max_chars=120)
            second = memory.build_context(current_player="Alice", max_chars=120)

            self.assertEqual(first, second)
            self.assertLessEqual(sum(len(value) for value in first.values()), 120)
            self.assertTrue(first["relationship_summary"].startswith("Alice:"))

    def test_durable_summary_is_persistent_and_injected_into_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "memory.json"
            memory = Memory(path)
            memory.add_summary({
                "id": "summary-one",
                "title": "Mining plan",
                "content": "Alice needs diamonds near the saved mine.",
                "source_ids": ["message:1"],
            })
            memory.save()

            restored = Memory(path)
            context = restored.build_context(max_chars=1000)

            self.assertEqual(restored.summaries[0]["id"], "summary-one")
            self.assertIn("Alice needs diamonds", context["durable_summaries"])

    def test_working_context_is_not_persisted_and_preferences_are_durable(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "memory.json"
            memory = Memory(path)
            memory.remember_preference("Alice", "uuid-a", "food", "vegetarian", confidence=0.8)
            memory.save()
            restored = Memory(path)

            context = restored.build_context(
                current_player="Alice", player_id="uuid-a",
                working_context=[{"role": "user", "content": "temporary request"}],
            )
            self.assertIn("temporary request", context["working_context"])
            self.assertIn("vegetarian", context["player_preferences"])
            self.assertNotIn("working_context", json.loads(path.read_text(encoding="utf-8")))

    def test_long_task_is_only_recorded_after_outcome(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = Session(FakeBody(), companion_id="memory-test", storage_root=Path(tmp), legacy_root=None)
            session._current_requester = ("Alice", "uuid-a")
            session._on_skill_command("craft_item", "req-1", "manual_chat", {"item": "stone_pickaxe"})

            session.handle_event("command_response", {"id": "req-1", "success": True})
            self.assertEqual(session.memory.task_outcomes, [])

            session.handle_event("command_progress", {"id": "req-1", "progress": 1.0, "message": "crafted"})
            self.assertEqual(session.memory.task_outcomes, [])
            session.handle_event("command_outcome", {"id": "req-1", "status": "succeeded", "message": "crafted"})
            self.assertEqual(session.memory.task_outcomes[-1]["outcome"], "success")
            self.assertEqual(session.memory.task_outcomes[-1]["requester"], "Alice")
            session.stop()

    def test_response_finalizes_command_without_terminal_progress_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = Session(FakeBody(), companion_id="memory-test", storage_root=Path(tmp), legacy_root=None)
            session._current_requester = ("Alice", "uuid-a")
            session._on_skill_command("explore", "req-2", "manual_chat", {"radius": 16})

            session.handle_event("command_response", {"id": "req-2", "success": True})

            self.assertEqual(session.memory.task_outcomes[-1]["command"], "explore")
            self.assertEqual(session.memory.task_outcomes[-1]["outcome"], "success")
            session.stop()

    def test_move_to_waits_for_pathfinder_outcome(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = Session(FakeBody(), companion_id="memory-test", storage_root=Path(tmp), legacy_root=None)
            session._on_skill_command("move_to", "req-move", "manual_chat", {"x": 10, "y": 64, "z": 10})

            session.handle_event("command_response", {"id": "req-move", "success": True})
            self.assertEqual(session.memory.task_outcomes, [])
            session.handle_event("command_progress", {"id": "req-move", "progress": 0.0, "message": "no path"})
            self.assertEqual(session.memory.task_outcomes, [])
            session.handle_event("command_outcome", {"id": "req-move", "status": "failed", "message": "no path"})

            self.assertEqual(session.memory.task_outcomes[-1]["outcome"], "failed")
            session.stop()

    def test_shutdown_finalizes_pending_task_as_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = Session(FakeBody(), companion_id="memory-test", storage_root=Path(tmp), legacy_root=None)
            session._on_skill_command("follow_player", "req-follow", "manual_chat", {"player": "Alice"})

            session.stop()

            self.assertEqual(session.memory.task_outcomes[-1]["outcome"], "unknown")
            self.assertIn("backend stopped", session.memory.task_outcomes[-1]["detail"])

    def test_direct_sdk_chat_updates_relationship_even_without_llm(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = Session(FakeBody(), companion_id="memory-test", storage_root=Path(tmp), legacy_root=None)

            response = session.handle_chat("LauncherUser", "hello", sender_id="sdk-user")

            self.assertIsNone(response)
            self.assertEqual(session.memory.player_relationships["uuid:sdk-user"]["message_count"], 1)
            self.assertEqual(session.memory.recent_messages[-1]["message"], "hello")
            session.stop()

    def test_external_control_leaves_player_chat_to_upstream_owner(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = Session(FakeBody(), companion_id="memory-test", storage_root=Path(tmp), legacy_root=None)
            session.set_control_mode("external")

            with patch.object(session, "handle_chat") as local_planner:
                session.handle_event("player_chat", {
                    "sender": "Alice",
                    "uuid": "uuid-a",
                    "message": "come with me",
                    "is_system": False,
                })

            local_planner.assert_not_called()
            self.assertEqual(session.memory.player_relationships, {})
            self.assertEqual(session.memory.recent_messages, [])
            session.stop()

    def test_durable_task_busy_blocks_local_chat_planner(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = Session(FakeBody(), companion_id="memory-test", storage_root=Path(tmp), legacy_root=None)
            session.set_external_task_busy(True)

            with patch.object(session.planner, "plan_and_execute") as planner:
                response = session.handle_chat("Alice", "do something")

            self.assertIsNone(response)
            planner.assert_not_called()
            session.stop()

    def test_external_actuator_command_is_recorded_on_response(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = Session(FakeBody(), companion_id="memory-test", storage_root=Path(tmp), legacy_root=None)
            session.register_external_command("jump", "req-sdk", {}, requester="sdk")

            session.handle_event("command_response", {"id": "req-sdk", "success": True})

            self.assertEqual(session.memory.task_outcomes[-1]["requester"], "sdk")
            self.assertEqual(session.memory.task_outcomes[-1]["command"], "jump")
            session.stop()


if __name__ == "__main__":
    unittest.main()
