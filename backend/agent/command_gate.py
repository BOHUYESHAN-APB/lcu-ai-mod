"""服务器指令闸门 —— 白名单（唯一来源）+ 硬黑名单 + 高风险二次确认。

用户口径（原话）：

    「有些指令**绝对不可以**用：kill、kick 之类。」

规则（顺序即优先级，**硬黑名单在配置之上**）：

1. `.local/commands.json` 是**唯一**白名单来源，**默认空**（没勾选的一律拒 ✗，
   如实回 `blocked=not_whitelisted`）。白名单按服/全局两级勾选。
2. 硬黑名单写死在代码里（`HARD_BLACKLIST`）：kill / kick / ban / … —— **配置也放不开**，
   UI 不可勾选；归一化时就把它们从白名单里剔掉。
3. 高风险指令（setblock / fill / summon / give / tp / …）首调用只返回 `needs_confirm`
   与一个**一次性**确认标识，二次带着它才真的执行；两端都记审计。
4. 指令一律经**单一工具口** `run_server_command {tool, args}` 下发，别的路径不发指令。

本模块只做**判定与记账**（不碰 socket）：真正下发由 session/planner 调用
`Skills.run_command` 完成 —— 这样它能在没有 wire 的测试里被完整驱动。
"""

from __future__ import annotations

import copy
import json
import logging
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger("command_gate")

# 唯一白名单来源：与 `.local/permissions.json`、`.local/persona.json` 同级。
DEFAULT_COMMANDS_PATH = Path(__file__).parent.parent / ".local" / "commands.json"

# 硬黑名单：**代码里硬拒，配置也无法放开**（用户："有些指令绝对不可以用"）。
# 全是"会让服务器/玩家不可用"的管理指令：封禁、踢人、夺权、停机、关自动保存。
HARD_BLACKLIST = frozenset({
    "kill", "kick", "ban", "ban-ip", "pardon", "op", "deop", "stop", "restart",
    "whitelist", "difficulty", "gamerule", "save-off", "save-all",
})

# 高风险：会不可逆地改世界/物品/时间，首调用先要一次确认。
HIGH_RISK = frozenset({
    "setblock", "fill", "clone", "summon", "give", "tp", "effect", "time", "weather",
})

# 单个 token 允许的字符：不认的东西（空格、引号、`{}[]` NBT、`@` 选择器、控制字符）一律拒。
# `~ ^ + -` 留着：`~ ~ ~` 这种相对坐标是原版指令的常规写法（少了它 setblock/fill 没法用），
# 它们本身不构成注入面（NBT/选择器需要的花括号与 `@` 仍然被挡）。
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_:.\-~^+]+$")

# 私信族：只有它们允许"自由文本"作为最后一个参数（消息正文天然带空格）。
WHISPER_FAMILIES = frozenset({"msg", "tell", "w", "whisper", "pm", "m"})

# 自由文本里禁止控制字符（换行/回车会把一条指令劈成两条 —— 这正是注入的手法）。
_FREE_TEXT_RE = re.compile(r"^[^\x00-\x1f\x7f]+$")

# 确认标识的有效期（秒）；过期即作废（免得一句"确认"被无限期复用）。
DEFAULT_CONFIRM_TTL = 60.0

# 审计环形缓冲长度：够查"刚才那条指令为什么没发出去"。
AUDIT_MAX = 50

# 认不出的白名单条目在归一化时被剔掉的原因（供 UI 显示"勾了但没用"）。
BLOCKED_REASON_HARD_BLACKLIST = "hard_blacklist"
BLOCKED_REASON_NOT_WHITELISTED = "not_whitelisted"


def _as_name(value: Any) -> str:
    """指令名归一化：去首尾空白与所有前导 `/`，大小写折叠；命名空间前缀保留在名字里。"""
    text = str(value or "").strip()
    while text.startswith("/"):
        text = text[1:].strip()
    return text.casefold()


