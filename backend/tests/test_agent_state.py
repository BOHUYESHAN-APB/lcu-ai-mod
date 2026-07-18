import sqlite3
import tempfile
import unittest
import math
from pathlib import Path

from agent.agent_state import AgentStateDB, LeaseConflictError, LeaseNotFoundError
from agent.skill_registry import SkillRegistry, SkillValidationError


class AgentStateTests(unittest.TestCase):
    def test_database_migrates_and_syncs_skill_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "agent_state.db"
            state = AgentStateDB(path)
            manifests = SkillRegistry().list()

            state.sync_skills(manifests)
            state.sync_skills(manifests)

            self.assertEqual(len(state.list_skills()), len(manifests))
            self.assertEqual(state.list_skills()[0]["source"], "builtin")
            state.close()
            conn = sqlite3.connect(path)
            try:
                self.assertEqual(conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0], 5)
            finally:
                conn.close()

    def test_control_lease_is_exclusive_renewable_and_fenced(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = AgentStateDB(Path(tmp) / "agent_state.db")
            lease = state.acquire_lease("roleplay-agent", "external", ["planner", "actions"], 30)

            with self.assertRaises(LeaseConflictError):
                state.acquire_lease("other-agent", "external", ["actions"], 30)

            renewed = state.renew_lease(lease["id"], lease["fencing_token"], 60)
            self.assertGreater(renewed["expires_at"], lease["expires_at"])
            with self.assertRaises(LeaseNotFoundError):
                state.release_lease(lease["id"], lease["fencing_token"] + 1)

            released = state.release_lease(lease["id"], lease["fencing_token"])
            self.assertIsNotNone(released["released_at"])
            with self.assertRaises(LeaseConflictError):
                with state.control_guard(lease["id"], lease["fencing_token"]):
                    pass
            next_lease = state.acquire_lease("other-agent", "external", ["actions"], 30)
            self.assertGreater(next_lease["fencing_token"], lease["fencing_token"])
            state.close()


class SkillRegistryTests(unittest.TestCase):
    def test_registry_validates_typed_skill_inputs(self):
        registry = SkillRegistry()

        manifest = registry.validate_input("general.craft_item", {"item": "minecraft:torch", "count": 8})

        self.assertEqual(manifest.command, "craft_item")
        self.assertTrue(manifest.offline)
        self.assertEqual(manifest.executor, "deterministic")
        with self.assertRaisesRegex(SkillValidationError, "missing fields"):
            registry.validate_input("general.craft_item", {"item": "minecraft:torch"})
        with self.assertRaisesRegex(SkillValidationError, "must be <="):
            registry.validate_input("general.craft_item", {"item": "minecraft:torch", "count": 9999})
        with self.assertRaisesRegex(SkillValidationError, "unknown fields"):
            registry.validate_input("core.jump", {"shell": "no"})
        with self.assertRaisesRegex(SkillValidationError, "must be finite"):
            registry.validate_input("core.move_to", {"x": math.nan, "y": 64, "z": 0})
        with self.assertRaisesRegex(SkillValidationError, "must be <="):
            registry.validate_input("general.craft_item", {"item": "minecraft:torch", "count": 10 ** 10000})


if __name__ == "__main__":
    unittest.main()
