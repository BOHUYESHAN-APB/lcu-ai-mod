import json
import unittest
from types import SimpleNamespace

from agent.identity import CompanionIdentity
from agent.memory_catalog import MemoryCatalog, MemoryQuery


class DummyMessageReader:
    def __init__(self, messages):
        self.messages = messages

    def get_recent_messages(self, limit=50, sender=None):
        messages = self.messages
        if sender:
            messages = [message for message in messages if message["sender"] == sender]
        return messages[-limit:]


class MemoryCatalogTests(unittest.TestCase):
    def setUp(self):
        self.memory = SimpleNamespace(
            events=[{
                "time": 20.0,
                "type": "death",
                "description": "Alice fell",
                "metadata": {"token": "do-not-export"},
            }],
            task_outcomes=[{
                "time": 30.0,
                "command": "craft_item",
                "target": "stone_pickaxe",
                "outcome": "success",
                "requester": "Alice",
            }],
            player_profiles={"Alice": {"last_active": 15.0, "message_count": 2, "avg_message_length": 5}},
            player_relationships={"uuid:alice": {"last_seen": 25.0, "names": ["Alice"], "message_count": 2}},
            locations={"home": {"saved_at": 10.0, "x": 1, "y": 64, "z": 2, "dimension": "overworld"}},
            experiences={
                "servers": {"example.org": {"last_seen": 5.0, "known_players": ["Alice"]}},
                "worlds": {"example.org\0survival": {"last_seen": 6.0, "last_position": {"x": 1}}},
            },
            summaries=[],
            _save_blocked_reason=None,
        )
        self.reader = DummyMessageReader([{
            "id": 7,
            "timestamp": 40.0,
            "sender": "Alice",
            "message": "hello",
            "is_system": 0,
            "is_ai": 0,
            "metadata": json.dumps({"api_key": "secret", "mood": "calm"}),
        }])
        self.catalog = MemoryCatalog(
            self.memory,
            self.reader,
            CompanionIdentity("companion-a", "world", "example.org", "survival"),
        )

    def test_status_reports_scope_counts_and_local_only_backend(self):
        status = self.catalog.status()

        self.assertEqual(status["record_count"], 8)
        self.assertEqual(status["category_counts"]["message"], 1)
        self.assertEqual(status["scope"]["scope"], "world")
        self.assertFalse(status["repository"]["production_ready"])
        self.assertEqual(status["repository"]["backend"], "sqlite")
        self.assertEqual(status["repository"]["production_required_backend"], "postgresql")
        self.assertTrue(status["revision"])

    def test_records_are_deterministic_filtered_and_paginated(self):
        first = self.catalog.list_records(MemoryQuery(query="Alice", limit=2))
        second = self.catalog.list_records(MemoryQuery(query="Alice", limit=2))

        self.assertEqual(first, second)
        self.assertEqual(len(first["records"]), 2)
        self.assertEqual(first["records"][0]["category"], "message")
        self.assertNotIn("content", first["records"][0])
        self.assertIsNotNone(first["next_offset"])

        tasks = self.catalog.list_records(MemoryQuery(categories=frozenset({"task_outcome"})))
        self.assertEqual(tasks["count"], 1)
        self.assertEqual(tasks["records"][0]["title"], "craft_item")

    def test_detail_has_provenance_and_redacts_sensitive_metadata(self):
        listed = self.catalog.list_records(MemoryQuery(categories=frozenset({"message"})))
        record = self.catalog.get_record(listed["records"][0]["id"])

        self.assertEqual(record["content"]["metadata"]["api_key"], "***")
        self.assertEqual(record["content"]["metadata"]["mood"], "calm")
        self.assertEqual(record["provenance"]["source"], "messages_db")
        self.assertTrue(record["provenance"]["source_hash"].startswith("sha256:"))

    def test_json_and_jsonl_exports_preserve_scope_and_can_omit_provenance(self):
        query = MemoryQuery(categories=frozenset({"location"}))

        json_payload, json_type = self.catalog.export(query, "json", include_provenance=False)
        document = json.loads(json_payload)
        jsonl_payload, jsonl_type = self.catalog.export(query, "jsonl")
        lines = jsonl_payload.decode("utf-8").splitlines()

        self.assertEqual(json_type, "application/json")
        self.assertEqual(document["scope"]["world_id"], "survival")
        self.assertNotIn("provenance", document["records"][0])
        self.assertEqual(jsonl_type, "application/x-ndjson")
        self.assertEqual(json.loads(lines[0])["type"], "metadata")
        self.assertEqual(json.loads(lines[1])["category"], "location")

    def test_unknown_record_and_export_format_are_rejected(self):
        self.assertIsNone(self.catalog.get_record("missing"))
        with self.assertRaisesRegex(ValueError, "format"):
            self.catalog.export(MemoryQuery(), "zip")


if __name__ == "__main__":
    unittest.main()
