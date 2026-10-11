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

import base64
import json
import logging
import time
from typing import Any, Generator, Optional

import httpx

logger = logging.getLogger("llm_service")

# No default provider - user must configure
DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-4o-mini"
DEFAULT_REQUEST_TIMEOUT = 120.0


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
        # 每个角色最近一次调用失败的原因（UI 直接显示，避免"配了却不知道错在哪"）
        self._last_error: dict[str, str] = {}

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

    def chat(self, messages: list[dict], agent: str | None = None,
             timeout: float | None = None, **kwargs) -> dict:
        """Send a chat completion request. Returns parsed response.

        `timeout` overrides the default request timeout for this call only. A
        latency-sensitive role such as the decider uses it so a slow response can be
        abandoned instead of blocking the caller.
        """
        config = self._resolve_config(agent)
        data = self._build_payload(messages, config=config, **kwargs)
        response = self._post("chat/completions", data, config=config, timeout=timeout, agent=agent)
        result = self._parse_response(response)
        self._track_usage(response)
        return result

    def chat_vision(self, prompt: str, image_jpeg: bytes, agent: str = "vision",
                    timeout: float | None = None, **kwargs) -> dict:
        """单图多模态请求：把 JPEG 以 data URI 内联进 user 消息。

        走 OpenAI 兼容的 content-parts 格式，因此任何支持视觉的兼容模型都能用。
        """
        encoded = base64.b64encode(image_jpeg).decode("ascii")
        messages = [{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {
                    "url": f"data:image/jpeg;base64,{encoded}"}},
            ],
        }]
        return self.chat(messages, agent=agent, timeout=timeout, **kwargs)

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
        blocks = context.get("persona_blocks") if isinstance(context, dict) else None
        blocks = blocks if isinstance(blocks, dict) else {}
        parts = [
            f"你是 {persona_name}，一个正在玩 Minecraft 的玩家，和服务器里的其他人一起玩。",
        ]
        if blocks.get("identity") or blocks.get("reply_style") or blocks.get("scene_rules"):
            # 三段独立块（人设正文以 .local/persona.json 为准）；状态块与身份分开。
            parts.append(f"【身份】{blocks.get('identity') or personality}")
            parts.append(f"【表达习惯】{blocks.get('reply_style') or speaking_style}")
            parts.append(f"【场景规则】{blocks.get('scene_rules') or '（未配置）'}")
            if blocks.get("state_block"):
                parts.append(f"【此刻状态（会变，不是身份）】{blocks['state_block']}")
        else:
            parts.append(f"你的人设性格是：{personality}。")
            parts.append(f"你的说话风格是：{speaking_style}。")
        parts.extend([
            "你不是 AI，不要暴露自己是 AI。说话要自然、随意，像真人玩家一样。",
            '可以用"哈哈"、"好的"、"来了"、"等下"等口语。回复要简短，不要长篇大论。',
            "",
        ])
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

    # 零思考契约：默认**不发送任何**思考/推理字段，模型直接给最终结果。
    # 对 decider 这类"只回一行 JSON / 只做工具调用"的角色，这比任何思考模式都合适——
    # 通用大语言模型直接输出 JSON 或 tool call 本来就是标准做法，不需要专有模型。
    # thinking=False（默认）：一个字段都不发。
    # thinking=True：把下面这些键按调用方/角色配置透传给供应商。
    #   注意 thinking 本身只是**本项目的控制开关**，永远不会作为字段发出去；
    #   需要按某供应商的格式显式声明时，用下面这些键（各家命名不同，按需增补）。
    THINKING_KEYS = ("reasoning_effort", "reasoning", "enable_thinking")

    def _build_payload(self, messages: list[dict], stream: bool = False,
                       config: dict[str, Any] | None = None, **kwargs) -> dict:
        resolved = config or self._resolve_config(None)
        payload = {
            "model": kwargs.get("model", resolved["model"]),
            "messages": messages,
            "temperature": kwargs.get("temperature", resolved.get("temperature", 0.7)),
            "max_tokens": kwargs.get("max_tokens", resolved.get("max_tokens", 2048)),
            "stream": stream,
        }
        # 角色级默认在角色配置里（thinking 缺省=False），调用方可按次覆盖；
        # 透传时调用参数优先，其次取角色配置里的同名键。
        if kwargs.get("thinking", resolved.get("thinking", False)):
            passed = {key: kwargs.get(key, resolved.get(key)) for key in self.THINKING_KEYS
                      if key in kwargs or key in resolved}
            payload.update(passed)
            logger.debug("[LLM] thinking 已开启，透传字段: %s", sorted(passed))
        return payload

    def _post(self, endpoint: str, data: dict, config: dict[str, Any] | None = None,
              timeout: float | None = None, agent: str | None = None) -> dict:
        resolved = config or self._resolve_config(None)
        url = f"{resolved['base_url']}/{endpoint}"
        headers = self._headers(resolved)
        effective_timeout = timeout if timeout is not None else DEFAULT_REQUEST_TIMEOUT
        role = agent or "default"
        try:
            resp = self._client.post(url, json=data, headers=headers, timeout=effective_timeout)
            resp.raise_for_status()
            self._last_error.pop(role, None)
            return resp.json()
        except httpx.HTTPStatusError as e:
            message = f"HTTP {e.response.status_code}: {e.response.text[:160]}"
            logger.error("[LLM] %s -> %s", url, message)
            self._last_error[role] = message
            raise
        except Exception as e:
            message = f"{type(e).__name__}: {e}"
            logger.error("[LLM] %s -> %s", url, message)
            self._last_error[role] = message
            raise

    def get_last_error(self, agent: str | None = None) -> str:
        """该角色上一次调用失败的原因（空串=最近一次成功），供 UI 直接显示。"""
        return self._last_error.get(agent or "default", "")

    def effective_config(self, agent: str) -> dict[str, Any]:
        """**运行中实际生效**的配置（含从 default 继承的结果）；不返回密钥明文，
        只告诉调用方 key 是否就位。与持久化文件对照，就能看出"配了但没生效"。"""
        resolved = self._resolve_config(agent)
        return {
            "base_url": resolved["base_url"],
            "model": resolved["model"],
            "key_present": bool(resolved.get("api_key")),
            "thinking": bool(resolved.get("thinking", False)),
            "temperature": resolved.get("temperature"),
            "max_tokens": resolved.get("max_tokens"),
        }

    def _headers(self, config: dict[str, Any] | None = None) -> dict:
        headers = {"Content-Type": "application/json"}
        key = (config or {}).get("api_key") if config is not None else self.api_key
        if key:
            headers["Authorization"] = f"Bearer {key}"
        return headers

    def _resolve_config(self, agent: str | None) -> dict[str, Any]:
        config = dict(self._agent_configs.get("default", {}))
        if agent:
            role = self._agent_configs.get(agent)
            if role:
                # 角色未填写的字段（None / 空串）回落到 default，避免空 key 覆盖主配置。
                config.update({k: v for k, v in role.items() if v not in (None, "")})
        resolved = {
            "base_url": str(config.get("base_url") or self.base_url).rstrip("/"),
            "model": config.get("model") or self.model,
            "api_key": config.get("api_key", self.api_key),
            "temperature": config.get("temperature", 0.7),
            "max_tokens": config.get("max_tokens", 2048),
            # 零思考是默认值；角色配置里显式打开 thinking 才会发送思考字段。
            "thinking": bool(config.get("thinking", False)),
        }
        for key in self.THINKING_KEYS:
            if key in config:
                resolved[key] = config[key]
        return resolved

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
