"""Strict, server-plugin-friendly command families for the body client."""

from __future__ import annotations

import re
from typing import Any

DEFAULT_FAMILIES = ("tp", "home", "tpa", "tpaccept", "tpdeny")
_TOKEN = re.compile(r"^[A-Za-z0-9_:.\-]+$")


def normalize_policy(value: dict[str, Any] | None = None) -> dict[str, Any]:
    raw = value if isinstance(value, dict) else {}
    families = raw.get("allowed_families", list(DEFAULT_FAMILIES))
    if not isinstance(families, list):
        raise ValueError("allowed_families must be a list")
    normalized = []
    for family in families:
        item = str(family).strip().casefold()
        if item and item not in normalized:
            normalized.append(item)
    return {
        "version": 1,
        "enabled": bool(raw.get("enabled", True)),
        "allowed_families": normalized,
        "max_per_minute": max(1, min(30, int(raw.get("max_per_minute", 6)))),
        "require_exact_tokens": True,
    }


def evaluate(command: str, policy: dict[str, Any] | None = None) -> dict[str, Any]:
    current = normalize_policy(policy)
    text = str(command or "").strip()
    if not current["enabled"]:
        raise ValueError("server command automation is disabled")
    if not text.startswith("/") or text.startswith("//") or any(ord(char) < 32 for char in text):
        raise ValueError("command must be a single slash command")
    tokens = text[1:].split()
    if not tokens:
        raise ValueError("command is empty")
    family = tokens[0].casefold().split(":")[-1]
    if family not in current["allowed_families"]:
        raise ValueError(f"command family is not allowed: {family}")
    if family in {"tp", "tpa", "tpaccept", "tpdeny"} and any(token.startswith("@") for token in tokens[1:]):
        raise ValueError("selectors are not allowed")
    if not all(_TOKEN.fullmatch(token) for token in tokens):
        raise ValueError("command contains an invalid token")
    if family == "home" and len(tokens) > 2:
        raise ValueError("home accepts at most one home name")
    if family in {"tpa", "tpaccept", "tpdeny"} and len(tokens) > 2:
        raise ValueError(f"{family} accepts at most one player")
    if family == "tp" and len(tokens) not in {2, 3}:
        raise ValueError("tp requires one or two player arguments")
    return {"family": family, "command": "/" + " ".join(tokens), "tokens": tokens}
