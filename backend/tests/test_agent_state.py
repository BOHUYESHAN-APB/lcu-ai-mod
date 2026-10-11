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
                self.assertEqual(conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0], 7)
                columns = {row[1] for row in conn.execute("PRAGMA table_info(task_runs)")}
                self.assertTrue({"task_state_json", "result_json", "pending_request_id"} <= columns)
            finally:
                conn.close()

    def test_v5_database_migrates_existing_runs_to_skill_kind(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "agent_state.db"
            conn = sqlite3.connect(path)
            conn.execute("CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at REAL NOT NULL)")
            conn.executemany(
                "INSERT INTO schema_migrations(version, applied_at) VALUES (?, 0)",
                [(version,) for version in range(1, 6)],
            )
            conn.execute("""
                CREATE TABLE task_runs (
                    id TEXT PRIMARY KEY, schedule_id TEXT, skill_id TEXT NOT NULL,
                    skill_version TEXT NOT NULL, input_json TEXT NOT NULL, completion TEXT NOT NULL,
                    status TEXT NOT NULL, request_id TEXT, progress REAL NOT NULL DEFAULT 0,
                    detail TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL,
                    dispatched_at REAL, started_at REAL, finished_at REAL, scope_id TEXT
                )
            """)
            conn.execute("""
                INSERT INTO task_runs(id, skill_id, skill_version, input_json, completion, status, created_at, scope_id)
                VALUES ('existing', 'core.jump', '1.0.0', '{}', 'response', 'queued', 1, 'default')
            """)
            conn.commit()
            conn.close()

            state = AgentStateDB(path)
            restored = state.get_run("existing")

            self.assertEqual(restored["run_kind"], "skill")
            self.assertIsNone(restored["parent_run_id"])
            self.assertEqual(restored["input"], {})
            state.close()

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

    def test_workflow_child_completion_advances_parent_atomically(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = AgentStateDB(Path(tmp) / "agent_state.db")
            workflow = {
                "id": "workflow.test", "version": "1.0.0", "kind": "workflow", "parameters": {},
                "steps": [
                    {"key": "jump", "title": "Jump", "skill_id": "core.jump", "skill_version": "1.0.0", "completion": "response", "input": {}},
                    {"key": "move", "title": "Move", "skill_id": "core.move_to", "skill_version": "2.0.0", "completion": "outcome", "input": {"x": 1, "y": 64, "z": 2}},
                ],
            }
            parent = state.create_workflow_run(workflow, scope_id="default")
            first_id = parent["active_child_id"]
            state.mark_run_dispatched(first_id, first_id)

            state.update_run_response(first_id, True, "jumped")

            restored = state.get_run(parent["id"])
            self.assertEqual(restored["status"], "queued")
            self.assertNotEqual(restored["active_child_id"], first_id)
            self.assertEqual([step["status"] for step in restored["steps"]], ["succeeded", "queued"])

            duplicate = state.update_run_response(first_id, True, "duplicate")
            after_duplicate = state.get_run(parent["id"])
            self.assertIsNone(duplicate)
            self.assertEqual(after_duplicate["active_child_id"], restored["active_child_id"])
            self.assertEqual(len(after_duplicate["steps"]), 2)
            state.close()

    def test_workflow_failure_and_cancellation_propagate_to_parent(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = AgentStateDB(Path(tmp) / "agent_state.db")
            workflow = {
                "id": "workflow.test", "version": "1.0.0", "kind": "workflow", "parameters": {},
                "steps": [{
                    "key": "move", "title": "Move", "skill_id": "core.move_to",
                    "skill_version": "2.0.0", "completion": "outcome", "input": {"x": 1, "y": 64, "z": 2},
                }],
            }
            failed = state.create_workflow_run(workflow)
            failed_child = failed["active_child_id"]
            state.mark_run_dispatched(failed_child, failed_child)
            state.update_run_outcome(failed_child, "failed", "blocked", "NO_PATH")
            self.assertEqual(state.get_run(failed["id"])["status"], "failed")

            cancelled = state.create_workflow_run(workflow)
            state.cancel_queued_workflow(cancelled["id"])
            self.assertEqual(state.get_run(cancelled["id"])["status"], "cancelled")
            state.close()

    def test_dynamic_workflow_state_and_result_are_persisted(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = AgentStateDB(Path(tmp) / "agent_state.db")
            workflow = {
                "id": "farm.region", "version": "1.0.0", "kind": "workflow",
                "dynamic_handler": "farm_region", "parameters": {"radius": 8}, "steps": [],
            }
            parent = state.create_dynamic_workflow_run(workflow, {
                "radius": 8, "scan_attempts": 0, "harvested": 0,
            })
            state.update_dynamic_workflow(
                parent["id"], {"radius": 8, "scan_attempts": 1, "harvested": 0},
                pending_request_id="scan-1",
            )

            restored = state.get_run(parent["id"])
            self.assertEqual(restored["task_state"]["scan_attempts"], 1)
            self.assertEqual(restored["pending_request_id"], "scan-1")

            finished = state.finish_dynamic_workflow(
                parent["id"], "succeeded", {"status": "succeeded", "harvested": 0},
            )
            self.assertEqual(finished["result"], {"status": "succeeded", "harvested": 0})
            self.assertIsNone(finished["pending_request_id"])
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

    def test_registry_reconciles_connected_body_capabilities(self):
        registry = SkillRegistry()
        registry.set_body_tools([{"command": "jump", "available": True}])

        self.assertTrue(next(item for item in registry.list() if item["id"] == "core.jump")["available"])
        self.assertFalse(next(item for item in registry.list() if item["id"] == "general.craft_item")["available"])
        registry.validate_input("core.jump", {})
        with self.assertRaisesRegex(SkillValidationError, "capability unavailable"):
            registry.validate_input("general.craft_item", {"item": "minecraft:torch", "count": 1})

    def test_registry_rejects_incompatible_body_contract(self):
        registry = SkillRegistry()
        registry.set_body_tools([{
            "command": "move_to", "available": True, "version": "1.0.0", "completion": "progress",
        }])

        item = next(item for item in registry.list() if item["id"] == "core.move_to")
        self.assertFalse(item["available"])
        with self.assertRaisesRegex(SkillValidationError, "capability unavailable"):
            registry.validate_input("core.move_to", {"x": 1, "y": 64, "z": 2})


if __name__ == "__main__":
    unittest.main()
