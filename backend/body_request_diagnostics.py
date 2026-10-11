"""Bounded, non-durable diagnostics for raw body commands."""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Any


TERMINAL_STATUSES = {"succeeded", "failed", "cancelled", "unknown"}
SECRET_MARKERS = (
    "authorization", "cookie", "credential", "password", "passwd", "secret", "token",
    "api_key", "apikey", "access_key", "private_key",
)


def _timestamp(now: float | None = None) -> str:
    return datetime.fromtimestamp(now if now is not None else time.time(), timezone.utc).isoformat()


def _safe_value(value: Any, depth: int = 0) -> Any:
    if depth >= 5:
        return "[truncated]"
    if isinstance(value, dict):
        result = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= 50:
                result["[truncated]"] = f"{len(value) - 50} more entries"
                break
            name = str(key)[:128]
            lowered = name.lower().replace("-", "_")
            result[name] = "[redacted]" if any(marker in lowered for marker in SECRET_MARKERS) else _safe_value(item, depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        result = [_safe_value(item, depth + 1) for item in value[:50]]
        if len(value) > 50:
            result.append(f"[truncated: {len(value) - 50} more entries]")
        return result
    if isinstance(value, str):
        return value if len(value) <= 2000 else value[:2000] + "[truncated]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:2000]


class BodyRequestDiagnostics:
    def __init__(self, max_records: int = 512, timeout_seconds: float = 30 * 60):
        self.max_records = max_records
        self.timeout_seconds = timeout_seconds
        self._records: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._started: dict[str, float] = {}
        self._completion: dict[str, str] = {}
        self._lock = threading.RLock()

    def register(self, request_id: str, command: str, args: dict[str, Any], requester: str,
                 completion: str = "response") -> None:
        now = time.time()
        with self._lock:
            self._records[request_id] = {
                "request_id": request_id,
                "command": str(command)[:256],
                "args": _safe_value(args),
                "requester": str(requester)[:128],
                "status": "dispatched",
                "progress": None,
                "detail": "",
                "response": None,
                "outcome": None,
                "created_at": _timestamp(now),
                "updated_at": _timestamp(now),
                "response_at": None,
                "progress_at": None,
                "completed_at": None,
                "terminal": False,
                "durable": False,
            }
            self._started[request_id] = time.monotonic()
            self._completion[request_id] = completion
            self._records.move_to_end(request_id)
            self._trim()

    def remove(self, request_id: str) -> None:
        with self._lock:
            self._records.pop(request_id, None)
            self._started.pop(request_id, None)
            self._completion.pop(request_id, None)

    def capture(self, event: Any) -> None:
        if event.type not in {"response", "progress", "outcome"}:
            return
        request_id = str(event.data.get("id", ""))
        now = time.time()
        with self._lock:
            record = self._records.get(request_id)
            if not record:
                return
            if record["terminal"]:
                return
            data = event.data
            if event.type == "response":
                record["response"] = _safe_value(data)
                record["response_at"] = _timestamp(now)
                success = bool(data.get("success", False))
                detail_data = data.get("data", {})
                message = detail_data.get("message", "") if isinstance(detail_data, dict) else detail_data
                record["detail"] = str(data.get("error") or message or "")[:2000]
                if not success:
                    self._complete(request_id, record, "failed", now)
                elif self._completion.get(request_id) == "response":
                    self._complete(request_id, record, "succeeded", now)
                else:
                    record["status"] = "accepted"
            elif event.type == "progress":
                try:
                    progress = float(data.get("progress", 0.0) or 0.0)
                except (TypeError, ValueError):
                    progress = 0.0
                record["progress"] = max(0.0, min(1.0, progress))
                record["progress_at"] = _timestamp(now)
                record["detail"] = str(data.get("message", ""))[:2000]
                if not record["terminal"]:
                    record["status"] = "running"
                if not record["terminal"] and self._completion.get(request_id) != "outcome" \
                        and (progress <= 0.0 or progress >= 1.0):
                    self._complete(request_id, record, "failed" if progress <= 0.0 else "succeeded", now)
            else:
                record["outcome"] = _safe_value(data)
                record["detail"] = str(data.get("message") or data.get("code") or "")[:2000]
                status = str(data.get("status", "unknown"))
                self._complete(request_id, record, status if status in TERMINAL_STATUSES else "unknown", now)
            record["updated_at"] = _timestamp(now)

    def get(self, request_id: str) -> dict[str, Any] | None:
        self.expire()
        with self._lock:
            record = self._records.get(request_id)
            return dict(record) if record else None

    def expire(self) -> None:
        now_mono = time.monotonic()
        now = time.time()
        with self._lock:
            for request_id, started in list(self._started.items()):
                record = self._records.get(request_id)
                if record and not record["terminal"] and now_mono - started >= self.timeout_seconds:
                    record["detail"] = "terminal event timeout; body state is uncertain"
                    self._complete(request_id, record, "unknown", now)

    def mark_inflight_unknown(self, detail: str) -> None:
        now = time.time()
        with self._lock:
            for request_id, record in self._records.items():
                if not record["terminal"]:
                    record["detail"] = str(detail)[:2000]
                    self._complete(request_id, record, "unknown", now)

    def _complete(self, request_id: str, record: dict[str, Any], status: str, now: float) -> None:
        record["status"] = status
        record["terminal"] = True
        record["completed_at"] = _timestamp(now)
        record["updated_at"] = _timestamp(now)
        self._started.pop(request_id, None)
        self._completion.pop(request_id, None)

    def _trim(self) -> None:
        while len(self._records) > self.max_records:
            request_id, _ = self._records.popitem(last=False)
            self._started.pop(request_id, None)
            self._completion.pop(request_id, None)
