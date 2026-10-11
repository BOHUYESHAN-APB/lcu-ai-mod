"""find_entity —— 「按类型找附近实体」的**只读**查询（只答，绝不移动、绝不扫描）。

对照 mindcraft `!searchForEntity`（`commands/actions.js:142`）与 `!entities`
（`commands/queries.js:148`）；本模块把那两条合成**一个**查询工具。

用户口径（硬）：
  - **数据只从已有 state 快照取**（`context["entities"]`，Java 每帧上报的"周围 16 格内
    实体"，`StateCollector.java:352` `p.getBoundingBox().inflate(16)`）；
    本模块**不自己扫描**任何方块/实体；
  - **诚实优先**：快照里没有 ⇒ 如实说"当前快照里没有"，并说明它只覆盖周围约 16 格
    （更远的看不见，不装知道）；**绝不编坐标**——候选的 x/y/z 逐字来自快照行；
  - **只读**：本模块只看、只答；要过去是模型**另外**调 `move_to`/`go_to_block`/`follow`。

候选形状与 `where.py` **完全一致**（`{kind,name,x,y,z,distance,source}` + `truncated` +
诚实 `note`）——同一把尺子，不另立第二套。
"""

from __future__ import annotations

import logging
import math
from typing import Any, Iterable, Optional

from . import where
from .borrow import normalize_item

logger = logging.getLogger("entities")

# 来源标签：进候选的 `source` 字段（让模型知道"这条哪来的"）。
SOURCE_ENTITIES = "state.entities"

DEFAULT_LIMIT = 8
MAX_LIMIT = 24

# kind 归一表：canonical → 认得的写法（英文 id + 客户端中文显示名 + 类别词）。
# Java 上报的 `name` 是**客户端显示名**（`entity.getName().getString()`），中文客户端的牛
# 就叫"牛"——所以两边都认。表里没有的写法仍按"名字/类型逐字或包含匹配"走；
# 认不出**绝不猜**（不命中就是没命中，如实的空清单）。
KIND_ALIASES: dict[str, tuple[str, ...]] = {
    # 农场动物（与 `farm.py` 的 ANIMAL_FOODS 同一批）
    "cow": ("cow", "牛"),
    "mooshroom": ("mooshroom", "哞菇"),
    "sheep": ("sheep", "羊", "绵羊"),
    "goat": ("goat", "山羊"),
    "pig": ("pig", "猪"),
    "chicken": ("chicken", "鸡"),
    "rabbit": ("rabbit", "兔子"),
    "horse": ("horse", "马"),
    "donkey": ("donkey", "驴"),
    "llama": ("llama", "羊驼"),
    "wolf": ("wolf", "狼"),
    "cat": ("cat", "猫"),
    "fox": ("fox", "狐狸"),
    "bee": ("bee", "蜜蜂"),
    "turtle": ("turtle", "海龟"),
    "panda": ("panda", "熊猫"),
    "polar_bear": ("polar_bear", "北极熊"),
    "axolotl": ("axolotl", "美西螈"),
    "frog": ("frog", "青蛙"),
    "camel": ("camel", "骆驼"),
    "parrot": ("parrot", "鹦鹉"),
    "strider": ("strider", "炽足兽"),
    # 村民 / 傀儡（交易、守卫）
    "villager": ("villager", "村民"),
    "wandering_trader": ("wandering_trader", "流浪商人"),
    "iron_golem": ("iron_golem", "铁傀儡"),
    "snow_golem": ("snow_golem", "雪傀儡"),
    # 常见敌对（"附近有威胁吗"最常问）
    "zombie": ("zombie", "僵尸"),
    "skeleton": ("skeleton", "骷髅"),
    "creeper": ("creeper", "苦力怕", "爬行者"),
    "spider": ("spider", "蜘蛛"),
    "enderman": ("enderman", "末影人"),
    "witch": ("witch", "女巫"),
    "slime": ("slime", "史莱姆"),
    "drowned": ("drowned", "溺尸"),
    "blaze": ("blaze", "烈焰人"),
    "ghast": ("ghast", "恶魂"),
    "phantom": ("phantom", "幻翼"),
    "pillager": ("pillager", "掠夺者"),
    "vindicator": ("vindicator", "卫道士"),
    "hoglin": ("hoglin", "疣猪兽"),
    "piglin": ("piglin", "猪灵"),
    # 快照里的**类别**（`type` 字段的原样取值：player/animal/hostile/item/mob）
    "player": ("player", "玩家"),
    "animal": ("animal", "动物", "牲畜"),
    "hostile": ("hostile", "敌对", "怪物", "敌人"),
    "item": ("item", "掉落物", "物品"),
    "mob": ("mob", "生物", "怪物"),
}


def key_of(value: Any) -> str:
    """匹配用的归一化键：小写、剥命名空间、去空白与下划线。

    `Iron Golem` / `iron_golem` / `minecraft:iron_golem` 归一后是同一个键 —— 名字的
    三种常见写法（显示名 / id / 带命名空间的 id）都能对上。
    """
    return normalize_item(value).replace("_", "")


