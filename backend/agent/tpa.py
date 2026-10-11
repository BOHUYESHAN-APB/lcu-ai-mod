"""TPA（传送请求）—— 两路径 + 分层策略 + 全量审计。

用户口径（原话）：

    「tpa 需要在对话列表去点同意 accept」（有的服是聊天里可点，有的服是**弹窗**
      要鼠标点同意/拒绝）。

两条路径（**后端绝不自己实现界面/鼠标模拟** ✗）：

- **路径 A（聊天里可点）**：直接执行等价指令（`/tpaccept` / `/tpdeny`），**不需要鼠标** ✓
  —— 走 `CommandGate` 的白名单通道。
- **路径 B（服务端弹窗）**：发 Java 命令 **`click_screen_button {match}`**（Java 侧实现，
  在**已打开**的界面里做**控件级**点击，不移动鼠标、不开界面）。

分层策略（默认值写死在 `TPA_POLICY_DEFAULTS`，可被 `.local/permissions.json` 的 `tpa`
段覆盖）：

- master：默认**自动接受**；
- friend：默认**询问**（ask）；
- guest：默认**拒绝且不去点**（不发指令、不点按钮、不花 token）；
- 主动发起 `tpa`：仅 master（friend 可配，guest 不可）。

识别话术**可配**（`.local/commands.json` 的 `tpa` 段，UI 可加多条）：默认给中英文
常见措辞；认不出**不猜**（当普通聊天）。每一次请求/响应/结果都进审计。
"""

from __future__ import annotations

import copy
import logging
import re
import time
from typing import Any, Callable, Optional

logger = logging.getLogger("tpa")

# 策略取值：自动接受 / 先问 / 拒绝（拒绝 ＝ **不动作**：不发指令也不点按钮）。
TPA_ACCEPT, TPA_ASK, TPA_DENY = "accept", "ask", "deny"
_TPA_POLICIES = (TPA_ACCEPT, TPA_ASK, TPA_DENY)
# 层级名（与 `permissions.TIERS` 同一套口径；不 import 以免循环依赖）。
_TPA_TIERS = ("master", "friend", "guest")

# 默认策略：master 自动接受；friend 先问；guest 拒绝且不动作。
TPA_POLICY_DEFAULTS: dict[str, Any] = {
    "master": TPA_ACCEPT,
    "friend": TPA_ASK,
    "guest": TPA_DENY,
    # 主动发起 tpa：只有 master 默认可以（friend 可配）。
    "outgoing": {"master": True, "friend": False, "guest": False},
    # 路径选择：auto＝按事件来源判定（弹窗事件走 B，聊天文本走 A）。
    "path": "auto",
}

# ── 默认识别话术（全部锚定首尾/行首，避免把普通聊天误判成 TPA）───────────
# 请求：服务器/插件广播"某人请求传送到你这里"。玩家名允许中文（`[^\s:：,，]`）。
DEFAULT_REQUEST_PATTERNS: tuple[str, ...] = (
    r"^(?P<player>[A-Za-z0-9_]{1,16})\s+(?:wants|would like|has requested|requested|"
    r"is requesting)\s+to\s+teleport\s+to\s+you\b",
    r"^(?:teleport|tpa)\s+request\s+from\s+(?P<player>[A-Za-z0-9_]{1,16})\b",
    r"^(?P<player>[^\s:：,，]{1,24})\s*(?:想要|请求|希望|申请)\s*(?:传送|tp)\s*(?:到|至)?\s*"
    r"你(?:这里|那里|处|身边)?",
    r"^(?P<player>[^\s:：,，]{1,24})\s*请求(?:与你|和你)?传送",
)

# 触发词：**我们这边的人**（master/friend）在聊天里对我们说的"接受/拒绝"。
DEFAULT_ACCEPT_TRIGGERS: tuple[str, ...] = (
    r"^(?:我)?(?:同意|接受|允许)(?:他|她)?(?:的)?(?:传送|tp|tpa)?\s*(?:请求)?[。.!！\s]*$",
    r"^(?:i\s+)?(?:accept|approve|allow)(?:\s+(?:the\s+)?(?:tpa|teleport))?(?:\s+request)?[.!]?$",
    r"^(?:accept|approve|allow)\s+(?P<player>\S+)[.!]?$",
)
DEFAULT_DENY_TRIGGERS: tuple[str, ...] = (
    r"^(?:我)?(?:拒绝|不同意|不许|别理)(?:他|她)?(?:的)?(?:传送|tp|tpa)?\s*(?:请求)?[。.!！\s]*$",
    r"^(?:i\s+)?(?:deny|decline|reject|refuse)(?:\s+(?:the\s+)?(?:tpa|teleport))?"
    r"(?:\s+request)?[.!]?$",
    r"^(?:deny|decline|reject)\s+(?P<player>\S+)[.!]?$",
)

