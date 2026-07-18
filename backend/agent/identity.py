"""Stable companion identity and scoped persistence paths."""

from __future__ import annotations

import hashlib
import copy
import json
import shutil
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_STORAGE_ROOT = BACKEND_ROOT / ".local" / "companions"
DEFAULT_LEGACY_ROOT = BACKEND_ROOT / "data"
VALID_SCOPES = {"global", "server", "world"}


def _key(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:20]


@dataclass(frozen=True)
class CompanionIdentity:
    companion_id: str
    scope: str = "global"
    server_id: str = "default"
    world_id: str = "default"

    def __post_init__(self) -> None:
        if not self.companion_id.strip():
            raise ValueError("companion_id must not be empty")
        if self.scope not in VALID_SCOPES:
            raise ValueError(f"scope must be one of: {', '.join(sorted(VALID_SCOPES))}")

    @property
    def scope_id(self) -> str:
        if self.scope == "global":
            return "global"
        if self.scope == "server":
            return f"server-{_key(self.server_id)}"
        return f"world-{_key(self.server_id + chr(0) + self.world_id)}"

    def storage_dir(self, root: Path = DEFAULT_STORAGE_ROOT) -> Path:
        return Path(root) / _key(self.companion_id) / self.scope_id

    def public_dict(self) -> dict[str, str]:
        return {
            "companion_id": self.companion_id,
            "scope": self.scope,
            "server_id": self.server_id,
            "world_id": self.world_id,
        }


def _merge_legacy_memory(files: list[Path], destination: Path) -> None:
    merged = {
        "recent_messages": [],
        "events": [],
        "player_profiles": {},
        "locations": {},
        "interaction_count": 0,
        "total_actions": 0,
        "player_relationships": {},
        "experiences": {"servers": {}, "worlds": {}},
        "task_outcomes": [],
        "summaries": [],
    }
    for path in sorted(files, key=lambda item: item.stat().st_mtime):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        merged["recent_messages"].extend(data.get("recent_messages", []))
        merged["events"].extend(data.get("events", []))
        merged["player_profiles"].update(data.get("player_profiles", {}))
        merged["locations"].update(data.get("locations", {}))
        merged["interaction_count"] += int(data.get("interaction_count", 0))
        merged["total_actions"] += int(data.get("total_actions", 0))
        for key, incoming in data.get("player_relationships", {}).items():
            if key not in merged["player_relationships"]:
                merged["player_relationships"][key] = copy.deepcopy(incoming)
                continue
            current = merged["player_relationships"][key]
            current["names"] = sorted(set(current.get("names", [])) | set(incoming.get("names", [])))
            current["first_seen"] = min(current.get("first_seen", float("inf")), incoming.get("first_seen", float("inf")))
            current["last_seen"] = max(current.get("last_seen", 0), incoming.get("last_seen", 0))
            current["message_count"] = current.get("message_count", 0) + incoming.get("message_count", 0)
            current["tasks_requested"] = current.get("tasks_requested", 0) + incoming.get("tasks_requested", 0)
            current_outcomes = current.setdefault("task_outcomes", {})
            for outcome, count in incoming.get("task_outcomes", {}).items():
                current_outcomes[outcome] = current_outcomes.get(outcome, 0) + count
            if incoming.get("last_task", {}).get("time", 0) >= current.get("last_task", {}).get("time", 0):
                current["last_task"] = copy.deepcopy(incoming.get("last_task", {}))
        experiences = data.get("experiences", {})
        for key, incoming in experiences.get("servers", {}).items():
            current = merged["experiences"]["servers"].setdefault(key, copy.deepcopy(incoming))
            if current is incoming:
                continue
            current["first_seen"] = min(current.get("first_seen", float("inf")), incoming.get("first_seen", float("inf")))
            current["last_seen"] = max(current.get("last_seen", 0), incoming.get("last_seen", 0))
            current["known_players"] = sorted(set(current.get("known_players", [])) | set(incoming.get("known_players", [])))
        for key, incoming in experiences.get("worlds", {}).items():
            if key not in merged["experiences"]["worlds"]:
                merged["experiences"]["worlds"][key] = copy.deepcopy(incoming)
                continue
            current = merged["experiences"]["worlds"][key]
            current["first_seen"] = min(current.get("first_seen", float("inf")), incoming.get("first_seen", float("inf")))
            current["last_seen"] = max(current.get("last_seen", 0), incoming.get("last_seen", 0))
            current["deaths"] = current.get("deaths", 0) + incoming.get("deaths", 0)
            current.setdefault("dimensions", {}).update(incoming.get("dimensions", {}))
            if incoming.get("last_seen", 0) >= current.get("last_seen", 0):
                current["last_position"] = copy.deepcopy(incoming.get("last_position", {}))
            current_stats = current.setdefault("task_stats", {})
            for command, outcomes in incoming.get("task_stats", {}).items():
                command_stats = current_stats.setdefault(command, {})
                for outcome, count in outcomes.items():
                    command_stats[outcome] = command_stats.get(outcome, 0) + count
        merged["task_outcomes"].extend(data.get("task_outcomes", []))
        merged["summaries"].extend(data.get("summaries", []))
    merged["recent_messages"] = sorted(merged["recent_messages"], key=lambda item: item.get("time", 0))[-50:]
    merged["events"] = sorted(merged["events"], key=lambda item: item.get("time", 0))[-500:]
    merged["task_outcomes"] = sorted(merged["task_outcomes"], key=lambda item: item.get("time", 0))[-100:]
    summaries_by_id = {str(item.get("id")): item for item in merged["summaries"] if item.get("id")}
    merged["summaries"] = sorted(
        summaries_by_id.values(), key=lambda item: item.get("created_at", 0),
    )[-100:]
    merged["schema_version"] = 4
    temporary = destination.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(merged, indent=2, ensure_ascii=False), encoding="utf-8")
    temporary.replace(destination)


