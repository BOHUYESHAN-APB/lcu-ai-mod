"""where_is —— 「先问位置、挑一个、再过去」的**问位置**工具（**只答，绝不移动**）。

用户口径（硬）：
  - **能力=工具**，用不用由 LLM 决定；后台**没有**自主开关 / 自动触发（本模块绝不移动）；
  - **诚实优先**：问不到就如实说"不知道"，**绝不编坐标**；缺几何就如实说缺；
  - 不面向结果编程。

本模块是这块能力的**纯逻辑**部分：把手上几份**现成**的真相来源聚合成一份候选清单。
坐标**只**逐字取自这些行——任何一条来源缺 x/y/z 就整条不报（记进 `skipped`），
问不到就返回空清单 + 一句如实的 `note`；**绝不合成、不估计、不猜**。

聚合的真相来源（都用现成存储，不新建第二套）：
  1. 当前 state 的**在线玩家**（`context["players"]`，名字 + x/y/z，可模糊匹配）；
  2. world 层记忆的**记忆点**：Java 每帧上报的 `remembered_pois` +
     后端 `WorldPoiMemory`（记录点/箱子/工作站/床，跨局持久）；
  3. world 层记忆的**站点**：`WorldMemoryStore.stations()`（精准 xyz 的挂机点）；
  4. **容器**：`WorldPoiMemory` 的 storage + 当帧 `nearby_storage` + 领地容器登记；
  5. **区域 / 基地**：`TerritoryStore` 的 zones / bases。

移动必须由模型**另外**调 `move_to` / `go_to_block` / `follow` —— 这正是用户要的闭环：
不确定位置 → `where_is` 拿候选 → 挑一个 → 过去。
"""

from __future__ import annotations

import logging
import math
import re
from typing import Any, Iterable, Optional

from .territory import point_of

logger = logging.getLogger("where")

# ── kind 取值 ────────────────────────────────────────────────
KIND_AUTO = "auto"
KIND_PLAYER = "player"
KIND_POI = "poi"
KIND_STATION = "station"
KIND_CONTAINER = "container"
KIND_ZONE = "zone"
KIND_BASE = "base"

# `kind` 参数接受的取值：auto + 具体类别。base 是 zone 之外的**补充**（基地是基地，
# 不是区域），模型写 kind=auto 时两者都会给。
KINDS: tuple[str, ...] = (KIND_AUTO, KIND_PLAYER, KIND_POI, KIND_STATION,
                          KIND_CONTAINER, KIND_ZONE, KIND_BASE)

DEFAULT_LIMIT = 8
MAX_LIMIT = 24

# 来源标签：进候选的 `source` 字段，让模型知道"这个坐标哪来的"（只做标注，不做判断）。
SOURCE_PLAYERS = "state.players"
SOURCE_REMEMBERED = "state.remembered_pois"
SOURCE_POI_MEMORY = "memory.poi"
SOURCE_STATIONS = "memory.stations"
SOURCE_POI_STORAGE = "memory.poi.storage"
SOURCE_NEARBY_STORAGE = "state.nearby_storage"
SOURCE_TERRITORY = "world.territory"

# 名字按这个顺序取（谁的先有值算谁）：站点/容器/记忆点的字段名并不统一。
_NAME_KEYS = ("name", "label", "id", "block_id", "item_id", "category", "kind")

# 方块/物品 id 的形状：纯 ascii、下划线或命名空间（`oak_log` / `minecraft:chest`）。
# 中文名与"Steve"这种名字不算——本工具不替模型对一个人名喊"去扫方块"。
_IDENTIFIER_RE = re.compile(r"^[a-z0-9_.:-]+$")


def looks_like_block_or_item(target: Any) -> bool:
    """目标看起来像不是坐标类的方块/物品 id 吗（`oak_log` / `minecraft:chest`）。"""
    text = str(target or "").strip()
    if not text or not text.isascii() or not _IDENTIFIER_RE.match(text):
        return False
    return "_" in text or ":" in text


def _norm(value: Any) -> str:
    """匹配用的归一化键：去掉所有空白、转小写。"""
    return re.sub(r"\s+", "", str(value or "")).strip().lower()


