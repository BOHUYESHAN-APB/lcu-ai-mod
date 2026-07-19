import sqlite3
import tempfile
import unittest
from pathlib import Path

from agent.message_db import MessageDB


class MessageDBTests(unittest.TestCase):
    def test_conversation_history_returns_newest_page_in_chronological_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = MessageDB(Path(tmp) / "messages.db")
            conversation_id = db.create_conversation(["Alice", "companion"], "thread")
            for index in range(5):
                db.add_message("Alice", f"message-{index}", conversation_id=conversation_id)

            messages = db.get_conversation_messages(conversation_id, limit=3)
            db.close()

        self.assertEqual([item["message"] for item in messages], [
            "message-2", "message-3", "message-4",
        ])

    def test_legacy_receipts_migrate_without_cross_conversation_conflicts(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "messages.db"
            connection = sqlite3.connect(path)
            connection.execute("""
                CREATE TABLE player_message_receipts (
                    client_message_id TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    response_text TEXT,
                    error TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
            """)
            connection.execute("""
                INSERT INTO player_message_receipts
                    (client_message_id, conversation_id, status, response_text, created_at, updated_at)
                VALUES ('same-id', 'conversation-a', 'completed', 'old reply', 1, 1)
            """)
            connection.commit()
            connection.close()

            db = MessageDB(path)
            claimed, _ = db.claim_player_message("same-id", "conversation-b", "new-hash")
            legacy_claimed, legacy = db.claim_player_message("same-id", "conversation-a", "ignored-hash")
            db.close()

        self.assertTrue(claimed)
        self.assertFalse(legacy_claimed)
        self.assertEqual(legacy["response_text"], "old reply")
        self.assertEqual(legacy["request_hash"], "legacy-unverified")

    def test_failed_receipt_migration_rolls_back_schema_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "messages.db"
            connection = sqlite3.connect(path)
            connection.row_factory = sqlite3.Row
            connection.execute("""
                CREATE TABLE player_message_receipts (
                    client_message_id TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL
                )
            """)

            with self.assertRaises(sqlite3.OperationalError):
                MessageDB._migrate_player_message_receipts(connection.cursor())

            tables = {
                row[0] for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
            columns = {
                row["name"] for row in connection.execute(
                    "PRAGMA table_info(player_message_receipts)"
                ).fetchall()
            }
            connection.close()

        self.assertIn("player_message_receipts", tables)
        self.assertNotIn("player_message_receipts_legacy", tables)
        self.assertEqual(columns, {"client_message_id", "conversation_id"})

    def test_legacy_receipt_hash_is_reconstructed_from_persisted_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "messages.db"
            connection = sqlite3.connect(path)
            connection.execute("""
                CREATE TABLE messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp REAL NOT NULL,
                    sender TEXT NOT NULL,
                    message TEXT NOT NULL,
                    is_system BOOLEAN DEFAULT 0,
                    is_ai BOOLEAN DEFAULT 0,
                    conversation_id TEXT,
                    metadata TEXT
                )
            """)
            connection.execute("""
                CREATE TABLE player_message_receipts (
                    client_message_id TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    response_text TEXT,
                    error TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
            """)
            connection.execute("""
                INSERT INTO messages (timestamp, sender, message, conversation_id, metadata)
                VALUES (1, 'Alice', 'hello', 'conversation-a',
                        '{"client_message_id":"same-id"}')
            """)
            connection.execute("""
                INSERT INTO player_message_receipts
                    (client_message_id, conversation_id, status, response_text, created_at, updated_at)
                VALUES ('same-id', 'conversation-a', 'completed', 'old reply', 1, 1)
            """)
            connection.commit()
            connection.close()

            db = MessageDB(path)
            expected_hash = db.player_message_request_hash("Alice", "hello")
            claimed, receipt = db.claim_player_message("same-id", "conversation-a", expected_hash)
            db.close()

        self.assertFalse(claimed)
        self.assertEqual(receipt["request_hash"], expected_hash)

    def test_existing_composite_receipt_with_null_hash_is_backfilled(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "messages.db"
            db = MessageDB(path)
            conversation_id = db.get_or_create_direct_conversation("contact", "Alice")
            db.add_message(
                "Alice", "hello", conversation_id=conversation_id,
                metadata={"client_message_id": "same-id"},
            )
            db.conn.execute("""
                INSERT INTO player_message_receipts
                    (conversation_id, client_message_id, request_hash, status, response_text,
                     created_at, updated_at)
                VALUES (?, 'same-id', NULL, 'completed', 'old reply', 1, 1)
            """, (conversation_id,))
            db.conn.commit()
            db.close()

            reopened = MessageDB(path)
            expected_hash = reopened.player_message_request_hash("Alice", "hello")
            claimed, receipt = reopened.claim_player_message("same-id", conversation_id, expected_hash)
            reopened.close()

        self.assertFalse(claimed)
        self.assertEqual(receipt["request_hash"], expected_hash)


if __name__ == "__main__":
    unittest.main()