# 结果广播（我们发起的请求被对方接受/拒绝）——只用于审计"结果"。
DEFAULT_RESULT_PATTERNS: dict[str, tuple[str, ...]] = {
    "accepted": (
        r"^(?P<player>\S+)\s+accepted your teleport request\b",
        r"^传送请求已被\s*(?P<player>\S+)\s*接受",
    ),
    "denied": (
        r"^(?P<player>\S+)\s+(?:denied|declined)\s+your teleport request\b",
        r"^传送请求(?:已被)?\s*(?P<player>\S+)?\s*拒绝",
    ),
}

# 弹窗里"接受/拒绝"按钮的默认匹配串（`click_screen_button {match}` 用的）。
DEFAULT_ACCEPT_BUTTONS: tuple[str, ...] = ("接受", "同意", "Accept", "Allow")
DEFAULT_DENY_BUTTONS: tuple[str, ...] = ("拒绝", "不同意", "Deny", "Decline", "Reject")

# 路径 A 的等价指令（走白名单通道）。
DEFAULT_ACCEPT_COMMAND = "tpaccept"
DEFAULT_DENY_COMMAND = "tpdeny"


def _as_str_list(value: Any, default: tuple[str, ...]) -> list[str]:
    """字符串列表解析：单个字符串也接受；空/非字符串条目丢掉。"""
    if value is None:
        return list(default)
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple, set)):
        return list(default)
    out = [str(item).strip() for item in value if str(item or "").strip()]
    return out or list(default)


def _as_policy(value: Any, default: str) -> str:
    text = str(value or "").strip().lower()
    return text if text in _TPA_POLICIES else default


def normalize_tpa_policy(raw: Any) -> dict[str, Any]:
    """`.local/permissions.json` 的 `tpa` 段归一化（认不出的一律回退默认）。"""
    data = raw if isinstance(raw, dict) else {}
    outgoing_raw = data.get("outgoing")
    outgoing_raw = outgoing_raw if isinstance(outgoing_raw, dict) else {}
    outgoing: dict[str, bool] = {}
    for tier, default in TPA_POLICY_DEFAULTS["outgoing"].items():
        value = outgoing_raw.get(tier, default)
        outgoing[tier] = bool(value) if isinstance(value, bool) else bool(default)
    path = str(data.get("path") or TPA_POLICY_DEFAULTS["path"]).strip().lower()
    if path not in {"auto", "chat", "popup"}:
        path = "auto"
    return {
        "master": _as_policy(data.get("master"), TPA_POLICY_DEFAULTS["master"]),
        "friend": _as_policy(data.get("friend"), TPA_POLICY_DEFAULTS["friend"]),
        "guest": _as_policy(data.get("guest"), TPA_POLICY_DEFAULTS["guest"]),
        "outgoing": outgoing,
        "path": path,
    }


def normalize_tpa_config(raw: Any) -> dict[str, Any]:
    """`.local/commands.json` 的 `tpa` 段归一化（用户加的话术**追加**在默认之后）。

    兼容设计文档里的简写（`docs/dev/SERVER_COMMANDS.md` §2）：
    `{"patterns": [...], "accept": "tpaccept", "deny": "tpdeny"}` —— 与完整键等价。
    """
    data = raw if isinstance(raw, dict) else {}
    request_patterns = data.get("request_patterns")
    if request_patterns is None:
        request_patterns = data.get("patterns")
    accept_triggers = data.get("accept_triggers")
    if accept_triggers is None:
        accept_triggers = data.get("accept_words")
    deny_triggers = data.get("deny_triggers")
    if deny_triggers is None:
        deny_triggers = data.get("deny_words")
    return {
        "request_patterns": list(DEFAULT_REQUEST_PATTERNS) + _extra(request_patterns),
        "accept_triggers": list(DEFAULT_ACCEPT_TRIGGERS) + _extra(accept_triggers),
        "deny_triggers": list(DEFAULT_DENY_TRIGGERS) + _extra(deny_triggers),
        "accept_buttons": _as_str_list(data.get("accept_buttons"), DEFAULT_ACCEPT_BUTTONS),
        "deny_buttons": _as_str_list(data.get("deny_buttons"), DEFAULT_DENY_BUTTONS),
        "accept_command": str(data.get("accept_command") or data.get("accept")
                              or DEFAULT_ACCEPT_COMMAND).strip() or DEFAULT_ACCEPT_COMMAND,
        "deny_command": str(data.get("deny_command") or data.get("deny")
                            or DEFAULT_DENY_COMMAND).strip() or DEFAULT_DENY_COMMAND,
    }


