"""use_on —— 「对目标使用手里的物品」（对照 mindcraft `!useOn`，`commands/actions.js:492`）。

底层**不重写**：实体目标走 `skills.use_on_entity(id)`（Java 侧自带瞄准 + 通用右键），
坐标/方块目标走 `skills.look_at(x,y,z)` + `skills.use_on()`（`skills.py` 已有的原语，
Java 的 `use_on` 是**通用右键**：不猜方块还是实体、也不猜手上是什么，语义交给原版）。

用户口径（硬）：
  - **本模块只做判定**（能不能用、对什么用、手上对不对），发帧在 planner；
  - **不编**：目标只来自 state 快照（实体）或模型自己给的 x/y/z；手上拿什么只来自 state
    （`player.equipment.main_hand`）——读不到就**说读不到**，绝不假装核对过；
  - **占 `use` 资源**（声明在 `resource_lease._TOOL_RESOURCE_OVERRIDES`）：右键与 P0 反射
    的 use 是同一份资源，互斥；
  - 失败一律**带 code 如实说**：`no_target` / `bad_target` / `out_of_range` / `wrong_item`。

**不声明** `area` 门（区域硬保护）：模组的 `ZoneGuard` 只管 `ATTACK/MINE/PLACE`，并且
Java 侧在攻击那条路上**明说**"喂食/剪毛等交互不在此列"（`ActionExecutor.java:744-746`）——
所以这里**不硬加**一层与模组不一致的拒绝（要拦就改模组的规则表，不在这里各写一套）。
"""

from __future__ import annotations

import logging
from typing import Any, Iterable, Optional

from . import entities
from . import perception
from .borrow import normalize_item
from .craft import inventory_counts

logger = logging.getLogger("use_on")

MODE_ENTITY = "entity"        # 对实体用（Java 按实体号右键，自带瞄准）
MODE_POSITION = "position"    # 对坐标/方块用（先 look_at 再通用右键）

# 失败 code（逐字进如实说明，便于模型/测试按码认账）。
NO_TARGET = "no_target"
BAD_TARGET = "bad_target"
OUT_OF_RANGE = "out_of_range"
WRONG_ITEM = "wrong_item"

# 原版右键距离约 3 格；给一点余量，够不着就如实说够不着（模型自己 go_to 过去）。
REACH = 4.0


def held_item(context: Any) -> dict:
    """当前主手拿着什么（`player.equipment.main_hand`；读不到给空 `name`，不猜）。"""
    ctx = context if isinstance(context, dict) else {}
    player = ctx.get("player") if isinstance(ctx.get("player"), dict) else {}
    equipment = player.get("equipment") if isinstance(player.get("equipment"), dict) else {}
    hand = equipment.get("main_hand") if isinstance(equipment.get("main_hand"), dict) else {}
    name = normalize_item(hand.get("name"))
    if not name:
        return {"name": "", "display": "", "count": 0}
    count = hand.get("count")
    if not isinstance(count, (int, float)) or isinstance(count, bool):
        count = 0
    return {"name": name, "display": str(hand.get("display") or "").strip(),
            "count": int(count)}


def player_pos(context: Any) -> Optional[tuple[float, float, float]]:
    """玩家当前坐标（缺一个轴就不猜，返回 None）。"""
    ctx = context if isinstance(context, dict) else {}
    player = ctx.get("player") if isinstance(ctx.get("player"), dict) else {}
    try:
        if any(not isinstance(player.get(axis), (int, float)) or isinstance(player.get(axis), bool)
               for axis in ("x", "y", "z")):
            return None
        return (float(player["x"]), float(player["y"]), float(player["z"]))
    except (TypeError, ValueError):
        return None


def _point(payload: dict) -> Optional[tuple[float, float, float]]:
    """模型给的坐标目标（三轴齐全才算；缺一个就当没说）。"""
    values = []
    for axis in ("x", "y", "z"):
        value = payload.get(axis)
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return None
        values.append(float(value))
    return (values[0], values[1], values[2])


def _coords_text(point: tuple[float, float, float]) -> str:
    """坐标好读一点：整数不拖小数点（说明里用，不参与判定）。"""
    def fmt(value: float) -> str:
        rounded = round(float(value), 1)
        return f"{rounded:.0f}" if rounded == int(rounded) else f"{rounded:.1f}"
    return f"({fmt(point[0])},{fmt(point[1])},{fmt(point[2])})"


def _result(ok: bool, *, code: str = "", mode: str = "", target: Optional[dict] = None,
            item: str = "", held: Optional[dict] = None, reason: str = "") -> dict:
    return {"ok": bool(ok), "code": code, "mode": mode, "target": target or {},
            "item": item, "held": held or {"name": "", "display": "", "count": 0},
            "reason": reason}


