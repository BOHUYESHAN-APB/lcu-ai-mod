"""
Orchestrator — Session-driven event loop.
Routes mod events to Session, drives tick cycle, manages lifecycle.
"""

import asyncio
from contextlib import contextmanager
import logging
import threading
import time
from typing import Optional

from protocol import BodyAdapter
from .decision_scheduler import DecisionScheduler
from .session import Session

logger = logging.getLogger("orchestrator")


class Orchestrator:
    """
    Main event loop coordinator.

    - Creates and manages a Session
    - Routes body events to the session
    - Drives the session tick cycle
    - Handles startup/shutdown

    Callbacks (set by server.py):
        on_chat: callable(sender, message, is_system) — for WebSocket broadcast
    """

    def __init__(self, body: BodyAdapter, session_id: str | None = None, **session_options):
        self.body = body
        self.session = Session(body, session_id=session_id, **session_options)
        self.running = False
        self._tick_count = 0
        self._last_stats = time.monotonic()
        self._session_lock = threading.RLock()
        self.on_chat = None  # type: ignore  # set by server.py
        self.on_event = None  # type: ignore  # set by server.py
        self.task_coordinator = None
        self.decision_scheduler = DecisionScheduler(self.session.llm)
        self._automatic_decision_contracts = {
            "general.eat": {
                "version": "2.0.0", "command": "eat", "safety_class": "standard",
                "effects": ("inventory.consume",),
            },
        }
        self._body_epoch = 0

    def start(self):
        """Start the orchestrator."""
        self.running = True
        self._body_epoch += 1
        with self._session_lock:
            self.session.set_body_connected(True)
        self._publish_event("body.connection", {"connected": True})
        logger.info("[Orch] Started (session=%s)", self.session.id)

    def stop(self):
        """Stop the orchestrator gracefully."""
        self.running = False
        logger.info("[Orch] Stopping...")

    def tick(self):
        """Called every event loop cycle."""
        if not self.running:
            return

        external_task_busy = self.task_coordinator.is_busy() if self.task_coordinator else False
        with self._session_lock:
            self.session.set_external_task_busy(external_task_busy)
            self._tick_count += 1

            # Process incoming body events
            self._drain_body()

            # Session tick (action manager, modes, tasks, auto-save)
            self.session.tick(external_task_busy=external_task_busy)
            if self.task_coordinator:
                self.task_coordinator.set_session_busy(self.session.is_busy_for_external_task())
                control_state = self.session.runtime.get("control_state", {})
                self.task_coordinator.set_body_armed(
                    isinstance(control_state, dict) and control_state.get("ai_controlled") is True,
                )
            runtime = dict(self.session.runtime)
            control_mode = self.session.control_mode
            clock_scope = f"{self.session.identity.server_id}\0{self.session.identity.world_id}"
            control_state = self.session.runtime.get("control_state", {})
            armed = isinstance(control_state, dict) and control_state.get("ai_controlled") is True
            can_request_decision = self.task_coordinator is not None and control_mode == "builtin" \
                and armed and self.session.llm.is_configured("decision_scheduler") \
                and not external_task_busy and not self.session.is_busy_for_external_task()
            if can_request_decision:
                triggers = self.session.pending_decision_triggers()
                if triggers and self.decision_scheduler.submit(
                    triggers, self.session.build_planner_context(), scope_id=clock_scope,
                    body_epoch=self._body_epoch,
                    observation_revision=self.session.world_model.revision,
                ):
                    request = self.decision_scheduler.get_status()
                    event = {
                        "request_id": request["request_id"],
                        "through_sequence": request["through_sequence"],
                        "scope_id": clock_scope,
                        "body_epoch": self._body_epoch,
                    }
                    self.task_coordinator.state.append_event(
                        "decision.requested", "decision", request["request_id"], event,
                    )
                    self._publish_event("decision.requested", event)

        if self.task_coordinator:
            self.task_coordinator.tick(runtime, control_mode, clock_scope)
        self._apply_decision_result()

        # Periodic stats
        now = time.monotonic()
        if now - self._last_stats > 60:
            self._last_stats = now
            self._log_stats()

    def _drain_body(self):
        """Process all pending body events."""
        for msg in self.body.drain():
            if msg.type == "event":
                event_type = msg.data.get("event", "unknown")
                event_data = msg.data.get("data", {})
                self.session.handle_event(event_type, event_data)
                if event_type != "player_chat":
                    self._publish_event(event_type, event_data, msg.data.get("ts"))
                # Broadcast chat events to WebSocket clients
                if event_type == "player_chat" and self.on_chat:
                    self.on_chat(
                        sender=event_data.get("sender", "?"),
                        message=event_data.get("message", ""),
                        is_system=event_data.get("is_system", False),
                    )
            elif msg.type == "response":
                self.session.handle_event("command_response", msg.data)
            elif msg.type == "progress":
                self._handle_progress(msg.data)
            elif msg.type == "outcome":
                self.session.handle_event("command_outcome", msg.data)
            if self.task_coordinator:
                self.task_coordinator.handle_body_message(msg)

    def set_task_coordinator(self, coordinator) -> None:
        self.task_coordinator = coordinator

    def _apply_decision_result(self) -> None:
        result = self.decision_scheduler.poll()
        if result is None:
            return
        if result.error:
            record = self.decision_scheduler.resolve("failed", detail=result.error)
            if not record.get("retryable"):
                self._acknowledge_decision(result.through_sequence)
            self._persist_decision_event("decision.failed", result.request_id, record)
            return
        with self._session_lock:
            current_scope = f"{self.session.identity.server_id}\0{self.session.identity.world_id}"
            current_control = self.session.control_mode
        if result.scope_id != current_scope or result.body_epoch != self._body_epoch or current_control != "builtin":
            self._resolve_decision("stale", result, detail="decision context is no longer current")
            return
        proposal = result.proposal
        if proposal is None or proposal.decision == "none":
            self._resolve_decision("no_action", result, detail=proposal.reason if proposal else "")
            return
        if self.decision_scheduler.is_expired(result):
            self._resolve_decision("expired", result, detail="proposal exceeded decision TTL")
            return
        contract = self._automatic_decision_contracts.get(proposal.skill_id)
        if contract is None:
            self._resolve_decision("rejected", result, detail="skill is not in automatic decision allowlist")
            return

        with self._session_lock:
            if self.task_coordinator is None:
                return
            scope_id = f"{self.session.identity.server_id}\0{self.session.identity.world_id}"
            control = self.session.runtime.get("control_state", {})
            armed = isinstance(control, dict) and control.get("ai_controlled") is True
            if result.scope_id != scope_id or result.body_epoch != self._body_epoch:
                self._resolve_decision("stale", result, detail="body epoch or world scope changed")
                return
            if self.session.control_mode != "builtin" or not armed or not self.body.is_connected:
                self._resolve_decision("rejected", result, detail="body control is no longer eligible")
                return
            if self.task_coordinator.is_busy() or self.session.is_busy_for_external_task():
                self._resolve_decision("deferred_busy", result, detail="a newer body operation owns admission")
                return
            try:
                manifest = self.task_coordinator.registry.validate_input(proposal.skill_id, proposal.input)
                actual_contract = {
                    "version": manifest.version, "command": manifest.command,
                    "safety_class": manifest.safety_class, "effects": manifest.effects,
                }
                if actual_contract != contract:
                    raise ValueError("automatic skill contract does not match pinned safety policy")
                player = self.session.runtime.get("player", {})
                if proposal.skill_id == "general.eat" and (
                    not isinstance(player, dict) or float(player.get("hunger", 20)) >= 20
                ):
                    raise ValueError("eat proposal no longer has a current hunger postcondition")
                run = self.task_coordinator.admit_automatic_run(proposal.skill_id, proposal.input)
            except (KeyError, TypeError, ConnectionError, ValueError) as exc:
                self._resolve_decision("rejected", result, detail=str(exc))
                return
        if run["status"] not in {"dispatched", "running", "succeeded"}:
            disposition = "dispatch_unknown" if run["status"] == "unknown" else "failed_terminal"
            self._resolve_decision(disposition, result, run_id=run["id"], detail=f"unexpected run status: {run['status']}")
            return
        self._resolve_decision("dispatched", result, run_id=run["id"], detail=proposal.reason)

    def _resolve_decision(self, disposition: str, result, *, run_id: str | None = None,
                          detail: str = "") -> None:
        record = self.decision_scheduler.resolve(disposition, run_id=run_id, detail=detail)
        self._acknowledge_decision(result.through_sequence)
        event_type = "decision.dispatched" if disposition == "dispatched" else "decision.resolved"
        self._persist_decision_event(event_type, result.request_id, record)

    def _acknowledge_decision(self, through_sequence: int) -> None:
        with self._session_lock:
            self.session.acknowledge_decision_triggers(through_sequence)

    def _persist_decision_event(self, event_type: str, request_id: str,
                                record: dict) -> None:
        if self.task_coordinator:
            self.task_coordinator.state.append_event(
                event_type, "decision", request_id, record,
            )
        self._publish_event(event_type, record)

    def on_body_disconnect(self) -> None:
        with self._session_lock:
            invalidated = self.decision_scheduler.invalidate("companion body disconnected")
            if invalidated:
                self._persist_decision_event(
                    "decision.invalidated", invalidated["request_id"], invalidated,
                )
            self.session.set_body_connected(False)
            self.session.handle_event("control_state", {
                "ai_controlled": False,
                "connected": False,
            })
            self.session.set_external_task_busy(False)
            if self.task_coordinator:
                self.task_coordinator.on_disconnect()
        self._publish_event("body.connection", {"connected": False})

    def _publish_event(self, event_type: str, data: dict, occurred_at: float | None = None) -> None:
        if self.on_event:
            timestamp = float(occurred_at) if occurred_at is not None else time.time()
            if timestamp > 100_000_000_000:
                timestamp /= 1000.0
            self.on_event(event_type, data, timestamp)

    def handle_chat(self, sender: str, message: str) -> Optional[str]:
        """Process a chat message through the session's LLM pipeline."""
        with self._session_lock:
            if self.task_coordinator and self.task_coordinator.is_busy():
                return None
            return self.session.handle_chat(sender, message)

    @contextmanager
    def session_context(self):
        """Serialize external reads and writes with the session tick loop."""
        with self._session_lock:
            yield self.session

    def _handle_progress(self, data: dict):
        req_id = data.get("id", "?")
        progress = data.get("progress", 0)
        message = data.get("message", "")
        self.session.handle_event("command_progress", data)
        if message:
            logger.debug("[Orch] Progress %s: %.0f%% - %s", req_id, progress * 100, message)

    def _log_stats(self):
        with self._session_lock:
            status = self.session.get_status()
        action = status.get("action", {})
        logger.info(
            "[Orch] Stats — action=%s modes=%s memory=%d tasks=%d tokens=%d",
            action.get("label"),
            status.get("modes", {}).get("active"),
            status.get("memory_size", 0),
            status.get("task_queue_len", 0),
            status.get("llm_usage", {}).get("total_tokens", 0),
        )

    def get_status(self) -> dict:
        with self._session_lock:
            return {
                "running": self.running,
                "ticks": self._tick_count,
                "session": self.session.get_status(),
                "decision_scheduler": self.decision_scheduler.get_status(),
            }

    def close(self) -> None:
        self.decision_scheduler.close()
