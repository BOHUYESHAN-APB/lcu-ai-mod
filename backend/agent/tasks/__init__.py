"""
Base Task class for all Python-side task orchestration.
Tasks are pure logic — they send commands to the Java mod via the wire protocol.
"""

from abc import ABC, abstractmethod
from typing import Optional
from protocol import WireClient


class Task(ABC):
    """Base class for all tasks. Runs in the Python backend."""

    def __init__(self, wire: WireClient):
        self.wire = wire
        self._running = False
        self._id: Optional[str] = None

    @property
    @abstractmethod
    def name(self) -> str: ...

    @abstractmethod
    def can_start(self) -> bool: ...

    @abstractmethod
    def tick(self) -> bool:
        """
        Called every tick (or every 0.5s). Return True when task is complete.
        Sends commands via self.wire.send_command()
        """
        ...

    def start(self, task_id: str):
        self._id = task_id
        self._running = True
        self.on_start()

    def on_start(self): ...

    def stop(self):
        self._running = False
        self.on_stop()

    def on_stop(self): ...
