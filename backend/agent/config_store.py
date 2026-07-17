"""Persistent runtime configuration for LCU Mod backend.

This module keeps provider presets, per-agent LLM settings, persona settings,
and external integration context in one JSON-backed store.  The store is
deliberately small and dependency-free so it can be used by FastAPI routes,
tests, and future SDK entry points without starting the Minecraft wire client.
"""

from __future__ import annotations

import copy
import json
import threading
import time
import uuid
from pathlib import Path
from typing import Any


DEFAULT_AGENT = "default"
DEFAULT_CONFIG_PATH = Path(__file__).parent.parent / ".local" / "config.json"
LEGACY_CONFIG_PATH = Path(__file__).parent.parent / "config.json"


PROVIDER_PRESETS: dict[str, dict[str, Any]] = {
    "openai": {
        "id": "openai",
        "name": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "models_path": "/models",
        "default_model": "gpt-4o-mini",
        "api_key_required": True,
        "openai_compatible": True,
    },
    "mimo": {
        "id": "mimo",
        "name": "Xiaomi MiMo / TokenPlan",
        "base_url": "https://token-plan-cn.xiaomimimo.com/v1",
        "models_path": "/models",
        "default_model": "mimo-v2.5",
        "recommended_models": [
            "mimo-v2.5",
            "mimo-v2.5-pro",
            "mimo-v2-pro",
            "mimo-v2-omni",
            "mimo-v2-flash",
        ],
        "api_key_required": True,
        "openai_compatible": True,
    },
    "deepseek": {
        "id": "deepseek",
        "name": "DeepSeek",
        "base_url": "https://api.deepseek.com/v1",
        "models_path": "/models",
        "default_model": "deepseek-chat",
        "api_key_required": True,
        "openai_compatible": True,
    },
    "openrouter": {
        "id": "openrouter",
        "name": "OpenRouter",
        "base_url": "https://openrouter.ai/api/v1",
        "models_path": "/models",
        "default_model": "openai/gpt-4o-mini",
        "api_key_required": True,
        "openai_compatible": True,
    },
    "qwen": {
        "id": "qwen",
        "name": "Qwen / 阿里百炼",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "models_path": "/models",
        "default_model": "qwen-plus",
        "api_key_required": True,
        "openai_compatible": True,
    },
    "kimi": {
        "id": "kimi",
        "name": "Moonshot / Kimi",
        "base_url": "https://api.moonshot.cn/v1",
        "models_path": "/models",
        "default_model": "moonshot-v1-8k",
        "api_key_required": True,
        "openai_compatible": True,
    },
    "siliconflow": {
        "id": "siliconflow",
        "name": "SiliconFlow / 硅基流动",
        "base_url": "https://api.siliconflow.cn/v1",
        "models_path": "/models",
        "default_model": "deepseek-ai/DeepSeek-V3",
        "api_key_required": True,
        "openai_compatible": True,
    },
    "ollama": {
        "id": "ollama",
        "name": "Ollama 本地",
        "base_url": "http://127.0.0.1:11434/v1",
        "models_path": "/models",
        "default_model": "llama3.1",
        "api_key_required": False,
        "openai_compatible": True,
    },
    "custom": {
        "id": "custom",
        "name": "自定义 OpenAI-Compatible",
        "base_url": "",
        "models_path": "/models",
        "default_model": "",
        "api_key_required": False,
        "openai_compatible": True,
    },
}


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _default_config() -> dict[str, Any]:
    preset = PROVIDER_PRESETS["mimo"]
    return {
        "version": 1,
        "llm": {
            "default_agent": DEFAULT_AGENT,
            "agents": {
                DEFAULT_AGENT: {
                    "provider": preset["id"],
                    "base_url": preset["base_url"],
                    "model": preset["default_model"],
                    "api_key": "",
                    "temperature": 0.7,
                    "max_tokens": 2048,
                }
            },
        },
        "persona": {
            "name": "AI",
            "wake_names": ["AI", "小A"],
            "personality": "友好、自然、像真人玩家",
            "speaking_style": "口语化、简短，不暴露 AI 身份",
            "external_context": {},
        },
        "integration": {
            "enabled": True,
            "allowed_origins": ["http://127.0.0.1", "http://localhost"],
            "updated_at": None,
        },
        "companion": {
            "id": "",
            "persistence": {
                "scope": "global",
                "server_id": "default",
                "world_id": "default",
            },
        },
        "whitelist": [],
        "listen_public": True,
        "patrol_radius": 8,
    }