def plan(payload: Any, *, context: Any = None,
         entities_rows: Optional[Iterable[Any]] = None) -> dict:
    """判定这次 `use_on` 能不能发（纯函数，**不发帧**）。

    `target`：实体名/类型（从 state 快照里挑最近的），或配合 `x,y,z` 指向一个坐标/方块。
    `item`  ：想让手里拿的那件（省＝就用现在手上的）。给了就**核对 state 里的主手**。

    返回 `{"ok","code","mode","target","item","held","reason"}`；`ok=False` 时 `code` 就是
    失败码（`no_target`/`bad_target`/`out_of_range`/`wrong_item`），调用方**一帧都不发**。
    """
    payload = payload if isinstance(payload, dict) else {}
    ctx = context if isinstance(context, dict) else {}
    target_name = str(payload.get("target") or payload.get("name")
                      or payload.get("entity") or "").strip()
    item = normalize_item(payload.get("item") or payload.get("tool") or "")
    held = held_item(ctx)

    point = _point(payload)
    chosen: Optional[dict]
    if point is not None:
        mode = MODE_POSITION
        chosen = {"kind": "", "name": target_name or _coords_text(point),
                  "x": point[0], "y": point[1], "z": point[2], "source": ""}
        distance = None
        own = player_pos(ctx)
        if own is not None:
            distance = round(((own[0] - point[0]) ** 2 + (own[1] - point[1]) ** 2
                              + (own[2] - point[2]) ** 2) ** 0.5, 1)
            chosen["distance"] = distance
    elif target_name:
        rows = (list(entities_rows) if entities_rows is not None else entities.gather(ctx))
        mode = MODE_ENTITY
        chosen = entities.nearest(target_name, rows, player_pos=player_pos(ctx))
        if chosen is None:
            return _result(False, code=NO_TARGET, mode=mode, item=item, held=held,
                           reason=target_name)
        distance = chosen.get("distance")
    else:
        return _result(False, code=BAD_TARGET, item=item, held=held,
                       reason="没说要对象：给实体名/类型，或 x,y,z")

    if distance is not None and float(distance) > REACH:
        return _result(False, code=OUT_OF_RANGE, mode=mode, target=chosen, item=item,
                       held=held, reason=str(distance))

    if item:
        if held.get("name") and held["name"] != item:
            return _result(False, code=WRONG_ITEM, mode=mode, target=chosen, item=item,
                           held=held, reason=held["name"])
        counts = inventory_counts(ctx.get("inventory"))
        if not held.get("name") and counts and item not in counts:
            # 背包清单读得到、里面又确实没有这件 —— 如实说（不去箱子里替它翻）。
            return _result(False, code=WRONG_ITEM, mode=mode, target=chosen, item=item,
                           held=held, reason="背包里没有")

    return _result(True, mode=mode, target=chosen, item=item, held=held)


def _where_text(target: dict) -> str:
    name = str(target.get("name") or "?")
    kind = str(target.get("kind") or "")
    label = f"{kind} {name}".strip() if kind and kind != name else name
    if target.get("source"):
        return f"{label}@({target.get('x')},{target.get('y')},{target.get('z')})"
    return label


def _held_text(held: dict, item: str) -> str:
    """说明里"手里拿的是哪件"：显示名优先（读不到才退回模型说的那件名，不编）。"""
    text = str(held.get("display") or "") or str(held.get("name") or "")
    if not text and item:
        text = item
    return f"«{text}»" if text else "**空手**"


def describe(result: Any) -> str:
    """把判定折成一句**如实**的说明（失败带 `code=`，成功说清对什么、拿什么）。"""
    data = result if isinstance(result, dict) else {}
    code = str(data.get("code") or "")
    target = data.get("target") if isinstance(data.get("target"), dict) else {}
    item = str(data.get("item") or "")
    held = data.get("held") if isinstance(data.get("held"), dict) else {}
    reason = str(data.get("reason") or "")
    if code == NO_TARGET:
        return (f"（use_on：code=no_target —— 我在**这一轮看得到的目标**里没找到「{reason}」，"
                f"**我没动手**。注意：默认快照是**分级摘要**，被动生物/普通方块**不带坐标**，"
                f"所以摘要里没有**不等于**附近没有 —— 要具体某一只就先用 "
                f"{perception.SCAN_ENTITY_TOOL}（只读、有界）扫一趟，扫回来的结果会并进候选；"
                f"或者你直接给坐标，我自己走过去用（约 3 格内）、不猜）")
    if code == BAD_TARGET:
        return f"（use_on：code=bad_target —— {reason}）"
    if code == OUT_OF_RANGE:
        return (f"（use_on：code=out_of_range —— 「{target.get('name') or '?'}」在 {reason} 格外，"
                f"够不着（右键约 3 格），**我没动手**；先 go_to_block/move_to 靠近再叫我）")
    if code == WRONG_ITEM:
        if reason == "背包里没有":
            return (f"（use_on：code=wrong_item —— 背包里没有 «{item}»，**我没动手**；"
                    f"先用 get_inventory 看看，缺料就先 craft_item/借）")
        return (f"（use_on：code=wrong_item —— 手上拿的是 «{reason}»，不是 «{item}»，"
                f"**我没动手**；先用 select_hotbar 换到 «{item}»，或把 item 改成你真正要用的那件）")
    if not data.get("ok"):
        return f"（use_on：{code or 'failed'} —— 没动手）"
    used = _held_text(held, item)
    target_text = _where_text(target) if target else "目标"
    note = f"（use_on：拿着 {used} 对 {target_text} 用了右键"
    if not held.get("name"):
        note += "（state 没报手持物品，我没法先核对）"
    note += "；成不成看原版判定与回执 —— 挤奶/剪羊毛/倒水/点火把都走这一下）"
    return note
