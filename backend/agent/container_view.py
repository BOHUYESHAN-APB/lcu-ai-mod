"""view_container —— 「只读看一眼容器里有什么」（对照 mindcraft `!viewChest`，`actions.js:234`）。

用户口径（硬）：
  - **只读**：列出内容 + 关掉界面，**不拿任何东西**（取用是 `container_move`/`borrow_item`/
    `haul` 的事）；
  - 数据只来自**已有快照**（Java state 的 `open_container`：`ContainerController.describeState`，
    含 `title/slots/player_inv_start/items[{slot,item,count,name}]`）——没开着的容器就**如实说
    不知道里面有什么**，绝不猜；
  - 它是 `haul`「取多少 / 搬什么」**之前那一步**：先看清有几件，再决定取多少、搬什么。

容器策略（在 planner 的**统一授权点**判，本模块只负责"看清楚是什么策略"的话术）：
  `no_touch` ⇒ 直接拒（连看都不看）；`borrow_only` ⇒ **只读可以**；`free` ⇒ 可以；
  没登记的箱子 ⇒ 仍要翻箱授权（不去开别人没让我看的箱子）。
"""

from __future__ import annotations

import logging
from typing import Any

from . import where
from .borrow import container_entries, player_inv_start

logger = logging.getLogger("container_view")

SOURCE_OPEN_CONTAINER = "state.open_container"

DEFAULT_LIMIT = 32
MAX_LIMIT = 64

# 失败 / 拒绝的 code（逐字进如实说明）。
CODE_NO_CONTAINER = "no_container"
CODE_OTHER_CONTAINER = "other_container"
CODE_NO_LAYOUT = "no_layout"
CODE_NO_TOUCH = "no_touch"
CODE_UNAUTHORIZED = "unauthorized"
CODE_BUSY = "busy"


def is_open(open_container: Any) -> bool:
    """快照看起来是"有容器开着"吗（Java 没开时给空 `{}`）。"""
    if not isinstance(open_container, dict) or not open_container:
        return False
    if open_container.get("id") is not None:
        return True
    return bool(str(open_container.get("title") or "").strip())


def title_of(open_container: Any) -> str:
    """容器的显示名（`title` / `name`；没有就给空串，不编）。"""
    if not isinstance(open_container, dict):
        return ""
    return str(open_container.get("title") or open_container.get("name") or "").strip()


def matches(open_container: Any, target: Any) -> bool:
    """模型说的容器名是不是**现在开着的这个**（标题/名字/类别，模糊匹配）。

    名字对不上就是"开着的不是它" —— 如实说，绝不假装看的是那个箱子。
    """
    wanted = str(target or "").strip()
    if not wanted:
        return True
    if not isinstance(open_container, dict):
        return False
    for key in ("title", "name", "kind"):
        value = str(open_container.get(key) or "").strip()
        if value and where.name_matches(wanted, value):
            return True
    return False


def _limit(value: Any) -> int:
    num = where._num(value)
    if num is None:
        return DEFAULT_LIMIT
    return max(1, min(int(num), MAX_LIMIT))