def _extra(value: Any) -> list[str]:
    """用户自加的正则：只留非空字符串（坏正则在使用处丢掉，不在这里炸）。"""
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple, set)):
        return []
    return [str(item) for item in value if isinstance(item, str) and item.strip()]


def decision_for(policy: dict[str, Any], tier: str) -> str:
    """该 tier 对收到 TPA 请求的策略：accept / ask / deny（认不出的 tier 当 guest）。"""
    normalized = normalize_tpa_policy(policy)
    tier_key = str(tier or "").strip().lower()
    if tier_key not in _TPA_TIERS:
        tier_key = "guest"
    value = normalized.get(tier_key)
    return value if value in _TPA_POLICIES else TPA_DENY


def may_initiate(policy: dict[str, Any], tier: str) -> bool:
    """该 tier 能否主动发起 tpa（默认只有 master）。"""
    normalized = normalize_tpa_policy(policy)
    outgoing = normalized["outgoing"]
    tier_key = str(tier or "").strip().lower()
    return bool(outgoing.get(tier_key, False))


class TpaDetector:
    """话术识别：请求 / 接受或拒绝的触发词 / 结果广播。

    配置每拍现取（`config_provider`），UI 加一条话术即时生效。认不出一律返回 None
    —— **绝不猜**（猜错会导致"没请求也去点"或"该接受却拒了"）。
    """

    def __init__(self, config_provider: Optional[Callable[[], dict]] = None):
        self._config_provider = config_provider

    def config(self) -> dict[str, Any]:
        raw: Any = None
        if self._config_provider is not None:
            try:
                raw = self._config_provider()
            except Exception as exc:  # noqa: BLE001 —— 配置读挂掉也要能跑默认话术
                logger.warning("[Tpa] 读取 tpa 配置失败，用默认话术: %s", exc)
        return normalize_tpa_config(raw)

    @staticmethod
    def _match(patterns: Any, text: str) -> Optional[re.Match]:
        for pattern in patterns or ():
            try:
                match = re.match(pattern, text, re.IGNORECASE)
            except re.error as exc:
                logger.warning("[Tpa] 正则非法，已忽略 %r: %s", pattern, exc)
                continue
            if match:
                return match
        return None

    def detect_request(self, text: Any) -> Optional[dict]:
        """识别"有人在请求传送到你这里"。返回 `{"kind":"request","player":…}`。"""
        raw = str(text or "").strip()
        if not raw:
            return None
        match = self._match(self.config()["request_patterns"], raw)
        if not match:
            return None
        groups = match.groupdict()
        player = str(groups.get("player") or "").strip()
        return {"kind": "request", "player": player, "text": raw}

    def detect_intent(self, text: Any) -> Optional[dict]:
        """识别我们这边的人说的"接受/拒绝"。返回 `{"kind":"intent","action","player"}`。"""
        raw = str(text or "").strip()
        if not raw:
            return None
        config = self.config()
        for action, key in (("accept", "accept_triggers"), ("deny", "deny_triggers")):
            match = self._match(config[key], raw)
            if match:
                groups = match.groupdict()
                return {"kind": "intent", "action": action,
                        "player": str(groups.get("player") or "").strip(), "text": raw}
        return None

    def detect(self, text: Any) -> Optional[dict]:
        """请求优先，其次是触发词（一条文本只归一类，避免"既是请求又是命令"的歧义）。"""
        return self.detect_request(text) or self.detect_intent(text)

    def classify_result(self, text: Any) -> Optional[dict]:
        """识别结果广播（我们发起的请求被接受/拒绝）——只用于审计。"""
        raw = str(text or "").strip()
        if not raw:
            return None
        for outcome, patterns in DEFAULT_RESULT_PATTERNS.items():
            match = self._match(patterns, raw)
            if match:
                return {"kind": "result", "outcome": outcome,
                        "player": str(match.groupdict().get("player") or "").strip(),
                        "text": raw}
        return None


