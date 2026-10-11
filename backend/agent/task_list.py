"""
Task List — 多任务队列 + 归属 + 权限规则（M3-19 §6）。

设计取舍：
- 本模块是**纯数据结构**：只维护内存里的任务条目与权限判断，**不下发任何 wire 指令**。
  取消 / 插队真正的副作用（发命令、改模式）由调用方处理，避免数据层耦合进传输层。
- 归属用 `owner_tier`（master / friend / guest）+ `owner`（玩家名）表示；
  默认值刻意不绑定任何具体角色 IP，tier 只是权重标签。
- 权限：master 可管任意任务；friend 只能管自己的（或"无主但属于 friend 层"的）；
  guest 在公开服无权。规则集中在本模块，仪表盘与 planner 都来问它。
- 去重：同一 (kind, owner) 的活跃任务不重复入队，防止反射层反复 `task_state` 刷爆队列。
- 容量：`max_items` 兜底 + `prune()` 只裁终态，保证长时间运行不无限增长。
- 线程安全：后端有 mod 事件线程与 asyncio 主循环两条写入路径，故内部统一 RLock。
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

logger = logging.getLogger("task_list")

STATUS_PENDING, STATUS_ACTIVE, STATUS_DONE, STATUS_FAILED, STATUS_CANCELLED = (
    "pending", "active", "done", "failed", "cancelled")
# 终态：不再变化、不再参与去重、可被 prune 回收。
TERMINAL_STATUSES = {"done", "failed", "cancelled"}

# tier → 缺省优先级。master 的活先干，guest 的活垫底但仍有权重（不设 0，避免全被忽略）。
_TIER_PRIORITY = {"master": 80, "friend": 50, "guest": 20}
_DEFAULT_TIER_PRIORITY = 20


def _clamp01(value: float, default: float = 0.0) -> float:
    """进度夹到 [0, 1]；非法值退回 default，避免 NaN/字符串污染 to_dict。"""
    try:
        num = float(value)
    except (TypeError, ValueError):
        return float(default)
    if num != num:  # NaN
        return float(default)
    return 0.0 if num < 0.0 else (1.0 if num > 1.0 else num)


def _clamp100(value: int, default: int = 0) -> int:
    try:
        num = int(value)
    except (TypeError, ValueError):
        return int(default)
    return 0 if num < 0 else (100 if num > 100 else num)


@dataclass
class TaskItem:
    """一条任务。字段刻意保持扁平、可 JSON 直出，方便 `/api/status` 与仪表盘直接消费。"""

    id: str
    kind: str
    owner: str = ""
    owner_tier: str = "guest"
    status: str = STATUS_PENDING
    priority: int = 0
    progress: float = 0.0
    detail: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0
    failure_reason: str = ""
    # **模型自己创建的**任务（`set_goal` 立的目标）——编排轮次的唯一触发条件：
    # 后台/自主/反射行为建的任务不带这个标（`docs/dev/AGENT_ARCH.md` §10.2 #3）。
    model_created: bool = False

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "owner": self.owner,
            "owner_tier": self.owner_tier,
            "status": self.status,
            "priority": self.priority,
            "progress": self.progress,
            "detail": self.detail,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "failure_reason": self.failure_reason,
            "model_created": self.model_created,
        }


class TaskList:
    """任务队列。所有公开方法都在锁内运行，可被多线程安全调用。"""

    def __init__(self, max_items: int = 50, clock: Callable[[], float] = time.time,
                 id_factory: Optional[Callable[[], str]] = None):
        self.max_items = max(1, int(max_items))
        self._clock = clock
        self._lock = threading.RLock()
        self._items: dict[str, TaskItem] = {}
        self._seq = 0
        self._id_factory = id_factory or self._next_id

    # ── 内部工具（调用方需已持锁） ──

    def _next_id(self) -> str:
        self._seq += 1
        return f"task_{self._seq}"

    def _new_id(self) -> str:
        return str(self._id_factory())

    def _find_active(self, kind: str, owner: str) -> Optional[TaskItem]:
        """按 (kind, owner) 找活跃任务；用于去重与 touch。"""
        candidates = [i for i in self._items.values()
                      if i.kind == kind and i.owner == owner
                      and i.status not in TERMINAL_STATUSES]
        if not candidates:
            return None
        # 最近创建的一条优先（touch 语义就是"接着上一条继续上报"）。
        return max(candidates, key=lambda i: (i.created_at, i.updated_at))

    def _make_room(self) -> None:
        """容量已满时腾位置：先裁最旧的终态，再裁最旧的其它任务。"""
        if len(self._items) < self.max_items:
            return
        overflow = len(self._items) - self.max_items + 1
        terminal = sorted((i for i in self._items.values() if i.status in TERMINAL_STATUSES),
                          key=lambda i: (i.updated_at, i.created_at))
        for item in terminal[:overflow]:
            self._items.pop(item.id, None)
            overflow -= 1
        if overflow <= 0:
            return
        # 全是活跃任务也要保证有界：牺牲最旧的一条（正常 max_items 远大于并发数）。
        rest = sorted(self._items.values(), key=lambda i: (i.created_at, i.updated_at))
        for item in rest[:overflow]:
            self._items.pop(item.id, None)

    def _apply(self, item: TaskItem, **fields) -> None:
        """把允许的字段应用到条目；未知字段忽略。终态切换时补齐 progress/时间。"""
        if "status" in fields and fields["status"] is not None:
            item.status = str(fields["status"])
        if "priority" in fields and fields["priority"] is not None:
            item.priority = _clamp100(fields["priority"], item.priority)
        if "progress" in fields and fields["progress"] is not None:
            item.progress = _clamp01(fields["progress"], item.progress)
        if "detail" in fields and fields["detail"] is not None:
            item.detail = str(fields["detail"])
        if "failure_reason" in fields and fields["failure_reason"] is not None:
            item.failure_reason = str(fields["failure_reason"])
        if "model_created" in fields and fields["model_created"] is not None:
            item.model_created = bool(fields["model_created"])
        if item.status in TERMINAL_STATUSES:
            if item.status == STATUS_DONE:
                item.progress = 1.0
            # failed / cancelled 保留当前进度（做了一半的信息对仪表盘有用）。
        item.updated_at = self._clock()

    # ── 写入 ──

    def add(self, kind: str, *, owner: str = "", owner_tier: str = "guest",
            priority: Optional[int] = None, detail: str = "",
            status: str = STATUS_PENDING, progress: float = 0.0,
            model_created: bool = False) -> TaskItem:
        """入队。返回新条目；若同 (kind, owner) 已有活跃任务，直接返回那一条（不重复入队）。"""
        with self._lock:
            existing = self._find_active(kind, owner)
            if existing is not None and status not in TERMINAL_STATUSES:
                return existing
            tier = str(owner_tier)
            if priority is None:
                priority = _TIER_PRIORITY.get(tier, _DEFAULT_TIER_PRIORITY)
            self._make_room()
            now = self._clock()
            item = TaskItem(
                id=self._new_id(),
                kind=kind,
                owner=owner,
                owner_tier=tier,
                status=status,
                priority=_clamp100(priority),
                progress=_clamp01(progress),
                detail=detail,
                created_at=now,
                updated_at=now,
                model_created=bool(model_created),
            )
            if item.status in TERMINAL_STATUSES and item.status == STATUS_DONE:
                item.progress = 1.0
            self._items[item.id] = item
            return item

    def get(self, task_id: str) -> Optional[TaskItem]:
        with self._lock:
            return self._items.get(task_id)

    def update(self, task_id: str, **fields) -> Optional[TaskItem]:
        """更新允许的字段（status/priority/progress/detail/failure_reason）。"""
        with self._lock:
            item = self._items.get(task_id)
            if item is None:
                return None
            self._apply(item, **fields)
            return item

    def touch(self, kind: str, *, owner: str = "", progress: Optional[float] = None,
              detail: str = "", status: Optional[str] = None,
              failure_reason: str = "") -> Optional[TaskItem]:
        """供 `task_state` 事件上报：更新既有活跃任务，找不到就新建。

        空字符串视为"未提供"（不覆盖旧值），避免只报进度时把 detail/失败原因冲掉。
        """
        with self._lock:
            item = self._find_active(kind, owner)
            if item is None:
                return self.add(kind, owner=owner, status=status or STATUS_PENDING,
                                progress=progress if progress is not None else 0.0,
                                detail=detail)
            fields: dict = {}
            if progress is not None:
                fields["progress"] = progress
            if detail:
                fields["detail"] = detail
            if status is not None:
                fields["status"] = status
            if failure_reason:
                fields["failure_reason"] = failure_reason
            if fields:
                self._apply(item, **fields)
            return item

    def cancel(self, task_id: str, *, actor_tier: str = "guest",
               actor: str = "") -> tuple[bool, str]:
        """取消任务。返回 (是否成功, 原因)。"""
        with self._lock:
            item = self._items.get(task_id)
            if item is None:
                return False, "not_found"
            if not self.can_manage(item, actor_tier=actor_tier, actor=actor):
                return False, "denied"
            if item.status in TERMINAL_STATUSES:
                return False, "already_terminal"
            self._apply(item, status=STATUS_CANCELLED)
            return True, "cancelled"

    def activate(self, task_id: str, *, actor_tier: str = "guest",
                 actor: str = "") -> tuple[bool, str]:
        """插队：把目标任务置 active 并抬到当前最高优先级，其余 active 降回 pending。

        "同一时刻只有一个 active"由本方法维护，调用方不必自己协调。
        """
        with self._lock:
            item = self._items.get(task_id)
            if item is None:
                return False, "not_found"
            if not self.can_manage(item, actor_tier=actor_tier, actor=actor):
                return False, "denied"
            if item.status in TERMINAL_STATUSES:
                return False, "already_terminal"
            top = max((i.priority for i in self._items.values()), default=0)
            for other in self._items.values():
                if other is not item and other.status == STATUS_ACTIVE:
                    self._apply(other, status=STATUS_PENDING)
            self._apply(item, status=STATUS_ACTIVE, priority=_clamp100(top + 1))
            return True, "activated"

    def complete(self, task_id: str, *, success: bool = True,
                 reason: str = "") -> Optional[TaskItem]:
        """标记完成 / 失败。失败原因（如 "stuck: ..."）会被仪表盘直接展示。"""
        with self._lock:
            item = self._items.get(task_id)
            if item is None:
                return None
            if success:
                self._apply(item, status=STATUS_DONE)
            else:
                self._apply(item, status=STATUS_FAILED, failure_reason=reason or item.failure_reason)
            return item

    # ── 读取 ──

    def active(self) -> Optional[TaskItem]:
        """当前唯一 active 的任务（正常情况下至多一条）。"""
        with self._lock:
            for item in self.items():
                if item.status == STATUS_ACTIVE:
                    return item
            return None

    def items(self, *, limit: Optional[int] = None) -> list[TaskItem]:
        """排序：active 最先 → pending → 终态最后；同组按 (-priority, created_at)。"""
        with self._lock:
            ordered = sorted(self._items.values(), key=_sort_key)
            if limit is not None:
                return ordered[:max(0, int(limit))]
            return ordered

    def prune(self, keep: int = 50) -> int:
        """终态任务最多保留 `keep` 条（按 updated_at 新的优先），返回删除条数。"""
        keep = max(0, int(keep))
        with self._lock:
            terminal = sorted((i for i in self._items.values()
                               if i.status in TERMINAL_STATUSES),
                              key=lambda i: (i.updated_at, i.created_at), reverse=True)
            doomed = terminal[keep:]
            for item in doomed:
                self._items.pop(item.id, None)
            return len(doomed)

    def status(self) -> dict:
        """给 `/api/status` 用的快照；结构保证可 JSON 直出。"""
        with self._lock:
            items = self.items()
            active = next((i for i in items if i.status == STATUS_ACTIVE), None)
            return {
                "items": [i.to_dict() for i in items],
                "active": active.to_dict() if active else None,
                "count": len(items),
                "pending": sum(1 for i in items if i.status == STATUS_PENDING),
                "failed": sum(1 for i in items if i.status == STATUS_FAILED),
            }

    @staticmethod
    def can_manage(item: TaskItem, *, actor_tier: str, actor: str) -> bool:
        """权限判定：master 任意；friend 只管自己的；guest 无权。"""
        if actor_tier == "master":
            return True
        if actor_tier == "friend":
            if not item.owner:
                # 无主任务：只允许 friend 层认领/管理，避免 guest 借空 owner 提权。
                return item.owner_tier == "friend"
            return item.owner == actor
        return False


# 分组序：active=0 → pending=1 → 终态=2。
_GROUP_ORDER = {
    STATUS_ACTIVE: 0,
    STATUS_PENDING: 1,
}


def _sort_key(item: TaskItem) -> tuple:
    return (_GROUP_ORDER.get(item.status, 2), -item.priority, item.created_at)
