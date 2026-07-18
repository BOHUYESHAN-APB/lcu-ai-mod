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
            if 2 not in applied:
                self._conn.execute("""
                    CREATE TABLE IF NOT EXISTS task_runs (
                        id TEXT PRIMARY KEY,
                        schedule_id TEXT,
                        skill_id TEXT NOT NULL,
                        skill_version TEXT NOT NULL,
                        input_json TEXT NOT NULL,
                        completion TEXT NOT NULL,
                        status TEXT NOT NULL,
                        request_id TEXT,
                        progress REAL NOT NULL DEFAULT 0,
                        detail TEXT NOT NULL DEFAULT '',
                        error TEXT NOT NULL DEFAULT '',
                        created_at REAL NOT NULL,
                        dispatched_at REAL,
                        started_at REAL,
                        finished_at REAL,
                        FOREIGN KEY(schedule_id) REFERENCES schedules(id) ON DELETE SET NULL
                    )
                """)
                self._conn.execute("""
                    CREATE TABLE IF NOT EXISTS schedules (
                        id TEXT PRIMARY KEY,
                        name TEXT NOT NULL,
                        skill_id TEXT NOT NULL,
                        skill_version TEXT NOT NULL,
                        input_json TEXT NOT NULL,
                        enabled INTEGER NOT NULL,
                        clock TEXT NOT NULL,
                        trigger_type TEXT NOT NULL,
                        misfire_policy TEXT NOT NULL,
                        wall_run_at REAL,
                        wall_interval_seconds REAL,
                        game_interval_ticks INTEGER,
                        time_of_day_tick INTEGER,
                        next_wall_at REAL,
                        next_game_tick INTEGER,
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL,
                        last_triggered_at REAL
                    )
                """)
                self._conn.execute("""
                    CREATE TABLE IF NOT EXISTS event_stream (
                        cursor INTEGER PRIMARY KEY AUTOINCREMENT,
                        type TEXT NOT NULL,
                        aggregate_type TEXT NOT NULL,
                        aggregate_id TEXT NOT NULL,
                        payload_json TEXT NOT NULL,
                        created_at REAL NOT NULL
                    )
                """)
                self._conn.execute("""
                    CREATE TABLE IF NOT EXISTS scheduler_state (
                        id INTEGER PRIMARY KEY CHECK(id=1),
                        game_time INTEGER,
                        day_time INTEGER,
                        updated_at REAL NOT NULL
                    )
                """)
                self._conn.execute("CREATE INDEX IF NOT EXISTS idx_task_runs_request ON task_runs(request_id)")
                self._conn.execute("CREATE INDEX IF NOT EXISTS idx_task_runs_status ON task_runs(status, created_at)")
                self._conn.execute("CREATE INDEX IF NOT EXISTS idx_schedules_due ON schedules(enabled, next_wall_at)")
                self._conn.execute("CREATE INDEX IF NOT EXISTS idx_event_stream_created ON event_stream(created_at)")
                self._conn.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)", (2, time.time())
                )
            if 3 not in applied:
                self._conn.execute("""
                    CREATE TABLE IF NOT EXISTS scheduler_state (
                        id INTEGER PRIMARY KEY CHECK(id=1),
                        game_time INTEGER,
                        day_time INTEGER,
                        updated_at REAL NOT NULL
                    )
                """)
                self._conn.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)", (3, time.time())
                )
            if 4 not in applied:
                columns = {row["name"] for row in self._conn.execute("PRAGMA table_info(scheduler_state)")}
                if "scope_id" not in columns:
                    self._conn.execute("ALTER TABLE scheduler_state ADD COLUMN scope_id TEXT")
                self._conn.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)", (4, time.time())
                )
            if 5 not in applied:
                run_columns = {row["name"] for row in self._conn.execute("PRAGMA table_info(task_runs)")}
                if "scope_id" not in run_columns:
                    self._conn.execute("ALTER TABLE task_runs ADD COLUMN scope_id TEXT")
                schedule_columns = {row["name"] for row in self._conn.execute("PRAGMA table_info(schedules)")}
                if "scope_id" not in schedule_columns:
                    self._conn.execute("ALTER TABLE schedules ADD COLUMN scope_id TEXT")
                self._conn.execute("UPDATE schedules SET enabled=0 WHERE scope_id IS NULL")
                self._conn.execute("CREATE INDEX IF NOT EXISTS idx_schedules_scope_due ON schedules(scope_id, enabled, next_wall_at)")
                self._conn.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)", (5, time.time())
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

    def create_run(self, skill: dict[str, Any], input_data: dict[str, Any],
                   schedule_id: str | None = None, scope_id: str | None = None) -> dict[str, Any]:
        run_id = str(uuid.uuid4())
        now = time.time()
        with self._lock, self._conn:
            self._conn.execute("""
                INSERT INTO task_runs(
                    id, schedule_id, skill_id, skill_version, input_json, completion, status, created_at, scope_id
                ) VALUES (?, ?, ?, ?, ?, ?, 'queued', ?, ?)
            """, (
                run_id, schedule_id, skill["id"], skill["version"],
                json.dumps(input_data, ensure_ascii=False, sort_keys=True), skill["completion"], now, scope_id,
            ))
            self._append_event("run.created", "run", run_id, {"skill_id": skill["id"]}, now)
        return self.get_run(run_id)

    def trigger_schedule(self, schedule_id: str, skill: dict[str, Any], *,
                         next_wall_at: float | None, next_game_tick: int | None,
                         disable: bool) -> dict[str, Any]:
        """Create a scheduled run and advance its schedule atomically."""
        run_id = str(uuid.uuid4())
        now = time.time()
        with self._lock, self._conn:
            schedule = self._conn.execute(
                "SELECT input_json, enabled, scope_id FROM schedules WHERE id=?", (schedule_id,)
            ).fetchone()
            if schedule is None or not schedule["enabled"]:
                raise ValueError("schedule is no longer enabled")
            self._conn.execute("""
                INSERT INTO task_runs(
                    id, schedule_id, skill_id, skill_version, input_json, completion, status, created_at, scope_id
                ) VALUES (?, ?, ?, ?, ?, ?, 'queued', ?, ?)
            """, (
                run_id, schedule_id, skill["id"], skill["version"], schedule["input_json"],
                skill["completion"], now, schedule["scope_id"],
            ))
            self._conn.execute("""
                UPDATE schedules SET next_wall_at=?, next_game_tick=?, enabled=?,
                    updated_at=?, last_triggered_at=? WHERE id=?
            """, (next_wall_at, next_game_tick, 0 if disable else 1, now, now, schedule_id))
            self._append_event("run.created", "run", run_id, {"skill_id": skill["id"]}, now)
            self._append_event("schedule.triggered", "schedule", schedule_id, {"run_id": run_id}, now)
        return self.get_run(run_id)

    def mark_run_dispatched(self, run_id: str, request_id: str) -> dict[str, Any]:
        now = time.time()
        with self._lock, self._conn:
            cursor = self._conn.execute("""
                UPDATE task_runs SET status='dispatched', request_id=?, dispatched_at=?
                WHERE id=? AND status='queued'
            """, (request_id, now, run_id))
            if cursor.rowcount != 1:
                raise ValueError("task run is no longer queued")
            self._append_event("run.dispatched", "run", run_id, {"request_id": request_id}, now)
        return self.get_run(run_id)

    def update_run_response(self, request_id: str, success: bool, detail: str = "", error: str = "") -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM task_runs WHERE request_id=?", (request_id,)).fetchone()
            if row is None or row["status"] in {"succeeded", "failed", "cancelled", "unknown"}:
                return None
            now = time.time()
            if not success:
                status, event_type, finished_at = "failed", "run.failed", now
            elif row["completion"] == "response":
                status, event_type, finished_at = "succeeded", "run.succeeded", now
            else:
                status, event_type, finished_at = "running", "run.started", None
            with self._conn:
                self._conn.execute("""
                    UPDATE task_runs SET status=?, progress=?, detail=?, error=?,
                        started_at=COALESCE(started_at, ?), finished_at=?
                    WHERE id=?
                """, (status, 1.0 if status == "succeeded" else row["progress"], detail, error, now, finished_at, row["id"]))
                self._append_event(event_type, "run", row["id"], {"detail": detail, "error": error}, now)
        return self.get_run(row["id"])

    def update_run_progress(self, request_id: str, progress: float, detail: str = "") -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM task_runs WHERE request_id=?", (request_id,)).fetchone()
            if row is None or row["status"] in {"succeeded", "failed", "cancelled", "unknown"}:
                return None
            now = time.time()
            if progress >= 1.0:
                status, event_type, finished_at = "succeeded", "run.succeeded", now
            elif progress <= 0.0 and detail:
                status, event_type, finished_at = "failed", "run.failed", now
            else:
                status, event_type, finished_at = "running", "run.progress", None
            bounded = max(0.0, min(1.0, progress))
            with self._conn:
                self._conn.execute("""
                    UPDATE task_runs SET status=?, progress=?, detail=?, started_at=COALESCE(started_at, ?), finished_at=?
                    WHERE id=?
                """, (status, bounded, detail, now, finished_at, row["id"]))
                self._append_event(event_type, "run", row["id"], {"progress": bounded, "detail": detail}, now)
        return self.get_run(row["id"])

    def mark_inflight_unknown(self, detail: str) -> int:
        now = time.time()
        with self._lock, self._conn:
            rows = list(self._conn.execute(
                "SELECT id FROM task_runs WHERE status IN ('dispatched', 'running')"
            ))
            for row in rows:
                self._conn.execute(
                    "UPDATE task_runs SET status='unknown', detail=?, finished_at=? WHERE id=?",
                    (detail, now, row["id"]),
                )
                self._append_event("run.unknown", "run", row["id"], {"detail": detail}, now)
        return len(rows)

    def expire_stale_runs(self, max_age_seconds: float, detail: str) -> int:
        now = time.time()
        cutoff = now - max_age_seconds
        with self._lock, self._conn:
            rows = list(self._conn.execute("""
                SELECT id FROM task_runs
                WHERE status IN ('dispatched', 'running')
                  AND COALESCE(started_at, dispatched_at, created_at) < ?
            """, (cutoff,)))
            for row in rows:
                self._conn.execute(
                    "UPDATE task_runs SET status='unknown', detail=?, finished_at=? WHERE id=?",
                    (detail, now, row["id"]),
                )
                self._append_event("run.unknown", "run", row["id"], {"detail": detail}, now)
        return len(rows)

    def cancel_queued_runs(self, detail: str) -> int:
        now = time.time()
        with self._lock, self._conn:
            rows = list(self._conn.execute("SELECT id FROM task_runs WHERE status='queued'"))
            for row in rows:
                self._conn.execute(
                    "UPDATE task_runs SET status='cancelled', detail=?, finished_at=? WHERE id=?",
                    (detail, now, row["id"]),
                )
                self._append_event("run.cancelled", "run", row["id"], {"detail": detail}, now)
        return len(rows)

    def mark_run_unknown(self, run_id: str, detail: str) -> dict[str, Any]:
        now = time.time()
        with self._lock, self._conn:
            cursor = self._conn.execute("""
                UPDATE task_runs SET status='unknown', detail=?, finished_at=?
                WHERE id=? AND status IN ('dispatched', 'running')
            """, (detail, now, run_id))
            if cursor.rowcount == 1:
                self._append_event("run.unknown", "run", run_id, {"detail": detail}, now)
        return self.get_run(run_id)

    def fail_run(self, run_id: str, error: str) -> dict[str, Any]:
        now = time.time()
        with self._lock, self._conn:
            self._conn.execute("""
                UPDATE task_runs SET status='failed', error=?, finished_at=?
                WHERE id=? AND status IN ('queued', 'dispatched', 'running')
            """, (error, now, run_id))
            self._append_event("run.failed", "run", run_id, {"error": error}, now)
        return self.get_run(run_id)

    def cancel_run(self, run_id: str, detail: str = "cancelled by controller") -> dict[str, Any]:
        now = time.time()
        with self._lock, self._conn:
            cursor = self._conn.execute("""
                UPDATE task_runs SET status='cancelled', detail=?, finished_at=?
                WHERE id=? AND status IN ('queued', 'dispatched', 'running')
            """, (detail, now, run_id))
            if cursor.rowcount != 1:
                raise ValueError("task run is not cancellable")
            self._append_event("run.cancelled", "run", run_id, {"detail": detail}, now)
        return self.get_run(run_id)

    def get_run(self, run_id: str) -> dict[str, Any]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM task_runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise KeyError("task run not found")
        return self._run_dict(row)

    def list_runs(self, limit: int = 50, status: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM task_runs"
        params: list[Any] = []
        if status:
            query += " WHERE status=?"
            params.append(status)
        query += " ORDER BY created_at ASC" if status == "queued" else " ORDER BY created_at DESC"
        query += " LIMIT ?"
        params.append(max(1, min(200, limit)))
        with self._lock:
            return [self._run_dict(row) for row in self._conn.execute(query, params)]

    def has_active_runs(self) -> bool:
        with self._lock:
            row = self._conn.execute("""
                SELECT 1 FROM task_runs WHERE status IN ('dispatched', 'running') LIMIT 1
            """).fetchone()
        return row is not None

    def get_scheduler_clock(self) -> dict[str, Any]:
        with self._lock:
            row = self._conn.execute("SELECT game_time, day_time, scope_id FROM scheduler_state WHERE id=1").fetchone()
        return {"game_time": None, "day_time": None, "scope_id": None} if row is None else dict(row)

    def set_scheduler_clock(self, game_time: int | None, day_time: int | None, scope_id: str) -> None:
        with self._lock, self._conn:
            self._conn.execute("""
                INSERT INTO scheduler_state(id, game_time, day_time, updated_at, scope_id) VALUES (1, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    game_time=excluded.game_time, day_time=excluded.day_time,
                    updated_at=excluded.updated_at, scope_id=excluded.scope_id
            """, (game_time, day_time, time.time(), scope_id))

    def create_schedule(self, schedule: dict[str, Any]) -> dict[str, Any]:
        schedule_id = str(uuid.uuid4())
        now = time.time()
        with self._lock, self._conn:
            self._conn.execute("""
                INSERT INTO schedules(
                    id, name, skill_id, skill_version, input_json, enabled, clock, trigger_type,
                    misfire_policy, wall_run_at, wall_interval_seconds, game_interval_ticks,
                    time_of_day_tick, next_wall_at, next_game_tick, created_at, updated_at, scope_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                schedule_id, schedule["name"], schedule["skill_id"], schedule["skill_version"],
                json.dumps(schedule["input"], ensure_ascii=False, sort_keys=True),
                1 if schedule.get("enabled", True) else 0, schedule["clock"], schedule["trigger_type"],
                schedule["misfire_policy"], schedule.get("wall_run_at"), schedule.get("wall_interval_seconds"),
                schedule.get("game_interval_ticks"), schedule.get("time_of_day_tick"),
                schedule.get("next_wall_at"), schedule.get("next_game_tick"), now, now,
                schedule.get("scope_id", "default"),
            ))
            self._append_event("schedule.created", "schedule", schedule_id, {"name": schedule["name"]}, now)
        return self.get_schedule(schedule_id)

    def get_schedule(self, schedule_id: str) -> dict[str, Any]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM schedules WHERE id=?", (schedule_id,)).fetchone()
        if row is None:
            raise KeyError("schedule not found")
        return self._schedule_dict(row)

    def list_schedules(self, enabled_only: bool = False) -> list[dict[str, Any]]:
        query = "SELECT * FROM schedules"
        if enabled_only:
            query += " WHERE enabled=1"
        query += " ORDER BY created_at DESC"
        with self._lock:
            return [self._schedule_dict(row) for row in self._conn.execute(query)]

    def set_schedule_enabled(self, schedule_id: str, enabled: bool) -> dict[str, Any]:
        now = time.time()
        with self._lock, self._conn:
            schedule = self._conn.execute(
                "SELECT trigger_type, next_wall_at FROM schedules WHERE id=?", (schedule_id,)
            ).fetchone()
            if schedule is None:
                raise KeyError("schedule not found")
            if enabled and schedule["trigger_type"] == "once" and schedule["next_wall_at"] is None:
                raise ValueError("a consumed one-shot schedule cannot be re-enabled")
            cursor = self._conn.execute(
                "UPDATE schedules SET enabled=?, updated_at=? WHERE id=?",
                (1 if enabled else 0, now, schedule_id),
            )
            self._append_event("schedule.updated", "schedule", schedule_id, {"enabled": enabled}, now)
        return self.get_schedule(schedule_id)

    def delete_schedule(self, schedule_id: str) -> None:
        now = time.time()
        with self._lock, self._conn:
            self._append_event("schedule.deleted", "schedule", schedule_id, {}, now)
            cursor = self._conn.execute("DELETE FROM schedules WHERE id=?", (schedule_id,))
            if cursor.rowcount != 1:
                raise KeyError("schedule not found")

    def advance_schedule(self, schedule_id: str, *, next_wall_at: float | None,
                         next_game_tick: int | None, triggered: bool, skipped: bool = False,
                         disable: bool = False) -> dict[str, Any]:
        now = time.time()
        with self._lock, self._conn:
            cursor = self._conn.execute("""
                UPDATE schedules SET next_wall_at=?, next_game_tick=?, enabled=?, updated_at=?,
                    last_triggered_at=CASE WHEN ? THEN ? ELSE last_triggered_at END
                WHERE id=? AND enabled=1
            """, (
                next_wall_at, next_game_tick, 0 if disable else 1, now,
                1 if triggered else 0, now, schedule_id,
            ))
            if cursor.rowcount != 1:
                raise KeyError("enabled schedule not found")
            event_type = "schedule.misfired" if skipped else "schedule.triggered" if triggered else "schedule.rebased"
            self._append_event(event_type, "schedule", schedule_id, {}, now)
        return self.get_schedule(schedule_id)

    def list_events(self, after: int = 0, limit: int = 100, latest: bool = False) -> list[dict[str, Any]]:
        bounded_limit = max(1, min(500, limit))
        with self._lock:
            if latest and after == 0:
                rows = list(self._conn.execute("""
                    SELECT * FROM event_stream ORDER BY cursor DESC LIMIT ?
                """, (bounded_limit,)))
                return [self._event_dict(row) for row in reversed(rows)]
            rows = self._conn.execute("""
                SELECT * FROM event_stream WHERE cursor>? ORDER BY cursor LIMIT ?
            """, (max(0, after), bounded_limit))
            return [self._event_dict(row) for row in rows]

    def append_event(self, event_type: str, aggregate_type: str, aggregate_id: str,
                     payload: dict[str, Any]) -> dict[str, Any]:
        now = time.time()
        with self._lock, self._conn:
            cursor = self._append_event(event_type, aggregate_type, aggregate_id, payload, now)
        return self.list_events(after=cursor - 1, limit=1)[0]

    def _append_event(self, event_type: str, aggregate_type: str, aggregate_id: str,
                      payload: dict[str, Any], now: float) -> int:
        cursor = self._conn.execute("""
            INSERT INTO event_stream(type, aggregate_type, aggregate_id, payload_json, created_at)
            VALUES (?, ?, ?, ?, ?)
        """, (event_type, aggregate_type, aggregate_id, json.dumps(payload, ensure_ascii=False), now))
        return int(cursor.lastrowid)

    @staticmethod
    def _run_dict(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["input"] = json.loads(result.pop("input_json"))
        return result

    @staticmethod
    def _schedule_dict(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["input"] = json.loads(result.pop("input_json"))
        result["enabled"] = bool(result["enabled"])
        return result

    @staticmethod
    def _event_dict(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["payload"] = json.loads(result.pop("payload_json"))
        return result

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
            if row is None and (lease_id is not None or fencing_token is not None):
                raise LeaseConflictError("supplied control lease is not active")
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
