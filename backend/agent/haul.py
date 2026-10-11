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

# ── 嵌套容器（潜影盒 / 储物袋）────────────────────────────────
# 用户口径：「有些时候物品还比较复杂，是**嵌套**，特别是潜影盒的那种逻辑，或者储物袋
# 这种逻辑」。搬这类物品＝**连盒带内容一起搬**（一个整体，绝不把内容拆散 ✗）；
# 若目标明确只要"盒里的东西"，就先取出该物品再搬（不搬整盒）—— 两种由 `mode` 说清楚。
NEST_SUFFIXES: tuple[str, ...] = ("shulker_box",)
NEST_EXACT: tuple[str, ...] = ("bundle",)
# 嵌套**深度**与**总量**上限：超了**如实拒结并说明**，绝不静默截断。
NEST_MAX_DEPTH = 2
NEST_MAX_TOTAL = 256

# 搬运模式（工具参数的 `mode`，逐字契约）：
MODE_ITEM = "item"      # 普通物品：按件搬（默认）
MODE_WHOLE = "whole"    # 整盒搬：盒＋内容一起走（不拆散）
MODE_INSIDE = "inside"  # 先取出盒里的这件物品，再搬（不搬整盒）
HAUL_MODES: tuple[str, ...] = (MODE_ITEM, MODE_WHOLE, MODE_INSIDE)

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


# ── 多物品 / 嵌套（纯函数；认不出的一律跳过，绝不猜）────────────


def normalize_items(value: Any) -> dict[str, Optional[int]]:
    """`items` 参数归一化：接受 `[{item,count}]` / `{item: count}` / `"a,b"` / `"a:8"`。

    返回**保序**的 `{item: 需求或 None}`（None＝这件"能搬多少算多少"，不是 0）；
    认不出的一律丢掉（不猜成别的物品）。
    """
    out: dict[str, Optional[int]] = {}

    def _add(name: Any, count: Any = None) -> None:
        item = normalize_item(name)
        if not item or item in out:
            return
        amount = _as_int(count)
        out[item] = amount if amount and amount > 0 else None

    if isinstance(value, dict):
        for name, count in value.items():
            _add(name, count)
    elif isinstance(value, (list, tuple)):
        for row in value:
            if isinstance(row, dict):
                _add(row.get("item") or row.get("item_id") or row.get("name"),
                     row.get("count"))
            else:
                _add(row)
    elif isinstance(value, str):
        for chunk in value.replace("，", ",").split(","):
            part = chunk.strip()
            if not part:
                continue
            name, sep, count = part.partition(":")
            _add(name, count if sep else None)
    return out


def is_container_item(item: Any) -> bool:
    """这件物品是不是"能装东西的容器"（潜影盒 / 储物袋）——按 id 认，认不出＝不是。"""
    text = normalize_item(item)
    if not text:
        return False
    return text in NEST_EXACT or any(text.endswith(suffix) for suffix in NEST_SUFFIXES)


def normalize_mode(value: Any, default: str = MODE_ITEM) -> str:
    """搬运模式归一化：认不出的回默认（默认＝普通物品，绝不猜成"整盒搬"）。"""
    text = str(value or "").strip().lower()
    return text if text in HAUL_MODES else default


def container_rows(open_container: Any) -> list[dict]:
    """`open_container.items` 归一化并**保留内容物**（嵌套读用；坏项一律跳过）。"""
    if not isinstance(open_container, dict):
        return []
    raw = open_container.get("items")
    if not isinstance(raw, (list, tuple)):
        return []
    out: list[dict] = []
    for row in raw:
        if not isinstance(row, dict):
            continue
        slot = _as_int(row.get("slot"))
        item = normalize_item(row.get("item") or row.get("item_id") or row.get("name"))
        if slot is None or not item:
            continue
        out.append({"slot": slot, "item": item,
                    "count": int(_as_int(row.get("count")) or 0),
                    "name": str(row.get("name") or ""),
                    "policy": str(row.get("policy") or ""),
                    "contents": row.get("contents") if row.get("contents") is not None else None})
    return out


