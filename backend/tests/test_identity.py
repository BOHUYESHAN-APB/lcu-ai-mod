import json
import tempfile
import unittest
from pathlib import Path

from agent.identity import CompanionIdentity, migrate_legacy_sessions
from agent.message_db import MessageDB
from agent.session import Session
from protocol import BodyAdapter


class FakeBody:
    is_connected = False

    def connect(self):
        return False

    def disconnect(self):
        pass

    def send_command(self, command, args=None):
        return f"req-{command}"

    def drain(self):
        return []


class CompanionIdentityTests(unittest.TestCase):
    def test_scope_paths_are_stable_and_isolated(self):
        root = Path("root")
        global_path = CompanionIdentity("companion-a").storage_dir(root)
        server_path = CompanionIdentity("companion-a", "server", "server-a").storage_dir(root)
        other_server_path = CompanionIdentity("companion-a", "server", "server-b").storage_dir(root)
        world_path = CompanionIdentity("companion-a", "world", "server-a", "world-a").storage_dir(root)

        self.assertEqual(global_path, CompanionIdentity("companion-a").storage_dir(root))
        self.assertNotEqual(global_path, server_path)
        self.assertNotEqual(server_path, other_server_path)
        self.assertNotEqual(server_path, world_path)

    def test_session_restores_memory_and_messages_across_runtime_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage_root = Path(tmp) / "storage"
            first = Session(FakeBody(), companion_id="companion-a", storage_root=storage_root, legacy_root=None)
            first.memory.add_interaction("owner", "remember me")
            first.message_db.add_message("owner", "persistent message")
            first_id = first.id
            first.stop()

            second = Session(FakeBody(), companion_id="companion-a", storage_root=storage_root, legacy_root=None)

            self.assertNotEqual(first_id, second.id)
            self.assertEqual(second.memory.recent_messages[-1]["message"], "remember me")
            self.assertEqual(second.message_db.get_recent_messages()[-1]["message"], "persistent message")
            second.stop()

    def test_legacy_sessions_are_merged_once_without_deleting_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            legacy = root / "legacy"
            target = root / "target"
            memory_source = legacy / "memory" / "session_deadbeef.json"
            memory_source.parent.mkdir(parents=True)
            memory_source.write_text(json.dumps({"recent_messages": [{"message": "old"}]}), encoding="utf-8")
            second_memory_source = legacy / "memory" / "session_cafebabe.json"
            memory_source.write_text(json.dumps({
                "recent_messages": [{"message": "old"}],
                "player_relationships": {
                    "uuid:alice": {"names": ["Alice"], "message_count": 2, "tasks_requested": 1,
                                     "task_outcomes": {"success": 1}, "first_seen": 1, "last_seen": 2}
                },
            }), encoding="utf-8")
            second_memory_source.write_text(json.dumps({
                "player_relationships": {
                    "uuid:alice": {"names": ["Alice2"], "message_count": 3, "tasks_requested": 2,
                                     "task_outcomes": {"success": 2}, "first_seen": 0, "last_seen": 3}
                },
            }), encoding="utf-8")
            db_source = legacy / "messages_deadbeef.db"
            db = MessageDB(db_source)
            db.add_message("owner", "old db")
            db.close()
            second_db_source = legacy / "messages_cafebabe.db"
            second_db = MessageDB(second_db_source)
            second_db.add_message("friend", "second db")
            second_db.close()

            migrated = migrate_legacy_sessions(target, legacy)
            repeated_target = root / "other-target"
            repeated = migrate_legacy_sessions(repeated_target, legacy)

            self.assertEqual(migrated, ["cafebabe", "deadbeef"])
            self.assertEqual(repeated, [])
            self.assertTrue(memory_source.exists())
            self.assertTrue(db_source.exists())
            self.assertTrue(second_db_source.exists())
            self.assertTrue((target / "memory.json").exists())
            self.assertTrue((target / "messages.db").exists())
            self.assertTrue((target / "legacy-migration.json").exists())
            self.assertTrue((legacy / ".lcu-migration.json").exists())
            merged_db = MessageDB(target / "messages.db")
            self.assertEqual(len(merged_db.get_recent_messages()), 2)
            merged_db.close()
            merged_memory = json.loads((target / "memory.json").read_text(encoding="utf-8"))
            relationship = merged_memory["player_relationships"]["uuid:alice"]
            self.assertEqual(relationship["message_count"], 5)
            self.assertEqual(relationship["task_outcomes"]["success"], 3)
            self.assertEqual(relationship["names"], ["Alice", "Alice2"])

    def test_wire_compatible_body_satisfies_adapter_contract(self):
        self.assertIsInstance(FakeBody(), BodyAdapter)

    def test_existing_stable_target_claims_legacy_data_without_copying(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            legacy = root / "legacy"
            legacy.mkdir()
            (legacy / "messages_deadbeef.db").write_bytes(b"legacy")
            target = root / "target"
            target.mkdir()
            (target / "memory.json").write_text("{}", encoding="utf-8")

            self.assertEqual(migrate_legacy_sessions(target, legacy), [])
            self.assertTrue((legacy / ".lcu-migration.json").exists())
            self.assertFalse((target / "messages.db").exists())


if __name__ == "__main__":
    unittest.main()
