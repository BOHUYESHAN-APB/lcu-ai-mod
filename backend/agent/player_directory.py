"""玩家名解析 —— tp 这类"必须指名道姓"的指令的目标解析。

用户口径：**不猜**。同名/近似名在多人在线列表里同时存在时，宁可拒绝并把候选
报回去，也不能随机挑一个 —— 挑错的代价是把 AI（或别人）传送到错误的人身边，
甚至把指令下到不该下的人身上。

规则（顺序即优先级）：

1. 空名 → 拒（`empty`）。
2. 精确匹配（大小写不敏感）→ 直接命中（`exact`）。
3. 否则按"相似"筛：子串命中（前缀/包含）或编辑距离相似度 ≥ 阈值。
   - 恰好 1 个 → 命中（`fuzzy`）；
   - 多个 → **拒并回候选**（`ambiguous`）；
   - 0 个 → 拒（`not_found`，不在线/不在本服）。

候选来源是"当前在世界的玩家列表"（模组上报的 `players`），所以不在本服的人天然
解析不出来 —— 这正是要的：不在线/不在本服 → 拒。
"""

from __future__ import annotations

from difflib import SequenceMatcher
from typing import Any, Iterable

# 纯编辑距离相似度的下限（子串命中另有更高的固定分，不走这里）。
FUZZY_RATIO_THRESHOLD = 0.72

_SUBSTRING_SCORE = 0.9
_EXACT_SCORE = 1.0


def _rows(players: Any) -> list[dict]:
    """把任意形态的玩家列表折成 `[{"name", "uuid"}]`（大小写不敏感去重、保序）。"""
    out: list[dict] = []
    seen: set[str] = set()
    if not isinstance(players, (list, tuple, set)):
        return out
    for item in players:
        if isinstance(item, dict):
            name = str(item.get("name") or "").strip()
            uuid = str(item.get("uuid") or item.get("player_uuid") or "").strip()
        elif isinstance(item, str):
            name, uuid = item.strip(), ""
        else:
            continue
        if not name:
            continue
        key = name.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append({"name": name, "uuid": uuid})
    return out


def similarity(query: str, name: str) -> float:
    """名字相似度：精确 1.0；子串 0.9；其余走编辑距离。"""
    left, right = str(query or "").casefold(), str(name or "").casefold()
    if not left or not right:
        return 0.0
    if left == right:
        return _EXACT_SCORE
    if left in right or right in left:
        return _SUBSTRING_SCORE
    return SequenceMatcher(None, left, right).ratio()


def rank_candidates(query: str, players: Any) -> list[tuple[float, dict]]:
    """按相似度给候选排序（只留过阈值的；同分按名字稳定排序）。"""
    scored: list[tuple[float, dict]] = []
    for row in _rows(players):
        score = similarity(query, row["name"])
        if score >= FUZZY_RATIO_THRESHOLD or score >= _SUBSTRING_SCORE:
            scored.append((score, row))
    scored.sort(key=lambda pair: (-pair[0], pair[1]["name"].casefold()))
    return scored


def resolve_player(name: Any, players: Any) -> dict:
    """解析玩家名。

    返回 `{"ok": True, "name", "uuid", "match": "exact"|"fuzzy"}` 或
    `{"ok": False, "reason": "empty"|"not_found"|"ambiguous", "candidates": [名字…]}`。
    """
    query = str(name or "").strip()
    if not query:
        return {"ok": False, "reason": "empty", "candidates": []}

    rows = _rows(players)
    key = query.casefold()
    for row in rows:
        if row["name"].casefold() == key:
            return {"ok": True, "name": row["name"], "uuid": row["uuid"], "match": "exact"}

    scored = rank_candidates(query, rows)
    if not scored:
        return {"ok": False, "reason": "not_found", "candidates": []}
    top = scored[0][0]
    best = [row for score, row in scored if score >= top - 1e-9]
    if len(best) > 1:
        return {"ok": False, "reason": "ambiguous", "candidates": [row["name"] for row in best]}
    return {"ok": True, "name": best[0]["name"], "uuid": best[0]["uuid"], "match": "fuzzy"}


def resolve_targets(names: Any, players: Iterable[Any]) -> list[dict]:
    """批量解析（tp 有两个目标时用）：逐个走 `resolve_player`，保持顺序。"""
    if isinstance(names, str):
        names = [names]
    return [resolve_player(name, players) for name in (names or [])]