def _nested_rows(raw: Any) -> list[dict]:
    """快照里"内容物"字段的宽容解析：`{item:count}` 或 `[{item,count,contents,…}]`。"""
    out: list[dict] = []
    if isinstance(raw, dict):
        for name, value in raw.items():
            if isinstance(value, dict):
                row = dict(value)
                row.setdefault("item", name)
                out.append(row)
            else:
                out.append({"item": name, "count": value})
    elif isinstance(raw, (list, tuple)):
        out = [dict(row) for row in raw if isinstance(row, dict)]
    clean: list[dict] = []
    for row in out:
        item = normalize_item(row.get("item") or row.get("item_id") or row.get("name"))
        if not item:
            continue
        clean.append({
            "item": item,
            "count": int(_as_int(row.get("count")) or 0),
            "slot": _as_int(row.get("slot")),
            "name": str(row.get("name") or ""),
            "policy": str(row.get("policy") or ""),
            "contents": row.get("contents") if row.get("contents") is not None else None,
        })
    return clean


def nested_contents(entry: Any) -> dict[str, int]:
    """一个槽位里**连着内容物**的计数（递归展开；认不出就空 —— 不假装知道）。"""
    if not isinstance(entry, dict):
        return {}
    out: dict[str, int] = {}
    for row in _nested_rows(entry.get("contents")):
        out[row["item"]] = out.get(row["item"], 0) + int(row["count"])
        for item, count in nested_contents(row).items():
            out[item] = out.get(item, 0) + int(count)
    return out


def nested_stats(entries: Any) -> dict:
    """嵌套的**最大深度**（顶层＝1）与**内容总量**（含每一层）。"""
    depth = 1
    total = 0
    stack = [(row, 1) for row in (entries or []) if isinstance(row, dict)]
    while stack:
        row, level = stack.pop()
        depth = max(depth, int(level))
        for child in _nested_rows(row.get("contents")):
            total += int(child["count"])
            stack.append((child, level + 1))
    return {"depth": int(depth), "total": int(total)}


def nested_limits(entries: Any, *, max_depth: int = NEST_MAX_DEPTH,
                  max_total: int = NEST_MAX_TOTAL) -> dict:
    """嵌套是否超限：超了**如实拒结**（`ok=False` + 说明），绝不静默截断。"""
    stats = nested_stats(entries)
    if stats["depth"] > int(max_depth):
        return {**stats, "ok": False, "reason": "nested_too_deep",
                "detail": f"嵌套深度 {stats['depth']} 超过上限 {int(max_depth)}"}
    if stats["total"] > int(max_total):
        return {**stats, "ok": False, "reason": "nested_too_much",
                "detail": f"内容总量 {stats['total']} 超过上限 {int(max_total)}"}
    return {**stats, "ok": True, "reason": "", "detail": ""}


def recursive_no_touch(entries: Any, *, blocked: Any = ()) -> Optional[dict]:
    """**递归**应用 `no_touch`：盒内任一层命中 no_touch 容器/领地 → **连盒带物都不能动**。

    `blocked` = 已知 `no_touch` 的容器名；行自带 `policy=="no_touch"` 或 `name` 命中
    也算命中。返回命中的那一层 `{item,name,depth,reason}`；没有就 None。
    """
    names = {str(item).strip().casefold() for item in (blocked or []) if str(item).strip()}
    stack = [(row, 1) for row in (entries or []) if isinstance(row, dict)]
    while stack:
        row, level = stack.pop()
        name = str(row.get("name") or "")
        if str(row.get("policy") or "").strip().lower() == "no_touch" \
                or (name and name.casefold() in names):
            return {"item": normalize_item(row.get("item")), "name": name,
                    "depth": int(level), "reason": "nested_no_touch"}
        for child in _nested_rows(row.get("contents")):
            stack.append((child, level + 1))
    return None


def split_side_items(open_container: Any, side: str = SIDE_CONTAINER) -> dict:
    """某一侧按**外层 / 内层**分开计数（外层＝顶层槽位，内层＝嵌套内容物，递归）。"""
    outer: dict[str, int] = {}
    inner: dict[str, int] = {}
    start = player_inv_start(open_container)
    want_container = str(side) != SIDE_PLAYER
    for row in container_rows(open_container):
        if (int(row["slot"]) < start) != want_container:
            continue
        item = row["item"]
        if not item:
            continue
        outer[item] = outer.get(item, 0) + int(row["count"] or 0)
        for nested_item, count in nested_contents(row).items():
            inner[nested_item] = inner.get(nested_item, 0) + int(count)
    return {"outer": outer, "inner": inner}


