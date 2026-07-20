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

from .access_policy import evaluate as evaluate_access, normalize_policy


DEFAULT_AGENT = "default"
DEFAULT_CONFIG_PATH = Path(__file__).parent.parent / ".local" / "config.json"
LEGACY_CONFIG_PATH = Path(__file__).parent.parent / "config.json"
CONFIG_VERSION = 2

LLM_INTEGER_FIELDS = (
    "context_window_tokens",
    "max_input_tokens",
    "max_output_tokens",
    "reserved_output_tokens",
    "max_request_bytes",
    "compression_trigger_tokens",
    "compression_target_tokens",
    "recent_messages_to_keep",
    "summary_max_output_tokens",
)


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
        "version": CONFIG_VERSION,
        "llm": {
            "default_agent": DEFAULT_AGENT,
                "agents": {
                    DEFAULT_AGENT: {
                    "provider_profile": "mimo-default",
                    "routing_mode": "manual",
                    "fallback_profiles": [],
                    "provider": preset["id"],
                    "base_url": preset["base_url"],
                    "model": preset["default_model"],
                    "api_key": "",
                    "temperature": 0.7,
                    "context_window_tokens": 200000,
                    "max_input_tokens": 180000,
                    "max_output_tokens": 16384,
                    "reserved_output_tokens": 16384,
                    "max_request_bytes": 4194304,
                    "compression_enabled": True,
                    "compression_trigger_tokens": 160000,
                    "compression_target_tokens": 120000,
                    "recent_messages_to_keep": 24,
                    "summary_model_agent": DEFAULT_AGENT,
                    "summary_max_output_tokens": 4096,
                    }
                },
                "provider_profiles": {
                    "mimo-default": {
                        "id": "mimo-default",
                        "name": "MiMo 默认",
                        "provider": preset["id"],
                        "base_url": preset["base_url"],
                        "model": preset["default_model"],
                        "api_key": "",
                        "enabled": True,
                    },
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
        "access": {
            "version": 1,
            "default_role": "player",
            "public_chat": True,
            "private_chat": True,
            "role_skills": {},
            "principals": [],
        },
        "patrol_radius": 8,
    }


class ConfigStore:
    """Thread-safe JSON-backed configuration store."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._migration_pending = False
        self._data = self._load()
        self._ensure_companion_identity()
        if self._migration_pending:
            self.save()

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
            agents = loaded.get("llm", {}).get("agents", {})
            if isinstance(agents, dict):
                for config in agents.values():
                    if not isinstance(config, dict):
                        continue
                    if "max_tokens" in config and "max_output_tokens" not in config:
                        config["max_output_tokens"] = config["max_tokens"]
                    if "max_tokens" in config:
                        config.pop("max_tokens")
                        self._migration_pending = True
            if "access" not in loaded and ("whitelist" in loaded or "listen_public" in loaded):
                legacy_principals = [
                    {
                        "id": f"legacy-name:{name}",
                        "name": str(name),
                        "role": "friend",
                        "skills": {"chat.reply": "allow"},
                    }
                    for name in loaded.get("whitelist", [])
                    if str(name).strip()
                ]
                loaded["access"] = {
                    "public_chat": bool(loaded.get("listen_public", True)),
                    "principals": legacy_principals,
                }
                self._migration_pending = True
            if loaded.get("version") != CONFIG_VERSION:
                loaded["version"] = CONFIG_VERSION
                self._migration_pending = True
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
            self._migration_pending = False

    def raw(self, redact: bool = True) -> dict[str, Any]:
        with self._lock:
            data = copy.deepcopy(self._data)
        return self._redact(data) if redact else data

    def get_access_policy(self) -> dict[str, Any]:
        with self._lock:
            return normalize_policy(self._data.get("access", {}))

    def set_access_policy(self, payload: dict[str, Any]) -> dict[str, Any]:
        policy = normalize_policy(payload)
        with self._lock:
            self._data["access"] = policy
            self.save()
            return copy.deepcopy(policy)

    def upsert_access_principal(self, principal: dict[str, Any]) -> dict[str, Any]:
        principal_id = str(principal.get("id", "")).strip()
        if not principal_id:
            raise ValueError("principal id must not be empty")
        with self._lock:
            policy = self.get_access_policy()
            existing = next((item for item in policy["principals"] if item["id"] == principal_id), None)
            if existing is None:
                policy["principals"].append(copy.deepcopy(principal))
            else:
                existing.update(copy.deepcopy(principal))
            policy = normalize_policy(policy)
            self._data["access"] = policy
            self.save()
            return copy.deepcopy(next(item for item in policy["principals"] if item["id"] == principal_id))

    def delete_access_principal(self, principal_id: str) -> bool:
        with self._lock:
            policy = self.get_access_policy()
            before = len(policy["principals"])
            policy["principals"] = [item for item in policy["principals"] if item["id"] != principal_id]
            if len(policy["principals"]) == before:
                return False
            self._data["access"] = normalize_policy(policy)
            self.save()
            return True

    def evaluate_access(self, requester: dict[str, Any], *, channel: str, skill: str,
                        server_id: str = "", body_id: str = "") -> dict[str, Any]:
        return evaluate_access(
            self.get_access_policy(), requester, channel=channel, skill=skill,
            server_id=server_id, body_id=body_id,
        )

    def list_provider_presets(self) -> list[dict[str, Any]]:
        return [copy.deepcopy(p) for p in PROVIDER_PRESETS.values()]

    def list_provider_profiles(self, redact: bool = True) -> list[dict[str, Any]]:
        with self._lock:
            profiles = list(copy.deepcopy(self._data.setdefault("llm", {}).setdefault("provider_profiles", {})).values())
        return [self._redact(profile) for profile in profiles] if redact else profiles

    def upsert_provider_profile(self, profile: dict[str, Any]) -> dict[str, Any]:
        profile_id = str(profile.get("id", "")).strip()
        if not profile_id:
            raise ValueError("provider profile id must not be empty")
        provider = str(profile.get("provider", "")).strip()
        if provider not in PROVIDER_PRESETS:
            raise ValueError(f"Unknown LLM provider: {provider}")
        with self._lock:
            profiles = self._data.setdefault("llm", {}).setdefault("provider_profiles", {})
            current = copy.deepcopy(profiles.get(profile_id, {
                "id": profile_id,
                "name": profile_id,
                "provider": provider,
                "base_url": PROVIDER_PRESETS[provider].get("base_url", ""),
                "model": PROVIDER_PRESETS[provider].get("default_model", ""),
                "api_key": "",
                "enabled": True,
            }))
            for key in ("name", "provider", "base_url", "model", "api_key", "enabled"):
                if key in profile and profile[key] is not None:
                    current[key] = profile[key]
            if current["provider"] not in PROVIDER_PRESETS:
                raise ValueError(f"Unknown LLM provider: {current['provider']}")
            current["id"] = profile_id
            current["name"] = str(current.get("name", profile_id)).strip() or profile_id
            current["base_url"] = str(current.get("base_url", "")).rstrip("/")
            current["model"] = str(current.get("model", "")).strip()
            if not isinstance(current.get("enabled", True), bool):
                raise ValueError("provider profile enabled must be a boolean")
            profiles[profile_id] = current
            self.save()
            return self._redact(copy.deepcopy(current))

    def delete_provider_profile(self, profile_id: str) -> bool:
        with self._lock:
            profiles = self._data.setdefault("llm", {}).setdefault("provider_profiles", {})
            if profile_id not in profiles:
                return False
            if any(config.get("provider_profile") == profile_id for config in self._data.get("llm", {}).get("agents", {}).values()):
                raise ValueError("provider profile is assigned to an agent")
            del profiles[profile_id]
            self.save()
            return True

    def get_agent_llm_config(self, agent: str | None = None, redact: bool = True) -> dict[str, Any]:
        agent_name = agent or self._data["llm"].get("default_agent", DEFAULT_AGENT)
        with self._lock:
            agents = self._data.setdefault("llm", {}).setdefault("agents", {})
            if agent_name not in agents:
                agents[agent_name] = copy.deepcopy(agents.get(DEFAULT_AGENT, _default_config()["llm"]["agents"][DEFAULT_AGENT]))
            config = copy.deepcopy(agents[agent_name])
            profile_id = config.get("provider_profile")
            profile = self._data.get("llm", {}).get("provider_profiles", {}).get(profile_id)
            if isinstance(profile, dict) and profile.get("enabled", True):
                for key in ("provider", "base_url", "model", "api_key"):
                    if profile.get(key) is not None:
                        config[key] = profile[key]
            config["fallback_configs"] = [
                copy.deepcopy(candidate)
                for candidate_id in config.get("fallback_profiles", [])
                if isinstance((candidate := self._data.get("llm", {}).get("provider_profiles", {}).get(candidate_id)), dict)
                and candidate.get("enabled", True)
            ]
        return self._redact(config) if redact else config

    def set_agent_llm_config(self, agent: str | None, payload: dict[str, Any]) -> dict[str, Any]:
        agent_name = agent or payload.get("agent") or DEFAULT_AGENT
        provider_id = payload.get("provider")
        with self._lock:
            agents = self._data.setdefault("llm", {}).setdefault("agents", {})
            current = copy.deepcopy(agents.get(agent_name) or agents.get(DEFAULT_AGENT) or _default_config()["llm"]["agents"][DEFAULT_AGENT])

            normalized_payload = dict(payload)
            if "max_tokens" in normalized_payload and "max_output_tokens" not in normalized_payload:
                normalized_payload["max_output_tokens"] = normalized_payload["max_tokens"]
            if "provider_profile" not in normalized_payload and any(
                key in normalized_payload for key in ("provider", "base_url", "model", "api_key")
            ):
                current.pop("provider_profile", None)

            if provider_id:
                preset = PROVIDER_PRESETS.get(provider_id)
                if not preset:
                    raise ValueError(f"Unknown LLM provider: {provider_id}")
                current["provider"] = provider_id
                if not payload.get("base_url") and preset.get("base_url"):
                    current["base_url"] = preset["base_url"]
                if not payload.get("model") and preset.get("default_model"):
                    current["model"] = preset["default_model"]

            for key in (
                "provider_profile", "routing_mode", "fallback_profiles",
                "base_url", "api_key", "model", "temperature",
                *LLM_INTEGER_FIELDS, "compression_enabled", "summary_model_agent",
            ):
                if key in normalized_payload and normalized_payload[key] is not None:
                    current[key] = normalized_payload[key]

            current.pop("max_tokens", None)

            profile_id = str(current.get("provider_profile", "")).strip()
            if profile_id:
                if profile_id not in self._data.setdefault("llm", {}).setdefault("provider_profiles", {}):
                    raise ValueError(f"Unknown provider profile: {profile_id}")
                current["provider_profile"] = profile_id
            routing_mode = current.get("routing_mode", "manual")
            if routing_mode not in {"manual", "priority", "scheduled"}:
                raise ValueError("routing_mode must be manual, priority, or scheduled")
            fallback_profiles = current.get("fallback_profiles", [])
            if not isinstance(fallback_profiles, list) or any(str(item) not in self._data["llm"]["provider_profiles"] for item in fallback_profiles):
                raise ValueError("fallback_profiles must reference configured provider profiles")
            current["fallback_profiles"] = [str(item) for item in fallback_profiles]

            raw_temperature = current.get("temperature", 0.7)
            try:
                if isinstance(raw_temperature, bool):
                    raise ValueError
                current["temperature"] = float(raw_temperature)
            except (TypeError, ValueError) as exc:
                raise ValueError("temperature must be numeric") from exc
            if not 0 <= current["temperature"] <= 2:
                raise ValueError("temperature must be between 0 and 2")

            for key in LLM_INTEGER_FIELDS:
                raw_value = current.get(key)
                if isinstance(raw_value, bool):
                    raise ValueError(f"{key} must be a positive integer")
                try:
                    value = int(raw_value)
                except (TypeError, ValueError) as exc:
                    raise ValueError(f"{key} must be a positive integer") from exc
                if value <= 0 or isinstance(raw_value, float) and not raw_value.is_integer():
                    raise ValueError(f"{key} must be a positive integer")
                current[key] = value

            compression_enabled = current.get("compression_enabled", True)
            if not isinstance(compression_enabled, bool):
                raise ValueError("compression_enabled must be a boolean")

            summary_agent = str(current.get("summary_model_agent", DEFAULT_AGENT)).strip()
            if not summary_agent:
                raise ValueError("summary_model_agent must not be empty")
            if summary_agent not in agents and summary_agent not in {DEFAULT_AGENT, agent_name}:
                raise ValueError("summary_model_agent must reference a configured agent")
            current["summary_model_agent"] = summary_agent

            if current["max_output_tokens"] > current["reserved_output_tokens"]:
                raise ValueError("max_output_tokens must not exceed reserved_output_tokens")
            if current["max_input_tokens"] + current["reserved_output_tokens"] > current["context_window_tokens"]:
                raise ValueError("max_input_tokens plus reserved_output_tokens must not exceed context_window_tokens")
            if current["compression_target_tokens"] >= current["compression_trigger_tokens"]:
                raise ValueError("compression_target_tokens must be less than compression_trigger_tokens")
            if current["compression_trigger_tokens"] > current["max_input_tokens"]:
                raise ValueError("compression_trigger_tokens must not exceed max_input_tokens")

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
