"""Append-only local audit log for administrative configuration changes."""

from __future__ import annotations

import copy
import json
import threading
import time
import uuid
from pathlib import Path
from typing import Any


SENSITIVE_KEYS = {"api_key", "authorization", "token", "secret", "password"}


def redact_sensitive(value: Any) -> Any:
    redacted = copy.deepcopy(value)

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, item in list(node.items()):
                if str(key).lower() in SENSITIVE_KEYS and item:
                    node[key] = "***"
                else:
                    walk(item)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(redacted)
    return redacted


class AuditLog:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def append(self, category: str, action: str, *, target: str = "", actor: str = "operator",
               outcome: str = "succeeded", details: dict[str, Any] | None = None) -> dict[str, Any]:
        event = {
            "id": str(uuid.uuid4()),
            "occurred_at": time.time(),
            "category": str(category),
            "action": str(action),
            "target": str(target),
            "actor": str(actor),
            "outcome": str(outcome),
            "details": redact_sensitive(details or {}),
        }
        encoded = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
        with self._lock:
            with self.path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(encoded + "\n")
        return copy.deepcopy(event)

    def list(self, *, category: str = "", limit: int = 100) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        if not self.path.exists():
            return []
        records: list[dict[str, Any]] = []
        with self._lock:
            try:
                lines = self.path.read_text(encoding="utf-8").splitlines()
            except OSError:
                return []
        for line in reversed(lines):
            try:
                event = json.loads(line)
            except (TypeError, ValueError):
                continue
            if not isinstance(event, dict) or category and event.get("category") != category:
                continue
            records.append(event)
            if len(records) >= limit:
                break
        return records