# ── 算量 ────────────────────────────────────────────────────────


def plan_haul(src_oc: Any, *, src_side: str = SIDE_CONTAINER, item: Any = "",
              want: Any = 0, wants: Optional[dict] = None, capacity: Optional[dict] = None,
              max_count: int = HAUL_MAX_COUNT,
              source_floor: int = SOURCE_FLOOR) -> dict:
    """算这一趟**搬多少**（纯函数，不看策略、不发帧）。

    * `item` 给了 → 只搬这一件，取 `min(需求, 源箱可用-留底, 目标侧剩余, 单趟上限)`；
    * `wants`（多物品，`{item: 需求或 None}`）→ 一次搬多种，每件各自受需求/留底/空间约束，
      **总量**仍受单趟上限约束（超了就按顺序搬到上限为止，余下的如实留在台账里）；
    * 都没给 → 搬源箱里**全部可搬物**，每件都受留底/空间约束，总量不超过单趟上限；
    * `capacity` 是**目标侧**剩余空间 `{item: free}`（None = 未知，只按上限节流 ——
      真正放不放得下在 `put` 那一步用真实快照再卡一次）。

    返回 `{"items": {item: take}, "reason": "", "blocked": {...}}`；
    `reason` 非空表示一件都搬不了（`source_empty` / `item_absent` / `source_floor` /
    `target_full` / `trip_limit` / `nothing_to_move`）。
    """
    src = side_items(src_oc, src_side)
    floor = max(0, int(source_floor))
    cap_total = max(1, int(max_count))

    requested: dict[str, Optional[int]] = normalize_items(wants) if wants else {}
    if not requested and item:
        item_id = normalize_item(item)
        amount = _as_int(want)
        requested = {item_id: int(amount) if amount and amount > 0 else None}

    if not src:
        return {"items": {}, "reason": "source_empty", "blocked": {},
                "available": {}, "capacity": dict(capacity or {})}
    if len(requested) == 1 and next(iter(requested)) not in src:
        return {"items": {}, "reason": "item_absent", "blocked": {},
                "available": src, "capacity": dict(capacity or {})}

    kinds = list(requested) if requested else sorted(src)
    takes: dict[str, int] = {}
    blocked: dict[str, str] = {}
    budget = cap_total

    for kind in kinds:
        if kind not in src:
            blocked[kind] = "item_absent"
            continue
        available = int(src.get(kind, 0)) - floor
        if available <= 0:
            blocked[kind] = "source_floor"
            continue
        if budget <= 0:
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
        per_item = requested.get(kind) if requested else None
        if per_item:
            take = min(take, int(per_item))
        take = min(take, budget)
        if take <= 0:
            blocked[kind] = "target_full" if room == 0 else "nothing_to_move"
            continue
        takes[kind] = int(take)
        budget -= int(take)

    if not takes:
        reasons = set(blocked.values())
        if reasons and reasons <= {"item_absent"}:
            reason = "item_absent"
        elif reasons and reasons <= {"source_floor"}:
            reason = "source_floor"
        elif reasons and reasons <= {"target_full"}:
            reason = "target_full"
        elif reasons and reasons <= {"trip_limit"}:
            reason = "trip_limit"
        else:
            reason = "nothing_to_move"
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


# ── 整盒搬 / 盒内取（嵌套容器；不拆散，含内容物对账）────────────

def _layer_label(layer: str) -> str:
    return "内层" if str(layer) == "inner" else "外层"


def moved_layers(moved: Any) -> dict:
    """`moved` 归一化成**外层 / 内层**两本账（认不出的丢掉，不编）。"""
    data = moved if isinstance(moved, dict) else {}
    out: dict[str, dict] = {}
    for layer in ("outer", "inner"):
        rows = data.get(layer) if isinstance(data.get(layer), dict) else {}
        out[layer] = {normalize_item(item): int(count) for item, count in rows.items()
                      if _as_int(count) and int(count) > 0}
    return out


def nested_moved_dict(moved: Any) -> dict:
    """`moved` 归一化：`{outer, inner, total}`（外层几件、内层几件，各自看得见）。"""
    layers = moved_layers(moved)
    return {"outer": dict(layers["outer"]), "inner": dict(layers["inner"]),
            "total": sum(layers["outer"].values()) + sum(layers["inner"].values())}


