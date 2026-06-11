"""
Special crop farming — sugar cane, melon, pumpkin, cocoa.
"""

from protocol import WireClient
from . import Task


class SpecialFarmTask(Task):
    """Harvest and replant special crops."""

    @property
    def name(self) -> str:
        return "特殊种植"

    def can_start(self) -> bool:
        return True

    def tick(self) -> bool:
        # Scan nearby blocks for harvestable crops
        # Send mine_block on mature crops
        # This is driven by state updates from the mod
        return False
