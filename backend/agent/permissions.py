"""Permissions — 身份分层 / 回复频率门 / 成本控制。

设计要点（docs/dev/M3-19_CONVERSATION.md §4）：**不要把等级判断写进决策主链的 if**。
tier 只决定三件事 —— ① 能否下指令 ② 任务优先级 ③ 记忆可见性 —— 全部由数据表
`TierPolicy` 表达；决策链只查表，改权限就是改 `.local/permissions.json`，不动代码。

一条进来的消息依次过这些门：
  L0 硬过滤  ban 词/正则命中 → 丢弃整条
  L1 身份解析 sender → master / friend / guest（大小写与首尾空格不敏感）
  L2 频率门   effective = base(server_mode) * session_adjust
              threshold = max(1, ceil(1/min(1,effective)))，pending 累加 + 空窗补偿
  L3 点名旁路 被 @/点名 → 跳过频率门与抗抖，直接 act
  L4 语义门  花 token 的那一步由调用方（session）执行；本模块只回答"该不该进 L4"
  L5 冷却    语义门返回 no_action 后静默 cooldown_seconds（默认 8s），期间被点名仍可旁路

`effective <= 0` 就是**静默模式**：只记录、不回、零 token（public 服的 guest 默认如此，
但消息照进对话记忆）；`public_reply_guests=true` 时给 guest 一个非零 base，否则那个开关
等于没接线。

审计：`submit`/`poll` 每次返回前都记一行 `effective/threshold/pending/decision/reason`
（MaiBot 第 4 条建议）——用户问"他为什么不理我"时查审计，不用猜。

落盘一律 try/except：权限文件坏了只 warning，绝不把后端主循环拖下水。
"""

from __future__ import annotations

import copy
import json
import logging
import math
import random
import re
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger("permissions")


TIER_MASTER, TIER_FRIEND, TIER_GUEST = "master", "friend", "guest"
TIERS: tuple[str, str, str] = (TIER_MASTER, TIER_FRIEND, TIER_GUEST)

SERVER_MODE_PRIVATE, SERVER_MODE_PUBLIC = "private", "public"

DEFAULT_PERMISSIONS_PATH = Path(__file__).parent.parent / ".local" / "permissions.json"

# 三个时间旋钮的默认值（都能被 permissions.json 里的同名键覆盖）
DEFAULT_COOLDOWN_SECONDS = 8.0
DEFAULT_DEBOUNCE_SECONDS = 1.0
DEFAULT_WINDOW_SECONDS = 30.0

# 公开服 guest 的"可聊"档位：public_reply_guests=true 时用它顶掉 0.0
PUBLIC_GUEST_REPLY_RATE = 0.5

# 会话调节系数（UI 滑杆）的上界。4 已经等于"每条都回"，再大没有语义。
SESSION_ADJUST_MAX = 4.0

# 审计环形缓冲长度：50 条足够查"刚才为什么没理他"，再长就是内存垃圾
AUDIT_MAX = 50

# tier → 数据表（决策链只查这两张表，不写 if）
TASK_PRIORITIES: dict[str, int] = {
    TIER_MASTER: 80,
    TIER_FRIEND: 50,
    TIER_GUEST: 20,
}
# 记忆可见性：master/friend 能看到全部记忆（含私聊）；guest 只看公开部分
MEMORY_VISIBILITY: dict[str, str] = {
    TIER_MASTER: "all",
    TIER_FRIEND: "all",
    TIER_GUEST: "public",
}

DEFAULT_SESSION_ADJUST = 1.0


def _default_permissions() -> dict[str, Any]:
    """默认权限：private 服（人人可聊）+ 公开服 guest 静默。

    默认**不绑定任何具体角色/服务器**：master/friends 名单为空，靠 UI 填。
    """
    return {
        "master": [],
        "friends": [],
        "server_mode": SERVER_MODE_PRIVATE,
        "public_reply_guests": False,
        "session_adjust": DEFAULT_SESSION_ADJUST,
        "base_rates": {
            SERVER_MODE_PRIVATE: {TIER_MASTER: 1.0, TIER_FRIEND: 1.0, TIER_GUEST: 1.0},
            SERVER_MODE_PUBLIC: {TIER_MASTER: 1.0, TIER_FRIEND: 0.7, TIER_GUEST: 0.0},
        },
        "cooldown_seconds": DEFAULT_COOLDOWN_SECONDS,
        "debounce_seconds": DEFAULT_DEBOUNCE_SECONDS,
        "window_seconds": DEFAULT_WINDOW_SECONDS,
        "ban_patterns": [],
    }