def plan_box_take(open_container: Any, box_item: Any, count: Any, *,
                  src_side: str = SIDE_CONTAINER,
                  source_floor: int = SOURCE_FLOOR) -> dict:
    """**整盒搬**：把盒子当**一个整体**（连盒带内容一起走，绝不拆散 ✗），只按盒数搬。

    盒子在快照里是 1 格 1 个（潜影盒带 NBT 不叠），所以"整格搬"天然就是"整个盒搬"。
    返回 `{"moves", "boxes", "contents", "reason"}`：`contents` 是**将随盒走的内容物**
    （对账要含内容物，不只数外层个数）；取不到 → `reason`（`box_absent` / `no_space` /
    `source_floor` / `no_layout`）。
    """
    item_id = normalize_item(box_item)
    start = player_inv_start(open_container)
    slots = _as_int(open_container.get("slots")) if isinstance(open_container, dict) else None
    if not item_id or start <= 0 or slots is None or slots <= start:
        return {"moves": [], "boxes": 0, "contents": {}, "reason": "no_layout"}
    want_container = str(src_side) != SIDE_PLAYER
    rows = [row for row in container_rows(open_container)
            if (row["slot"] < start) == want_container
            and row["item"] == item_id and int(row["count"]) > 0]
    if not rows:
        return {"moves": [], "boxes": 0, "contents": {}, "reason": "box_absent"}
    floor = max(0, int(source_floor))
    cap = sum(int(row["count"]) for row in rows) - floor
    if cap <= 0:
        return {"moves": [], "boxes": 0, "contents": {}, "reason": "source_floor"}
    want = _as_int(count) or 0
    remaining = cap if want <= 0 else min(int(want), cap)
    dst_low, dst_high = (start, slots) if want_container else (0, start)
    occupied = {row["slot"]: row for row in container_rows(open_container)
                if dst_low <= row["slot"] < dst_high and int(row["count"]) > 0}
    moves: list[dict] = []
    boxes = 0
    contents: dict[str, int] = {}
    for row in sorted(rows, key=lambda item: int(item["count"]), reverse=True):
        if remaining <= 0:
            break
        target = _target_slot(occupied, item_id, dst_low, dst_high)
        if target is None:
            break
        slot, room = target
        move_count = min(int(row["count"]), room, remaining)
        if move_count <= 0:
            break
        moves.append({"from_slot": int(row["slot"]), "to_slot": int(slot),
                      "count": int(move_count)})
        occupied[slot] = {"slot": slot, "item": item_id,
                          "count": int((occupied.get(slot) or {}).get("count") or 0) + move_count}
        boxes += move_count
        for nested_item, amount in nested_contents(row).items():
            contents[nested_item] = contents.get(nested_item, 0) + int(amount) * move_count
        remaining -= move_count
    if not moves:
        return {"moves": [], "boxes": 0, "contents": {}, "reason": "no_space"}
    return {"moves": moves, "boxes": int(boxes), "contents": contents, "reason": ""}


