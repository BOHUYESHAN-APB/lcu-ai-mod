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

from . import perception
from .craft import inventory_counts, normalize_item
from .territory import ACTION_FARM, may_act

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
    """附近满足条件的**最近**方块（只按**真数据**行里报的坐标；找不到返回 None）。

    行来自 `perception.blocks_rows`（**唯一读法**）：模组报**逐条清单**时就是全量；
    报**分级摘要**时行里通常**没有坐标**（摘要只给"哪种方块几个 + 最近一种"的距离）——
    这时这里会返回 `None`，调用方**必须**说明"摘要里没有，得按需扫描"，**不许**说成
    "附近没有"（`note(..., partial=True)` / `perception.block_missing_note` 就是这个说法）。
    没坐标的行**不编坐标**、也不参与排序。
    """
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
         blocked: Any = None, seed: Any = "", partial: bool = False) -> str:
    """一句**如实**的说法（干成了什么 / 为什么没干 / 被哪条规则拦下）。

    `partial=True`（这一帧是**分级摘要**：方块的逐条坐标默认没上报）时，"
    没找到作物/耕地/土"这类结论**必须**改口：摘要里没有**不等于**附近没有。
    """
    where = f"「{plot}」" if plot else "授权农田"
    if blocked:
        return (f"（区域「{blocked.get('zone')}」的「{blocked.get('rule')}」规则不许在这儿动手，"
                f"我没动 —— 那是别人的保护区）")
    if reason == "no_grant":
        return "（这附近没有授权给我的农田/基地，我不在别人的地里干活）"
    if bool(partial) and reason in ("nothing_to_do", "no_crop", "no_farmland", "no_soil"):
        subject = {"no_crop": "作物", "no_farmland": "耕地", "no_soil": "能锄的土"}.get(
            reason, "作物/耕地/土")
        return (f"（{where}的{subject}：这一帧是**分级摘要**，方块的逐条坐标默认没上报 —— "
                f"「摘要里没有」**不等于**「附近没有」；要动手先按需扫描"
                f"（{perception.SCAN_BLOCK_TOOL}）拿到具体位置）")
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


# ── 别人家的田（人设驱动；**默认关**，权限表显式放开）────────────
# 用户口径：「别人家的田，那要看情况……如果是朋友家的，那可以**帮忙种一种**，也可以去
# **捣乱**……想**展示出人设**、**个性化**一点，让他更像**人**」。所以：
#   `none`（默认）= 别人家的田一概不碰；
#   `help`        = 可以**帮忙种**（锄地/播种/收割/补种这类）；
#   `mischief`    = 人设允许时还能搞点**无伤大雅**的恶作剧（**禁止破坏财产/伤害生物** ✗）。
# 「当前被允许做哪种」如实给 L2，**用不用由 LLM 决定** —— 决策链里**不写 tier 分支**。
OTHERS_NONE = "none"
OTHERS_HELP = "help"
OTHERS_MISCHIEF = "mischief"
FARM_OTHERS_MODES: tuple[str, ...] = (OTHERS_NONE, OTHERS_HELP, OTHERS_MISCHIEF)
# help 能做的活（"帮忙种一种"）：不破坏、不伤害。
HELP_ACTIONS: tuple[str, ...] = (ACTION_TILL, ACTION_PLANT, ACTION_HARVEST, ACTION_REPLANT)
# mischief 能做的：**只动土/种东西**这一档，绝不破坏财产、绝不伤害生物 ✗。
MISCHIEF_ACTIONS: tuple[str, ...] = (ACTION_TILL, ACTION_PLANT)


def normalize_others(value: Any, default: str = OTHERS_NONE) -> str:
    """`farm_others` 档位归一化：认不出的回默认（默认＝**不碰**）。"""
    text = str(value or "").strip().lower()
    return text if text in FARM_OTHERS_MODES else default


def others_verdict(mode: Any, action: Any, *, mischievous: bool = False) -> dict:
    """别人家田上"这次允不允许动手、算不算捣乱"的判定（**全查表，不写 tier 分支**）。

    返回 `{"allowed", "mischief", "reason"}`；`allowed=False` 时 `reason` 说清为什么。
    """
    mode = normalize_others(mode)
    verb = normalize_action(action)
    if mode == OTHERS_NONE:
        return {"allowed": False, "mischief": False, "reason": "farm_others_off"}
    if mischievous:
        if mode != OTHERS_MISCHIEF:
            return {"allowed": False, "mischief": False, "reason": "farm_others_no_mischief"}
        if verb not in MISCHIEF_ACTIONS:
            return {"allowed": False, "mischief": False, "reason": "mischief_not_harmless"}
        return {"allowed": True, "mischief": True, "reason": "mischief_allowed"}
    if verb in HELP_ACTIONS:
        return {"allowed": True, "mischief": False, "reason": "help_allowed"}
    return {"allowed": False, "mischief": False, "reason": "farm_others_action"}


