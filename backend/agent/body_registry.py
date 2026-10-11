"""Thread-safe registry of AI bodies attached to this backend."""

from __future__ import annotations

import copy
import threading
import time
from typing import Any


RUNTIME_ROLES = {"body_client", "server_fake_player"}


class BodyRegistry:
    def __init__(self):
        self._lock = threading.RLock()
        self._bodies: dict[str, dict[str, Any]] = {}

    def register(self, body_id: str, *, runtime_role: str, server_id: str = "default",
                 world_id: str = "default", owner_type: str = "backend",
                 owner_id: str = "local", metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        body_id = str(body_id).strip()
        if not body_id:
            raise ValueError("body_id must not be empty")
        if runtime_role not in RUNTIME_ROLES:
            raise ValueError("runtime_role must be body_client or server_fake_player")
        now = time.time()
        with self._lock:
            current = self._bodies.get(body_id, {})
            record = {
                "id": body_id,
                "runtime_role": runtime_role,
                "server_id": str(server_id),
                "world_id": str(world_id),
                "owner_type": str(owner_type),
                "owner_id": str(owner_id),
                "connected": bool(current.get("connected", False)),
                "armed": bool(current.get("armed", False)),
                "control_mode": current.get("control_mode", "builtin"),
                "state_age_seconds": current.get("state_age_seconds"),
                "stale": bool(current.get("stale", True)),
                "lease": copy.deepcopy(current.get("lease")),
                "capabilities": list(current.get("capabilities", [])),
                "peer": copy.deepcopy(current.get("peer", {})),
                "metadata": {**copy.deepcopy(current.get("metadata", {})), **copy.deepcopy(metadata or {})},
                "registered_at": current.get("registered_at", now),
                "updated_at": now,
            }
            self._bodies[body_id] = record
            return copy.deepcopy(record)

    def update(self, body_id: str, **changes: Any) -> dict[str, Any]:
        with self._lock:
            if body_id not in self._bodies:
                raise KeyError(body_id)
            record = self._bodies[body_id]
            for key in (
                "connected", "armed", "control_mode", "state_age_seconds", "stale",
                "lease", "capabilities", "peer", "server_id", "world_id", "metadata",
            ):
                if key in changes:
                    record[key] = copy.deepcopy(changes[key])
            record["updated_at"] = time.time()
            return copy.deepcopy(record)

    def get(self, body_id: str) -> dict[str, Any] | None:
        with self._lock:
            record = self._bodies.get(body_id)
            return copy.deepcopy(record) if record else None

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            return [copy.deepcopy(self._bodies[key]) for key in sorted(self._bodies)]

    def remove(self, body_id: str) -> bool:
        with self._lock:
            return self._bodies.pop(body_id, None) is not None