def plan_inside_take(open_container: Any, item: Any, count: Any, *,
                     src_side: str = SIDE_CONTAINER, box: Any = "",
                     source_floor: int = SOURCE_FLOOR) -> dict:
    """**先取出盒里的这件物品**再搬（不搬整盒）：从盒的内容槽位取到**另一侧**。

    返回 `{"moves","item","box","available","reason"}`；取不出来 → `reason` 为
    `not_in_box`（盒里没有这件）或 `nested_not_visible`（盒里可能有，但快照没报可取的
    内容槽位 —— **绝不假装取出来了**）。
    """
    item_id = normalize_item(item)
    box_id = normalize_item(box)
    start = player_inv_start(open_container)
    slots = _as_int(open_container.get("slots")) if isinstance(open_container, dict) else None
    if not item_id or start <= 0 or slots is None or slots <= start:
        return {"moves": [], "item": item_id, "box": box_id, "available": 0,
                "reason": "no_layout"}
    want_container = str(src_side) != SIDE_PLAYER
    hits: list[tuple[str, Optional[int], int, str]] = []
    for row in container_rows(open_container):
        if (row["slot"] < start) != want_container:
            continue
        for holder, nested in _walk_nested(row):
            if nested["item"] != item_id:
                continue
            if box_id and box_id not in (normalize_item(holder.get("item")),
                                         normalize_item(holder.get("name"))):
                continue
            hits.append((normalize_item(holder.get("item")), nested.get("slot"),
                         int(nested["count"]), str(holder.get("name") or "")))
    if not hits:
        return {"moves": [], "item": item_id, "box": box_id, "available": 0,
                "reason": "not_in_box"}
    floor = max(0, int(source_floor))
    available = sum(amount for _, _, amount, _ in hits)
    total_cap = max(0, available - floor * len({slot for _, slot, _, _ in hits if slot is not None}))
    if total_cap <= 0:
        return {"moves": [], "item": item_id, "box": box_id, "available": available,
                "reason": "source_floor"}
    want = _as_int(count) or 0
    remaining = total_cap if want <= 0 else min(int(want), total_cap)
    dst_low, dst_high = (start, slots) if want_container else (0, start)
    occupied = {row["slot"]: row for row in container_rows(open_container)
                if dst_low <= row["slot"] < dst_high and int(row["count"]) > 0}
    moves: list[dict] = []
    moved = 0
    for holder_item, nested_slot, amount, _ in hits:
        if remaining <= 0:
            break
        if nested_slot is None:
            continue
        target = _target_slot(occupied, item_id, dst_low, dst_high)
        if target is None:
            break
        slot, room = target
        move_count = min(int(amount), room, remaining)
        if move_count <= 0:
            continue
        moves.append({"from_slot": int(nested_slot), "to_slot": int(slot),
                      "count": int(move_count), "box_item": str(holder_item)})
        occupied[slot] = {"slot": slot, "item": item_id,
                          "count": int((occupied.get(slot) or {}).get("count") or 0) + move_count}
        moved += move_count
        remaining -= move_count
    if not moves:
        return {"moves": [], "item": item_id, "box": box_id, "available": available,
                "reason": "nested_not_visible"}
    return {"moves": moves, "item": item_id, "box": box_id, "available": available,
            "reason": ""}


def _walk_nested(holder: Any, *, depth: int = 1) -> list[tuple[dict, dict]]:
    """递归遍历一个槽位的内容物：产出 `(宿主的行, 内容行)` 对（顶层行也可能自带内容）。"""
    out: list[tuple[dict, dict]] = []
    for child in _nested_rows((holder or {}).get("contents") if isinstance(holder, dict) else None):
        out.append((holder, child))
        out.extend(_walk_nested(child, depth=depth + 1))
    return out


def reconcile_nested(before: Any, after: Any, moved: Any, *,
                     src_side: Optional[str] = SIDE_CONTAINER,
                     dst_side: Optional[str] = None,
                     src_layers: tuple[str, ...] = ("outer", "inner"),
                     dst_layers: tuple[str, ...] = ("outer", "inner")) -> dict:
    """整盒搬/盒内取的两侧对账（**外层几件、内层几件**分开比，差在哪里照实说）。

    `moved` = `{"outer": {item: n}, "inner": {item: n}}`；源侧应**减少**、目标侧应
    **增加**选中的层里的量（`src_layers`/`dst_layers` 决定两侧各比哪几层 —— 整盒搬时
    目标侧内层也涨、源侧内层也跌；盒内取时源侧只跌内层、目标侧只涨外层）。
    缺可用快照 → `reason="no_snapshot"`（**绝不**当作对上了）。
    """
    if not (has_layout(before) and has_layout(after)):
        return {"ok": False, "reason": "no_snapshot", "checked": {},
                "detail": "没拿到能分清箱子/背包格的两侧快照，不能假装对上"}
    layers = moved_layers(moved)
    if not any(layers[layer] for layer in set(src_layers) | set(dst_layers)):
        return {"ok": True, "reason": "empty", "detail": "", "checked": {}}
    checked: dict[str, dict] = {}
    problems: list[str] = []
    for layer in src_layers:
        if not src_side:
            continue
        for item, want in layers[layer].items():
            row: dict = {"layer": layer, "item": item, "expect": want}
            before_src = split_side_items(before, src_side)[layer].get(item, 0)
            after_src = split_side_items(after, src_side)[layer].get(item, 0)
            got_src = int(before_src) - int(after_src)
            row["source_delta"] = got_src
            if got_src != want:
                problems.append(f"{_layer_label(layer)} {item}：源侧应少 {want}，实际少 {got_src}")
            checked[f"src:{layer}:{item}"] = row
    for layer in dst_layers:
        if not dst_side:
            continue
        for item, want in layers[layer].items():
            row: dict = {"layer": layer, "item": item, "expect": want}
            before_dst = split_side_items(before, dst_side)[layer].get(item, 0)
            after_dst = split_side_items(after, dst_side)[layer].get(item, 0)
            got_dst = int(after_dst) - int(before_dst)
            row["dest_delta"] = got_dst
            if got_dst != want:
                problems.append(f"{_layer_label(layer)} {item}：目标侧应多 {want}，实际多 {got_dst}")
            checked[f"dst:{layer}:{item}"] = row
    if problems:
        return {"ok": False, "reason": "verify_failed", "detail": "；".join(problems),
                "checked": checked}
    return {"ok": True, "reason": "ok", "detail": "", "checked": checked}


