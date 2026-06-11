"""
Self-Prompter — Continuous autonomous prompting when idle.
Replicates mindcraft's self_prompter.js architecture.

When the agent is idle (no pending tasks, no active action, no mode running)
for a configurable cooldown period, the self-prompter triggers a self-prompt
cycle: it calls the LLM with the current goal context to generate new actions.

This enables continuous autonomous behavior without requiring user chat input.
"""

import logging
import time
from typing import Optional

logger = logging.getLogger("self_prompter")


class SelfPrompter:
    """
    Self-prompting loop for autonomous behavior.

    States:
        idle: Waiting for cooldown
        prompting: Generating self-prompt
        waiting: Waiting for action completion
        stopped: Disabled

    Flow:
        idle → (cooldown expires) → prompting → (send prompt) → waiting
        → (action completes) → idle
        → (no action generated for 3 cycles) → stopped
    """

    def __init__(self, cooldown: float = 15.0, max_idle_cycles: int = 3):
        self.cooldown = cooldown
        self.max_idle_cycles = max_idle_cycles
        self._last_action_time: float = 0.0
        self._idle_cycles = 0
        self._enabled = True
        self._goal = ""

    def set_goal(self, goal: str):
        """Set the autonomous goal."""
        self._goal = goal
        self._enabled = True
        self._idle_cycles = 0
        logger.info("[SelfPrompter] Goal set: %s", goal)

    def disable(self):
        """Disable self-prompting."""
        self._enabled = False
        logger.info("[SelfPrompter] Disabled")

    def enable(self):
        """Enable self-prompting."""
        self._enabled = True
        self._idle_cycles = 0
        logger.info("[SelfPrompter] Enabled")

    def mark_action(self):
        """Called when any action is taken (resets idle counter)."""
        self._last_action_time = time.time()
        self._idle_cycles = 0

    def should_prompt(self, is_idle: bool, action_busy: bool) -> bool:
        """
        Check if a self-prompt should be triggered.

        Args:
            is_idle: Whether the agent is in idle state
            action_busy: Whether an action is currently executing

        Returns:
            True if a self-prompt should be generated
        """
        if not self._enabled or not self._goal:
            return False
        if action_busy:
            return False  # Don't interrupt running actions
        if not is_idle:
            self._idle_cycles = 0
            return False  # Agent is busy with non-idle work

        # Check cooldown
        elapsed = time.time() - self._last_action_time
        if elapsed < self.cooldown:
            return False

        # Check max idle cycles
        if self._idle_cycles >= self.max_idle_cycles:
            logger.info("[SelfPrompter] Max idle cycles reached, stopping")
            self._enabled = False
            return False

        return True

    def build_prompt(self) -> str:
        """
        Build the self-prompt message for the LLM.

        Returns:
            Prompt string that mimics: "You are self-prompting with the goal: '{goal}'"
        """
        self._idle_cycles += 1
        cycle_info = f" (self-prompt cycle {self._idle_cycles}/{self.max_idle_cycles})"
        return f"You are self-prompting with the goal: '{self._goal}'. What should you do next?{cycle_info}"

    def on_prompt_sent(self):
        """Called after a self-prompt is sent to the LLM."""
        self._last_action_time = time.time()
        logger.debug("[SelfPrompter] Prompt sent (cycle %d/%d)",
                     self._idle_cycles, self.max_idle_cycles)

    def get_status(self) -> dict:
        return {
            "enabled": self._enabled,
            "goal": self._goal,
            "idle_cycles": self._idle_cycles,
            "last_action_seconds_ago": round(time.time() - self._last_action_time, 1) if self._last_action_time else None,
        }