def view(open_container: Any, *, limit: Any = DEFAULT_LIMIT) -> dict:
    """把 `open_container` 快照折成内容清单（纯函数，**不发帧、不拿东西**）。

    返回 `{"title", "kind", "items":[{"name","count","slot"}], "empty", "truncated",
    "total", "known", "note"}`：
      - `items` 只含**容器侧**（槽位 < `player_inv_start`）的非空槽，按槽位号排列；
      - `empty=True` 只在"确实开着且容器侧一格都没有"时给 —— 没开 / 分不清槽位一侧时
        `known=False`，此时 `empty` 一律 `False`（**不把不知道说成空**）；
      - `truncated=True` 表示非空槽超过 `limit`，只列了前 `limit` 格。
    """
    data = open_container if isinstance(open_container, dict) else {}
    title = title_of(data)
    result: dict = {"title": title, "kind": str(data.get("kind") or "").strip(),
                    "items": [], "empty": False, "truncated": False, "total": 0,
                    "known": False, "code": "", "source": SOURCE_OPEN_CONTAINER}
    if not is_open(data):
        result["code"] = CODE_NO_CONTAINER
        result["note"] = describe(result)
        return result
    start = player_inv_start(data)
    if start <= 0:
        # 快照没给槽位布局：分不清哪半是箱子、哪半是背包 —— 如实说分不清，不硬念。
        result["code"] = CODE_NO_LAYOUT
        result["note"] = describe(result)
        return result
    rows = [row for row in container_entries(data) if int(row["slot"]) < start]
    rows.sort(key=lambda row: int(row["slot"]))
    cap = _limit(limit)
    result["known"] = True
    result["total"] = len(rows)
    result["items"] = [{"name": str(row["item"] or ""), "count": int(row["count"]),
                        "slot": int(row["slot"])} for row in rows[:cap]]
    result["empty"] = not rows
    result["truncated"] = len(rows) > cap
    result["note"] = describe(result)
    return result


def _label(result: dict) -> str:
    title = str(result.get("title") or "").strip()
    return f"「{title}」" if title else "这个容器"


def describe(result: Any) -> str:
    """把清单折成一句**如实**的说明（有什么说什么，空就说空，不知道就说不知道）。"""
    data = result if isinstance(result, dict) else {}
    label = _label(data)
    if not data.get("known"):
        code = str(data.get("code") or "")
        if code == CODE_NO_CONTAINER or not is_open(data):
            return (f"（view_container：code={CODE_NO_CONTAINER} —— 现在**没有开着的容器**，"
                    f"我不知道里面有什么，也不猜；先 go_to_block 过去、用 use_on 右键打开，"
                    f"再叫我一次）")
        return (f"（view_container：code={CODE_NO_LAYOUT} —— 快照没给槽位布局"
                f"（分不清哪半是箱子、哪半是我的背包），清单我不敢乱念）")
    items = [row for row in (data.get("items") or []) if isinstance(row, dict)]
    if data.get("empty"):
        return (f"（view_container：{label}是**空的**（容器侧一格都没有）；"
                f"看完我把界面关了，没拿任何东西）")
    parts = [f"{row.get('name')} x{row.get('count')}（槽 {row.get('slot')}）" for row in items]
    note = (f"（view_container：{label}里 {data.get('total')} 格非空 —— " + "；".join(parts) + "）")
    if data.get("truncated"):
        note += f"（还有更多，只列了前 {len(items)} 格）"
    note += "（这是只读一眼：**什么都没拿**，看完我把它关了；要取就 container_move/borrow_item，要搬就 haul）"
    return note


def denied_note(code: str, *, container: Any = None, tier: str = "") -> str:
    """拒绝的如实说明（带 code）。判定在 planner 的统一授权点，话术在这里，只有一份。"""
    if code == CODE_NO_TOUCH:
        name = str((container or {}).get("name") or "这个箱子")
        return (f"（view_container：code={CODE_NO_TOUCH} ——「{name}」是 no_touch 的："
                f"什么都不许动，我连看都没看）")
    if code == CODE_UNAUTHORIZED:
        who = tier or "当前身份"
        return (f"（view_container：code={CODE_UNAUTHORIZED} —— {who} 没被允许让我翻箱子，"
                f"所以没看（这个箱子也没登记过））")
    if code == CODE_BUSY:
        return (f"（view_container：code={CODE_BUSY} —— 正在搬运（haul）用着这个容器界面，"
                f"先等它搬完）")
    return f"（view_container：code={code} —— 没看）"


def other_container_note(target: Any, open_container: Any) -> str:
    """开着的不是模型说的那个箱子 —— 如实说，不拿它冒充。"""
    return (f"（view_container：code={CODE_OTHER_CONTAINER} —— 现在开着的是"
            f"{_label({'title': title_of(open_container)})}，不是你说的「{target}」；"
            f"先过去用 use_on 打开它，再叫我）")
