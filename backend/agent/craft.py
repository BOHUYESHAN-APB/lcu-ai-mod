"""制作 / 取物的库存优先策略（纯逻辑，不碰 wire、不碰 LLM）。

用户口径：
  - 造东西只有**被允许**才能造（授权表见 `permissions.py` 的 `craft` / `containers`，
    决策链只查表）；
  - 自己想造时**优先只用背包里的材料**（含快捷栏），不够就别硬造；
  - 只有**被授权去辅助制作**才允许翻箱子，而且**用多少取多少**（不搬空）。

本模块只回答三件事，全部是纯函数（好测、可离线验证）：
  1. `evaluate_craft()`：要造 N 个某物，自己背包够不够、缺什么；
  2. `route_containers()`：已知容器（POI storage）里谁能补上缺口、各拿多少（绝不超需求）；
  3. `clamp_take()`：一次取用最多只能取"还缺的量"。

**合成本身不在这里实现** —— Java 侧用原版配方放置（`craft_via_recipe`）。下面的
`RECIPES` 只用来"评估材料够不够"（数据表，不是合成算法，也不做递归展开）。
表里没有的配方一律交给 Java 的原版配方判断（`known=False`），**绝不猜**。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

# ── 容器 0（玩家自己的背包菜单）槽位布局 —— 与 Java 侧逐字对齐 ──
SLOT_ARMOR_START, SLOT_ARMOR_END = 5, 8      # 头盔 / 胸甲 / 护腿 / 靴子
SLOT_BACKPACK_START, SLOT_BACKPACK_END = 9, 35
SLOT_HOTBAR_START, SLOT_HOTBAR_END = 36, 44
SLOT_OFFHAND = 45                             # 背包菜单槽位就是 0..45

# 盔甲槽位顺序（5..8），与 `open_container` 的背包菜单布局一致。
ARMOR_SLOT_ORDER: tuple[int, int, int, int] = (
    SLOT_ARMOR_START, SLOT_ARMOR_START + 1, SLOT_ARMOR_START + 2, SLOT_ARMOR_END)

_DIAMOND = "diamond"
_IRON = "iron_ingot"
_LEATHER = "leather"
_GOLD = "gold_ingot"

# 盔甲件（"点容器找盔甲"时要能认出容器里的盔甲）。key = 材料，value = 部件后缀。
_ARMOR_ITEMS: frozenset[str] = frozenset(
    f"{material}_{piece}"
    for material in ("leather", "chainmail", "iron", "golden", "diamond", "netherite", "turtle")
    for piece in ("helmet", "chestplate", "leggings", "boots")
) | {"turtle_helmet"}

# item → 单次合成所需材料（数据表；未知的 item 不在表里 = known False）。
RECIPES: dict[str, dict[str, int]] = {
    "oak_planks": {"oak_log": 1},
    "stick": {"oak_planks": 2},
    "crafting_table": {"oak_planks": 4},
    "chest": {"oak_planks": 8},
    "furnace": {"cobblestone": 8},
    "torch": {"coal": 1, "stick": 1},
    "ladder": {"stick": 7},
    "bowl": {"oak_planks": 3},
    "wooden_sword": {"oak_planks": 2, "stick": 1},
    "wooden_pickaxe": {"oak_planks": 3, "stick": 2},
    "stone_sword": {"cobblestone": 2, "stick": 1},
    "stone_pickaxe": {"cobblestone": 3, "stick": 2},
    "iron_sword": {_IRON: 2, "stick": 1},
    "iron_pickaxe": {_IRON: 3, "stick": 2},
    "diamond_sword": {_DIAMOND: 2, "stick": 1},
    "diamond_pickaxe": {_DIAMOND: 3, "stick": 2},
    "shield": {"oak_planks": 6, _IRON: 1},
    "bucket": {_IRON: 3},
    "bread": {"wheat": 3},
    "leather_helmet": {_LEATHER: 5},
    "leather_chestplate": {_LEATHER: 8},
    "leather_leggings": {_LEATHER: 7},
    "leather_boots": {_LEATHER: 4},
    "iron_helmet": {_IRON: 5},
    "iron_chestplate": {_IRON: 8},
    "iron_leggings": {_IRON: 7},
    "iron_boots": {_IRON: 4},
    "golden_helmet": {_GOLD: 5},
    "golden_chestplate": {_GOLD: 8},
    "golden_leggings": {_GOLD: 7},
    "golden_boots": {_GOLD: 4},
    "diamond_helmet": {_DIAMOND: 5},
    "diamond_chestplate": {_DIAMOND: 8},
    "diamond_leggings": {_DIAMOND: 7},
    "diamond_boots": {_DIAMOND: 4},
}


def normalize_item(name: Any) -> str:
    """物品名归一化：去空白、转小写、剥掉 `minecraft:` 命名空间。"""
    text = str(name or "").strip().lower().replace(" ", "")
    if text.startswith("minecraft:"):
        text = text[len("minecraft:"):]
    return text


def inventory_counts(inventory: Any) -> dict[str, int]:
    """背包快照 → {物品: 总数量}。接受 `[{"name"|"item_id": ..., "count": ...}]`。"""
    counts: dict[str, int] = {}
    if not isinstance(inventory, (list, tuple)):
        return counts
    for entry in inventory:
        if not isinstance(entry, dict):
            continue
        item = normalize_item(entry.get("name") or entry.get("item_id") or entry.get("item"))
        if not item:
            continue
        raw = entry.get("count")
        try:
            amount = int(raw) if raw is not None else 0
        except (TypeError, ValueError):
            continue
        if amount > 0:
            counts[item] = counts.get(item, 0) + amount
    return counts


def is_armor(name: Any) -> bool:
    """是不是盔甲件（"点容器找盔甲"用：容器里的盔甲要能认出来）。"""
    return normalize_item(name) in _ARMOR_ITEMS


@dataclass(frozen=True)
class MaterialPlan:
    """一次制作请求的材料评估结果（`known=False` 表示配方不在表里，交给 Java）。"""

    item: str
    count: int
    known: bool
    required: dict[str, int]
    have: dict[str, int]
    missing: dict[str, int]

    @property
    def sufficient(self) -> bool:
        """自己背包里的材料够不够（含快捷栏 —— 背包快照本来就是全部）。"""
        return not self.missing


def evaluate_craft(item: Any, count: Any = 1, inventory: Any = None) -> MaterialPlan:
    """评估"造 count 个 item"在**自己背包**里够不够，返回缺口（缺料优先只用背包这种）。"""
    item_id = normalize_item(item)
    try:
        amount = int(count)
    except (TypeError, ValueError):
        amount = 1
    amount = max(1, amount)
    have = inventory_counts(inventory)
    recipe = RECIPES.get(item_id)
    if recipe is None:
        return MaterialPlan(item_id, amount, False, {}, have, {})
    required = {material: per * amount for material, per in recipe.items()}
    missing = {
        material: need - have.get(material, 0)
        for material, need in required.items()
        if need - have.get(material, 0) > 0
    }
    return MaterialPlan(item_id, amount, True, required, have, missing)


def describe_missing(plan: MaterialPlan) -> str:
    """缺口的中文摘要（诚实说明用），如 `iron_ingot x3, stick x2`。"""
    return ", ".join(f"{material} x{amount}" for material, amount in plan.missing.items())


def container_contents(entry: dict) -> dict[str, int]:
    """容器（POI / nearby_storage）的内容 → {物品: 数量}。"""
    if not isinstance(entry, dict):
        return {}
    for key in ("contents", "items"):
        value = entry.get(key)
        if isinstance(value, (list, tuple)):
            return inventory_counts(value)
    return {}


@dataclass(frozen=True)
class ContainerPick:
    """去哪个容器、拿什么、拿多少（`take` 恒 <= 缺口，绝不搬空）。"""

    block_id: str
    x: Optional[float]
    y: Optional[float]
    z: Optional[float]
    distance: Optional[float]
    take: dict[str, int]


def _coord(entry: dict, key: str) -> Optional[float]:
    value = entry.get(key)
    if isinstance(value, bool):
        return None
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def route_containers(missing: Any, containers: Any, *, limit: int = 3) -> list[ContainerPick]:
    """按"最近优先"选容器补齐缺口，每个容器只取还缺的量（不搬空）。

    `containers` = 已知容器条目（POI storage / nearby_storage），字段尽力而为：
    `block_id`/`name`、`x`/`y`/`z`、`distance`、`contents`（`[{item_id, count}]`）。
    """
    need = {
        normalize_item(key): int(value)
        for key, value in (missing or {}).items()
        if int(value) > 0
    } if isinstance(missing, dict) else {}
    if not need:
        return []

    def _order(entry: dict):
        distance = entry.get("distance")
        distance = float(distance) if isinstance(distance, (int, float)) else 1e9
        last_seen = entry.get("last_seen")
        last_seen = float(last_seen) if isinstance(last_seen, (int, float)) else 0.0
        return (distance, -last_seen)

    picks: list[ContainerPick] = []
    seen: set[tuple] = set()
    for entry in sorted((c for c in (containers or []) if isinstance(c, dict)), key=_order):
        x, y, z = _coord(entry, "x"), _coord(entry, "y"), _coord(entry, "z")
        block_id = str(entry.get("block_id") or entry.get("name") or "minecraft:chest")
        key = (block_id, x, y, z)
        if key in seen:  # POI 与视野内条目常常是同一个箱子，别去两次
            continue
        seen.add(key)
        contents = container_contents(entry)
        take: dict[str, int] = {}
        for material, want in list(need.items()):
            available = contents.get(material, 0)
            if available <= 0:
                continue
            grabbed = min(want, available)
            take[material] = grabbed
            left = want - grabbed
            if left > 0:
                need[material] = left
            else:
                del need[material]
        if take:
            picks.append(ContainerPick(block_id=block_id, x=x, y=y, z=z,
                                       distance=entry.get("distance")
                                       if isinstance(entry.get("distance"), (int, float)) else None,
                                       take=take))
        if not need or len(picks) >= limit:
            break
    return picks


def armor_in_containers(containers: Any, *, limit: int = 3) -> list[ContainerPick]:
    """已知容器里翻出来的盔甲（"点容器找盔甲"）：每个容器只报它有的盔甲件。

    `take` 里的数量仍然只代表"容器里有几件"，真正取用时由 `clamp_take` 按需求封顶。
    """
    picks: list[ContainerPick] = []
    ordered = sorted((c for c in (containers or []) if isinstance(c, dict)),
                     key=lambda e: e.get("distance")
                     if isinstance(e.get("distance"), (int, float)) else 1e9)
    for entry in ordered:
        contents = container_contents(entry)
        pieces = {name: amount for name, amount in contents.items() if is_armor(name)}
        if not pieces:
            continue
        picks.append(ContainerPick(
            block_id=str(entry.get("block_id") or entry.get("name") or "minecraft:chest"),
            x=_coord(entry, "x"), y=_coord(entry, "y"), z=_coord(entry, "z"),
            distance=entry.get("distance") if isinstance(entry.get("distance"), (int, float)) else None,
            take=pieces))
        if len(picks) >= limit:
            break
    return picks


def clamp_take(item: Any, requested: Any, need: Any) -> int:
    """一次取用最多只能取"还缺的量"（"用多少取多少"的硬保证）。

    - 有缺口登记（`need` 里有这件物品）→ `min(requested, remaining)`；requested 缺省/<=0
      表示"整组"，按剩余缺口封顶（**绝不整组搬空**）。
    - 没有缺口登记（不是为某次制作而取）→ 如实按调用方给的数字（0 表示不取）。
    """
    item_id = normalize_item(item)
    try:
        remaining = int((need or {}).get(item_id, 0))
    except (TypeError, ValueError):
        remaining = 0
    try:
        want = int(requested) if requested is not None else 0
    except (TypeError, ValueError):
        want = 0
    if remaining <= 0:
        return max(0, want)
    if want <= 0:
        return remaining
    return min(want, remaining)