class TpaCoordinator:
    """把"识别 → 策略 → 下发（指令或控件点击）→ 审计"串起来。

    外部依赖全部注入，方便在没有 wire 的测试里完整驱动：

    - `command(tool, args)`：走 `CommandGate` 白名单通道下发等价指令（路径 A）；
    - `click(match)`：发 Java 命令 `click_screen_button {match}`（路径 B，Java 侧实现）；
    - `resolve(name)`：玩家名解析（`resolve_player` 的包装，目标必须在线且无歧义）。
    """

    def __init__(self, detector: TpaDetector, policy_provider: Optional[Callable[[], dict]] = None,
                 *, command: Optional[Callable[[str, list], dict]] = None,
                 click: Optional[Callable[[str], dict]] = None,
                 resolve: Optional[Callable[[str], dict]] = None,
                 audit: Optional[Callable[[dict], None]] = None,
                 clock: Callable[[], float] = time.time):
        self.detector = detector
        self._policy_provider = policy_provider
        self._command = command
        self._click = click
        self._resolve = resolve
        self._audit_sink = audit
        self._clock = clock
        self._audit: list[dict] = []

    # ── 审计 ──

    def _record(self, row: dict) -> None:
        entry = dict(row)
        entry.setdefault("ts", self._clock())
        self._audit.append(entry)
        del self._audit[:-50]
        if self._audit_sink is not None:
            try:
                self._audit_sink(entry)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[Tpa] 审计 sink 失败: %s", exc)

    def audit_log(self, n: int = 10) -> list[dict]:
        return self._audit[-int(n):] if n else list(self._audit)

    # ── 策略 ──

    def policy(self) -> dict[str, Any]:
        raw: Any = None
        if self._policy_provider is not None:
            try:
                raw = self._policy_provider()
            except Exception as exc:  # noqa: BLE001
                logger.warning("[Tpa] 读取 tpa 策略失败，用默认: %s", exc)
        return normalize_tpa_policy(raw)

    # ── 主入口 ──

    def handle_request(self, text: Any, *, tier: str = "guest", via: str = "auto",
                       requester: str = "", server_key: str = "") -> dict:
        """收到一个 TPA 请求：按策略决定接受 / 先问 / 拒绝。

        `via`：`chat`（聊天里可点，路径 A）/ `popup`（服务端弹窗，路径 B）/ `auto`
        （由配置里的 `path` 决定，默认按事件来源判定）。
        """
        detected = self.detector.detect_request(text)
        if detected is None:
            return {"action": "none", "reason": "not_detected"}
        player = str(detected.get("player") or requester or "").strip()
        return self._act(player, tier=tier, via=via, server_key=server_key,
                         source="request", text=str(text or ""))

    def respond(self, action: str, *, player: str = "", tier: str = "guest",
                via: str = "auto", server_key: str = "", text: str = "") -> dict:
        """显式响应（模型调用 `tpa_respond`，或识别到我们这边的人说"接受/拒绝"）。

        认不出的动作**不猜** —— 直接拒（`bad_action`）。
        """
        raw = str(action or "").strip().lower()
        if raw in {"accept", "同意", "接受", "allow", "approve"}:
            chosen = "accept"
        elif raw in {"deny", "拒绝", "decline", "reject", "refuse"}:
            chosen = "deny"
        else:
            return {"action": "none", "reason": "bad_action"}
        return self._act(player, tier=tier, via=via, server_key=server_key,
                         source="explicit", text=str(text or ""), force=chosen)

    def initiate(self, player: str, *, tier: str = "guest", server_key: str = "") -> dict:
        """主动发起 tpa（默认仅 master；guest 不行）。"""
        policy = self.policy()
        if not may_initiate(policy, tier):
            self._record({"kind": "initiate", "player": player, "tier": tier,
                          "outcome": "refused", "reason": "tier_not_allowed"})
            return {"action": "none", "reason": "tier_not_allowed"}
        resolved = self._resolve_target(player)
        if not resolved.get("ok"):
            self._record({"kind": "initiate", "player": player, "tier": tier,
                          "outcome": "refused", "reason": resolved.get("reason", "not_found")})
            return {"action": "none", "reason": resolved.get("reason", "not_found"),
                    "candidates": resolved.get("candidates", [])}
        result = self._send_command("tpa", [resolved["name"]], player=resolved["name"])
        self._record({"kind": "initiate", "player": resolved["name"], "tier": tier,
                      "outcome": "sent" if result.get("ok") else "blocked",
                      "blocked": result.get("blocked", ""), "server_key": server_key})
        return {"action": "initiate", "ok": bool(result.get("ok")), **result}

    # ── 内部 ──

    def _act(self, player: str, *, tier: str, via: str, server_key: str, source: str,
             text: str, force: str = "") -> dict:
        policy = self.policy()
        policy_decision = decision_for(policy, tier)
        path = self._resolve_path(policy, via)
        if force:
            # 显式响应（模型调 tpa_respond / 识别到我们这边的人说"接受/拒绝"）：
            # guest 仍然不许（默认拒绝且不动作），master/friend 按说的做。
            if policy_decision == TPA_DENY:
                self._record({"kind": source, "player": player, "tier": tier, "path": path,
                              "outcome": "refused", "reason": "tier_not_allowed",
                              "text": str(text or "")[:200]})
                return {"action": "refused", "player": player, "tier": tier, "path": path,
                        "reason": "tier_not_allowed"}
            action = force
        elif policy_decision == TPA_ASK:
            # 先问：不自动动作（由人/模型随后调 tpa_respond），但审计留痕。
            self._record({"kind": source, "player": player, "tier": tier, "path": path,
                          "outcome": "ask", "text": str(text or "")[:200]})
            return {"action": "ask", "player": player, "tier": tier, "path": path}
        elif policy_decision == TPA_DENY:
            # 拒绝 ＝ **不动作**（guest 的口径：拒绝且不去点，也不发指令）。
            self._record({"kind": source, "player": player, "tier": tier, "path": path,
                          "outcome": "refused", "text": str(text or "")[:200]})
            return {"action": "refused", "player": player, "tier": tier, "path": path}
        else:
            action = TPA_ACCEPT

        resolved = self._resolve_target(player)
        if not resolved.get("ok"):
            self._record({"kind": source, "player": player, "tier": tier, "path": path,
                          "outcome": "unresolved", "reason": resolved.get("reason", "not_found")})
            return {"action": "none", "reason": resolved.get("reason", "not_found"),
                    "candidates": resolved.get("candidates", [])}

        accept = action == TPA_ACCEPT
        if path == "popup":
            result = self._send_click(accept, player=resolved["name"])
        else:
            config = self.detector.config()
            tool = config["accept_command"] if accept else config["deny_command"]
            result = self._send_command(tool, [resolved["name"]], player=resolved["name"])
        self._record({"kind": source, "player": resolved["name"], "tier": tier,
                      "path": path, "action": action,
                      "outcome": ("accepted" if accept else "denied")
                      if result.get("ok") else "blocked",
                      "blocked": result.get("blocked", ""), "server_key": server_key,
                      "text": str(text or "")[:200]})
        return {"action": action, "player": resolved["name"], "path": path, **result}

    def _resolve_path(self, policy: dict[str, Any], via: str) -> str:
        """路径选择：配置优先（chat/popup 强制），auto 时按事件来源判定。"""
        configured = str(policy.get("path") or "auto")
        if configured in {"chat", "popup"}:
            return configured
        return "popup" if str(via or "").strip().lower() == "popup" else "chat"

    def _resolve_target(self, player: str) -> dict:
        if self._resolve is None:
            return {"ok": False, "reason": "no_resolver", "candidates": []}
        try:
            return self._resolve(str(player or ""))
        except Exception as exc:  # noqa: BLE001
            logger.warning("[Tpa] 玩家解析失败: %s", exc)
            return {"ok": False, "reason": "resolve_error", "candidates": []}

    def _send_command(self, tool: str, args: list, *, player: str = "") -> dict:
        if self._command is None:
            return {"ok": False, "blocked": "no_command_channel"}
        try:
            result = self._command(str(tool), list(args))
        except Exception as exc:  # noqa: BLE001
            logger.warning("[Tpa] 下发指令失败 %s: %s", tool, exc)
            return {"ok": False, "blocked": "exception"}
        return result if isinstance(result, dict) else {"ok": False, "blocked": "bad_result"}

    def _send_click(self, accept: bool, *, player: str = "") -> dict:
        """路径 B：在已打开的界面里做控件级点击（Java 命令 `click_screen_button`）。"""
        if self._click is None:
            return {"ok": False, "blocked": "no_click_channel"}
        config = self.detector.config()
        buttons = config["accept_buttons"] if accept else config["deny_buttons"]
        match = str((buttons or [""])[0] or "")
        if not match:
            return {"ok": False, "blocked": "no_button_match"}
        try:
            result = self._click(match)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[Tpa] 点击界面按钮失败 %s: %s", match, exc)
            return {"ok": False, "blocked": "exception"}
        return result if isinstance(result, dict) else {"ok": False, "blocked": "bad_result"}