def _num(value: Any) -> Optional[float]:
    """可空数值（bool 不算数；转不动就 None —— 不猜）。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _row_name(row: Any) -> str:
    """一行数据的名字（按 `_NAME_KEYS` 顺序取第一个有值的）。"""
    if not isinstance(row, dict):
        return ""
    for key in _NAME_KEYS:
        text = str(row.get(key) or "").strip()
        if text:
            return text
    return ""


def name_matches(target: Any, name: Any) -> bool:
    """目标名与候选名是否算命中：全等，或（长度 ≥2 时）互相包含的模糊匹配。

    单字目标（如"铁"）只认全等——否则一个字母就会命中一大片，那不算"知道"。
    """
    wanted, candidate = _norm(target), _norm(name)
    if not wanted or not candidate:
        return False
    if wanted == candidate:
        return True
    if len(wanted) >= 2 and (wanted in candidate or candidate in wanted):
        return True
    return False


def _fmt(value: float) -> str:
    """坐标好读一点：整数不拖小数点，非整数留一位。"""
    rounded = round(float(value), 1)
    return f"{rounded:.0f}" if rounded == int(rounded) else f"{rounded:.1f}"


def _row_point(row: Any) -> Optional[tuple[float, float, float]]:
    """一行数据的坐标：顶层 x/y/z 或 `center`（区域），再退到嵌套的 `anchor`（基地）
    / `position` / `home_bed`。**一个都取不到就 None**（不猜、不造）。"""
    point = point_of(row)
    if point is not None:
        return point
    if not isinstance(row, dict):
        return None
    for key in ("anchor", "position", "home_bed"):
        point = point_of(row.get(key))
        if point is not None:
            return point
    return None


def _candidate(kind: str, source: str, row: Any,
               player_pos: Optional[tuple[float, float, float]]) -> Optional[dict]:
    """把一行来源折算成一条候选；**缺几何返回 None**（绝不造坐标）。"""
    point = _row_point(row)
    if point is None:
        return None
    out: dict = {
        "kind": kind,
        "name": _row_name(row),
        "x": point[0], "y": point[1], "z": point[2],
        "source": source,
    }
    if player_pos is not None:
        out["distance"] = round(math.dist(player_pos, point), 1)
    return out


def _sort_key(candidate: dict) -> tuple:
    distance = candidate.get("distance")
    return (0 if distance is not None else 1, distance if distance is not None else 0.0,
            candidate.get("kind", ""), candidate.get("name", ""))


def _limit(value: Any) -> int:
    num = _num(value)
    if num is None:
        return DEFAULT_LIMIT
    return max(1, min(int(num), MAX_LIMIT))


def find(target: Any, *, kind: Any = KIND_AUTO, limit: Any = DEFAULT_LIMIT,
         player_pos: Optional[tuple[float, float, float]] = None,
         sources: Iterable[Any] = ()) -> dict:
    """把各来源里叫 `target` 的东西折成候选清单（纯函数，**不发帧、不移动**）。

    `sources`：`[{"kind": 类别, "source": 来源标签, "rows": [行…]}, …]`——
    行是**现成存储里的原始 dict**（不要求统一字段，缺名字/坐标的会被如实跳过）。

    返回 `{"target", "kind", "candidates", "truncated", "skipped", "found", "note"}`：
      - 每条候选 `{kind, name, x, y, z, distance?, source}`；
      - 名字命中但**没有坐标**的行只计数（`skipped`），不进候选（如实说缺，不编）；
      - `truncated=True` 表示候选超过 `limit`，只保留了最近的一批；
      - `note` 是给模型看的**如实**说明（`describe()` 的产物：有就给候选、没有就说不知道）。
    """
    wanted = _norm(kind) or KIND_AUTO
    if wanted not in KINDS:
        # 认不出的 kind：不猜它的意思，一条都不报（note 里如实说明）。
        result = {"target": str(target or ""), "kind": wanted, "candidates": [],
                  "truncated": False, "skipped": 0, "found": False, "unknown_kind": True}
        result["note"] = describe(result)
        return result
    cap = _limit(limit)
    found: list[dict] = []
    seen: set[tuple] = set()
    skipped = 0
    for source in sources or ():
        if not isinstance(source, dict):
            continue
        source_kind = _norm(source.get("kind"))
        if wanted != KIND_AUTO and source_kind != wanted:
            continue
        label = str(source.get("source") or source_kind)
        rows = source.get("rows")
        for row in rows if isinstance(rows, (list, tuple)) else ():
            if not isinstance(row, dict):
                continue
            if not name_matches(target, _row_name(row)):
                continue
            candidate = _candidate(source_kind, label, row, player_pos)
            if candidate is None:
                skipped += 1
                continue
            key = (source_kind, _norm(candidate["name"]),
                   round(candidate["x"], 3), round(candidate["y"], 3), round(candidate["z"], 3))
            if key in seen:
                continue
            seen.add(key)
            found.append(candidate)
    found.sort(key=_sort_key)
    result = {
        "target": str(target or ""),
        "kind": wanted,
        "candidates": found[:cap],
        "truncated": len(found) > cap,
        "skipped": skipped,
        "found": bool(found),
    }
    result["note"] = describe(result)
    return result


def _store_rows(store: Any, method: str) -> list[dict]:
    """只读地调存储上的一个列表方法；没存储 / 调用失败一律给空表（宁缺毋滥）。"""
    reader = getattr(store, method, None)
    if not callable(reader):
        return []
    try:
        rows = reader()
    except Exception as exc:  # noqa: BLE001 —— 读不到就少一个来源，绝不因此崩掉问位置
        logger.warning("[where] 读 %s.%s 失败: %s", type(store).__name__, method, exc)
        return []
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, (list, tuple)) else []


def _context_rows(context: Any, key: str) -> list[dict]:
    rows = (context or {}).get(key) if isinstance(context, dict) else None
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, (list, tuple)) else []


def gather_sources(context: Any, *, poi: Any = None, places: Any = None,
                   territory: Any = None) -> list[dict]:
    """把这一轮手上的真相来源收成 `find()` 要的形状（**只读**，不发帧）。

    `context` 是这一轮的 L2 上下文（`players` / `remembered_pois` / `known_containers` /
    `nearby_storage` / `territory` 快照）；`poi`/`places`/`territory` 是注入的**现成存储**
    （`WorldPoiMemory` / `WorldMemoryStore` / `TerritoryStore`）——没注入就少一个来源。
    """
    out: list[dict] = []

    players = _context_rows(context, "players")
    if players:
        out.append({"kind": KIND_PLAYER, "source": SOURCE_PLAYERS, "rows": players})

    remembered = _context_rows(context, "remembered_pois")
    if remembered:
        out.append({"kind": KIND_POI, "source": SOURCE_REMEMBERED, "rows": remembered})

    poi_rows = _store_rows(poi, "find")
    if poi_rows:
        out.append({"kind": KIND_POI, "source": SOURCE_POI_MEMORY, "rows": poi_rows})

    station_rows = _store_rows(places, "stations")
    if station_rows:
        out.append({"kind": KIND_STATION, "source": SOURCE_STATIONS, "rows": station_rows})

    known_containers = _context_rows(context, "known_containers")
    if known_containers:
        out.append({"kind": KIND_CONTAINER, "source": SOURCE_POI_STORAGE,
                    "rows": known_containers})

    nearby_storage = _context_rows(context, "nearby_storage")
    if nearby_storage:
        out.append({"kind": KIND_CONTAINER, "source": SOURCE_NEARBY_STORAGE,
                    "rows": nearby_storage})

    snapshot = (context or {}).get("territory") if isinstance(context, dict) else None
    if not isinstance(snapshot, dict) or not snapshot:
        # context 没带快照（例如测试里手搭的上下文）→ 直接只读问注入的存储。
        snapshot = {}
        reader = getattr(territory, "snapshot", None)
        if callable(reader):
            try:
                loaded = reader()
                snapshot = loaded if isinstance(loaded, dict) else {}
            except Exception as exc:  # noqa: BLE001
                logger.warning("[where] 读领地快照失败: %s", exc)
                snapshot = {}
    for key, source_kind in (("containers", KIND_CONTAINER), ("zones", KIND_ZONE),
                             ("bases", KIND_BASE)):
        rows = [row for row in (snapshot.get(key) or []) if isinstance(row, dict)]
        if rows:
            out.append({"kind": source_kind, "source": SOURCE_TERRITORY, "rows": rows})
    return out


def describe(result: Any) -> str:
    """把结果折成一句**如实**的补充说明（给回复用：有什么说什么，没有就说没有）。"""
    data = result if isinstance(result, dict) else {}
    target = str(data.get("target") or "").strip()
    label = f"「{target}」" if target else "（没说要找什么）"
    if data.get("unknown_kind"):
        return (f"（where_is {label}：kind="
                f"{data.get('kind')} 我不认得——只能填 "
                f"{'/'.join(KINDS)}，我没查）")
    rows = [row for row in (data.get("candidates") or []) if isinstance(row, dict)]
    if not rows:
        note = (f"（where_is {label}：我这边**不知道**它在哪——在线玩家 / 记忆点 / 站点 / "
                f"容器 / 区域 / 基地里都没有；位置要靠问来的，我不猜坐标）")
        if data.get("skipped"):
            note += f"（另有 {data['skipped']} 条名字对上了但没有坐标，报不了）"
        if looks_like_block_or_item(target):
            note += "（这看着像方块/物品：用 find_block 去扫附近——我不在这里自己扫）"
        else:
            note += "（要是找的是方块或物品，用 find_block 去扫附近）"
        return note
    parts = []
    for row in rows:
        where = f"@({_fmt(row.get('x'))},{_fmt(row.get('y'))},{_fmt(row.get('z'))})"
        distance = f" 距{row['distance']}" if row.get("distance") is not None else ""
        parts.append(f"{row.get('kind')} {row.get('name')}{where}{distance}[{row.get('source')}]")
    note = f"（where_is {label}：{len(rows)} 个候选 —— " + "；".join(parts) + "）"
    if data.get("truncated"):
        nearest = any(row.get("distance") is not None for row in rows)
        note += (f"（还有更多，只报了最近的 {len(rows)} 个）" if nearest
                 else f"（还有更多，只报了前 {len(rows)} 个）")
    if data.get("skipped"):
        note += f"（另有 {data['skipped']} 条没有坐标，报不了）"
    note += "（挑一个后用 move_to/go_to_block/follow 过去；我只答位置，不会自己动）"
    return note
