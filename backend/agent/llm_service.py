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
import time
from typing import Any, Generator, Optional

import httpx

logger = logging.getLogger("llm_service")

# No default provider - user must configure
DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-4o-mini"


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

    def set_api_key(self, key: str):
        self.api_key = key
        logger.info("[LLM] API key set (prefix: %s)", key[:8] + "..." if len(key) > 8 else "?")
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
        if merged.get("base_url"):
            merged["base_url"] = str(merged["base_url"]).rstrip("/")
        self._agent_configs[agent] = merged
        if agent in {"default", "session"}:
            self.configure(merged)
        return self

    def get_config(self) -> dict:
        """Get current LLM configuration."""
        return {
            "configured": self.api_key is not None,
            "base_url": self.base_url,
            "model": self.model,
            "agents": {k: self._redact_config(v) for k, v in self._agent_configs.items()},
        }

    # ── Chat ──

    def chat(self, messages: list[dict], agent: str | None = None, **kwargs) -> dict:
        """Send a chat completion request. Returns parsed response."""
        config = self._resolve_config(agent)
        data = self._build_payload(messages, config=config, **kwargs)
        response = self._post("chat/completions", data, config=config)
        result = self._parse_response(response)
        self._track_usage(response)
        return result

    def chat_stream(self, messages: list[dict], agent: str | None = None, **kwargs) -> Generator[str, None, None]:
        """Stream a chat completion. Yields content chunks."""
        config = self._resolve_config(agent)
        data = self._build_payload(messages, stream=True, config=config, **kwargs)
        url = f"{config['base_url']}/chat/completions"
        headers = self._headers(config)
        full = ""
        final_usage = {}

        with self._client.stream("POST", url, json=data, headers=headers) as resp:
            for line in resp.iter_lines():
                if not line.startswith("data: "):
                    continue
                chunk_str = line[6:]
                if chunk_str == "[DONE]":
                    break
                try:
                    chunk = json.loads(chunk_str)
                    delta = chunk.get("choices", [{}])[0].get("delta", {})
                    content = delta.get("content", "")
                    if content:
                        full += content
                        yield content
                    if "usage" in chunk:
                        final_usage = chunk["usage"]
                except json.JSONDecodeError:
                    continue

        if final_usage:
            self._track_usage({"usage": final_usage})

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
        return {
            "model": kwargs.get("model", resolved["model"]),
            "messages": messages,
            "temperature": kwargs.get("temperature", resolved.get("temperature", 0.7)),
            "max_tokens": kwargs.get("max_tokens", resolved.get("max_tokens", 2048)),
            "stream": stream,
        }

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
        return {
            "base_url": str(config.get("base_url") or self.base_url).rstrip("/"),
            "model": config.get("model") or self.model,
            "api_key": config.get("api_key", self.api_key),
            "temperature": config.get("temperature", 0.7),
            "max_tokens": config.get("max_tokens", 2048),
        }

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

    def _track_usage(self, response: dict):
        usage = response.get("usage", {})
        pt = usage.get("prompt_tokens", 0)
        ct = usage.get("completion_tokens", 0)
        if pt or ct:
            self.total_prompt_tokens += pt
            self.total_completion_tokens += ct
            self._usage_history.append({"time": time.time(), "prompt": pt, "completion": ct})

    def get_usage(self) -> dict:
        return {
            "total_prompt_tokens": self.total_prompt_tokens,
            "total_completion_tokens": self.total_completion_tokens,
            "total_tokens": self.total_prompt_tokens + self.total_completion_tokens,
            "request_count": len(self._usage_history),
        }

    def close(self):
        self._client.close()
