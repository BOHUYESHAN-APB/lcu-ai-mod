"""Durable control-plane state for SDK V2."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any


DEFAULT_AGENT_STATE_PATH = Path(__file__).parent.parent / ".local" / "agent_state.db"


class LeaseConflictError(RuntimeError):
    pass


class LeaseNotFoundError(RuntimeError):
    pass


class AgentStateDB:
    """Migrated SQLite store for registries, leases, tasks, and schedules."""

    def __init__(self, path: str | Path = DEFAULT_AGENT_STATE_PATH):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False, timeout=5.0)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._migrate()

    def _migrate(self) -> None:
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at REAL NOT NULL)"
            )
            applied = {row["version"] for row in self._conn.execute("SELECT version FROM schema_migrations")}
            if 1 not in applied:
                self._conn.execute("""
                    CREATE TABLE IF NOT EXISTS skill_registry (
                    id TEXT PRIMARY KEY,
                    version TEXT NOT NULL,
                    category TEXT NOT NULL,
                    source TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    manifest_json TEXT NOT NULL,
                    updated_at REAL NOT NULL
                    )
                """)
                self._conn.execute("""
                    CREATE TABLE IF NOT EXISTS control_leases (
                    id TEXT PRIMARY KEY,
                    owner TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    owns_json TEXT NOT NULL,
                    fencing_token INTEGER NOT NULL UNIQUE,
                    created_at REAL NOT NULL,
                    renewed_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    released_at REAL
                    )
                """)
                self._conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_control_leases_active
                    ON control_leases(released_at, expires_at)
                """)
                self._conn.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)", (1, time.time())
                )
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    def sync_skills(self, manifests: list[dict[str, Any]]) -> None:
        now = time.time()
        with self._lock, self._conn:
            self._conn.execute("UPDATE skill_registry SET enabled=0 WHERE source='builtin'")
            for manifest in manifests:
                self._conn.execute("""
                    INSERT INTO skill_registry(id, version, category, source, enabled, manifest_json, updated_at)
                    VALUES (?, ?, ?, ?, 1, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        version=excluded.version,
                        category=excluded.category,
                        source=excluded.source,
                        enabled=1,
                        manifest_json=excluded.manifest_json,
                        updated_at=excluded.updated_at
                """, (
                    manifest["id"], manifest["version"], manifest["category"], manifest["source"],
                    json.dumps(manifest, ensure_ascii=False, sort_keys=True), now,
                ))

    def list_skills(self, enabled_only: bool = True) -> list[dict[str, Any]]:
        query = "SELECT manifest_json, enabled FROM skill_registry"
        if enabled_only:
            query += " WHERE enabled=1"
        query += " ORDER BY id"
        with self._lock:
            return [
                {**json.loads(row["manifest_json"]), "enabled": bool(row["enabled"])}
                for row in self._conn.execute(query)
            ]

    def acquire_lease(self, owner: str, mode: str, owns: list[str], ttl_seconds: int) -> dict[str, Any]:
        if not owner.strip():
            raise ValueError("owner must not be empty")
        if mode != "external":
            raise ValueError("invalid control mode")
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        now = time.time()
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._expire_leases(now)
                active = self._active_lease_row(now)
                if active is not None:
                    raise LeaseConflictError(f"control is already leased by {active['owner']}")
                row = self._conn.execute(
                    "SELECT COALESCE(MAX(fencing_token), 0) + 1 AS next_token FROM control_leases"
                ).fetchone()
                lease_id = str(uuid.uuid4())
                fencing_token = int(row["next_token"])
                expires_at = now + ttl_seconds
                self._conn.execute("""
                    INSERT INTO control_leases(
                        id, owner, mode, owns_json, fencing_token, created_at, renewed_at, expires_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    lease_id, owner, mode, json.dumps(sorted(set(owns))), fencing_token,
                    now, now, expires_at,
                ))
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        return self.get_lease(lease_id)

    def renew_lease(self, lease_id: str, fencing_token: int, ttl_seconds: int) -> dict[str, Any]:
        now = time.time()
        with self._lock, self._conn:
            cursor = self._conn.execute("""
                UPDATE control_leases
                SET renewed_at=?, expires_at=?
                WHERE id=? AND fencing_token=? AND released_at IS NULL AND expires_at>?
            """, (now, now + ttl_seconds, lease_id, fencing_token, now))
            if cursor.rowcount != 1:
                raise LeaseNotFoundError("active control lease not found")
        return self.get_lease(lease_id)

    def release_lease(self, lease_id: str, fencing_token: int) -> dict[str, Any]:
        now = time.time()
        with self._lock, self._conn:
            cursor = self._conn.execute("""
                UPDATE control_leases SET released_at=?
                WHERE id=? AND fencing_token=? AND released_at IS NULL
            """, (now, lease_id, fencing_token))
            if cursor.rowcount != 1:
                raise LeaseNotFoundError("active control lease not found")
        return self.get_lease(lease_id)

    def get_lease(self, lease_id: str) -> dict[str, Any]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM control_leases WHERE id=?", (lease_id,)).fetchone()
        if row is None:
            raise LeaseNotFoundError("control lease not found")
        return self._lease_dict(row)

    def get_active_lease(self) -> dict[str, Any] | None:
        now = time.time()
        with self._lock, self._conn:
            self._expire_leases(now)
            row = self._active_lease_row(now)
        return self._lease_dict(row) if row is not None else None

    def latest_fencing_token(self) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(MAX(fencing_token), 0) AS token FROM control_leases"
            ).fetchone()
        return int(row["token"])

    @contextmanager
    def control_guard(self, lease_id: str | None, fencing_token: int | None):
        """Hold lease ownership stable while a command is handed to the body."""
        now = time.time()
        with self._lock, self._conn:
            self._expire_leases(now)
            row = self._active_lease_row(now)
            if row is not None and (lease_id != row["id"] or fencing_token != row["fencing_token"]):
                raise LeaseConflictError("an active control lease owns companion actions")
            yield self._lease_dict(row) if row is not None else None

    @contextmanager
    def transition_guard(self):
        """Serialize lease transitions with runtime ownership changes."""
        with self._lock:
            yield

    def _expire_leases(self, now: float) -> None:
        self._conn.execute(
            "UPDATE control_leases SET released_at=expires_at WHERE released_at IS NULL AND expires_at<=?", (now,)
        )

    def _active_lease_row(self, now: float) -> sqlite3.Row | None:
        return self._conn.execute("""
            SELECT * FROM control_leases
            WHERE released_at IS NULL AND expires_at>?
            ORDER BY fencing_token DESC LIMIT 1
        """, (now,)).fetchone()

    @staticmethod
    def _lease_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "owner": row["owner"],
            "mode": row["mode"],
            "owns": json.loads(row["owns_json"]),
            "fencing_token": row["fencing_token"],
            "created_at": row["created_at"],
            "renewed_at": row["renewed_at"],
            "expires_at": row["expires_at"],
            "released_at": row["released_at"],
        }

    def close(self) -> None:
        with self._lock:
            self._conn.close()
