"""领地 / 多基地 / 容器策略 / 区域保护 —— 全部按 **world 层** 分区落盘。

设计口径：`docs/dev/TERRITORY.md`（§1 三对象 / §7 多基地 / §9 区域保护 / §8 造床）与
代码注释里的用户原话。三条硬约束：

* **坐标只在 world 层**（`world:<server>/<world>/<dim>`）：换服 / 换世界 / 换维度各自一份
  文件，绝不串号（与 `world_memory.WorldMemoryStore` 同一套分区键）；
* **保护优先于允许**：命中区域保护就是拒绝，领地授权再多也不翻案（与 Java `ZoneGuard`
  同口径 —— Java 是运行时硬拦的真相，这里是事实与存盘的那一份）；
* **区域外一律回落到借用台账**：没有领地授权的地方不是"随便拿"，而是"要走借用"。

本模块只做数据与**纯函数**判定（`protection_hit` / `may_act` / `current_base` /
`plan_zone_replay` / `bed_attempt` / `home_situation`），下发由 Session / Planner 负责 ——
这样判定规则能在没有 socket 的测试里被完整驱动。
"""

from __future__ import annotations

import copy
import json
import logging
import re
import threading
import time
from pathlib import Path
from typing import Any, Optional

from .world_memory import UNKNOWN, world_scope

logger = logging.getLogger("territory")

# 落盘根目录：与 `data/stations/`、`data/routes/` 同级，按 world 分区。
DEFAULT_TERRITORY_ROOT = Path(__file__).parent.parent / "data" / "territory"

# ── 领地授权项（§1 三对象）──────────────────────────────────────
GRANT_TAKE = "take"
GRANT_PLACE = "place"
GRANT_BREAK = "break"
GRANT_FARM = "farm"
GRANT_STORE = "store"
GRANTS: tuple[str, ...] = (GRANT_TAKE, GRANT_PLACE, GRANT_BREAK, GRANT_FARM, GRANT_STORE)

# ── 容器访问策略（§1：两维正交，范围管"在哪动手"，策略管"哪个箱子能动"）──
POLICY_FREE = "free"              # 所有权已交给它（随取，不记欠账）
POLICY_BORROW_ONLY = "borrow_only"  # 只走借用台账（借多少还多少）
POLICY_NO_TOUCH = "no_touch"      # 什么都不许
POLICIES: tuple[str, ...] = (POLICY_FREE, POLICY_BORROW_ONLY, POLICY_NO_TOUCH)

# friend 能给出的最强策略＝传递权（**默认不给任意取用权**）
FRIEND_MAX_POLICY = POLICY_BORROW_ONLY

BASE_KINDS: tuple[str, ...] = ("base", "camp", "outpost")

# 区域种类（声明语义）与保护项（硬拦；顺序 = 保守序，与 Java `ZoneGuard.RULE_PRIORITY` 一致）
ZONE_KINDS: tuple[str, ...] = ("farm", "animal_pen", "base", "storage", "build", "protected")
PROTECT_RULES: tuple[str, ...] = ("animals", "crops", "blocks", "containers")

# 动作 → 会触发哪些保护规则（Java 侧硬拦的种类：攻击 / 挖 / 放）
ACTION_ATTACK = "attack"
ACTION_MINE = "mine"
ACTION_PLACE = "place"
ACTION_TAKE = "take"
ACTION_STORE = "store"
ACTION_FARM = "farm"

ACTION_RULES: dict[str, tuple[str, ...]] = {
    ACTION_ATTACK: ("animals",),
    ACTION_MINE: ("crops", "blocks"),
    ACTION_PLACE: ("blocks",),
    # 农活会碰到作物与土方块：挨着 `protect` 里的 crops/blocks 就**先别动手**
    # （自家农田声明 `protect:[]` → 命中不了任何规则，照常干活）。
    ACTION_FARM: ("crops", "blocks"),
}
# 动手类动作 → 需要的领地授权项
ACTION_GRANT: dict[str, str] = {
    ACTION_MINE: GRANT_BREAK,
    ACTION_PLACE: GRANT_PLACE,
    ACTION_TAKE: GRANT_TAKE,
    ACTION_STORE: GRANT_STORE,
    ACTION_FARM: GRANT_FARM,
}

# 离家阈值（§3 安全兜底）：超过这个距离与时长**只上报**，绝不自己偷偷跑回去。
HOME_LEAVE_DISTANCE = 64.0
HOME_LEAVE_SECONDS = 600.0

# 造床材料（§8）：3 羊毛 + 3 木板
BED_WOOL = 3
BED_PLANKS = 3

# 敌对生物关键词（评估"周围安全"用；只按名字认，认不出就不当敌人）
HOSTILE_KEYWORDS: tuple[str, ...] = (
    "zombie", "skeleton", "creeper", "spider", "enderman", "witch", "slime",
    "pillager", "phantom", "blaze", "ghast", "drowned", "husk", "stray",
    "piglin", "hoglin", "magma", "silverfish", "warden", "ravager", "vex",
    "evoker", "vindicator", "guardian", "shulker", "bogged", "breeze",
)

