"""Normalized, revisioned view of live companion observations."""

from __future__ import annotations

import copy
import json
import math
import time
from collections import deque
from dataclasses import dataclass
from typing import Any


@dataclass
class FactState:
    value: Any
    revision: int
    observed_at: float
    source: str
    ttl_seconds: float


class WorldModel:
    SCHEMA_VERSION = 1
    SNAPSHOT_FIELDS = {
        "player": (dict, {}, 3.0),
        "world": (dict, {}, 3.0),
        "inventory": (list, [], 10.0),
        "equipment": (dict, {}, 10.0),
        "integrations": (dict, {}, 30.0),
        "entities": (list, [], 3.0),
        "online_players": (list, [], 10.0),
        "nearby_blocks": (list, [], 3.0),
        "nearby_workstations": (list, [], 5.0),
        "nearby_storage": (list, [], 5.0),
    }
    OVERLAY_FIELDS = {"behavior_state", "task_state", "control_state"}
    OBSERVATION_ORDER = (
        "control_state", "task_state", "player", "world", "behavior_state",
        "equipment", "inventory", "online_players", "entities", "nearby_blocks",
        "nearby_workstations", "nearby_storage", "integrations",
    )

    def __init__(self, initial: dict[str, Any] | None = None):
        self.revision = 0
        self.observed_at: float | None = None
        self.connected = False
        self._facts: dict[str, FactState] = {}
        self._passthrough: dict[str, Any] = {}
        self.invalid_updates = 0
        self._journal: deque[dict[str, Any]] = deque(maxlen=128)
        self._decision_triggers: deque[dict[str, Any]] = deque(maxlen=32)
        self._journal_sequence = 0
        if initial:
            self._seed(initial)

    def ingest_snapshot(self, data: dict[str, Any], *, observed_at: float | None = None,
                        source: str = "body.state_update") -> int:
        if not isinstance(data, dict):
            self.invalid_updates += 1
            return self.revision
        now = time.time() if observed_at is None else float(observed_at)
        previous = self.legacy_projection()
        self.revision += 1
        core_observed = False
        for name, (expected_type, empty, ttl) in self.SNAPSHOT_FIELDS.items():
            if name not in data:
                value = copy.deepcopy(empty)
            elif isinstance(data[name], expected_type):
                value = copy.deepcopy(data[name])
                if name in {"player", "world"}:
                    core_observed = True
            else:
                self.invalid_updates += 1
                continue
            self._facts[name] = FactState(value, self.revision, now, source, ttl)
        if core_observed:
            self.observed_at = now

        for name in self.OVERLAY_FIELDS:
            if name in data:
                self._ingest_overlay(name, data[name], now, source)

        owned = set(self.SNAPSHOT_FIELDS) | self.OVERLAY_FIELDS
        for name, value in data.items():
            if name not in owned:
                self._passthrough[name] = copy.deepcopy(value)
        current = self.legacy_projection()
        self._record_snapshot_changes(previous, current, now)
        for name in self.OVERLAY_FIELDS:
            if name in data and isinstance(data[name], dict):
                self._record_overlay_change(name, previous.get(name), current.get(name, {}), now)
        return self.revision

    def ingest_overlay(self, name: str, value: dict[str, Any], *,
                       observed_at: float | None = None, source: str | None = None) -> int:
        if name not in self.OVERLAY_FIELDS:
            raise ValueError(f"unknown world model overlay: {name}")
        now = time.time() if observed_at is None else float(observed_at)
        previous = copy.deepcopy(self._facts.get(name).value) if name in self._facts else None
        self.revision += 1
        self._ingest_overlay(name, value, now, source or f"body.{name}")
        current = self._facts.get(name)
        if current is not None:
            self._record_overlay_change(name, previous, current.value, now)
        return self.revision

    def set_connected(self, connected: bool) -> None:
        normalized = bool(connected)
        if normalized == self.connected:
            return
        self.connected = normalized
        self._record(
            "body.connected" if normalized else "body.disconnected",
            "info" if normalized else "warning", {"connected": normalized},
            time.time(), decision_boundary=False,
        )

    def legacy_projection(self, current: dict[str, Any] | None = None) -> dict[str, Any]:
        result = copy.deepcopy(current) if isinstance(current, dict) else {}
        result.update(copy.deepcopy(self._passthrough))
        for name, fact in self._facts.items():
            result[name] = copy.deepcopy(fact.value)
        return result

    def observation_slice(self, projection: dict[str, Any], *, max_chars: int = 8000,
                          now: float | None = None) -> dict[str, Any]:
        if max_chars < 512:
            raise ValueError("observation budget must be at least 512 characters")
        current_time = time.time() if now is None else float(now)
        result: dict[str, Any] = {
            "observation_meta": {
                "schema": self.SCHEMA_VERSION,
                "revision": self.revision,
                "observed_at": self.observed_at,
                "age_seconds": None if self.observed_at is None else max(0.0, current_time - self.observed_at),
                "stale": self.is_stale(current_time),
                "source": "world_model",
            }
        }
        normalized = self._normalized_projection(projection)
        journal_inserted = False
        for name in self.OBSERVATION_ORDER:
            value = normalized.get(name)
            if value not in (None, {}, []):
                self._append_with_budget(result, name, value, max_chars)
            if name == "world":
                journal = self.recent_journal(limit=12)
                if journal:
                    self._append_with_budget(result, "semantic_journal", journal, max_chars)
                    journal_inserted = True
        if not journal_inserted:
            journal = self.recent_journal(limit=12)
            if journal:
                self._append_with_budget(result, "semantic_journal", journal, max_chars)
        return result

    def status(self, *, now: float | None = None) -> dict[str, Any]:
        current_time = time.time() if now is None else float(now)
        facts = {}
        for name, fact in sorted(self._facts.items()):
            age = max(0.0, current_time - fact.observed_at)
            facts[name] = {
                "revision": fact.revision,
                "observed_at": fact.observed_at,
                "source": fact.source,
                "ttl_seconds": fact.ttl_seconds,
                "age_seconds": age,
                "stale": not self.connected or age > fact.ttl_seconds,
            }
        return {
            "schema": self.SCHEMA_VERSION,
            "revision": self.revision,
            "observed_at": self.observed_at,
            "connected": self.connected,
            "stale": self.is_stale(current_time),
            "invalid_updates": self.invalid_updates,
            "journal": {
                "count": len(self._journal),
                "latest_sequence": self._journal_sequence,
                "pending_decision_triggers": len(self._decision_triggers),
            },
            "facts": facts,
        }

    def recent_journal(self, limit: int = 20) -> list[dict[str, Any]]:
        bounded = max(1, min(100, int(limit)))
        return copy.deepcopy(list(self._journal)[-bounded:])

    def pending_decision_triggers(self, limit: int = 20) -> list[dict[str, Any]]:
        bounded = max(1, min(32, int(limit)))
        return copy.deepcopy(list(self._decision_triggers)[:bounded])

    def acknowledge_decision_triggers(self, through_sequence: int) -> int:
        removed = 0
        while self._decision_triggers and self._decision_triggers[0]["sequence"] <= through_sequence:
            self._decision_triggers.popleft()
            removed += 1
        return removed

    def is_stale(self, now: float | None = None) -> bool:
        current_time = time.time() if now is None else float(now)
        return not self.connected or self.observed_at is None or current_time - self.observed_at > 3.0

    def _seed(self, initial: dict[str, Any]) -> None:
        now = time.time()
        for name, (expected_type, _, ttl) in self.SNAPSHOT_FIELDS.items():
            value = initial.get(name)
            if isinstance(value, expected_type):
                self._facts[name] = FactState(copy.deepcopy(value), 0, now, "session.seed", ttl)
        for name in self.OVERLAY_FIELDS:
            value = initial.get(name)
            if isinstance(value, dict):
                self._facts[name] = FactState(copy.deepcopy(value), 0, now, "session.seed", 3.0)

    def _record_snapshot_changes(self, previous: dict[str, Any], current: dict[str, Any], now: float) -> None:
        old_player = previous.get("player", {}) if isinstance(previous.get("player"), dict) else {}
        new_player = current.get("player", {}) if isinstance(current.get("player"), dict) else {}
        old_world = previous.get("world", {}) if isinstance(previous.get("world"), dict) else {}
        new_world = current.get("world", {}) if isinstance(current.get("world"), dict) else {}

        old_health = self._number_or_none(old_player.get("health"))
        new_health = self._number_or_none(new_player.get("health"))
        if old_health is not None and new_health is not None and new_health < old_health:
            critical = new_health <= 4
            dangerous = new_health <= 8 or old_health - new_health >= 4
            self._record("player.health_decreased", "critical" if critical else "warning", {
                "previous": old_health, "current": new_health, "delta": new_health - old_health,
            }, now, decision_boundary=dangerous)

        old_hunger = self._number_or_none(old_player.get("hunger"))
        new_hunger = self._number_or_none(new_player.get("hunger"))
        if old_hunger is not None and new_hunger is not None and old_hunger > 6 >= new_hunger:
            self._record("player.hunger_low", "warning", {
                "previous": old_hunger, "current": new_hunger,
            }, now, decision_boundary=True)

        old_dimension = old_player.get("dimension") or old_world.get("dimension")
        new_dimension = new_player.get("dimension") or new_world.get("dimension")
        if old_dimension and new_dimension and old_dimension != new_dimension:
            self._record("world.dimension_changed", "warning", {
                "previous": str(old_dimension), "current": str(new_dimension),
            }, now, decision_boundary=True)

        old_raining = old_world.get("is_raining")
        new_raining = new_world.get("is_raining")
        if isinstance(old_raining, bool) and isinstance(new_raining, bool) and old_raining != new_raining:
            self._record("world.weather_changed", "info", {"raining": new_raining}, now)

        old_counts = self._inventory_counts(previous.get("inventory"))
        new_counts = self._inventory_counts(current.get("inventory"))
        if old_counts != new_counts:
            changes = []
            for item_id in sorted(set(old_counts) | set(new_counts)):
                delta = new_counts.get(item_id, 0) - old_counts.get(item_id, 0)
                if delta:
                    changes.append({"item": item_id, "delta": delta, "count": new_counts.get(item_id, 0)})
            self._record("inventory.counts_changed", "info", {"changes": changes[:16]}, now)

    def _record_overlay_change(self, name: str, previous: Any, current: dict[str, Any], now: float) -> None:
        old = previous if isinstance(previous, dict) else {}
        first_observation = not isinstance(previous, dict)
        if old == current:
            return
        if name == "task_state":
            old_status = str(old.get("status", "idle")).strip().lower()
            new_status = str(current.get("status", "idle")).strip().lower()
            if old_status == new_status and old.get("kind") == current.get("kind") and old.get("target") == current.get("target"):
                return
            boundary = new_status in {
                "succeeded", "done", "failed", "cancelled", "unknown", "blocked", "waiting_input",
            }
            severity = "warning" if new_status in {"failed", "unknown", "blocked"} else "info"
            self._record("task.state_changed", severity, {
                "kind": current.get("kind"), "status": new_status,
                "target": current.get("target"), "detail": current.get("detail"),
            }, now, decision_boundary=boundary and not first_observation)
        elif name == "control_state":
            old_control = old.get("ai_controlled")
            new_control = current.get("ai_controlled")
            if old_control != new_control:
                self._record("control.changed", "warning", {
                    "ai_controlled": new_control,
                }, now, decision_boundary=not first_observation)
        elif name == "behavior_state":
            old_active = old.get("active_behavior")
            new_active = current.get("active_behavior")
            if old_active != new_active:
                self._record("behavior.changed", "info", {"active_behavior": new_active}, now)

    def _record(self, event_type: str, severity: str, data: dict[str, Any], occurred_at: float,
                decision_boundary: bool = False) -> None:
        self._journal_sequence += 1
        event = {
            "sequence": self._journal_sequence,
            "type": event_type,
            "severity": severity,
            "occurred_at": occurred_at,
            "revision": self.revision,
            "decision_boundary": decision_boundary,
            "data": self._compact(data),
        }
        self._journal.append(event)
        if decision_boundary:
            self._decision_triggers.append(copy.deepcopy(event))

    @staticmethod
    def _inventory_counts(value: Any) -> dict[str, int]:
        if not isinstance(value, list):
            return {}
        counts: dict[str, int] = {}
        for item in value:
            if not isinstance(item, dict):
                continue
            item_id = str(item.get("name") or item.get("item_id") or "").strip()
            if not item_id:
                continue
            try:
                count = max(0, int(item.get("count", 0)))
            except (TypeError, ValueError):
                continue
            counts[item_id] = counts.get(item_id, 0) + count
        return counts

    @staticmethod
    def _number_or_none(value: Any) -> float | None:
        try:
            number = float(value)
            return number if math.isfinite(number) else None
        except (TypeError, ValueError):
            return None

    def _ingest_overlay(self, name: str, value: Any, now: float, source: str) -> None:
        if not isinstance(value, dict):
            self.invalid_updates += 1
            return
        self._facts[name] = FactState(copy.deepcopy(value), self.revision, now, source, 3.0)

    @classmethod
    def _normalized_projection(cls, projection: dict[str, Any]) -> dict[str, Any]:
        result = {}
        for name in cls.OBSERVATION_ORDER:
            value = projection.get(name)
            if isinstance(value, dict):
                result[name] = cls._compact(value)
            elif isinstance(value, list):
                items = cls._deduplicate(name, value)
                if name == "inventory":
                    items.sort(key=lambda item: (
                        cls._number(item.get("slot"), math.inf), cls._stable_marker(item),
                    ))
                elif name in {"entities", "nearby_blocks", "nearby_workstations", "nearby_storage"}:
                    items.sort(key=lambda item: (
                        cls._number(item.get("distance"), math.inf), cls._stable_marker(item),
                    ))
                elif name == "online_players":
                    items.sort(key=lambda item: (
                        str(item.get("name", "")).casefold(), cls._stable_marker(item),
                    ))
                result[name] = [cls._compact(item) for item in items]
        return result

    @classmethod
    def _append_with_budget(cls, target: dict[str, Any], name: str,
                            value: Any, max_chars: int) -> None:
        candidate = copy.deepcopy(target)
        candidate[name] = value
        if cls._encoded_size(candidate) <= max_chars:
            target[name] = value
            return
        if isinstance(value, list):
            accepted = []
            for item in value:
                candidate = copy.deepcopy(target)
                candidate[name] = [*accepted, item]
                if cls._encoded_size(candidate) > max_chars:
                    break
                accepted.append(item)
            if accepted:
                target[name] = accepted
            return
        if isinstance(value, dict):
            accepted = {}
            for key, item in value.items():
                candidate = copy.deepcopy(target)
                candidate[name] = {**accepted, key: item}
                if cls._encoded_size(candidate) > max_chars:
                    continue
                accepted[key] = item
            if accepted:
                target[name] = accepted

    @classmethod
    def _compact(cls, value: Any, depth: int = 0) -> Any:
        if depth >= 4:
            return str(value)[:128]
        if isinstance(value, str):
            return value[:256]
        if isinstance(value, (int, float, bool)) or value is None:
            return value
        if isinstance(value, dict):
            return {
                str(key)[:64]: cls._compact(value[key], depth + 1)
                for key in sorted(value, key=lambda item: str(item))[:32]
            }
        if isinstance(value, list):
            return [cls._compact(item, depth + 1) for item in value[:64]]
        return str(value)[:128]

    @staticmethod
    def _deduplicate(name: str, values: list[Any]) -> list[dict[str, Any]]:
        result = []
        seen = set()
        for raw in values:
            if not isinstance(raw, dict):
                continue
            if name == "inventory":
                identity = (raw.get("slot"), raw.get("name"))
            elif name == "entities":
                identity = (raw.get("id"), raw.get("type"), raw.get("name"))
            elif name == "online_players":
                identity = raw.get("uuid") or str(raw.get("name", "")).casefold()
            else:
                identity = (raw.get("x"), raw.get("y"), raw.get("z"), raw.get("block_id") or raw.get("name"))
            marker = json.dumps(identity, ensure_ascii=True, default=str)
            if marker in seen:
                continue
            seen.add(marker)
            result.append(copy.deepcopy(raw))
        return result

    @staticmethod
    def _number(value: Any, fallback: float) -> float:
        try:
            number = float(value)
            return number if math.isfinite(number) else fallback
        except (TypeError, ValueError):
            return fallback

    @staticmethod
    def _encoded_size(value: dict[str, Any]) -> int:
        return len(json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str))

    @staticmethod
    def _stable_marker(value: dict[str, Any]) -> str:
        return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), default=str)
