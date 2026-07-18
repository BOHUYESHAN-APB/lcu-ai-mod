"""
LLM Service — OpenAI-compatible API integration.
Supports any OpenAI-compatible provider (OpenAI, MiMo, DeepSeek, etc.)

Features:
- OpenAI-compatible chat completions
- Custom base URL support
- Streaming support
- Token tracking per session
- Multiple model support
"""

import json
import logging
import math
import threading
import time
from typing import Any, Generator, Optional

import httpx

logger = logging.getLogger("llm_service")

# No default provider - user must configure
DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-4o-mini"
DEFAULT_LIMITS = {
    "context_window_tokens": 200000,
    "max_input_tokens": 180000,
    "max_output_tokens": 16384,
    "reserved_output_tokens": 16384,
    "max_request_bytes": 4194304,
    "compression_enabled": True,
    "compression_trigger_tokens": 160000,
    "compression_target_tokens": 120000,
    "recent_messages_to_keep": 24,
    "summary_model_agent": "default",
    "summary_max_output_tokens": 4096,
}


class LLMRequestRejected(ValueError):
    """A request rejected locally before contacting the model provider."""

    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.details = details or {}


class LLMService:
    """
    Lightweight LLM API client.
    Supports any OpenAI-compatible provider.
    """

    def __init__(self, base_url: str = DEFAULT_BASE_URL):
        self.base_url = base_url
        self.api_key: Optional[str] = None
        self.model = DEFAULT_MODEL
        self._client = httpx.Client(timeout=120)
        self._agent_configs: dict[str, dict[str, Any]] = {}

        # Token tracking
        self.total_prompt_tokens = 0
        self.total_completion_tokens = 0
        self._usage_history: list[dict] = []
        self._usage_lock = threading.RLock()
        self._request_count = 0
        self._successful_requests = 0
        self._rejected_requests = 0
        self._failed_requests = 0
        self._usage_by_agent: dict[str, dict[str, int]] = {}
        self._latest_rejection: dict[str, Any] | None = None
        self._latest_compression: dict[str, Any] | None = None

    def set_api_key(self, key: str):
        self.api_key = key
        logger.info("[LLM] API key configured")
        return self

    def set_model(self, model: str):
        self.model = model
        logger.info("[LLM] Model set to: %s", model)
        return self

    def set_base_url(self, url: str):
        self.base_url = url.rstrip('/')
        logger.info("[LLM] Base URL set to: %s", self.base_url)
        return self

    def configure(self, config: dict[str, Any]):
        """Apply a single LLM configuration to the default client."""
        if config.get("base_url"):
            self.set_base_url(config["base_url"])
        if config.get("api_key") and config.get("api_key") != "***":
            self.set_api_key(config["api_key"])
        if config.get("model"):
            self.set_model(config["model"])
        return self

    def set_agent_config(self, agent: str, config: dict[str, Any]):
        """Configure a named agent without changing other agent settings."""
        current = self._agent_configs.get(agent, {})
        merged = {**current, **{k: v for k, v in config.items() if v is not None}}
        if "max_tokens" in merged and "max_output_tokens" not in merged:
            merged["max_output_tokens"] = merged["max_tokens"]
        merged.pop("max_tokens", None)
        if merged.get("base_url"):
            merged["base_url"] = str(merged["base_url"]).rstrip("/")
        self._agent_configs[agent] = merged
        if agent in {"default", "session"}:
            self.configure(merged)
        return self

    def get_config(self) -> dict:
        """Get current LLM configuration."""
        return {
            "configured": self.is_configured("default"),
            "base_url": self.base_url,
            "model": self.model,
            "agents": {k: self._redact_config(v) for k, v in self._agent_configs.items()},
        }

    # ── Chat ──

    def chat(self, messages: list[dict], agent: str | None = None, **kwargs) -> dict:
        """Send a chat completion request. Returns parsed response."""
        config = self._resolve_config(agent)
        if not self.is_configured(agent):
            self._reject(agent or "default", config, "not_configured", "model provider is not configured")
        data, request_meta = self._prepare_request(messages, agent, config, stream=False, **kwargs)
        try:
            response = self._post("chat/completions", data, config=config)
            result = self._parse_response(response)
        except Exception as exc:
            self._record_request(request_meta, "failed", error=str(exc))
            raise
        self._track_usage(response, request_meta)
        return result

    def chat_stream(self, messages: list[dict], agent: str | None = None, **kwargs) -> Generator[str, None, None]:
        """Stream a chat completion. Yields content chunks."""
        config = self._resolve_config(agent)
        if not self.is_configured(agent):
            self._reject(agent or "default", config, "not_configured", "model provider is not configured")
        data, request_meta = self._prepare_request(messages, agent, config, stream=True, **kwargs)
        url = f"{config['base_url']}/chat/completions"
        headers = self._headers(config)
        final_usage = {}
        completed = False

        try:
            with self._client.stream("POST", url, json=data, headers=headers) as resp:
                resp.raise_for_status()
                for line in resp.iter_lines():
                    if not line.startswith("data: "):
                        continue
                    chunk_str = line[6:]
                    if chunk_str == "[DONE]":
                        completed = True
                        break
                    try:
                        chunk = json.loads(chunk_str)
                        choices = chunk.get("choices") or []
                        delta = choices[0].get("delta", {}) if choices else {}
                        content = delta.get("content", "")
                        if content:
                            yield content
                        if "usage" in chunk:
                            final_usage = chunk["usage"]
                    except json.JSONDecodeError:
                        continue
            completed = True
        except Exception as exc:
            self._record_request(request_meta, "failed", error=str(exc))
            raise
        finally:
            if completed:
                self._track_usage({"usage": final_usage}, request_meta)

    # ── System Prompt ──

    def build_system_prompt(self, context: dict, commands_docs: str = "") -> str:
        """Build a system prompt from context and command docs."""
        persona = context.get("persona", {}) if isinstance(context, dict) else {}
        persona_name = persona.get("name", "AI")
        personality = persona.get("personality", "友好、自然")
        speaking_style = persona.get("speaking_style", "口语化、简短")
        external_context = persona.get("external_context", {})
        parts = [
            f"你是 {persona_name}，一个正在玩 Minecraft 的玩家，和服务器里的其他人一起玩。",
            f"你的人设性格是：{personality}。",
            f"你的说话风格是：{speaking_style}。",
            "你不是 AI，不要暴露自己是 AI。说话要自然、随意，像真人玩家一样。",
            '可以用"哈哈"、"好的"、"来了"、"等下"等口语。回复要简短，不要长篇大论。',
            "",
        ]
        if external_context:
            parts.append(f"上游系统注入的人设上下文：{external_context}")
            parts.append("")
        if commands_docs:
            parts.append(commands_docs)
            parts.append("")
        if context.get("action_insights"):
            parts.append(f"Action history: {context['action_insights']}")
            parts.append("")
        if context.get("interaction_summary"):
            parts.append("Recent conversation:")
            parts.append(context["interaction_summary"])
            parts.append("")
        return "\n".join(parts).strip()

    # ── Internal ──

    def fetch_models(self, agent: str | None = None, base_url: str | None = None,
                     api_key: str | None = None) -> list[str]:
        """Fetch model ids from an OpenAI-compatible /models endpoint."""
        config = self._resolve_config(agent)
        resolved_base_url = (base_url or config["base_url"]).rstrip("/")
        resolved_key = api_key if api_key is not None else config.get("api_key")
        headers = self._headers({"api_key": resolved_key})
        resp = self._client.get(f"{resolved_base_url}/models", headers=headers)
        resp.raise_for_status()
        payload = resp.json()
        data = payload.get("data", payload.get("models", []))
        models: list[str] = []
        for item in data:
            if isinstance(item, str):
                models.append(item)
            elif isinstance(item, dict):
                model_id = item.get("id") or item.get("name") or item.get("model")
                if model_id:
                    models.append(str(model_id))
        return models

    def _build_payload(self, messages: list[dict], stream: bool = False,
                         config: dict[str, Any] | None = None, **kwargs) -> dict:
        resolved = config or self._resolve_config(None)
        max_output_tokens = kwargs.get(
            "max_output_tokens",
            kwargs.get("max_tokens", resolved.get("max_output_tokens", 2048)),
        )
        return {
            "model": kwargs.get("model", resolved["model"]),
            "messages": messages,
            "temperature": kwargs.get("temperature", resolved.get("temperature", 0.7)),
            "max_tokens": max_output_tokens,
            "stream": stream,
        }

    def get_agent_config(self, agent: str | None = None, redact: bool = True) -> dict[str, Any]:
        config = self._resolve_config(agent)
        return self._redact_config(config) if redact else config

    def _prepare_request(self, messages: list[dict], agent: str | None,
                         config: dict[str, Any], stream: bool, **kwargs) -> tuple[dict, dict[str, Any]]:
        agent_name = agent or "default"
        if not isinstance(messages, list) or not messages:
            self._reject(agent_name, config, "invalid_messages", "messages must be a non-empty list")
        if not all(isinstance(message, dict) for message in messages):
            self._reject(agent_name, config, "invalid_messages", "every message must be an object")

        requested_output = kwargs.get(
            "max_output_tokens",
            kwargs.get("max_tokens", config["max_output_tokens"]),
        )
        if isinstance(requested_output, bool) or not isinstance(requested_output, int) or requested_output <= 0:
            self._reject(agent_name, config, "invalid_output_limit", "max output tokens must be a positive integer")
        if requested_output > config["max_output_tokens"]:
            self._reject(
                agent_name, config, "output_limit_exceeded",
                "requested output exceeds the configured maximum",
                {"requested": requested_output, "maximum": config["max_output_tokens"]},
            )

        working_messages = [dict(message) for message in messages]
        estimated_before = self._estimate_messages(working_messages)
        available_input = min(
            config["max_input_tokens"],
            config["context_window_tokens"] - config["reserved_output_tokens"],
        )
        compression: dict[str, Any] | None = None
        if config["compression_enabled"] and (
            estimated_before > config["compression_trigger_tokens"] or estimated_before > available_input
        ):
            working_messages, compression = self._compact_messages(
                working_messages,
                min(config["compression_target_tokens"], available_input),
                config["recent_messages_to_keep"],
            )

        estimated_after = self._estimate_messages(working_messages)
        if estimated_after > available_input:
            self._reject(
                agent_name, config, "input_limit_exceeded",
                "request input exceeds the configured context budget",
                {
                    "estimated_input_tokens": estimated_after,
                    "max_input_tokens": available_input,
                    "compression_enabled": config["compression_enabled"],
                },
                compression,
            )

        wire_messages = [self._wire_message(message) for message in working_messages]
        payload = self._build_payload(
            wire_messages,
            stream=stream,
            config=config,
            **{**kwargs, "max_output_tokens": requested_output},
        )
        request_bytes = self._payload_bytes(payload)
        if request_bytes > config["max_request_bytes"] and config["compression_enabled"]:
            working_messages, byte_compression = self._compact_messages(
                working_messages,
                min(config["compression_target_tokens"], available_input),
                config["recent_messages_to_keep"],
                target_bytes=max(1, config["max_request_bytes"] - 512),
            )
            compression = self._merge_compression(compression, byte_compression)
            estimated_after = self._estimate_messages(working_messages)
            wire_messages = [self._wire_message(message) for message in working_messages]
            payload = self._build_payload(
                wire_messages,
                stream=stream,
                config=config,
                **{**kwargs, "max_output_tokens": requested_output},
            )
            request_bytes = self._payload_bytes(payload)

        if request_bytes > config["max_request_bytes"]:
            self._reject(
                agent_name, config, "request_bytes_exceeded",
                "serialized request exceeds the configured byte limit",
                {"request_bytes": request_bytes, "max_request_bytes": config["max_request_bytes"]},
                compression,
            )

        now = time.time()
        request_meta = {
            "time": now,
            "agent": agent_name,
            "model": payload["model"],
            "stream": stream,
            "estimated_input_tokens": estimated_after,
            "estimated_input_tokens_before_compression": estimated_before,
            "reserved_output_tokens": config["reserved_output_tokens"],
            "max_output_tokens": requested_output,
            "request_bytes": request_bytes,
            "estimate_approximate": True,
            "compression": compression,
        }
        if compression:
            with self._usage_lock:
                self._latest_compression = {**compression, "time": now, "agent": agent_name}
        return payload, request_meta

    def _compact_messages(self, messages: list[dict], target_tokens: int,
                          recent_to_keep: int, target_bytes: int | None = None) -> tuple[list[dict], dict[str, Any]]:
        working = [dict(message) for message in messages]
        protected = {
            index for index, message in enumerate(working)
            if message.get("role") == "system"
            or message.get("required") is True
            or message.get("priority") == "required"
        }
        protected.update(range(max(0, len(working) - recent_to_keep), len(working)))
        removed = 0

        def over_target() -> bool:
            if self._estimate_messages(working) > target_tokens:
                return True
            if target_bytes is not None and self._messages_bytes(working) > target_bytes:
                return True
            return False

        original_indices = list(range(len(working)))
        while over_target():
            removable_position = next(
                (position for position, original_index in enumerate(original_indices) if original_index not in protected),
                None,
            )
            if removable_position is None:
                break
            working.pop(removable_position)
            original_indices.pop(removable_position)
            removed += 1

        return working, {
            "strategy": "drop_oldest_optional",
            "removed_messages": removed,
            "remaining_messages": len(working),
            "degraded": removed > 0,
            "target_tokens": target_tokens,
        }

    @staticmethod
    def _wire_message(message: dict[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in message.items() if key not in {"required", "priority"}}

    @staticmethod
    def _estimate_messages(messages: list[dict]) -> int:
        estimate = 2
        for message in messages:
            content = message.get("content", "")
            if not isinstance(content, str):
                content = json.dumps(content, ensure_ascii=False, separators=(",", ":"))
            estimate += 8 + math.ceil(len(content.encode("utf-8")) / 3)
            estimate += math.ceil(len(str(message.get("role", "")).encode("utf-8")) / 3)
        return estimate

    @staticmethod
    def _messages_bytes(messages: list[dict]) -> int:
        return len(json.dumps(messages, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))

    @staticmethod
    def _payload_bytes(payload: dict[str, Any]) -> int:
        return len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))

    @staticmethod
    def _merge_compression(first: dict[str, Any] | None,
                           second: dict[str, Any] | None) -> dict[str, Any] | None:
        if not first:
            return second
        if not second:
            return first
        return {
            **second,
            "removed_messages": first.get("removed_messages", 0) + second.get("removed_messages", 0),
            "degraded": first.get("degraded", False) or second.get("degraded", False),
        }

    def _reject(self, agent: str, config: dict[str, Any], code: str, message: str,
                details: dict[str, Any] | None = None,
                compression: dict[str, Any] | None = None) -> None:
        rejection = {
            "time": time.time(),
            "agent": agent,
            "model": config.get("model", self.model),
            "outcome": "rejected",
            "code": code,
            "error": message,
            "details": details or {},
            "compression": compression,
        }
        with self._usage_lock:
            self._latest_rejection = dict(rejection)
        self._record_request(rejection, "rejected", error=message)
        raise LLMRequestRejected(code, message, details)

    def _post(self, endpoint: str, data: dict, config: dict[str, Any] | None = None) -> dict:
        resolved = config or self._resolve_config(None)
        url = f"{resolved['base_url']}/{endpoint}"
        headers = self._headers(resolved)
        try:
            resp = self._client.post(url, json=data, headers=headers)
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as e:
            logger.error("[LLM] HTTP %d: %s", e.response.status_code, e.response.text[:200])
            raise
        except Exception as e:
            logger.error("[LLM] Request failed: %s", e)
            raise

    def _headers(self, config: dict[str, Any] | None = None) -> dict:
        headers = {"Content-Type": "application/json"}
        key = (config or {}).get("api_key") if config is not None else self.api_key
        if key:
            headers["Authorization"] = f"Bearer {key}"
        return headers

    def _resolve_config(self, agent: str | None) -> dict[str, Any]:
        config = dict(self._agent_configs.get("default", {}))
        if agent and agent in self._agent_configs:
            config.update(self._agent_configs[agent])
        resolved = {
            "provider": config.get("provider", "openai"),
            "base_url": str(config.get("base_url") or self.base_url).rstrip("/"),
            "model": config.get("model") or self.model,
            "api_key": config.get("api_key", self.api_key),
            "temperature": config.get("temperature", 0.7),
        }
        for key, default in DEFAULT_LIMITS.items():
            resolved[key] = config.get(key, default)
        if "max_output_tokens" not in config and "max_tokens" in config:
            resolved["max_output_tokens"] = config["max_tokens"]
        return resolved

    def is_configured(self, agent: str | None = None) -> bool:
        config = self._resolve_config(agent)
        provider = str(config.get("provider", "")).lower()
        keyless = provider in {"ollama", "custom"}
        return bool(config.get("base_url") and config.get("model") and (config.get("api_key") or keyless))

    @staticmethod
    def _redact_config(config: dict[str, Any]) -> dict[str, Any]:
        redacted = dict(config)
        if redacted.get("api_key"):
            redacted["api_key"] = "***"
        return redacted

    def _parse_response(self, response: dict) -> dict:
        try:
            choice = response["choices"][0]
            return {
                "role": choice.get("role", "assistant"),
                "content": choice.get("message", {}).get("content", ""),
                "finish_reason": choice.get("finish_reason", ""),
            }
        except (KeyError, IndexError) as e:
            logger.error("[LLM] Unexpected response: %s", response)
            return {"role": "assistant", "content": "", "error": str(e)}

    def _track_usage(self, response: dict, request_meta: dict[str, Any] | None = None):
        usage = response.get("usage", {})
        pt = usage.get("prompt_tokens", 0)
        ct = usage.get("completion_tokens", 0)
        with self._usage_lock:
            self.total_prompt_tokens += pt
            self.total_completion_tokens += ct
        self._record_request(request_meta or {"time": time.time(), "agent": "default"}, "succeeded", pt, ct)

    def _record_request(self, request_meta: dict[str, Any], outcome: str,
                        prompt_tokens: int = 0, completion_tokens: int = 0,
                        error: str | None = None) -> None:
        record = {
            **request_meta,
            "outcome": outcome,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
        }
        if error:
            record["error"] = error
        agent = str(record.get("agent") or "default")
        with self._usage_lock:
            self._request_count += 1
            if outcome == "succeeded":
                self._successful_requests += 1
            elif outcome == "rejected":
                self._rejected_requests += 1
            else:
                self._failed_requests += 1
            agent_usage = self._usage_by_agent.setdefault(agent, {
                "request_count": 0,
                "successful_requests": 0,
                "rejected_requests": 0,
                "failed_requests": 0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
            })
            agent_usage["request_count"] += 1
            outcome_key = {
                "succeeded": "successful_requests",
                "rejected": "rejected_requests",
            }.get(outcome, "failed_requests")
            agent_usage[outcome_key] += 1
            agent_usage["prompt_tokens"] += prompt_tokens
            agent_usage["completion_tokens"] += completion_tokens
            self._usage_history.append(record)
            if len(self._usage_history) > 100:
                del self._usage_history[:-100]

    def get_usage(self) -> dict:
        with self._usage_lock:
            return {
                "total_prompt_tokens": self.total_prompt_tokens,
                "total_completion_tokens": self.total_completion_tokens,
                "total_tokens": self.total_prompt_tokens + self.total_completion_tokens,
                "request_count": self._request_count,
                "successful_requests": self._successful_requests,
                "rejected_requests": self._rejected_requests,
                "failed_requests": self._failed_requests,
                "by_agent": {agent: dict(usage) for agent, usage in self._usage_by_agent.items()},
                "recent_requests": [dict(record) for record in self._usage_history[-20:]],
                "latest_rejection": dict(self._latest_rejection) if self._latest_rejection else None,
                "latest_compression": dict(self._latest_compression) if self._latest_compression else None,
            }

    def close(self):
        self._client.close()
