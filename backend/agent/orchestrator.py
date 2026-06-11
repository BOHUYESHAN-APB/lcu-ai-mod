"""
Orchestrator — Session-driven event loop.
Routes mod events to Session, drives tick cycle, manages lifecycle.
"""

import asyncio
import logging
import time
from typing import Optional

from protocol import WireClient
from .session import Session

logger = logging.getLogger("orchestrator")


class Orchestrator:
    """
    Main event loop coordinator.

    - Creates and manages a Session
    - Routes wire events to the session
    - Drives the session tick cycle
    - Handles startup/shutdown

    Callbacks (set by server.py):
        on_chat: callable(sender, message, is_system) — for WebSocket broadcast
    """

    def __init__(self, wire: WireClient, session_id: str | None = None):
        self.wire = wire
        self.session = Session(wire, session_id=session_id)
        self.running = False
        self._tick_count = 0
        self._last_stats = time.monotonic()
        self.on_chat = None  # type: ignore  # set by server.py

    def start(self):
        """Start the orchestrator."""
        self.running = True
        logger.info("[Orch] Started (session=%s)", self.session.id)

    def stop(self):
        """Stop the orchestrator gracefully."""
        self.running = False
        logger.info("[Orch] Stopping...")

    def tick(self):
        """Called every event loop cycle."""
        if not self.running:
            return

        self._tick_count += 1

        # Process incoming wire messages
        self._drain_wire()

        # Session tick (action manager, modes, tasks, auto-save)
        self.session.tick()

        # Periodic stats
        now = time.monotonic()
        if now - self._last_stats > 60:
            self._last_stats = now
            self._log_stats()

    def _drain_wire(self):
        """Process all pending wire messages."""
        if not self.wire:
            return
        for msg in self.wire.drain():
            if msg.type == "event":
                event_type = msg.data.get("event", "unknown")
                event_data = msg.data.get("data", {})
                self.session.handle_event(event_type, event_data)
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

    def handle_chat(self, sender: str, message: str) -> Optional[str]:
        """Process a chat message through the session's LLM pipeline."""
        return self.session.handle_chat(sender, message)

    def _handle_progress(self, data: dict):
        req_id = data.get("id", "?")
        progress = data.get("progress", 0)
        message = data.get("message", "")
        self.session.handle_event("command_progress", data)
        if message:
            logger.debug("[Orch] Progress %s: %.0f%% - %s", req_id, progress * 100, message)

    def _log_stats(self):
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
        return {
            "running": self.running,
            "ticks": self._tick_count,
            "session": self.session.get_status(),
        }