AUDIT_MAX_LINES = 500


# ── 宽容解析（认不出就 None —— 不猜）────────────────────────────


def _num(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_int(value: Any) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _name(row: Any) -> str:
    return str((row or {}).get("name") or "").strip() if isinstance(row, dict) else ""


def _str_list(value: Any) -> list[str]:
    """词表归一化：接受字符串（**按逗号切**，工具参数就是 `a,b` 这种写法）或列表。"""
    if isinstance(value, str):
        value = re.split(r"[,，]", value)
    if not isinstance(value, (list, tuple)):
        return []
    out: list[str] = []
    for item in value:
        text = str(item or "").strip().lower()
        if text and text not in out:
            out.append(text)
    return out


def normalize_grants(value: Any) -> list[str]:
    """授权项归一化：只认已知的 5 项，保序去重（认不出就丢掉，不猜）。"""
    return [item for item in _str_list(value) if item in GRANTS]


def normalize_policy(value: Any, default: str = POLICY_BORROW_ONLY) -> str:
    """容器策略归一化：认不出就回默认（默认＝传递权，不是随取）。"""
    text = str(value or "").strip().lower()
    return text if text in POLICIES else default


def normalize_protect(value: Any) -> list[str]:
    """保护项归一化：只留已知项，按保守序排（与 Java 同序，结果稳定）。"""
    wanted = set(_str_list(value))
    return [rule for rule in PROTECT_RULES if rule in wanted]


def point_of(row: Any) -> Optional[tuple[float, float, float]]:
    """从一行数据里取坐标：支持顶层 x/y/z 或 `center:{x,y,z}`（区域用后者）。"""
    if not isinstance(row, dict):
        return None
    center = row.get("center") if isinstance(row.get("center"), dict) else row
    x, y, z = _num(center.get("x")), _num(center.get("y")), _num(center.get("z"))
    if x is None or y is None or z is None:
        return None
    return (x, y, z)


def _box_of(row: Any) -> Optional[dict]:
    box = row.get("box") if isinstance(row, dict) else None
    if not isinstance(box, dict):
        return None
    values = {key: _num(box.get(key)) for key in
              ("min_x", "min_y", "min_z", "max_x", "max_y", "max_z")}
    if any(value is None for value in values.values()):
        return None
    if values["min_x"] > values["max_x"] or values["min_y"] > values["max_y"] \
            or values["min_z"] > values["max_z"]:
        return None
    return {key: float(value) for key, value in values.items()}


def _distance(a: tuple[float, float, float], b: tuple[float, float, float]) -> float:
    return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2) ** 0.5


def zone_contains(zone: Any, x: Any, y: Any, z: Any) -> bool:
    """点是否落在区域内：给了 box 就按长方体（端点含），否则按**球**（三维欧氏）。

    与 Java `ZoneGuard.Zone.contains` 逐字同口径（形状只在 Java 侧硬拦生效，这里用于
    上下文与"事前别去试"的判断）。
    """
    point = (x, y, z)
    coords = tuple(_num(item) for item in point)
    if any(item is None for item in coords):
        return False
    px, py, pz = (float(item) for item in coords)  # type: ignore[misc]
    box = _box_of(zone)
    if box is not None:
        return (box["min_x"] <= px <= box["max_x"]
                and box["min_y"] <= py <= box["max_y"]
                and box["min_z"] <= pz <= box["max_z"])
    center = point_of(zone)
    radius = _num((zone or {}).get("radius")) if isinstance(zone, dict) else None
    if center is None or radius is None:
        return False
    dx, dy, dz = px - center[0], py - center[1], pz - center[2]
    return dx * dx + dy * dy + dz * dz <= radius * radius


def protection_hit(zones: Any, x: Any, y: Any, z: Any, action: str) -> Optional[dict]:
    """这次动作会不会被区域保护拦下 → `{"zone","rule"}`；不会拦就 None。

    **保护优先于允许**：命中保护规则就是拒绝，没有"更允许"的例外。
    多区重叠取**最保守**：按 `PROTECT_RULES` 的保守序定规则，同序时按区域名排序（稳定）。
    """
    applicable = ACTION_RULES.get(str(action or "").strip().lower())
    if not applicable or not isinstance(zones, (list, tuple)):
        return None
    for rule in PROTECT_RULES:
        if rule not in applicable:
            continue
        hits = []
        for zone in zones:
            if not isinstance(zone, dict):
                continue
            if rule not in _str_list(zone.get("protect")):
                continue
            if zone_contains(zone, x, y, z):
                hits.append(_name(zone) or "?")
        if hits:
            return {"zone": sorted(hits)[0], "rule": rule}
    return None


