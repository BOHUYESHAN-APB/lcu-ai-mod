"""资源租约 —— 按**资源**分租的仲裁（与 ``control_lease`` 的"全局控制租约"互补）。

用户口径（硬）：

* 「如果当前正在进行的**状态无法正常工作**，那就得**切到下一个状态**，或者**直接打断**」
  —— 打断在 ``state_governor``，本模块负责"谁能占哪些资源"；
* 「**允许多个状态叠加**，比如走路的时候去吃东西、或者对话 —— 有些情况下是要**并行**的」
  —— 于是"资源"是**可分租**的：占**不同**资源的两个状态可以**真的并行**。

资源集（每个状态/任务/工具**声明它要占哪些资源**）：

===========  ============================================  ==========
资源          覆盖的底层通道                                  并行性
===========  ============================================  ==========
``move``     forward/back/left/right/jump/sprint/sneak       可与 use/attack/chat 并行
``use``      右键                                            可与 move/attack/chat 并行
``attack``   左键                                            可与 move/use/chat 并行
``look``     视角（**独占**：持有者排斥其它一切资源）            独占
``container` 容器 GUI（**独占**：开箱界面排斥其它一切）           独占
``chat``     聊天/私信/指令（**永远可并行**）                    永远并行
===========  ============================================  ==========

冲突判定：把 ``chat`` 从两边剔掉后，若仍有交集就冲突；此外只要任一边含**独占**资源
（``look`` / ``container``），也与另一边的任何非空资源冲突（开箱界面/独占视角期间
什么都别做）。``chat`` 单项请求永远不冲突。

优先级：**P0 生存反射最高**（``PRIORITY_SURVIVAL``）—— 永远可抢、永远不被排队；
模型创建的普通任务按 ``task_list`` 的归属/优先级比较。**非**生存请求抢占不了生存请求。

每一次**授予 / 排队 / 抢占 / 释放 / 取消**都留一行可读审计（谁、何时、为何、抢了谁的什么）。

边界（硬）✗：本模块**不新增能力**、**不自己发起任何工具/动作** —— 它只**仲裁**（放行/排队/
抢占）与**记账**；真正的下发仍由调用方（planner / skills）走既有通道。
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Optional

logger = logging.getLogger("resource_lease")


# ── 资源集 ─────────────────────────────────────────────────────
RESOURCE_MOVE = "move"            # forward/back/left/right/jump/sprint/sneak 合为一份
RESOURCE_USE = "use"              # 右键
RESOURCE_ATTACK = "attack"        # 左键
RESOURCE_LOOK = "look"            # 视角（独占）
RESOURCE_CONTAINER = "container"  # 容器 GUI（独占）
RESOURCE_CHAT = "chat"            # 永远可并行

RESOURCES: frozenset[str] = frozenset({
    RESOURCE_MOVE, RESOURCE_USE, RESOURCE_ATTACK,
    RESOURCE_LOOK, RESOURCE_CONTAINER, RESOURCE_CHAT,
})
# 独占资源：持有期间排斥其它一切非空资源（视角只能有一个主人；开箱界面是模态的）。
EXCLUSIVE_RESOURCES: frozenset[str] = frozenset({RESOURCE_LOOK, RESOURCE_CONTAINER})
# 永远可并行：单项请求永不冲突，且与任何请求共存。
ALWAYS_PARALLEL: frozenset[str] = frozenset({RESOURCE_CHAT})


# ── 优先级 ─────────────────────────────────────────────────────
PRIORITY_SURVIVAL = 1000    # P0 生存反射：最高、永远可抢、永不被挡
PRIORITY_DEFAULT = 50       # 模型创建的普通任务
PRIORITY_MIN = 0
PRIORITY_MAX = 1000


# ── 事件类别 ───────────────────────────────────────────────────
LEASE_GRANT = "grant"
LEASE_QUEUE = "queue"
LEASE_PREEMPT = "preempt"
LEASE_RELEASE = "release"
LEASE_CANCEL = "cancel"

EVENT_KINDS: frozenset[str] = frozenset({
    LEASE_GRANT, LEASE_QUEUE, LEASE_PREEMPT, LEASE_RELEASE, LEASE_CANCEL,
})

# 审计环形缓冲长度（与 ``control_lease.AUDIT_MAX`` 同口径）。
AUDIT_MAX = 80
# 排队队列上限（超出丢**最低优先级**的，保证长时间运行有界）。
PENDING_MAX = 50


# ── 工具 → 资源（"每个工具声明它要占哪些资源"的**唯一**声明处）──────
# 按动作组给默认值，再对个别工具做覆盖。改这里＝改声明，别处不许再手写一份。
_GROUP_RESOURCES: Mapping[str, frozenset[str]] = {
    "move": frozenset({RESOURCE_MOVE}),
    "locate": frozenset(),                                # 只查不走
    "gather": frozenset({RESOURCE_MOVE, RESOURCE_ATTACK}),  # 走过去 + 左键挖
    "craft": frozenset({RESOURCE_CONTAINER}),             # 合成走物品/工作台界面
    "container": frozenset({RESOURCE_CONTAINER}),
    "farm": frozenset({RESOURCE_MOVE, RESOURCE_USE}),     # 走过去 + 右键锄/种/收
    "combat": frozenset({RESOURCE_ATTACK}),
    "items": frozenset(),
    "survive": frozenset({RESOURCE_USE}),
    "night": frozenset({RESOURCE_MOVE, RESOURCE_USE}),
    "guard": frozenset({RESOURCE_MOVE}),
    "hud": frozenset(),
    "server": frozenset({RESOURCE_CHAT}),
    "memory": frozenset(),
    "goal": frozenset(),                                  # 立/结束长期目标：只是账本，不占资源
    "territory": frozenset(),
}

# 个别工具的覆盖（组默认不够精确时用）。
_TOOL_RESOURCE_OVERRIDES: Mapping[str, frozenset[str]] = {
    "stop": frozenset(),                                   # 停止不是占用
    "guard_off": frozenset(),
    "afk_stop": frozenset(),                               # 停止驻守＝释放（用户口径：谁都能停）
    "container_close": frozenset(),                        # 关掉＝释放，不占用
    "drop_item": frozenset(),
    "sort_inventory": frozenset(),
    "get_inventory": frozenset(),
    "trade": frozenset({RESOURCE_CONTAINER}),              # 村民交易是界面
    "sleep": frozenset({RESOURCE_USE, RESOURCE_CONTAINER}),  # 睡眠走床界面
    "use_on": frozenset({RESOURCE_USE}),                   # 右键：与反射的 use 同一份资源
    "sleep_in_bed": frozenset({RESOURCE_MOVE, RESOURCE_USE}),
    "run_server_command": frozenset({RESOURCE_CHAT}),
    "send_whisper": frozenset({RESOURCE_CHAT}),
    "tpa_respond": frozenset({RESOURCE_CHAT}),
    "tpa_request": frozenset({RESOURCE_CHAT}),
    "equip_armor": frozenset({RESOURCE_CONTAINER}),        # 可能去已知容器找甲
}


def _build_tool_resources() -> dict[str, frozenset[str]]:
    """从注册表 ``TOOL_SPECS`` 生成 `工具 → 资源`（工具数是注册表说了算）。"""
    try:
        from .tool_registry import TOOL_SPECS
    except Exception:  # pragma: no cover — 注册表不可用时退化为空表（不猜资源）
        logger.warning("[ResourceLease] 工具注册表不可用，工具资源表为空")
        return {}
    out: dict[str, frozenset[str]] = {}
    for spec in TOOL_SPECS:
        res: set[str] = set()
        for group in spec.groups:
            res |= _GROUP_RESOURCES.get(group, frozenset())
        out[spec.name] = frozenset(res)
    out.update(_TOOL_RESOURCE_OVERRIDES)
    return out


TOOL_RESOURCES: dict[str, frozenset[str]] = _build_tool_resources()


def resources_for_tool(tool_name: Any) -> frozenset[str]:
    """某工具要占哪些资源（未登记的工具＝不占资源，诚实返回空集，不猜）。"""
    return TOOL_RESOURCES.get(str(tool_name or "").strip().lower(), frozenset())


# ── 模式（状态）→ 资源 ─────────────────────────────────────────
# P0 生存反射永远 ``survival=True``（见 ``PRIORITY_SURVIVAL``）。
MODE_RESOURCES: Mapping[str, frozenset[str]] = {
    "self_preservation": frozenset({RESOURCE_MOVE}),
    "self_heal": frozenset({RESOURCE_USE}),
    "cowardice": frozenset({RESOURCE_MOVE}),
    "self_defense": frozenset({RESOURCE_ATTACK}),
    "stuck_recovery": frozenset({RESOURCE_MOVE, RESOURCE_USE}),
}


# P0 生存反射在技能层写控制流时用的唯一持租人身份。
OWNER_P0_REFLEX = "p0:reflex"


# ── 错误 ───────────────────────────────────────────────────────
class ResourceLeaseError(Exception):
    """资源租约相关错误的基类。"""


def normalize_resources(resources: Any) -> frozenset[str]:
    """把 ``str`` / 可迭代 归一成合法资源集（未知资源名 → ``ValueError``，不假装）。"""
    if resources is None:
        return frozenset()
    if isinstance(resources, str):
        items = [resources]
    else:
        try:
            items = list(resources)
        except TypeError as exc:  # pragma: no cover
            raise ValueError(f"资源集不可迭代：{resources!r}") from exc
    out: set[str] = set()
    for raw in items:
        name = str(raw or "").strip().lower()
        if not name:
            continue
        if name not in RESOURCES:
            raise ValueError(f"未知资源 {name!r}；合法资源：{sorted(RESOURCES)}")
        out.add(name)
    return frozenset(out)


def conflicts(a: Any, b: Any) -> bool:
    """两**组**资源是否冲突（chat 永远并行；独占资源排斥一切）。"""
    ra = normalize_resources(a) - ALWAYS_PARALLEL
    rb = normalize_resources(b) - ALWAYS_PARALLEL
    if not ra or not rb:
        return False
    if ra & rb:
        return True
    return bool((ra | rb) & EXCLUSIVE_RESOURCES)


def _clamp_priority(value: Any, default: int = PRIORITY_DEFAULT) -> int:
    try:
        num = int(value)
    except (TypeError, ValueError):
        return default
    return PRIORITY_MIN if num < PRIORITY_MIN else (PRIORITY_MAX if num > PRIORITY_MAX else num)


# ── 授权记录 ───────────────────────────────────────────────────
@dataclass(frozen=True)
class LeaseGrant:
    """一个持租人的当前占用（谁 / 占什么 / 多高优先级 / 为什么 / 何时）。"""

    owner: str
    resources: frozenset[str]
    priority: int
    reason: str
    granted_at: float
    survival: bool = False
    kind: str = ""
    target: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "owner": self.owner,
            "resources": sorted(self.resources),
            "priority": self.priority,
            "reason": self.reason,
            "granted_at": self.granted_at,
            "survival": self.survival,
            "kind": self.kind,
            "target": self.target,
        }


# ── 资源租约 ───────────────────────────────────────────────────
class ResourceLease:
    """按资源分租的仲裁器：同资源互斥、异资源并行、生存反射永远可抢。

    时钟与审计 sink 可注入（无 wire 的测试可完整驱动，与 ``ControlLease`` 同一套路）。
    所有公开方法都在同一把 RLock 下运行，可被 mod 事件线程与 asyncio 主循环并发调用。
    """

    def __init__(self, *, clock: Callable[[], float] = time.time,
                 audit: Optional[Callable[[dict], None]] = None):
        self._clock = clock
        self._audit_sink = audit
        self._lock = threading.RLock()
        self._grants: dict[str, LeaseGrant] = {}
        self._pending: list[dict] = []
        self._events: list[dict] = []
        self._seq = 0
        self._pending_seq = 0

    # ── 审计 ──

    def _record(self, kind: str, *, owner: str, resources: Iterable[str],
                priority: int = PRIORITY_DEFAULT, reason: str = "", actor: str = "",
                detail: Optional[Mapping[str, Any]] = None) -> dict:
        self._seq += 1
        event = {
            "seq": self._seq,
            "kind": kind,
            "at": self._clock(),
            "owner": str(owner),
            "actor": str(actor or owner),
            "resources": sorted(normalize_resources(resources)),
            "priority": int(priority),
            "reason": str(reason or ""),
            "detail": dict(detail or {}),
        }
        self._events.append(event)
        del self._events[:-AUDIT_MAX]
        if self._audit_sink is not None:
            try:
                self._audit_sink(dict(event))
            except Exception as exc:  # noqa: BLE001 — 审计 sink 坏掉绝不拖垮主链路
                logger.warning("[ResourceLease] 审计 sink 失败: %s", exc)
        if kind == LEASE_PREEMPT:
            logger.info("[ResourceLease] 抢占：%s 抢了 %s 的 %s（理由=%s）",
                        owner, event["detail"].get("victim"), sorted(normalize_resources(resources)),
                        reason or event["detail"].get("victim"))
        return event

    def events(self, n: int = 0) -> list[dict]:
        """只读审计列表（``n=0`` 全量；否则最近 ``n`` 条）。"""
        with self._lock:
            rows = list(self._events) if not n else list(self._events[-int(n):])
        return [dict(row, resources=list(row["resources"]),
                     detail=dict(row["detail"])) for row in rows]

    def audit_log(self, n: int = 10) -> list[dict]:
        """最近 ``n`` 条审计（与 ``control_lease`` / ``permissions`` 同名口径）。"""
        return self.events(int(n))

    # ── 只读状态 ──

    def holders(self) -> list[dict]:
        """当前持租人快照（拷贝）。"""
        with self._lock:
            return [grant.as_dict() for grant in self._grants.values()]

    def pending(self) -> list[dict]:
        """排队中的请求快照（拷贝）。"""
        with self._lock:
            return [{"owner": p["owner"], "resources": sorted(p["resources"]),
                     "priority": p["priority"], "reason": p["reason"],
                     "kind": p["kind"], "target": p["target"],
                     "survival": p["survival"]} for p in self._pending]

    def is_held_by(self, owner: str) -> bool:
        with self._lock:
            return str(owner) in self._grants

    def conflicts_for(self, resources: Any, *, exclude: Optional[str] = None) -> list[str]:
        """与这组资源冲突的当前持租人名单（只读；不改变任何状态）。"""
        res = normalize_resources(resources)
        with self._lock:
            return [owner for owner, grant in self._grants.items()
                    if owner != exclude and self._conflicts(res, grant.resources)]

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "holders": [grant.as_dict() for grant in self._grants.values()],
                "pending": self.pending(),
                "seq": self._seq,
                "events": len(self._events),
            }

    # ── 冲突 / 授予（调用方需已持锁或经公开方法）──

    @staticmethod
    def _conflicts(a: frozenset[str], b: frozenset[str]) -> bool:
        return conflicts(a, b)

    def _grant_locked(self, owner: str, resources: frozenset[str], priority: int,
                      reason: str, survival: bool, kind: str, target: str) -> LeaseGrant:
        grant = LeaseGrant(
            owner=owner, resources=frozenset(resources), priority=int(priority),
            reason=str(reason or ""), granted_at=self._clock(),
            survival=bool(survival), kind=str(kind or ""), target=str(target or ""),
        )
        self._grants[owner] = grant
        return grant

    def _enqueue(self, owner: str, resources: frozenset[str], priority: int,
                 reason: str, survival: bool, kind: str, target: str) -> None:
        self._pending_seq += 1
        self._pending = [p for p in self._pending if p["owner"] != owner]
        self._pending.append({
            "owner": owner, "resources": frozenset(resources), "priority": int(priority),
            "reason": str(reason or ""), "survival": bool(survival),
            "kind": str(kind or ""), "target": str(target or ""), "seq": self._pending_seq,
        })
        if len(self._pending) > PENDING_MAX:
            # 超出上限先丢最低优先级（同优先级丢最老的）。
            self._pending.sort(key=lambda p: (p["priority"], p["seq"]))
            self._pending = self._pending[len(self._pending) - PENDING_MAX:]

    def _drain_pending(self) -> None:
        """资源刚空出来：按优先级把能放行的排队请求放行（不再抢占，只填空）。"""
        if not self._pending:
            return
        remaining: list[dict] = []
        for req in sorted(self._pending, key=lambda p: (-p["priority"], p["seq"])):
            owner = req["owner"]
            existing = self._grants.get(owner)
            target_res = (existing.resources | req["resources"]) if existing else req["resources"]
            blocked = any(self._conflicts(target_res, g.resources)
                          for o, g in self._grants.items() if o != owner)
            if blocked:
                remaining.append(req)
                continue
            self._grant_locked(owner, target_res, req["priority"], req["reason"],
                               req["survival"], req["kind"], req["target"])
            self._record(LEASE_GRANT, owner=owner, resources=target_res,
                         priority=req["priority"], reason=req["reason"] or "排队放行",
                         detail={"dequeued": True, "kind": req["kind"],
                                 "target": req["target"], "survival": req["survival"]})
        self._pending = remaining

    # ── 公开 API：请求 / 释放 / 撤销 ──────────────────────────────

    def request(self, owner: str, resources: Any, *, priority: int = PRIORITY_DEFAULT,
                reason: str = "", survival: bool = False, kind: str = "",
                target: str = "", actor: str = "") -> dict:
        """请求占用一组资源。

        返回字典字段：``granted`` / ``queued`` / ``renew`` / ``owner`` / ``resources`` /
        ``preempted``（被抢的持租人名单）/ ``blocked_by`` / ``reason`` / ``event``。

        * 同资源 ⇒ 冲突；冲突时：**生存请求**或优先级**严格高于全部**冲突方 → 抢占；
          否则**排队**（留一行 ``queue`` 审计，调用方应把"为什么没让做"如实回给模型）；
        * 异资源（或 ``chat``）⇒ 不冲突，直接授予；
        * 同一 owner 重复请求**完全相同**的资源/优先级 ⇒ 静默续租（不打审计，避免每 tick 刷屏）。
        """
        owner = str(owner or "").strip()
        if not owner:
            raise ValueError("owner 必须是非空身份串")
        res = normalize_resources(resources)
        if survival:
            priority = PRIORITY_SURVIVAL
        priority = _clamp_priority(priority)
        with self._lock:
            existing = self._grants.get(owner)
            if existing is not None and existing.resources == res and existing.priority == priority:
                return {"granted": True, "queued": False, "renew": True, "owner": owner,
                        "resources": sorted(res), "preempted": [], "blocked_by": [],
                        "reason": existing.reason, "event": None}
            target_res = (existing.resources | res) if existing is not None else res
            if not target_res:
                # 空资源请求 = 不占任何东西：直接放行，不留审计（无事发生）。
                return {"granted": True, "queued": False, "renew": bool(existing), "owner": owner,
                        "resources": [], "preempted": [], "blocked_by": [],
                        "reason": str(reason or ""), "event": None}
            conflicts_now = [(o, g) for o, g in self._grants.items()
                             if o != owner and self._conflicts(target_res, g.resources)]
            if not conflicts_now:
                self._grant_locked(owner, target_res, priority, reason, survival, kind, target)
                event = self._record(LEASE_GRANT, owner=owner, resources=target_res,
                                     priority=priority, reason=reason, actor=actor,
                                     detail={"kind": kind, "target": target, "survival": survival,
                                             "extended": existing is not None})
                self._drain_pending()
                return {"granted": True, "queued": False, "renew": False, "owner": owner,
                        "resources": sorted(target_res), "preempted": [], "blocked_by": [],
                        "reason": str(reason or ""), "event": event}
            # 有冲突：生存请求永远可抢；普通请求只在**全部**冲突方都严格更低时抢。
            can_preempt = survival or all(
                (not g.survival) and g.priority < priority for _o, g in conflicts_now)
            if can_preempt:
                preempted: list[str] = []
                for victim, g in list(conflicts_now):
                    del self._grants[victim]
                    self._record(LEASE_PREEMPT, owner=owner, resources=g.resources,
                                 priority=priority, reason=reason or f"抢占 {victim}", actor=actor,
                                 detail={"victim": victim, "victim_priority": g.priority,
                                         "victim_resources": sorted(g.resources),
                                         "victim_reason": g.reason, "kind": kind,
                                         "target": target, "survival": survival})
                    preempted.append(victim)
                self._grant_locked(owner, target_res, priority, reason, survival, kind, target)
                event = self._record(LEASE_GRANT, owner=owner, resources=target_res,
                                     priority=priority, reason=reason, actor=actor,
                                     detail={"kind": kind, "target": target, "survival": survival,
                                             "preempted": list(preempted)})
                self._drain_pending()
                return {"granted": True, "queued": False, "renew": False, "owner": owner,
                        "resources": sorted(target_res), "preempted": preempted,
                        "blocked_by": [], "reason": str(reason or ""), "event": event}
            # 排队（并如实回报被谁挡住）。
            self._enqueue(owner, target_res, priority, reason, survival, kind, target)
            blocked_by = [o for o, _g in conflicts_now]
            event = self._record(LEASE_QUEUE, owner=owner, resources=target_res,
                                 priority=priority, reason=reason, actor=actor,
                                 detail={"kind": kind, "target": target, "survival": survival,
                                         "blocked_by": blocked_by})
            return {"granted": False, "queued": True, "renew": False, "owner": owner,
                    "resources": sorted(target_res), "preempted": [], "blocked_by": blocked_by,
                    "reason": str(reason or ""), "event": event}

    def release(self, owner: str, reason: str = "", *, actor: str = "") -> Optional[dict]:
        """释放该 owner 的占用（顺带丢掉它的排队请求）；无占用则返回 ``None``。"""
        owner = str(owner)
        with self._lock:
            grant = self._grants.pop(owner, None)
            dropped = any(p["owner"] == owner for p in self._pending)
            self._pending = [p for p in self._pending if p["owner"] != owner]
            if grant is None:
                return None
            event = self._record(LEASE_RELEASE, owner=owner, resources=grant.resources,
                                 priority=grant.priority, reason=reason, actor=actor,
                                 detail={"kind": grant.kind, "target": grant.target,
                                         "dropped_pending": dropped})
            self._drain_pending()
            return event

    def revoke(self, owner: str, reason: str, *, actor: str = "governor") -> Optional[dict]:
        """**撤销**占用（治理用：卡死被摘掉时走它）——必须带真实理由 ✓。"""
        if not reason or not str(reason).strip():
            raise ResourceLeaseError("撤销必须带理由（reason 不能为空）")
        owner = str(owner)
        with self._lock:
            grant = self._grants.pop(owner, None)
            dropped = any(p["owner"] == owner for p in self._pending)
            self._pending = [p for p in self._pending if p["owner"] != owner]
            if grant is None:
                return None
            event = self._record(LEASE_CANCEL, owner=owner, resources=grant.resources,
                                 priority=grant.priority, reason=reason, actor=actor,
                                 detail={"kind": grant.kind, "target": grant.target,
                                         "dropped_pending": dropped})
            self._drain_pending()
            return event


# ── 接线辅助：把 P0 反射的控制流占用如实登记进资源租约 ──────────────
def register_survival_hold(lease: Optional[ResourceLease], *, owner: str = OWNER_P0_REFLEX,
                           resources: Any, reason: str = "", kind: str = "reflex",
                           target: str = "", actor: str = "system") -> Optional[dict]:
    """把一次 P0 反射的控制流（drive）占用登记进资源租约。

    ``lease is None``（没接线）→ 返回 ``None``，**不做任何事**（行为照旧）。登记失败
    （未知资源等）只 warning，**绝不拖垮 P0 逃生链路** ✗。
    """
    if lease is None:
        return None
    try:
        return lease.request(owner, resources, priority=PRIORITY_SURVIVAL, survival=True,
                             reason=reason or "P0 生存反射占用控制流", kind=kind,
                             target=target, actor=actor)
    except Exception as exc:  # noqa: BLE001 — 登记失败绝不影响逃生
        logger.warning("[ResourceLease] P0 占用登记失败（不影响逃生）: %s", exc)
        return None
