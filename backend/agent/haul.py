"""搬运（haul）—— 容器 ↔ 容器 / 容器 ↔ 背包 的**算量 + 计划 + 对账**（纯逻辑）。

用户口径（设计见 `docs/dev/TERRITORY.md` §4）：

- 箱子「用于**传递物品**」：搬运 = 从 A 取、走到 B、放进 B；
- **算量受三约束**：目标侧**剩余空间** + 本次**需求** + 单次**上限**（沿用借用的 256 口径）；
- **不许整箱搬空**：源箱每件物品至少留底 `SOURCE_FLOOR` 个（绝不动最后一件）；
- **任何情况不许顺手从别人箱子拿**：`no_touch` 一律不碰、`borrow_only` 只能走借用台账
  （这两条在 planner 的入口门里判，本模块不重复实现策略）；
- **两侧对账**：搬前/搬后 `items` 对比，数量不符 → 如实报 `verify_failed`，绝不掩盖；
- 失败回执逐字契约：`{ok:false, step, from, to, moved:{item,count}, reason}`。

本模块只做纯函数与数据（好测、可离线验证）：发帧与跨轮推进在 planner 里。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from .borrow import (container_entries, container_slots_with, player_inv_start,
                     player_slots_with)
from .craft import normalize_item

# 单趟搬运上限：沿用借用台账的 256 口径（用户口径里的"256 上限"）。
HAUL_MAX_COUNT = 256
# 源箱留底：每件物品**至少留 1 个**（绝不取走最后一件 —— 「不许整箱搬空」）。
# 只管**源箱**（容器侧）；从自己背包往外搬不受这条约束（那是自己的东西）。
SOURCE_FLOOR = 1
# 叠数按 64 算空间（本模块只做"放不放得下"的估算；真正的移动量以快照里的实际数量为准）。
STACK = 64

# ── 步骤名（失败回执里的 `step`，逐字契约）──────────────────────
STEP_WALK_SOURCE = "walk_to_source"
STEP_OPEN_SOURCE = "open_source"
STEP_TAKE = "take"
STEP_WALK_TARGET = "walk_to_target"
STEP_OPEN_TARGET = "open_target"
STEP_PUT = "put"
STEP_VERIFY = "verify"
STEP_DONE = "done"
HAUL_STEPS: tuple[str, ...] = (
    STEP_WALK_SOURCE, STEP_OPEN_SOURCE, STEP_TAKE, STEP_WALK_TARGET,
    STEP_OPEN_TARGET, STEP_PUT, STEP_VERIFY, STEP_DONE,
)

# ── 侧别（同一份容器菜单快照里：箱子侧 = 槽位 < player_inv_start）──
SIDE_CONTAINER = "container"
SIDE_PLAYER = "player"

# 对账前最多重开几次容器去等快照（等不到就**如实报**"没等到对账快照"，不无限重试）。
HAUL_MAX_ATTEMPTS = 3
# 等搬前/搬后快照的上限（秒）：Java 侧容器内容一变就立刻上报，超过这个时间还没来
# 就当对账做不了 → **如实报**，绝不假装搬完了。
HAUL_VERIFY_TIMEOUT = 5.0
# 走去容器的上限（秒）：寻路失败/卡住时**如实报**"走不到"（绝不靠放方块解决地形）。
HAUL_WALK_TIMEOUT = 60.0


def _as_int(value: Any) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# ── 快照读数（认不出的一律跳过，绝不猜）────────────────────────


def has_layout(open_container: Any) -> bool:
    """这份快照能不能分清"箱子侧 / 背包侧"（分不清就别拿它算量）。"""
    return player_inv_start(open_container) > 0


def side_items(open_container: Any, side: str = SIDE_CONTAINER) -> dict[str, int]:
    """某一侧的物品计数 `{item: total}`（物品 id 认不出的槽位不计入，也不当空格）。"""
    start = player_inv_start(open_container)
    want_container = str(side) != SIDE_PLAYER
    out: dict[str, int] = {}
    for row in container_entries(open_container):
        if (int(row["slot"]) < start) != want_container:
            continue
        item = row["item"]
        if not item:
            continue
        out[item] = out.get(item, 0) + int(row["count"] or 0)
    return out


def side_free_space(open_container: Any, item: Any, side: str = SIDE_CONTAINER) -> int:
    """某一侧对这件物品的剩余空间（能再叠进去 + 空槽；布局未知 → 0）。

    只认**容器侧显示出来的非空槽**（Java `open_container.items` 只报非空槽），
    空槽按 `range(start)` 推算 —— 不足一个"槽位布局"的快照一律返回 0（不假装放得下）。
    """
    start = player_inv_start(open_container)
    slots = _as_int(open_container.get("slots")) if isinstance(open_container, dict) else None
    if start <= 0 or slots is None or slots <= start:
        return 0
    item_id = normalize_item(item)
    want_container = str(side) != SIDE_PLAYER
    low, high = (0, start) if want_container else (start, slots)
    rows = [row for row in container_entries(open_container) if low <= row["slot"] < high]
    free = sum(max(0, STACK - int(row["count"])) for row in rows
               if row["item"] == item_id and int(row["count"]) > 0)
    occupied = {row["slot"] for row in rows if int(row["count"]) > 0}
    empty = sum(1 for slot in range(low, high) if slot not in occupied)
    return free + empty * STACK


# ── 算量 ────────────────────────────────────────────────────────


def plan_haul(src_oc: Any, *, src_side: str = SIDE_CONTAINER, item: Any = "",
              want: Any = 0, capacity: Optional[dict] = None,
              max_count: int = HAUL_MAX_COUNT,
              source_floor: int = SOURCE_FLOOR) -> dict:
    """算这一趟**搬多少**（纯函数，不看策略、不发帧）。

    * `item` 给了 → 只搬这一件，取 `min(需求, 源箱可用-留底, 目标侧剩余, 单趟上限)`；
    * `item` 省略 → 搬源箱里**全部可搬物**，每件都受留底/空间约束，总量不超过单趟上限；
    * `capacity` 是**目标侧**剩余空间 `{item: free}`（None = 未知，只按上限节流 ——
      真正放不放得下在 `put` 那一步用真实快照再卡一次）。

    返回 `{"items": {item: take}, "reason": "", "blocked": {...}}`；
    `reason` 非空表示一件都搬不了（`source_empty` / `item_absent` / `source_floor` /
    `target_full` / `nothing_to_move`）。
    """
    src = side_items(src_oc, src_side)
    item_id = normalize_item(item) if item else ""
    floor = max(0, int(source_floor))
    cap_total = max(1, int(max_count))

    if not src:
        return {"items": {}, "reason": "source_empty", "blocked": {},
                "available": {}, "capacity": dict(capacity or {})}
    if item_id and item_id not in src:
        return {"items": {}, "reason": "item_absent", "blocked": {},
                "available": src, "capacity": dict(capacity or {})}

    kinds = [item_id] if item_id else sorted(src)
    takes: dict[str, int] = {}
    blocked: dict[str, str] = {}
    budget = cap_total if not item_id else (int(want) if _as_int(want) and int(want) > 0 else None)

    for kind in kinds:
        available = int(src.get(kind, 0)) - floor
        if available <= 0:
            blocked[kind] = "source_floor"
            continue
        if budget is not None and budget <= 0:
            blocked[kind] = "trip_limit"
            continue
        room = None
        if isinstance(capacity, dict):
            room = _as_int(capacity.get(kind))
        if room is not None and room <= 0:
            blocked[kind] = "target_full"
            continue
        take = available
        if room is not None:
            take = min(take, room)
        if budget is not None:
            take = min(take, budget)
        take = min(take, cap_total)
        if take <= 0:
            blocked[kind] = "target_full" if room == 0 else "nothing_to_move"
            continue
        takes[kind] = int(take)
        if budget is not None:
            budget -= int(take)

    if not takes:
        reason = "source_floor" if blocked and all(v == "source_floor" for v in blocked.values()) \
            else "target_full" if blocked and all(v == "target_full" for v in blocked.values()) \
            else "nothing_to_move"
        return {"items": {}, "reason": reason, "blocked": blocked, "available": src,
                "capacity": dict(capacity or {})}
    return {"items": takes, "reason": "", "blocked": blocked, "available": src,
            "capacity": dict(capacity or {})}


def _target_slot(occupied: dict, item_id: str, start: int, end: int) -> Optional[tuple]:
    """目标侧能放下这件物品的槽位与余量：先叠同类，再占空格。"""
    for slot in range(start, end):
        row = occupied.get(slot)
        count = int((row or {}).get("count") or 0)
        if row is None or count <= 0:
            return slot, STACK
        if row.get("item") == item_id and count < STACK:
            return slot, STACK - count
    return None


def plan_take_moves(open_container: Any, item: Any, count: Any) -> list[dict]:
    """容器侧 → 背包侧：`container_move` 参数（一格一条，最多一叠）。

    背包放不下（没有可用槽位）就只返回能放下的那几条 —— **剩下的原地不动**。
    """
    item_id = normalize_item(item)
    want = _as_int(count) or 0
    start = player_inv_start(open_container)
    slots = _as_int(open_container.get("slots")) if isinstance(open_container, dict) else None
    if not item_id or want <= 0 or start <= 0 or slots is None or slots <= start:
        return []
    rows = container_entries(open_container)
    occupied = {row["slot"]: row for row in rows if int(row["count"]) > 0}
    moves: list[dict] = []
    remaining = int(want)
    for stack in container_slots_with(open_container, item_id):
        if remaining <= 0:
            break
        while remaining > 0 and int(stack["count"]) > 0:
            target = _target_slot(occupied, item_id, start, slots)
            if target is None:
                return moves
            slot, room = target
            have = int(stack["count"]) - sum(
                move["count"] for move in moves if move["from_slot"] == stack["slot"])
            take = min(room, have, remaining)
            if take <= 0:
                break
            moves.append({"from_slot": int(stack["slot"]), "to_slot": int(slot),
                          "count": int(take)})
            occupied[slot] = {"slot": slot, "item": item_id,
                              "count": int((occupied.get(slot) or {}).get("count") or 0) + take}
            remaining -= take
    return moves


def plan_put_moves(open_container: Any, item: Any, count: Any) -> list[dict]:
    """背包侧 → 容器侧：`container_move` 参数（先叠同类、再占空槽）。

    容器侧放不下就只放到放下为止 —— **剩下的仍在背包里**（planner 会如实报出来）。
    """
    item_id = normalize_item(item)
    want = _as_int(count) or 0
    start = player_inv_start(open_container)
    if not item_id or want <= 0 or start <= 0:
        return []
    rows = [row for row in container_entries(open_container) if row["slot"] < start]
    occupied = {row["slot"]: row for row in rows if int(row["count"]) > 0}
    moves: list[dict] = []
    remaining = int(want)
    for source in player_slots_with(open_container, item_id):
        if remaining <= 0:
            break
        have = int(source["count"]) - sum(
            move["count"] for move in moves if move["from_slot"] == source["slot"])
        while remaining > 0 and have > 0:
            target = _target_slot(occupied, item_id, 0, start)
            if target is None:
                return moves
            slot, room = target
            take = min(room, have, remaining)
            if take <= 0:
                break
            moves.append({"from_slot": int(source["slot"]), "to_slot": int(slot),
                          "count": int(take)})
            occupied[slot] = {"slot": slot, "item": item_id,
                              "count": int((occupied.get(slot) or {}).get("count") or 0) + take}
            have -= take
            remaining -= take
    return moves


# ── 两侧对账 ─────────────────────────────────────────────────


def reconcile_sides(before: Any, after: Any, moved: Any, *,
                    src_side: Optional[str] = SIDE_CONTAINER,
                    dst_side: Optional[str] = None) -> dict:
    """搬前/搬后 `items` 对比：源侧应**减少**、目标侧应**增加** `moved` 里的量。

    `src_side` / `dst_side` 给 `None` 表示"这一侧这次没有可比对的快照"，跳过它
    （绝不把没比过的当成对上了）。返回 `{"ok", "reason", "detail", "checked"}`；
    缺可用快照 → `reason="no_snapshot"` 且 `ok=False`。
    """
    if not (has_layout(before) and has_layout(after)):
        return {"ok": False, "reason": "no_snapshot", "checked": {},
                "detail": "没拿到能分清箱子/背包格的两侧快照，不能假装对上"}
    rows = {normalize_item(item): int(count) for item, count in (moved or {}).items()
            if _as_int(count) and int(count) > 0}
    if not rows:
        return {"ok": True, "reason": "empty", "detail": "", "checked": {}}
    checked: dict[str, dict] = {}
    problems: list[str] = []
    for item, want in rows.items():
        row: dict = {"item": item, "expect": want}
        if src_side:
            before_src = side_items(before, src_side).get(item, 0)
            after_src = side_items(after, src_side).get(item, 0)
            got_src = int(before_src) - int(after_src)
            row["source_delta"] = got_src
            if got_src != want:
                problems.append(f"{item}：源侧应少 {want}，实际少 {got_src}")
        if dst_side:
            before_dst = side_items(before, dst_side).get(item, 0)
            after_dst = side_items(after, dst_side).get(item, 0)
            got_dst = int(after_dst) - int(before_dst)
            row["dest_delta"] = got_dst
            if got_dst != want:
                problems.append(f"{item}：目标侧应多 {want}，实际多 {got_dst}")
        checked[item] = row
    if problems:
        return {"ok": False, "reason": "verify_failed", "detail": "；".join(problems),
                "checked": checked}
    return {"ok": True, "reason": "ok", "detail": "", "checked": checked}


# ── 回执（逐字契约）───────────────────────────────────────────


def moved_dict(moved: Any) -> dict:
    """`moved` 归一化：`{item, count, by_item}`（多件时 item 记 `*`，明细在 by_item）。"""
    rows = {str(item): int(count) for item, count in (moved or {}).items()
            if _as_int(count) and int(count) > 0}
    total = sum(rows.values())
    if len(rows) == 1:
        single = next(iter(rows))
    else:
        single = "*" if rows else ""
    return {"item": str(single), "count": int(total), "by_item": rows}


def failure_receipt(step: Any, *, src: Any = "", dst: Any = "", moved: Any = None,
                    reason: Any = "") -> dict:
    """失败回执：`{ok:false, step, from, to, moved:{item,count}, reason}`（如实，不编）。"""
    return {"ok": False, "step": str(step or ""), "from": str(src or ""),
            "to": str(dst or ""), "moved": moved_dict(moved), "reason": str(reason or "")}


def success_receipt(src: Any, dst: Any, moved: Any) -> dict:
    return {"ok": True, "step": STEP_DONE, "from": str(src or ""), "to": str(dst or ""),
            "moved": moved_dict(moved), "reason": ""}


# ── 跨轮任务（planner 持有；发帧在 planner 里）──────────────────


@dataclass
class HaulJob:
    """一趟搬运的进度（跨轮推进：走到 A → 开箱 → 取 → 走到 B → 开箱 → 放 → 对账）。"""

    src: str
    dst: str
    item: str = ""
    want: int = 0
    step: str = STEP_WALK_SOURCE
    src_side: str = SIDE_CONTAINER
    dst_side: str = SIDE_CONTAINER
    wanted: str = "src"                             # 现在需要开的是哪一侧：src | dst
    phase: str = "take"                             # 打开后干什么：probe | take | put | verify
    carried: dict = field(default_factory=dict)     # 已取出、还没放进去的
    moved: dict = field(default_factory=dict)       # 已经**放好并核对通过**的
    src_before: Optional[dict] = None               # 源侧搬前快照（对账基准）
    dst_before: Optional[dict] = None               # 目标侧搬前快照（探测/首次打开时记下）
    put_expected: dict = field(default_factory=dict)  # 这一批放进目标箱的量（对账基准）
    awaiting: str = ""                              # "" | take | put（等哪一侧的回执快照）
    awaiting_since: float = 0.0                     # 开始等快照的时刻（超时如实报）
    walked_since: float = 0.0                       # 开始走这一趟的时刻（走不到就如实报）
    walked: set = field(default_factory=set)        # 已经为哪一侧走过一趟（不重复下发）
    attempts: int = 0
    receipt: Optional[dict] = None                  # 终态回执（成功或失败）

    @property
    def done(self) -> bool:
        return self.receipt is not None

    def shortfall(self) -> dict:
        """已经取出来但还没放进目标箱的量（失败时要如实报，别假装搬完了）。"""
        out: dict[str, int] = {}
        for item, count in (self.carried or {}).items():
            remain = int(count) - int((self.moved or {}).get(item, 0))
            if remain > 0:
                out[str(item)] = remain
        return out

    def status(self) -> dict:
        return {"from": self.src, "to": self.dst, "item": self.item, "want": int(self.want),
                "step": self.step, "awaiting": self.awaiting,
                "carried": dict(self.carried or {}), "moved": dict(self.moved or {}),
                "shortfall": self.shortfall(), "attempts": int(self.attempts),
                "ok": None if self.receipt is None else bool(self.receipt.get("ok")),
                "reason": "" if self.receipt is None else str(self.receipt.get("reason") or "")}
