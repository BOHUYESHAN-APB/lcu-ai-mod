"""Preview and commit coordination for durable memory summaries."""

from __future__ import annotations

import hashlib
import json
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any

from .memory_catalog import MemoryQuery


class MemoryPreviewError(ValueError):
    pass


@dataclass
class StoredPreview:
    public: dict[str, Any]
    query: MemoryQuery
    source_revision: str
    source_hash: str
    expires_at: float
    committed_summary: dict[str, Any] | None = None


@dataclass
class StoredStatePreview:
    public: dict[str, Any]
    source_revision: str
    source_hash: str
    expires_at: float
    changes: dict[str, list[str]]
    committed_result: dict[str, Any] | None = None


class MemoryPreviewStore:
    """Bounded process-local previews; durable data is written only on commit."""

    def __init__(self, ttl_seconds: float = 900, max_previews: int = 100):
        self.ttl_seconds = ttl_seconds
        self.max_previews = max_previews
        self._lock = threading.RLock()
        self._previews: dict[str, StoredPreview] = {}
        self._state_previews: dict[str, StoredStatePreview] = {}

    def create_summary_preview(self, *, query: MemoryQuery, records: list[dict[str, Any]],
                               source_revision: str, summary: str, agent: str,
                               model: str, target_tokens: int,
                               usage: dict[str, Any] | None = None) -> dict[str, Any]:
        if not records:
            raise MemoryPreviewError("No memory records matched the selection")
        summary = summary.strip()
        if not summary:
            raise MemoryPreviewError("Summary model returned empty content")
        now = time.time()
        preview_id = str(uuid.uuid4())
        source_hash = self.source_hash(records)
        public = {
            "id": preview_id,
            "status": "ready",
            "strategy": "semantic_summary",
            "summary": summary,
            "source_revision": source_revision,
            "source_hash": source_hash,
            "source_ids": [record["id"] for record in records],
            "source_count": len(records),
            "source_range": {
                "from": min((record.get("occurred_at", 0) for record in records), default=0),
                "to": max((record.get("occurred_at", 0) for record in records), default=0),
            },
            "agent": agent,
            "model": model,
            "target_tokens": target_tokens,
            "usage": usage or {},
            "source_records_retained": True,
            "created_at": now,
            "expires_at": now + self.ttl_seconds,
        }
        stored = StoredPreview(public, query, source_revision, source_hash, public["expires_at"])
        with self._lock:
            self._purge(now)
            self._previews[preview_id] = stored
            while len(self._previews) > self.max_previews:
                self._previews.pop(next(iter(self._previews)))
        return dict(public)

    def get(self, preview_id: str) -> StoredPreview:
        with self._lock:
            self._purge(time.time())
            preview = self._previews.get(preview_id)
            if preview is None:
                raise MemoryPreviewError("Memory preview not found or expired")
            return preview

    def create_state_preview(self, *, action: str, records: list[dict[str, Any]],
                             source_revision: str, changes: dict[str, list[str]],
                             reason: str = "") -> dict[str, Any]:
        selected_ids = {record_id for record_ids in changes.values() for record_id in record_ids}
        selected_records = [record for record in records if record.get("id") in selected_ids]
        if not selected_records or len(selected_records) != len(selected_ids):
            raise MemoryPreviewError("Memory state preview contains missing records")
        now = time.time()
        preview_id = str(uuid.uuid4())
        confirmation_text = f"{action.upper()} {len(selected_records)} RECORDS"
        confirmation_token = str(uuid.uuid4())
        public = {
            "id": preview_id,
            "status": "ready",
            "action": action,
            "changes": {state: list(record_ids) for state, record_ids in changes.items()},
            "affected_count": len(selected_records),
            "category_counts": self._category_counts(selected_records),
            "state_counts": self._state_counts(selected_records),
            "source_revision": source_revision,
            "source_hash": self.source_hash(selected_records),
            "confirmation_token": confirmation_token,
            "confirmation_text": confirmation_text,
            "reason": reason,
            "created_at": now,
            "expires_at": now + self.ttl_seconds,
            "source_records_retained": True,
        }
        stored = StoredStatePreview(
            public=public,
            source_revision=source_revision,
            source_hash=public["source_hash"],
            expires_at=public["expires_at"],
            changes=public["changes"],
        )
        with self._lock:
            self._purge(now)
            self._state_previews[preview_id] = stored
            while len(self._state_previews) > self.max_previews:
                self._state_previews.pop(next(iter(self._state_previews)))
        return dict(public)

    def get_state_preview(self, preview_id: str) -> StoredStatePreview:
        with self._lock:
            self._purge(time.time())
            preview = self._state_previews.get(preview_id)
            if preview is None:
                raise MemoryPreviewError("Memory state preview not found or expired")
            return preview

    def commit_state_preview(self, preview_id: str, *, confirmation_token: str,
                             confirmation_text: str, current_revision: str,
                             current_records: list[dict[str, Any]], overlay_store: Any,
                             scope_id: str) -> dict[str, Any]:
        preview = self.get_state_preview(preview_id)
        if preview.committed_result is not None:
            return dict(preview.committed_result)
        if confirmation_token != preview.public["confirmation_token"]:
            raise MemoryPreviewError("Memory confirmation token is invalid")
        if confirmation_text != preview.public["confirmation_text"]:
            raise MemoryPreviewError("Memory confirmation text does not match")
        if current_revision != preview.source_revision:
            raise MemoryPreviewError("Memory changed after preview creation")
        selected_ids = {record_id for record_ids in preview.changes.values() for record_id in record_ids}
        selected_records = [record for record in current_records if record.get("id") in selected_ids]
        if len(selected_records) != len(selected_ids) or self.source_hash(selected_records) != preview.source_hash:
            raise MemoryPreviewError("Selected memory records changed after preview creation")
        result = overlay_store.apply_changes(
            scope_id, preview.changes, reason=str(preview.public.get("reason", "")),
        )
        preview.committed_result = dict(result)
        preview.public["status"] = "committed"
        return dict(result)

    def commit(self, preview_id: str, *, current_revision: str,
               current_records: list[dict[str, Any]], memory: Any) -> dict[str, Any]:
        preview = self.get(preview_id)
        if preview.committed_summary is not None:
            return dict(preview.committed_summary)
        if current_revision != preview.source_revision:
            raise MemoryPreviewError("Memory changed after preview creation")
        if self.source_hash(current_records) != preview.source_hash:
            raise MemoryPreviewError("Selected memory records changed after preview creation")
        if getattr(memory, "_save_blocked_reason", None):
            raise MemoryPreviewError("Memory persistence is blocked and cannot accept summaries")
        summary = {
            "id": f"summary_{preview_id}",
            "title": f"Semantic summary of {preview.public['source_count']} records",
            "content": preview.public["summary"],
            "created_at": time.time(),
            "state": "active",
            "source_ids": list(preview.public["source_ids"]),
            "source_hash": preview.source_hash,
            "source_revision": preview.source_revision,
            "source_range": dict(preview.public["source_range"]),
            "summary_version": 1,
            "agent": preview.public["agent"],
            "model": preview.public["model"],
            "target_tokens": preview.public["target_tokens"],
            "source_records_retained": True,
        }
        previous = list(memory.summaries)
        memory.add_summary(summary)
        if not memory.save():
            memory.summaries = previous
            raise MemoryPreviewError("Failed to persist memory summary")
        preview.committed_summary = dict(summary)
        preview.public["status"] = "committed"
        return dict(summary)

    @staticmethod
    def source_hash(records: list[dict[str, Any]]) -> str:
        source = [
            {"id": record.get("id"), "updated_at": record.get("updated_at"), "content": record.get("content")}
            for record in records
        ]
        payload = json.dumps(source, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _purge(self, now: float) -> None:
        expired = [preview_id for preview_id, preview in self._previews.items() if preview.expires_at <= now]
        for preview_id in expired:
            self._previews.pop(preview_id, None)
        expired_states = [
            preview_id for preview_id, preview in self._state_previews.items() if preview.expires_at <= now
        ]
        for preview_id in expired_states:
            self._state_previews.pop(preview_id, None)

    @staticmethod
    def _category_counts(records: list[dict[str, Any]]) -> dict[str, int]:
        counts: dict[str, int] = {}
        for record in records:
            category = str(record.get("category", "unknown"))
            counts[category] = counts.get(category, 0) + 1
        return counts

    @staticmethod
    def _state_counts(records: list[dict[str, Any]]) -> dict[str, int]:
        counts: dict[str, int] = {}
        for record in records:
            state = str(record.get("state", "active"))
            counts[state] = counts.get(state, 0) + 1
        return counts


def evaluate_retention(records: list[dict[str, Any]], rules: list[dict[str, Any]],
                       now: float | None = None) -> dict[str, list[str]]:
    """Return reversible state changes for the configured retention rules."""
    current_time = time.time() if now is None else now
    changes: dict[str, list[str]] = {"archived": [], "deleted": []}
    by_category: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        by_category.setdefault(str(record.get("category", "")), []).append(record)
    for rule in rules:
        if not rule.get("enabled", True):
            continue
        category = str(rule.get("category", ""))
        category_records = sorted(
            by_category.get(category, []),
            key=lambda record: (-float(record.get("occurred_at", 0)), str(record.get("id", ""))),
        )
        min_keep = max(0, int(rule.get("min_keep", 0)))
        protected_ids = {record.get("id") for record in category_records[:min_keep]}
        archive_days = rule.get("archive_after_days")
        delete_days = rule.get("delete_after_days")
        for record in category_records:
            if record.get("id") in protected_ids:
                continue
            age_days = max(0.0, current_time - float(record.get("occurred_at", 0))) / 86400
            state = record.get("state", "active")
            if state == "archived" and delete_days is not None and age_days >= float(delete_days):
                changes["deleted"].append(str(record["id"]))
            elif state == "active" and archive_days is not None and age_days >= float(archive_days):
                changes["archived"].append(str(record["id"]))
    return {state: sorted(set(record_ids)) for state, record_ids in changes.items() if record_ids}
