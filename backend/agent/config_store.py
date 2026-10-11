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
from pathlib import Path
from typing import Any, Optional


DEFAULT_AGENT = "default"
DEFAULT_CONFIG_PATH = Path(__file__).parent.parent / ".local" / "config.json"

# decider 置信度阈值的默认值（可由 config.json 的 decision.confidence_threshold 覆盖）
DECISION_DEFAULT_CONFIDENCE = 0.6
LEGACY_CONFIG_PATH = Path(__file__).parent.parent / "config.json"


# 角色化模型配置。
# lcumod 的 AI 分层：planner(System 2 主脑) → decider(System 3 决策/打分) → modes_engine(System 1 反射)。
# 每个角色都可以指向不同的后端模型，未填写的字段自动继承 default。
AGENT_ROLES: list[dict[str, str]] = [
    {"id": "default", "name": "默认 / 兜底", "purpose": "未指定角色时使用，同时作为其它角色的继承来源"},
    {"id": "planner", "name": "Planner（规划 · System 2）", "purpose": "理解玩家意图、结合世界状态生成候选动作"},
    {"id": "decider", "name": "Decider（决策/打分 · System 3）", "purpose": "对候选动作打分并返回置信度，低置信时回退"},
    {"id": "vision", "name": "Vision（视觉 · VLM）", "purpose": "读取游戏截图，输出场景描述（需多模态模型）"},
    {"id": "embedding", "name": "Embedding（记忆检索）", "purpose": "记忆语义检索 / RAG（可选）"},
]
AGENT_ROLE_IDS: list[str] = [role["id"] for role in AGENT_ROLES]


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
        # v2.5 系列 2026-10-21 官方下线，默认指向 v2.6。
        "default_model": "mimo-v2.6-flash",
        "recommended_models": [
            "mimo-v2.6-flash",
            "mimo-v2-omni",
            "mimo-v2.5-pro",
            "mimo-v2.5",
        ],
        "api_key_required": True,
        "openai_compatible": True,
    },
    "deepseek": {
        "id": "deepseek",
        "name": "DeepSeek",
        "base_url": "https://api.deepseek.com/v1",
        "models_path": "/models",
        # V4.1-Flash（2026-09 发布）552B MoE / 8B 激活、原生多模态、便宜。
        "default_model": "deepseek-v4.1-flash",
        "recommended_models": [
            "deepseek-v4.1-flash",
            "deepseek-chat",
            "deepseek-reasoner",
        ],
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


# 单模型模式下，非 default 角色里这些"换家相关"字段一律清空（改为继承 default）。
SINGLE_MODEL_ROLE_KEYS = ("provider", "base_url", "api_key", "model")


def _normalize_single_model(data: dict[str, Any]) -> dict[str, Any]:
    """单模型模式的**结构性防线**。

    现象（用户实机遇到）：换 key/URL 后 `vision` 仍在打旧地址。原因是历史配置给**每个角色**
    都写过旧家的 provider/base_url/api_key/model，而解析规则是"角色非空则覆盖 default"。
    这里在**加载/保存时**就把这些字段清掉，角色只保留自己的调参
    （temperature / max_tokens / thinking），因此"换了家却还在打旧地址"不可能再发生。
    """
    llm = data.get("llm")
    if not isinstance(llm, dict) or not llm.get("single_model", True):
        return data
    agents = llm.get("agents")
    if not isinstance(agents, dict):
        return data
    for name, role in agents.items():
        if name == DEFAULT_AGENT or not isinstance(role, dict):
            continue
        for key in SINGLE_MODEL_ROLE_KEYS:
            role.pop(key, None)
    return data


CAPABILITY_MIN, CAPABILITY_MAX = 1, 3


def _clamp_capability(value: Any) -> int:
    """能力层级夹到 1..3；非法值回退 L1。"""
    try:
        return min(max(int(value), CAPABILITY_MIN), CAPABILITY_MAX)
    except (TypeError, ValueError):
        return 1


def _clamp_confidence(value: Any) -> float:
    """把置信度阈值夹到 [0, 1]；非法值回退默认。"""
    try:
        return min(max(float(value), 0.0), 1.0)
    except (TypeError, ValueError):
        return DECISION_DEFAULT_CONFIDENCE


def _parse_bool(value: Any) -> Optional[bool]:
    """识别布尔语义的值（UI 会以布尔或字符串两种形式回传）；认不出返回 None。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"true", "1", "yes", "on", "是"}:
            return True
        if text in {"false", "0", "no", "off", "否"}:
            return False
    return None


def _as_bool(value: Any, default: bool) -> bool:
    """宽松布尔归一化：认不出就用 default。"""
    parsed = _parse_bool(value)
    return default if parsed is None else parsed


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _default_config() -> dict[str, Any]:
    # 默认**不绑定任何一家**：用 DeepSeek 的 OpenAI 兼容端点（用户指定为"最泛用"），
    # base_url / model / api_key 在 UI 里都是可见可改的一等字段；小米 MiMo 只是可选预设之一。
    preset = PROVIDER_PRESETS["deepseek"]
    base_agent = {
        "provider": preset["id"],
        "base_url": preset["base_url"],
        "model": preset["default_model"],
        "api_key": "",
        "temperature": 0.7,
        "max_tokens": 2048,
        # 零思考（默认）：不传思考模式/思考深度，模型直接输出结果（JSON / tool call）。
        "thinking": False,
    }
    agents = {role_id: copy.deepcopy(base_agent) for role_id in AGENT_ROLE_IDS}
    # 决策/打分模型需要稳定、可复现的判断，用低温度。
    agents["decider"]["temperature"] = 0.2
    # 视觉角色需要多模态模型：默认模型本身（deepseek-v4.1-flash）就支持图像输入，故继承即可；
    # 若换成不支持视觉的默认模型，请在 UI 里单独给 vision 指定多模态模型。
    return {
        "version": 1,
        "llm": {
            "default_agent": DEFAULT_AGENT,
            # 单模型模式（默认开）：规划/决策/视觉/记忆**全部用 default 这一个模型**。
            # 用户要求"先只用一个模型做最小验证，之后再逐个加"。
            "single_model": True,
            "agents": agents,
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
        # 注意：旧的 whitelist / listen_public 已由 `.local/permissions.json` 的
        # master / friends / server_mode / public_reply_guests 取代（M3-19 的分层权限模型）。
        "patrol_radius": 8,
        # 能力分层（用户的"从最小开始、逐层加"）：
        #   1 = L1 纯文本单模型（默认；不抓屏、不传图片）
        #   2 = L2 单模型 + 视觉（同一个模型传截图）
        #   3 = L3 决策模型 / VLA（规划中，尚未接入）
        "capability_level": 1,
        "decision": {
            "confidence_threshold": DECISION_DEFAULT_CONFIDENCE,
            # 回复语义门（L4）：每条消息是否值得接话的廉价一次判断。
            # 关闭后直接进入 planner（省一次小调用，但会更容易"什么话都接"）。
            "semantic_gate": True,
        },
        # 会话树读取开关（新地基 `agent/session_core/`，见 docs/dev/AGENT_ARCH.md §1）。
        # 关闭（默认）→ 「对话历史的读取」走既有链路，路径与字节完全照旧；
        # 打开 → 「对话历史的读取」改走 session_core 纯投影 + 只读适配（写侧仍归旧链路）。
        # 默认关闭是刻意的：新链路未逐项验收前不许默认生效。
        "session_tree": False,
        # 周期性状态事件的日志（`backend/protocol/wire_client.py`）：
        #   log_changes_only=true（默认）→ 只有内容**变了**才打行（相同就压掉并计数），
        #     避免 behavior_state/control_state 每秒刷屏把关键日志淹掉；
        #   log_heartbeat_seconds=0（默认关）→ 内容长时间没变时每隔 N 秒强打一行"未变"，
        #     用来区分"链路活着但状态没变"与"链路断了"。用户口径是"没刷新就不刷新"，
        #     所以默认必须是 0；需要时再开。
        "log_changes_only": True,
        "log_heartbeat_seconds": 0,
        # 状态治理（`agent/state_governor.py`）：卡死即摘掉 + 资源分租仲裁。
        # 默认**开**（用户明确要这个行为）；关掉 ⇒ 行为回到现在（不仲裁、不取消）。
        "state_governor": {
            "enabled": True,
            # N 秒无进展（位置未变 / 同一工具结果重复）⇒ 取消该状态/任务。
            "no_progress_seconds": 20.0,
            # 取消后同一目标 T 秒内不许立刻重试（否则又成死循环）。
            "retry_cooldown_seconds": 30.0,
            # 同屏治理台账上限（超出淘汰最低优先级的非生存项）。
            "max_tracked": 50,
        },
        # 编排轮次（`agent/job_registry.py`，AGENT_ARCH §10.2 #3）：当且仅当 `task_list` 里有
        # **模型自己创建的活跃任务**时，在**事件**发生（进度变化 / 出结果 / 卡住 / 玩家说话）时
        # 把模型请回来做下一步。**不是定时器**、**不是后台自主引擎**。
        # 默认**开**（用户明确要这个）；关掉 ⇒ 行为回到现在（不再有编排轮）。
        "job_orchestration": True,
    }


class ConfigStore:
    """Thread-safe JSON-backed configuration store."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._data = self._load()

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
            return _normalize_single_model(_deep_merge(defaults, loaded))
        except Exception:
            return defaults

    def save(self) -> None:
        with self._lock:
            self.path.write_text(
                json.dumps(self._data, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )

    def raw(self, redact: bool = True) -> dict[str, Any]:
        with self._lock:
            data = copy.deepcopy(self._data)
        return self._redact(data) if redact else data

    def list_provider_presets(self) -> list[dict[str, Any]]:
        return [copy.deepcopy(p) for p in PROVIDER_PRESETS.values()]

    @staticmethod
    def list_agent_roles() -> list[dict[str, str]]:
        """Return the role catalogue used to configure per-role backend models."""
        return copy.deepcopy(AGENT_ROLES)

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

            if current.get("base_url"):
                current["base_url"] = str(current["base_url"]).rstrip("/")

            agents[agent_name] = current

            if "single_model" in payload:
                self._data["llm"]["single_model"] = bool(payload["single_model"])

            # 单模型模式：清掉非 default 角色的换家字段（见 _normalize_single_model 注释）。
            _normalize_single_model(self._data)

            self._data["llm"]["default_agent"] = self._data["llm"].get("default_agent") or DEFAULT_AGENT
            self.save()
            result = copy.deepcopy(current)
        return self._redact(result)

    def get_persona(self) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._data.setdefault("persona", _default_config()["persona"]))

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
            for key in ("patrol_radius",):
                if key in payload:
                    self._data[key] = payload[key]
            if "capability_level" in payload:
                self._data["capability_level"] = _clamp_capability(payload["capability_level"])
            if isinstance(payload.get("decision"), dict):
                decision = payload["decision"]
                threshold = decision.get("confidence_threshold")
                if threshold is not None:
                    self._data.setdefault("decision", {})[
                        "confidence_threshold"
                    ] = _clamp_confidence(threshold)
                if "semantic_gate" in decision:
                    parsed_gate = _parse_bool(decision["semantic_gate"])
                    if parsed_gate is not None:
                        self._data.setdefault("decision", {})["semantic_gate"] = parsed_gate
            if "session_tree" in payload:
                parsed_tree = _parse_bool(payload["session_tree"])
                if parsed_tree is not None:
                    self._data["session_tree"] = parsed_tree
            if "log_changes_only" in payload:
                parsed_changes = _parse_bool(payload["log_changes_only"])
                if parsed_changes is not None:
                    self._data["log_changes_only"] = parsed_changes
            if "log_heartbeat_seconds" in payload:
                try:
                    self._data["log_heartbeat_seconds"] = max(
                        0.0, float(payload["log_heartbeat_seconds"]))
                except (TypeError, ValueError):
                    pass
            if isinstance(payload.get("state_governor"), dict):
                from .state_governor import GovernorConfig
                self._data["state_governor"] = GovernorConfig.from_mapping(
                    payload["state_governor"]).as_dict()
            if "job_orchestration" in payload:
                parsed_orch = _parse_bool(payload["job_orchestration"])
                if parsed_orch is not None:
                    self._data["job_orchestration"] = parsed_orch
            self.save()
            return self.raw(redact=True)

    def get_capability_level(self) -> int:
        """能力层级：1=纯文本单模型（默认）2=+视觉 3=决策模型/VLA（规划中）。"""
        with self._lock:
            raw = self._data.get("capability_level", 1)
        return _clamp_capability(raw)

    def get_semantic_gate(self) -> bool:
        """回复语义门开关（config.json → decision.semantic_gate，默认开）。"""
        with self._lock:
            raw = self._data.get("decision", {}).get("semantic_gate", True)
        return _as_bool(raw, True)

    def get_session_tree(self) -> bool:
        """会话树读取开关（config.json → session_tree，**默认关**）。

        关闭（默认）→ 「对话历史的读取」走既有链路（路径与字节完全照旧）；
        打开 → 改走 `agent/session_core/` 的纯投影 + 只读适配。
        """
        with self._lock:
            raw = self._data.get("session_tree", False)
        return _as_bool(raw, False)

    def get_log_changes_only(self) -> bool:
        """周期性状态日志只打"变化"行（config.json → log_changes_only，默认开）。

        关闭 → wire 日志逐字回到旧行为（每个事件一行），给靠旧日志排障的人留退路。
        """
        with self._lock:
            raw = self._data.get("log_changes_only", True)
        return _as_bool(raw, True)

    def get_log_heartbeat_seconds(self) -> float:
        """心跳间隔（config.json → log_heartbeat_seconds，**默认 0 = 关**）。

        设了正数 → 内容长时间没变也每隔 N 秒强打一行"未变"，区分"链路活着但状态没变"
        与"链路断了"。非法/负值一律回退 0（关）。
        """
        with self._lock:
            raw = self._data.get("log_heartbeat_seconds", 0)
        try:
            return max(0.0, float(raw))
        except (TypeError, ValueError):
            return 0.0

    def get_state_governor_config(self) -> dict[str, Any]:
        """状态治理开关与阈值（``config.json`` → ``state_governor``，带默认值）。

        键：``enabled``（默认 True）/ ``no_progress_seconds``（默认 20）/
        ``retry_cooldown_seconds``（默认 30）/ ``max_tracked``（默认 50）。
        """
        from .state_governor import GovernorConfig
        with self._lock:
            raw = self._data.get("state_governor")
        merged = dict(GovernorConfig().as_dict())
        if isinstance(raw, dict):
            merged.update(raw)
        return merged

    def get_job_orchestration(self) -> bool:
        """编排轮次开关（``config.json`` → ``job_orchestration``，**默认开**）。

        开 → 当 `task_list` 里有模型自己创建的活跃任务、且有事件发生时，把模型请回来做下一步；
        关 → 行为回到现在（不再有编排轮；模型只由对话/事件注入那条既有路径触发）。
        """
        with self._lock:
            raw = self._data.get("job_orchestration", True)
        return _as_bool(raw, True)

    def get_decision_threshold(self) -> float:
        """decider 置信度阈值；非法值回退默认。"""
        with self._lock:
            raw = self._data.get("decision", {}).get("confidence_threshold")
        if raw is None:
            return DECISION_DEFAULT_CONFIDENCE
        try:
            return min(max(float(raw), 0.0), 1.0)
        except (TypeError, ValueError):
            return DECISION_DEFAULT_CONFIDENCE

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

# ── 共享实例注册表 ─────────────────────────────────────────────
# server.py 与 Session/Planner 必须拿到**同一个** ConfigStore 实例：否则 UI 改配置
# 写进一个实例的内存副本，Planner 读另一个实例 → 改动不生效（见 ROADMAP M3-4）。
_STORE_REGISTRY: dict[str, "ConfigStore"] = {}
_STORE_REGISTRY_LOCK = threading.RLock()


def get_config_store(path: str | Path = DEFAULT_CONFIG_PATH) -> "ConfigStore":
    """按绝对路径共享的 ConfigStore 单例。"""
    resolved = str(Path(path).resolve())
    with _STORE_REGISTRY_LOCK:
        store = _STORE_REGISTRY.get(resolved)
        if store is None:
            store = ConfigStore(path)
            _STORE_REGISTRY[resolved] = store
        return store