# alias → canonical（请求方与快照行都先过 `key_of`，再查这张表）。
_ALIAS_INDEX: dict[str, str] = {}
for _canonical, _aliases in KIND_ALIASES.items():
    _ALIAS_INDEX[key_of(_canonical)] = _canonical
    for _alias in _aliases:
        _ALIAS_INDEX[key_of(_alias)] = _canonical


def canonical_kind(value: Any) -> str:
    """一个写法归一到的 canonical kind（认不出返回空串）。"""
    return _ALIAS_INDEX.get(key_of(value), "")


def tokens_for(kind: Any) -> frozenset[str]:
    """请求的 kind 展开成可命中的键集合（`key_of` 后的写法 + 同组别的写法）。

    `"牛"` → `{牛, cow}`；`"cow"` → `{cow, 牛}`；表里没有的写法只保留它自己
    （仍然按逐字/包含匹配试，不猜别的意思）。
    """
    key = key_of(kind)
    if not key:
        return frozenset()
    out = {key}
    canonical = _ALIAS_INDEX.get(key)
    if canonical:
        for alias in KIND_ALIASES.get(canonical, ()):
            out.add(key_of(alias))
    return frozenset(token for token in out if token)


# `id` 是数字实体号，不作为名字来源；`uuid` 不是可读名字。
_NAME_KEYS = ("name", "display")
_TYPE_KEYS = ("type", "entity_type", "category")


def _strs(row: Any, keys: tuple[str, ...]) -> list[str]:
    if not isinstance(row, dict):
        return []
    out: list[str] = []
    for key in keys:
        text = str(row.get(key) or "").strip()
        if text:
            out.append(text)
    return out


def _row_names(row: Any) -> list[str]:
    return _strs(row, _NAME_KEYS)


def _row_types(row: Any) -> list[str]:
    return _strs(row, _TYPE_KEYS)


def _hit(requested: frozenset[str], value: Any) -> bool:
    """一个写法是否命中请求集合：归一后全等，或（长度 ≥2 时）互相包含。"""
    key = key_of(value)
    if not key:
        return False
    return any(where.name_matches(token, key) for token in requested)


def matches(kind: Any, row: Any) -> bool:
    """这一行实体算不算"模型在问的东西"（按名字**和**类别两处比对）。"""
    requested = tokens_for(kind)
    if not requested:
        return True                      # 没说要找什么＝把附近的都列出来（如实说明）
    return (any(_hit(requested, name) for name in _row_names(row))
            or any(_hit(requested, kind_name) for kind_name in _row_types(row)))


def kind_of(row: Any) -> str:
    """这一行实体是哪一类：认得就归一（`cow`），认不出就用快照的 `type` 原样，再没有就空串。"""
    for name in _row_names(row):
        canonical = canonical_kind(name)
        if canonical:
            return canonical
    for type_name in _row_types(row):
        canonical = canonical_kind(type_name)
        if canonical:
            return canonical
    types = _row_types(row)
    return where._norm(types[0]) if types else ""


def name_of(row: Any) -> str:
    """这一行显示出来的名字（显示名优先，没有就用类别；**不编**）。"""
    for name in _row_names(row):
        if name.strip():
            return name.strip()
    types = _row_types(row)
    return types[0] if types else ""


def _distance(row: Any, point: tuple[float, float, float],
              player_pos: Optional[tuple[float, float, float]]) -> Optional[float]:
    """距离：优先用快照自己报的（Java 的真实值），没有才按玩家坐标算（都缺 → None）。"""
    reported = where._num(row.get("distance"))
    if reported is not None:
        return round(float(reported), 1)
    if player_pos is not None:
        return round(math.dist(player_pos, point), 1)
    return None


def candidate(row: Any, player_pos: Optional[tuple[float, float, float]] = None) -> Optional[dict]:
    """一行实体折成候选；**缺几何返回 None**（绝不造坐标）。形状与 `where` 一致。"""
    point = where._row_point(row)
    if point is None:
        return None
    out: dict = {
        "kind": kind_of(row),
        "name": name_of(row),
        "x": point[0], "y": point[1], "z": point[2],
        "source": SOURCE_ENTITIES,
    }
    distance = _distance(row, point, player_pos)
    if distance is not None:
        out["distance"] = distance
    entity_id = row.get("id") if isinstance(row, dict) else None
    if isinstance(entity_id, int) and not isinstance(entity_id, bool):
        out["id"] = entity_id           # 给 use_on / attack 用（原样来自快照）
    return out


def _sort_key(row: dict) -> tuple:
    distance = row.get("distance")
    return (0 if distance is not None else 1, distance if distance is not None else 0.0,
            str(row.get("kind") or ""), str(row.get("name") or ""))


def _limit(value: Any) -> int:
    num = where._num(value)
    if num is None:
        return DEFAULT_LIMIT
    return max(1, min(int(num), MAX_LIMIT))


