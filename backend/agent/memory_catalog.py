"""Read-only canonical memory projection for management APIs and exports."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Iterable, Protocol

from .identity import CompanionIdentity


MEMORY_CATEGORIES = {
    "message",
    "event",
    "task_outcome",
    "profile",
    "relationship",
    "location",
    "experience",
    "summary",
}

SENSITIVE_KEYS = {"api_key", "authorization", "password", "secret", "token"}


class MessageReader(Protocol):
    def get_recent_messages(self, limit: int = 50, sender: str | None = None) -> list[dict]: ...


@dataclass(frozen=True)
class MemoryQuery:
    query: str = ""
    categories: frozenset[str] = frozenset()
    player: str = ""
    offset: int = 0
    limit: int = 50
    states: frozenset[str] = frozenset({"active"})
    record_ids: frozenset[str] = frozenset()


class MemoryCatalog:
    """Projects current local stores into a PostgreSQL-ready record contract."""

    def __init__(self, memory: Any, message_reader: MessageReader, identity: CompanionIdentity,
                 overlay_store: Any | None = None):
        self.memory = memory
        self.message_reader = message_reader
        self.identity = identity
        self.overlay_store = overlay_store

    def status(self) -> dict[str, Any]:
        records, revision = self._snapshot()
        counts: dict[str, int] = {}
        state_counts: dict[str, int] = {}
        for record in records:
            counts[record["category"]] = counts.get(record["category"], 0) + 1
            state_counts[record["state"]] = state_counts.get(record["state"], 0) + 1
        timestamps = [record["occurred_at"] for record in records if record["occurred_at"] > 0]
        return {
            "scope": self._scope(),
            "repository": {
                "backend": "sqlite",
                "catalog_mode": "local_projection",
                "production_ready": False,
                "production_required_backend": "postgresql",
            },
            "revision": revision,
            "record_count": len(records),
            "category_counts": counts,
            "state_counts": state_counts,
            "oldest_at": min(timestamps) if timestamps else None,
            "newest_at": max(timestamps) if timestamps else None,
            "save_blocked_reason": getattr(self.memory, "_save_blocked_reason", None),
        }

    def list_records(self, query: MemoryQuery) -> dict[str, Any]:
        records, revision = self._snapshot()
        filtered = self._filter(records, query)
        page = filtered[query.offset:query.offset + query.limit]
        return {
            "records": [self._summary(record) for record in page],
            "count": len(filtered),
            "next_offset": query.offset + len(page) if query.offset + len(page) < len(filtered) else None,
            "revision": revision,
            "facets": self._facets(filtered),
        }

    def get_record(self, record_id: str) -> dict[str, Any] | None:
        records, _ = self._snapshot()
        return next((record for record in records if record["id"] == record_id), None)

    def select_records(self, query: MemoryQuery) -> tuple[list[dict[str, Any]], str]:
        records, revision = self._snapshot()
        return self._filter(records, query), revision

    def export(self, query: MemoryQuery, export_format: str = "jsonl",
               include_provenance: bool = True) -> tuple[bytes, str]:
        records, revision = self._snapshot()
        filtered = self._filter(records, query)
        if not include_provenance:
            filtered = [{key: value for key, value in record.items() if key != "provenance"} for record in filtered]
        if export_format == "json":
            payload = {
                "schema_version": 1,
                "scope": self._scope(),
                "revision": revision,
                "records": filtered,
            }
            return json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"), "application/json"
        if export_format != "jsonl":
            raise ValueError("format must be json or jsonl")
        lines = [json.dumps({"type": "metadata", "schema_version": 1, "scope": self._scope(), "revision": revision}, ensure_ascii=False)]
        lines.extend(json.dumps(record, ensure_ascii=False, separators=(",", ":")) for record in filtered)
        return ("\n".join(lines) + "\n").encode("utf-8"), "application/x-ndjson"

    def _snapshot(self) -> tuple[list[dict[str, Any]], str]:
        records: list[dict[str, Any]] = []
        for message in self.message_reader.get_recent_messages(limit=1000):
            records.append(self._record(
                "message",
                f"message:{message.get('id', self._digest(message))}",
                float(message.get("timestamp", 0)),
                str(message.get("sender", "unknown")),
                str(message.get("message", "")),
                {
                    "sender": str(message.get("sender", "")),
                    "message": str(message.get("message", "")),
                    "is_system": bool(message.get("is_system", False)),
                    "is_ai": bool(message.get("is_ai", False)),
                    "conversation_id": message.get("conversation_id"),
                    "metadata": self._decode_json(message.get("metadata")),
                },
                "messages_db",
            ))
        records.extend(self._list_records("event", self.memory.events, "memory_json", "description"))
        records.extend(self._list_records("task_outcome", self.memory.task_outcomes, "memory_json", "command"))
        records.extend(self._mapping_records("profile", self.memory.player_profiles, "memory_json"))
        records.extend(self._mapping_records("relationship", self.memory.player_relationships, "memory_json"))
        records.extend(self._mapping_records("location", self.memory.locations, "memory_json"))
        for group in ("servers", "worlds"):
            records.extend(self._mapping_records(
                "experience", self.memory.experiences.get(group, {}), "memory_json", prefix=group,
            ))
        for summary in getattr(self.memory, "summaries", []):
            summary_id = str(summary.get("id") or self._digest(summary))
            records.append(self._record(
                "summary", f"summary:{summary_id}", float(summary.get("created_at", 0) or 0),
                str(summary.get("title") or "Memory summary"), str(summary.get("content") or ""),
                summary, "memory_json", source_ids=list(summary.get("source_ids", [])),
            ))
        records.sort(key=lambda record: (-record["occurred_at"], record["id"]))
        if self.overlay_store is not None:
            states = self.overlay_store.get_states(self.identity.scope_id)
            for record in records:
                record["state"] = states.get(record["id"], "active")
        revision = self._digest([{key: record[key] for key in ("id", "updated_at", "content")} for record in records])
        revision = self._digest({"records": revision, "states": [(record["id"], record["state"]) for record in records]})
        return records, revision

    def _list_records(self, category: str, items: Iterable[dict], source: str,
                      title_key: str) -> list[dict[str, Any]]:
        records = []
        for item in items:
            occurred_at = float(item.get("time", item.get("created_at", item.get("saved_at", 0))) or 0)
            title = str(item.get(title_key) or item.get("type") or category)
            excerpt = str(item.get("description") or item.get("detail") or item.get("message") or title)
            records.append(self._record(
                category, f"{category}:{self._digest(item)}", occurred_at,
                title, excerpt, item, source,
            ))
        return records

    def _mapping_records(self, category: str, items: dict[str, dict], source: str,
                         prefix: str = "") -> list[dict[str, Any]]:
        records = []
        for key, value in items.items():
            occurred_at = float(
                value.get("last_seen", value.get("last_active", value.get("saved_at", value.get("first_seen", 0)))) or 0
            )
            source_key = f"{prefix}:{key}" if prefix else key
            records.append(self._record(
                category, f"{category}:{self._digest(source_key)}", occurred_at,
                str(key), self._mapping_excerpt(category, key, value), value, source,
                source_ids=[source_key],
            ))
        return records

    def _record(self, category: str, record_id: str, occurred_at: float,
                title: str, excerpt: str, content: Any, source: str,
                source_ids: list[str] | None = None) -> dict[str, Any]:
        safe_content = self._redact(content)
        return {
            "id": record_id,
            "category": category,
            "tier": "summary" if category == "summary" else "durable",
            "scope": self._scope(),
            "state": "active",
            "occurred_at": occurred_at,
            "updated_at": occurred_at,
            "revision": 1,
            "title": title[:200],
            "excerpt": excerpt[:500],
            "content": safe_content,
            "provenance": {
                "source": source,
                "source_ids": source_ids or [record_id.split(":", 1)[-1]],
                "source_hash": f"sha256:{self._digest(safe_content)}",
            },
        }

    def _filter(self, records: list[dict[str, Any]], query: MemoryQuery) -> list[dict[str, Any]]:
        needle = query.query.casefold().strip()
        player = query.player.casefold().strip()
        filtered = []
        for record in records:
            if query.record_ids and record["id"] not in query.record_ids:
                continue
            if query.categories and record["category"] not in query.categories:
                continue
            if query.states and record["state"] not in query.states:
                continue
            searchable = json.dumps(record, ensure_ascii=False, sort_keys=True).casefold()
            if needle and needle not in searchable:
                continue
            if player and player not in searchable:
                continue
            filtered.append(record)
        return filtered

    @staticmethod
    def _summary(record: dict[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in record.items() if key != "content"}

    @staticmethod
    def _facets(records: list[dict[str, Any]]) -> dict[str, Any]:
        categories: dict[str, int] = {}
        for record in records:
            categories[record["category"]] = categories.get(record["category"], 0) + 1
        states: dict[str, int] = {}
        for record in records:
            states[record["state"]] = states.get(record["state"], 0) + 1
        return {"categories": categories, "states": states}

    def _scope(self) -> dict[str, str]:
        return {"id": self.identity.scope_id, **self.identity.public_dict()}

    @staticmethod
    def _mapping_excerpt(category: str, key: str, value: dict[str, Any]) -> str:
        if category == "location":
            return f"{value.get('dimension', 'unknown')} ({value.get('x', 0)}, {value.get('y', 0)}, {value.get('z', 0)})"
        if category == "relationship":
            return f"messages={value.get('message_count', 0)}, tasks={value.get('tasks_requested', 0)}"
        if category == "profile":
            return f"messages={value.get('message_count', 0)}, average_length={value.get('avg_message_length', 0):.1f}"
        return str(value.get("description") or value.get("last_position") or key)

    @staticmethod
    def _decode_json(value: Any) -> Any:
        if not isinstance(value, str):
            return value
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value

    @classmethod
    def _redact(cls, value: Any) -> Any:
        if isinstance(value, dict):
            return {
                str(key): "***" if str(key).casefold() in SENSITIVE_KEYS else cls._redact(item)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [cls._redact(item) for item in value]
        return value

    @staticmethod
    def _digest(value: Any) -> str:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()
