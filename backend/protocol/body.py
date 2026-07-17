"""Shared contract for real-client and future fake-player bodies."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class BodyAdapter(Protocol):
    @property
    def is_connected(self) -> bool: ...

    def connect(self) -> bool: ...

    def disconnect(self) -> None: ...

    def send_command(self, command: str, args: dict[str, Any] | None = None) -> str: ...

    def drain(self) -> list[Any]: ...
