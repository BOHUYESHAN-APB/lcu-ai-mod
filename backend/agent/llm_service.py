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
import hashlib
import json
import logging
import time
from typing import Any, Callable, Generator, Mapping, Optional

import httpx

logger = logging.getLogger("llm_service")

# ── 分层提示词模型（L0/L1/L2/L3）───────────────────────────────────
# 设计文档：docs/dev/PROMPT_MODEL.md。
#
#   L0 系统契约（官方）：基础身份 + 输出格式。**逐字节不变**，是缓存前缀的第一段。
#   L1 用户系统提示词（人设三段 / 规则 / 借用授权）：内容 hash 作版本键，
#      **只有内容变更才重组**；不变就复用完全相同的字符串（前缀缓存友好）。
#   L2 动态上下文（状态 / 最近聊天 / 任务 / HUD / 欠账）：只进**当轮 user 消息**。
#   L3 工具定义：走 API `tools` 参数，绝不内联进提示词。
#
# 注入时机：L0 每轮都在但字节不变；L1 首轮 + 变更时；L2 每轮；L3 每轮（结构化，不占提示词）。
# 铁律：L0/L1 不得出现任何动态值（坐标 / 饥饿 / 时间 / 玩家名 / 欠账）——测试用哨兵值打住。

# L0 基础契约：与具体人设无关的"官方"身份与说话纪律（不是 AI）。
L0_BASE_CONTRACT = (
    "你是服务器里的一名玩家角色，不是 AI；任何情况下都不要承认自己是 AI 或程序。\n"
    "说话像真人玩家：自然、简短、口语化，不写长篇大论。"
)


