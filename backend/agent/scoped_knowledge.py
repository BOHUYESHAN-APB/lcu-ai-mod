"""Layered declarative knowledge and learned GUI skill templates."""

from __future__ import annotations

import copy
import json
import threading
import time
from pathlib import Path
from typing import Any


SCOPE_KINDS = ("global", "pack", "server", "world")


class ScopedKnowledgeStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._data = self._load()

    @staticmethod
    def scope_chain(companion_id: str, context: dict[str, Any]) -> list[str]:
        prefix = str(companion_id).strip()
        chain = [f"{prefix}:global"]
        pack = str(context.get("pack_fingerprint", "")).strip()
        server = str(context.get("server_id", "")).strip()
        world = str(context.get("world_id", "")).strip()
        if pack:
            chain.append(f"{prefix}:pack:{pack}")
        if server:
            chain.append(f"{prefix}:server:{server}")
        if server and world:
            chain.append(f"{prefix}:world:{server}\0{world}")
        return chain

    def put(self, scope_key: str, template_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        template_id = str(template_id).strip()
        if not template_id:
            raise ValueError("template id must not be empty")
        self._validate_scope(scope_key)
        steps = payload.get("steps", [])
        if not isinstance(steps, list) or not steps:
            raise ValueError("template steps must be a non-empty list")
        allowed_actions = {"observe_gui", "ui_click", "key_press", "inventory_click", "container_button"}
        for step in steps:
            if not isinstance(step, dict) or step.get("action") not in allowed_actions:
                raise ValueError("template contains an unsupported action")
            if any(key in step for key in ("code", "script", "python", "javascript")):
                raise ValueError("executable code is not allowed in learned templates")
        stored = {
            "id": template_id,
            "name": str(payload.get("name") or template_id),
            "screen_class": str(payload.get("screen_class", "")),
            "menu_class": str(payload.get("menu_class", "")),
            "mod_id": str(payload.get("mod_id", "")),
            "steps": copy.deepcopy(steps),
            "preconditions": copy.deepcopy(payload.get("preconditions", {})),
            "postconditions": copy.deepcopy(payload.get("postconditions", {})),
            "state": str(payload.get("state", "draft")),
            "updated_at": time.time(),
            "scope_key": scope_key,
        }
        if stored["state"] not in {"draft", "approved", "disabled", "tombstone"}:
            raise ValueError("template state is invalid")
        with self._lock:
            self._data.setdefault("templates", {}).setdefault(scope_key, {})[template_id] = stored
            self._save()
        return copy.deepcopy(stored)

    def resolve(self, chain: list[str]) -> list[dict[str, Any]]:
        winners: dict[str, dict[str, Any]] = {}
        with self._lock:
            for scope_key in chain:
                for template_id, template in self._data.get("templates", {}).get(scope_key, {}).items():
                    if template.get("state") == "tombstone":
                        winners.pop(template_id, None)
                    else:
                        winners[template_id] = copy.deepcopy(template)
        return sorted(winners.values(), key=lambda item: item["id"])

    def list_all(self, chain: list[str]) -> list[dict[str, Any]]:
        result = []
        with self._lock:
            for rank, scope_key in enumerate(chain):
                for template in self._data.get("templates", {}).get(scope_key, {}).values():
                    item = copy.deepcopy(template)
                    item["scope_rank"] = rank
                    result.append(item)
        return sorted(result, key=lambda item: (item["id"], item["scope_rank"]))

    @staticmethod
    def _validate_scope(scope_key: str) -> None:
        if not any(f":{kind}" in scope_key for kind in SCOPE_KINDS):
            raise ValueError("write_scope must be global, pack, server, or world")

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"version": 1, "templates": {}}
        data = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("version") != 1:
            raise ValueError("unsupported scoped knowledge schema")
        data.setdefault("templates", {})
        return data

    def _save(self) -> None:
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.path)