_KNOWN_KEYS = frozenset(_default_permissions().keys())


# ── 宽容解析 ────────────────────────────────────────────────────
# UI / 手写 JSON 什么都可能传进来，这里统一"认不出就回退默认"，绝不当场抛。

def _as_float(value: Any, default: Optional[float] = None) -> Optional[float]:
    """数值解析：非法、NaN/Inf、布尔一律回退 default。

    bool 单独挡掉：`float(True) == 1.0` 会让误传的开关悄悄变成一个频率值。
    """
    if isinstance(value, bool):
        return default
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(parsed):
        return default
    return parsed


def _as_rate(value: Any, default: float) -> float:
    """频率/系数：夹到 [0, SESSION_ADJUST_MAX]，非法回退 default。"""
    parsed = _as_float(value, default)
    return min(max(float(parsed), 0.0), SESSION_ADJUST_MAX)


def _as_seconds(value: Any, default: float) -> float:
    """秒数：负数时间没有意义，夹到 >=0，非法回退 default。"""
    parsed = _as_float(value, default)
    return max(float(parsed), 0.0)


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


def _as_server_mode(value: Any, default: Optional[str]) -> Optional[str]:
    """服模式解析：只认 private/public（含中文写法）；认不出返回 default。"""
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"public", "公开", "公共"}:
            return SERVER_MODE_PUBLIC
        if text in {"private", "私人", "私有"}:
            return SERVER_MODE_PRIVATE
    return default


def _as_tier(value: Any) -> str:
    """tier 名归一化；认不出的一律当 guest（**默认拒绝**，而不是默认放行）。"""
    text = str(value or "").strip().lower()
    return text if text in TIERS else TIER_GUEST


def _as_names(value: Any) -> list[str]:
    """玩家名单：接受单个字符串或列表，去首尾空格、按大小写不敏感去重、保序。"""
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple, set)):
        return []
    names: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str):
            continue
        name = item.strip()
        if not name:
            continue
        key = name.casefold()
        if key in seen:
            continue
        seen.add(key)
        names.append(name)
    return names


def _as_patterns(value: Any) -> list[str]:
    """ban 正则列表：只留非空字符串。

    空串/纯空白会 `re.search` 命中一切（等于封掉全部聊天），一律丢掉 ——
    这是 UI 加一行空行的常见误配，代价却是"谁都不理"。
    """
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple, set)):
        return []
    return [item for item in value if isinstance(item, str) and item.strip()]


def _normalize_base_rates(raw: Any) -> dict[str, dict[str, float]]:
    """base_rates 补全成 {private|public: {master|friend|guest: float}}，缺格取默认。"""
    defaults = _default_permissions()["base_rates"]
    table: dict[str, dict[str, float]] = {}
    for mode in (SERVER_MODE_PRIVATE, SERVER_MODE_PUBLIC):
        given = raw.get(mode) if isinstance(raw, dict) else None
        given = given if isinstance(given, dict) else {}
        table[mode] = {
            tier: _as_rate(given.get(tier), defaults[mode][tier]) for tier in TIERS
        }
    return table


