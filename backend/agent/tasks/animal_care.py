"""
Animal care — shearing sheep, milking cows, feeding animals.
"""

from protocol import WireClient
from . import Task


class AnimalCareTask(Task):
    """Care for nearby animals."""

    @property
    def name(self) -> str:
        return "动物护理"

    def can_start(self) -> bool:
        return True

    def tick(self) -> bool:
        # Find unshorn sheep → equip shears → walk to → shear
        # Find cow → equip bucket → milk
        # Find breedable animals → feed
        return False


class ExtinguishTask(Task):
    """Put out nearby fires."""

    @property
    def name(self) -> str:
        return "灭火"

    def can_start(self) -> bool:
        return True

    def tick(self) -> bool:
        # Find fire blocks nearby
        # Walk to and punch them
        return False