class ConfigStore:
    """Thread-safe JSON-backed configuration store."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._data = self._load()
        self._ensure_companion_identity()

    def _ensure_companion_identity(self) -> None:
        companion = self._data.setdefault("companion", {})
        if not companion.get("id"):
            companion["id"] = str(uuid.uuid4())
            companion.setdefault("persistence", _default_config()["companion"]["persistence"])
            self.save()

    def _load(self) -> dict[str, Any]:
        defaults = _default_config()
        source_path = self.path
        if not source_path.exists() and self.path == DEFAULT_CONFIG_PATH and LEGACY_CONFIG_PATH.exists():
            source_path = LEGACY_CONFIG_PATH
        if not source_path.exists():
            return defaults
        try:
            loaded = json.loads(source_path.read_text(encoding="utf-8"))
            if not isinstance(loaded, dict):
                return defaults
            return _deep_merge(defaults, loaded)
        except Exception:
            return defaults

    def save(self) -> None:
        with self._lock:
            temporary = self.path.with_suffix(self.path.suffix + ".tmp")
            temporary.write_text(
                json.dumps(self._data, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            temporary.replace(self.path)

    def raw(self, redact: bool = True) -> dict[str, Any]:
        with self._lock:
            data = copy.deepcopy(self._data)
        return self._redact(data) if redact else data

    def list_provider_presets(self) -> list[dict[str, Any]]:
        return [copy.deepcopy(p) for p in PROVIDER_PRESETS.values()]

    def get_agent_llm_config(self, agent: str | None = None, redact: bool = True) -> dict[str, Any]:
        agent_name = agent or self._data["llm"].get("default_agent", DEFAULT_AGENT)
        with self._lock:
            agents = self._data.setdefault("llm", {}).setdefault("agents", {})
            if agent_name not in agents:
                agents[agent_name] = copy.deepcopy(agents.get(DEFAULT_AGENT, _default_config()["llm"]["agents"][DEFAULT_AGENT]))
            config = copy.deepcopy(agents[agent_name])
        return self._redact(config) if redact else config

    def set_agent_llm_config(self, agent: str | None, payload: dict[str, Any]) -> dict[str, Any]:
        agent_name = agent or payload.get("agent") or DEFAULT_AGENT
        provider_id = payload.get("provider")
        with self._lock:
            agents = self._data.setdefault("llm", {}).setdefault("agents", {})
            current = copy.deepcopy(agents.get(agent_name) or agents.get(DEFAULT_AGENT) or _default_config()["llm"]["agents"][DEFAULT_AGENT])

            if provider_id:
                preset = PROVIDER_PRESETS.get(provider_id)
                if not preset:
                    raise ValueError(f"Unknown LLM provider: {provider_id}")
                current["provider"] = provider_id
                if not payload.get("base_url") and preset.get("base_url"):
                    current["base_url"] = preset["base_url"]
                if not payload.get("model") and preset.get("default_model"):
                    current["model"] = preset["default_model"]

            for key in ("base_url", "api_key", "model", "temperature", "max_tokens"):
                if key in payload and payload[key] is not None:
                    current[key] = payload[key]

            raw_temperature = current.get("temperature", 0.7)
            raw_max_tokens = current.get("max_tokens", 2048)
            try:
                if isinstance(raw_temperature, bool):
                    raise ValueError
                current["temperature"] = float(raw_temperature)
                if isinstance(raw_max_tokens, bool):
                    raise ValueError
                if isinstance(raw_max_tokens, int):
                    current["max_tokens"] = raw_max_tokens
                elif isinstance(raw_max_tokens, str) and raw_max_tokens.strip().isdigit():
                    current["max_tokens"] = int(raw_max_tokens.strip())
                else:
                    raise ValueError
            except (TypeError, ValueError) as exc:
                raise ValueError("temperature must be numeric and max_tokens must be an integer") from exc
            if not 0 <= current["temperature"] <= 2:
                raise ValueError("temperature must be between 0 and 2")
            if current["max_tokens"] <= 0:
                raise ValueError("max_tokens must be greater than 0")

            if current.get("base_url"):
                current["base_url"] = str(current["base_url"]).rstrip("/")

            agents[agent_name] = current
            self._data["llm"]["default_agent"] = self._data["llm"].get("default_agent") or DEFAULT_AGENT
            self.save()
            result = copy.deepcopy(current)
        return self._redact(result)

    def get_persona(self) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._data.setdefault("persona", _default_config()["persona"]))

    def get_companion_config(self) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._data["companion"])

    def set_companion_config(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            companion = copy.deepcopy(self._data["companion"])
            requested_id = payload.get("companion_id", payload.get("id"))
            if requested_id is not None:
                companion_id = str(requested_id).strip()
                if not companion_id:
                    raise ValueError("companion id must not be empty")
                companion["id"] = companion_id
            persistence = companion.setdefault("persistence", {})
            update = payload.get("persistence", payload)
            for key in ("scope", "server_id", "world_id"):
                if key in update:
                    persistence[key] = str(update[key]).strip()
            if persistence.get("scope", "global") not in {"global", "server", "world"}:
                raise ValueError("scope must be global, server, or world")
            self._data["companion"] = companion
            self.save()
            return copy.deepcopy(companion)

    def set_persona(self, payload: dict[str, Any]) -> dict[str, Any]:
        allowed = {"name", "wake_names", "personality", "speaking_style", "external_context"}
        with self._lock:
            persona = self._data.setdefault("persona", _default_config()["persona"])
            for key in allowed:
                if key in payload:
                    persona[key] = payload[key]
            self.save()
            return copy.deepcopy(persona)

    def set_integration_context(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            persona = self._data.setdefault("persona", _default_config()["persona"])
            persona["external_context"] = payload.get("external_context", payload)
            integration = self._data.setdefault("integration", _default_config()["integration"])
            integration["updated_at"] = time.time()
            self.save()
            return {"persona": copy.deepcopy(persona), "integration": copy.deepcopy(integration)}

    def set_app_config(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            for key in ("whitelist", "listen_public", "patrol_radius"):
                if key in payload:
                    self._data[key] = payload[key]
            self.save()
            return self.raw(redact=True)

    @staticmethod
    def _redact(data: dict[str, Any]) -> dict[str, Any]:
        redacted = copy.deepcopy(data)

        def walk(node: Any) -> None:
            if isinstance(node, dict):
                for key, value in list(node.items()):
                    if key == "api_key" and value:
                        node[key] = "***"
                    else:
                        walk(value)
            elif isinstance(node, list):
                for item in node:
                    walk(item)

        walk(redacted)
        return redacted
