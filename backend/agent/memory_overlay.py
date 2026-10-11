"""SQLite development adapter for reversible memory lifecycle state."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any


MEMORY_STATES = {"active", "archived", "deleted"}


class RetentionConflictError(ValueError):
    pass


class MemoryOverlayStore:
    """Stores reversible overlays and audit history without deleting source data."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._migrate()

    def _migrate(self) -> None:
        with self._conn:
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS memory_record_states (
                    scope_id TEXT NOT NULL,
                    record_id TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('active', 'archived', 'deleted')),
                    reason TEXT NOT NULL DEFAULT '',
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (scope_id, record_id)
                )
            """)
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS memory_audit (
                    id TEXT PRIMARY KEY,
                    scope_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    changes TEXT NOT NULL,
                    reason TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL
                )
            """)
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS memory_retention (
                    scope_id TEXT PRIMARY KEY,
                    version INTEGER NOT NULL,
                    rules TEXT NOT NULL,
                    updated_at REAL NOT NULL
                )
            """)
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_memory_state_scope_state ON memory_record_states(scope_id, state)"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_memory_audit_scope_time ON memory_audit(scope_id, created_at DESC)"
            )

    def get_states(self, scope_id: str) -> dict[str, str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT record_id, state FROM memory_record_states WHERE scope_id = ?",
                (scope_id,),
            ).fetchall()
        return {str(row["record_id"]): str(row["state"]) for row in rows}

    def apply_changes(self, scope_id: str, changes: dict[str, list[str]], reason: str = "") -> dict[str, Any]:
        normalized: dict[str, list[str]] = {}
        for state, record_ids in changes.items():
            if state not in MEMORY_STATES:
                raise ValueError(f"invalid memory state: {state}")
            unique_ids = sorted({str(record_id) for record_id in record_ids if str(record_id)})
            if unique_ids:
                normalized[state] = unique_ids
        if not normalized:
            raise ValueError("no memory state changes requested")
        now = time.time()
        audit_id = str(uuid.uuid4())
        with self._lock, self._conn:
            for state, record_ids in normalized.items():
                self._conn.executemany("""
                    INSERT INTO memory_record_states(scope_id, record_id, state, reason, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(scope_id, record_id) DO UPDATE SET
                        state = excluded.state,
                        reason = excluded.reason,
                        updated_at = excluded.updated_at
                """, [(scope_id, record_id, state, reason, now) for record_id in record_ids])
            self._conn.execute("""
                INSERT INTO memory_audit(id, scope_id, action, changes, reason, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (audit_id, scope_id, "state_change", json.dumps(normalized), reason, now))
        return {
            "audit_id": audit_id,
            "scope_id": scope_id,
            "changes": normalized,
            "affected_count": sum(len(record_ids) for record_ids in normalized.values()),
            "created_at": now,
        }

    def list_audit(self, scope_id: str, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute("""
                SELECT id, action, changes, reason, created_at
                FROM memory_audit WHERE scope_id = ?
                ORDER BY created_at DESC, id DESC LIMIT ?
            """, (scope_id, limit)).fetchall()
        return [{**dict(row), "changes": json.loads(row["changes"])} for row in rows]

    def get_retention(self, scope_id: str) -> dict[str, Any]:
        with self._lock:
            row = self._conn.execute(
                "SELECT version, rules, updated_at FROM memory_retention WHERE scope_id = ?",
                (scope_id,),
            ).fetchone()
        if row is None:
            return {"scope_id": scope_id, "version": 0, "rules": [], "updated_at": None}
        return {
            "scope_id": scope_id,
            "version": int(row["version"]),
            "rules": json.loads(row["rules"]),
            "updated_at": float(row["updated_at"]),
        }

    def set_retention(self, scope_id: str, expected_version: int, rules: list[dict[str, Any]]) -> dict[str, Any]:
        now = time.time()
        with self._lock, self._conn:
            current = self._conn.execute(
                "SELECT version FROM memory_retention WHERE scope_id = ?",
                (scope_id,),
            ).fetchone()
            current_version = int(current["version"]) if current else 0
            if current_version != expected_version:
                raise RetentionConflictError(
                    f"retention version conflict: expected {expected_version}, current {current_version}"
                )
            new_version = current_version + 1
            self._conn.execute("""
                INSERT INTO memory_retention(scope_id, version, rules, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(scope_id) DO UPDATE SET
                    version = excluded.version,
                    rules = excluded.rules,
                    updated_at = excluded.updated_at
            """, (scope_id, new_version, json.dumps(rules), now))
        return {"scope_id": scope_id, "version": new_version, "rules": rules, "updated_at": now}

    def close(self) -> None:
        with self._lock:
            self._conn.close()
