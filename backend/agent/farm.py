"""种田（farm）—— 在**授权地块**里锄地 / 播种 / 收割 / 补种的判定（纯逻辑）。

用户口径（设计见 `docs/dev/TERRITORY.md` §5、§11）：

- **全部走通用右键**（`use_item` 的按住语义）与 `mine_block` —— **不新增任何 Java 命令**，
  也**不写特殊交互分支**（用户反复强调的"右键去特殊化"）；
- 自家农田声明为 `protect:[]`（**要干活**）；带 `protect:["crops"]` 的是**保护区**，
  Java 会硬拦收割 —— 被拦就**如实回报"区域名 + 规则"**，**绝不绕过**、**不换地方偷干**；
- **没有任何授权地块 → 如实拒绝**（不在别人地里干活）；
- 补种用**同一作物**（种子从背包或借用来都行，反正都在背包里）；**缺种子/缺锄头如实报**。

本模块只回答"该干什么、拿什么干、缺什么"，发帧在 planner 里。
"""

from __future__ import annotations

from typing import Any, Optional

from .craft import inventory_counts, normalize_item

# 作物方块 → 种子/种植物品（"补种用同一作物"就靠这张表）。
CROP_SEEDS: dict[str, str] = {
    "wheat": "wheat_seeds",
    "carrots": "carrot",
    "potatoes": "potato",
    "beetroots": "beetroot_seeds",
    "nether_wart": "nether_wart",
    "torchflower_crop": "torchflower_seeds",
    "pitcher_crop": "pitcher_pod",
}

# 能锄成耕地（或补种时要处理的）的土方块。
SOIL_BLOCKS: tuple[str, ...] = (
    "dirt", "grass_block", "coarse_dirt", "rooted_dirt", "podzol", "dirt_path",
)
# 播种的目标方块。
PLANT_BLOCKS: tuple[str, ...] = ("farmland",)

# 动作名（工具参数的规范写法；中文口语在 `normalize_action` 里翻）。
ACTION_TILL = "till"
ACTION_PLANT = "plant"
ACTION_HARVEST = "harvest"
ACTION_REPLANT = "replant"
FARM_ACTIONS: tuple[str, ...] = (ACTION_TILL, ACTION_PLANT, ACTION_HARVEST, ACTION_REPLANT)

_ACTION_ALIASES: dict[str, str] = {
    "till": ACTION_TILL, "hoe": ACTION_TILL, "耕作": ACTION_TILL, "锄地": ACTION_TILL,
    "耕地": ACTION_TILL, "锄": ACTION_TILL,
    "plant": ACTION_PLANT, "sow": ACTION_PLANT, "播种": ACTION_PLANT, "种": ACTION_PLANT,
    "种植": ACTION_PLANT,
    "harvest": ACTION_HARVEST, "reap": ACTION_HARVEST, "收割": ACTION_HARVEST,
    "收": ACTION_HARVEST, "收成": ACTION_HARVEST, "采摘": ACTION_HARVEST,
    "replant": ACTION_REPLANT, "补种": ACTION_REPLANT, "补播": ACTION_REPLANT,
    "收完补种": ACTION_REPLANT,
}

_ACTION_LABEL: dict[str, str] = {
    ACTION_TILL: "锄地", ACTION_PLANT: "播种", ACTION_HARVEST: "收割", ACTION_REPLANT: "收割并补种",
}


def _block_id(value: Any) -> str:
    return normalize_item(value)


def normalize_action(value: Any) -> str:
    """动作名归一化：认不出的返回空串（不猜成别的动作）。"""
    text = str(value or "").strip().lower().replace(" ", "")
    if not text:
        return ""
    return _ACTION_ALIASES.get(text, text if text in FARM_ACTIONS else "")


def action_label(action: Any) -> str:
    return _ACTION_LABEL.get(str(action or ""), str(action or ""))


