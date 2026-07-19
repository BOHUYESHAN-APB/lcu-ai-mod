"""Durable Skill runs and real/game clock scheduling."""

from __future__ import annotations

import math
import threading
import time
import uuid
from contextlib import contextmanager
from typing import Any, Callable

from protocol import BodyAdapter, BodyEvent

from .agent_state import AgentStateDB, LeaseConflictError
from .skill_registry import SkillRegistry


FENCING_FIELD = "__lcu_fencing_token"
TERMINAL_STATUSES = {"succeeded", "failed", "cancelled", "unknown"}
COMMAND_TIMEOUT_SECONDS = 30 * 60


class TaskCoordinator:
    def __init__(self, state: AgentStateDB, registry: SkillRegistry, body: BodyAdapter,
                 initial_scope: str | None = None):
        self.state = state
        self.registry = registry
        self.body = body
        self._dispatch_lock = threading.RLock()
        self._last_scheduler_tick = 0.0
        self._session_busy = False
        self._body_armed = False
        self._raw_requests: dict[str, tuple[str, float]] = {}
        self._cancel_requests: dict[str, str] = {}
        self._stop_request_id: str | None = None
        clock = state.get_scheduler_clock()
        scope_changed = initial_scope is not None and clock["scope_id"] != initial_scope
        self._last_game_time = None if scope_changed else clock["game_time"]
        self._last_day_time = None if scope_changed else clock["day_time"]
        self._clock_scope = initial_scope or clock["scope_id"]
        if scope_changed:
            self.state.set_scheduler_clock(None, None, initial_scope)
        self.state.mark_inflight_unknown("backend restarted before terminal event")

    def create_run(self, skill_id: str, input_data: dict[str, Any], *,
                   lease_id: str | None = None, fencing_token: int | None = None) -> dict[str, Any]:
        with self._dispatch_lock:
            manifest = self.registry.validate_input(skill_id, input_data)
            if not manifest.durable:
                raise ValueError("skill does not provide a durable terminal event")
            with self.state.control_guard(lease_id, fencing_token):
                if lease_id and self.is_busy():
                    raise ValueError("another task run is active")
                run = self.state.create_run(
                    manifest.public_dict(), input_data, scope_id=self._clock_scope,
                )
                if self.state.has_active_runs() or self._session_busy or self._raw_requests or not self._body_armed:
                    return run
                return self.dispatch(run["id"], lease_id=lease_id, fencing_token=fencing_token)

    def create_workflow(self, workflow: dict[str, Any], *, lease_id: str | None = None,
                        fencing_token: int | None = None) -> dict[str, Any]:
        with self._dispatch_lock:
            if workflow.get("kind") != "workflow":
                raise ValueError("task preset is not a workflow")
            for step in workflow.get("steps", []):
                manifest = self.registry.validate_input(step["skill_id"], step["input"])
                if not manifest.durable or manifest.version != step["skill_version"]:
                    raise ValueError(f"workflow step contract changed: {step['key']}")
            with self.state.control_guard(lease_id, fencing_token):
                if lease_id and self.is_busy():
                    raise ValueError("another task run is active")
                run = self.state.create_workflow_run(
                    workflow, scope_id=self._clock_scope, lease_id=lease_id, fencing_token=fencing_token,
                )
                if self.state.has_active_runs() or self._session_busy or self._raw_requests or not self._body_armed:
                    return run
                self.dispatch(run["active_child_id"], lease_id=lease_id, fencing_token=fencing_token)
                return self.state.get_run(run["id"])

    def admit_automatic_run(self, skill_id: str, input_data: dict[str, Any]) -> dict[str, Any]:
        """Atomically admit an autonomous proposal without leaving a resumable queued run."""
        with self._dispatch_lock:
            manifest = self.registry.validate_input(skill_id, input_data)
            if not manifest.durable:
                raise ValueError("automatic decision requires a durable skill")
            if not self.body.is_connected:
                raise ConnectionError("companion body is not connected")
            if not self._body_armed:
                raise ValueError("companion body is not armed")
            if self.state.has_active_runs() or self._raw_requests or self._session_busy:
                raise ValueError("the companion is already executing a command or task run")
            with self.state.control_guard(None, None):
                run = self.state.create_run(
                    manifest.public_dict(), input_data, scope_id=self._clock_scope,
                )
                dispatched = self.dispatch(run["id"])
            if dispatched["status"] == "queued":
                self.state.fail_run(run["id"], "automatic admission changed before dispatch")
                raise ConnectionError("automatic run could not be dispatched")
            return dispatched

    def admit_planner_run(self, skill_id: str, input_data: dict[str, Any]) -> dict[str, Any]:
        """Admit one typed chat-Planner action without creating a deferred queue."""
        if skill_id == "core.stop":
            return self.preempt_all("planner stop intent")
        with self._dispatch_lock:
            manifest = self.registry.validate_input(skill_id, input_data)
            if not manifest.durable:
                raise ValueError("planner action requires a durable skill")
            if manifest.safety_class == "combat":
                raise ValueError("combat skills are not admitted from chat Planner")
            if not self.body.is_connected:
                raise ConnectionError("companion body is not connected")
            if not self._body_armed:
                raise ValueError("companion body is not armed")
            if self.state.has_active_runs() or self._raw_requests or self._session_busy:
                raise ValueError("the companion is already executing a command or task run")
            with self.state.control_guard(None, None):
                run = self.state.create_run(
                    manifest.public_dict(), input_data, scope_id=self._clock_scope,
                )
                dispatched = self.dispatch(run["id"])
            if dispatched["status"] == "queued":
                self.state.fail_run(run["id"], "planner admission changed before dispatch")
                raise ConnectionError("planner run could not be dispatched")
            if dispatched["status"] not in {"dispatched", "running", "succeeded"}:
                raise ConnectionError(
                    f"planner dispatch is uncertain for run {run['id']}: {dispatched['status']}"
                )
            return dispatched

    def preempt_all(self, reason: str = "stop requested") -> dict[str, Any]:
        """Send deterministic stop through the coordinator even while an operation is active."""
        with self._dispatch_lock:
            if not self.body.is_connected:
                raise ConnectionError("companion body is not connected")
            if self._stop_request_id and self._stop_request_id in self._raw_requests:
                return {
                    "id": self._stop_request_id, "request_id": self._stop_request_id,
                    "skill_id": "core.stop", "status": "dispatched",
                }
            with self.state.control_guard(None, None):
                request_id = str(uuid.uuid4())
                self._raw_requests[request_id] = ("response", time.monotonic())
                self._stop_request_id = request_id
                try:
                    returned_id = self.body.send_command("stop_all", {}, request_id=request_id)
                except Exception:
                    self._raw_requests.pop(request_id, None)
                    self._stop_request_id = None
                    raise
                if returned_id != request_id:
                    self._raw_requests.pop(request_id, None)
                    self._stop_request_id = None
                    raise ConnectionError("body did not preserve stop request id")
                self.state.append_event("control.stop_requested", "control", request_id, {
                    "reason": str(reason)[:500],
                })
                return {"id": request_id, "request_id": request_id, "skill_id": "core.stop", "status": "dispatched"}

    def dispatch(self, run_id: str, *, lease_id: str | None = None,
                 fencing_token: int | None = None) -> dict[str, Any]:
        with self._dispatch_lock:
            run = self.state.get_run(run_id)
            if run["run_kind"] == "workflow":
                if not run["active_child_id"]:
                    return run
                self.dispatch(
                    run["active_child_id"],
                    lease_id=run.get("lease_id") if lease_id is None else lease_id,
                    fencing_token=run.get("fencing_token") if fencing_token is None else fencing_token,
                )
                return self.state.get_run(run_id)
            if run["status"] != "queued" or not self.body.is_connected:
                return run
            if self.state.has_active_runs():
                return run
            if self._raw_requests or self._session_busy or not self._body_armed:
                return run
            try:
                manifest = self.registry.validate_input(run["skill_id"], run["input"])
                if manifest.version != run["skill_version"]:
                    return self.state.fail_run(run_id, "installed skill version changed")
                with self.state.control_guard(lease_id, fencing_token) as lease:
                    args = dict(run["input"])
                    if lease:
                        args[FENCING_FIELD] = lease["fencing_token"]
                    self.state.mark_run_dispatched(run_id, run_id)
                    request_id = self.body.send_command(manifest.command, args, request_id=run_id)
                if request_id != run_id:
                    return self.state.mark_run_unknown(run_id, "body did not preserve durable request id")
                return self.state.get_run(run_id)
            except LeaseConflictError:
                return self.state.fail_run(run_id, "control lease changed before dispatch") if lease_id else run
            except ConnectionError:
                return self.state.mark_run_unknown(run_id, "body connection failed during dispatch")
            except (KeyError, ValueError) as exc:
                current = self.state.get_run(run_id)
                return current if current["status"] in TERMINAL_STATUSES else self.state.fail_run(run_id, str(exc))

    def handle_body_message(self, message: BodyEvent) -> dict[str, Any] | None:
        request_id = str(message.data.get("id", ""))
        with self._dispatch_lock:
            raw_request = self._raw_requests.get(request_id)
            completion = raw_request[0] if raw_request else None
            if completion and message.type == "response" \
                    and (not message.data.get("success", False) or completion == "response"):
                self._raw_requests.pop(request_id, None)
                if request_id == self._stop_request_id:
                    self._stop_request_id = None
            elif completion and message.type == "progress" and completion != "outcome":
                progress = float(message.data.get("progress", 0.0) or 0.0)
                if progress <= 0.0 or progress >= 1.0:
                    self._raw_requests.pop(request_id, None)
            elif raw_request and message.type == "outcome":
                self._raw_requests.pop(request_id, None)
            updated = None
            if message.type == "response":
                data = message.data
                cancel_run_id = self._cancel_requests.pop(str(data.get("id", "")), None)
                if cancel_run_id is not None:
                    if not data.get("success", False):
                        updated = self.state.mark_run_unknown(cancel_run_id, "body rejected cancellation request")
                    else:
                        updated = self.state.get_run(cancel_run_id)
                else:
                    detail_data = data.get("data", {})
                    detail = detail_data.get("message", "") if isinstance(detail_data, dict) else str(detail_data or "")
                    updated = self.state.update_run_response(
                        str(data.get("id", "")), bool(data.get("success", False)), detail,
                        str(data.get("error", "")),
                    )
            elif message.type == "progress":
                data = message.data
                updated = self.state.update_run_progress(
                    str(data.get("id", "")), float(data.get("progress", 0.0) or 0.0),
                    str(data.get("message", "")),
                )
            elif message.type == "outcome":
                data = message.data
                for cancel_id, run_id in list(self._cancel_requests.items()):
                    if run_id == str(data.get("id", "")):
                        self._cancel_requests.pop(cancel_id, None)
                updated = self.state.update_run_outcome(
                    str(data.get("id", "")), str(data.get("status", "")),
                    str(data.get("message", "")), str(data.get("code", "")),
                )
            if updated is not None:
                self._dispatch_workflow_successor(updated)
            return updated

    def _dispatch_workflow_successor(self, run: dict[str, Any]) -> None:
        parent_id = run.get("parent_run_id")
        if not parent_id:
            return
        parent = self.state.get_run(parent_id)
        child_id = parent.get("active_child_id")
        if parent["status"] != "queued" or not child_id or child_id == run["id"]:
            return
        self.dispatch(
            child_id, lease_id=parent.get("lease_id"), fencing_token=parent.get("fencing_token"),
        )

    def on_disconnect(self) -> int:
        with self._dispatch_lock:
            self._raw_requests.clear()
            self._cancel_requests.clear()
            self._body_armed = False
        return self.state.mark_inflight_unknown("companion body disconnected before terminal event")

    def on_control_transition(self) -> int:
        with self._dispatch_lock:
            self._raw_requests.clear()
            self._cancel_requests.clear()
        detail = "control ownership changed before terminal event"
        return self.state.mark_inflight_unknown(detail) + self.state.cancel_queued_runs(detail)

    def is_busy(self) -> bool:
        with self._dispatch_lock:
            return self.state.has_active_runs() or bool(self._raw_requests)

    def set_session_busy(self, busy: bool) -> None:
        with self._dispatch_lock:
            self._session_busy = busy

    def set_body_armed(self, armed: bool) -> None:
        with self._dispatch_lock:
            self._body_armed = armed

    @contextmanager
    def coordination_guard(self):
        with self._dispatch_lock:
            yield

    @contextmanager
    def raw_command_guard(self):
        with self._dispatch_lock:
            if self.state.has_active_runs() or self._raw_requests or self._session_busy:
                raise ValueError("the companion is already executing a command or task run")
            yield

    def register_raw_command(self, command: str, request_id: str) -> None:
        completion = "response"
        for manifest in self.registry.list():
            if manifest["command"] == command:
                completion = manifest["completion"]
                break
        with self._dispatch_lock:
            self._raw_requests[request_id] = (completion, time.monotonic())

    def dispatch_raw_command(self, command: str, args: dict[str, Any], *,
                             on_reserved: Callable[[str], None] | None = None,
                             on_failed: Callable[[str], None] | None = None) -> str:
        with self._dispatch_lock:
            request_id = str(uuid.uuid4())
            self.register_raw_command(command, request_id)
            if on_reserved:
                on_reserved(request_id)
            try:
                returned_id = self.body.send_command(command, args, request_id=request_id)
            except Exception:
                self._raw_requests.pop(request_id, None)
                if on_failed:
                    on_failed(request_id)
                raise
            if returned_id != request_id:
                self._raw_requests.pop(request_id, None)
                if on_failed:
                    on_failed(request_id)
                raise ConnectionError("body did not preserve raw request id")
            return request_id

    def cancel(self, run_id: str, *, lease_id: str | None = None,
               fencing_token: int | None = None) -> dict[str, Any]:
        with self._dispatch_lock:
            with self.state.control_guard(lease_id, fencing_token) as lease:
                run = self.state.get_run(run_id)
                if run.get("parent_run_id"):
                    run = self.state.get_run(run["parent_run_id"])
                if run["status"] in TERMINAL_STATUSES:
                    return run
                parent = run if run["run_kind"] == "workflow" else None
                if parent:
                    if not parent["active_child_id"]:
                        return self.state.cancel_run(parent["id"])
                    run = self.state.get_run(parent["active_child_id"])
                    if run["status"] == "queued":
                        return self.state.cancel_queued_workflow(parent["id"])
                elif run["status"] == "queued":
                    return self.state.cancel_run(run["id"])
                manifest = self.registry.get(run["skill_id"])
                if run["status"] in {"dispatched", "running"} and not manifest.cancellable:
                    raise ValueError("skill does not support cancellation after dispatch")
                if parent:
                    self.state.request_workflow_cancel(parent["id"])
                if run["status"] in {"dispatched", "running"} and self.body.is_connected:
                    args = {"operation_id": run["request_id"] or run_id}
                    if lease:
                        args[FENCING_FIELD] = lease["fencing_token"]
                    try:
                        cancel_request_id = self.body.send_command("cancel_operation", args)
                        self._cancel_requests[cancel_request_id] = run["id"]
                    except ConnectionError:
                        self.state.mark_run_unknown(run["id"], "body disconnected while cancellation was requested")
                        raise
                    return self.state.get_run(parent["id"] if parent else run["id"])
                if run["status"] in {"dispatched", "running"}:
                    self.state.mark_run_unknown(run["id"], "body disconnected before cancellation could be confirmed")
                    return self.state.get_run(parent["id"] if parent else run["id"])
                self.state.cancel_run(run["id"])
                return self.state.get_run(parent["id"] if parent else run["id"])

    def resume(self, run_id: str, *, lease_id: str | None = None,
               fencing_token: int | None = None) -> dict[str, Any]:
        with self._dispatch_lock:
            with self.state.control_guard(lease_id, fencing_token):
                run = self.state.get_run(run_id)
                if run.get("parent_run_id"):
                    run = self.state.get_run(run["parent_run_id"])
                if run["status"] != "queued":
                    raise ValueError("only a queued task run can be resumed")
                if not self._clock_scope or run.get("scope_id") != self._clock_scope:
                    raise ValueError("task run belongs to a different or unknown server/world scope")
                if run["run_kind"] == "workflow" and run.get("cancel_requested_at") is not None:
                    return self.state.cancel_queued_workflow(run["id"])
                if self.state.has_active_runs() or self._raw_requests or self._session_busy:
                    raise ValueError("the companion is already executing a command or task run")
                if not self.body.is_connected:
                    raise ConnectionError("companion body is not connected")
                if not self._body_armed:
                    raise ValueError("companion body is not armed")
                if run["run_kind"] == "workflow":
                    self.state.set_workflow_control(run["id"], lease_id, fencing_token)
                    self.dispatch(run["active_child_id"], lease_id=lease_id, fencing_token=fencing_token)
                    return self.state.get_run(run["id"])
                return self.dispatch(run["id"], lease_id=lease_id, fencing_token=fencing_token)

    def tick(self, runtime: dict[str, Any], control_mode: str, clock_scope: str = "default") -> None:
        now_mono = time.monotonic()
        if now_mono - self._last_scheduler_tick < 0.5:
            return
        self._last_scheduler_tick = now_mono
        with self._dispatch_lock:
            stale_raw = [
                request_id for request_id, (_, started_at) in self._raw_requests.items()
                if now_mono - started_at >= COMMAND_TIMEOUT_SECONDS
            ]
            for request_id in stale_raw:
                self._raw_requests.pop(request_id, None)
            self.state.expire_stale_runs(
                COMMAND_TIMEOUT_SECONDS, "terminal event timeout; body state is uncertain",
            )
        lease = self.state.get_active_lease()

        world = runtime.get("world", {}) if isinstance(runtime.get("world"), dict) else {}
        game_time = self._int_or_none(world.get("game_time"))
        day_time = self._int_or_none(world.get("day_time", world.get("time")))
        clock_available = game_time is not None or day_time is not None
        scope_reset = clock_available and self._clock_scope is not None and self._clock_scope != clock_scope
        game_reset = scope_reset or game_time is not None and self._last_game_time is not None and game_time < self._last_game_time
        day_reset = scope_reset or day_time is not None and self._last_day_time is not None and day_time < self._last_day_time
        if game_reset or day_reset:
            self.state.append_event("clock.reset", "clock", "game", {
                "previous_game_time": self._last_game_time,
                "game_time": game_time,
                "previous_day_time": self._last_day_time,
                "day_time": day_time,
            })
            for schedule in self.state.list_schedules(enabled_only=True):
                if schedule["clock"] != "game" or schedule.get("scope_id") != clock_scope:
                    continue
                if schedule["trigger_type"] == "interval" and game_reset and game_time is not None:
                    next_tick = game_time + int(schedule["game_interval_ticks"])
                elif schedule["trigger_type"] == "time_of_day" and day_reset and day_time is not None:
                    next_tick = self._next_time_of_day(day_time, int(schedule["time_of_day_tick"]))
                else:
                    continue
                try:
                    self.state.advance_schedule(
                        schedule["id"], next_wall_at=None, next_game_tick=next_tick, triggered=False,
                    )
                except KeyError:
                    continue
        self._last_game_time = game_time if game_time is not None else self._last_game_time
        self._last_day_time = day_time if day_time is not None else self._last_day_time
        if clock_available or self._clock_scope is None:
            self._clock_scope = clock_scope
        self.state.set_scheduler_clock(self._last_game_time, self._last_day_time, self._clock_scope)

        if game_reset or day_reset:
            return

        allow_dispatch = control_mode != "external" and lease is None \
            and self._body_armed and not self._session_busy and not self._raw_requests
        for schedule in reversed(self.state.list_schedules(enabled_only=True)):
            if schedule.get("scope_id") != clock_scope:
                continue
            try:
                if self._evaluate_schedule(schedule, time.time(), game_time, day_time, allow_dispatch):
                    break
            except (KeyError, ValueError):
                continue

    def _evaluate_schedule(self, schedule: dict[str, Any], wall_now: float,
                           game_time: int | None, day_time: int | None, allow_dispatch: bool = True) -> bool:
        due = False
        next_wall = schedule.get("next_wall_at")
        next_game = schedule.get("next_game_tick")

        if schedule["clock"] == "wall":
            due = next_wall is not None and wall_now >= float(next_wall)
        elif schedule["trigger_type"] == "interval" and game_time is not None:
            if next_game is None:
                next_game = game_time + int(schedule["game_interval_ticks"])
                self.state.advance_schedule(schedule["id"], next_wall_at=None, next_game_tick=next_game, triggered=False)
                return False
            due = game_time >= int(next_game)
        elif schedule["trigger_type"] == "time_of_day" and day_time is not None:
            if next_game is None:
                next_game = self._next_time_of_day(day_time, int(schedule["time_of_day_tick"]))
                self.state.advance_schedule(schedule["id"], next_wall_at=None, next_game_tick=next_game, triggered=False)
                return False
            due = day_time >= int(next_game)

        if not due:
            return False

        try:
            manifest = self.registry.get(schedule["skill_id"])
        except KeyError:
            self.state.set_schedule_enabled(schedule["id"], False)
            return True
        if manifest.version != schedule["skill_version"]:
            self.state.append_event("schedule.version_mismatch", "schedule", schedule["id"], {
                "expected": schedule["skill_version"], "installed": manifest.version,
            })
            self.state.set_schedule_enabled(schedule["id"], False)
            return True

        next_wall, next_game = self._next_occurrence(schedule, wall_now, game_time, day_time)
        can_dispatch = allow_dispatch and self.body.is_connected and self.state.get_active_lease() is None
        if not can_dispatch and schedule["misfire_policy"] == "skip":
            self.state.advance_schedule(
                schedule["id"], next_wall_at=next_wall, next_game_tick=next_game,
                triggered=False, skipped=True, disable=schedule["trigger_type"] == "once",
            )
            return True
        if not can_dispatch:
            return False

        run = self.state.trigger_schedule(
            schedule["id"], manifest.public_dict(), next_wall_at=next_wall,
            next_game_tick=next_game, disable=schedule["trigger_type"] == "once",
        )
        self.dispatch(run["id"])
        return True

    @staticmethod
    def _next_occurrence(schedule: dict[str, Any], wall_now: float,
                         game_time: int | None, day_time: int | None) -> tuple[float | None, int | None]:
        if schedule["clock"] == "wall":
            if schedule["trigger_type"] == "once":
                return None, None
            interval = float(schedule["wall_interval_seconds"])
            current = float(schedule["next_wall_at"])
            steps = max(1, math.floor((wall_now - current) / interval) + 1)
            return current + steps * interval, None
        if schedule["trigger_type"] == "interval" and game_time is not None:
            interval = int(schedule["game_interval_ticks"])
            current = int(schedule["next_game_tick"])
            steps = max(1, (game_time - current) // interval + 1)
            return None, current + steps * interval
        if day_time is not None:
            return None, TaskCoordinator._next_time_of_day(day_time, int(schedule["time_of_day_tick"]))
        return None, schedule.get("next_game_tick")

    @staticmethod
    def _next_time_of_day(day_time: int, target: int) -> int:
        base = day_time - day_time % 24000
        candidate = base + target
        return candidate if candidate > day_time else candidate + 24000

    @staticmethod
    def _int_or_none(value: Any) -> int | None:
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None