def _family_of(name: str) -> str:
    """取指令族名：`minecraft:kill` → `kill`（命名空间不影响黑名单判定）。"""
    return _as_name(name).split(":")[-1]


def _truthy(value: Any, default: bool = True) -> bool:
    """宽松布尔（UI 回包可能是 true/1/"是"/"on"）。"""
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
    return default


# 条目没写 `tiers` 时的默认允许层级：master/friend 可以，**guest 一律不行**
# （设计文档 §3 的表：guest 连"使用其它白名单指令"都是 ✗）。
DEFAULT_COMMAND_TIERS = ("master", "friend")
# 私信族的默认层级多一个 guest —— 这是**故意的**：guest 不能被 AI 拿去私信别人，
# 但"回他发来的私信"必须能发（否则"guest 发来的私信它可以回 ✓"就落不了地）。
# "不能主动私信"那条规则在下面 evaluate 的 §2d 里单独判（reply 才放行）。
DEFAULT_WHISPER_TIERS = ("master", "friend", "guest")
_TIER_NAMES = ("master", "friend", "guest")


def default_tiers_for(family: str) -> list[str]:
    """该指令族没登记时的默认层级。"""
    if _family_of(family) in WHISPER_FAMILIES:
        return list(DEFAULT_WHISPER_TIERS)
    return list(DEFAULT_COMMAND_TIERS)


def _as_tiers(value: Any, family: str = "") -> list[str]:
    """允许层级解析：接受字符串/列表；认不出的丢掉；空的回该指令族的默认。"""
    fallback = default_tiers_for(family)
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple, set)):
        return fallback
    tiers: list[str] = []
    for item in value:
        name = str(item or "").strip().lower()
        if name in _TIER_NAMES and name not in tiers:
            tiers.append(name)
    return tiers or fallback


def _as_cooldown(value: Any) -> float:
    """冷却秒数：非法/负数回 0（0 = 不限频）。"""
    if isinstance(value, bool):
        return 0.0
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return 0.0


def _entry(name: Any, raw: Any = None) -> Optional[dict[str, Any]]:
    """把一条白名单条目归一化；名字非法或落在硬黑名单里 → None（**配置也放不开**）。"""
    clean = _as_name(name)
    if not clean:
        return None
    if _family_of(clean) in HARD_BLACKLIST:
        logger.warning("[CommandGate] 忽略硬黑名单指令 %r（配置无法放开）", clean)
        return None
    if not _TOKEN_RE.fullmatch(clean):
        logger.warning("[CommandGate] 忽略非法白名单条目 %r", clean)
        return None
    source = raw if isinstance(raw, dict) else {}
    return {
        "name": clean,
        "usage": str(source.get("usage") or ""),
        "desc": str(source.get("desc") or ""),
        "tiers": _as_tiers(source.get("tiers"), clean),
        "args": source.get("args") if isinstance(source.get("args"), dict) else {},
        "cooldown": _as_cooldown(source.get("cooldown")),
        "enabled": _truthy(source.get("enabled"), True),
    }


def _as_entries(value: Any) -> list[dict[str, Any]]:
    """条目列表解析：接受 `"home"` / `{"name": "home", ...}` 混写，保序去重。"""
    if isinstance(value, (str, dict)):
        value = [value]
    if not isinstance(value, (list, tuple, set)):
        return []
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in value:
        if isinstance(item, str):
            parsed = _entry(item)
        elif isinstance(item, dict):
            parsed = _entry(item.get("name") or item.get("command"), item)
        else:
            parsed = None
        if parsed and parsed["name"] not in seen:
            seen.add(parsed["name"])
            entries.append(parsed)
    return entries


