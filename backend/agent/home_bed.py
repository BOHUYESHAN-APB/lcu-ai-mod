"""把一张床**绑成家**：走过去 → 右键一次 → 用原版重生点核对。

用户纠正的语义（2026-10-12）：
    "最少要在这张床上右键点击一次，无论是什么时候 —— 哪怕不是晚上/不是雷暴天气，右键这张床
     它都会**优先设置重生点**，只有到正确的时间段右键才会顺便睡觉。"

所以"把床位记下来"（`set_home` 原来只做这件事）**不等于**"家绑上了"：不右键，死了还是回世界
出生点。这个模块就是那条补上的链路，并且**诚实**：右键过、核对过，才算成功；核对不上就如实报
失败原因，绝不假装绑上了。

设计上刻意保持**零依赖 + 纯函数**（不 import 后端其它模块、不碰网络）：
`start()` 建账、`step()` 推进并返回"要下发哪个原语"，副作用（发帧）留给调用方 ——
这样 `verifyWalk`/单测可以直接对状态机做行为断言。
"""

from __future__ import annotations

from typing import Any, Optional

STAGE_WALK = "walk"
STAGE_CLICK = "click"
STAGE_VERIFY = "verify"
STAGE_DONE = "done"
STAGE_FAILED = "failed"

#: 认为"已经走到床边"的水平距离（原版右键够得着的范围比这宽松，留点余量）。
REACH = 3.0
#: 最多右键几次（第一次没绑上，可能被别的床占着；再试一次，还不行就如实报）。
MAX_CLICKS = 2
#: 整个链路的轮数上限（每轮一次决策；40 轮足够走一段路 + 点两下）。
MAX_ROUNDS = 40


def start(point: Any, *, base: str = "") -> dict:
    """建立一次"绑定床位"的账。坐标用**当前这一轮**读到的值，不吃历史值。"""
    x, y, z = float(point[0]), float(point[1]), float(point[2])
    return {
        "stage": STAGE_WALK,
        "x": x,
        "y": y,
        "z": z,
        "base": str(base or ""),
        "clicks": 0,
        "rounds": 0,
        "walked": False,
        "note": "",
        "ok": None,
    }


def _numbers(context: Any) -> tuple[float, float, float] | None:
    player = (context or {}).get("player") if isinstance(context, dict) else None
    if not isinstance(player, dict):
        return None
    try:
        return float(player["x"]), float(player["y"]), float(player["z"])
    except (KeyError, TypeError, ValueError):
        return None


def _horizontal_distance(context: Any, x: float, z: float) -> Optional[float]:
    here = _numbers(context)
    if here is None:
        return None
    dx = here[0] - x
    dz = here[2] - z
    return (dx * dx + dz * dz) ** 0.5


def _same_point(point: Any, x: float, y: float, z: float, tolerance: float = 1.5) -> bool:
    if not isinstance(point, dict):
        return False
    try:
        px, py, pz = float(point["x"]), float(point["y"]), float(point["z"])
    except (KeyError, TypeError, ValueError):
        return False
    return abs(px - x) <= tolerance and abs(py - y) <= tolerance + 1.0 and abs(pz - z) <= tolerance


def describe(state: Any) -> str:
    """给人看的一行（不编：没账目就说没有）。"""
    if not isinstance(state, dict):
        return ""
    stage = str(state.get("stage") or "")
    if stage == STAGE_DONE:
        return "（家绑好了）"
    if stage == STAGE_FAILED:
        return str(state.get("note") or "（绑定床位失败）")
    return str(state.get("note") or "（正在绑定床位）")


def step(state: Any, context: Any) -> dict:
    """推进一轮。

    返回 `{"action", "done", "ok", "note"}`：
    - `action == "move_to"` ⇒ 调用方下发 `skills.move_to(x, y, z)`；
    - `action == "use_on"`  ⇒ 调用方先 `skills.look_at(x, y+0.5, z)` 再 `skills.use_on()`（右键一次）；
    - `action == ""`        ⇒ 这一轮只等状态（在走路 / 等新状态回来核对）；
    - `done=True` 时 `ok` 才是最终结论（True 绑好了 / False 如实失败）。
    """
    if not isinstance(state, dict):
        return {"action": "", "done": True, "ok": False, "note": "（没有待绑的床位）"}

    state["rounds"] = int(state.get("rounds") or 0) + 1
    state["stage"] = str(state.get("stage") or STAGE_WALK)
    if state["rounds"] > MAX_ROUNDS:
        state["stage"] = STAGE_FAILED
        state["ok"] = False
        state["note"] = "（绑定床位超时：限定轮数内没走到/没绑上，如实收尾）"
        return {"action": "", "done": True, "ok": False, "note": state["note"]}

    x, y, z = float(state["x"]), float(state["y"]), float(state["z"])

    # 先看原版怎么说：重生点就是这张床 ⇒ 真的绑上了（唯一可信的成功判据）。
    bound_here = bool((context or {}).get("home_bed_bound")) and _same_point(
        (context or {}).get("home_bed"), x, y, z)
    if bound_here:
        state["stage"] = STAGE_DONE
        state["ok"] = True
        state["note"] = f"（家绑好了：重生点就是这张床 ({x:.0f},{y:.0f},{z:.0f})）"
        return {"action": "", "done": True, "ok": True, "note": state["note"]}

    if state["stage"] == STAGE_WALK:
        distance = _horizontal_distance(context, x, z)
        if distance is not None and distance <= REACH:
            state["stage"] = STAGE_CLICK
        elif state["walked"]:
            state["note"] = "（正在走过去）"
            return {"action": "", "done": False, "ok": None, "note": state["note"]}
        else:
            # 只下发一次 move_to：每轮重下会把寻路进度清零（"有路却一步不动"的老毛病）。
            state["walked"] = True
            state["note"] = "（走过去，站到床边）"
            return {"action": "move_to", "done": False, "ok": None, "note": state["note"]}

    if state["stage"] == STAGE_CLICK:
        if int(state.get("clicks") or 0) >= MAX_CLICKS:
            state["stage"] = STAGE_FAILED
            state["ok"] = False
            state["note"] = (f"（在床边右键了 {state['clicks']} 次，重生点仍然不是这张床 —— "
                             "可能被别的床/重生锚占着，或这张床不能用；如实报，不假装绑上了）")
            return {"action": "", "done": True, "ok": False, "note": state["note"]}
        state["clicks"] = int(state.get("clicks") or 0) + 1
        state["stage"] = STAGE_VERIFY
        state["note"] = ("（右键这张床：原版会**优先**把重生点绑到它，"
                         "哪怕现在不是睡觉的时间）")
        return {"action": "use_on", "done": False, "ok": None, "note": state["note"]}

    # STAGE_VERIFY：等下一份 state 回来复核（右键是包操作，重生点要下一轮才看得到）。
    state["stage"] = STAGE_WALK
    state["walked"] = True        # 已经在床边了，别再重新寻路
    state["note"] = "（右键过了，等一份新状态核对重生点）"
    return {"action": "", "done": False, "ok": None, "note": state["note"]}
