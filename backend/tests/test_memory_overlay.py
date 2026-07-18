import tempfile
import unittest
from pathlib import Path

from agent.memory_management import evaluate_retention
from agent.memory_overlay import MemoryOverlayStore, RetentionConflictError


class MemoryOverlayStoreTests(unittest.TestCase):
    def test_state_changes_are_transactional_audited_and_persistent(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "memory_management.db"
            store = MemoryOverlayStore(path)

            result = store.apply_changes("global", {"archived": ["message:1", "message:2"]}, "manual")
            states = store.get_states("global")
            audit = store.list_audit("global")
            store.close()

            reopened = MemoryOverlayStore(path)
            persisted = reopened.get_states("global")
            reopened.close()

        self.assertEqual(result["affected_count"], 2)
        self.assertEqual(states["message:1"], "archived")
        self.assertEqual(audit[0]["changes"]["archived"], ["message:1", "message:2"])
        self.assertEqual(persisted, states)

    def test_retention_uses_optimistic_versions(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = MemoryOverlayStore(Path(tmp) / "memory_management.db")
            rules = [{"category": "message", "archive_after_days": 30, "delete_after_days": 90}]

            saved = store.set_retention("global", 0, rules)
            loaded = store.get_retention("global")
            with self.assertRaises(RetentionConflictError):
                store.set_retention("global", 0, rules)
            store.close()

        self.assertEqual(saved["version"], 1)
        self.assertEqual(loaded["rules"], rules)

    def test_retention_evaluation_respects_state_age_and_min_keep(self):
        now = 100 * 86400
        records = [
            {"id": "new", "category": "message", "state": "active", "occurred_at": 99 * 86400},
            {"id": "old", "category": "message", "state": "active", "occurred_at": 10 * 86400},
            {"id": "archived", "category": "message", "state": "archived", "occurred_at": 0},
        ]
        rules = [{
            "category": "message", "enabled": True,
            "archive_after_days": 30, "delete_after_days": 60, "min_keep": 1,
        }]

        changes = evaluate_retention(records, rules, now=now)

        self.assertEqual(changes["archived"], ["old"])
        self.assertEqual(changes["deleted"], ["archived"])


if __name__ == "__main__":
    unittest.main()