def territory_containing(territories: Any, x: Any, y: Any, z: Any) -> Optional[dict]:
    """点落在哪块领地里（多块重叠时按「最近中心」优先，同距离取「最先授权」）。"""
    hits: list[tuple[float, float, str, dict]] = []
    for row in territories or []:
        if not isinstance(row, dict):
            continue
        center = point_of(row)
        if center is None:
            continue
        inside = zone_contains(row, x, y, z)
        if not inside:
            continue
        distance = abs(_distance(center, (float(x), float(y), float(z))))
        since = _num(row.get("since")) or 0.0
        hits.append((distance, since, _name(row), row))
    if not hits:
        return None
    hits.sort(key=lambda item: (item[0], item[1], item[2]))
    return copy.deepcopy(hits[0][3])


def may_act(territories: Any, zones: Any, x: Any, y: Any, z: Any, action: str) -> dict:
    """动手前的判定：先看区域保护（硬），再看领地授权。

    返回 `{"allowed": bool, "reason": str, "zone"?, "rule"?, "territory"?}`：

    * `zone_protection` —— 命中了区域保护（带 region 名与规则），**无论有没有领地授权都拒**；
    * `granted` —— 落在某块领地里且该项授权在清单里；
    * `outside_grant` —— 没有授权（区域外一律回落到借用台账，不是"随便拿"）。
    """
    action = str(action or "").strip().lower()
    hit = protection_hit(zones, x, y, z, action)
    if hit:
        out = {"allowed": False, "reason": "zone_protection"}
        out.update(hit)
        return out
    need = ACTION_GRANT.get(action)
    if need is None:
        return {"allowed": False, "reason": "unknown_action"}
    territory = territory_containing(territories, x, y, z)
    if territory is not None and need in normalize_grants(territory.get("grants")):
        return {"allowed": True, "reason": "granted", "territory": _name(territory)}
    return {"allowed": False, "reason": "outside_grant"}


def current_base(bases: Any, x: Any, y: Any, z: Any) -> Optional[dict]:
    """当前在哪个基地：按**锚点距离最近**且落在半径内判定；没进任何基地 → None（野外）。

    野外一切按**最低权限**处理（判定方看到 None 就走借用台账那条保守路径）。
    """
    point = (_num(x), _num(y), _num(z))
    if any(item is None for item in point):
        return None
    where = (float(point[0]), float(point[1]), float(point[2]))
    best: Optional[tuple[float, str, dict]] = None
    for row in bases or []:
        if not isinstance(row, dict):
            continue
        anchor = point_of(row.get("anchor"))
        radius = _num(row.get("radius"))
        if anchor is None or radius is None:
            continue
        distance = _distance(anchor, where)
        if distance > radius:
            continue
        candidate = (distance, _name(row), row)
        if best is None or candidate[:2] < best[:2]:
            best = candidate
    return copy.deepcopy(best[2]) if best is not None else None


def nearest_base(bases: Any, x: Any, y: Any, z: Any) -> Optional[dict]:
    """离得最近的基地（不管在不在半径内）——"离家多远"要能看到。"""
    point = (_num(x), _num(y), _num(z))
    if any(item is None for item in point):
        return None
    where = (float(point[0]), float(point[1]), float(point[2]))
    best: Optional[tuple[float, str, dict]] = None
    for row in bases or []:
        if not isinstance(row, dict):
            continue
        anchor = point_of(row.get("anchor"))
        if anchor is None:
            continue
        candidate = (_distance(anchor, where), _name(row), row)
        if best is None or candidate[:2] < best[:2]:
            best = candidate
    return copy.deepcopy(best[2]) if best is not None else None


def home_situation(bases: Any, x: Any, y: Any, z: Any, away_since: Optional[float],
                   now: Optional[float] = None) -> dict:
    """"离家多远 / 多久"这件事的**事实**（只上报，不自己跑回去）。

    返回 `{"base","distance","away_seconds","away","over","threshold"...}`；没有基地或
    坐标缺失时 `base` 为空、`distance` 为 None（调用方照实说"不知道"）。
    """
    base = nearest_base(bases, x, y, z)
    if base is None:
        return {"base": "", "distance": None, "away_seconds": None, "away": False,
                "over": False, "distance_threshold": HOME_LEAVE_DISTANCE,
                "seconds_threshold": HOME_LEAVE_SECONDS}
    anchor = point_of(base.get("anchor"))
    distance = _distance(anchor, (float(x), float(y), float(z))) if anchor else None
    inside = current_base(bases, x, y, z)
    away = inside is None
    stamp = _num(away_since)
    seconds = None
    if away and stamp is not None and now is not None:
        seconds = max(0.0, float(now) - stamp)
    over = bool(away and distance is not None and distance > HOME_LEAVE_DISTANCE
                and seconds is not None and seconds >= HOME_LEAVE_SECONDS)
    return {
        "base": _name(base),
        "distance": distance,
        "away_seconds": seconds,
        "away": away,
        "over": over,
        "distance_threshold": HOME_LEAVE_DISTANCE,
        "seconds_threshold": HOME_LEAVE_SECONDS,
    }