def normalize_commands_config(raw: Any = None) -> dict[str, Any]:
    """把任意来源（文件 / UI 回包）的配置解析成合法结构：缺键取默认、非法值回退。

    白名单的形状（两种都支持，与设计文档 `docs/dev/SERVER_COMMANDS.md` 对齐）：

    - `commands`：条目对象 `{name, usage, desc, tiers, args, cooldown, enabled}` —— 全局；
    - `servers`：`{server_key: [条目或名字]}` —— 只对那台服生效；
    - `global`：纯名字列表（同 `commands` 的简写，保留向后兼容）。
    """
    data = raw if isinstance(raw, dict) else {}
    entries = _as_entries(data.get("commands"))
    for name in _as_entries(data.get("global")):
        if name["name"] not in {item["name"] for item in entries}:
            entries.append(name)

    servers: dict[str, list[dict[str, Any]]] = {}
    servers_raw = data.get("servers")
    if isinstance(servers_raw, dict):
        for key, value in servers_raw.items():
            parsed = _as_entries(value)
            if parsed:
                servers[str(key).strip()] = parsed

    whisper = _as_name(data.get("whisper_family")) or "msg"
    if whisper not in {"msg", "tell", "w"}:
        whisper = "msg"
    ttl = data.get("confirm_ttl_seconds")
    try:
        ttl_seconds = max(1.0, min(600.0, float(ttl)))
    except (TypeError, ValueError):
        ttl_seconds = DEFAULT_CONFIRM_TTL
    return {
        "version": 1,
        "enabled": _truthy(data.get("enabled"), True),
        "commands": entries,
        # 旧视图：全局条目的名字（UI 的"勾选"列表可以继续读它）。
        "global": [item["name"] for item in entries],
        "servers": servers,
        "whisper_family": whisper,
        # 私信接收识别规则（交给 `WhisperDetector` 归一化；这里只做透传）。
        "whisper": data.get("whisper") if isinstance(data.get("whisper"), dict) else {},
        "tpa": data.get("tpa") if isinstance(data.get("tpa"), dict) else {},
        "confirm_ttl_seconds": ttl_seconds,
    }


def _apply_patch(current: dict[str, Any], patch: Any) -> dict[str, Any]:
    """把 UI 补丁叠到当前配置上：未知键忽略，servers 逐服合并、tpa/whisper 逐键合并。"""
    merged = copy.deepcopy(current)
    if not isinstance(patch, dict):
        return merged
    # `commands` 与 `global` 是同一份全局白名单的两种写法：谁给了谁说了算。
    if "commands" in patch and "global" not in patch:
        merged["global"] = []
    if "global" in patch and "commands" not in patch:
        merged["commands"] = []
    for key, value in patch.items():
        if key not in merged:
            continue
        if key == "servers" and isinstance(value, dict):
            bucket = dict(merged.get("servers") or {})
            bucket.update({str(k).strip(): v for k, v in value.items()})
            merged[key] = bucket
        elif key == "tpa" and isinstance(value, dict):
            bucket = dict(merged.get("tpa") or {})
            bucket.update(value)
            merged[key] = bucket
        elif key == "whisper" and isinstance(value, dict):
            bucket = dict(merged.get("whisper") or {})
            bucket.update(value)
            merged[key] = bucket
        else:
            merged[key] = value
    return merged