def _merge_legacy_databases(files: list[Path], destination: Path) -> None:
    ordered = sorted(files, key=lambda item: item.stat().st_mtime, reverse=True)
    shutil.copy2(ordered[0], destination)
    connection = sqlite3.connect(destination)
    try:
        for index, source in enumerate(ordered[1:]):
            alias = f"legacy_{index}"
            connection.execute(f"ATTACH DATABASE ? AS {alias}", (str(source),))
            connection.execute(
                f"INSERT INTO messages (timestamp, sender, message, is_system, is_ai, conversation_id, metadata) "
                f"SELECT timestamp, sender, message, is_system, is_ai, conversation_id, metadata FROM {alias}.messages"
            )
            connection.execute(
                f"INSERT INTO events (timestamp, event_type, description, player_involved, location, metadata) "
                f"SELECT timestamp, event_type, description, player_involved, location, metadata FROM {alias}.events"
            )
            connection.execute(
                f"INSERT INTO conversations (id, started_at, last_activity, participants, message_count, topic) "
                f"SELECT id, started_at, last_activity, participants, message_count, topic FROM {alias}.conversations WHERE true "
                "ON CONFLICT(id) DO UPDATE SET "
                "started_at = min(conversations.started_at, excluded.started_at), "
                "last_activity = max(conversations.last_activity, excluded.last_activity), "
                "message_count = conversations.message_count + excluded.message_count, "
                "participants = coalesce(conversations.participants, excluded.participants), "
                "topic = coalesce(conversations.topic, excluded.topic)"
            )
            connection.execute(
                f"INSERT INTO players (uuid, name, first_seen, last_seen, message_count, avg_message_length, interaction_style, notes) "
                f"SELECT uuid, name, first_seen, last_seen, message_count, avg_message_length, interaction_style, notes "
                f"FROM {alias}.players WHERE true ON CONFLICT(uuid) DO UPDATE SET "
                "name = excluded.name, "
                "first_seen = min(players.first_seen, excluded.first_seen), "
                "last_seen = max(players.last_seen, excluded.last_seen), "
                "avg_message_length = CASE WHEN players.message_count + excluded.message_count = 0 THEN 0 "
                "ELSE (players.avg_message_length * players.message_count + excluded.avg_message_length * excluded.message_count) "
                "/ (players.message_count + excluded.message_count) END, "
                "message_count = players.message_count + excluded.message_count, "
                "interaction_style = coalesce(players.interaction_style, excluded.interaction_style), "
                "notes = coalesce(players.notes, excluded.notes)"
            )
            connection.commit()
            connection.execute(f"DETACH DATABASE {alias}")
        connection.commit()
    finally:
        connection.close()


def migrate_legacy_sessions(target: Path, legacy_root: Path = DEFAULT_LEGACY_ROOT) -> list[str]:
    """Merge legacy random sessions once without deleting their source files."""
    claim_path = legacy_root / ".lcu-migration.json"
    if claim_path.exists():
        return []
    if (target / "memory.json").exists() or (target / "messages.db").exists():
        legacy_root.mkdir(parents=True, exist_ok=True)
        temporary_claim = claim_path.with_suffix(".tmp")
        temporary_claim.write_text(
            json.dumps({"target": str(target.resolve()), "skipped": "stable target already exists"}, indent=2),
            encoding="utf-8",
        )
        temporary_claim.replace(claim_path)
        return []
    memory_dir = legacy_root / "memory"
    memory_files = {path.stem.removeprefix("session_"): path for path in memory_dir.glob("session_*.json")}
    db_files = {path.stem.removeprefix("messages_"): path for path in legacy_root.glob("messages_*.db")}
    session_ids = sorted(set(memory_files) | set(db_files))
    if not session_ids:
        return []
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = target.parent / f".{target.name}.migration-{uuid.uuid4().hex}"
    migration = {"legacy_session_ids": session_ids, "target": str(target.resolve())}
    try:
        staging.mkdir()
        if memory_files:
            _merge_legacy_memory(list(memory_files.values()), staging / "memory.json")
        if db_files:
            _merge_legacy_databases(list(db_files.values()), staging / "messages.db")
        (staging / "legacy-migration.json").write_text(json.dumps(migration, indent=2), encoding="utf-8")
        staging.replace(target)
        legacy_root.mkdir(parents=True, exist_ok=True)
        temporary_claim = claim_path.with_suffix(".tmp")
        temporary_claim.write_text(json.dumps(migration, indent=2), encoding="utf-8")
        temporary_claim.replace(claim_path)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return session_ids