def crop_of(block_id: Any) -> str:
    """这个方块是不是作物 → 返回作物名（不是作物返回空串）。"""
    name = _block_id(block_id)
    for crop in CROP_SEEDS:
        if name == crop or name.endswith(f"_{crop}"):
            return crop
    return ""


def seed_for_crop(block_id: Any) -> str:
    """这个作物方块对应的种子物品（认不出返回空串 —— 不猜）。"""
    return CROP_SEEDS.get(crop_of(block_id), "")


def is_hoe(item: Any) -> bool:
    return _block_id(item).endswith("_hoe")


def is_soil(block_id: Any) -> bool:
    return _block_id(block_id) in SOIL_BLOCKS


def is_plantable(block_id: Any) -> bool:
    return _block_id(block_id) in PLANT_BLOCKS


def _rows(blocks: Any) -> list[dict]:
    out = []
    for row in blocks or []:
        if not isinstance(row, dict):
            continue
        x, y, z = row.get("x"), row.get("y"), row.get("z")
        if any(not isinstance(value, (int, float)) or isinstance(value, bool)
               for value in (x, y, z)):
            continue
        out.append(row)
    return out


def _distance(row: dict) -> float:
    value = row.get("distance")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return 1e9


def nearest(blocks: Any, predicate) -> Optional[dict]:
    """附近满足条件的**最近**方块（只按 `nearby_blocks` 的真数据；找不到返回 None）。"""
    hits = [row for row in _rows(blocks) if predicate(row)]
    if not hits:
        return None
    hits.sort(key=_distance)
    return hits[0]


def targets(blocks: Any) -> dict:
    """把附近方块分成三类（作物 / 耕地 / 可锄的土）。"""
    rows = _rows(blocks)
    return {
        "crops": sorted([row for row in rows if crop_of(row.get("block_id"))], key=_distance),
        "farmland": sorted([row for row in rows if is_plantable(row.get("block_id"))],
                           key=_distance),
        "soil": sorted([row for row in rows if is_soil(row.get("block_id"))], key=_distance),
    }


def choose_action(requested: Any, blocks: Any, counts: Any) -> dict:
    """该干什么：`action` 给了就用它，没给就在授权地块里挑一件最值得做的。

    返回 `{"action", "target", "seed", "reason"}`；`action` 为空 + `reason` 非空 = 没活干。
    """
    found = targets(blocks)
    counts = counts if isinstance(counts, dict) else {}
    if requested:
        action = normalize_action(requested)
        if not action:
            return {"action": "", "target": None, "seed": "", "reason": "unknown_action"}
        return _for_action(action, found, counts)
    if found["crops"]:
        return _for_action(ACTION_HARVEST, found, counts)
    if found["farmland"] and _any_seed(counts):
        return _for_action(ACTION_PLANT, found, counts)
    if found["soil"] and any(is_hoe(item) for item in counts):
        return _for_action(ACTION_TILL, found, counts)
    return {"action": "", "target": None, "seed": "", "reason": "nothing_to_do"}


def _any_seed(counts: dict) -> bool:
    seeds = set(CROP_SEEDS.values())
    return any(item in seeds for item in counts)


def _for_action(action: str, found: dict, counts: dict) -> dict:
    if action == ACTION_HARVEST:
        target = found["crops"][0] if found["crops"] else None
        if target is None:
            return {"action": "", "target": None, "seed": "", "reason": "no_crop"}
        return {"action": action, "target": target,
                "seed": seed_for_crop(target.get("block_id")), "reason": ""}
    if action == ACTION_REPLANT:
        target = found["crops"][0] if found["crops"] else None
        if target is None:
            return {"action": "", "target": None, "seed": "", "reason": "no_crop"}
        return {"action": action, "target": target,
                "seed": seed_for_crop(target.get("block_id")), "reason": ""}
    if action == ACTION_PLANT:
        target = found["farmland"][0] if found["farmland"] else None
        if target is None:
            return {"action": "", "target": None, "seed": "", "reason": "no_farmland"}
        seed = _best_seed(counts)
        if not seed:
            return {"action": ACTION_PLANT, "target": target, "seed": "", "reason": "missing_seeds"}
        return {"action": action, "target": target, "seed": seed, "reason": ""}
    # till
    target = found["soil"][0] if found["soil"] else None
    if target is None:
        return {"action": "", "target": None, "seed": "", "reason": "no_soil"}
    if not any(is_hoe(item) for item in counts):
        return {"action": ACTION_TILL, "target": target, "seed": "", "reason": "missing_hoe"}
    return {"action": action, "target": target, "seed": "", "reason": ""}