def gather(context: Any) -> list[dict]:
    """这一轮的实体快照（state 里那一份，**只读**，不扫描）。"""
    rows = (context or {}).get("entities") if isinstance(context, dict) else None
    if not isinstance(rows, (list, tuple)):
        return []
    return [row for row in rows if isinstance(row, dict)]


def find(kind: Any = "", *, limit: Any = DEFAULT_LIMIT,
         player_pos: Optional[tuple[float, float, float]] = None,
         entities: Iterable[Any] = ()) -> dict:
    """把快照里匹配 `kind` 的实体折成候选清单（纯函数，**不发帧、不移动、不扫描**）。

    返回 `{"kind", "candidates", "truncated", "skipped", "found", "note"}`：
      - 每条候选 `{kind, name, x, y, z, distance?, source, id?}`；
      - 名字/类别对上但**快照没给坐标**（在线玩家只有距离）⇒ 只计数（`skipped`），
        不进候选（如实说缺，不编坐标）；
      - `truncated=True` 表示候选超过 `limit`，只保留了最近的一批；
      - `note` 是给模型看的**如实**说明（`describe()` 的产物）。
    """
    cap = _limit(limit)
    found: list[dict] = []
    seen: set[tuple] = set()
    skipped = 0
    rows = [row for row in (entities if isinstance(entities, (list, tuple)) else ())
            if isinstance(row, dict)]
    for row in rows:
        if not matches(kind, row):
            continue
        row_candidate = candidate(row, player_pos)
        if row_candidate is None:
            skipped += 1
            continue
        key = (where._norm(row_candidate["name"]),
               round(row_candidate["x"], 3), round(row_candidate["y"], 3),
               round(row_candidate["z"], 3))
        if key in seen:
            continue
        seen.add(key)
        found.append(row_candidate)
    found.sort(key=_sort_key)
    result = {
        "kind": str(kind or "").strip(),
        "candidates": found[:cap],
        "truncated": len(found) > cap,
        "skipped": skipped,
        "found": bool(found),
    }
    result["note"] = describe(result)
    return result


def nearest(kind: Any, entities: Iterable[Any], *,
            player_pos: Optional[tuple[float, float, float]] = None) -> Optional[dict]:
    """最近的匹配实体（没有就 None）——`use_on` 这类"对最近的目标动手"用它。

    返回与 `find()` 同形状的一条候选（`id` 有就给，供 Java 侧按实体号右键）。
    """
    found = find(kind, limit=MAX_LIMIT, player_pos=player_pos, entities=entities)
    rows = found.get("candidates") or []
    return rows[0] if rows else None


def _fmt(value: Any) -> str:
    return where._fmt(where._num(value) or 0.0)


def describe(result: Any) -> str:
    """把结果折成一句**如实**的补充说明（有什么说什么，没有就说没有）。"""
    data = result if isinstance(result, dict) else {}
    wanted = str(data.get("kind") or "").strip()
    label = f"「{wanted}」" if wanted else "附近"
    rows = [row for row in (data.get("candidates") or []) if isinstance(row, dict)]
    if not rows:
        if wanted:
            note = (f"（find_entity {label}：**当前快照里没有** —— 这份快照只覆盖我周围约 "
                    f"16 格内的实体，更远的我看不到，也不装知道）")
        else:
            note = "（find_entity：当前快照里（周围约 16 格内）一个实体都没有）"
        if data.get("skipped"):
            note += (f"（另有 {data['skipped']} 条名字对上了但快照只给了距离、没给坐标，"
                     f"报不了 —— 找在线玩家用 where_is kind=player）")
        if wanted and where.looks_like_block_or_item(wanted):
            note += "（这看着像方块/物品：用 find_block 扫附近）"
        else:
            note += "（找方块用 find_block；找玩家/箱子/基地用 where_is）"
        return note
    parts = []
    for row in rows:
        where_at = f"@({_fmt(row.get('x'))},{_fmt(row.get('y'))},{_fmt(row.get('z'))})"
        distance = f" 距{row['distance']}" if row.get("distance") is not None else ""
        parts.append(f"{row.get('kind') or '?'} {row.get('name')}{where_at}{distance}")
    note = (f"（find_entity {label}：{len(rows)} 个候选 —— " + "；".join(parts) +
            f"〔{SOURCE_ENTITIES}〕）")
    if data.get("truncated"):
        nearest_rows = any(row.get("distance") is not None for row in rows)
        note += (f"（还有更多，只报了最近的 {len(rows)} 个）" if nearest_rows
                 else f"（还有更多，只报了前 {len(rows)} 个）")
    if data.get("skipped"):
        note += (f"（另有 {data['skipped']} 条只有距离没有坐标，报不了 —— "
                 f"找在线玩家用 where_is kind=player）")
    note += "（挑一个后用 move_to/go_to_block/follow 过去；我只查位置，不会自己走）"
    return note
