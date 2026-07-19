"""
Message Database — SQLite-based message persistence.
Provides:
- Message history storage
- Conversation threading
- Player interaction tracking
- Search and retrieval
"""

import sqlite3
import time
import json
import logging
import hashlib
from pathlib import Path
from typing import Optional, List, Dict, Any

logger = logging.getLogger("message_db")
UNVERIFIED_REQUEST_HASH = "legacy-unverified"


class MessageDB:
    """
    SQLite database for message persistence.
    
    Tables:
    - messages: All chat messages
    - conversations: Conversation threads
    - players: Player profiles and stats
    - events: Important game events
    """
    
    def __init__(self, db_path: str = "data/messages.db"):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._init_tables()
    
    def _init_tables(self):
        """Initialize database tables."""
        cursor = self.conn.cursor()
        
        # Messages table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS messages (
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
        
        # Conversations table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS conversations (
                id TEXT PRIMARY KEY,
                started_at REAL NOT NULL,
                last_activity REAL NOT NULL,
                participants TEXT,
                message_count INTEGER DEFAULT 0,
                topic TEXT
            )
        """)
        
        # Players table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS players (
                uuid TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                first_seen REAL NOT NULL,
                last_seen REAL NOT NULL,
                message_count INTEGER DEFAULT 0,
                avg_message_length REAL DEFAULT 0,
                interaction_style TEXT,
                notes TEXT
            )
        """)
        
        # Events table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                event_type TEXT NOT NULL,
                description TEXT,
                player_involved TEXT,
                location TEXT,
                metadata TEXT
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS player_message_receipts (
                conversation_id TEXT NOT NULL,
                client_message_id TEXT NOT NULL,
                request_hash TEXT,
                status TEXT NOT NULL,
                response_text TEXT,
                error TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                PRIMARY KEY (conversation_id, client_message_id)
            )
        """)
        self._migrate_player_message_receipts(cursor)
        
        # Create indexes
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_messages_timestamp ON messages(timestamp)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_messages_sender ON messages(sender)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_messages_conversation ON messages(conversation_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_players_name ON players(name)")
        
        self.conn.commit()
        logger.info("[MessageDB] Database initialized at %s", self.db_path)

    @staticmethod
    def _migrate_player_message_receipts(cursor: sqlite3.Cursor) -> None:
        columns = cursor.execute("PRAGMA table_info(player_message_receipts)").fetchall()
        primary_key = [row["name"] for row in sorted(columns, key=lambda row: row["pk"]) if row["pk"]]
        names = {row["name"] for row in columns}
        if primary_key == ["conversation_id", "client_message_id"] and "request_hash" in names:
            MessageDB._backfill_player_receipt_hashes(cursor)
            return

        cursor.execute("SAVEPOINT migrate_player_message_receipts")
        try:
            cursor.execute("ALTER TABLE player_message_receipts RENAME TO player_message_receipts_legacy")
            cursor.execute("""
                CREATE TABLE player_message_receipts (
                    conversation_id TEXT NOT NULL,
                    client_message_id TEXT NOT NULL,
                    request_hash TEXT,
                    status TEXT NOT NULL,
                    response_text TEXT,
                    error TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (conversation_id, client_message_id)
                )
            """)
            request_hash = "request_hash" if "request_hash" in names else "NULL"
            cursor.execute(f"""
                INSERT OR IGNORE INTO player_message_receipts
                    (conversation_id, client_message_id, request_hash, status, response_text, error,
                     created_at, updated_at)
                SELECT conversation_id, client_message_id, {request_hash}, status, response_text, error,
                       created_at, updated_at
                FROM player_message_receipts_legacy
            """)
            MessageDB._backfill_player_receipt_hashes(cursor)
            cursor.execute("DROP TABLE player_message_receipts_legacy")
        except Exception:
            cursor.execute("ROLLBACK TO SAVEPOINT migrate_player_message_receipts")
            cursor.execute("RELEASE SAVEPOINT migrate_player_message_receipts")
            raise
        cursor.execute("RELEASE SAVEPOINT migrate_player_message_receipts")

    @staticmethod
    def _backfill_player_receipt_hashes(cursor: sqlite3.Cursor) -> None:
        receipts = cursor.execute("""
            SELECT conversation_id, client_message_id FROM player_message_receipts
            WHERE request_hash IS NULL
        """).fetchall()
        for receipt in receipts:
            request_hash = UNVERIFIED_REQUEST_HASH
            messages = cursor.execute("""
                SELECT sender, message, metadata FROM messages
                WHERE conversation_id = ? ORDER BY id DESC
            """, (receipt["conversation_id"],)).fetchall()
            for message in messages:
                try:
                    metadata = json.loads(message["metadata"] or "{}")
                except (TypeError, json.JSONDecodeError):
                    continue
                if metadata.get("client_message_id") == receipt["client_message_id"]:
                    request_hash = MessageDB.player_message_request_hash(
                        message["sender"], message["message"],
                    )
                    break
            cursor.execute("""
                UPDATE player_message_receipts SET request_hash = ?
                WHERE conversation_id = ? AND client_message_id = ?
            """, (request_hash, receipt["conversation_id"], receipt["client_message_id"]))
    
    # ── Message Operations ──
    
    def add_message(self, sender: str, message: str, is_system: bool = False, 
                    is_ai: bool = False, conversation_id: Optional[str] = None,
                    metadata: Optional[dict] = None) -> Optional[int]:
        """Add a message to the database."""
        cursor = self.conn.cursor()
        cursor.execute("""
            INSERT INTO messages (timestamp, sender, message, is_system, is_ai, conversation_id, metadata)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            time.time(),
            sender,
            message,
            1 if is_system else 0,
            1 if is_ai else 0,
            conversation_id,
            json.dumps(metadata) if metadata is not None else None
        ))
        self.conn.commit()
        
        # Update player stats
        if not is_system and not is_ai:
            self._update_player_stats(sender, message)
        
        # Update conversation
        if conversation_id:
            self._update_conversation(conversation_id, sender)
        
        return cursor.lastrowid
    
    def get_recent_messages(self, limit: int = 50, sender: Optional[str] = None) -> List[Dict]:
        """Get recent messages, optionally filtered by sender."""
        cursor = self.conn.cursor()
        if sender:
            cursor.execute("""
                SELECT * FROM messages 
                WHERE sender = ? 
                ORDER BY timestamp DESC 
                LIMIT ?
            """, (sender, limit))
        else:
            cursor.execute("""
                SELECT * FROM messages 
                ORDER BY timestamp DESC 
                LIMIT ?
            """, (limit,))
        
        rows = cursor.fetchall()
        return [dict(row) for row in reversed(rows)]
    
    def get_conversation_messages(self, conversation_id: str, limit: int = 100) -> List[Dict]:
        """Get the newest messages in a conversation thread in chronological order."""
        cursor = self.conn.cursor()
        cursor.execute("""
            SELECT * FROM (
                SELECT * FROM messages
                WHERE conversation_id = ?
                ORDER BY timestamp DESC, id DESC
                LIMIT ?
            )
            ORDER BY timestamp ASC, id ASC
        """, (conversation_id, limit))
        
        rows = cursor.fetchall()
        return [dict(row) for row in rows]
    
    def search_messages(self, query: str, limit: int = 50) -> List[Dict]:
        """Search messages by content."""
        cursor = self.conn.cursor()
        cursor.execute("""
            SELECT * FROM messages 
            WHERE message LIKE ? 
            ORDER BY timestamp DESC 
            LIMIT ?
        """, (f"%{query}%", limit))
        
        rows = cursor.fetchall()
        return [dict(row) for row in rows]
    
    def get_messages_since(self, timestamp: float, limit: int = 100) -> List[Dict]:
        """Get messages since a timestamp."""
        cursor = self.conn.cursor()
        cursor.execute("""
            SELECT * FROM messages 
            WHERE timestamp >= ? 
            ORDER BY timestamp ASC 
            LIMIT ?
        """, (timestamp, limit))
        
        rows = cursor.fetchall()
        return [dict(row) for row in rows]
    
    # ── Conversation Operations ──
    
    def create_conversation(self, participants: List[str], topic: Optional[str] = None) -> str:
        """Create a new conversation thread."""
        conv_id = f"conv_{int(time.time())}_{hash(tuple(participants)) % 10000}"
        cursor = self.conn.cursor()
        cursor.execute("""
            INSERT INTO conversations (id, started_at, last_activity, participants, topic)
            VALUES (?, ?, ?, ?, ?)
        """, (
            conv_id,
            time.time(),
            time.time(),
            json.dumps(participants),
            topic
        ))
        self.conn.commit()
        return conv_id

    def get_or_create_direct_conversation(self, contact_id: str, display_name: str) -> str:
        """Return a stable direct conversation for one verified transport identity."""
        digest = hashlib.sha256(contact_id.encode("utf-8")).hexdigest()[:24]
        conversation_id = f"direct_{digest}"
        now = time.time()
        participants = json.dumps([contact_id, "companion"], ensure_ascii=False)
        with self.conn:
            self.conn.execute("""
                INSERT OR IGNORE INTO conversations
                    (id, started_at, last_activity, participants, message_count, topic)
                VALUES (?, ?, ?, ?, 0, ?)
            """, (conversation_id, now, now, participants, display_name))
            self.conn.execute("""
                UPDATE conversations SET topic = ?, last_activity = MAX(last_activity, ?)
                WHERE id = ?
            """, (display_name, now, conversation_id))
        return conversation_id

    def get_conversation(self, conversation_id: str) -> Optional[Dict]:
        row = self.conn.execute(
            "SELECT * FROM conversations WHERE id = ?", (conversation_id,),
        ).fetchone()
        return self._decode_conversation(row) if row else None

    def list_conversations(self, limit: int = 100) -> List[Dict]:
        rows = self.conn.execute("""
            SELECT * FROM conversations
            ORDER BY last_activity DESC, id ASC LIMIT ?
        """, (limit,)).fetchall()
        return [self._decode_conversation(row) for row in rows]

    def claim_player_message(self, client_message_id: str, conversation_id: str,
                             request_hash: str) -> tuple[bool, Dict]:
        now = time.time()
        with self.conn:
            cursor = self.conn.execute("""
                INSERT OR IGNORE INTO player_message_receipts
                    (conversation_id, client_message_id, request_hash, status, created_at, updated_at)
                VALUES (?, ?, ?, 'processing', ?, ?)
            """, (conversation_id, client_message_id, request_hash, now, now))
            row = self.conn.execute(
                """SELECT * FROM player_message_receipts
                   WHERE conversation_id = ? AND client_message_id = ?""",
                (conversation_id, client_message_id),
            ).fetchone()
        return cursor.rowcount == 1, dict(row)

    @staticmethod
    def player_message_request_hash(player_name: str, message: str) -> str:
        payload = json.dumps(
            {"message": message, "player_name": player_name},
            ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def complete_player_message(self, client_message_id: str, conversation_id: str,
                                response_text: str) -> None:
        with self.conn:
            self.conn.execute("""
                UPDATE player_message_receipts
                SET status = 'completed', response_text = ?, error = NULL, updated_at = ?
                WHERE conversation_id = ? AND client_message_id = ?
            """, (response_text, time.time(), conversation_id, client_message_id))

    def fail_player_message(self, client_message_id: str, conversation_id: str, error: str) -> None:
        with self.conn:
            self.conn.execute("""
                UPDATE player_message_receipts
                SET status = 'failed', error = ?, updated_at = ?
                WHERE conversation_id = ? AND client_message_id = ?
            """, (error[:1000], time.time(), conversation_id, client_message_id))

    @staticmethod
    def _decode_conversation(row) -> Dict:
        result = dict(row)
        try:
            result["participants"] = json.loads(result.get("participants") or "[]")
        except json.JSONDecodeError:
            result["participants"] = []
        return result
    
    def _update_conversation(self, conv_id: str, sender: str):
        """Update conversation activity."""
        cursor = self.conn.cursor()
        cursor.execute("""
            UPDATE conversations 
            SET last_activity = ?, message_count = message_count + 1
            WHERE id = ?
        """, (time.time(), conv_id))
        self.conn.commit()
    
    def get_active_conversations(self, hours: float = 24) -> List[Dict]:
        """Get active conversations within time window."""
        cutoff = time.time() - (hours * 3600)
        cursor = self.conn.cursor()
        cursor.execute("""
            SELECT * FROM conversations 
            WHERE last_activity >= ? 
            ORDER BY last_activity DESC
        """, (cutoff,))
        
        rows = cursor.fetchall()
        return [dict(row) for row in rows]
    
    # ── Player Operations ──
    
    def _update_player_stats(self, sender: str, message: str):
        """Update player statistics."""
        cursor = self.conn.cursor()
        
        # Check if player exists
        cursor.execute("SELECT * FROM players WHERE name = ?", (sender,))
        player = cursor.fetchone()
        
        if player:
            # Update existing player
            msg_count = player['message_count'] + 1
            avg_len = (player['avg_message_length'] * player['message_count'] + len(message)) / msg_count
            
            cursor.execute("""
                UPDATE players 
                SET last_seen = ?, message_count = ?, avg_message_length = ?
                WHERE name = ?
            """, (time.time(), msg_count, avg_len, sender))
        else:
            # Create new player
            cursor.execute("""
                INSERT INTO players (uuid, name, first_seen, last_seen, message_count, avg_message_length)
                VALUES (?, ?, ?, ?, 1, ?)
            """, (sender, sender, time.time(), time.time(), len(message)))
        
        self.conn.commit()
    
    def get_player_stats(self, name: str) -> Optional[Dict]:
        """Get player statistics."""
        cursor = self.conn.cursor()
        cursor.execute("SELECT * FROM players WHERE name = ?", (name,))
        row = cursor.fetchone()
        return dict(row) if row else None
    
    def get_all_players(self) -> List[Dict]:
        """Get all known players."""
        cursor = self.conn.cursor()
        cursor.execute("SELECT * FROM players ORDER BY last_seen DESC")
        rows = cursor.fetchall()
        return [dict(row) for row in rows]
    
    # ── Event Operations ──
    
    def add_event(self, event_type: str, description: str, 
                  player_involved: Optional[str] = None,
                  location: Optional[str] = None,
                  metadata: Optional[dict] = None) -> Optional[int]:
        """Add a game event."""
        cursor = self.conn.cursor()
        cursor.execute("""
            INSERT INTO events (timestamp, event_type, description, player_involved, location, metadata)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            time.time(),
            event_type,
            description,
            player_involved,
            location,
            json.dumps(metadata) if metadata is not None else None
        ))
        self.conn.commit()
        return cursor.lastrowid
    
    def get_recent_events(self, limit: int = 50, event_type: Optional[str] = None) -> List[Dict]:
        """Get recent events."""
        cursor = self.conn.cursor()
        if event_type:
            cursor.execute("""
                SELECT * FROM events 
                WHERE event_type = ? 
                ORDER BY timestamp DESC 
                LIMIT ?
            """, (event_type, limit))
        else:
            cursor.execute("""
                SELECT * FROM events 
                ORDER BY timestamp DESC 
                LIMIT ?
            """, (limit,))
        
        rows = cursor.fetchall()
        return [dict(row) for row in rows]
    
    # ── Statistics ──
    
    def get_stats(self) -> Dict:
        """Get database statistics."""
        cursor = self.conn.cursor()
        
        cursor.execute("SELECT COUNT(*) FROM messages")
        total_messages = cursor.fetchone()[0]
        
        cursor.execute("SELECT COUNT(*) FROM players")
        total_players = cursor.fetchone()[0]
        
        cursor.execute("SELECT COUNT(*) FROM conversations")
        total_conversations = cursor.fetchone()[0]
        
        cursor.execute("SELECT COUNT(*) FROM events")
        total_events = cursor.fetchone()[0]
        
        # Messages in last 24 hours
        cutoff = time.time() - 86400
        cursor.execute("SELECT COUNT(*) FROM messages WHERE timestamp >= ?", (cutoff,))
        messages_24h = cursor.fetchone()[0]
        
        return {
            "total_messages": total_messages,
            "total_players": total_players,
            "total_conversations": total_conversations,
            "total_events": total_events,
            "messages_24h": messages_24h,
        }
    
    # ── Cleanup ──
    
    def cleanup_old_messages(self, days: int = 30):
        """Remove messages older than specified days."""
        cutoff = time.time() - (days * 86400)
        cursor = self.conn.cursor()
        cursor.execute("DELETE FROM messages WHERE timestamp < ?", (cutoff,))
        deleted = cursor.rowcount
        self.conn.commit()
        logger.info("[MessageDB] Cleaned up %d old messages", deleted)
        return deleted
    
    def close(self):
        """Close database connection."""
        self.conn.close()