def reconcile_haul(mode: Any, leg: Any, before: Any, after: Any, moved: Any, *,
                   src_side: Optional[str] = SIDE_CONTAINER,
                   dst_side: Optional[str] = None) -> dict:
    """按搬运模式选对账口径（整盒搬 / 盒内取要分外层/内层；普通搬运照旧）。

    `leg` = `"take"`（源→我）或 `"put"`（我→目标）。
    """
    mode = normalize_mode(mode)
    layered = isinstance(moved, dict) and ("outer" in moved or "inner" in moved)
    if mode == MODE_WHOLE and layered:
        if str(leg) == "take":
            return reconcile_nested(before, after, moved, src_side=src_side, dst_side=dst_side,
                                    src_layers=("outer", "inner"), dst_layers=("outer",))
        return reconcile_nested(before, after, moved, src_side=src_side, dst_side=dst_side,
                                src_layers=("outer",), dst_layers=("outer", "inner"))
    if mode == MODE_INSIDE and str(leg) == "take":
        return reconcile_nested(before, after, moved, src_side=src_side, dst_side=dst_side,
                                src_layers=("inner",), dst_layers=("outer",))
    return reconcile_sides(before, after, moved, src_side=src_side, dst_side=dst_side)


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
                    reason: Any = "", nested: Any = None) -> dict:
    """失败回执：`{ok:false, step, from, to, moved:{item,count}, reason}`（如实，不编）。

    嵌套搬运额外带 `nested`（外层/内层各搬了几件、差在哪里）。
    """
    out = {"ok": False, "step": str(step or ""), "from": str(src or ""),
           "to": str(dst or ""), "moved": moved_dict(moved), "reason": str(reason or "")}
    if nested is not None:
        out["nested"] = nested_moved_dict(nested)
    return out


def success_receipt(src: Any, dst: Any, moved: Any, nested: Any = None) -> dict:
    out = {"ok": True, "step": STEP_DONE, "from": str(src or ""), "to": str(dst or ""),
           "moved": moved_dict(moved), "reason": ""}
    if nested is not None:
        out["nested"] = nested_moved_dict(nested)
    return out


# ── 跨轮任务（planner 持有；发帧在 planner 里）──────────────────


@dataclass
class HaulJob:
    """一趟搬运的进度（跨轮推进：走到 A → 开箱 → 取 → 走到 B → 开箱 → 放 → 对账）。"""

    src: str
    dst: str
    item: str = ""
    want: int = 0
    mode: str = MODE_ITEM                         # item | whole | inside
    box: str = ""                                 # inside：从哪个盒里取（空＝任一盒）
    items: dict = field(default_factory=dict)     # 多物品需求 {item: want}（want=0 → 能搬多少算多少）
    nested: dict = field(default_factory=dict)    # 整盒搬：这次随盒走的**外层/内层**账
    nested_active: bool = False                   # 这一趟是不是"整盒搬"（对账要含内容物）
    note_done: bool = False                       # 这一轮已经写过专门的人话了（别被通用话盖掉）
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
                "mode": self.mode, "box": self.box, "items": dict(self.items or {}),
                "nested": dict(self.nested or {}) if self.nested_active else {},
                "step": self.step, "awaiting": self.awaiting,
                "carried": dict(self.carried or {}), "moved": dict(self.moved or {}),
                "shortfall": self.shortfall(), "attempts": int(self.attempts),
                "ok": None if self.receipt is None else bool(self.receipt.get("ok")),
                "reason": "" if self.receipt is None else str(self.receipt.get("reason") or "")}