# ── 家的记忆（模组主动上报的带标签 `remembered_pois`）──

HOME_TAG = "home"
BED_TAG = "bed"
# 床没了 → 给模型的是**事实 + 一条可走的路子**（动不动手由模型自己判断，后端绝不自动调用）。
HOME_BED_MISSING_NOTE = ("床没了：家的位置还在，可以先 go_home 走到家，到了自己找一张床，"
                         "或 make_bed 做一张再说")


def poi_tags(poi: Any) -> set[str]:
    """一个记忆点的标签集合：新字段 `tags`（数组）∪ 向后兼容的 `category`（主标签）。

    模组新报的点两个都有（`category` 只是 `tags` 里的主标签）；旧数据 / 手写记忆只有
    `category`。小写归一、去空白；不是 dict 或一个标签都没有 → 空集合。
    """
    if not isinstance(poi, dict):
        return set()
    rows: list[str] = []
    tags = poi.get("tags")
    if isinstance(tags, (list, tuple, set)):
        rows.extend(str(item) for item in tags)
    rows.append(str(poi.get("category") or ""))
    return {item.strip().lower() for item in rows if item and item.strip()}


def home_memory_facts(pois: Any) -> dict:
    """从模组主动上报的带标签 `remembered_pois` 里读出「家 / 床」的**事实**（只读，绝不动手）。

    - 带 `home` + `bed` ⇒ 家完好：`home_point` 是家的位置，`home_bed` = `"present"`。
    - 带 `home` 不带 `bed` ⇒ **床丢了**（模组按原版重生点 + 方块核对的结果，后端不重复判定）：
      `home_bed` = `"missing"` + `home_note`（先说清事实，再给一条可走的路子）。
    - 只带 `bed` 不带 `home` ⇒ **不是家**（旧数据 / 手写记忆照 bed 记着，绝不自动升级成 home）。
    - 模组从没报过带 `home` 的点 ⇒ `{}`（不知道就说不知道，不猜、不造）。

    多个带 `home` 的点：优先取**还带着 `bed` 的那个**（那才是当前的家）；都掉了床标签时取最先
    报的那个（正常只有一条 —— `home` 标签由模组跟着新床搬家）。
    """
    rows = [poi for poi in (pois if isinstance(pois, (list, tuple)) else [])
            if isinstance(poi, dict) and HOME_TAG in poi_tags(poi)]
    if not rows:
        return {}
    with_bed = [poi for poi in rows if BED_TAG in poi_tags(poi)]
    row = with_bed[0] if with_bed else rows[0]
    point = point_of(row)
    facts: dict = {}
    if point is not None:
        facts["home_point"] = {"x": point[0], "y": point[1], "z": point[2]}
    if with_bed:
        facts["home_bed"] = "present"
    else:
        facts["home_bed"] = "missing"
        facts["home_note"] = HOME_BED_MISSING_NOTE
    return facts


# ── 区域回灌（Java 侧 ZoneGuard 是内存态，重启即丢 → 后端必须先存再回灌）──


def zone_args(zone: Any) -> Optional[dict]:
    """区域 → `define_zone` 参数（键名与 Java `handleDefineZone` 对齐）。

    缺名字或缺形状（radius 与 box 都没有）→ None（本地先挡，别浪费一次注定失败的往返）。
    """
    if not isinstance(zone, dict):
        return None
    name = _name(zone)
    center = point_of(zone)
    if not name or center is None:
        return None
    radius = _num(zone.get("radius"))
    box = _box_of(zone)
    if radius is None and box is None:
        return None
    args: dict = {"name": name, "kind": str(zone.get("kind") or "")}
    args.update({"x": center[0], "y": center[1], "z": center[2]})
    if radius is not None:
        args["radius"] = radius
    if box is not None:
        args["box"] = box
    protect = normalize_protect(zone.get("protect"))
    if protect:
        args["protect"] = protect
    return args


def zones_match(a: Any, b: Any) -> bool:
    """两个区域是不是同一份（幂等的判据）：名字、种类、保护项与形状都对得上。"""
    if not isinstance(a, dict) or not isinstance(b, dict) or not _name(a):
        return False
    if _name(a) != _name(b) or str(a.get("kind") or "") != str(b.get("kind") or ""):
        return False
    if normalize_protect(a.get("protect")) != normalize_protect(b.get("protect")):
        return False
    left, right = point_of(a), point_of(b)
    if left is None or right is None or left != right:
        return False
    if _box_of(a) != _box_of(b):
        return False
    return _num(a.get("radius")) == _num(b.get("radius"))


