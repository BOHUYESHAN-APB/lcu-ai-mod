"""Identity and skill access policy primitives."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any


ROLES = ("unknown", "player", "friend", "master", "owner", "server_moderator", "operator", "server_admin", "system")
DECISIONS = ("allow", "deny")

SKILL_CLASSES = {
    "core.stop": "safety.stop",
    "core.send_chat": "chat.reply",
    "core.move_to": "task.movement",
    "core.look_at": "task.movement",
    "core.look_at_entity": "task.movement",
    "core.jump": "task.movement",
    "core.follow_player": "task.movement",
    "core.mine_block": "task.resource",
    "core.mine_block_at": "task.resource",
    "core.collect_blocks": "task.resource",
    "core.craft_item": "task.resource",
    "core.get_inventory": "observe.basic",
    "core.get_state": "observe.basic",
    "core.eat": "task.inventory",
    "core.use_item": "task.inventory",
    "core.attack": "task.combat",
    "core.attack_entity": "task.combat",
    "core.place_block": "task.world",
    "core.build": "task.world",
    "core.use_on": "task.world",
    "core.use_on_entity": "task.world",
    "core.interact_block_at": "task.world",
}

DEFAULT_ROLE_SKILLS: dict[str, dict[str, str]] = {
    "unknown": {},
    "player": {"chat.reply": "allow"},
    "friend": {"chat.reply": "allow", "observe.basic": "allow"},
    "master": {"chat.reply": "allow", "observe.basic": "allow", "task.movement": "allow", "task.resource": "allow"},
    "owner": {"chat.reply": "allow", "observe.basic": "allow", "task.movement": "allow", "task.resource": "allow", "task.inventory": "allow", "task.combat": "allow"},
    "server_moderator": {"chat.reply": "allow", "observe.basic": "allow", "task.moderation": "allow", "safety.stop": "allow"},
    "operator": {"chat.reply": "allow", "observe.basic": "allow", "safety.stop": "allow", "task.movement": "allow", "task.resource": "allow", "task.inventory": "allow", "task.combat": "allow", "task.world": "allow", "admin.body": "allow"},
    "server_admin": {"*": "allow"},
    "system": {"safety.stop": "allow", "chat.reply": "allow"},
}


def default_access_policy() -> dict[str, Any]:
    return {"version": 1, "default_role": "player", "public_chat": True, "private_chat": True, "role_skills": copy.deepcopy(DEFAULT_ROLE_SKILLS), "principals": []}


def classify_skill(skill_id: str) -> str:
    return SKILL_CLASSES.get(str(skill_id), "task.unknown")


def _merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def normalize_policy(value: Any) -> dict[str, Any]:
    policy = _merge(default_access_policy(), value if isinstance(value, dict) else {})
    if policy["default_role"] not in ROLES:
        raise ValueError("default_role must be a known role")
    if not isinstance(policy["public_chat"], bool):
        raise ValueError("public_chat must be a boolean")
    if not isinstance(policy["private_chat"], bool):
        raise ValueError("private_chat must be a boolean")
    principals = policy.get("principals", [])
    if not isinstance(principals, list):
        raise ValueError("principals must be a list")
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in principals:
        if not isinstance(item, dict):
            raise ValueError("each principal must be an object")
        principal = copy.deepcopy(item)
        principal_id = str(principal.get("id", "")).strip()
        role = str(principal.get("role", policy["default_role"])).strip()
        if not principal_id or principal_id in seen:
            raise ValueError("principal ids must be unique and non-empty")
        if role not in ROLES:
            raise ValueError(f"unknown principal role: {role}")
        skills = principal.get("skills", {})
        if not isinstance(skills, dict):
            raise ValueError("principal skills must be an object")
        principal.update({
            "id": principal_id,
            "role": role,
            "enabled": bool(principal.get("enabled", True)),
            "uuid": str(principal.get("uuid", "")).strip(),
            "name": str(principal.get("name", "")).strip(),
            "server_ids": [str(value).strip() for value in principal.get("server_ids", []) if str(value).strip()],
            "body_ids": [str(value).strip() for value in principal.get("body_ids", []) if str(value).strip()],
            "skills": {str(key): str(value) for key, value in skills.items() if str(key).strip() and str(value) in DECISIONS},
        })
        normalized.append(principal)
        seen.add(principal_id)
    policy["principals"] = normalized
    return policy


def load_policy(path: str | Path) -> dict[str, Any]:
    config_path = Path(path)
    if not config_path.exists():
        return default_access_policy()
    try:
        loaded = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        loaded = {}
    return normalize_policy(loaded.get("access", {}) if isinstance(loaded, dict) else {})


def _matches(principal: dict[str, Any], requester: dict[str, Any], server_id: str, body_id: str) -> bool:
    if not principal.get("enabled", True):
        return False
    principal_uuid = str(principal.get("uuid", "")).strip()
    requester_uuid = str(requester.get("uuid", "")).strip()
    if principal_uuid:
        identity_matches = bool(requester_uuid) and requester_uuid == principal_uuid
    else:
        requester_identifiers = {str(requester.get(key, "")).strip() for key in ("id", "name")} - {""}
        configured_identifiers = {principal.get("id", ""), principal.get("name", "")} - {""}
        identity_matches = bool(requester_identifiers.intersection(configured_identifiers))
    if not identity_matches:
        return False
    return (not principal["server_ids"] or server_id in principal["server_ids"]) and (not principal["body_ids"] or body_id in principal["body_ids"])


def evaluate(policy: dict[str, Any], requester: dict[str, Any], *, channel: str, skill: str, server_id: str = "", body_id: str = "") -> dict[str, Any]:
    normalized = normalize_policy(policy)
    principal = next((item for item in normalized["principals"] if _matches(item, requester, server_id, body_id)), None)
    role = principal["role"] if principal else normalized["default_role"]
    channel_gate = {"chat.public": "public_chat", "chat.private": "private_chat"}.get(channel)
    if channel_gate and skill == "chat.reply" and not principal:
        decision = "allow" if normalized[channel_gate] else "deny"
        reason = channel_gate
    else:
        skills = dict(normalized["role_skills"].get(role, {}))
        if principal:
            skills.update(principal.get("skills", {}))
        decision, reason = skills.get(skill, skills.get("*", "deny")), "principal" if principal else "role_default"
    return {"allowed": decision == "allow", "decision": decision, "reason": reason, "role": role, "principal_id": principal.get("id") if principal else None, "channel": channel, "skill": skill, "server_id": server_id, "body_id": body_id}
