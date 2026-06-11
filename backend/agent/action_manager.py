"""
Action Manager — execution engine with timeout, resume, and loop detection.

Replicates mindcraft's action_manager.js pattern.
Each action runs with timeout, resume support, and loop detection.
"""

import time
import logging
from dataclasses import dataclass, field
from typing import Callable, Any, Optional

logger = logging.getLogger("action_manager")


@dataclass
class Action:
    label: str
    fn: Callable[[], Any]
    timeout: float = 600.0
    resume: bool = False
    interruptible: bool = True
    start_time: float = 0.0
    completed: bool = False
    interrupted: bool = False
    timed_out: bool = False
    result: Any = None
    error: Optional[str] = None

    @property
    def elapsed(self) -> float:
        return time.time() - self.start_time if self.start_time else 0.0


class ActionManager:
    """
    Manages action execution lifecycle.

    Features:
    - run_action: start a new action (stops current if interruptible)
    - stop: cancel current action immediately
    - cancel_resume: prevent resume-able action from re-queuing
    - tick: check timeouts and completion each cycle
    - loop detection: warns after 3 fast repeats, kills after 5
    """

    def __init__(self):
        self.current: Optional[Action] = None
        self._resume_queue: list[Action] = []
        self._history: list[tuple[str, float]] = []
        self._warnings = 0
        self._max_warnings = 3
        self._max_kills = 5
        self._loop_window = 5.0

    # ── Public API ──────────────────────────────────────────────

    def run_action(self, label: str, fn: Callable, **opts) -> bool:
        """Start executing an action. Returns True if started."""
        if self.current and not self.current.interruptible:
            logger.warning("[ActionMgr] Cannot interrupt non-interruptible: %s", self.current.label)
            return False

        # Save current as resume if applicable
        if self.current and self.current.resume and not self.current.completed:
            self._resume_queue.append(self.current)

        self.stop()

        timeout = opts.get("timeout", 600.0)
        resume = opts.get("resume", False)
        interruptible = opts.get("interruptible", True)

        self.current = Action(
            label=label, fn=fn, timeout=timeout,
            resume=resume, interruptible=interruptible,
            start_time=time.time(),
        )
        self._track_history(label)
        logger.info("[ActionMgr] Started: %s (timeout=%.1fs, resume=%s)", label, timeout, resume)
        return True

    def stop(self):
        """Stop current action immediately."""
        cur = self.current
        if cur and not cur.completed:
            cur.interrupted = True
            logger.info("[ActionMgr] Stopped: %s (elapsed=%.1fs)", cur.label, cur.elapsed)
        self.current = None

    def cancel_resume(self):
        """Prevent all resume-able actions from firing."""
        self._resume_queue.clear()
        logger.debug("[ActionMgr] Resume queue cleared")

    def tick(self):
        """
        Called every event loop cycle.
        Checks timeout and auto-completion.
        """
        cur = self.current
        if not cur or cur.completed:
            return

        # Check timeout
        if cur.elapsed > cur.timeout:
            logger.warning("[ActionMgr] Timeout: %s (%.1fs > %.1fs)", cur.label, cur.elapsed, cur.timeout)
            cur.timed_out = True
            self.current = None
            return

        # Execute the action function
        try:
            result = cur.fn()
            if result is not None:
                cur.result = result
                cur.completed = True
                logger.info("[ActionMgr] Completed: %s", cur.label)
                self.current = None
        except Exception as e:
            cur.error = str(e)
            cur.interrupted = True
            logger.error("[ActionMgr] Error in %s: %s", cur.label, e)
            self.current = None

    def handle_response(self, req_id: str, success: bool):
        """Handle a response from the Java mod for a pending action."""
        cur = self.current
        if cur:
            logger.debug("[ActionMgr] Response for %s: req=%s success=%s", cur.label, req_id, success)

    def pop_resume(self) -> Optional[Action]:
        """Return next resume-able action if idle, or None."""
        if self.is_busy:
            return None
        return self._resume_queue.pop(0) if self._resume_queue else None

    # ── Status ──────────────────────────────────────────────────

    @property
    def is_busy(self) -> bool:
        cur = self.current
        return cur is not None and not cur.completed

    @property
    def current_label(self) -> Optional[str]:
        cur = self.current
        return cur.label if cur else None

    def get_status(self) -> dict:
        cur = self.current
        if cur:
            return {
                "label": cur.label,
                "elapsed": round(cur.elapsed, 1),
                "timeout": cur.timeout,
                "completed": cur.completed,
                "interrupted": cur.interrupted,
                "timed_out": cur.timed_out,
            }
        return {"label": None, "resume_queue": len(self._resume_queue)}

    # ── Loop Detection ──────────────────────────────────────────

    def _track_history(self, label: str):
        now = time.time()
        self._history.append((label, now))
        self._history = [(l, t) for l, t in self._history if now - t < self._loop_window]

        same = [l for l, t in self._history if l == label]
        count = len(same)
        if count >= self._max_kills:
            logger.error("[ActionMgr] LOOP DETECTED: %s ran %d times in %.1fs — KILLING",
                         label, count, self._loop_window)
            raise RuntimeError(f"Loop detected: {label} repeated {count} times")
        elif count >= self._max_warnings:
            self._warnings += 1
            logger.warning("[ActionMgr] Loop warning #%d: %s ran %d times", self._warnings, label, count)