def plan_zone_replay(saved_zones: Any, mod_zones: Any) -> dict:
    """决定这一轮要推哪些区域回 Java（纯函数，不看盘、不发帧）。

    返回 `{zones, skipped, blocked, adopted}`：`zones` 要下发的参数列表、
    `skipped` 模组已有同名的、`blocked` 想推却推不了的（缺形状 / 模组不上报区域）、
    `adopted` 只在模组侧存在（Java 是硬拦真相，直接采纳落盘）。
    """
    report: dict = {"zones": [], "skipped": [], "blocked": [], "adopted": []}
    if mod_zones is None:
        if saved_zones:
            report["blocked"].append({"kind": "zone", "name": "", "reason": "mod_not_reporting"})
        return report
    known = {_name(row): row for row in mod_zones if isinstance(row, dict) and _name(row)}
    for row in saved_zones or []:
        if not isinstance(row, dict):
            continue
        name = _name(row)
        if not name:
            continue
        existing = known.pop(name, None)
        if existing is not None and zones_match(existing, row):
            report["skipped"].append({"kind": "zone", "name": name, "reason": "identical"})
            continue
        args = zone_args(row)
        if args is None:
            report["blocked"].append({"kind": "zone", "name": name, "reason": "no_shape"})
            continue
        report["zones"].append(args)
    for name, row in known.items():
        report["adopted"].append({"name": name, "zone": copy.deepcopy(row)})
    return report


# ── 造床（§8）：默认允许自己造，缺料 / 不安全就**如实报** ─────────


def _count_items(inventory: Any, suffix: str) -> int:
    total = 0
    for entry in inventory or []:
        if not isinstance(entry, dict):
            continue
        item = str(entry.get("name") or entry.get("item_id") or entry.get("item") or "").lower()
        if not item.endswith(suffix) and suffix not in item:
            continue
        amount = _as_int(entry.get("count"))
        total += amount if amount and amount > 0 else 1
    return total


def hostiles_in(entities: Any) -> list[str]:
    """附近的敌对生物（只按名字认；认不出就不当敌人，绝不乱报）。"""
    out: list[str] = []
    for entry in entities or []:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("type") or "").lower() not in ("", "mob", "monster", "hostile", "entity"):
            kind = str(entry.get("type") or "").lower()
            name = str(entry.get("name") or "").lower()
        else:
            kind = str(entry.get("name") or "").lower()
            name = kind
        probe = f"{kind} {name}"
        if any(word in probe for word in HOSTILE_KEYWORDS):
            label = str(entry.get("name") or entry.get("type") or "?")
            out.append(label)
    return out


def bed_attempt(inventory: Any, entities: Any = None) -> dict:
    """找床失败后"要不要/能不能自己造床"的判定（纯函数）。

    * 周围有敌对生物 → `unsafe`（先处理；处理不了就放弃，不硬凑）；
    * 缺羊毛/木板 → `missing`（带还差多少，如实说）；
    * 齐了 → `craft`。
    """
    hostiles = hostiles_in(entities)
    if hostiles:
        return {"action": "unsafe", "hostiles": hostiles[:6], "missing": {}}
    wool = _count_items(inventory, "wool")
    planks = _count_items(inventory, "planks")
    missing: dict[str, int] = {}
    if wool < BED_WOOL:
        missing["wool"] = BED_WOOL - wool
    if planks < BED_PLANKS:
        missing["planks"] = BED_PLANKS - planks
    if missing:
        return {"action": "missing", "hostiles": [], "missing": missing}
    return {"action": "craft", "hostiles": [], "missing": {}}


def bed_note(decision: dict) -> str:
    """造床判定的**如实**说法（缺料/不安全都把原因说清楚，不硬凑）。"""
    action = str((decision or {}).get("action") or "")
    if action == "unsafe":
        who = "、".join(str(item) for item in (decision.get("hostiles") or [])) or "敌人"
        return f"（周围有敌对生物（{who}），先处理掉；处理不了就先不造床了）"
    if action == "missing":
        missing = decision.get("missing") or {}
        parts = []
        if missing.get("wool"):
            parts.append(f"羊毛 {missing['wool']} 个")
        if missing.get("planks"):
            parts.append(f"木板 {missing['planks']} 块")
        return "（找不到床，想自己造一张，但材料不够：还缺 " + "、".join(parts) + "）"
    return "（附近没有床，材料够，我造一张放床边认领）"


# ── 落盘（world 层分区，一个分区一个 `.json` + 一条审计流）──────