def land_owner(row: Any) -> dict:
    """这块田/畜栏是谁的：`granted_by` 是 master（或没写）→ 自家；否则算**别人家**。

    邻居只可能是"别人明确声明成他家的"（`granted_by.tier` 是 friend/guest）——
    没写授予人的一律当自家（那是我们自己记下来/被 master 授的）。
    """
    granted = row.get("granted_by") if isinstance(row, dict) else None
    granted = granted if isinstance(granted, dict) else {}
    tier = str(granted.get("tier") or "").strip().lower()
    name = str(granted.get("name") or "").strip()
    if tier and tier != "master":
        return {"mine": False, "tier": tier, "name": name}
    return {"mine": True, "tier": tier or "master", "name": name}


def others_note(owner: dict, action: Any, verdict: dict) -> str:
    """动别人家田时的**如实**说法（让 LLM 知道"这是谁家的、这次算不算捣乱"）。"""
    who = owner.get("name") or owner.get("tier") or "别人"
    label = action_label(action) or "动手"
    if not verdict.get("allowed"):
        if verdict.get("reason") == "farm_others_off":
            return f"（那是{who}家的田：权限表里没放开采别人家的田，我不动）"
        if verdict.get("reason") == "farm_others_no_mischief":
            return f"（那是{who}家的田：只许帮忙种，不许搞恶作剧）"
        if verdict.get("reason") == "mischief_not_harmless":
            return f"（那是{who}家的田：恶作剧只许无伤大雅的，破坏/伤生物的我不做）"
        return f"（那是{who}家的田：这一档没放开「{label}」）"
    if verdict.get("mischief"):
        return f"（在{who}家的田上搞点无伤大雅的恶作剧：{label}）"
    return f"（帮{who}家的田{label}）"


# ── 喂动物（只喂**授权区域**内的；走通用右键，不新增 Java 命令）──────
# 动物种类 → 能喂的食物（按 id 认；认不出就不喂 —— 不硬凑）。
ANIMAL_FOODS: dict[str, tuple[str, ...]] = {
    "cow": ("wheat",), "mooshroom": ("wheat",), "sheep": ("wheat",),
    "goat": ("wheat",), "llama": ("wheat",), "trader_llama": ("wheat",),
    "pig": ("carrot", "potato", "beetroot"),
    "chicken": ("wheat_seeds", "beetroot_seeds", "melon_seeds", "pumpkin_seeds"),
    "rabbit": ("carrot", "dandelion"),
    "horse": ("golden_apple", "apple"), "donkey": ("golden_apple", "apple"),
    "mule": ("golden_apple", "apple"), "fox": ("sweet_berries",),
    "panda": ("bamboo",), "turtle": ("seagrass",), "bee": ("dandelion", "poppy"),
    "cat": ("cod", "salmon"), "ocelot": ("cod", "salmon"),
    "parrot": ("wheat_seeds", "melon_seeds", "pumpkin_seeds", "beetroot_seeds"),
    "wolf": ("beef", "cooked_beef", "chicken", "cooked_chicken", "porkchop",
             "cooked_porkchop", "mutton", "cooked_mutton", "rabbit", "cooked_rabbit"),
}


def animal_kind(entity: Any) -> str:
    """这只实体是不是动物、是哪一种（只按 `type`/`name` 认，认不出返回空串）。"""
    if not isinstance(entity, dict):
        return ""
    text = normalize_item(entity.get("type") or entity.get("entity_type")
                          or entity.get("name"))
    if not text:
        return ""
    short = text.rsplit("/", 1)[-1]
    for kind in ANIMAL_FOODS:
        if short == kind or short.endswith("_" + kind) or short.endswith(kind):
            return kind
    return ""


def foods_for(entity: Any) -> tuple[str, ...]:
    """这只动物能吃的食物表（认不出就空表）。"""
    return ANIMAL_FOODS.get(animal_kind(entity), ())


def feed_food(counts: Any, entity: Any) -> str:
    """背包里能喂这只动物的食物（按食物表顺序挑真有的）——没有就空串（不硬凑）。"""
    rows = counts if isinstance(counts, dict) else {}
    for food in foods_for(entity):
        if int(rows.get(food, 0) or 0) > 0:
            return food
    return ""


def _entity_rows(entities: Any) -> list[dict]:
    return [row for row in (entities or []) if isinstance(row, dict)]