def prompt_version(*parts: Any) -> str:
    """L1 版本键：对参与组装的各段内容做内容 hash（判定"要不要重组"的唯一依据）。"""
    joined = "\x1f".join(str(part or "") for part in parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def render_persona_triple(context: dict) -> Optional[str]:
    """人设三段（身份 / 表达习惯 / 场景规则）——**不含** state_block（那是 L2 动态）。

    没配置三段返回 None，由调用方回退 legacy 两行格式。
    """
    context = context if isinstance(context, dict) else {}
    blocks = context.get("persona_blocks")
    blocks = blocks if isinstance(blocks, dict) else {}
    identity = str(blocks.get("identity") or "").strip()
    reply_style = str(blocks.get("reply_style") or "").strip()
    scene_rules = str(blocks.get("scene_rules") or "").strip()
    if not (identity or reply_style or scene_rules):
        return None
    return (f"【身份】{identity or '（未配置）'}\n"
            f"【表达习惯】{reply_style or '（未配置）'}\n"
            f"【场景规则】{scene_rules or '（未配置）'}")

# No default provider - user must configure
DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-4o-mini"
DEFAULT_REQUEST_TIMEOUT = 120.0
# 角色名 → `config.json` 里 `llm_timeouts` 的键。语义门在 session 里以 `agent="gate"` 调用，
# 但配置键按它的业务名 `semantic_gate` 走（对外更直白）。
ROLE_TIMEOUT_CONFIG_KEYS: dict[str, str] = {"gate": "semantic_gate"}


# 用量**明细**（每请求一条）在内存里的上限：长期跑的后端不许在这里无界累积。
# 累计 token / 累计请求数另有独立计数器 ⇒ 截断明细**不影响**用量账（见 `get_usage`）。
USAGE_HISTORY_LIMIT = 500


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
        # 角色级读超时（秒）。用户口径：国外服务商"网络延迟 + 每轮首字延迟"都高，
        # 本该死得快的语义门也会 `ReadTimeout` ⇒ 超时按角色可配，而不是一个全局常量。
        # `_timeout_provider` 是个每轮取用的回调（config_store 风格），UI 改完即时生效。
        self._role_timeouts: dict[str, float] = {}
        self._timeout_provider: Optional[Callable[[], Mapping[str, Any]]] = None
        # "模型在飞"回调：请求发出前后各通知一次（供 state_governor 暂停进展判定）。
        self._inflight_begin: Optional[Callable[[str], None]] = None
        self._inflight_end: Optional[Callable[[], None]] = None
        # L1 版本键 + 上一次组装出的 L0+L1 字符串（键不变就原样复用，前缀缓存友好）。
        self._system_key: Optional[str] = None
        self._system_cache: str = ""

        # Token tracking：累计量是**计数器**（无界增长的是数值，不是内存）；明细列表**有上限**。
        self.total_prompt_tokens = 0
        self.total_completion_tokens = 0
        self._request_count = 0
        self._usage_history: list[dict] = []

    # ── 按角色读超时 ──

    def set_role_timeout(self, role: str, seconds: float | None) -> None:
        """固定某角色的读超时（秒）；``None`` 表示移除该角色的覆盖。"""
        name = str(role or "")
        if not name:
            return
        if seconds is None:
            self._role_timeouts.pop(name, None)
            return
        try:
            value = float(seconds)
        except (TypeError, ValueError):
            return
        if value > 0:
            self._role_timeouts[name] = value

    def set_timeout_provider(self, provider: Optional[Callable[[], Mapping[str, Any]]]) -> None:
        """注册"角色 → 秒"的**每轮取用**回调（例如 config_store 的读取函数）。

        用回调而不是一次性快照：UI 改完超时不必重启、不必重新接线。回调抛异常一律忽略。
        """
        self._timeout_provider = provider

    def configured_role_timeout(self, agent: str | None) -> Optional[float]:
        """该角色**配置**的读超时（秒）：provider → 固定表；没配置返回 ``None``。"""
        role = str(agent or "default")
        keys = (ROLE_TIMEOUT_CONFIG_KEYS.get(role, role), role)
        provider = self._timeout_provider
        if provider is not None:
            try:
                mapping = provider() or {}
            except Exception as exc:  # noqa: BLE001 — 配置读失败绝不拖垮调用
                logger.debug("[LLM] 读超时配置读取失败，用默认: %s", exc)
                mapping = {}
            if isinstance(mapping, Mapping):
                for key in keys:
                    value = mapping.get(key)
                    try:
                        value = float(value) if value is not None else 0.0
                    except (TypeError, ValueError):
                        value = 0.0
                    if value > 0:
                        return value
        for key in keys:
            value = self._role_timeouts.get(key)
            if value and value > 0:
                return float(value)
        return None

    def role_timeout(self, agent: str | None, explicit: float | None = None) -> float:
        """该角色这次请求实际用的读超时（秒）。

        取值顺序：**角色级配置**（`config.json → llm_timeouts`）→ 调用点显式值 → 全局默认。

        为什么角色配置**优先于**调用点：用户口径是"这个角色等得起多久，由我配的算" ——
        decider 这类调用点在代码里写死 8s 的地方（`planner.DECIDER_TIMEOUT_SECONDS`），
        对国外服务商的首字延迟只会误杀；配大了能等、配小了照样能收紧，都由配置说了算。
        """
        configured = self.configured_role_timeout(agent)
        if configured is not None:
            return float(configured)
        if explicit is not None:
            try:
                value = float(explicit)
            except (TypeError, ValueError):
                return DEFAULT_REQUEST_TIMEOUT
            if value > 0:
                return value
        return DEFAULT_REQUEST_TIMEOUT

    def set_inflight_callbacks(self, begin: Optional[Callable[[str], None]],
                               end: Optional[Callable[[], None]]) -> None:
        """注册"模型在飞"通知（请求发出前 `begin(角色)`、结束后 `end()`）。

        session 把它接到 `state_governor.begin_model_call/end_model_call`：读模型期间进展判定
        暂停 —— 用户等大模型时任务被看门狗掐掉，等于"自己把正在做的事打断"。
        """
        self._inflight_begin = begin
        self._inflight_end = end

    def _notify_inflight(self, begin: bool, role: str) -> None:
        callback = self._inflight_begin if begin else self._inflight_end
        if callback is None:
            return
        try:
            if begin:
                callback(str(role))
            else:
                callback()
        except Exception as exc:  # noqa: BLE001 — 通知坏掉绝不拖垮调用
            logger.debug("[LLM] 模型在飞通知失败: %s", exc)

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
        role = agent or "default"
        full = ""
        final_usage = {}

        # 流式同样是"模型在飞"：请求期间进展判定暂停（与 `_post` 同一套通知）。
        self._notify_inflight(True, role)
        try:
            with self._client.stream("POST", url, json=data, headers=headers,
                                     timeout=self.role_timeout(role)) as resp:
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
        finally:
            self._notify_inflight(False, role)

        if final_usage:
            self._track_usage({"usage": final_usage})

    # ── System Prompt ──

    def build_system_prompt(self, context: dict, commands_docs: str = "") -> str:
        """L0 + L1（system）：**只含跨轮不变的内容**。

        L1 = 人设三段 + 上游注入的人设上下文 + 命令文档；用内容 hash 作版本键，
        不变就直接返回上一次的字符串（**逐字节相同**，前缀缓存命中）。

        动态值（`persona_blocks.state_block` / `action_insights` / `interaction_summary`）
        **绝不在这里** —— 它们属于 L2，由 `build_dynamic_context` 拼进当轮 user 消息。
        """
        context = context if isinstance(context, dict) else {}
        l1 = self._build_l1(context, commands_docs)
        key = prompt_version(L0_BASE_CONTRACT, l1)
        if key != self._system_key:
            self._system_key = key
            self._system_cache = f"{L0_BASE_CONTRACT}\n\n{l1}".strip()
        return self._system_cache

    def _build_l1(self, context: dict, commands_docs: str = "") -> str:
        """用户系统提示词正文（人设 / 外部上下文 / 命令文档）；不含任何动态值。"""
        persona = context.get("persona")
        persona = persona if isinstance(persona, dict) else {}
        name = persona.get("name") or "AI"
        parts = [f"你是 {name}，一个正在玩 Minecraft 的玩家，和服务器里的其他玩家一起玩。"]
        triple = render_persona_triple(context)
        if triple:
            # 三段独立块（正文以 .local/persona.json 为准）；状态块与身份分开。
            parts.append(triple)
        else:
            personality = persona.get("personality") or "友好、自然"
            speaking_style = persona.get("speaking_style") or "口语化、简短"
            parts.append(f"你的人设性格是：{personality}。")
            parts.append(f"你的说话风格是：{speaking_style}。")
        external_context = persona.get("external_context")
        if external_context:
            parts.append(f"上游系统注入的人设上下文：{external_context}")
        if commands_docs:
            parts.append(commands_docs)
        return "\n".join(parts)

    def build_dynamic_context(self, context: dict, user_text: str = "") -> str:
        """L2：每轮的动态上下文 —— 只进**当轮 user 消息**，绝不混进 system。"""
        context = context if isinstance(context, dict) else {}
        parts = [str(user_text or "").strip()]
        blocks = context.get("persona_blocks")
        blocks = blocks if isinstance(blocks, dict) else {}
        state_block = str(blocks.get("state_block") or "").strip()
        if state_block:
            parts.append(f"【此刻状态（会变，不是身份）】{state_block}")
        if context.get("action_insights"):
            parts.append(f"Action history: {context['action_insights']}")
        if context.get("interaction_summary"):
            parts.append("Recent conversation:")
            parts.append(str(context["interaction_summary"]))
        return "\n\n".join(part for part in parts if part)

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
        # L3：工具定义走 API 的 tools 参数（**绝不内联进提示词**）。
        # 只在调用方真的给了工具时发送，避免给不支持的工具的供应商加字段。
        if kwargs.get("tools"):
            payload["tools"] = kwargs["tools"]
            if kwargs.get("tool_choice"):
                payload["tool_choice"] = kwargs["tool_choice"]
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
        role = agent or "default"
        # 角色级配置优先，其次调用点显式值，最后全局默认（见 `role_timeout`）。
        effective_timeout = self.role_timeout(role, timeout)
        # "模型在飞"：请求发出前通知（读模型期间进展判定暂停），无论成功/失败都收尾。
        self._notify_inflight(True, role)
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
        finally:
            self._notify_inflight(False, role)

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
            message = choice.get("message", {})
            return {
                "role": choice.get("role", "assistant"),
                "content": message.get("content", ""),
                "finish_reason": choice.get("finish_reason", ""),
                # L3 工具走 `tools` 参数后，模型可能改为**原生** tool_calls 而不是 JSON；
                # 原样透传给调用方，由它折算成候选（绝不丢掉模型选定的动作）。
                "tool_calls": message.get("tool_calls") or [],
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
            self._request_count += 1
            self._usage_history.append({"time": time.time(), "prompt": pt, "completion": ct})
            # **有界**：只留最近 `USAGE_HISTORY_LIMIT` 条明细（长期跑的后端不许在这里无界累积）。
            # 累计量（`total_*` / `request_count`）是独立的计数器，**不受**截断影响 ——
            # `/api/status` 的"总用量"和 session 的"本轮增量"照旧精确。
            if len(self._usage_history) > USAGE_HISTORY_LIMIT:
                del self._usage_history[:-USAGE_HISTORY_LIMIT]

    def get_usage(self) -> dict:
        return {
            "total_prompt_tokens": self.total_prompt_tokens,
            "total_completion_tokens": self.total_completion_tokens,
            "total_tokens": self.total_prompt_tokens + self.total_completion_tokens,
            # 累计请求数（**不是** `len(_usage_history)`：明细有上限，计数没有）。
            "request_count": self._request_count,
            "history_entries": len(self._usage_history),
            "history_limit": USAGE_HISTORY_LIMIT,
        }

    def close(self):
        self._client.close()
