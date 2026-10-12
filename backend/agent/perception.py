"""perception —— 读"分级摘要"感知快照的**唯一入口**（纯函数、零依赖）。

背景（用户口径，模组已落地）：`state_update` 里的 `entities` / `nearby_blocks` 从"逐条带坐标的
清单"改成了**分级摘要** —— 村庄/繁殖基地/农场旁边几十只生物一起动，逐条灌坐标只会把 prompt
刷屏。于是只有**要处理的（威胁）**与**人（玩家）**才带坐标，被动生物的逐条坐标**默认不上报**；
要某一只 / 某一种的具体坐标时走**按需**路径再查。

冻结的摘要形状（Java `PerceptionView`，逐字对齐）：

    entities      = {count, by_kind:{dangerous,hostile,neutral,passive,player,item,unknown},
                     nearest?{distance,name,type,health?},                 # 不带坐标
                     threats:[{name,type,tier,x,y,z,distance}],            # **带坐标**
                     players:[{name,x,y,z,distance,health}],               # **带坐标**
                     items:{count, nearest?{item_id,item_count,distance,
                                            owner,age,despawn_in}}}        # 不带坐标
    nearby_blocks = {count, by_block:{block_id:count}, nearest?{block_id,distance}}  # 不带坐标

本模块只做一件事：把**两种形状**（新摘要 / 旧列表）读成**同一份候选行**，并**如实标注**
"这份视图是不是部分视图"（`partial`）。消费点据此决定"能不能动手" —— **绝不允许**把
"摘要里看不到"当成"附近没有"。

三条铁律：
  1. **不编坐标**：行里的 `x/y/z` 只来自模组报的字段。摘要里没有坐标的档位（被动生物、
     掉落物、方块的逐条位置）就**不带坐标**，绝不按 kind 编一个出来；
  2. **`partial` 是形状的属性**，不是猜的：摘要 ⇒ `True`（默认屏蔽了没有坐标的档位）；
     旧列表 ⇒ `False`（模组把逐条明细都给了）；认不出的形状 ⇒ `True`（保守，宁当部分视图）；
  3. **零依赖**：只读上下文里的字典/列表，**不 import 其它 agent 模块**（免得环，也免得
     测试里要拖一堆依赖）。

行形状（统一口径）：两种形状都以 `name` / `type`（+`tier`）/ `x` / `y` / `z` / `distance` 读；
摘要行额外带 `kind`（模组自己的归档）与 `PARTIAL_KEY` 标记。旧列表行**原样**保留
（模组本来就带坐标；原样保留是为了不改变既有 `entities.gather` 的快照语义）。

"按需"路径（不在这里实现，只报**工具名**）：实体明细 `scan_entities`，方块 `find_block`。
"""

from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger("perception")

# 行标记：这一行来自**分级摘要**（≠ 完整清单）。`entities.find` 之类的消费点靠它如实标注。
PARTIAL_KEY = "partial_view"

# 快照形状（消费点可据此决定话怎么说，不必自己嗅探）。
SHAPE_SUMMARY = "summary"     # 分级摘要（新）
SHAPE_LIST = "list"           # 逐条清单（旧）
SHAPE_UNKNOWN = "unknown"     # 有值但认不出 ⇒ 保守当部分视图
SHAPE_MISSING = "missing"     # 压根没这一项 ⇒ 不谈"部分"

# 按需路径（后端已注册/冻结的命令名，只用来写字，不在本模块发帧）。
SCAN_ENTITY_TOOL = "scan_entities"
SCAN_BLOCK_TOOL = "find_block"

# `by_kind` 的七档（与 Java `PerceptionView.Pure.KINDS` 同序、同名）。
KIND_ORDER: tuple[str, ...] = ("dangerous", "hostile", "neutral", "passive",
                               "player", "item", "unknown")
KIND_LABELS: dict[str, str] = {
    "dangerous": "危险", "hostile": "敌对", "neutral": "中立", "passive": "被动",
    "player": "玩家", "item": "掉落物", "unknown": "认不出",
}