class TerritoryStore:
    """一个 **world 层** 分区下的领地 / 基地 / 容器 / 区域（读写线程安全）。

    分区键 `world_scope(server, world_name, dimension)`：坐标只在同一世界同一维度里有效，
    换服 / 换世界 / 换维度都换文件（绝不串号）。
    """

    def __init__(self, root: str | Path = DEFAULT_TERRITORY_ROOT,
                 server: Any = "", world_name: Any = "", dimension: Any = "",
                 clock=time.time):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._clock = clock
        self._lock = threading.RLock()
        self._namespace = world_scope(server, world_name, dimension)
        self._data: dict = self._blank()
        self._load()

    @staticmethod
    def _blank() -> dict:
        return {"version": 1, "territories": {}, "bases": {}, "containers": {}, "zones": {}}

    # ── 分区 ──

    def use_namespace(self, server: Any, world_name: Any, dimension: Any = "") -> str:
        """切换到指定 world 层分区并按盘重建（同一分区重复调用不重读）。"""
        with self._lock:
            key = world_scope(server, world_name, dimension)
            if key == self._namespace:
                return key
            self._namespace = key
            self._load()
            return key

    @property
    def namespace(self) -> str:
        return self._namespace

    @property
    def path(self) -> Path:
        return self.root / f"{self._namespace or UNKNOWN}.json"

    @property
    def audit_path(self) -> Path:
        return self.root / f"{self._namespace or UNKNOWN}.audit.jsonl"

    # ── 读 ──

    def territories(self) -> list[dict]:
        with self._lock:
            return [copy.deepcopy(row) for row in self._data["territories"].values()]

    def bases(self) -> list[dict]:
        with self._lock:
            return [copy.deepcopy(row) for row in self._data["bases"].values()]

    def containers(self) -> list[dict]:
        with self._lock:
            return [copy.deepcopy(row) for row in self._data["containers"].values()]

    def zones(self) -> list[dict]:
        with self._lock:
            return [copy.deepcopy(row) for row in self._data["zones"].values()]

    def territory(self, name: Any) -> Optional[dict]:
        return self._get("territories", name)

    def base(self, name: Any) -> Optional[dict]:
        return self._get("bases", name)

    def container(self, name: Any) -> Optional[dict]:
        return self._get("containers", name)

    def zone(self, name: Any) -> Optional[dict]:
        return self._get("zones", name)

    def _get(self, bucket: str, name: Any) -> Optional[dict]:
        key = str(name or "").strip()
        with self._lock:
            row = self._data[bucket].get(key)
            return copy.deepcopy(row) if row is not None else None

    def container_by_position(self, x: Any, y: Any, z: Any, tolerance: float = 1.5) -> Optional[dict]:
        """按坐标找容器（"我的箱子在这"）——容差 1.5 格，认不出就说没有，不猜。"""
        point = (_num(x), _num(y), _num(z))
        if any(item is None for item in point):
            return None
        where = (float(point[0]), float(point[1]), float(point[2]))
        best: Optional[tuple[float, str, dict]] = None
        with self._lock:
            rows = list(self._data["containers"].values())
        for row in rows:
            pos = point_of(row)
            if pos is None:
                continue
            distance = _distance(pos, where)
            if distance > tolerance:
                continue
            candidate = (distance, _name(row), row)
            if best is None or candidate[:2] < best[:2]:
                best = candidate
        return copy.deepcopy(best[2]) if best is not None else None

    # ── 吸收（Java 上报的区域 → 盘）──

    def absorb_state(self, state: Any) -> dict:
        """把一次 `state_update` 里的 `zones[]` 吸收进当前分区（同名覆盖，永不删除）。

        Java 的 `ZoneGuard` 是**内存态**，重启即丢 —— 所以后端才是这份声明的持久真相，
        进世界时按 `plan_zone_replay` 推回去（幂等）。这里只做 upsert。
        """
        if not isinstance(state, dict):
            return {"zones": 0}
        rows = state.get("zones")
        if not isinstance(rows, (list, tuple)):
            return {"zones": 0}
        changed = 0
        with self._lock:
            for row in rows:
                if not isinstance(row, dict):
                    continue
                name = _name(row)
                if not name:
                    continue
                stored = copy.deepcopy(row)
                stored["name"] = name
                stored["protect"] = normalize_protect(row.get("protect"))
                if self._data["zones"].get(name) == stored:
                    continue
                self._data["zones"][name] = stored
                changed += 1
            if changed:
                self._save()
        return {"zones": changed}

    # ── 写：领地 / 基地 / 容器 / 区域 ──

    def set_territory(self, name: Any, *, kind: str = "", x: Any = None, y: Any = None,
                      z: Any = None, radius: Any = None, box: Any = None, grants: Any = None,
                      granted_by: Any = None, now: Optional[float] = None) -> Optional[dict]:
        """落一块领地（同名覆盖）。缺名字或完全没有形状 → None（不落半条）。"""
        clean = str(name or "").strip()
        point = (_num(x), _num(y), _num(z))
        if any(item is None for item in point):
            point = None
        radius_value = _num(radius)
        box_value = _box_of({"box": box}) if isinstance(box, dict) else None
        if not clean or (point is None and box_value is None):
            return None
        if radius_value is None and box_value is None:
            return None
        row: dict = {
            "name": clean,
            "kind": str(kind or "").strip().lower(),
            "grants": normalize_grants(grants),
            "granted_by": _granted_by(granted_by),
            "since": float(now if now is not None else self._clock()),
        }
        if point is not None:
            row["center"] = {"x": point[0], "y": point[1], "z": point[2]}
        if radius_value is not None:
            row["radius"] = radius_value
        if box_value is not None:
            row["box"] = box_value
        with self._lock:
            self._data["territories"][clean] = row
            self._save()
        self.audit("grant_territory", target=clean, granted_by=row["granted_by"],
                   details={"granted": row["grants"], "kind": row["kind"]})
        logger.info("[Territory] 领地 '%s' 授权 %s", clean, row["grants"])
        return copy.deepcopy(row)

    def revoke_territory(self, name: Any) -> Optional[dict]:
        """收回一块领地（立即生效 —— 没有缓存，下一帧就查不到）。"""
        return self._revoke("territories", name, "revoke_territory")

    def set_base(self, name: Any, *, kind: str = "base", anchor: Any = None,
                 radius: Any = 32.0, granted_by: Any = None,
                 now: Optional[float] = None) -> Optional[dict]:
        """落一个基地（同名覆盖，保留已认下的床位/成员清单）。"""
        clean = str(name or "").strip()
        point = point_of(anchor if isinstance(anchor, dict) else None)
        radius_value = _num(radius)
        if not clean or point is None or radius_value is None or radius_value <= 0:
            return None
        kind_value = str(kind or "base").strip().lower() or "base"
        if kind_value not in BASE_KINDS:
            kind_value = "base"
        with self._lock:
            existing = self._data["bases"].get(clean) or {}
            row = {
                "name": clean,
                "kind": kind_value,
                "anchor": {"x": point[0], "y": point[1], "z": point[2]},
                "radius": radius_value,
                "home_bed": copy.deepcopy(existing.get("home_bed")),
                "zones": list(existing.get("zones") or []),
                "containers": list(existing.get("containers") or []),
                "granted_by": _granted_by(granted_by) or existing.get("granted_by"),
                "since": float(existing.get("since") or (now if now is not None else self._clock())),
            }
            self._data["bases"][clean] = row
            self._save()
        self.audit("set_base", target=clean, granted_by=row["granted_by"],
                   details={"kind": kind_value, "radius": radius_value})
        logger.info("[Territory] 基地 '%s'（%s，半径 %.0f）", clean, kind_value, radius_value)
        return copy.deepcopy(row)

    def del_base(self, name: Any) -> Optional[dict]:
        return self._revoke("bases", name, "del_base")

    def set_home_bed(self, base_name: Any, position: Any) -> Optional[dict]:
        """把一张床认成该基地的 `home_bed`（床位只在 world 层）。"""
        point = point_of(position if isinstance(position, dict) else None)
        if point is None:
            return None
        with self._lock:
            row = self._data["bases"].get(str(base_name or "").strip())
            if row is None:
                return None
            row["home_bed"] = {"x": point[0], "y": point[1], "z": point[2]}
            self._save()
            out = copy.deepcopy(row)
        self.audit("set_home_bed", target=str(base_name), details={"bed": row["home_bed"]})
        return out

    def remember_container(self, name: Any, *, x: Any, y: Any, z: Any, kind: str = "chest",
                           policy: str = POLICY_BORROW_ONLY, owner: str = "",
                           granted_by: Any = None,
                           now: Optional[float] = None) -> Optional[dict]:
        """**记住一个箱子的确切位置** + 访问策略（同名覆盖）。"""
        clean = str(name or "").strip()
        point = (_num(x), _num(y), _num(z))
        if not clean or any(item is None for item in point):
            return None
        row = {
            "name": clean,
            "kind": str(kind or "chest").strip().lower() or "chest",
            "x": float(point[0]), "y": float(point[1]), "z": float(point[2]),
            "policy": normalize_policy(policy),
            "owner": str(owner or "").strip(),
            "granted_by": _granted_by(granted_by),
            "since": float(now if now is not None else self._clock()),
        }
        with self._lock:
            self._data["containers"][clean] = row
            self._save()
        self.audit("remember_container", target=clean, granted_by=row["granted_by"],
                   details={"policy": row["policy"], "owner": row["owner"],
                            "pos": [row["x"], row["y"], row["z"]]})
        logger.info("[Territory] 容器 '%s' 记下 %s（策略 %s）", clean, row["policy"],
                    (row["x"], row["y"], row["z"]))
        return copy.deepcopy(row)

    def set_container_policy(self, name: Any, policy: Any, *, granted_by: Any = None) -> Optional[dict]:
        """改一个已知容器的策略（撤销/降级也走它）。"""
        clean = str(name or "").strip()
        with self._lock:
            row = self._data["containers"].get(clean)
            if row is None:
                return None
            row["policy"] = normalize_policy(policy, default=POLICY_NO_TOUCH)
            row["granted_by"] = _granted_by(granted_by) or row.get("granted_by")
            self._save()
            out = copy.deepcopy(row)
        self.audit("grant_container", target=clean, granted_by=row["granted_by"],
                   details={"policy": row["policy"]})
        return out

    def revoke_container(self, name: Any) -> Optional[dict]:
        """收回箱子的访问授权：**立刻生效**（条目从表里消失，查不到就是不许动）。"""
        return self._revoke("containers", name, "revoke_container")

    def set_zone(self, zone: Any, *, granted_by: Any = None) -> Optional[dict]:
        """落一条区域声明（Java 接受后回灌用它；同名覆盖）。"""
        if not isinstance(zone, dict):
            return None
        name = _name(zone)
        args = zone_args(zone)
        if not name or args is None:
            return None
        row = copy.deepcopy(zone)
        row["name"] = name
        row["protect"] = normalize_protect(zone.get("protect"))
        row["granted_by"] = _granted_by(granted_by) or row.get("granted_by")
        with self._lock:
            self._data["zones"][name] = row
            self._save()
        self.audit("define_zone", target=name, granted_by=row.get("granted_by"),
                   details={"kind": row.get("kind"), "protect": row["protect"]})
        return copy.deepcopy(row)

    def clear_zone(self, name: Any) -> Optional[dict]:
        return self._revoke("zones", name, "clear_zone")

    def _revoke(self, bucket: str, name: Any, action: str) -> Optional[dict]:
        clean = str(name or "").strip()
        with self._lock:
            row = self._data[bucket].pop(clean, None)
            if row is None:
                return None
            self._save()
        self.audit(action, target=clean, details={"kind": row.get("kind")})
        logger.info("[Territory] %s '%s'", action, clean)
        return copy.deepcopy(row)

    # ── 审计 ──

    def audit(self, action: str, *, target: str = "", actor: str = "", tier: str = "",
              granted_by: Any = None, details: Any = None) -> dict:
        """每次授予 / 撤销 / 记位置都留痕（"这箱子怎么没了"必须能查）。"""
        giver = _granted_by(granted_by) if granted_by else {}
        row = {
            "ts": float(self._clock()),
            "namespace": self._namespace,
            "action": str(action),
            "target": str(target),
            "actor": str(actor or giver.get("name") or ""),
            "tier": str(tier or giver.get("tier") or ""),
            "details": copy.deepcopy(details or {}),
        }
        try:
            self.audit_path.parent.mkdir(parents=True, exist_ok=True)
            with self.audit_path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
        except Exception as exc:  # noqa: BLE001 —— 审计写不动不能把业务带崩
            logger.warning("[Territory] 审计写盘失败 %s: %s", self.audit_path, exc)
        return row

    def audit_log(self, n: int = 20) -> list[dict]:
        if not self.audit_path.exists():
            return []
        try:
            lines = self.audit_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        out: list[dict] = []
        for line in reversed(lines):
            try:
                row = json.loads(line)
            except (TypeError, ValueError):
                continue
            if isinstance(row, dict):
                out.append(row)
            if len(out) >= max(1, int(n)):
                break
        return out

    # ── 盘 ──

    def snapshot(self) -> dict:
        """当前分区的全量快照（`/api/status` 与 prompt 用）。"""
        with self._lock:
            return {
                "namespace": self._namespace,
                "territories": self.territories(),
                "bases": self.bases(),
                "containers": self.containers(),
                "zones": self.zones(),
            }

    def _load(self) -> None:
        data = self._blank()
        path = self.path
        if path.exists():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
            except Exception as exc:
                logger.warning("[Territory] 读取失败，已回退空表 %s: %s", path, exc)
                loaded = None
            if isinstance(loaded, dict):
                for bucket in ("territories", "bases", "containers", "zones"):
                    rows = loaded.get(bucket)
                    if isinstance(rows, list):
                        for row in rows:
                            if isinstance(row, dict) and _name(row):
                                data[bucket][_name(row)] = copy.deepcopy(row)
        self._data = data

    def _save(self) -> None:
        payload = {
            "version": 1,
            "namespace": self._namespace,
            "saved_at": float(self._clock()),
            **{bucket: list(self._data[bucket].values())
               for bucket in ("territories", "bases", "containers", "zones")},
        }
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        try:
            temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                                 encoding="utf-8")
            temporary.replace(self.path)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[Territory] 写盘失败 %s: %s", self.path, exc)


def _granted_by(value: Any) -> dict:
    """授予人归一化：`{tier,name}` 或 `"名字"`。"""
    if isinstance(value, dict):
        return {"tier": str(value.get("tier") or ""), "name": str(value.get("name") or "")}
    text = str(value or "").strip()
    return {"tier": "", "name": text} if text else {}
