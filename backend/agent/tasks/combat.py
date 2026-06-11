"""
Ranged combat — use bow/crossbow to attack hostiles from distance.
"""

from protocol import WireClient
from . import Task


class RangedCombatTask(Task):
    """Engage hostiles with bow or crossbow at range."""

    def __init__(self, wire: WireClient):
        super().__init__(wire)
        self._engaged = False

    @property
    def name(self) -> str:
        return "远程战斗"

    def can_start(self) -> bool:
        # Check if we have a ranged weapon and hostiles are nearby
        # This is checked against the current state from EntityTracker
        return True  # Called when the orchestrator decides

    def tick(self) -> bool:
        # Look at target, charge bow, release
        # This is a placeholder — the actual combat logic
        # will be implemented when we integrate with the state system
        if not self._engaged:
            print("[Ranged] 开始远程战斗")
            self._engaged = True
        return False  # Keep running until out of hostiles
