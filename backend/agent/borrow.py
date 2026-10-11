"""借用台账 + 规则性记忆（"从仓库拿的东西是借的"）。

用户口径（生电服）：桶（刷铁机）里的资源几乎不限量，但**拿 = 借**：

- 先在对话里被明确授权（"你可以使用仓库里所有铁，因为我们有刷铁机"）才准借；
  授权按物品**族**记（铁类），落盘到**按 server 分区**的文件里，下次进同一个服仍然生效。
- 借用有硬上限：单次不超过「需求 × 2」且不超过 256（`BORROW_MAX_COUNT`），
  不给明确数量（＝想整箱搬）一律拒绝。
- 用完 / 任务结束 / 失败 / 被中断 / 玩家接管 → 自动把**剩余**放回**原容器**，
  并用 `open_container.items` 的真实快照**校验真的回去了**；回不去要**报出来**。
- 每次借/还都记审计（物品 / 数量 / 容器 / 原因 / 结果）。

本模块只做**数据与判定**（规则、上限、账本、快照核对），下发命令由 planner 负责 ——
这样它能在没有 socket 的测试里被完整驱动。
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from .conversation import sanitize_key, server_key

logger = logging.getLogger("borrow")

# 默认规则/台账落盘根目录：与 `data/conversations/` 同级，按 server 分区。
DEFAULT_RULES_ROOT = Path(__file__).parent.parent / "data" / "rules"

# 单件物品单次借用的**绝对**上限（用户口径里的"256 上限"）。
BORROW_MAX_COUNT = 256
# 需求量之外的余量倍数（"2×需求"）。
BORROW_NEED_MULTIPLIER = 2

# 归还条件（写进规则、也写进 prompt）。
CONDITION_RETURN = "return_after_use"     # 用完归还
CONDITION_NO_CARRY = "no_carry"           # 不要随身携带/带走
DEFAULT_CONDITIONS: tuple[str, ...] = (CONDITION_RETURN, CONDITION_NO_CARRY)

CONDITION_LABELS = {
    CONDITION_RETURN: "用完必须放回原箱",
    CONDITION_NO_CARRY: "不要随身携带/带走",
}

# ── 物品族 ────────────────────────────────────────────────────────
# 规则按族记（"仓库里所有铁" → iron），匹配时用通配 pattern，族只是给 prompt/审计用的名字。
FAMILY_PATTERNS: dict[str, tuple[str, ...]] = {
    "iron": ("iron_*", "raw_iron"),
    "gold": ("gold_*", "raw_gold"),
    "copper": ("copper_*", "raw_copper"),
    "coal": ("coal", "coal_ore", "deepslate_coal_ore"),
    "redstone": ("redstone", "redstone_ore", "deepslate_redstone_ore", "redstone_block"),
    "diamond": ("diamond", "diamond_ore", "deepslate_diamond_ore", "diamond_block"),
    "emerald": ("emerald", "emerald_ore", "deepslate_emerald_ore", "emerald_block"),
    "lapis": ("lapis_lazuli", "lapis_ore", "deepslate_lapis_ore", "lapis_block"),
    "quartz": ("quartz", "nether_quartz_ore", "quartz_block"),
}

FAMILY_LABELS = {
    "iron": "铁类",
    "gold": "金类",
    "copper": "铜类",
    "coal": "煤",
    "redstone": "红石",
    "diamond": "钻石",
    "emerald": "绿宝石",
    "lapis": "青金石",
    "quartz": "石英",
}

# 中文口语名 → 物品族（"仓库里所有铁"）。
FAMILY_ALIASES: dict[str, str] = {
    "铁": "iron", "铁锭": "iron", "铁块": "iron", "铁矿": "iron", "铁矿石": "iron",
    "金": "gold", "金锭": "gold", "金块": "gold",
    "铜": "copper", "铜锭": "copper", "铜块": "copper",
    "煤": "coal", "煤炭": "coal",
    "红石": "redstone",
    "钻石": "diamond", "钻": "diamond",
    "绿宝石": "emerald",
    "青金石": "lapis",
    "石英": "quartz",
}

# 英文族名（"you can use all iron in the chest"）。
FAMILY_WORDS: dict[str, str] = {
    "iron": "iron", "gold": "gold", "copper": "copper", "coal": "coal",
    "redstone": "redstone", "diamond": "diamond", "emerald": "emerald",
    "lapis": "lapis", "quartz": "quartz",
}

# 常见非"族"物品的中文口语名（"可以用仓库里的圆石"）。族名另有 FAMILY_ALIASES。
ITEM_ALIASES: dict[str, str] = {
    "圆石": "cobblestone", "石头": "stone", "沙子": "sand", "玻璃": "glass",
    "泥土": "dirt", "木头": "oak_log", "原木": "oak_log", "木板": "oak_planks",
    "羊毛": "white_wool", "骨头": "bone", "线": "string",
    "皮革": "leather", "小麦": "wheat", "苹果": "apple", "面包": "bread",
    "火把": "torch", "玻璃板": "glass_pane", "甘蔗": "sugar_cane",
}

# 讲话里出现"仓库/箱子"这类词才认为是在讲仓库授权（中文按子串，英文按整词）。
_CJK_WAREHOUSE = ("仓库", "箱子", "箱里", "储物", "储物间", "原料")
_EN_WAREHOUSE = ("chest", "storage", "warehouse", "shulker")
# 授权动词（"你可以使用/随便用/拿去用"）。
_CJK_GRANT = ("可以", "能用", "可以用", "允许", "随便", "尽管", "拿去", "拿来", "使用")
# 注意：英文授权词必须**按整词**匹配 —— `"use" in "because"` 会误判成授权。
_EN_GRANT = ("use", "can", "may", "allowed", "take", "borrow")
# 数量词（"所有铁/全部铁/任意铁"）—— 有它也算授权。
_CJK_QUANTITY = ("所有", "全部", "任意", "任何", "全用")
_EN_QUANTITY = ("all", "any", "everything")
# 问句不算授权（"我可以用箱子里的铁吗？" 是提问，不是许可）。宁可漏授权也不误授权。
_QUESTION_MARKERS = ("吗", "？", "?", "么")
# 否定：命中即视为**收回/拒绝**，绝不落成授权。
# 只收"明确否掉取用"的说法 —— 不能收裸的"不要/不能"，否则
# "你可以用仓库里的铁，用完放回去，不要随身携带" 这句**条件**会被误判成拒绝。
_DENY_WORDS = ("不许", "不准", "禁止", "收回", "不要用", "别用", "不能用", "不许用", "不准用")

_WORD_RE = re.compile(r"[a-z0-9_]+")


def _words(text: str) -> set[str]:
    """英文整词集合（判断英文关键字用；中文关键字仍按子串）。"""
    return set(_WORD_RE.findall(str(text or "").lower()))


def normalize_item(name: Any) -> str:
    """物品名归一化：小写、去空白、剥 `minecraft:` 命名空间。"""
    text = str(name or "").strip().lower().replace(" ", "")
    if text.startswith("minecraft:"):
        text = text[len("minecraft:"):]
    return text


def family_of(item: Any) -> Optional[str]:
    """物品属于哪个族（匹配 `FAMILY_PATTERNS`）；认不出返回 None。"""
    item_id = normalize_item(item)
    if not item_id:
        return None
    for family, patterns in FAMILY_PATTERNS.items():
        for pattern in patterns:
            if pattern.endswith("*"):
                if item_id.startswith(pattern[:-1]):
                    return family
            elif item_id == pattern:
                return family
    return None


def _pattern_matches(item_id: str, pattern: str) -> bool:
    if not pattern:
        return False
    if pattern.endswith("*"):
        return item_id.startswith(pattern[:-1])
    return item_id == pattern


def _mentions_warehouse(raw: str, words: set[str]) -> bool:
    return any(word in raw for word in _CJK_WAREHOUSE) or bool(words & set(_EN_WAREHOUSE))


def _has_grant_words(raw: str, words: set[str]) -> bool:
    return (any(word in raw for word in _CJK_GRANT) or bool(words & set(_EN_GRANT))
            or any(word in raw for word in _CJK_QUANTITY) or bool(words & set(_EN_QUANTITY)))


def parse_borrow_grant(text: Any) -> Optional[dict]:
    """从一句人话里认出"仓库授权"，返回规则草案；认不出返回 None。

    例句："你可以使用仓库里所有铁，因为我们有刷铁机" →
        {"family": "iron", "patterns": ["iron_*", "raw_iron"],
         "conditions": ["return_after_use", "no_carry"], "explicit": []}

    判定要求同时出现 ① 仓库词 ② 授权词或数量词 ③ 认识的物品（族名或物品 id）；
    出现否定词直接不认（"不许用仓库里的铁" 不是授权）。
    """
    raw = str(text or "").strip()
    if not raw:
        return None
    lowered = raw.lower()
    words = _words(lowered)
    if any(marker in raw for marker in _QUESTION_MARKERS):
        return None
    if any(word in lowered for word in _DENY_WORDS):
        return None
    if not _mentions_warehouse(raw, words):
        return None
    if not _has_grant_words(raw, words):
        return None

    family = None
    patterns: list[str] = []
    # 中文口语名（"铁"）优先；其次英文族名（"iron"）。
    for alias in sorted(FAMILY_ALIASES, key=len, reverse=True):
        if alias in raw:
            family = FAMILY_ALIASES[alias]
            break
    if family is None:
        for word, fam in FAMILY_WORDS.items():
            if word in lowered:
                family = fam
                break
    if family:
        patterns = list(FAMILY_PATTERNS[family])
    else:
        # 没认出族：先看常见物品的中文口语名（"圆石/木头"），再看像物品 id 的英文 token。
        for alias in sorted(ITEM_ALIASES, key=len, reverse=True):
            if alias in raw:
                patterns.append(ITEM_ALIASES[alias])
        if not patterns:
            for token in raw.lower().replace("，", " ").replace(",", " ").split():
                token = normalize_item(token)
                if len(token) >= 3 and token.replace("_", "").isalpha() and token.isascii():
                    patterns.append(token)
        if not patterns:
            return None

    explicit = [name for name, cond in (
        (CONDITION_RETURN, any(w in raw for w in ("放回", "归还", "还回去", "还回"))),
        (CONDITION_NO_CARRY, any(w in raw for w in ("随身", "带走", "带在", "揣"))),
    ) if cond]

    return {
        "family": family or "",
        "patterns": patterns,
        "conditions": list(DEFAULT_CONDITIONS),
        "explicit_conditions": explicit,
        "source": raw,
    }


def parse_borrow_revoke(text: Any) -> Optional[dict]:
    """认出"收回授权"（"别用仓库里的铁了"），返回与授权同形的草案；认不出 None。

    只说族/物品，不带条件（收回就是把整条规则删掉）；认不出具体是谁就用 `family=""` +
    空 patterns 表示"收回全部仓库授权"。
    """
    raw = str(text or "").strip()
    if not raw:
        return None
    lowered = raw.lower()
    if not any(word in lowered for word in _DENY_WORDS):
        return None
    if not _mentions_warehouse(raw, _words(lowered)):
        return None
    family = ""
    for alias in sorted(FAMILY_ALIASES, key=len, reverse=True):
        if alias in raw:
            family = FAMILY_ALIASES[alias]
            break
    if not family:
        for word, fam in FAMILY_WORDS.items():
            if word in lowered:
                family = fam
                break
    patterns: list[str] = []
    if not family:
        for alias in sorted(ITEM_ALIASES, key=len, reverse=True):
            if alias in raw:
                patterns.append(ITEM_ALIASES[alias])
    return {"family": family, "patterns": patterns, "source": raw}


# ── 规则记忆（按 server 分区持久化）────────────────────────────────


class BorrowRulesStore:
    """按 server 分区的借用授权规则（`.json` 落盘，读写线程安全）。

    分区键与对话记忆一致（`conversation.server_key` + `sanitize_key`）：
    `world.server` 优先，其次存档名，都没有就 unknown。
    """

    def __init__(self, root: str | Path = DEFAULT_RULES_ROOT,
                 server: str = "", world_name: str = "", clock=time.time):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._clock = clock
        self._lock = threading.RLock()
        self._namespace = server_key(server, world_name)
        self._data = self._load()

    # ── 分区 ──

    def use_namespace(self, server: Any, world_name: Any) -> str:
        """切换分区并重读盘（换服就换文件，重启不丢授权）。"""
        with self._lock:
            self._namespace = server_key(server, world_name)
            self._data = self._load()
            return self._namespace

    @property
    def namespace(self) -> str:
        return self._namespace

    @property
    def path(self) -> Path:
        return self.root / f"{sanitize_key(self._namespace)}.json"

    @property
    def audit_path(self) -> Path:
        return self.root / f"{sanitize_key(self._namespace)}.audit.jsonl"

    # ── 读 ──

    def rules(self) -> list[dict]:
        with self._lock:
            return [dict(rule) for rule in self._data.get("rules", [])]

    def match(self, item: Any) -> Optional[dict]:
        """这件物品是否有借用授权（返回命中的规则；没有返回 None）。"""
        item_id = normalize_item(item)
        if not item_id:
            return None
        with self._lock:
            for rule in self._data.get("rules", []):
                for pattern in rule.get("patterns", []):
                    if _pattern_matches(item_id, str(pattern)):
                        return dict(rule)
        return None

    def status(self) -> dict:
        with self._lock:
            return {
                "namespace": self._namespace,
                "path": str(self.path),
                "rules": [dict(rule) for rule in self._data.get("rules", [])],
            }

    # ── 写 ──

    @staticmethod
    def rule_id(family: str, patterns: list[str]) -> str:
        """规则 id：有族用族，没有就用第一个 pattern（授权与收回共用，保证对得上）。"""
        return f"borrow:{family}" if family else f"borrow:{patterns[0] if patterns else '*'}"

    def grant(self, grant: dict, *, granted_by: str = "", granted_tier: str = "guest",
              source: str = "") -> dict:
        """落一条授权规则（同族覆盖旧的，幂等），返回落盘后的规则。"""
        if not isinstance(grant, dict):
            raise ValueError("grant must be a dict")
        patterns = [str(p) for p in (grant.get("patterns") or []) if str(p).strip()]
        if not patterns:
            raise ValueError("grant must carry at least one item pattern")
        family = str(grant.get("family") or "")
        rule_id = self.rule_id(family, patterns)
        conditions = [str(c) for c in (grant.get("conditions") or DEFAULT_CONDITIONS)
                      if str(c).strip()] or list(DEFAULT_CONDITIONS)
        rule = {
            "id": rule_id,
            "family": family,
            "label": FAMILY_LABELS.get(family, family or patterns[0]),
            "patterns": patterns,
            "conditions": conditions,
            "explicit_conditions": [str(c) for c in (grant.get("explicit_conditions") or [])],
            "granted_by": str(granted_by or ""),
            "granted_tier": str(granted_tier or "guest"),
            "source": str(source or grant.get("source") or ""),
            "ts": float(self._clock()),
        }
        with self._lock:
            rules = [r for r in self._data.get("rules", []) if r.get("id") != rule_id]
            rules.append(rule)
            self._data["rules"] = rules
            self._save()
        logger.info("[Borrow] 落授权规则 %s（%s）", rule_id, "、".join(patterns))
        return dict(rule)

    def revoke(self, target: dict | str) -> Optional[dict]:
        """收回授权（用户改口"别用仓库里的铁了"）：删掉对应规则。

        目标是族名/物品名（字符串）或与授权同形的 `{"family","patterns"}`；两边都没指定
        （`family=""` 且 patterns 空）＝收回该分区的**全部**仓库授权。没删到返回 None。
        """
        if isinstance(target, dict):
            family = str(target.get("family") or "")
            patterns = [str(p) for p in (target.get("patterns") or []) if str(p).strip()]
        else:
            text = str(target or "").strip()
            family = text if text in FAMILY_PATTERNS else ""
            patterns = [] if family else ([text] if text else [])
        with self._lock:
            rules = self._data.get("rules", [])
            if not family and not patterns:
                removed = rules
                kept: list[dict] = []
            else:
                want_id = self.rule_id(family, patterns)
                removed = [r for r in rules if r.get("id") == want_id]
                if not removed and patterns:
                    removed = [r for r in rules
                               if any(p in (r.get("patterns") or []) for p in patterns)]
                kept = [r for r in rules if r not in removed]
            if not removed:
                return None
            self._data["rules"] = kept
            self._save()
        logger.info("[Borrow] 收回授权 %s", [r.get("id") for r in removed])
        return dict(removed[0])

    # ── prompt ──

    def describe(self) -> str:
        """给 prompt 看的一句话（没有授权就是空串）。"""
        rules = self.rules()
        if not rules:
            return ""
        lines = []
        for rule in rules:
            conditions = "、".join(CONDITION_LABELS.get(c, c) for c in rule.get("conditions", []))
            who = rule.get("granted_by") or "有人"
            lines.append(f"- {rule.get('label') or rule.get('id')}（{who} 授权）：{conditions}")
        return "\n".join(lines)

    # ── 盘 ──

    def _load(self) -> dict:
        path = self.root / f"{sanitize_key(self._namespace)}.json"
        if not path.exists():
            return {"version": 1, "rules": []}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("[Borrow] 读取授权规则失败，已回退空表 %s: %s", path, exc)
            return {"version": 1, "rules": []}
        if not isinstance(data, dict) or not isinstance(data.get("rules"), list):
            logger.warning("[Borrow] 授权规则结构不对，已回退空表: %s", path)
            return {"version": 1, "rules": []}
        data.setdefault("version", 1)
        return data

    def _save(self) -> None:
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        try:
            temporary.write_text(json.dumps(self._data, ensure_ascii=False, indent=2),
                                 encoding="utf-8")
            temporary.replace(self.path)
        except Exception as exc:
            logger.warning("[Borrow] 写授权规则失败 %s: %s", self.path, exc)


# ── 容器快照（`open_container`）读写 ──────────────────────────────


def _as_int(value: Any) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def container_entries(open_container: Any) -> list[dict]:
    """`open_container.items` 归一化成 `[{"slot","item","count"}]`（坏项跳过）。"""
    if not isinstance(open_container, dict):
        return []
    rows = open_container.get("items")
    if not isinstance(rows, (list, tuple)):
        return []
    out = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        slot = _as_int(row.get("slot"))
        if slot is None:
            continue
        out.append({
            "slot": slot,
            "item": normalize_item(row.get("item") or row.get("item_id") or row.get("name")),
            "count": _as_int(row.get("count")) or 0,
        })
    return out


def player_inv_start(open_container: Any) -> int:
    """玩家背包在菜单里的起始槽位（`player_inv_start`；缺省按"末 36 格"推算）。"""
    if isinstance(open_container, dict):
        explicit = _as_int(open_container.get("player_inv_start"))
        if explicit is not None and explicit >= 0:
            return explicit
        slots = _as_int(open_container.get("slots"))
        if slots is not None and slots > 36:
            return slots - 36
    return 0


def container_count(open_container: Any, item: Any) -> int:
    """容器侧（槽位 < player_inv_start）某物品的总数。"""
    item_id = normalize_item(item)
    start = player_inv_start(open_container)
    return sum(row["count"] for row in container_entries(open_container)
               if row["slot"] < start and row["item"] == item_id)


def container_slot(open_container: Any, item: Any) -> Optional[tuple[int, int]]:
    """容器里放这件物品的槽位与现有数量：优先能再叠的（<64），否则第一个空槽。

    返回 `(slot, existing_count)`；容器里既没有这件物品也没有空槽 → None。
    """
    start = player_inv_start(open_container)
    if start <= 0:
        return None
    item_id = normalize_item(item)
    entries = [row for row in container_entries(open_container) if row["slot"] < start]
    for row in entries:
        if row["item"] == item_id and row["count"] < 64:
            return row["slot"], row["count"]
    occupied = {row["slot"] for row in entries}
    for slot in range(start):
        if slot not in occupied:
            return slot, 0
    return None


def container_slots_with(open_container: Any, item: Any) -> list[dict]:
    """容器侧所有含该物品的槽位（数量从多到少）。"""
    item_id = normalize_item(item)
    start = player_inv_start(open_container)
    rows = [row for row in container_entries(open_container)
            if row["slot"] < start and row["item"] == item_id and row["count"] > 0]
    return sorted(rows, key=lambda row: row["count"], reverse=True)


def player_slots_with(open_container: Any, item: Any) -> list[dict]:
    """玩家背包侧所有含该物品的槽位（数量从多到少）。"""
    item_id = normalize_item(item)
    start = player_inv_start(open_container)
    rows = [row for row in container_entries(open_container)
            if row["slot"] >= start and row["item"] == item_id and row["count"] > 0]
    return sorted(rows, key=lambda row: row["count"], reverse=True)


def container_key(open_container: Any) -> tuple[Optional[int], str]:
    """容器的身份 `(id, title)`：核对归还时先认 id，退而认标题。"""
    if not isinstance(open_container, dict):
        return None, ""
    return _as_int(open_container.get("id")), str(open_container.get("title") or "")


# ── 借用台账 ─────────────────────────────────────────────────────


@dataclass
class BorrowEntry:
    """一笔未还清的借用。"""

    id: str
    item: str
    count: int
    reason: str = ""
    container_id: Optional[int] = None
    container_title: str = ""
    container_pos: Optional[tuple] = None
    container_block: str = ""            # 回原容器走 `go_to_block` 用的方块 id
    from_slot: Optional[int] = None
    baseline: Optional[int] = None      # 借出前容器里这件物品的数量（归还校验的基准）
    ts: float = 0.0
    returned: int = 0
    state: str = "open"                 # open | returning | returned | failed
    attempts: int = 0
    awaiting_snapshot: bool = False     # 已下发归还动作，等一份新快照来核对（防重复下发）
    trip_sent: bool = False             # 已经走过一趟"回原容器"的寻路
    failure: str = ""

    @property
    def remaining(self) -> int:
        return max(0, int(self.count) - int(self.returned))

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "item": self.item,
            "count": self.count,
            "returned": self.returned,
            "remaining": self.remaining,
            "reason": self.reason,
            "container_id": self.container_id,
            "container_title": self.container_title,
            "container_pos": list(self.container_pos) if self.container_pos else None,
            "state": self.state,
            "attempts": self.attempts,
            "failure": self.failure,
            "ts": self.ts,
        }


class BorrowLedger:
    """借用台账：上限判定、记录、剩余、审计、（按容器快照）归还核对。

    审计行字段固定为 物品 / 数量 / 容器 / 原因 / 结果（`item/count/container/reason/result`），
    可 append 到 JSONL（`use_audit_path`），也可只留内存（测试用）。
    """

    def __init__(self, audit_path: str | Path | None = None, clock=time.time,
                 max_count: int = BORROW_MAX_COUNT,
                 need_multiplier: int = BORROW_NEED_MULTIPLIER,
                 audit_limit: int = 200):
        self._clock = clock
        self.max_count = max(1, int(max_count))
        self.need_multiplier = max(1, int(need_multiplier))
        self._audit: list[dict] = []
        self._audit_limit = max(1, int(audit_limit))
        self._entries: list[BorrowEntry] = []
        self._lock = threading.RLock()
        self._audit_path: Optional[Path] = Path(audit_path) if audit_path else None
        if self._audit_path is not None:
            try:
                self._audit_path.parent.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                logger.warning("[Borrow] 建审计目录失败 %s: %s", self._audit_path.parent, exc)

    # ── 审计落盘 ──

    def use_audit_path(self, path: str | Path | None) -> None:
        self._audit_path = Path(path) if path else None
        if self._audit_path is not None:
            try:
                self._audit_path.parent.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                logger.warning("[Borrow] 建审计目录失败 %s: %s", self._audit_path.parent, exc)

    def _record(self, action: str, *, item: str, count: int, container: str = "",
                container_slot: Optional[int] = None, player_slot: Optional[int] = None,
                reason: str = "", result: str = "", detail: str = "",
                entry_id: str = "") -> dict:
        row = {
            "ts": float(self._clock()),
            "action": str(action),
            "item": str(item),
            "count": int(count or 0),
            "container": str(container or ""),
            "container_slot": container_slot,
            "player_slot": player_slot,
            "reason": str(reason or ""),
            "result": str(result or ""),
            "detail": str(detail or ""),
            "entry_id": str(entry_id or ""),
        }
        self._audit.append(row)
        del self._audit[:-self._audit_limit]
        self._append(row)
        return row

    def _append(self, row: dict) -> None:
        if self._audit_path is None:
            return
        try:
            with self._audit_path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        except Exception as exc:
            logger.warning("[Borrow] 写审计失败 %s: %s", self._audit_path, exc)

    def audit(self, n: int = 10) -> list[dict]:
        """最近 n 条审计（时间从早到晚）。"""
        with self._lock:
            rows = self._audit[-int(n):] if n and int(n) > 0 else list(self._audit)
            return [dict(row) for row in rows]

    # ── 上限 ──

    def limit_for(self, item: Any, need: Any = None) -> int:
        """单次可借上限：min(256, 需求 × 2)；没给需求就是 256。"""
        try:
            need_value = int(need) if need is not None else 0
        except (TypeError, ValueError):
            need_value = 0
        if need_value > 0:
            return max(1, min(self.max_count, need_value * self.need_multiplier))
        return self.max_count

    def check(self, item: Any, count: Any, need: Any = None) -> tuple[bool, str, int]:
        """借前判定 → `(allowed, reason, count)`。

        - 不给数量（`None`）＝想整组/整箱搬 → `whole_container_forbidden`；
        - 超过上限 → `over_limit`（返回上限，供调用方如实说明"最多只能拿多少"）。
        """
        parsed = _as_int(count)
        if parsed is None:
            return False, "whole_container_forbidden", 0
        if parsed <= 0:
            return False, "invalid_count", 0
        cap = self.limit_for(item, need)
        if parsed > cap:
            return False, "over_limit", cap
        return True, "ok", parsed

    # ── 记账 ──

    def record_borrow(self, item: Any, count: int, *, container: Any = None,
                      from_slot: Optional[int] = None, to_slot: Optional[int] = None,
                      reason: str = "", container_pos: Any = None,
                      container_block: str = "", baseline: Optional[int] = None) -> BorrowEntry:
        """记一笔借出（同时写审计）。"""
        container_id, container_title = container_key(container)
        item_id = normalize_item(item)
        if baseline is None and container:
            baseline = container_count(container, item_id)
        entry = BorrowEntry(
            id=uuid.uuid4().hex[:8],
            item=item_id,
            count=int(count),
            reason=str(reason or ""),
            container_id=container_id,
            container_title=container_title,
            container_pos=tuple(container_pos) if container_pos else None,
            container_block=str(container_block or ""),
            from_slot=_as_int(from_slot),
            baseline=_as_int(baseline),
            ts=float(self._clock()),
        )
        with self._lock:
            self._entries.append(entry)
        self._record("borrow", item=item_id, count=int(count),
                     container=container_title, container_slot=_as_int(from_slot),
                     player_slot=_as_int(to_slot), reason=str(reason or ""),
                     result="taken", detail=f"baseline={entry.baseline}",
                     entry_id=entry.id)
        return entry

    def begin_return(self, entry_id: str, *, count: Optional[int] = None,
                     from_slot: Optional[int] = None, to_slot: Optional[int] = None,
                     reason: str = "auto") -> Optional[BorrowEntry]:
        """登记"已下发归还动作"，等新快照核对（`awaiting_snapshot=True` 防重复下发）。"""
        with self._lock:
            entry = self._find(entry_id)
            if entry is None:
                return None
            entry.state = "returning"
            entry.attempts += 1
            entry.awaiting_snapshot = True
            planned = _as_int(count) or entry.remaining
        self._record("return", item=entry.item, count=planned,
                     container=entry.container_title, container_slot=_as_int(to_slot),
                     player_slot=_as_int(from_slot), reason=str(reason or "auto"),
                     result="sent", entry_id=entry.id)
        return entry

    def mark_unreturnable(self, entry_id: str, reason: str) -> Optional[BorrowEntry]:
        """回不去（没有打开的容器、也没有容器坐标）→ 记账为失败并**报出来**。"""
        with self._lock:
            entry = self._find(entry_id)
            if entry is None or entry.state == "returned":
                return None
            entry.state = "failed"
            entry.failure = str(reason or "unreturnable")
            entry.awaiting_snapshot = False
            remaining = entry.remaining
        self._record("return_failed", item=entry.item, count=remaining,
                     container=entry.container_title, reason=str(reason or "unreturnable"),
                     result="failed", entry_id=entry.id)
        return entry

    def _find(self, entry_id: str) -> Optional[BorrowEntry]:
        for entry in self._entries:
            if entry.id == entry_id:
                return entry
        return None

    # ── 查询 ──

    def outstanding(self) -> list[BorrowEntry]:
        """还没还清、也没判失败的借用。"""
        with self._lock:
            return [e for e in self._entries if e.state in {"open", "returning"} and e.remaining > 0]

    def remaining(self, item: Any = None) -> int:
        item_id = normalize_item(item) if item else ""
        with self._lock:
            return sum(e.remaining for e in self.outstanding()
                       if not item_id or e.item == item_id)

    def entries(self) -> list[BorrowEntry]:
        with self._lock:
            return list(self._entries)

    def status(self) -> dict:
        return {
            "outstanding": [e.to_dict() for e in self.outstanding()],
            "total_outstanding": self.remaining(),
            "audit": self.audit(10),
        }

    def describe(self) -> str:
        """给 prompt 的一句话：还欠着哪家的什么东西。"""
        rows = self.outstanding()
        if not rows:
            return ""
        parts = []
        for entry in rows:
            where = entry.container_title or "原容器"
            parts.append(f"{entry.item} x{entry.remaining}（借自 {where}）")
        return "；".join(parts)

    # ── 归还规划（纯函数：只读快照，不碰 socket）──

    def plan_move_back(self, open_container: Any, item: Any,
                       remaining: Optional[int] = None) -> Optional[dict]:
        """把背包里的 `item` 挪回容器：返回 `container_move` 参数；挪不动返回 None。"""
        item_id = normalize_item(item)
        want = self.remaining(item_id) if remaining is None else max(1, int(remaining))
        held = player_slots_with(open_container, item_id)
        if not held:
            return None
        target = container_slot(open_container, item_id)
        if target is None:
            return None
        slot, existing = target
        space = 64 - int(existing)
        if space <= 0:
            return None
        source = held[0]
        move = min(want, source["count"], space)
        if move <= 0:
            return None
        return {"from_slot": source["slot"], "to_slot": slot, "count": int(move)}

    def plan_moves_out(self, open_container: Any, item: Any, count: int) -> list[dict]:
        """把 `count` 件从容器挪到背包：一格里最多一叠（64），需要几条就返回几条。

        每条是 `container_move {from_slot,to_slot,count}`；加起来不超过要拿的量，
        也不超过容器里真实有的量（拿不够就如实少拿，绝不假装拿满）。
        """
        item_id = normalize_item(item)
        want = max(0, int(count))
        if want <= 0:
            return []
        stacks = container_slots_with(open_container, item_id)
        if not stacks:
            return []
        start = player_inv_start(open_container)
        slots = _as_int(open_container.get("slots")) if isinstance(open_container, dict) else None
        if slots is None or slots <= start:
            return []
        occupied = {row["slot"] for row in container_entries(open_container)}
        targets = [slot for slot in range(start, slots) if slot not in occupied]
        plans: list[dict] = []
        remaining = want
        for stack in stacks:
            if remaining <= 0 or not targets:
                break
            take = min(remaining, int(stack["count"]), 64)
            if take <= 0:
                continue
            plans.append({"from_slot": stack["slot"], "to_slot": targets.pop(0),
                          "count": int(take)})
            remaining -= take
        return plans

    def holds(self, open_container: Any, item: Any) -> bool:
        """玩家背包侧还有这件物品吗（用于判断"东西还在手上却放不回去"）。"""
        return bool(player_slots_with(open_container, item))

    def same_container(self, entry: BorrowEntry, open_container: Any) -> bool:
        """快照里的容器是不是这笔借用的原容器（id 优先，其次标题）。

        空快照（`{}` / 没有 id 与标题）一律不算 —— 不能拿"什么都没开"当核对依据。
        """
        if not isinstance(open_container, dict):
            return False
        if open_container.get("id") is None and not str(open_container.get("title") or ""):
            return False
        container_id, title = container_key(open_container)
        if container_id is not None and entry.container_id is not None:
            return container_id == entry.container_id
        if title and entry.container_title:
            return title == entry.container_title
        if entry.container_id is None and entry.container_title:
            return bool(title) and title == entry.container_title
        return container_id is None and not entry.container_title

    # ── 归还核对 ──

    def verify(self, open_container: Any) -> list[dict]:
        """用一份 `open_container` 快照核对未还清的借用。

        判定：容器里该物品的数量**回到借出前的基线**即确认归还（写审计
        `return_confirmed`）；快照来自别的容器则跳过（不能拿错箱子的数据当证据）。
        """
        results: list[dict] = []
        with self._lock:
            candidates = [e for e in self._entries if e.state in {"open", "returning"} and e.remaining > 0]
        for entry in candidates:
            if not self.same_container(entry, open_container):
                continue
            entry.awaiting_snapshot = False
            have = container_count(open_container, entry.item)
            if entry.baseline is not None and have >= int(entry.baseline):
                with self._lock:
                    entry.returned = entry.count
                    entry.state = "returned"
                    entry.failure = ""
                self._record("return_confirmed", item=entry.item, count=entry.count,
                             container=entry.container_title, reason="container_restored",
                             result="verified", detail=f"container={have} baseline={entry.baseline}",
                             entry_id=entry.id)
                results.append({"entry_id": entry.id, "item": entry.item,
                                "count": entry.count, "result": "verified",
                                "detail": f"容器里已有 {have}（基线 {entry.baseline}）"})
            elif entry.state == "returning" and not player_slots_with(open_container, entry.item):
                with self._lock:
                    entry.state = "failed"
                    entry.failure = "still_short"
                self._record("return_failed", item=entry.item, count=entry.remaining,
                             container=entry.container_title, reason="still_short",
                             result="failed",
                             detail=f"container={have} baseline={entry.baseline}",
                             entry_id=entry.id)
                results.append({"entry_id": entry.id, "item": entry.item,
                                "count": entry.remaining, "result": "failed",
                                "reason": "still_short",
                                "detail": f"背包里已没有、容器里只有 {have}（基线 {entry.baseline}）"})
        return results

    def pending_report(self) -> list[dict]:
        """还没还清 / 已判失败的欠账（供状态与"报出来"）。"""
        with self._lock:
            rows = [e for e in self._entries if e.state in {"open", "returning", "failed"}]
        return [e.to_dict() for e in rows]