def _best_seed(counts: dict) -> str:
    """背包里（含借来的）能用的种子：优先小麦种子，其余按名字排 —— 只挑真有的。"""
    seeds = sorted(item for item in counts if item in set(CROP_SEEDS.values()))
    if not seeds:
        return ""
    return "wheat_seeds" if "wheat_seeds" in seeds else seeds[0]


def missing_items(action: Any, seed: Any, counts: Any) -> list[str]:
    """这个动作缺什么（如实列出来；不缺就是空表）。"""
    counts = counts if isinstance(counts, dict) else {}
    action = normalize_action(action)
    missing: list[str] = []
    if action == ACTION_TILL and not any(is_hoe(item) for item in counts):
        missing.append("锄头（*_hoe）")
    if action in (ACTION_PLANT, ACTION_REPLANT):
        seed_id = normalize_item(seed)
        if not seed_id or counts.get(seed_id, 0) <= 0:
            missing.append(f"种子（{seed_id or '同一作物的种子'}）")
    return missing


def hold_index(inventory: Any, item: Any, *, hotbar_only: bool = True) -> Optional[int]:
    """背包里这件物品的槽位（`select_hotbar` 只认 0-8，所以要 `hotbar_only`）。"""
    item_id = normalize_item(item)
    if not item_id or not isinstance(inventory, (list, tuple)):
        return None
    for entry in inventory:
        if not isinstance(entry, dict):
            continue
        if normalize_item(entry.get("name") or entry.get("item_id") or entry.get("item")) != item_id:
            continue
        raw = entry.get("slot")
        if not isinstance(raw, int) or isinstance(raw, bool):
            continue
        if hotbar_only and not 0 <= raw <= 8:
            continue
        return int(raw)
    return None


def counts_of(inventory: Any) -> dict:
    return inventory_counts(inventory)


def note(action: Any, plot: Any, target: Any, *, reason: str = "", missing: Any = None,
         blocked: Any = None, seed: Any = "") -> str:
    """一句**如实**的说法（干成了什么 / 为什么没干 / 被哪条规则拦下）。"""
    where = f"「{plot}」" if plot else "授权农田"
    if blocked:
        return (f"（区域「{blocked.get('zone')}」的「{blocked.get('rule')}」规则不许在这儿动手，"
                f"我没动 —— 那是别人的保护区）")
    if reason == "no_grant":
        return "（这附近没有授权给我的农田/基地，我不在别人的地里干活）"
    if reason == "nothing_to_do":
        return "（这块授权农田附近没有能干的活：没作物、没耕地、也没法锄的土）"
    if reason == "no_crop":
        return f"（{where}附近没看到作物，收不了）"
    if reason == "no_farmland":
        return f"（{where}附近没看到耕地，没地方播种）"
    if reason == "no_soil":
        return f"（{where}附近没看到能锄的土）"
    if reason == "unknown_action":
        return "（种田动作只认 till/plant/harvest/replant，这个我不认）"
    if reason in ("missing_hoe", "missing_seeds"):
        lack = "、".join(missing or []) or "材料"
        return f"（{where}缺{lack}：背包里没有，去借或让我先弄来，我不硬凑）"
    pos = ""
    if isinstance(target, dict):
        pos = f"({float(target.get('x')):.0f},{float(target.get('y')):.0f},{float(target.get('z')):.0f})"
    tail = f"，补种用{seed}" if seed else ""
    return f"（在{where}干{action_label(action)}{pos}{tail}）"