def feed_targets(entities: Any, zones: Any, territories: Any) -> dict:
    """能喂的动物（**只喂授权区域**内的）+ 被硬拦的（报区域名+规则）+ 没授权的。

    授权口径与农活一致（`may_act(..., ACTION_FARM)`）：自家畜栏能喂，别人家的畜栏不碰；
    动物若站在带保护的保护区里 → 如实报"区域名 + 规则"，**不绕过**。
    """
    feedable: list[dict] = []
    blocked: list[dict] = []
    unauthorized: list[dict] = []
    for entity in _entity_rows(entities):
        kind = animal_kind(entity)
        if not kind:
            continue
        x, y, z = entity.get("x"), entity.get("y"), entity.get("z")
        if any(not isinstance(value, (int, float)) or isinstance(value, bool)
               for value in (x, y, z)):
            unauthorized.append({"animal": kind, "reason": "no_position"})
            continue
        verdict = may_act(territories, zones, float(x), float(y), float(z), ACTION_FARM)
        if verdict.get("allowed"):
            feedable.append({**entity, "kind": kind})
            continue
        if verdict.get("reason") == "zone_protection":
            blocked.append({"animal": kind, "zone": verdict.get("zone"),
                            "rule": verdict.get("rule"), "x": float(x), "y": float(y),
                            "z": float(z)})
        else:
            unauthorized.append({"animal": kind, "reason": str(verdict.get("reason") or "")})
    return {"feedable": feedable, "blocked": blocked, "unauthorized": unauthorized}


def feed_note(targets: dict, *, fed: Any = None, missing: Any = None,
              partial: bool = False) -> str:
    """喂食这一段的**如实**说法（喂了几只 / 缺什么 / 被哪条规则拦下）。

    `partial=True`（分级摘要：被动生物的逐条坐标默认没上报）时，"没喂成/没有能喂的动物"
    **不许**说成"附近没有动物"—— 摘要里看不到的档位本来就没上报。
    """
    blocked = (targets or {}).get("blocked") or []
    if blocked:
        hit = blocked[0]
        return (f"（「{hit.get('zone')}」的「{hit.get('rule')}」规则不许在这儿动手，"
                f"那只 {hit.get('animal')} 我没喂）")
    fed_rows = {str(k): int(v) for k, v in (fed or {}).items() if int(v or 0) > 0}
    if fed_rows:
        total = sum(fed_rows.values())
        detail = "、".join(f"{kind} x{count}" for kind, count in fed_rows.items())
        return f"（喂了 {total} 只（{detail}））"
    if missing:
        return f"（没喂成：缺 {'、'.join(str(item) for item in missing)}）"
    unauthorized = (targets or {}).get("unauthorized") or []
    if unauthorized:
        return "（附近没有授权给我的畜栏，别人的牲口我不喂）"
    if partial:
        return ("（动物：这一帧是**分级摘要**，被动生物的逐条坐标默认没上报 —— "
                "「摘要里没有」**不等于**「附近没有」；要喂就先按需扫描"
                f"（{perception.SCAN_ENTITY_TOOL}）拿到具体位置）")
    return "（附近没有能喂的动物）"


# ── 复合链（farm_cycle）：收割 → 补种 → 取料 → 合成 → 喂 ────────────
# 用户口径：「种田和喂养动物可以是**一连套**的东西……究竟可以**收割，然后再种下去**，
# 然后再把收割出来的东西**直接去喂动物**，也有可能**要合成再喂**」。
CYCLE_STEPS: tuple[str, ...] = ("harvest", "replant", "fetch", "craft", "feed")


def cycle_step(name: Any, ok: bool, *, reason: str = "", detail: str = "",
               items: Any = None) -> dict:
    """复合链的一步账：**每一步的产物/缺口都如实入账**（不收成分也不硬凑）。"""
    return {"step": str(name or ""), "ok": bool(ok), "reason": str(reason or ""),
            "detail": str(detail or ""),
            "items": {str(k): int(v) for k, v in (items or {}).items() if int(v or 0) > 0}}


def cycle_note(steps: Any) -> str:
    """复合链的**如实**一句：哪几步成了、哪几步缺什么（差在哪里说得清）。"""
    rows = [row for row in (steps or []) if isinstance(row, dict)]
    if not rows:
        return ""
    done = [f"{row.get('step')}" for row in rows if row.get("ok")]
    gaps = [f"{row.get('step')}（{row.get('reason') or '没成'}"
            + (f"：{row.get('detail')}" if row.get("detail") else "") + "）"
            for row in rows if not row.get("ok")]
    head = f"（复合链：成了 {'、'.join(done)}" if done else "（复合链："
    if gaps:
        return head + f"；缺口 {'、'.join(gaps)}）"
    return head + "，全成了）"