class CommandsStore:
    """`.local/commands.json` 的读写门面（线程安全，UI 改完 `reload()` 即时生效）。"""

    def __init__(self, path: str | Path = DEFAULT_COMMANDS_PATH):
        self.path = Path(path)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:  # 目录建不出来也要能跑：内存里用默认值继续
            logger.warning("创建指令配置目录失败 %s: %s", self.path.parent, exc)
        self._lock = threading.RLock()
        self._data = self._read()

    def _read(self) -> dict[str, Any]:
        """读盘；缺文件/坏 JSON/不是对象一律回退默认（**默认白名单为空**）。"""
        try:
            if not self.path.exists():
                return normalize_commands_config({})
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("读取指令配置失败，已回退默认 %s: %s", self.path, exc)
            return normalize_commands_config({})
        return normalize_commands_config(raw)

    def _save(self) -> None:
        try:
            self.path.write_text(
                json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as exc:
            logger.warning("写指令配置失败 %s: %s", self.path, exc)

    def get(self) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._data)

    def set(self, payload: Any) -> dict[str, Any]:
        """宽容解析 UI 回包并写盘；返回归一化后的完整配置。"""
        with self._lock:
            self._data = normalize_commands_config(_apply_patch(self._data, payload))
            self._save()
            return copy.deepcopy(self._data)

    def reload(self) -> dict[str, Any]:
        with self._lock:
            self._data = self._read()
            return copy.deepcopy(self._data)

    # ── 查询 ──

    def enabled(self) -> bool:
        with self._lock:
            return bool(self._data["enabled"])

    def whisper_family(self) -> str:
        with self._lock:
            return str(self._data["whisper_family"])

    def tpa_config(self) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._data.get("tpa") or {})

    def entries_for(self, server_key: str = "") -> list[dict[str, Any]]:
        """该服生效的白名单条目（全局 ∪ 本服；同名的以本服为准）。"""
        key = str(server_key or "").strip()
        with self._lock:
            merged: dict[str, dict[str, Any]] = {
                item["name"]: copy.deepcopy(item) for item in self._data["commands"]}
            for candidate, entries in self._data["servers"].items():
                if candidate.casefold() != key.casefold():
                    continue
                for item in entries:
                    merged[item["name"]] = copy.deepcopy(item)
            return list(merged.values())

    def whitelist_for(self, server_key: str = "") -> list[str]:
        """该服生效的白名单名字（只含启用条目；**默认空**）。"""
        return [item["name"] for item in self.entries_for(server_key) if item.get("enabled", True)]

    def entry_for(self, family: str, server_key: str = "") -> Optional[dict[str, Any]]:
        """按指令族找条目（`minecraft:tp` 与 `tp` 视为同一个）。"""
        wanted = _family_of(family)
        for item in self.entries_for(server_key):
            if _family_of(item["name"]) == wanted:
                return item
        return None

    def tiers_for(self, family: str, server_key: str = "") -> list[str]:
        """该指令允许的层级（没登记 → 用该指令族的默认：私信族含 guest，其余不含）。"""
        item = self.entry_for(family, server_key)
        return list(item["tiers"]) if item else default_tiers_for(family)

    def cooldown_for(self, family: str, server_key: str = "") -> float:
        """该指令的冷却秒数（0 = 不限频）。"""
        item = self.entry_for(family, server_key)
        return float(item["cooldown"]) if item else 0.0

    def is_whitelisted(self, family: str, server_key: str = "") -> bool:
        wanted = _family_of(family)
        return any(_family_of(name) == wanted for name in self.whitelist_for(server_key))


