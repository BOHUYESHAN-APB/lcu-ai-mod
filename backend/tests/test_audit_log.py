import tempfile
import unittest
from pathlib import Path

from agent.audit_log import AuditLog


class AuditLogTests(unittest.TestCase):
    def test_events_are_append_only_filtered_and_redacted(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = AuditLog(Path(tmp) / "audit.jsonl")
            log.append("access", "principal.updated", target="alice", details={"api_key": "secret", "role": "master"})
            log.append("provider", "profile.updated", target="main", details={"nested": {"token": "secret"}})

            records = log.list(limit=10)
            access = log.list(category="access", limit=10)

            self.assertEqual([record["category"] for record in records], ["provider", "access"])
            self.assertEqual(access[0]["details"]["api_key"], "***")
            self.assertEqual(records[0]["details"]["nested"]["token"], "***")


if __name__ == "__main__":
    unittest.main()
