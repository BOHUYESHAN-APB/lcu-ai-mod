"""
Resource gathering — honey, fishing, clearing grass/snow.
"""

from protocol import WireClient
from . import Task


class HoneyTask(Task):
    @property
    def name(self) -> str:
        return "采蜂蜜"

    def can_start(self) -> bool:
        return True

    def tick(self) -> bool:
        # Find beehive with honey_level >= 5
        # Walk to it, use glass bottle
        return False


class FishTask(Task):
    """Cast fishing rod and wait for a bite."""

    def __init__(self, wire: WireClient):
        super().__init__(wire)
        self._wait_ticks = 0
        self._max_wait = 300

    @property
    def name(self) -> str:
        return "钓鱼"

    def can_start(self) -> bool:
        return True

    def tick(self) -> bool:
        self._wait_ticks += 1
        if self._wait_ticks >= self._max_wait:
            self._wait_ticks = 0
        return False


class GrassSnowTask(Task):
    @property
    def name(self) -> str:
        return "清理草/雪"

    def can_start(self) -> bool:
        return True

    def tick(self) -> bool:
        # Find tall grass, fern, snow layers nearby
        # Walk to and break them
        return False
