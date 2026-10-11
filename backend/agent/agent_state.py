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
            if 6 not in applied:
                run_columns = {row["name"] for row in self._conn.execute("PRAGMA table_info(task_runs)")}
                additions = {
                    "run_kind": "TEXT NOT NULL DEFAULT 'skill'",
                    "parent_run_id": "TEXT",
                    "root_run_id": "TEXT",
                    "step_index": "INTEGER",
                    "step_key": "TEXT",
                    "workflow_spec_json": "TEXT",
                    "current_step": "INTEGER",
                    "active_child_id": "TEXT",
                    "cancel_requested_at": "REAL",
                    "lease_id": "TEXT",
                    "fencing_token": "INTEGER",
                }
                for name, definition in additions.items():
                    if name not in run_columns:
                        self._conn.execute(f"ALTER TABLE task_runs ADD COLUMN {name} {definition}")
                self._conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_task_runs_parent ON task_runs(parent_run_id, step_index)"
                )
                self._conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_task_runs_root ON task_runs(root_run_id, created_at)"
                )
                self._conn.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)", (6, time.time())
                )
            if 7 not in applied:
                run_columns = {row["name"] for row in self._conn.execute("PRAGMA table_info(task_runs)")}
                additions = {
                    "task_state_json": "TEXT",
                    "result_json": "TEXT",
                    "pending_request_id": "TEXT",
                }
                for name, definition in additions.items():
                    if name not in run_columns:
                        self._conn.execute(f"ALTER TABLE task_runs ADD COLUMN {name} {definition}")
                self._conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_task_runs_pending_request ON task_runs(pending_request_id)"
                )
                self._conn.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)", (7, time.time())
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

    def create_workflow_run(self, workflow: dict[str, Any], *, scope_id: str | None = None,
                            lease_id: str | None = None, fencing_token: int | None = None) -> dict[str, Any]:
        steps = workflow.get("steps", [])
        if not steps:
            raise ValueError("workflow must contain at least one step")
        run_id = str(uuid.uuid4())
        child_id = str(uuid.uuid4())
        now = time.time()
        first = steps[0]
        with self._lock, self._conn:
            self._conn.execute("""
                INSERT INTO task_runs(
                    id, skill_id, skill_version, input_json, completion, status, created_at,
                    scope_id, run_kind, root_run_id, workflow_spec_json, current_step, active_child_id,
                    lease_id, fencing_token
                ) VALUES (?, ?, ?, ?, 'workflow', 'queued', ?, ?, 'workflow', ?, ?, 0, ?, ?, ?)
            """, (
                run_id, workflow["id"], workflow["version"],
                json.dumps(workflow.get("parameters", {}), ensure_ascii=False, sort_keys=True),
                now, scope_id, run_id,
                json.dumps(workflow, ensure_ascii=False, sort_keys=True), child_id, lease_id, fencing_token,
            ))
            self._insert_workflow_child(child_id, run_id, run_id, 0, first, scope_id, now)
            self._append_event("run.created", "run", run_id, {
                "run_kind": "workflow", "step_count": len(steps),
            }, now)
            self._append_event("run.created", "run", child_id, {
                "skill_id": first["skill_id"], "parent_run_id": run_id, "step_index": 0,
            }, now)
        return self.get_run(run_id)

    def create_dynamic_workflow_run(self, workflow: dict[str, Any], task_state: dict[str, Any], *,
                                    scope_id: str | None = None, lease_id: str | None = None,
                                    fencing_token: int | None = None) -> dict[str, Any]:
        run_id = str(uuid.uuid4())
        now = time.time()
        with self._lock, self._conn:
            self._conn.execute("""
                INSERT INTO task_runs(
                    id, skill_id, skill_version, input_json, completion, status, created_at,
                    scope_id, run_kind, root_run_id, workflow_spec_json, task_state_json,
                    lease_id, fencing_token
                ) VALUES (?, ?, ?, ?, 'workflow', 'queued', ?, ?, 'workflow', ?, ?, ?, ?, ?)
            """, (
                run_id, workflow["id"], workflow["version"],
                json.dumps(workflow.get("parameters", {}), ensure_ascii=False, sort_keys=True),
                now, scope_id, run_id, json.dumps(workflow, ensure_ascii=False, sort_keys=True),
                json.dumps(task_state, ensure_ascii=False, sort_keys=True), lease_id, fencing_token,
            ))
            self._append_event("run.created", "run", run_id, {
                "run_kind": "workflow", "dynamic_handler": workflow.get("dynamic_handler"),
            }, now)
        return self.get_run(run_id)

    def update_dynamic_workflow(self, run_id: str, task_state: dict[str, Any], *,
                                status: str | None = None, pending_request_id: str | None = None,
                                active_child_id: str | None = None, detail: str = "") -> dict[str, Any]:
        now = time.time()
        next_status = status or "running"
        with self._lock, self._conn:
            cursor = self._conn.execute("""
                UPDATE task_runs SET task_state_json=?, status=?, pending_request_id=?,
                    active_child_id=?, detail=?, started_at=COALESCE(started_at, ?)
                WHERE id=? AND run_kind='workflow' AND status IN ('queued', 'running')
            """, (
                json.dumps(task_state, ensure_ascii=False, sort_keys=True), next_status,
                pending_request_id, active_child_id, detail, now, run_id,
            ))
            if cursor.rowcount != 1:
                raise ValueError("dynamic workflow is not active")
        return self.get_run(run_id)

    def create_dynamic_workflow_child(self, parent_id: str, step: dict[str, Any],
                                      task_state: dict[str, Any]) -> dict[str, Any]:
        child_id = str(uuid.uuid4())
        now = time.time()
        with self._lock, self._conn:
            parent = self._conn.execute("SELECT * FROM task_runs WHERE id=?", (parent_id,)).fetchone()
            if parent is None or parent["status"] not in {"queued", "running"} or parent["active_child_id"]:
                raise ValueError("dynamic workflow cannot queue a child")
            step_index = int(parent["current_step"] or 0)
            self._insert_workflow_child(
                child_id, parent_id, parent["root_run_id"] or parent_id, step_index,
                step, parent["scope_id"], now,
            )
            self._conn.execute("""
                UPDATE task_runs SET status='queued', active_child_id=?, current_step=?,
                    task_state_json=?, pending_request_id=NULL, detail=''
                WHERE id=?
            """, (
                child_id, step_index + 1,
                json.dumps(task_state, ensure_ascii=False, sort_keys=True), parent_id,
            ))
            self._append_event("run.created", "run", child_id, {
                "skill_id": step["skill_id"], "parent_run_id": parent_id, "step_index": step_index,
            }, now)
            self._append_event("workflow.step_queued", "run", parent_id, {
                "child_run_id": child_id, "step_index": step_index,
            }, now)
        return self.get_run(child_id)

    def finish_dynamic_workflow(self, run_id: str, status: str, result: dict[str, Any],
                                detail: str = "", error: str = "") -> dict[str, Any]:
        if status not in {"succeeded", "failed", "cancelled", "unknown"}:
            raise ValueError("invalid dynamic workflow status")
        now = time.time()
        progress = 1.0 if status == "succeeded" else 0.0
        with self._lock, self._conn:
            cursor = self._conn.execute("""
                UPDATE task_runs SET status=?, progress=?, detail=?, error=?, result_json=?,
                    pending_request_id=NULL, active_child_id=NULL, finished_at=?
                WHERE id=? AND run_kind='workflow' AND status IN ('queued', 'running')
            """, (
                status, progress, detail, error,
                json.dumps(result, ensure_ascii=False, sort_keys=True), now, run_id,
            ))
            if cursor.rowcount:
                self._append_event(f"run.{status}", "run", run_id, {
                    "detail": detail, "code": error, "result": result,
                }, now)
        return self.get_run(run_id)

    def reset_dynamic_scan_requests(self, detail: str) -> int:
        with self._lock, self._conn:
            cursor = self._conn.execute("""
                UPDATE task_runs SET status='queued', pending_request_id=NULL, detail=?
                WHERE run_kind='workflow' AND status IN ('queued', 'running')
                  AND pending_request_id IS NOT NULL AND active_child_id IS NULL
            """, (detail,))
        return cursor.rowcount

    def get_run_by_pending_request(self, request_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT id FROM task_runs WHERE pending_request_id=?", (request_id,)
            ).fetchone()
        return self.get_run(row["id"]) if row is not None else None

    def mark_dynamic_harvests_unknown(self, detail: str) -> int:
        now = time.time()
        with self._lock, self._conn:
            rows = list(self._conn.execute("""
                SELECT child.*, parent.task_state_json AS parent_task_state_json
                FROM task_runs child JOIN task_runs parent ON parent.id=child.parent_run_id
                WHERE child.skill_id='world.harvest_crop_at'
                  AND child.status IN ('dispatched', 'running')
                  AND parent.run_kind='workflow' AND parent.status IN ('queued', 'running')
                  AND json_extract(parent.workflow_spec_json, '$.dynamic_handler')='farm_region'
            """))
            for row in rows:
                self._conn.execute("""
                    UPDATE task_runs SET status='unknown', detail=?, finished_at=? WHERE id=?
                """, (detail, now, row["id"]))
                task_state = json.loads(row["parent_task_state_json"] or "{}")
                result_status = "partial_unknown" if task_state.get("harvested", 0) else "unknown"
                result = self._dynamic_farm_result(task_state, result_status)
                self._conn.execute("""
                    UPDATE task_runs SET status='unknown', detail=?, result_json=?,
                        active_child_id=NULL, pending_request_id=NULL, finished_at=? WHERE id=?
                """, (result_status, json.dumps(result, ensure_ascii=False, sort_keys=True), now, row["parent_run_id"]))
                self._append_event("run.unknown", "run", row["id"], {"detail": detail}, now)
                self._append_event("run.unknown", "run", row["parent_run_id"], {
                    "detail": result_status, "child_run_id": row["id"], "result": result,
                }, now)
        return len(rows)

    def finalize_dynamic_unknown_children(self, detail: str) -> int:
        now = time.time()
        with self._lock, self._conn:
            rows = list(self._conn.execute("""
                SELECT child.*, parent.task_state_json AS parent_task_state_json
                FROM task_runs child JOIN task_runs parent ON parent.id=child.parent_run_id
                WHERE child.status='unknown' AND parent.active_child_id=child.id
                  AND parent.status IN ('queued', 'running')
                  AND json_extract(parent.workflow_spec_json, '$.dynamic_handler')='farm_region'
            """))
            for row in rows:
                task_state = json.loads(row["parent_task_state_json"] or "{}")
                result_status = "partial_unknown" if task_state.get("harvested", 0) else "unknown"
                result = self._dynamic_farm_result(task_state, result_status)
                self._conn.execute("""
                    UPDATE task_runs SET status='unknown', detail=?, result_json=?,
                        active_child_id=NULL, pending_request_id=NULL, finished_at=? WHERE id=?
                """, (result_status, json.dumps(result, ensure_ascii=False, sort_keys=True), now, row["parent_run_id"]))
                self._append_event("run.unknown", "run", row["parent_run_id"], {
                    "detail": detail, "child_run_id": row["id"], "result": result,
                }, now)
        return len(rows)

    @staticmethod
    def _dynamic_farm_result(task_state: dict[str, Any], status: str) -> dict[str, Any]:
        return {
            "status": status,
            "harvested": int(task_state.get("harvested", 0)),
            "candidate_attempts": int(task_state.get("candidate_attempts", 0)),
            "scan_attempts": int(task_state.get("scan_attempts", 0)),
            "restoration_obligations": list(task_state.get("restoration_obligations", [])),
            "failures": list(task_state.get("failures", [])),
        }

    def set_workflow_control(self, run_id: str, lease_id: str | None,
                             fencing_token: int | None) -> dict[str, Any]:
        with self._lock, self._conn:
            cursor = self._conn.execute("""
                UPDATE task_runs SET lease_id=?, fencing_token=?
                WHERE id=? AND run_kind='workflow' AND status='queued'
            """, (lease_id, fencing_token, run_id))
            if cursor.rowcount != 1:
                raise ValueError("workflow is not queued")
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
            row = self._conn.execute("SELECT parent_run_id FROM task_runs WHERE id=?", (run_id,)).fetchone()
            if row is not None and row["parent_run_id"]:
                self._conn.execute("""
                    UPDATE task_runs SET status='running', detail='', started_at=COALESCE(started_at, ?)
                    WHERE id=? AND run_kind='workflow' AND active_child_id=? AND status='queued'
                """, (now, row["parent_run_id"], run_id))
                self._append_event("workflow.step_dispatched", "run", row["parent_run_id"], {
                    "child_run_id": run_id,
                }, now)
        return self.get_run(run_id)

    def update_run_response(self, request_id: str, success: bool, detail: str = "", error: str = "") -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM task_runs WHERE request_id=?", (request_id,)).fetchone()
            if row is None or row["status"] in {"succeeded", "failed", "cancelled", "unknown"}:
                return None
            now = time.time()
            with self._conn:
                if not success:
                    self._finish_run_locked(row, "failed", detail, error, now)
                elif row["completion"] == "response":
                    self._finish_run_locked(row, "succeeded", detail, "", now)
                else:
                    self._conn.execute("""
                        UPDATE task_runs SET status='running', detail=?, error='',
                            started_at=COALESCE(started_at, ?) WHERE id=?
                    """, (detail, now, row["id"]))
                    self._append_event("run.started", "run", row["id"], {"detail": detail}, now)
                    self._update_parent_progress_locked(row, float(row["progress"] or 0.0), detail, now)
        return self.get_run(row["id"])

    def update_run_progress(self, request_id: str, progress: float, detail: str = "") -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM task_runs WHERE request_id=?", (request_id,)).fetchone()
            if row is None or row["status"] in {"succeeded", "failed", "cancelled", "unknown"}:
                return None
            now = time.time()
            if row["completion"] == "outcome":
                status, event_type, finished_at = "running", "run.progress", None
            elif progress >= 1.0:
                status, event_type, finished_at = "succeeded", "run.succeeded", now
            elif progress <= 0.0 and detail:
                status, event_type, finished_at = "failed", "run.failed", now
            else:
                status, event_type, finished_at = "running", "run.progress", None
            bounded = max(0.0, min(1.0, progress))
            with self._conn:
                if status in {"succeeded", "failed"}:
                    self._finish_run_locked(row, status, detail, "", now, progress=bounded)
                else:
                    self._conn.execute("""
                        UPDATE task_runs SET status=?, progress=?, detail=?, started_at=COALESCE(started_at, ?)
                        WHERE id=?
                    """, (status, bounded, detail, now, row["id"]))
                    self._append_event(event_type, "run", row["id"], {"progress": bounded, "detail": detail}, now)
                    self._update_parent_progress_locked(row, bounded, detail, now)
        return self.get_run(row["id"])

    def update_run_outcome(self, request_id: str, status: str, detail: str = "", code: str = "") -> dict[str, Any] | None:
        normalized = str(status).strip().lower()
        if normalized not in {"succeeded", "failed", "cancelled"}:
            raise ValueError(f"invalid operation outcome: {status}")
        with self._lock:
            row = self._conn.execute("SELECT * FROM task_runs WHERE request_id=?", (request_id,)).fetchone()
            if row is None or row["status"] in {"succeeded", "failed", "cancelled", "unknown"}:
                return None
            now = time.time()
            progress = 1.0 if normalized == "succeeded" else max(0.0, min(1.0, float(row["progress"] or 0.0)))
            with self._conn:
                self._finish_run_locked(row, normalized, detail, code, now, progress=progress)
        return self.get_run(row["id"])

    def _finish_run_locked(self, row: sqlite3.Row, status: str, detail: str, error: str,
                           now: float, progress: float | None = None) -> str | None:
        bounded = 1.0 if status == "succeeded" else max(
            0.0, min(1.0, float(row["progress"] or 0.0) if progress is None else progress)
        )
        cursor = self._conn.execute("""
            UPDATE task_runs SET status=?, progress=?, detail=?, error=?,
                started_at=COALESCE(started_at, dispatched_at, ?), finished_at=?
            WHERE id=? AND status IN ('queued', 'dispatched', 'running')
        """, (status, bounded, detail, error, now, now, row["id"]))
        if cursor.rowcount != 1:
            return None
        self._append_event(f"run.{status}", "run", row["id"], {
            "detail": detail, "code": error,
        }, now)
        if not row["parent_run_id"]:
            return None
        return self._advance_workflow_locked(row, status, detail, error, now)

    def _advance_workflow_locked(self, child: sqlite3.Row, child_status: str, detail: str,
                                 error: str, now: float) -> str | None:
        parent = self._conn.execute("SELECT * FROM task_runs WHERE id=?", (child["parent_run_id"],)).fetchone()
        if parent is None or parent["status"] in {"succeeded", "failed", "cancelled", "unknown"}:
            return None
        if parent["active_child_id"] != child["id"]:
            return None
        workflow = json.loads(parent["workflow_spec_json"] or "{}")
        if workflow.get("dynamic_handler"):
            return None
        steps = workflow.get("steps", [])
        if child_status == "unknown":
            parent_status = "unknown"
        elif parent["cancel_requested_at"] is not None:
            parent_status = "cancelled"
        elif child_status != "succeeded":
            parent_status = child_status
        else:
            next_index = int(child["step_index"] or 0) + 1
            if next_index < len(steps):
                next_id = str(uuid.uuid4())
                self._insert_workflow_child(
                    next_id, parent["id"], parent["root_run_id"] or parent["id"], next_index,
                    steps[next_index], parent["scope_id"], now,
                )
                self._conn.execute("""
                    UPDATE task_runs SET status='queued', current_step=?, active_child_id=?,
                        progress=?, detail='', error='' WHERE id=?
                """, (next_index, next_id, next_index / len(steps), parent["id"]))
                self._append_event("run.created", "run", next_id, {
                    "skill_id": steps[next_index]["skill_id"], "parent_run_id": parent["id"],
                    "step_index": next_index,
                }, now)
                self._append_event("workflow.step_queued", "run", parent["id"], {
                    "child_run_id": next_id, "step_index": next_index,
                }, now)
                return next_id
            parent_status = "succeeded"

        parent_progress = 1.0 if parent_status == "succeeded" else max(
            0.0, min(1.0, (int(child["step_index"] or 0) + float(child["progress"] or 0.0)) / max(1, len(steps)))
        )
        parent_detail = (
            "cancelled by controller"
            if parent_status == "cancelled" and parent["cancel_requested_at"] is not None
            else detail
        )
        self._conn.execute("""
            UPDATE task_runs SET status=?, progress=?, detail=?, error=?, active_child_id=NULL, finished_at=?
            WHERE id=? AND status IN ('queued', 'running')
        """, (parent_status, parent_progress, parent_detail, error, now, parent["id"]))
        self._append_event(f"run.{parent_status}", "run", parent["id"], {
            "detail": parent_detail, "code": error, "child_run_id": child["id"],
        }, now)
        return None

    def _update_parent_progress_locked(self, child: sqlite3.Row, progress: float,
                                       detail: str, now: float) -> None:
        if not child["parent_run_id"]:
            return
        parent = self._conn.execute(
            "SELECT workflow_spec_json, active_child_id FROM task_runs WHERE id=?",
            (child["parent_run_id"],),
        ).fetchone()
        if parent is None or parent["active_child_id"] != child["id"]:
            return
        steps = json.loads(parent["workflow_spec_json"] or "{}").get("steps", [])
        overall = (int(child["step_index"] or 0) + progress) / max(1, len(steps))
        self._conn.execute("""
            UPDATE task_runs SET status='running', progress=?, detail=?
            WHERE id=? AND run_kind='workflow' AND status IN ('queued', 'running')
        """, (max(0.0, min(1.0, overall)), detail, child["parent_run_id"]))
        self._append_event("workflow.progress", "run", child["parent_run_id"], {
            "child_run_id": child["id"], "progress": overall, "detail": detail,
        }, now)

    def _insert_workflow_child(self, child_id: str, parent_id: str, root_id: str,
                               step_index: int, step: dict[str, Any], scope_id: str | None,
                               now: float) -> None:
        self._conn.execute("""
            INSERT INTO task_runs(
                id, skill_id, skill_version, input_json, completion, status, created_at, scope_id,
                run_kind, parent_run_id, root_run_id, step_index, step_key
            ) VALUES (?, ?, ?, ?, ?, 'queued', ?, ?, 'skill', ?, ?, ?, ?)
        """, (
            child_id, step["skill_id"], step["skill_version"],
            json.dumps(step["input"], ensure_ascii=False, sort_keys=True), step["completion"],
            now, scope_id, parent_id, root_id, step_index, step["key"],
        ))

    def mark_inflight_unknown(self, detail: str) -> int:
        now = time.time()
        with self._lock, self._conn:
            rows = list(self._conn.execute(
                "SELECT * FROM task_runs WHERE run_kind='skill' AND status IN ('dispatched', 'running')"
            ))
            for row in rows:
                self._finish_run_locked(row, "unknown", detail, "", now)
        return len(rows)

    def expire_stale_runs(self, max_age_seconds: float, detail: str) -> int:
        now = time.time()
        cutoff = now - max_age_seconds
        with self._lock, self._conn:
            rows = list(self._conn.execute("""
                SELECT * FROM task_runs
                WHERE run_kind='skill' AND status IN ('dispatched', 'running')
                  AND COALESCE(started_at, dispatched_at, created_at) < ?
            """, (cutoff,)))
            for row in rows:
                self._finish_run_locked(row, "unknown", detail, "", now)
        return len(rows)

    def cancel_queued_runs(self, detail: str) -> int:
        now = time.time()
        with self._lock, self._conn:
            rows = list(self._conn.execute(
                "SELECT * FROM task_runs WHERE run_kind='skill' AND status='queued'"
            ))
            for row in rows:
                self._finish_run_locked(row, "cancelled", detail, "", now)
        return len(rows)

    def mark_run_unknown(self, run_id: str, detail: str) -> dict[str, Any]:
        now = time.time()
        with self._lock, self._conn:
            row = self._conn.execute("SELECT * FROM task_runs WHERE id=?", (run_id,)).fetchone()
            if row is not None and row["status"] in {"dispatched", "running"}:
                self._finish_run_locked(row, "unknown", detail, "", now)
        return self.get_run(run_id)

    def fail_run(self, run_id: str, error: str) -> dict[str, Any]:
        now = time.time()
        with self._lock, self._conn:
            row = self._conn.execute("SELECT * FROM task_runs WHERE id=?", (run_id,)).fetchone()
            if row is not None and row["status"] in {"queued", "dispatched", "running"}:
                self._finish_run_locked(row, "failed", "", error, now)
        return self.get_run(run_id)

    def cancel_run(self, run_id: str, detail: str = "cancelled by controller") -> dict[str, Any]:
        now = time.time()
        with self._lock, self._conn:
            row = self._conn.execute("SELECT * FROM task_runs WHERE id=?", (run_id,)).fetchone()
            if row is None or row["status"] not in {"queued", "dispatched", "running"}:
                raise ValueError("task run is not cancellable")
            self._finish_run_locked(row, "cancelled", detail, "", now)
        return self.get_run(run_id)

    def request_workflow_cancel(self, run_id: str) -> dict[str, Any]:
        now = time.time()
        with self._lock, self._conn:
            row = self._conn.execute("SELECT * FROM task_runs WHERE id=?", (run_id,)).fetchone()
            if row is None:
                raise KeyError("task run not found")
            if row["run_kind"] != "workflow":
                raise ValueError("task run is not a workflow")
            if row["status"] in {"succeeded", "failed", "cancelled", "unknown"}:
                return self._run_dict(row)
            self._conn.execute("""
                UPDATE task_runs SET cancel_requested_at=?, detail='cancellation requested' WHERE id=?
            """, (now, run_id))
            self._append_event("workflow.cancel_requested", "run", run_id, {
                "active_child_id": row["active_child_id"],
            }, now)
        return self.get_run(run_id)

    def cancel_queued_workflow(self, run_id: str,
                               detail: str = "cancelled by controller") -> dict[str, Any]:
        now = time.time()
        with self._lock, self._conn:
            parent = self._conn.execute("SELECT * FROM task_runs WHERE id=?", (run_id,)).fetchone()
            if parent is None:
                raise KeyError("task run not found")
            if parent["run_kind"] != "workflow" or parent["status"] != "queued":
                raise ValueError("workflow is not queued")
            child = self._conn.execute(
                "SELECT * FROM task_runs WHERE id=?", (parent["active_child_id"],)
            ).fetchone()
            if child is None or child["status"] != "queued":
                raise ValueError("workflow active step is not queued")
            self._conn.execute("""
                UPDATE task_runs SET cancel_requested_at=?, detail='cancellation requested' WHERE id=?
            """, (now, run_id))
            self._append_event("workflow.cancel_requested", "run", run_id, {
                "active_child_id": child["id"],
            }, now)
            self._finish_run_locked(child, "cancelled", detail, "", now)
        return self.get_run(run_id)

    def get_run(self, run_id: str) -> dict[str, Any]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM task_runs WHERE id=?", (run_id,)).fetchone()
            children = list(self._conn.execute(
                "SELECT * FROM task_runs WHERE parent_run_id=? ORDER BY step_index", (run_id,)
            )) if row is not None and row["run_kind"] == "workflow" else []
        if row is None:
            raise KeyError("task run not found")
        result = self._run_dict(row)
        if result["run_kind"] == "workflow":
            persisted = {int(child["step_index"]): self._run_dict(child) for child in children}
            result["steps"] = []
            for index, step in enumerate(result.get("workflow_spec", {}).get("steps", [])):
                child = persisted.get(index)
                result["steps"].append({
                    **step,
                    "index": index,
                    "run_id": child["id"] if child else None,
                    "status": child["status"] if child else "pending",
                    "progress": child["progress"] if child else 0.0,
                    "detail": child["detail"] if child else "",
                    "error": child["error"] if child else "",
                    "created_at": child["created_at"] if child else None,
                    "started_at": child["started_at"] if child else None,
                    "finished_at": child["finished_at"] if child else None,
                })
        return result

    def list_runs(self, limit: int = 50, status: str | None = None,
                  root_only: bool = False) -> list[dict[str, Any]]:
        query = "SELECT * FROM task_runs"
        params: list[Any] = []
        conditions = []
        if status:
            conditions.append("status=?")
            params.append(status)
        if root_only:
            conditions.append("parent_run_id IS NULL")
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY created_at ASC" if status == "queued" else " ORDER BY created_at DESC"
        query += " LIMIT ?"
        params.append(max(1, min(200, limit)))
        with self._lock:
            return [self._run_dict(row) for row in self._conn.execute(query, params)]

    def has_active_runs(self) -> bool:
        with self._lock:
            row = self._conn.execute("""
                SELECT 1 FROM task_runs
                WHERE run_kind='skill' AND status IN ('dispatched', 'running') LIMIT 1
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
        spec = result.pop("workflow_spec_json", None)
        result["workflow_spec"] = json.loads(spec) if spec else None
        task_state = result.pop("task_state_json", None)
        result["task_state"] = json.loads(task_state) if task_state else None
        stored_result = result.pop("result_json", None)
        result["result"] = json.loads(stored_result) if stored_result else None
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