def _normalize(raw: Any) -> dict[str, Any]:
    """把任意来源（磁盘 / UI 回包）的配置解析成合法结构：缺键取默认、非法值回退默认。"""
    source = raw if isinstance(raw, dict) else {}
    data = _default_permissions()
    data["master"] = _as_names(source.get("master"))
    data["friends"] = _as_names(source.get("friends"))
    data["server_mode"] = _as_server_mode(source.get("server_mode"), SERVER_MODE_PRIVATE)
    data["public_reply_guests"] = _as_bool(source.get("public_reply_guests"), False)
    data["session_adjust"] = _as_rate(source.get("session_adjust"), DEFAULT_SESSION_ADJUST)
    data["base_rates"] = _normalize_base_rates(source.get("base_rates"))
    data["cooldown_seconds"] = _as_seconds(
        source.get("cooldown_seconds"), DEFAULT_COOLDOWN_SECONDS)
    data["debounce_seconds"] = _as_seconds(
        source.get("debounce_seconds"), DEFAULT_DEBOUNCE_SECONDS)
    data["window_seconds"] = _as_seconds(
        source.get("window_seconds"), DEFAULT_WINDOW_SECONDS)
    data["ban_patterns"] = _as_patterns(source.get("ban_patterns"))
    return data


def _apply_patch(current: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """把 UI 补丁叠到当前配置上：未知键忽略，base_rates 逐层合并（UI 常常只改一格）。"""
    merged = copy.deepcopy(current)
    for key in _KNOWN_KEYS:
        if key not in patch:
            continue
        value = patch[key]
        if key == "base_rates" and isinstance(value, dict):
            rates: Any = merged.get("base_rates")
            rates = copy.deepcopy(rates) if isinstance(rates, dict) else {}
            for mode, tier_map in value.items():
                if isinstance(tier_map, dict):
                    bucket = rates.get(mode)
                    bucket = dict(bucket) if isinstance(bucket, dict) else {}
                    bucket.update(tier_map)
                    rates[mode] = bucket
                else:
                    rates[mode] = tier_map
            merged[key] = rates
        else:
            merged[key] = value
    return merged


# ── 权限表 ──────────────────────────────────────────────────────

@dataclass(frozen=True)
class TierPolicy:
    """某个 tier 的全部权限，**查表得到**（决策链里不写权限 if）。"""

    tier: str
    can_command: bool          # private 服人人可；public 服仅 master + friend
    task_priority: int         # master 80 / friend 50 / guest 20
    memory_visibility: str     # "all"（含私聊）/ "public"（只看公开部分）


@dataclass
class GateDecision:
    """一条消息（或一次 poll）的放行结论 + 当时的水位，供调用方与审计使用。"""

    decision: str              # "act" | "wait" | "silent" | "cooldown" | "drop" | "none"
    reason: str
    tier: str
    effective: float
    threshold: int
    pending: int
    can_command: bool
    memory_visibility: str
    mentioned: bool
    event_kind: Optional[str]  # 事件注入时的事件名，普通聊天为 None
    audit: dict                # 落审计表的那一行


class PermissionsStore:
    """`.local/permissions.json` 的读写门面（线程安全）。

    `reload()` 丢掉内存缓存重读盘 —— UI 改完即时生效，不用重启后端
    （用户要求"以上都可在后端配置"）。
    """

    def __init__(self, path: str | Path = DEFAULT_PERMISSIONS_PATH):
        self.path = Path(path)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:  # 目录建不出来也要能跑：内存里用默认值继续
            logger.warning("创建权限目录失败 %s: %s", self.path.parent, exc)
        self._lock = threading.RLock()
        self._data = self._read()

    # ── 读写 ──

    def _read(self) -> dict[str, Any]:
        """读盘；缺文件/坏 JSON/不是对象一律回退默认（不抛）。"""
        try:
            if not self.path.exists():
                return _default_permissions()
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("读取权限配置失败，已回退默认 %s: %s", self.path, exc)
            return _default_permissions()
        if not isinstance(raw, dict):
            logger.warning("权限配置不是对象，已回退默认: %s", self.path)
            return _default_permissions()
        return _normalize(raw)

    def _save(self) -> None:
        try:
            self.path.write_text(
                json.dumps(self._data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as exc:
            logger.warning("写权限配置失败 %s: %s", self.path, exc)

    def get(self) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._data)

    def set(self, payload: dict[str, Any]) -> dict[str, Any]:
        """宽容解析 UI 回包并写盘；返回归一化后的完整配置。

        只认已知键（未知键忽略）、非法值回退默认 —— UI 传什么垃圾都不该让后端
        半失效（比如 master 名单变成字符串，结果谁都不是 master）。
        """
        patch = payload if isinstance(payload, dict) else {}
        with self._lock:
            self._data = _normalize(_apply_patch(self._data, patch))
            self._save()
            return copy.deepcopy(self._data)

    def reload(self) -> dict[str, Any]:
        """丢掉内存缓存重新读盘（UI 保存后调用，让改动即时生效）。"""
        with self._lock:
            self._data = self._read()
            return copy.deepcopy(self._data)

    # ── 身份 ──

    def tier_of(self, name: str) -> str:
        """sender → tier。master > friends > guest；大小写与首尾空格不敏感。

        名单里没有的人一律 guest（**默认拒绝**）：漏配只是"少搭理"，反过来默认
        放行则等于公开服对所有人开闸烧 token。
        """
        key = str(name or "").strip().casefold()
        if not key:
            return TIER_GUEST
        with self._lock:
            masters = self._data["master"]
            friends = self._data["friends"]
        if any(str(entry).strip().casefold() == key for entry in masters):
            return TIER_MASTER
        if any(str(entry).strip().casefold() == key for entry in friends):
            return TIER_FRIEND
        return TIER_GUEST

    # ── 服模式与通道参数 ──

    def server_mode(self) -> str:
        with self._lock:
            return self._data["server_mode"]

    def public_reply_guests(self) -> bool:
        with self._lock:
            return bool(self._data["public_reply_guests"])

    def session_adjust(self) -> float:
        """UI 滑杆的会话级调节系数（ReplyGate 每次实时读它，改完即生效）。"""
        with self._lock:
            return float(self._data["session_adjust"])

    def cooldown_seconds(self) -> float:
        with self._lock:
            return float(self._data["cooldown_seconds"])

    def debounce_seconds(self) -> float:
        with self._lock:
            return float(self._data["debounce_seconds"])

    def window_seconds(self) -> float:
        with self._lock:
            return float(self._data["window_seconds"])

    def ban_patterns(self) -> list[str]:
        with self._lock:
            return list(self._data["ban_patterns"])

    # ── 查表 ──

    def policy(self, tier: str, *, server_mode: Optional[str] = None) -> TierPolicy:
        """该 tier 的三项权限（决策链唯一的权限入口）。"""
        tier = _as_tier(tier)
        mode = _as_server_mode(server_mode, None) or self.server_mode()
        # private 服人人可下指令（用户要求"私人服务器所有人都可以跟他对话"）；
        # public 服只有 master/friend 能动他干活，guest 只能聊/被听。
        can_command = True if mode == SERVER_MODE_PRIVATE else tier in (TIER_MASTER, TIER_FRIEND)
        return TierPolicy(
            tier=tier,
            can_command=can_command,
            task_priority=TASK_PRIORITIES[tier],
            memory_visibility=MEMORY_VISIBILITY[tier],
        )

    def base_rate(self, tier: str, *, server_mode: Optional[str] = None) -> float:
        """该 tier 在当前服的基准回复频率。

        public 服 guest 默认 0.0（静默：只记录不回、零 token）；开了
        `public_reply_guests` 就换成非零档位，否则"公开服也能聊"这个开关等于没接线。
        """
        tier = _as_tier(tier)
        mode = _as_server_mode(server_mode, None) or self.server_mode()
        with self._lock:
            rates = self._data["base_rates"].get(mode) or {}
            value = float(rates.get(tier, 0.0))
            reply_guests = bool(self._data["public_reply_guests"])
        if value <= 0.0 and mode == SERVER_MODE_PUBLIC and tier == TIER_GUEST and reply_guests:
            return PUBLIC_GUEST_REPLY_RATE
        return value

    def task_priority(self, tier: str) -> int:
        """任务权重（任务列表排序用）：master 80 > friend 50 > guest 20。"""
        return TASK_PRIORITIES[_as_tier(tier)]


class ReplyGate:
    """L2/L3/L5 的落地：频率门 + 抗抖 + 点名旁路 + no_action 冷却。

    `submit()` 是每条消息的入口（返回"该怎么处理"），`poll()` 由 tick 调用
    （抗抖窗过完且 pending 达标 → 返回 act，让"等一下再回"的消息真的能发出去）。
    L4 语义门由调用方执行；本类只回答"该不该进 L4"并记审计。
    """

    def __init__(self, permissions: PermissionsStore, clock: Callable[[], float] = time.time,
                 rng: Optional[random.Random] = None):
        self.permissions = permissions
        self._clock = clock if callable(clock) else time.time
        self._rng = rng if rng is not None else random.Random()
        self._lock = threading.RLock()
        # pending 条目 = (到达时间, tier, sender)：tier 让 poll 也能算门槛，sender 让审计可追人
        self._pending: list[tuple[float, str, str]] = []
        self._last_message_at: Optional[float] = None
        self._cooldown_until: float = 0.0
        self._session_adjust_override: Optional[float] = None
        self._audit: deque[dict] = deque(maxlen=AUDIT_MAX)

    # ── 时间与参数 ──

    def _now(self, now: Optional[float] = None) -> float:
        """取时间：调用方给了就用（保证同一条消息只取一次时间，也方便测试压时间）。"""
        if now is not None:
            try:
                return float(now)
            except (TypeError, ValueError):
                pass
        try:
            return float(self._clock())
        except Exception:
            return time.time()

    def set_session_adjust(self, value: float) -> None:
        """会话级系数：夹到 [0, 4]，非法回退 1.0（"能聊"的中性值）。

        只改本次运行；要持久化请写 permissions.json（`PermissionsStore.set`）。
        """
        parsed = _as_float(value, None)
        override = DEFAULT_SESSION_ADJUST if parsed is None else min(
            max(parsed, 0.0), SESSION_ADJUST_MAX)
        with self._lock:
            self._session_adjust_override = override

    def session_adjust(self) -> float:
        """当前系数：显式设置过就用它，否则跟权限文件走（UI 改完即时生效）。"""
        with self._lock:
            override = self._session_adjust_override
        if override is not None:
            return override
        return self.permissions.session_adjust()

    def effective_for(self, tier: str) -> float:
        """effective = base(server_mode) * session_adjust；<=0 即静默模式。"""
        return self.permissions.base_rate(tier) * self.session_adjust()

    @staticmethod
    def threshold_for(effective: float) -> int:
        """几条消息里放行一条：1.0 → 1（每条都进语义门），0.5 → 2，0.34 → 3，0 → 0。

        effective > 1 没有"比每条都回更频繁"的语义，夹到 1；0 表示静默（不由频率门放行）。
        """
        parsed = _as_float(effective, None)
        if parsed is None or parsed <= 0.0:
            return 0
        return max(1, math.ceil(1.0 / min(1.0, parsed)))

    def _debounce(self) -> float:
        return self.permissions.debounce_seconds()

    def _window(self) -> float:
        return self.permissions.window_seconds()

    def _cooldown(self) -> float:
        return self.permissions.cooldown_seconds()

    def _prune(self, now: float) -> None:
        """空窗补偿：窗口外的 pending 清掉 —— 五分钟前那一条不该给现在这一条"凑数"。"""
        window = self._window()
        if window <= 0.0:
            return  # 0 表示不设窗：不做时间过滤
        cutoff = now - window
        self._pending = [entry for entry in self._pending if entry[0] >= cutoff]

    def _banned(self, text: str) -> bool:
        """L0：ban 正则命中即丢整条（大小写不敏感）。坏正则只跳过，不拖垮主循环。"""
        patterns = self.permissions.ban_patterns()
        if not patterns:
            return False
        haystack = str(text or "")
        for pattern in patterns:
            try:
                if re.search(pattern, haystack, re.IGNORECASE):
                    return True
            except re.error as exc:
                logger.warning("ban 正则非法，已忽略 %r: %s", pattern, exc)
        return False

    def _decide(self, *, decision: str, reason: str, tier: str, effective: float,
                threshold: int, sender: str, mentioned: bool,
                event_kind: Optional[str], ts: float) -> GateDecision:
        """组装结论 + 写审计行（调用方持有的锁内调用）。"""
        row = {
            "ts": ts,
            "sender": sender,
            "tier": tier,
            "effective": effective,
            "threshold": threshold,
            "pending": len(self._pending),
            "decision": decision,
            "reason": reason,
        }
        self._audit.append(row)
        policy = self.permissions.policy(tier)
        return GateDecision(
            decision=decision,
            reason=reason,
            tier=tier,
            effective=effective,
            threshold=threshold,
            pending=row["pending"],
            can_command=policy.can_command,
            memory_visibility=policy.memory_visibility,
            mentioned=bool(mentioned),
            event_kind=event_kind,
            audit=row,
        )

    # ── 主入口 ──

    def submit(self, sender, text, *, is_system: bool = False, mentioned: bool = False,
               now: Optional[float] = None, canonical: Optional[str] = None,
               event_kind: Optional[str] = None) -> GateDecision:
        """处理一条进来的消息，返回该怎么走。

        - `canonical`：解析后的身份（别名/大小写归一后），tier 按它查；不给就用 sender。
        - `event_kind`：事件注入（death/任务完成…）时的事件名，普通聊天为 None。

        顺序即优先级：系统消息 → 静默 → ban → 冷却 → 点名 → 抗抖 → 频率。
        """
        sent_at = self._now(now)
        with self._lock:
            identity = canonical or sender
            tier = self.permissions.tier_of(identity)
            effective = self.effective_for(tier)
            threshold = self.threshold_for(effective)
            # 审计的 sender 用解析后的身份：名单大小写不一致/有别名时也能追到人
            audit_sender = str(identity or sender or "")

            if is_system:
                # 系统消息（模组回报/死亡通告）永不触发回复；调用方仍要记进记忆
                return self._decide(decision="none", reason="system_message", tier=tier,
                                    effective=effective, threshold=threshold,
                                    sender=audit_sender, mentioned=mentioned,
                                    event_kind=event_kind, ts=sent_at)

            if effective <= 0.0:
                # 静默模式：只记录、不回、零 token。点名也救不了 —— 公开服 guest 的静默是
                # 硬成本控制，要放开请开 public_reply_guests，而不是让一条 @ 破防。
                return self._decide(decision="silent", reason="silent_mode", tier=tier,
                                    effective=effective, threshold=threshold,
                                    sender=audit_sender, mentioned=mentioned,
                                    event_kind=event_kind, ts=sent_at)

            if self._banned(text):
                # L0：整条丢弃，连 pending 都不进（用户明确不想看到的内容）
                return self._decide(decision="drop", reason="ban_match", tier=tier,
                                    effective=effective, threshold=threshold,
                                    sender=audit_sender, mentioned=mentioned,
                                    event_kind=event_kind, ts=sent_at)

            if sent_at < self._cooldown_until and not mentioned:
                # L5：刚说过"这轮不接话"，再问一遍就是纯烧 token；点名可旁路（见下）
                return self._decide(decision="cooldown", reason="no_action_cooldown",
                                    tier=tier, effective=effective, threshold=threshold,
                                    sender=audit_sender, mentioned=mentioned,
                                    event_kind=event_kind, ts=sent_at)

            if mentioned:
                # L3 硬旁路：主人在叫你，频率门与抗抖都不该挡；顺手清掉 pending
                self._pending.clear()
                self._last_message_at = sent_at
                return self._decide(decision="act", reason="mention_bypass", tier=tier,
                                    effective=effective, threshold=threshold,
                                    sender=audit_sender, mentioned=mentioned,
                                    event_kind=event_kind, ts=sent_at)

            previous_at = self._last_message_at
            # 空窗补偿 + 累加本条：抗抖分支同样要体现"pending 已经累加"（否则
            # 连发两条时槽位白占）。这两步放在抗抖判断之前，`pending` 数才自洽。
            self._prune(sent_at)
            self._pending.append((sent_at, tier, audit_sender))
            self._last_message_at = sent_at

            if previous_at is not None and sent_at - previous_at < self._debounce():
                # 抗抖：用户可能还在打字，等下一拍再判断（poll 会来收）
                return self._decide(decision="wait", reason="debounce", tier=tier,
                                    effective=effective, threshold=threshold,
                                    sender=audit_sender, mentioned=mentioned,
                                    event_kind=event_kind, ts=sent_at)

            if len(self._pending) < threshold:
                return self._decide(decision="wait",
                                    reason=f"frequency({len(self._pending)}/{threshold})",
                                    tier=tier, effective=effective, threshold=threshold,
                                    sender=audit_sender, mentioned=mentioned,
                                    event_kind=event_kind, ts=sent_at)

            # 放行即消化：先组装（审计里留下触发它的 pending 数），再清槽位
            decision = self._decide(decision="act", reason="frequency_pass", tier=tier,
                                    effective=effective, threshold=threshold,
                                    sender=audit_sender, mentioned=mentioned,
                                    event_kind=event_kind, ts=sent_at)
            self._pending.clear()
            return decision

    def poll(self, now: Optional[float] = None) -> Optional[GateDecision]:
        """tick 里调用：没有未消化的消息就 None，否则判断"现在该放行了吗"。

        抗抖窗过完 + 频率达标才返回 act（reason="debounce_elapsed"）—— 否则两条消息
        挨得太近时最后那条永远卡在 wait 里，谁都不回。
        """
        with self._lock:
            if not self._pending:
                return None
            at = self._now(now)
            self._prune(at)
            if not self._pending:
                return None
            if self._last_message_at is not None and at - self._last_message_at < self._debounce():
                return None  # 还在抗抖窗里，等下一拍
            if at < self._cooldown_until:
                return None  # 冷却是硬门（poll 没有"点名"上下文，不能旁路）
            tier = self._pending[-1][1]
            effective = self.effective_for(tier)
            threshold = self.threshold_for(effective)
            if len(self._pending) < threshold:
                return None  # 攒得还不够，继续等
            sender = self._pending[-1][2]
            decision = self._decide(decision="act", reason="debounce_elapsed", tier=tier,
                                    effective=effective, threshold=threshold,
                                    sender=sender, mentioned=False, event_kind=None, ts=at)
            self._pending.clear()
            return decision

    # ── L5 冷却 ──

    def note_decision(self, decision: GateDecision) -> None:
        """调用方在 L4 语义门返回 no_action 后调用：静默 cooldown_seconds（默认 8s）。

        只有 no_action 才置冷却；continue/wait 之后马上冷却会把正常对话也掐死。
        """
        kind = decision.get("decision") if isinstance(decision, dict) else getattr(
            decision, "decision", None)
        if kind != "no_action":
            return
        self.mark_no_action()

    def mark_no_action(self, now: Optional[float] = None) -> float:
        """置冷却，返回冷却到期的时间戳（与 clock 同一时基，方便调用方对齐 tick）。"""
        at = self._now(now)
        with self._lock:
            self._cooldown_until = at + self._cooldown()
            return self._cooldown_until

    # ── 审计与仪表盘 ──

    def audit_log(self, n: int = 10) -> list[dict]:
        """最近 n 行审计（时间从早到晚）；最多保留 AUDIT_MAX=50 行。n=None 取全部。"""
        with self._lock:
            rows = list(self._audit)
        if n is None:
            picked = rows
        else:
            picked = rows[-int(n):] if int(n) > 0 else []
        return [dict(row) for row in picked]

    def status(self) -> dict:
        """仪表盘快照：各 tier 的 effective/threshold、pending、冷却剩余、最近决策。"""
        with self._lock:
            at = self._now(None)
            pending = len(self._pending)
            cooldown_remaining = max(0.0, self._cooldown_until - at)
        effective = {tier: self.effective_for(tier) for tier in TIERS}
        return {
            "server_mode": self.permissions.server_mode(),
            "session_adjust": self.session_adjust(),
            "effective": effective,
            "threshold": {tier: self.threshold_for(value) for tier, value in effective.items()},
            "pending": pending,
            "cooldown_remaining": cooldown_remaining,
            "last_decisions": self.audit_log(10),
        }