# 摘要里**带坐标**的档位（模组口径：威胁要处理、人要找；其余默认屏蔽）。
COORD_KINDS: tuple[str, ...] = ("dangerous", "hostile", "player")

_ENTITY_KEYS = ("count", "by_kind", "nearest", "threats", "players", "items")
_BLOCK_KEYS = ("count", "by_block", "nearest")


# ── 小工具（只认数字，布尔不算） ────────────────────────────────────

def _num(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _text(value: Any) -> str:
    return str(value if value is not None else "").strip()


def point_of(row: Any) -> Optional[tuple[float, float, float]]:
    """一行（实体/方块）的坐标：三个都是数才算有；缺一个就 `None` —— **不补 0、不编**。"""
    if not isinstance(row, dict):
        return None
    values = [_num(row.get(key)) for key in ("x", "y", "z")]
    if any(value is None for value in values):
        return None
    return (values[0], values[1], values[2])


def is_partial(row: Any) -> bool:
    """这一行是不是来自分级摘要（≠ 完整清单）。"""
    return bool(isinstance(row, dict) and row.get(PARTIAL_KEY))


def rows_are_partial(rows: Any) -> bool:
    """一串行里只要有一条来自摘要 ⇒ 这串就是部分视图。"""
    return any(is_partial(row) for row in rows or [])


def _marked(row: dict) -> dict:
    row[PARTIAL_KEY] = True
    return row


def _by_kind(value: Any) -> dict[str, int]:
    """`by_kind` 归一成 `{档名: 条数}`（只认七档，非数字/负数一律当 0）。"""
    out = {kind: 0 for kind in KIND_ORDER}
    if not isinstance(value, dict):
        return out
    for kind, count in value.items():
        key = _text(kind).lower()
        if key in out:
            number = _num(count)
            out[key] = int(number) if number is not None and number > 0 else 0
    return out


def _count_of(value: Any, by_kind: dict[str, int], fallback: int) -> int:
    """摘要的 `count`：模组报的数优先；缺字段才退回按档求和（两者本来就该相等）。"""
    number = _num(value)
    if number is not None and number >= 0:
        return int(number)
    total = sum(by_kind.values())
    return total if total else fallback


# ── 实体：两种形状 → 统一行 ────────────────────────────────────────

def _summary_entity_row(entry: Any, kind: str, source: str) -> Optional[dict]:
    """摘要里某个档位的一条 → 统一行（原字段照抄；只补"模组自己归档的那一档"）。

    坐标**只可能**来自 `entry` 自己带的 `x/y/z`；这里绝不按档位补坐标。
    """
    if not isinstance(entry, dict):
        return None
    row = dict(entry)
    if not _text(row.get("type")):
        # 模组把这一条归在哪个档（威胁带 `type`、玩家/掉落物只有归档）—— 复述归档，不是猜档位。
        row["type"] = kind
    if not _text(row.get("name")):
        fallback = _text(row.get("item_id")) or _text(row.get("block_id"))
        if fallback:
            row["name"] = fallback           # 掉落物：物品 id 就是它的名字（模组报的事实）
    row["kind"] = kind
    row["source"] = source
    return _marked(row)


def _summary_entity_rows(value: dict) -> list[dict]:
    """摘要 → 行：只有 `threats` / `players` / `items.nearest` / `nearest` 这几处有东西。

    `items.nearest` 与 `nearest` **没有坐标** —— 照样给出来（它们是事实：距离/血量/谁丢的），
    但下游按"缺几何"如实处理（`entities.find` 会算进 `skipped`），**不编位置**。
    """
    rows: list[dict] = []
    seen: set[tuple[str, str, float]] = set()

    def add(entry: Any, kind: str, source: str) -> None:
        row = _summary_entity_row(entry, kind, source)
        if row is None:
            return
        key = (_text(row.get("name")), _text(row.get("type")), _num(row.get("distance")) or -1.0)
        if key in seen:
            return                            # 同一条被两个档位都报了（如最近的就是那个玩家）
        seen.add(key)
        rows.append(row)

    for entry in value.get("threats") or []:
        kind = _text((entry or {}).get("type") if isinstance(entry, dict) else "").lower()
        if kind not in ("dangerous", "hostile"):
            tier = _text((entry or {}).get("tier") if isinstance(entry, dict) else "").lower()
            kind = tier if tier in ("dangerous", "hostile") else "hostile"
        add(entry, kind, "state.entities.threats")

    for entry in value.get("players") or []:
        add(entry, "player", "state.entities.players")

    items = value.get("items") if isinstance(value.get("items"), dict) else {}
    add(items.get("nearest"), "item", "state.entities.items.nearest")

    nearest = value.get("nearest")
    if isinstance(nearest, dict):
        kind = _text(nearest.get("type")).lower()
        add(nearest, kind if kind in KIND_ORDER else "unknown", "state.entities.nearest")

    return rows


def _empty_view(shape: str, *, partial: bool, blocks: bool = False) -> dict:
    """空视图（形状一样，消费点不必判键在不在）。"""
    view = {"rows": [], "partial": bool(partial), "count": 0, "shape": shape}
    if blocks:
        view["by_block"] = {}
    else:
        view["by_kind"] = _by_kind(None)
        view["items"] = {"count": 0, "nearest": None}
    return view


def _legacy_rows(value: Any) -> list[dict]:
    """旧列表形状 → 行（**原样**复制：模组本来就带坐标，不改它的字节语义）。"""
    return [dict(row) for row in value if isinstance(row, dict)]


def entities_rows(context: Any) -> dict:
    """这一轮的实体快照 → `{"rows", "partial", "count", "shape", "by_kind", "items"}`。

    * `rows`：统一口径的候选行（摘要里只可能是"带坐标的威胁/玩家" + "只有距离的最近一条/掉落物"）；
    * `partial`：**这份视图是不是部分视图** —— 摘要 `True`（没有坐标的档位默认没上报）、
      旧列表 `False`、认不出的形状 `True`（保守）、压根没这一项 `False`；
    * `count`：模组报的条数（缺字段才退回按 `by_kind` 求和）—— **不是** `len(rows)`：
      摘要把没有坐标的档位挡在门外了，行数本来就会少；
    * `by_kind` / `items`：模组自己的归档与掉落物聚合（只有摘要形状有；旧列表给空）。
    """
    value = (context or {}).get("entities") if isinstance(context, dict) else None

    if isinstance(value, (list, tuple)):
        rows = _legacy_rows(value)
        return {"rows": rows, "partial": False, "count": len(rows),
                "shape": SHAPE_LIST, "by_kind": _by_kind(None),
                "items": {"count": 0, "nearest": None}}

    if isinstance(value, dict):
        if any(key in value for key in _ENTITY_KEYS):
            by_kind = _by_kind(value.get("by_kind"))
            rows = _summary_entity_rows(value)
            items = value.get("items") if isinstance(value.get("items"), dict) else {}
            item_count = _num(items.get("count"))
            return {"rows": rows, "partial": True,
                    "count": _count_of(value.get("count"), by_kind, len(rows)),
                    "shape": SHAPE_SUMMARY, "by_kind": by_kind,
                    "items": {"count": int(item_count) if item_count is not None
                              and item_count >= 0 else 0,
                              "nearest": items.get("nearest")
                              if isinstance(items.get("nearest"), dict) else None}}
        logger.warning("[Perception] 认不出的 entities 形状：%s", sorted(value)[:8])
        return _empty_view(SHAPE_UNKNOWN, partial=True)

    if value is not None:
        logger.warning("[Perception] entities 既不是清单也不是摘要：%r", type(value).__name__)
        return _empty_view(SHAPE_UNKNOWN, partial=True)

    return _empty_view(SHAPE_MISSING, partial=False)


# ── 方块：两种形状 → 统一行 ────────────────────────────────────────

def blocks_rows(context: Any) -> dict:
    """方块的快照 → `{"rows", "partial", "count", "shape", "by_block"}`。

    摘要形状下**只有 `nearest` 一条**（而且模组刻意没给它坐标，只有 `block_id` + 距离）；
    `by_block` 只有"哪种方块几个"，**没有位置** —— 消费点不能拿它当"附近这些方块在哪"。
    旧列表形状 ⇒ 逐条原样（`partial=False`）。要找某一种方块的位置，走按需扫描（`find_block`）。
    """
    value = (context or {}).get("nearby_blocks") if isinstance(context, dict) else None

    if isinstance(value, (list, tuple)):
        rows = _legacy_rows(value)
        return {"rows": rows, "partial": False, "count": len(rows),
                "shape": SHAPE_LIST, "by_block": {}}

    if isinstance(value, dict):
        if any(key in value for key in _BLOCK_KEYS):
            by_block: dict[str, int] = {}
            raw = value.get("by_block") if isinstance(value.get("by_block"), dict) else {}
            for block_id, count in raw.items():
                number = _num(count)
                if _text(block_id):
                    by_block[_text(block_id)] = int(number) if number is not None else 0
            rows: list[dict] = []
            nearest = value.get("nearest")
            if isinstance(nearest, dict):
                row = dict(nearest)
                if not _text(row.get("name")):
                    row["name"] = _text(row.get("block_id"))
                row.setdefault("kind", "block")
                row["source"] = "state.nearby_blocks.nearest"
                rows.append(_marked(row))
            count = value.get("count")
            number = _num(count)
            total = int(number) if number is not None and number >= 0 else sum(by_block.values())
            return {"rows": rows, "partial": True, "count": total,
                    "shape": SHAPE_SUMMARY, "by_block": by_block}
        logger.warning("[Perception] 认不出的 nearby_blocks 形状：%s", sorted(value)[:8])
        return _empty_view(SHAPE_UNKNOWN, partial=True, blocks=True)

    if value is not None:
        logger.warning("[Perception] nearby_blocks 既不是清单也不是摘要：%r",
                       type(value).__name__)
        return _empty_view(SHAPE_UNKNOWN, partial=True, blocks=True)

    return _empty_view(SHAPE_MISSING, partial=False, blocks=True)


# ── 给人/模型看的一行 ──────────────────────────────────────────────

def summary_note(context: Any) -> str:
    """一行**如实**的感知摘要（计数 + 哪些带坐标 + 细节怎么拿）。**不报没有的坐标**。"""
    view = entities_rows(context)
    shape = view.get("shape")
    count = view.get("count") or 0
    if shape == SHAPE_MISSING:
        return "附近实体：这一帧没这一项（不知道附近有什么）"
    if shape == SHAPE_UNKNOWN:
        return ("附近实体：这一帧的形状我认不出（**当部分视图**处理：不知道的比知道的多）"
                f"；要细节按需扫描（{SCAN_ENTITY_TOOL}）")
    if shape == SHAPE_LIST:
        return f"附近 {count} 个实体（逐条清单，都带坐标）"
    parts = [f"{KIND_LABELS[kind]} {view['by_kind'].get(kind, 0)}"
             for kind in KIND_ORDER if (view["by_kind"].get(kind) or 0) > 0]
    detail = "：" + "、".join(parts) if parts else ""
    return (f"附近 {count} 个实体{detail}（只有威胁与玩家带坐标）"
            f"；细节按需扫描（{SCAN_ENTITY_TOOL}）")


def block_note(context: Any) -> str:
    """方块那一路的一行**如实**说明（摘要只有计数与最近一种，且都不带坐标）。"""
    view = blocks_rows(context)
    shape = view.get("shape")
    if shape == SHAPE_MISSING:
        return "附近方块：这一帧没这一项（不知道附近有什么）"
    if shape == SHAPE_UNKNOWN:
        return f"附近方块：形状我认不出（**当部分视图**处理）；要看就按需扫描（{SCAN_BLOCK_TOOL}）"
    if shape == SHAPE_LIST:
        return f"附近 {view['count']} 个方块（逐条清单，都带坐标）"
    kinds = view.get("by_block") or {}
    nearest = view.get("rows") or []
    nearest_text = ""
    if nearest:
        row = nearest[0]
        distance = row.get("distance")
        nearest_text = (f"，最近一种 {row.get('block_id') or row.get('name')}"
                        f"@{distance}m" if distance is not None
                        else f"，最近一种 {row.get('block_id') or row.get('name')}")
    return (f"附近 {view['count']} 个方块（{len(kinds)} 种计数的**摘要**，逐条位置没上报"
            f"{nearest_text}）；要看具体位置就按需扫描（{SCAN_BLOCK_TOOL}）")


# ── 消费点共用的"部分视图"说法 ─────────────────────────────────────

def partial_note(what: str = "实体") -> str:
    """一句如实的「这是部分视图」说明（**必须有「按需」**：这就是消费点该说的话）。"""
    tool = SCAN_BLOCK_TOOL if what.startswith("方块") else SCAN_ENTITY_TOOL
    return (f"（这是**分级摘要**：默认只上报要处理的与人所在的位置，{what}的逐条坐标没上报 —— "
            f"所以「摘要里没有」**不等于**「附近没有」；要细节就按需扫描（{tool}））")


def missing_note(what: str = "实体", *, detail: str = "") -> str:
    """「摘要里没找到」该怎么说清（不许说成「附近没有」）。"""
    note = partial_note(what)
    if detail:
        return f"{detail}{note}"
    return note


def block_missing_note(what: str = "方块") -> str:
    """方块那一路「找了却没找到」的如实说法（摘要里没有逐条位置，不等于附近没有）。"""
    return (f"（{what}：这一帧是**分级摘要**，方块的逐条位置默认没上报 —— "
            f"「摘要里没有」**不等于**「附近没有」；要看就按需扫描（{SCAN_BLOCK_TOOL}））")


def bed_caution_note(*, hostiles: Any = ()) -> str:
    """造床安全判定的**保守**说法：摘要看不全时，「没看到怪」**不算**安全。"""
    who = "、".join(str(item) for item in hostiles or []) or "敌人"
    if hostiles:
        return f"（周围有敌对生物（{who}），先处理掉；处理不了就先不造床了）"
    return ("（这一帧是**分级摘要**：没有坐标的档位（被动/中立等）根本没上报，"
            "所以**不能确认周围没有威胁** —— 按「可能不安全」处理，先不造床；"
            f"要确认就按需扫描（{SCAN_ENTITY_TOOL}））")


def conservative_bed_decision(decision: Any, view: Any) -> dict:
    """把造床判定按「部分视图」**保守**修正（返回新 dict）。

    `territory.bed_attempt` 的判据是"看到敌对 ⇒ 不安全" —— 这在**部分视图**下会反过来骗人：
    摘要里没有坐标的档位根本没上报，「没看到」绝不等于「没有」。所以 `partial=True` 且原判定
    是 `craft`（材料够、没看到威胁）时，**不改写成"安全"**：结论抬成 `unsafe` 并带上
    `partial` 标记 + 空 `hostiles`（`bed_caution_note` 会把原因说清）。
    其余结论（`unsafe` / `missing`）原样透传，只补 `partial` 标记。
    """
    out = dict(decision) if isinstance(decision, dict) else {}
    if not (isinstance(view, dict) and view.get("partial")):
        return out
    action = _text(out.get("action"))
    if action == "craft":
        out.update({"action": "unsafe", "hostiles": [], "partial": True})
    else:
        out["partial"] = True
    return out