class CommandGate:
    """判定"这条服务器指令能不能发" + 一次性确认 + 审计（**不碰 socket**）。"""

    def __init__(self, store: CommandsStore, audit: Optional[Callable[[dict], None]] = None,
                 clock: Callable[[], float] = time.time):
        self.store = store
        self._audit_sink = audit if callable(audit) else None
        self._clock = clock if callable(clock) else time.time
        self._lock = threading.RLock()
        # 一次性确认：token → {command, family, actor, tier, expires}
        self._pending: dict[str, dict] = {}
        # 冷却表：family → 上次真正下发的时刻。
        self._last_run: dict[str, float] = {}
        self._audit: list[dict] = []

    # ── 审计 ──

    def _record(self, row: dict) -> None:
        row = dict(row)
        row.setdefault("ts", time.time())
        with self._lock:
            self._audit.append(row)
            del self._audit[:-AUDIT_MAX]
        if self._audit_sink is not None:
            try:
                self._audit_sink(row)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[CommandGate] 审计 sink 失败: %s", exc)

    def audit_log(self, n: int = 10) -> list[dict]:
        with self._lock:
            rows = list(self._audit)
        return rows[-int(n):] if n else rows

    # ── 主入口 ──

    @staticmethod
    def _blocked(reason: str, **extra: Any) -> dict:
        return {"ok": False, "blocked": reason, "reason": reason, **extra}

    def evaluate(self, tool: Any, args: Any = None, *, server_key: str = "",
                 actor: str = "", tier: str = "guest", confirm: str = "",
                 free_text: str = "", reply: bool = False) -> dict:
        """判定一条 `run_server_command {command, args}` 调用。

        返回 `{"ok": True, "command": "/tpaccept Alice", ...}` 或
        `{"ok": False, "blocked": "not_whitelisted"|"hard_blacklist"|"needs_confirm"|...}`。

        `free_text` 只给私信族用（`msg`/`tell`/`w`）：消息正文天然带空格，不能按 token
        校验；它单独按"无控制字符"校验 —— 换行会把一条指令劈成两条，那正是要防的注入。

        `reply=True` 表示这条私信是**回**对方发来的（不是"去私信别人"）—— 只有这一种
        情况允许 guest 触发的私信；主动发起在 §2d 被拒。
        """
        family = _family_of(tool)
        row_base = {"tool": str(tool or ""), "args": args, "actor": str(actor or ""),
                    "tier": str(tier or "guest"), "server_key": str(server_key or "")}
        if not family:
            self._record({**row_base, "outcome": "blocked", "blocked": "empty"})
            return self._blocked("empty")
        if not self.store.enabled():
            self._record({**row_base, "outcome": "blocked", "blocked": "disabled"})
            return self._blocked("disabled")

        tokens = self._tokens(family, args)
        if tokens is None:
            self._record({**row_base, "outcome": "blocked", "blocked": "invalid"})
            return self._blocked("invalid")

        if free_text:
            text = str(free_text).strip()
            if family not in WHISPER_FAMILIES or not text or not _FREE_TEXT_RE.fullmatch(text):
                self._record({**row_base, "outcome": "blocked", "blocked": "invalid_text"})
                return self._blocked("invalid_text")
            tokens.append(text)

        command = "/" + " ".join(tokens)

        # 1) 硬黑名单：配置也放不开（先于白名单，白名单勾了也没用）。
        if family in HARD_BLACKLIST:
            self._record({**row_base, "command": command, "outcome": "blocked",
                          "blocked": BLOCKED_REASON_HARD_BLACKLIST})
            return self._blocked(BLOCKED_REASON_HARD_BLACKLIST, command=command)

        # 2) 白名单：**默认空**，没勾选一律拒。
        if not self.store.is_whitelisted(family, server_key):
            self._record({**row_base, "command": command, "outcome": "blocked",
                          "blocked": BLOCKED_REASON_NOT_WHITELISTED, "family": family})
            return self._blocked(BLOCKED_REASON_NOT_WHITELISTED, command=command, family=family)

        # 2b) 层级：条目上写的 `tiers` 谁都不许越（没写的默认 master+friend，guest 一律不行）。
        allowed_tiers = self.store.tiers_for(family, server_key)
        if str(tier or "guest").strip().lower() not in allowed_tiers:
            self._record({**row_base, "command": command, "family": family,
                          "outcome": "blocked", "blocked": "tier_not_allowed",
                          "allowed_tiers": allowed_tiers})
            return self._blocked("tier_not_allowed", command=command, family=family,
                                 tiers=allowed_tiers)

        # 2c) 私信：guest **不能**借 AI 去私信别人；只有"回他发来的"才放行（`reply`）。
        if (_family_of(family) in WHISPER_FAMILIES and not reply
                and str(tier or "guest").strip().lower() == "guest"):
            self._record({**row_base, "command": command, "family": family,
                          "outcome": "blocked", "blocked": "tier_not_allowed",
                          "reason_detail": "guest_cannot_initiate"})
            return self._blocked("tier_not_allowed", command=command, family=family,
                                 detail="guest_cannot_initiate")

        # 2d) 冷却：该指令配了 cooldown 才生效（默认 0 = 不限频）。
        cooldown = self.store.cooldown_for(family, server_key)
        if cooldown > 0.0:
            remaining = self._cooldown_remaining(family, cooldown)
            if remaining > 0.0:
                self._record({**row_base, "command": command, "family": family,
                              "outcome": "blocked", "blocked": "cooldown",
                              "remaining": round(remaining, 3)})
                return self._blocked("cooldown", command=command, family=family,
                                     remaining=round(remaining, 3))

        # 3) 高风险：首调用只发确认，二次带标识才执行（两端都进审计）。
        if family in HIGH_RISK:
            pending = self._consume_confirm(confirm, command)
            if pending is None:
                token = self._issue_confirm(command, family, actor, tier)
                self._record({**row_base, "command": command, "family": family,
                              "outcome": "needs_confirm", "confirm": token})
                return self._blocked("needs_confirm", command=command, family=family,
                                     confirm=token)
            self._record({**row_base, "command": command, "family": family,
                          "outcome": "confirmed", "confirm": confirm,
                          "confirmed_by": pending.get("actor", "")})

        self._mark_run(family)
        self._record({**row_base, "command": command, "family": family, "outcome": "allowed"})
        return {"ok": True, "command": command, "family": family, "tokens": tokens,
                "server_key": str(server_key or "")}

    def _cooldown_remaining(self, family: str, cooldown: float) -> float:
        with self._lock:
            last = float(self._last_run.get(_family_of(family), 0.0))
        return max(0.0, last + float(cooldown) - float(self._clock()))

    def _mark_run(self, family: str) -> None:
        with self._lock:
            self._last_run[_family_of(family)] = float(self._clock())

    def _tokens(self, family: str, args: Any) -> Optional[list[str]]:
        """把 (tool, args) 折成指令 token 列表；任何非法字符一律 None（**不猜**）。"""
        tokens = [family]
        if args is None:
            return tokens
        if isinstance(args, str):
            raw = args.split()
        elif isinstance(args, dict):
            raw = [value for value in args.values()]
        elif isinstance(args, (list, tuple, set)):
            raw = list(args)
        else:
            raw = [args]
        for item in raw:
            if item is None:
                continue
            if isinstance(item, bool) or isinstance(item, (dict, list, tuple, set)):
                return None
            if isinstance(item, float) and item.is_integer():
                item = int(item)
            text = str(item).strip()
            if not text:
                continue
            if text.startswith("@") or not _TOKEN_RE.fullmatch(text):
                return None
            tokens.append(text)
        return tokens

    # ── 一次性确认 ──

    def _issue_confirm(self, command: str, family: str, actor: str, tier: str) -> str:
        token = uuid.uuid4().hex
        with self._lock:
            self._prune_confirm()
            self._pending[token] = {
                "command": command, "family": family, "actor": str(actor or ""),
                "tier": str(tier or ""),
                "expires": self._clock() + float(self.store.get().get(
                    "confirm_ttl_seconds", DEFAULT_CONFIRM_TTL)),
            }
        return token

    def _consume_confirm(self, token: str, command: str) -> Optional[dict]:
        """取用并作废一次性确认：token 空/过期/对不上命令一律 None（**不猜**）。"""
        key = str(token or "").strip()
        if not key:
            return None
        with self._lock:
            self._prune_confirm()
            row = self._pending.pop(key, None)
        if not row:
            return None
        if str(row.get("command")) != str(command):
            return None
        return row

    def _prune_confirm(self) -> None:
        now = self._clock()
        for key in [k for k, row in self._pending.items() if float(row.get("expires", 0.0)) < now]:
            self._pending.pop(key, None)

    def pending_confirms(self) -> list[dict]:
        with self._lock:
            self._prune_confirm()
            return [{"token": k, **v} for k, v in self._pending.items()]
