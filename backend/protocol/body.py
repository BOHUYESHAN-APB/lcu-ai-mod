"""Shared contract for real-client and future fake-player bodies."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable


@dataclass(slots=True)
class BodyEvent:
    """Transport-neutral event emitted by a companion body."""

    type: str
    data: dict[str, Any]


@runtime_checkable
class BodyAdapter(Protocol):
    @property
    def is_connected(self) -> bool: ...

    def connect(self) -> bool: ...

    def disconnect(self) -> None: ...

    def send_command(self, command: str, args: dict[str, Any] | None = None) -> str: ...

    def drain(self) -> list[BodyEvent]: ...
