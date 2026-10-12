"""
Planner — 规划器。
参考 MaiBot 的 maisaka_chat.prompt 设计。

在 Timing Gate 判断应该回复后，Planner 决定具体做什么：
- reply(): 生成回复
- query_memory(): 查询记忆
- execute_action(): 执行游戏动作
- finish(): 结束本轮

支持可中断规划：新消息到达时可以中断当前规划。
"""

import copy
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from . import perception
from . import where
from .borrow import BorrowLedger
from .container_view import describe as container_view_note
from .container_view import (CODE_BUSY as CONTAINER_VIEW_BUSY, CODE_NO_TOUCH,
                             CODE_OTHER_CONTAINER, CODE_UNAUTHORIZED)
from .container_view import is_open as container_is_open
from .container_view import matches as container_matches
from .composites import (CODE_TOOL, COMPOSITE_BY_NAME, DEFAULT_FAILURE_CODE, GATE_CODES,
                         STATUS_DONE, CompositeRun, abort_run as abort_composite_run,
                         composite_note, expire_run, run_composite)
from .container_view import other_container_note as container_view_other_note
from .container_view import title_of as container_title
from .container_view import view as container_view_snapshot
from .container_view import denied_note as container_view_denied_note
from .control_lease import ControlLease
from .craft import (ContainerPick, armor_in_containers, clamp_take, describe_missing,
                    evaluate_craft, is_armor, normalize_item, route_containers)
from .entities import describe as entity_note
from .entities import find as find_entities
from .farm import (ACTION_HARVEST, ACTION_PLANT, ACTION_REPLANT, ACTION_TILL,
                   choose_action, counts_of, crop_of, hold_index, is_hoe, missing_items,
                   normalize_action)
from .farm import animal_kind as farm_animal_kind
from .farm import nearest as farm_nearest
from .farm import note as farm_note
from .farm import cycle_note as farm_cycle_note
from .farm import cycle_step as farm_cycle_step
from .farm import feed_note as farm_feed_note
from .farm import feed_targets as farm_feed_targets
from .farm import feed_food as farm_feed_food
from .farm import land_owner as farm_land_owner
from .farm import others_note as farm_others_note
from .farm import others_verdict as farm_others_verdict
from .job_registry import JOB_DONE, JOB_FAILED, JobRejected
from .haul import (HAUL_MAX_ATTEMPTS, HAUL_VERIFY_TIMEOUT, HAUL_WALK_TIMEOUT,
                   MODE_INSIDE, MODE_ITEM, MODE_WHOLE,
                   SIDE_CONTAINER, SIDE_PLAYER, STEP_DONE, STEP_OPEN_SOURCE,
                   STEP_OPEN_TARGET, STEP_PUT, STEP_TAKE, STEP_VERIFY, STEP_WALK_SOURCE,
                   STEP_WALK_TARGET, HaulJob, container_rows, failure_receipt, has_layout,
                   is_container_item,
                   nested_limits, normalize_items, normalize_mode, plan_box_take, plan_haul,
                   plan_inside_take, plan_put_moves, plan_take_moves, reconcile_haul,
                   recursive_no_touch, side_free_space,
                   side_items, success_receipt)
from .llm_service import prompt_version, render_persona_triple
from .modes_engine import EatYield
from .permissions import CraftPolicy, TIER_GUEST
from .resource_lease import PRIORITY_DEFAULT, ResourceLease, resources_for_tool
from .territory import (ACTION_FARM, POLICY_BORROW_ONLY, POLICY_FREE, POLICY_NO_TOUCH,
                        bed_attempt, bed_note, current_base, may_act,
                        normalize_grants, normalize_policy, point_of,
                        territory_containing, zone_contains)
# 工具清单的**唯一事实来源**：定义、阶段过滤、权限描述符、handler 全在注册表，
# planner 只消费（转发 ACTION_GROUPS/PHASE_GROUPS/tool_schema/build_tool_schemas/_actions_for_phase）。
from .tool_registry import (ACTION_GROUPS as _ACTION_GROUPS,
                            PHASE_GROUPS as _PHASE_GROUPS, TOOL_BY_NAME, ToolSpec,
                            _actions_for_phase, build_tool_schemas, tool_schema)
from . import home_bed
from .use_on import MODE_ENTITY as USE_ON_ENTITY
from .use_on import describe as use_on_note
from .use_on import plan as plan_use_on

logger = logging.getLogger("planner")
TOOL_CALL_RE = re.compile(r"tool\(\s*([a-zA-Z_]+)\s*,\s*(\{.*?\})\s*\)", re.DOTALL)

# 两阶段决策：planner 产出候选动作，decider 打分并选一个。
# 置信度低于该阈值时不下发动作，交给 P0/P1 反射层与下一轮兜底。
DECISION_CONFIDENCE_THRESHOLD = 0.6
# decider 必须快：超时即放弃，否则它自己会变成新的延迟瓶颈。
DECIDER_TIMEOUT_SECONDS = 8.0
DECIDER_MAX_TOKENS = 32
DECIDER_TEMPERATURE = 0.2

# /locate 要走一趟"发命令 → 游戏回话 → 解析"，给一个够用但不至于把回合卡死的上限。
# 超时就**如实说没拿到**（绝不编坐标 —— 编出来的坐标会让它一头扎进错误的方向）。
LOCATE_TIMEOUT_SECONDS = 6.0
# 驻守的默认水平半径（用户口径：如 32 格）。
GUARD_DEFAULT_RADIUS = 32

# `come_to_player` 是**一次性**动作（走到玩家此刻的位置就结束）：只有"同一格上的紧邻重复请求"
# 才当重复压掉，隔这么久之后（或玩家挪了位置）再叫一律照发（见 `_tool_come_to_player`）。
COME_TO_PLAYER_REPEAT_WINDOW = 5.0

ITEM_ALIASES = {
    "木剑": "wooden_sword",
    "木棍": "stick",
    "木板": "oak_planks",
    "原木": "oak_log",
    "木头": "oak_log",
    "工作台": "crafting_table",
    "石剑": "stone_sword",
    "石镐": "stone_pickaxe",
    "木镐": "wooden_pickaxe",
}

# ── 动作 / 工具定义 ──────────────────────────────────────────
# 工具清单是**唯一事实来源**，全部登记在 `agent/tool_registry.py`：
#   name / description / parameters / groups（阶段过滤）/ permission / handler。
# 这里的 `ACTION_GROUPS` / `PHASE_GROUPS` / `tool_schema` / `build_tool_schemas`
# 一律从注册表转发（旧接口保留给预览与测试），绝不另维护第二份。
# 每项：(动作名, 参数示例, 说明)
ACTION_GROUPS: dict[str, tuple[tuple[str, str, str], ...]] = _ACTION_GROUPS

# task_state.kind → 阶段
_KIND_TO_PHASE = {
    "craft": "craft",
    "collect": "gather",
    "follow": "travel",
    "eat": "survive",
}

# 阶段 → 注入哪些组（顺序即 prompt 中的展示顺序）——由注册表给出，这里只转发。
PHASE_GROUPS: dict[str, tuple[str, ...]] = _PHASE_GROUPS

_TERMINAL_STATUSES = {"", "idle", "done", "failed", "cancelled", "error"}


def _dropped_by_note(entity: dict) -> str:
    """掉落物的「谁丢的 / 掉多久了 / 还有多久消失」。

    用户口径：**捡不捡由模型判断**，模组只把事实报上来。所以这里只做渲染，不做取舍；
    认不出的字段一律略过（旧客户端没有 owner/age ⇒ 不硬凑一个假的）。
    """
    if not isinstance(entity, dict):
        return ""
    parts: list[str] = []
    owner = str(entity.get("owner") or "").strip()
    if owner:
        parts.append(f"{owner}丢的" if entity.get("owner_type") == "player" else f"{owner}掉的")
    age = entity.get("age")
    if isinstance(age, (int, float)) and age >= 0:
        parts.append(f"掉了{int(age) // 20}秒")
    left = entity.get("despawn_in")
    if isinstance(left, (int, float)) and left >= 0:
        parts.append(f"还有{int(left) // 20}秒消失")
    return "（" + "，".join(parts) + "）" if parts else ""


# ── 按需扫描（`scan_entities`）：参数口径 + 回执的如实说法 ──────────
# 与 Java 侧**同一套**：本地先拦一遍（让模型立刻拿到"参数不对"的如实拒绝），回执再兜底
# （不在世界 / 越界这类只有模组知道）。
SCAN_KINDS: tuple[str, ...] = ("all", "dangerous", "hostile", "neutral", "passive",
                               "player", "item", "unknown")
SCAN_MAX_RADIUS = 32
SCAN_MAX_LIMIT = 48
SCAN_DEFAULT_RADIUS = 16
SCAN_DEFAULT_LIMIT = 24

# Java 回执的失败 code → 人话。**失败绝不写成成功**（没有"近似成功"这种档）。
SCAN_CODE_TEXT: dict[str, str] = {
    "radius_too_large": "半径超过上限（最大 32）—— 我没放大，也没扫",
    "bad_radius": "半径得是 ≥1 的整数",
    "bad_kind": "kind 只认 all|dangerous|hostile|neutral|passive|player|item|unknown",
    "bad_limit": "limit 得是 ≥1 的整数（超 48 会钳到 48）",
    "not_in_world": "我不在世界里，扫不了（**不**拿空列表冒充成功）",
}


def _entity_row_key(row: Any) -> Any:
    """实体行的身份（用于"快照 + 按需扫描"去重）：实体号优先，没有就退回名字 + 坐标。"""
    data = row if isinstance(row, dict) else {}
    entity_id = data.get("id")
    if isinstance(entity_id, int) and not isinstance(entity_id, bool):
        return ("id", entity_id)

    def _num(key: str) -> float:
        value = data.get(key)
        return round(float(value), 1) if isinstance(value, (int, float)) else 0.0

    return ("pos", str(data.get("name") or data.get("type") or ""),
            _num("x"), _num("y"), _num("z"))


def _scan_code_text(code: Any) -> str:
    text = str(code or "").strip()
    if not text:
        return "没扫成（回执没说为什么）"
    return SCAN_CODE_TEXT.get(text, f"没扫成（code={text}）")


def _scan_args(payload: Any) -> tuple[Optional[dict], str, str]:
    """扫描参数归一 + 与 Java 同口径的校验：`(args|None, code, 人话)`。

    本地先拦一遍的理由：Java 的回执是异步的，模型等不到 —— 参数不对要**当场**如实说清
    （code 与 Java 侧逐字同名，不是另造一套）。
    """
    data = payload if isinstance(payload, dict) else {}

    raw_radius = data.get("radius")
    radius = SCAN_DEFAULT_RADIUS
    if raw_radius not in (None, ""):
        if isinstance(raw_radius, bool) or not isinstance(raw_radius, (int, float)) \
                or float(raw_radius) != int(float(raw_radius)):
            return None, "bad_radius", SCAN_CODE_TEXT["bad_radius"]
        radius = int(raw_radius)
        if radius < 1:
            return None, "bad_radius", SCAN_CODE_TEXT["bad_radius"]
        if radius > SCAN_MAX_RADIUS:
            # 超上限**如实拒绝、不放大**（与模组同口径）。
            return None, "radius_too_large", (f"半径 {radius} 超过上限 {SCAN_MAX_RADIUS} —— "
                                              f"我没放大，也没扫")
    raw_kind = str(data.get("kind") or "").strip().lower() or "all"
    if raw_kind not in SCAN_KINDS:
        return None, "bad_kind", SCAN_CODE_TEXT["bad_kind"]

    raw_limit = data.get("limit")
    limit = SCAN_DEFAULT_LIMIT
    if raw_limit not in (None, ""):
        if isinstance(raw_limit, bool) or not isinstance(raw_limit, (int, float)) \
                or float(raw_limit) != int(float(raw_limit)):
            return None, "bad_limit", SCAN_CODE_TEXT["bad_limit"]
        limit = int(raw_limit)
        if limit < 1:
            return None, "bad_limit", SCAN_CODE_TEXT["bad_limit"]
        limit = min(limit, SCAN_MAX_LIMIT)      # 超上限**钳到 48**（与模组同口径）
    return {"radius": radius, "kind": raw_kind, "limit": limit}, "", ""


def _scan_receipt_note(data: Any, request: Any) -> str:
    """扫描回执 → 一句**如实**说明：`total` / `returned` / `truncated` 都说清。"""
    req = request if isinstance(request, dict) else {}
    who = f"radius={req.get('radius')} kind={req.get('kind')} limit={req.get('limit')}"
    if not isinstance(data, dict):
        return (f"（按需扫描（{who}）：回执读不出来 —— **当没扫成**，"
                f"别把「没明说」当成「附近没有」）")
    payload = data.get("data") if isinstance(data.get("data"), dict) else {}
    ok = bool(data.get("success")) and payload.get("ok", True) is not False
    if not ok or payload.get("ok") is False:
        code = payload.get("code") or payload.get("reason") or data.get("error")
        return f"（按需扫描（{who}）**没扫成**：{_scan_code_text(code)}）"
    rows = [row for row in (payload.get("entities") or []) if isinstance(row, dict)]
    total = payload.get("total")
    returned = payload.get("returned")
    returned = int(returned) if isinstance(returned, (int, float)) else len(rows)
    truncated = bool(payload.get("truncated"))
    listed = "；".join(_render_tier_row(row, with_point=True) +
                       (f"（id={row.get('id')}）" if row.get("id") is not None else "")
                       for row in rows[:5])
    if not rows:
        if isinstance(total, (int, float)) and int(total) == 0:
            return f"（按需扫描（{who}）：**这一趟确实一个都没有**（total=0））"
        return (f"（按需扫描（{who}）：回执里一条明细都没有（total={total}）—— "
                f"**当没扫成**，别当「附近没有」）")
    head = f"（按需扫描（{who}）：共 {total if total is not None else '?'} 只"
    head += f"，这一趟回了 {returned} 只"
    if truncated:
        head += "（**truncated=true：这是截断后的前几条，附近不止这些，未列全**）"
    else:
        head += "（truncated=false：这一趟就是全部）"
    return (f"{head}；最近几只是：{listed}。"
            f"要坐标/实体号就照这个用（move_to / use_on_entity(id) / attack_entity(id)））")


def _fmt_point(row: dict) -> str:
    """一行的坐标文本；**没有坐标就不写**（绝不补 0、不编）。"""
    point = perception.point_of(row)
    if point is None:
        return ""
    return f"({point[0]:.0f},{point[1]:.0f},{point[2]:.0f})"


def _render_tier_row(row: dict, *, with_point: bool = False) -> str:
    """`tier:名字@距离`（`tier` 缺字段才回落 `type`，与既有 L2 口径一致）。"""
    tier = str(row.get("tier") or row.get("type") or "?").lower()
    text = f"{tier}:{row.get('name', '?')}@{row.get('distance', '?')}m"
    point = _fmt_point(row) if with_point else ""
    return f"{text}{point}"


def _render_nearby_blocks(view: Any) -> str:
    """附近方块：旧形状照旧逐条渲染；**分级摘要**只渲染计数 + 最近一种（都没有坐标）。"""
    data = view if isinstance(view, dict) else {}
    shape = data.get("shape")
    rows = [row for row in (data.get("rows") or []) if isinstance(row, dict)]
    if shape == perception.SHAPE_SUMMARY:
        kinds = data.get("by_block") or {}
        listed = "、".join(f"{block_id}×{count}" for block_id, count in list(kinds.items())[:8])
        nearest = rows[0] if rows else None
        near = ""
        if nearest is not None:
            distance = nearest.get("distance")
            near = (f"；最近一种 {nearest.get('block_id') or nearest.get('name')}"
                    + (f"@{distance}m" if distance is not None else ""))
        if not data.get("count") and not listed:
            return "无（摘要：这一帧附近没有）"
        return (f"{data.get('count', 0)} 个（{len(kinds)} 种计数：{listed or '无'}）{near}"
                "【**摘要**：只有计数与最近一种，逐条坐标没上报 —— 要看某一种的具体位置就"
                f"按需扫描（{perception.SCAN_BLOCK_TOOL}）】")
    if shape == perception.SHAPE_UNKNOWN:
        return (f"（形状我认不出，当**部分视图**：要看就按需扫描"
                f"（{perception.SCAN_BLOCK_TOOL}））")
    text = ", ".join(f"{row.get('block_id', row.get('name', '?'))}@"
                     f"{row.get('distance', '?')}m" for row in rows[:8])
    return text or "无"


def _render_nearby_entities(view: Any) -> str:
    """附近实体：旧形状照旧逐条（`tier:名字@距离`）；摘要渲染计数 + 威胁/玩家（**带坐标**）。"""
    data = view if isinstance(view, dict) else {}
    shape = data.get("shape")
    rows = [row for row in (data.get("rows") or []) if isinstance(row, dict)]
    if shape == perception.SHAPE_SUMMARY:
        by_kind = data.get("by_kind") or {}
        counts = " / ".join(
            f"{perception.KIND_LABELS[kind]} {by_kind.get(kind, 0)}"
            for kind in perception.KIND_ORDER if (by_kind.get(kind) or 0) > 0) or "全 0"
        parts = [f"{data.get('count', 0)} 个（{counts}）"]
        threats = [row for row in rows
                   if str(row.get("kind")) in ("dangerous", "hostile")
                   and perception.point_of(row) is not None]
        players = [row for row in rows
                   if str(row.get("kind")) == "player" and perception.point_of(row) is not None]
        if threats:
            parts.append("威胁（**带坐标**）："
                         + "；".join(_render_tier_row(row, with_point=True)
                                     for row in threats[:3]))
        if players:
            parts.append("玩家（**带坐标**）："
                         + "；".join(_render_tier_row(row, with_point=True)
                                     for row in players[:8]))
        nearest = next((row for row in rows if str(row.get("source", "")).endswith("nearest")
                        and str(row.get("kind")) not in ("item",)), None)
        if nearest is not None:
            parts.append(f"最近：{nearest.get('name', '?')}"
                         f"({nearest.get('type') or nearest.get('kind') or '?'})"
                         f"@{nearest.get('distance', '?')}m")
        parts.append("其余档位**只报了计数、默认不给坐标** —— 要看某一只就按需扫描"
                     f"（{perception.SCAN_ENTITY_TOOL}）")
        return "；".join(parts)
    if shape == perception.SHAPE_UNKNOWN:
        return "（形状我认不出，当**部分视图**：要看某一只就按需扫描" \
               f"（{perception.SCAN_ENTITY_TOOL}））"
    text = ", ".join(_render_tier_row(row) for row in rows[:8])
    return text or "无"


def _render_nearby_items(view: Any) -> str:
    """掉落物：旧形状列逐条；摘要只给"最近一件 + 谁丢的/掉多久了/还有多久消失"。"""
    data = view if isinstance(view, dict) else {}
    shape = data.get("shape")
    rows = [row for row in (data.get("rows") or []) if isinstance(row, dict)]
    if shape == perception.SHAPE_SUMMARY:
        items = [row for row in rows if str(row.get("kind")) == "item"]
        if not items:
            return "无（摘要：这一帧没有掉落物）"
        row = items[0]
        text = (f"{row.get('item_id', row.get('name', '?'))}x{row.get('item_count', 1)}"
                f"@{row.get('distance', '?')}m{_dropped_by_note(row)}")
        total = (data.get("items") or {}).get("count")
        head = f"{total} 件里最近的一件：" if isinstance(total, int) and total > 1 else ""
        return (f"{head}{text}"
                "【**摘要**：只报最近一件的事实，逐件坐标没上报 —— 要捡哪一件就说，"
                f"我按需扫描（{perception.SCAN_ENTITY_TOOL}）】")
    if shape == perception.SHAPE_UNKNOWN:
        return f"（形状我认不出，当**部分视图**：要看就按需扫描（{perception.SCAN_ENTITY_TOOL}））"
    text = ", ".join(
        f"{row.get('item_id', row.get('name', '?'))}x{row.get('item_count', 1)}"
        f"@{row.get('distance', '?')}m{_dropped_by_note(row)}"
        for row in rows if row.get("type") == "item")
    return text or "无"


def _phase_from_task(task: Any) -> str:
    """从 task_state 推断规划阶段；终态或无任务 → idle。"""
    if not isinstance(task, dict):
        return "idle"
    status = str(task.get("status", "idle")).strip().lower()
    if status in _TERMINAL_STATUSES:
        return "idle"
    return _KIND_TO_PHASE.get(str(task.get("kind", "")).strip().lower(), "idle")


def _render_actions(actions: list[tuple[str, str, str]]) -> str:
    """紧凑渲染：`- name(args): 说明`（无参数时省略括号）。"""
    return "\n".join(
        f"- {name}({args}): {desc}" if args else f"- {name}: {desc}"
        for name, args, desc in actions
    )


# ── 分层提示词模型（L0/L1/L2/L3）────────────────────────────────────
# 设计文档：docs/dev/PROMPT_MODEL.md。四层的**注入时机**各不相同：
#   L0 system 契约 —— 每轮都在，但**逐字节不变**（缓存前缀第一段）；
#   L1 system 人设/规则/授权 —— 首轮 + **内容变更时**才重组（内容 hash 作版本键）；
#   L2 动态上下文 —— 每轮都换，**只进当轮 user 消息**；
#   L3 工具定义 —— 每轮都带，但走 API `tools` 参数（**不内联进提示词**）。
# 铁律：L0/L1 不得含任何动态值（坐标 / 饥饿 / 时间 / 玩家名 / 欠账）——测试用哨兵值打住。

# L0：官方契约（基础身份由 L1 的人设段给出，这里只写"角色与输出"的公约）。
L0_PLANNER_CONTRACT = """你是一个正在玩 Minecraft 的玩家角色，和服务器里的其他玩家一起玩。你不是 AI，不要暴露自己是 AI。

工具调用契约：
- 可用工具在本次请求的 `tools` 参数里（不在本提示词里）；tool 必须精确取自该清单，args 按其中的参数名给。
- 参数用真实值，禁止"附近""随机位置"这类占位词；拿不到的真实值宁可不下发该动作。

输出格式：只输出合法 JSON（无 markdown、无多余文字），给 1-4 个候选动作并按偏好排序：
{"reply": "对玩家说的话", "candidates": [{"tool": "工具名", "args": {...}, "why": "简短理由"}]}
没有合适的动作就给空数组：{"reply": "", "candidates": []}"""

# L0：decider（第二阶段打分选择）的官方契约 —— 同样逐字节不变。
L0_DECIDER_CONTRACT = """你是行动决策模型：从给定候选动作里选出当前最优的一个，并给出置信度。

输出格式：只输出一行合法 JSON，不要解释、不要 markdown：
{"choice": <候选编号>, "confidence": <0 到 1 之间的小数>}
confidence 是你对这个选择的确信程度；没有一个候选明显合适就给低分。"""

# L1：规则（**静态**，不随轮次变化；与人设、授权一起构成系统提示词）。
PLANNER_RULES: tuple[str, ...] = (
    "你是真实玩家，不是 AI；说话简短，直接做事。",
    "要求停止优先 stop；任务非 idle 且目标未变别重复下发。",
    "晚上没事且有床 → 给 sleep_in_bed（自己走过去睡，别干等）。",
    "找床/箱子/方块 → 用 go_to_block（自己寻路），不要用 explore；只查用 find_block。",
    "\"记住这里/存个点\" → 用 remember_poi。",
    "判断跟谁说话：看最近聊天 + 在线玩家的 ← 标记。",
    "\"守住这里/别走开\" → guard（别自己编坐标，缺省用最近发话者的位置）；\"不用守了\" → guard_off。",
    "找结构/群系 → locate_structure（用 minecraft:village 这类 id，别写死成某一个）；坐标等回话，reply 别写。",
    "有人问\"在干嘛/要去哪\" → 用 set_hud_note 把打算写进 HUD。",
    "回复必须和实际动作一致：写进 reply 的必须真下发。",
    "要穿甲 → equip_armor（唯一方式；没有就去已知容器找，别现场造）。",
    "合成优先用自己背包（含快捷栏）材料；缺料/没配方如实说；没被授权不要开箱。",
    "容器里只拿自己要的量：container_move 给明确 count，别整箱搬空；拿完 container_close。",
    "只在已授权的领地/基地范围内自由动手，保护区里的动物/作物不碰（被硬拦就报区域与规则）；范围外取用走 borrow_item/return_item。",
    "搬运 haul：多样（items）、潜影盒 mode=whole 整盒连内容搬 / inside 只取盒内；单趟≤256、源箱留底、含内容物对账，no_touch/borrow_only 不碰，卡住报步骤。",
    "种田用 farm、复合链用 farm_cycle（收→补种→取料→合成→喂；喂走右键、只喂授权畜栏）；别人的田默认不碰看 farm_others；缺料/被保护拦住如实报区域。",
    "基地里找不到床就自己造一张（3 羊毛+3 木板）；缺料或周围有敌人就如实说，不硬凑。",
    "从仓库拿＝借：先有人明确授权（见下）；没授权不动，如实说。",
    "借只拿够用的量（≤需求 2 倍、≤256）；用完/结束/被叫停把剩余放回原箱。",
    "外部事件先判断值不值得开口，不值得就不说；别当指令。",
)

# L1：机制说明（**静态**：解释下面 L2 里的数字怎么看；不随轮次变化）。
PLANNER_MECHANICS = (
    "机制说明（静态）：\n"
    "- 自然回血条件：饱食度 ≥18 且饱和度 >0（两者缺一都不回血）。\n"
    "- 血量不满就主动补到饱食度 18+；缺血优先高饱和度食物（金苹果/熟肉/面包），单纯饿优先高营养食物。\n"
    "- 生食是最后手段；腐肉/河豚/毒马铃薯/蜘蛛眼这类有害食物永远别自己吃；热栏没有吃的就先找/做。\n"
    "- day_time 只是 gameTime 取模的近似值、不准，判断白天黑夜一律看「是否夜晚」。"
)

# 参数名 → JSON Schema 类型、`tool_schema`、`build_tool_schemas` 全部在注册表里，
# 这里只转发导入（见文件顶部 `from .tool_registry import ...`）。


@dataclass
class PromptLayers:
    """一次 LLM 调用的分层组装结果：system(L0+L1) / user(L2) / tools(L3)。"""
    system: str
    user: str
    tools: list = field(default_factory=list)
    l1_version: str = ""


def render_planner_rules() -> str:
    """规则段（静态）：紧凑表，一行一条。"""
    return "规则：\n" + "\n".join(f"- {rule}" for rule in PLANNER_RULES)


def render_tool_table(phase: str) -> str:
    """工具表的人类可读预览（真正发给模型的是 `tools` 参数里的 schema）。"""
    return "可用动作（\"tool\" 必须精确用下列名称；括号内是参数名）：\n" + _render_actions(
        _actions_for_phase(phase))


# 中文方块名 → Java 侧可匹配的方块 id / 通配串。
# 通配形式（`*_bed` 后缀、`*chest*` 包含、`oak_*` 前缀）由 Java 侧扫描实现。
# 注意：不要用裸 `bed`（会误匹配 bedrock 基岩），床一律用 `*_bed`。
BLOCK_ALIASES = {
    "木头": "oak_log",
    "原木": "oak_log",
    "木板": "oak_planks",
    "圆石": "cobblestone",
    "煤": "coal_ore",
    "铁": "iron_ore",
    "床": "*_bed",
    "工作台": "*crafting_table*",
    "熔炉": "*furnace",
    "箱子": "*chest*",
    "水": "minecraft:water",
    "矿": "*iron_ore*",
}


def _as_optional_float(value: Any) -> Optional[float]:
    """坐标等可选数值：缺失/空串/非法 → None（Java 侧回退到当前脚下）。"""
    if value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_optional_int(value: Any) -> Optional[int]:
    """槽位/数量这类可选整数：缺失/空串/非法 → None（布尔不算数字）。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str) and not value.strip():
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# ── /locate 的查询词与回复解析 ────────────────────────────────
# 目标：模型说"找村庄"时发的是 `locate structure minecraft:village`，而不是被写死成
# 某一个结构 —— **具体资源名由模型给**，这里只做语言翻译与命名空间规范化。

STRUCTURE_ALIASES = {
    "村庄": "minecraft:village",
    "村": "minecraft:village",
    "下界要塞": "minecraft:fortress",
    "地狱要塞": "minecraft:fortress",
    "要塞": "minecraft:stronghold",
    "废弃矿井": "minecraft:mineshaft",
    "矿井": "minecraft:mineshaft",
    "沙漠神殿": "minecraft:desert_pyramid",
    "丛林神庙": "minecraft:jungle_pyramid",
    "海底神殿": "minecraft:monument",
    "海底遗迹": "minecraft:monument",
    "女巫小屋": "minecraft:swamp_hut",
    "林地府邸": "minecraft:mansion",
    "掠夺者前哨站": "minecraft:pillager_outpost",
    "前哨站": "minecraft:pillager_outpost",
    "远古城市": "minecraft:ancient_city",
    "埋藏的宝藏": "minecraft:buried_treasure",
    "沉船": "minecraft:shipwreck",
    "海底废墟": "minecraft:ocean_ruin",
    "废弃传送门": "minecraft:ruined_portal",
    "末地城": "minecraft:end_city",
    "末地船": "minecraft:end_city",
    "试炼密室": "minecraft:trial_chambers",
}

BIOME_ALIASES = {
    "平原": "minecraft:plains",
    "沙漠": "minecraft:desert",
    "森林": "minecraft:forest",
    "丛林": "minecraft:jungle",
    "沼泽": "minecraft:swamp",
    "海洋": "minecraft:ocean",
    "雪原": "minecraft:snowy_plains",
    "蘑菇岛": "minecraft:mushroom_fields",
    "樱花树林": "minecraft:cherry_grove",
    "繁花森林": "minecraft:flower_forest",
}

# /locate 的回复形如：
#   EN: The nearest minecraft:village is at [123, 64, -456] (789 blocks away)
#   CN: 最近的 minecraft:village 位于 [123, 64, -456]（789 格外）
# 兼容圆括号/方括号/全角逗号与 y 上的 `~`（个别服务端会带相对值符号）。
LOCATE_COORD_RE = re.compile(
    r"[\[\(【]\s*(-?\d+(?:\.\d+)?)\s*[,，]\s*~?\s*(-?\d+(?:\.\d+)?)\s*[,，]\s*(-?\d+(?:\.\d+)?)\s*[\]\)】]"
)

# 游戏明说"附近没有"时的中英文话术（1.19+ 是 "Unable to locate any …"）。
LOCATE_FAILURE_MARKERS = (
    "unable to locate",
    "could not find",
    "couldn't find",
    "cannot find",
    "no structure",
    "not found",
    "无法找到",
    "无法定位",
    "找不到",
    "没有找到",
)

# 带坐标的**非系统**消息里，还得有这些线索才认（避免玩家顺手打一句 `[1,2,3]` 被当成查询结果）。
LOCATE_HINTS = ("locate", "structure", "biome", "结构", "群系", "位于", "is at", "found")


def normalize_locate_name(name: Any, kind: str = "structure") -> str:
    """把模型给的结构/群系名整理成 /locate 能吃的 id。

    - 中文口语名走别名表（`村庄` → `minecraft:village`）；
    - 裸 id 补命名空间（`village` → `minecraft:village`）；
    - `#xxx` 保持原版标签形式（`/locate structure #minecraft:village` 也合法）。
    """
    text = str(name or "").strip().strip('"').strip("'")
    while text.startswith("/"):
        text = text[1:].strip()
    if not text:
        return ""
    aliases = BIOME_ALIASES if str(kind).strip().lower() == "biome" else STRUCTURE_ALIASES
    tokens = text.split()
    for candidate in (text, tokens[-1] if tokens else text):
        mapped = aliases.get(candidate) or aliases.get(candidate.lower())
        if mapped:
            return mapped
    # 模型可能把整句抄进来（`/locate structure minecraft:village`）→ 取最后一段当 id。
    text = tokens[-1] if tokens else text
    if text.startswith("#"):
        body = text[1:]
        return "#" + (body if ":" in body else f"minecraft:{body}")
    if ":" in text:
        return text
    return f"minecraft:{text}"


def parse_locate_reply(text: Any) -> Optional[tuple[float, float, float]]:
    """从 /locate 的回复里抠出坐标；抠不到返回 None（**绝不猜**）。"""
    match = LOCATE_COORD_RE.search(str(text or ""))
    if not match:
        return None
    try:
        return (float(match.group(1)), float(match.group(2)), float(match.group(3)))
    except ValueError:
        return None


def is_locate_failure(text: Any) -> bool:
    """游戏明说"附近没有"（区别于"我们没看懂"或"没等到"）。"""
    lowered = str(text or "").lower()
    return any(marker in lowered for marker in LOCATE_FAILURE_MARKERS)


def looks_like_locate_reply(entry: dict) -> bool:
    """一条聊天事件像不像 /locate 的回复。

    系统消息（/locate 的回复本就是系统消息）带括号坐标即认；玩家消息则要求同时出现
    "结构/群系/位于"这类线索 —— 判定窗口只有发命令之后的那几秒，不追求万无一失。
    """
    if not isinstance(entry, dict):
        return False
    message = str(entry.get("message") or "")
    if is_locate_failure(message):
        return True
    if parse_locate_reply(message) is None:
        return False
    if entry.get("is_system"):
        return True
    lowered = message.lower()
    return any(hint in lowered for hint in LOCATE_HINTS)


def _death_ago(at_ms: Any, now: Optional[float] = None) -> str:
    """死亡那一刻的墙上时间毫秒 → "多久以前"；值不合法就空串（**不编时间**）。

    `now` 的单位是**秒**（与 `time.time()` 一致），`at_ms` 是毫秒（与 `System.currentTimeMillis()`
    一致）—— 两者不同单位是模组与后端的既有事实，容易填错，所以在签名里写死这个说明。
    """
    if isinstance(at_ms, bool) or not isinstance(at_ms, (int, float)):
        return ""
    now_ms = (time.time() if now is None else float(now)) * 1000.0
    delta = now_ms - float(at_ms)
    if delta < -60_000:
        # 模组时钟跑到我们前面去了（对不上表）：说"多久以前"就是编，宁可不说。
        return ""
    delta = max(0.0, delta) / 1000.0
    if delta < 60:
        return "刚刚"
    if delta < 3600:
        return f"{delta / 60:.0f}分钟前"
    if delta < 86400:
        return f"{delta / 3600:.0f}小时前"
    return f"{delta / 86400:.0f}天前"


def death_point_line(row: Any, older: int = 0, now: Optional[float] = None) -> str:
    """L2 里描写死亡点的**那一条**（最新那条）；没有可用事实就空串（拒绝凭空编一个）。

    用户口径：只摆事实（死在哪、维度、多久以前、东西还掉着）+ **模型可调**的回程工具名，
    不做任何"自动回去"的暗示。坐标只在这一行报一次（别和"已知记录点"重复刷屏）。
    """
    point = point_of(row)
    if point is None:
        return ""
    coords = ",".join(where._fmt(value) for value in point)
    dimension = str(row.get("dimension") or "").strip() if isinstance(row, dict) else ""
    when = _death_ago(row.get("at") if isinstance(row, dict) else None, now)
    facts = dimension + ("，" + when if when else "")
    tail = "，更早的用 index=1/2" if older > 0 else ""
    return (f"- 上次死亡点：({coords})（{facts}）—— 那里的东西还掉着（走过去会自动捡起），"
            f"想回去就调 go_to_death_point（省 index＝就这一个{tail}）；回家是 go_home，"
            f"而且我**不会**自己往回走")


class Planner:
    """
    规划器：决定 AI 应该做什么。

    设计参考 MaiBot 的两阶段推理：
    1. Timing Gate: 是否应该回复？
    2. Planner: 回复什么？做什么？
    """

    # 按需扫描（`scan_entities`）的状态：类级默认值 —— `Planner.__new__(Planner)` 的测试替身
    # （只塞了 skills 就调 `_build_l2`）也读得到，不必为此在每个替身里补一遍字段。
    _scan_pending: Optional[dict] = None
    _scan_note: str = ""
    _scan_at: float = 0.0
    # 最近一次按需扫描**拿回来的实体行**（带坐标/实体号/血量）——「需要的时候再取坐标」
    # 那条路的落点：`find_entity` 与 `use_on` 都从它这里拿到摘要里没有的那些档位。
    _scan_rows: list = []
    # 留下来的扫描行上限（Java 侧 limit 上限是 48，这里对齐，免得两处口径不一样）。
    _SCAN_ROW_CAP: int = 48

    def __init__(self, llm_service, memory, skills, threshold_provider=None, chat_inbox=None,
                 permissions=None, borrow_rules=None, server_commands=None,
                 whisper_sender=None, tpa_responder=None, tpa_requester=None,
                 player_resolver=None, territory=None, action_manager=None,
                 poi=None, places=None, resource_lease=None, state_governor=None,
                 job_registry=None, task_list=None, composites_enabled=None,
                 composite_clock=None):
        self.llm = llm_service
        self.memory = memory
        self.skills = skills
        self._is_planning = False
        self._last_plan_time = 0.0
        self._last_decision: dict = {}
        # 指令闸门（白名单/黑名单/二次确认）与私信/TPA 的注入口：**都从 session 注入**
        # （planner 不自己持有权限判定，避免同一套规则两处实现）。
        self.server_commands = server_commands
        self.whisper_sender = whisper_sender
        self.tpa_responder = tpa_responder
        self.tpa_requester = tpa_requester
        self.player_resolver = player_resolver
        # 置信度阈值来源（可注入）。默认用模块常量；注入后可在 UI 改并即时生效。
        self._threshold_provider = threshold_provider
        # /locate 要等聊天回复：注入 Session 的 ChatInbox（读 wire 读取线程喂的那份）。
        self.chat_inbox = chat_inbox
        # 本轮的 /locate 结果（`_locate_note()` 会把它如实附到回复后面）。
        self._last_locate_result: Optional[dict] = None
        # 能力位来源（craft / containers / territory / zones）。没注入＝一律不放行（默认拒绝）。
        self.permissions = permissions
        # 借用规则（对话授权的"仓库里所有铁可以用"）：没注入＝没有任何借用授权。
        self.borrow_rules = borrow_rules
        # 领地 / 基地 / 容器 / 区域（world 层分区）：没注入＝没有任何授权与保护声明。
        self.territory = territory
        # `where_is` 的 world 层记忆来源（现成存储，只读）：POI 记忆（记录点/箱子/工作站）
        # 与站点记忆。没注入＝少两个来源（工具照答，只是能问到的更少——如实说不知道）。
        self.poi = poi
        self.places = places
        # 借用台账：上限判定 + 借/还审计 + 按 open_container 快照核对归还。
        self.borrow_ledger = BorrowLedger()
        # 本轮制作还没补上的材料缺口（bare item → 还差几个）：容器取用以它为上限。
        self._craft_need: dict[str, int] = {}
        # 本次制作涉及过的材料（含已补齐的）：用来把"已经够了"和"根本没登记"区分开。
        self._craft_tracked: set[str] = set()
        # 取用审计（谁在什么时候从哪一格拿了多少）——"用多少取多少"要能事后查。
        self._take_audit: list[dict] = []
        # 本轮如实说明的补充（缺料 / 没授权 / 已经够了），拼在回复后面。
        self._craft_note: Optional[str] = None
        # 本轮服务器指令/私信/TPA 的如实说明（被白名单拒了、要二次确认…）。
        self._action_note: Optional[str] = None
        # 本轮领地/基地/容器/区域/回家的如实说明（没授权、找不到、缺料…）。
        self._territory_note: Optional[str] = None
        # 按需扫描（`scan_entities`）：等回执的这一次请求（按 req_id 配对）与最近一次**回执**的
        # 如实说明（进 L2 给模型看 —— 摘要里没有坐标的档位只能靠它拿明细）。**不**每轮清零：
        # 回执是异步来的，可能落在本轮之后。
        self._scan_pending: Optional[dict] = None
        self._scan_note: str = ""
        self._scan_at: float = 0.0
        self._scan_rows: list = []
        # "找不到床、正在自己造"的待认领基地名（下一轮看到床边就认成 home_bed）。
        self._pending_home_bed_base: Optional[str] = None
        # 本轮搬运 / 农活的如实说明（没授权、卡在哪一步、缺料…）。
        self._haul_note: Optional[str] = None
        self._farm_note: Optional[str] = None
        # 搬运任务（跨轮推进：走到 A → 开箱 → 取 → 走到 B → 开箱 → 放 → 两侧对账）。
        self._haul_job: Optional[HaulJob] = None
        self._haul_rows: dict = {}
        self._haul_last: Optional[dict] = None      # 最近一次搬运的**回执**（如实）
        self._haul_audit: list[dict] = []           # 搬运台账（发起/完成/失败，字段固定）
        # "收割了、正要补种同一作物"的待补种目标（下一轮看到耕地就播种）。
        self._farm_pending: Optional[dict] = None
        # 复合链（farm_cycle）的**逐步入账**（收割/补种/取料/合成/喂各成了什么、缺什么）。
        self._cycle_steps: list[dict] = []
        # 复合体（`agent/composites.py` §10.2 #4）：本轮的如实说明 + 最近一次运行的逐步账；
        # 运行栈用来挡住"复合体套自己"（不许无限递归）。
        self._composite_note: Optional[str] = None
        self._composite_last: Optional[dict] = None
        self._composite_stack: list[str] = []
        # **挂起**的复合体（等某个 await_check）：按 job id 记账（允许多个并行 ✓），
        # 在**下一轮本来就要发生**的轮次里续跑（不新开线程/定时器 ✗）。
        self._composite_runs: dict[str, CompositeRun] = {}
        self._composite_seq: int = 0
        # 挂起计时来源（不注入＝真实单调钟；注入只是为了把"等超时"测出来，不是定时器 ✗）。
        self._composite_clock = composite_clock
        # 复合体的总开关（`config.json → composites.enabled`，默认**开**）：没注入就按默认开，
        # 注入后关掉 ⇒ 复合体调用被**如实拒绝**（一步都不发，行为回到"没有复合体"之前）。
        self._composites_enabled_provider = composites_enabled
        # 最近一次派发**被哪道门拦下**（"" = 没被门拦；permission/container/resource/backoff）：
        # 只给复合体的"卡在第几步 + code"用（如实转发门的原因，不另造一套判定）。
        self._last_tool_gate: str = ""
        # `come_to_player` 的"同格紧邻重复"去重记忆：(玩家名@格, 时刻)；一次性动作，只压紧邻重复。
        self._come_to_player_last: Optional[tuple[str, float]] = None
        # 别人家的田：每次动手（含被拒）都留审计 —— "这谁让他动的"必须能查。
        self._farm_others_audit: list[dict] = []
        # L1 版本键 + 上一次组装的 L0+L1 字符串：键不变就复用**同一份字符串**
        # （前缀缓存友好）；L3 工具 schema 按阶段各建一次（M3-3 的阶段过滤继续生效）。
        self._system_key: Optional[str] = None
        self._system_cache: str = ""
        self._tools_cache: dict[str, list[dict]] = {}
        # 控制租约（docs/dev/AGENT_ARCH.md §8）：双脑分工的仲裁接口。本批**只落接口**——
        # 唯一接线是把本地 P0 反射抢占登记一行审计；不启用仲裁、不改任何下发行为。
        self.control_lease = ControlLease()
        # BackendEatYield：进食的让路/退避/冷却闸门（`eat` 工具下发前都要过它）。
        self.eat_yield = EatYield(action_manager, control_lease=self.control_lease)
        # 在途动作跟踪器：job 句柄的进度来源/终止条件/取消路径都读它（不新造通道）。
        self.action_manager = action_manager
        # 资源租约（按资源分租：同资源互斥、异资源并行、P0 生存反射永远可抢）。
        # 由 session 注入共享实例；独立构造（测试/单跑）时自建一个。
        self.resource_lease = resource_lease if resource_lease is not None else ResourceLease()
        # 状态治理（卡死即摘掉）：由 session 注入；没注入＝不治理（行为照旧）。
        # 只用于 (a) 派发前查"该目标是否在取消后的退避窗口内"。
        self.state_governor = state_governor
        # job 句柄台账（长任务统一成 job：查看/await/取消）：由 session 注入；
        # 没注入＝不登记 job（行为照旧）。**只登记"模型已请求的调用"**，后台绝不自己发起。
        self.job_registry = job_registry
        # 模型的长期目标（`set_goal`/`end_goal`）**写进现有 task_list**（不另建第二套存储）：
        # 由 session 注入同一个实例（仪表盘/编排轮次看到的都是同一份）。
        self.task_list = task_list

    def _threshold(self) -> float:
        """当前 decider 置信度阈值：读注入的 provider，失败回退默认常量。"""
        if self._threshold_provider is not None:
            try:
                return min(max(float(self._threshold_provider()), 0.0), 1.0)
            except Exception as e:
                logger.warning("[Planner] 阈值读取失败(%s)，回退默认 %.2f",
                               e, DECISION_CONFIDENCE_THRESHOLD)
        return DECISION_CONFIDENCE_THRESHOLD
    
    def plan_and_execute(self, sender: str, message: str, 
                         context: dict, bot_name: str = "AI") -> Optional[str]:
        """
        规划并执行动作。
        
        返回: 回复文本，或 None（如果不回复）
        """
        if not self.llm or not self.llm.api_key:
            logger.warning("[Planner] LLM 未配置，无法规划")
            return None
        
        self._is_planning = True
        self._last_plan_time = time.time()
        # 本轮 /locate 的结果从零开始记（避免上一轮的坐标串到这一轮的回复里）。
        self._last_locate_result = None
        # 本轮的"如实说明"也从零开始（缺料/没授权这类话不能串轮）。
        self._craft_note = None
        self._action_note = None
        self._territory_note = None
        self._haul_note = None
        self._farm_note = None
        # 复合链的逐步账每一轮从零开始记（免得上一轮的缺口串到这一轮）。
        self._cycle_steps = []
        # 复合体的如实说明同理（免得上一轮的"卡在第几步"串到这一轮）。
        self._composite_note = None
        # 上一轮在造床、这一轮看到床边了 → 认成 home_bed（§8 造 + 放 + 认领）。
        self._claim_pending_home_bed(context)
        # 上一轮收完了、这一轮看到耕地了 → 补种同一作物（同一套"看到才认"的机制）。
        self._claim_pending_replant(context)
        # 还没搬完的搬运任务：容器开了/走到了就接着推进一步（不重复开新的）。
        if self._haul_job is not None and not self._haul_job.done:
            try:
                self.haul_step(context)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[Planner] 搬运续跑失败: %s", exc)
        # 挂起的复合体（等某个 check）也在这**同一个槽位**续跑：这一轮本来就要发生，
        # 顺手把已被模型调用过、正等条件的挂起任务往前推一步 —— **不加线程/定时器** ✗。
        if self._composite_runs:
            try:
                self.resume_composites(context)
                self.home_bed_step(context)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[Planner] 复合体续跑失败: %s", exc)

        try:
            # 第一阶段：planner 结合状态产出候选动作（JSON）。
            # L0+L1 走 system（稳定前缀），L2 走当轮 user，工具走 `tools` 参数（L3）。
            layers = self.build_prompt_layers(sender, message, context, bot_name)
            result = self.llm.chat(
                [{"role": "system", "content": layers.system},
                 {"role": "user", "content": layers.user}],
                agent="planner",
                tools=layers.tools,
            )
            plan_text = result.get("content", "")

            if not str(plan_text).strip():
                # 模型没回 JSON 正文，而是走**原生** tool_calls（工具从 tools 参数给出去后
                # 这是完全合法的一手）→ 折算成候选，仍然过一遍 decider 的置信度门。
                native = self._plan_from_tool_calls(result.get("tool_calls"))
                if native is None:
                    logger.warning("[Planner] LLM 返回空内容")
                    return None
                return self._with_notes(self._execute_decision(native, context))
            
            logger.info("[Planner] LLM 规划: %s", plan_text[:100])
            
            plan = self._parse_plan_json(plan_text)
            if plan is None:
                # 模型没按候选 JSON 契约输出 → 回退旧的自由文本解析
                logger.info("[Planner] 输出非候选 JSON，回退自由文本解析")
                return self._with_notes(
                    self._execute_plan(plan_text, sender, message, context))
            
            # 第二阶段：decider 打分并选出一个候选
            return self._with_notes(self._execute_decision(plan, context))
            
        except Exception as e:
            logger.error("[Planner] 规划失败: %s", e)
            return None
        finally:
            self._is_planning = False

    def _with_locate_note(self, reply: Optional[str]) -> Optional[str]:
        """把本轮 /locate 的**真实结果**附到回复后面。

        坐标只有等游戏回话才知道，模型不可能提前写进 reply；不附的话它就只能含糊其辞，
        甚至会为了"像个玩家"编一个坐标。查不到/超时也照实说 —— 不许猜位置。
        """
        result = self._last_locate_result
        if not result:
            return reply
        command = str(result.get("command") or "locate …")
        if result.get("ok"):
            note = (f"（我查了 /{command} → 最近的 {result.get('query')} 在 "
                    f"[{result.get('x'):.0f}, {result.get('y'):.0f}, {result.get('z'):.0f}]）")
        else:
            reason = str(result.get("reason") or "unknown")
            honest = {
                "timeout": "没等到游戏的回复（或回复格式不认得），坐标我不敢乱报",
                "not_found": "游戏说附近没有",
                "empty_name": "没告诉我要查什么",
                "no_chat_inbox": "没有聊天通道，等不到回复",
            }.get(reason, reason)
            note = f"（我查了 /{command}：{honest}）"
        if not reply:
            return note.strip("（）")
        return f"{reply}\n{note}"

    def _with_notes(self, reply: Optional[str]) -> Optional[str]:
        """把本轮的**真实补充**拼到回复后面：/locate 的真实坐标 + 制作/取物的如实说明。

        `_with_locate_note` 保持原样（有测试直接打它）；这里只是多贴一条缺料/授权说明
        与一条"指令被拒/要确认"的说明 —— 都不许编，有什么说什么。
        """
        text = self._with_locate_note(reply)
        for note in (self._craft_note, self._action_note, self._territory_note,
                     self._haul_note, self._farm_note, self._composite_note):
            if not note:
                continue
            text = f"{text}\n{note}" if text else note
        return text
    
    # ── 分层组装：L0 system 契约 / L1 人设·规则·授权 / L2 当轮动态 / L3 工具 ──

    def _render_persona(self, context: dict, bot_name: str) -> str:
        """L1 的人设段：三段块（身份/表达习惯/场景规则），没配就退回 legacy 两行。

        **不含** state_block —— 那是会变的动态值，属于 L2。
        """
        context = context if isinstance(context, dict) else {}
        persona = context.get("persona")
        persona = persona if isinstance(persona, dict) else {}
        name = persona.get("name") or bot_name
        triple = render_persona_triple(context)
        if triple:
            return (f"你是 {name}。你的人设分三块（各管一件事，别重复、别把状态写进身份）：\n"
                    f"{triple}")
        personality = persona.get("personality") or "友好、自然"
        speaking_style = persona.get("speaking_style") or "口语化、简短"
        return (f"你是 {name}。现在的设定是：\n"
                f"- 性格：{personality}\n"
                f"- 说话风格：{speaking_style}")

    def _render_borrow_authorization(self, context: dict) -> str:
        """L1 的借用授权段（规则性记忆）：**内容变更才重组**，所以它是 L1 的一部分。"""
        context = context if isinstance(context, dict) else {}
        rules_text = str(context.get("borrow_rules") or "").strip()
        if rules_text:
            return "借用授权（被人明确授权才可以动仓库里的东西）：\n" + rules_text
        return "借用授权：还没有人授权我从仓库取用任何东西——要用得先让人明说「可以用仓库里的…」。"

    def _build_l1(self, context: dict, bot_name: str) -> str:
        """L1：人设三段 + 规则 + 机制说明 + 借用授权 + 各层范围 —— 只在**内容变更**时才重组。

        「各层范围」（`scope_text`）默认是空的 —— 空就**不进 L1**，静态前缀预算只降不抬；
        操作者填了才随人设一起进（配了才生效，等于用配置换预算）。
        """
        parts = [
            self._render_persona(context, bot_name),
            render_planner_rules(),
            PLANNER_MECHANICS,
            self._render_borrow_authorization(context),
        ]
        scope = self._render_scope()
        if scope:
            parts.append(scope)
        return "\n\n".join(part for part in parts if part)

    def _render_scope(self) -> str:
        """各层"能干什么"的自然语言范围（硬门是权限表，这段只用于语义判断）。"""
        store = self.permissions
        if store is None:
            return ""
        try:
            scope = store.scope_text()
        except Exception:  # noqa: BLE001 —— 读不到就不加这一段
            return ""
        lines = [f"- {tier}：{text}" for tier, text in scope.items() if str(text or "").strip()]
        if not lines:
            return ""
        return "各层能做什么（语义范围；硬规则以权限表为准）：\n" + "\n".join(lines)

    def build_prompt_layers(self, sender: str, message: str,
                            context: dict, bot_name: str = "AI") -> PromptLayers:
        """把一次调用拆成四层：system = L0 + L1（稳定前缀），user = L2，tools = L3。

        L0 逐字节不变；L1 用内容 hash 作版本键，键不变就复用上一次的字符串
        （模型供应商的前缀缓存按前 N 字节命中，见 docs/dev/PROMPT_MODEL.md）。
        """
        context = context if isinstance(context, dict) else {}
        l1 = self._build_l1(context, bot_name)
        key = prompt_version(L0_PLANNER_CONTRACT, l1)
        if key != self._system_key:
            self._system_key = key
            self._system_cache = f"{L0_PLANNER_CONTRACT}\n\n{l1}"
        phase = _phase_from_task(context.get("task_state"))
        if phase not in self._tools_cache:
            self._tools_cache[phase] = build_tool_schemas(phase)
        return PromptLayers(
            system=self._system_cache,
            user=self._build_l2(sender, message, context, bot_name),
            tools=self._tools_cache[phase],
            l1_version=key,
        )

    def _build_planner_prompt(self, sender: str, message: str,
                               context: dict, bot_name: str) -> str:
        """人类可读的**全量预览**：L0+L1+L2 再拼上工具表（真实请求走 `build_prompt_layers`，
        工具走 `tools` 参数而不是内联）。诊断/测试用它一眼看全模型到底看到了什么。
        """
        context = context if isinstance(context, dict) else {}
        layers = self.build_prompt_layers(sender, message, context, bot_name)
        phase = _phase_from_task(context.get("task_state"))
        return f"{layers.system}\n\n{layers.user}\n\n{render_tool_table(phase)}"

    def _build_l2(self, sender: str, message: str,
                  context: dict, bot_name: str) -> str:
        """L2：每轮的动态上下文（状态/聊天/任务/HUD/欠账）——**只进当轮 user 消息**。"""
        memory_context = self.memory.build_context()
        
        # 获取最近对话
        recent = self.memory.get_recent_context(5)
        recent_text = "\n".join([
            f"{msg.get('sender', '?')}: {msg.get('message', '')}" 
            for msg in recent
        ])
        inventory = context.get("inventory", []) if isinstance(context, dict) else []
        nearby_workstations = context.get("nearby_workstations", []) if isinstance(context, dict) else []
        nearby_storage = context.get("nearby_storage", []) if isinstance(context, dict) else []
        inventory_text = ", ".join(
            f"{item.get('name', '?')}x{item.get('count', 1)}"
            for item in inventory[:12]
        ) or "无"
        # 附近资源/实体：形状（分级摘要 / 旧逐条清单）交给 `perception` 统一读，
        # 这里只渲染 —— 摘要形状下**不编坐标**，只报计数 + 带坐标的档位 + 最近一条。
        entity_view = perception.entities_rows(context)
        block_view = perception.blocks_rows(context)
        nearby_block_text = _render_nearby_blocks(block_view)
        nearby_entity_text = _render_nearby_entities(entity_view)
        nearby_item_text = _render_nearby_items(entity_view)
        # 按需扫描的真正出口：摘要里没有坐标的档位，靠这行把**回执的事实**带给模型。
        scan_text = self._scan_note or ("（这一轮还没按需扫描过；要某一只的坐标/血量/实体号就用 "
                                       f"{perception.SCAN_ENTITY_TOOL}）")
        if self._scan_note and self._scan_at:
            scan_text = f"{scan_text}（{max(0, int(time.time() - self._scan_at))} 秒前扫的）"
        nearby_station_text = ", ".join(
            f"{poi.get('block_id', '?')}@{poi.get('distance', '?')}m"
            for poi in nearby_workstations[:8]
        ) or "无"
        nearby_storage_text = ", ".join(
            f"{poi.get('block_id', '?')}@{poi.get('distance', '?')}m"
            for poi in nearby_storage[:8]
        ) or "无"
        storage_contents_text = "\n".join(
            f"  - {poi.get('block_id', '?')}@{poi.get('distance', '?')}m: "
            + ", ".join(f"{c.get('item_id', '?')}x{c.get('count', 1)}" for c in (poi.get('contents') or [])[:6])
            for poi in nearby_storage[:6]
            if poi.get('contents')
        ) or "无"

        vision_text = (context.get("vision") or "").strip() if isinstance(context, dict) else ""
        if not vision_text:
            vision_text = "无（视觉未开启）"

        known_poi_text = (context.get("known_poi") or "").strip() if isinstance(context, dict) else ""
        if not known_poi_text:
            known_poi_text = "暂无"

        territory_text = self._render_territory(context)
        # 死亡点（模组报的事实 + 后端记的那份，最新在前）：**只摆事实**，绝不自动回去。
        # 没有任何死亡记录 ⇒ 整段不出现（绝不凭空编一个"上次死在哪"）。
        death_point_text = self._render_death_point(context)

        # 借用授权（L1，见 `_render_borrow_authorization`）+ 当前欠账（L2：每轮都变）。
        borrow_pending_text = (context.get("borrow_pending") or "").strip() if isinstance(context, dict) else ""
        if borrow_pending_text:
            borrow_pending_text = f"\n- 现在还没还：{borrow_pending_text}（用完就放回去，别带着跑）"
        borrow_debt_text = ("欠账（借了还没还）：" + borrow_pending_text) if borrow_pending_text else ""

        # 时间 / 夜晚 / 能否睡觉：state_update 顶层字段（也兼容塞在 world 里）。
        world_ctx = context.get("world", {}) if isinstance(context, dict) else {}
        if not isinstance(world_ctx, dict):
            world_ctx = {}

        def _ctx_value(key):
            if isinstance(context, dict) and context.get(key) is not None:
                return context.get(key)
            return world_ctx.get(key)

        is_night = bool(_ctx_value("is_night"))
        day_time = _ctx_value("day_time")
        sky_darken = _ctx_value("sky_darken")
        can_sleep = bool(_ctx_value("can_sleep"))
        night_text = "是夜晚" if is_night else "是白天"
        day_time_text = str(day_time) if day_time is not None else "未知"
        sky_darken_text = str(sky_darken) if sky_darken is not None else "未知"
        sleepable_text = "现在能睡觉" if can_sleep else "现在不能睡觉"

        nearby_beds = context.get("nearby_beds", []) if isinstance(context, dict) else []
        nearby_bed_text = ", ".join(
            f"{bed.get('id', bed.get('block_id', '?'))}"
            f"@({bed.get('x')},{bed.get('y')},{bed.get('z')}) {bed.get('distance', '?')}m"
            for bed in nearby_beds[:4] if isinstance(bed, dict)
        ) or "暂无"

        remembered_pois = context.get("remembered_pois", []) if isinstance(context, dict) else []
        remembered_text = ", ".join(
            f"{poi.get('name') or poi.get('id', '?')}"
            f"[{poi.get('category') or 'waypoint'}]"
            f"@({poi.get('x')},{poi.get('y')},{poi.get('z')})"
            for poi in remembered_pois[:16] if isinstance(poi, dict)
        ) or "暂无"

        # 在线玩家 + 最近聊天：判断"他在跟谁说话"靠的是这两块，而不是每次都要喊名字。
        last_speaker = context.get("last_speaker") if isinstance(context, dict) else None
        last_speaker = last_speaker if isinstance(last_speaker, dict) else {}
        players = context.get("players") if isinstance(context, dict) else None
        players = players if isinstance(players, list) else []
        players_text = "\n".join(
            "- {name}{where}{who}".format(
                name=player.get("name", "?"),
                where=f"（距你 {float(player.get('distance')):.1f}m）"
                if isinstance(player.get("distance"), (int, float)) else "",
                who=" ← 最近跟你说话的就是他" if player.get("speaking") else "",
            )
            for player in players[:8] if isinstance(player, dict)
        ) or "（视野里没有别的玩家）"

        recent_chat = context.get("recent_chat") if isinstance(context, dict) else None
        recent_chat = recent_chat if isinstance(recent_chat, list) else []
        now = time.time()

        def _ago(entry: dict) -> str:
            stamp = entry.get("time")
            if not isinstance(stamp, (int, float)):
                return "?"
            delta = max(0.0, now - float(stamp))
            if delta < 3:
                return "刚刚"
            if delta < 60:
                return f"{delta:.0f}秒前"
            return f"{delta / 60:.0f}分钟前"

        recent_chat_text = "\n".join(
            "({when}) {sender}{dist}: {message}".format(
                when=_ago(entry),
                sender="[系统]" if entry.get("is_system") else (entry.get("sender") or "?"),
                dist=f"（距你 {float(entry.get('sender_distance')):.1f}m）"
                if isinstance(entry.get("sender_distance"), (int, float)) else "",
                message=entry.get("message", ""),
            )
            for entry in recent_chat[-8:] if isinstance(entry, dict)
        ) or "（还没人说过话）"

        speaker_line = ""
        if last_speaker.get("name"):
            where = ""
            if isinstance(last_speaker.get("x"), (int, float)) and isinstance(last_speaker.get("z"), (int, float)):
                where = f"，说话时站在 ({float(last_speaker['x']):.0f}, {float(last_speaker.get('y') or 0):.0f}, {float(last_speaker['z']):.0f})"
            speaker_line = f"- 最近发话者：{last_speaker['name']}{where}\n"

        # L2 的状态块：人设三段已进 L1（system），这里只留**会变**的 state_block，
        # 绝不与身份混写（"状态不是身份"）。
        persona_blocks = context.get("persona_blocks") if isinstance(context, dict) else None
        persona_blocks = persona_blocks if isinstance(persona_blocks, dict) else {}
        state_block = str(persona_blocks.get("state_block") or "").strip()
        state_block_text = (f"【此刻状态（会变，不是身份）】{state_block}" if state_block else "")

        # HUD 描述词（模组报回来的当前 HUD 文案）：说话/改 HUD 前先知道现在显示什么。
        hud_note = str(context.get("hud_note") or "").strip() if isinstance(context, dict) else ""
        hud_note_text = f"当前 HUD 描述词：{hud_note}" if hud_note else ""

        # 私信（悄悄话）：这条话是**定向**的，回话也该回私信（不要当着全服说）。
        whisper_from = str(context.get("whisper_from") or "").strip() if isinstance(context, dict) else ""
        whisper_text = (
            f"这条是 {whisper_from} 发来的**私信**（悄悄话）：只有你俩看得到。"
            f"要回就用 send_whisper 回给 {whisper_from}，并把 reply 留空（别在公开聊天里说）。"
            if whisper_from else ""
        )

        # 对话记忆（按服务器分区的 L1/L2/L3）：死亡、进出也在同一份记忆里。
        conversation = context.get("conversation") if isinstance(context, dict) else None
        conversation = conversation if isinstance(conversation, dict) else {}

        def _ago_ts(entry: dict) -> str:
            stamp = entry.get("ts") if isinstance(entry, dict) else None
            if not isinstance(stamp, (int, float)):
                return "?"
            delta = max(0.0, now - float(stamp))
            if delta < 3:
                return "刚刚"
            if delta < 60:
                return f"{delta:.0f}秒前"
            return f"{delta / 60:.0f}分钟前"

        memory_lines = []
        for entry in (conversation.get("recent") or [])[-12:]:
            if not isinstance(entry, dict):
                continue
            kind = str(entry.get("type") or "chat")
            who = str(entry.get("sender") or "?")
            text = str(entry.get("message") or "")
            if kind == "chat":
                memory_lines.append(f"({_ago_ts(entry)}) {who}: {text}")
            else:
                memory_lines.append(f"({_ago_ts(entry)}) [{'我' if entry.get('is_self') else kind}] {who} {text}".strip())
        memory_text = "\n".join(memory_lines) or "（本服务器还没有对话记录）"
        keywords = conversation.get("keywords") or []
        keywords_text = ", ".join(str(k) for k in keywords[:12]) or "无"
        summary_text = str(conversation.get("summary") or "").strip() or "（还没到需要摘要的量）"
        namespace = str(conversation.get("namespace") or "unknown")

        # 外部事件注入：不是"必发消息"，模型完全可以 no_action。
        # （"怎么对待事件"的规则是静态的，在 L1 的规则表里。）
        event = context.get("event") if isinstance(context, dict) else None
        event = event if isinstance(event, dict) else {}
        event_section = ""
        if event:
            event_section = (
                "\n外部事件（**不是**玩家直接对你说的话，只是世界里发生了一件事）：\n"
                f"{json.dumps(event, ensure_ascii=False)}\n"
                "要不要说点什么完全由你决定：不想说就把 reply 留空、candidates 给空数组——"
                "硬找话聊比不说话更糟。\n"
            )

        # 饥饿/饱和度：自然回血要"饱食度 ≥18 且饱和度 >0"，所以缺血时不能等饿到见底才吃。
        player_ctx = context.get("player", {}) if isinstance(context, dict) else {}
        if not isinstance(player_ctx, dict):
            player_ctx = {}
        hunger_value = _ctx_value("food") if _ctx_value("food") is not None else player_ctx.get("hunger")
        saturation_value = _ctx_value("saturation")
        if saturation_value is None:
            saturation_value = player_ctx.get("saturation")
        health_value = player_ctx.get("health")
        max_health_value = player_ctx.get("max_health", 20)
        hunger_text = str(hunger_value) if hunger_value is not None else "未知"
        saturation_text = str(saturation_value) if saturation_value is not None else "未知"
        health_text = (f"{health_value}/{max_health_value}"
                       if health_value is not None else "未知")

        # L2 只在**当轮 user 消息**里；工具表、规则、人设都不在这里（分别走 tools 与 system）。
        extras = "\n".join(part for part in (state_block_text, hud_note_text, whisper_text) if part)
        extras_text = f"{extras}\n" if extras else ""

        # 编排轮次的**紧凑进展摘要**（§10.2 #3）：只在编排轮出现，**只进 L2**（绝不进 L0/L1）。
        orchestration_text = (context.get("orchestration") or "").strip() if isinstance(context, dict) else ""
        orchestration_section = f"\n{orchestration_text}\n" if orchestration_text else ""

        prompt = f"""当前状态：
- 发送者：{sender}
- 消息：{message}
{event_section}
在线玩家（按距离，标记出最近跟你说话的那个人）：
{players_text}
{speaker_line}
最近聊天上下文（按时间从早到晚；"刚刚/N秒前"是距现在多久。判断"这话是不是对你说的/在跟谁说"看这里，
不一定要有人喊你的名字才算对你说）：
{recent_chat_text}

最近对话：
{recent_text}

记忆上下文：
{memory_context.get('interaction_summary', '无')}

对话记忆（服务器 {namespace}；L2 窗口 + L3 中期摘要 + 关键词。
判断"他之前说过什么/这个服务器上发生过什么"看这里）：
{memory_text}
中期摘要：{summary_text}
关键词：{keywords_text}

{extras_text}当前任务状态：
{context.get('task_state', {'kind': 'idle', 'status': 'idle'})}
{orchestration_section}
饥饿与回血（当前值；怎么解释这些数字见系统提示里的规则）：
- 血量 {health_text}｜饱食度 {hunger_text}/20｜饱和度 {saturation_text}（饱和度是隐藏值）

当前库存摘要：
{inventory_text}

附近资源：
- 方块：{nearby_block_text}
- 掉落物：{nearby_item_text}
  （捡不捡**你自己判断**：走过去踩到就会自动捡起；括号里是模组报的事实 —— 谁丢的、掉多久了、
  还有多久消失。谁丢的通常有意义：主人刚丢给你的就是要你拿的，别人掉的就先问一句；
  背包满了/太远/快消失了就别白跑。）
  （坐标口径：默认只上报**要处理的（威胁）/ 人（玩家）**的位置，其余档位只报计数 ——
  **「摘要里没有」不等于「附近没有」**；需要细节就**按需扫描**（实体 `scan_entities`、
  方块 `find_block`），别自己把"没报"读成"没有"。）

最近一次按需扫描（`scan_entities` 的回执；摘要里没坐标的档位只能这样拿明细）：
{scan_text}

附近工作站：
{nearby_station_text}

附近仓储：
{nearby_storage_text}

仓库已知内容：
{storage_contents_text}

{borrow_debt_text}

已知箱子（历史记忆，可能不在当前视野内）：
{known_poi_text}

{territory_text}

时间与睡眠：
- 现在是{night_text}（sky_darken={sky_darken_text}，{sleepable_text}）；day_time={day_time_text}
- 附近的床：{nearby_bed_text}

已知记录点（你自己记住的持久记忆，可能在很远的地方）：
{remembered_text}

{death_point_text}
附近实体（`tier:名字@距离`（+带坐标的档位后面跟 `(x,y,z)`）；tier 是模组按同一张表算的对待档：
dangerous 高危险=不主动打也不还手、hostile 敌对=该处理、neutral 中立=不惹（它先动手才还手）、
passive 被动=能猎的肉、unknown 认不出=先别当敌人）：
{nearby_entity_text}

画面观察（来自截图，可能略有延迟）：
{vision_text}
"""

        return prompt

    # ── 两阶段决策：第二阶段（decider 打分选择） ──────────────────

    def _execute_decision(self, plan: dict, context: dict) -> Optional[str]:
        """用 decider 从 planner 给出的候选里选一个执行。

        只看置信度：低于 DECISION_CONFIDENCE_THRESHOLD 就不下发动作，把这一轮
        让给 P0/P1 反射层与下一轮规划，避免在没把握时乱动。
        """
        reply_text = plan.get("reply") or None
        candidates = [c for c in plan.get("candidates", [])
                      if isinstance(c, dict) and c.get("tool")]

        if not candidates:
            self._last_decision = {"source": "planner_only", "reason": "no candidates"}
            logger.info("[Planner] 无候选动作，只回复")
            return reply_text

        choice, confidence = self._decide(candidates, context)
        threshold = self._threshold()
        self._last_decision = {
            "source": "decider",
            "choice": choice,
            "confidence": round(confidence, 3),
            "candidate_count": len(candidates),
            "threshold": threshold,
        }

        if choice is None or confidence < threshold:
            logger.info(
                "[Planner] 决策置信度不足 (%.2f < %.2f)，不下发动作，交给反射层",
                confidence, threshold,
            )
            return reply_text

        candidate = candidates[choice]
        tool = str(candidate.get("tool", "")).strip().lower()
        args = candidate.get("args") if isinstance(candidate.get("args"), dict) else {}
        executed = self._dispatch_tool_call(tool, args, context)
        # 记进 last_decision，供 /api/status 与前端展示
        self._last_decision["tool"] = tool
        self._last_decision["executed"] = bool(executed)
        logger.info("[Planner] 决策执行 %s(%s) conf=%.2f -> %s",
                    tool, args, confidence, executed)
        return reply_text

    def _decide(self, candidates: list[dict], context: dict) -> tuple[Optional[int], float]:
        """调用 decider 角色选一个候选，返回 (编号, 置信度)。

        超时、报错或输出无法解析都视为"没有结论"，返回 (None, 0.0) 由调用方回退。
        """
        if not self.llm or not self.llm.api_key:
            return None, 0.0

        prompt = self._build_decider_prompt(candidates, context)
        try:
            result = self.llm.chat(
                [{"role": "system", "content": L0_DECIDER_CONTRACT},
                 {"role": "user", "content": prompt}],
                agent="decider",
                temperature=DECIDER_TEMPERATURE,
                max_tokens=DECIDER_MAX_TOKENS,
                timeout=DECIDER_TIMEOUT_SECONDS,
                # 零思考：不发任何思考模式/深度字段，直接要一行 JSON。这是标准做法，
                # 不依赖专有决策模型（System One 是可选升级，见 docs/dev/ROADMAP.md）。
                thinking=False,
            )
        except Exception as e:
            logger.warning("[Planner] Decider 调用失败或超时: %s", e)
            return None, 0.0

        raw = (result.get("content") or "").strip()
        verdict = self._parse_decision(raw, len(candidates))
        if verdict is None:
            logger.warning("[Planner] Decider 输出无法解析: %s", raw[:80])
            return None, 0.0
        return verdict

    def _build_decider_prompt(self, candidates: list[dict], context: dict) -> str:
        """L2：decider 的当轮动态（状态摘要 + 编号候选）；输出契约在 L0 system 里。"""
        player = context.get("player", {}) if isinstance(context, dict) else {}
        task_state = context.get("task_state", {}) if isinstance(context, dict) else {}
        speaker = context.get("last_speaker") if isinstance(context, dict) else None
        speaker_name = speaker.get("name") if isinstance(speaker, dict) else ""
        speaker_text = f"，最近发话者 {speaker_name}" if speaker_name else ""
        lines = []
        for i, candidate in enumerate(candidates):
            args = json.dumps(candidate.get("args", {}), ensure_ascii=False)
            lines.append(f"{i}. {candidate.get('tool')}({args}) — {candidate.get('why', '')}")
        options_text = "\n".join(lines)
        return (f"当前状态：血量 {player.get('health', '?')}/{player.get('max_health', 20)}，"
                f"饥饿 {player.get('hunger', '?')}/20，饱和度 {player.get('saturation', '?')}，"
                f"任务 {task_state.get('kind', 'idle')}/{task_state.get('status', 'idle')}{speaker_text}\n\n"
                f"候选动作：\n{options_text}")

    @staticmethod
    def _plan_from_tool_calls(tool_calls: Any) -> Optional[dict]:
        """把供应商的原生 `tool_calls` 折算成候选动作（L3 走 tools 参数后的合法回法）。

        参数可能是 JSON 字符串（OpenAI 约定）或已是对象；解析不了一律给空 args ——
        **绝不猜参数**（猜出来的坐标/数量会直接下发到游戏里）。
        """
        candidates = []
        for call in tool_calls or []:
            if not isinstance(call, dict):
                continue
            function = call.get("function")
            function = function if isinstance(function, dict) else {}
            name = str(function.get("name") or "").strip()
            if not name:
                continue
            args = function.get("arguments")
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {}
            candidates.append({
                "tool": name,
                "args": args if isinstance(args, dict) else {},
                "why": "模型直接调用",
            })
        if not candidates:
            return None
        return {"reply": "", "candidates": candidates}

    @staticmethod
    def _parse_plan_json(text: str) -> Optional[dict]:
        """从模型输出中抽取候选 JSON；不符合契约返回 None。"""
        cleaned = text.strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```[a-zA-Z]*\s*", "", cleaned)
            cleaned = re.sub(r"\s*```$", "", cleaned).strip()
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            data = json.loads(cleaned[start:end + 1])
        except json.JSONDecodeError:
            return None
        if not isinstance(data, dict) or not isinstance(data.get("candidates"), list):
            return None
        return data

    @staticmethod
    def _parse_decision(text: str, candidate_count: int) -> Optional[tuple[int, float]]:
        """解析 decider 的单行 JSON，返回 (编号, 置信度)；越界或非法返回 None。"""
        if not text:
            return None
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            data = json.loads(text[start:end + 1])
            choice = int(data["choice"])
            confidence = float(data.get("confidence", 0.0))
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            return None
        if choice < 0 or choice >= candidate_count:
            return None
        return choice, max(0.0, min(1.0, confidence))

    def _execute_plan(self, plan_text: str, sender: str, message: str, 
                      context: dict) -> Optional[str]:
        """执行规划结果。"""
        plan_lower = plan_text.lower()
        reply_text: Optional[str] = None
        executed_any_action = self._execute_tool_calls(plan_text, context)
        
        # 回复
        if "reply(" in plan_lower:
            content = self._extract_between(plan_text, "reply(", ")")
            if content:
                reply_text = content.strip()
        
        # 移动
        if "move_to(" in plan_lower:
            coords = self._extract_coords(plan_text, "move_to(")
            if coords and len(coords) >= 3:
                self.skills.move_to(coords[0], coords[1], coords[2])
                logger.info("[Planner] 移动到 (%.0f, %.0f, %.0f)", coords[0], coords[1], coords[2])
                executed_any_action = True
        
        # 跟随玩家
        if "follow(" in plan_lower:
            player_name = self._extract_between(plan_text, "follow(", ")")
            if player_name:
                normalized_target = player_name.strip()
                if not self._is_duplicate_task(context, {"follow"}, normalized_target):
                    self.skills.follow_player(normalized_target)
                    logger.info("[Planner] 跟随 %s", normalized_target)
                    executed_any_action = True
        
        # 攻击
        if "attack()" in plan_lower:
            self.skills.attack()
            logger.info("[Planner] 攻击")
            executed_any_action = True
        
        # 挖掘
        if "mine_block()" in plan_lower or "mine(" in plan_lower:
            self.skills.mine_block()
            logger.info("[Planner] 挖掘")
            executed_any_action = True
        
        # 放置方块
        if "place_block()" in plan_lower or "place(" in plan_lower:
            self.skills.place_block()
            logger.info("[Planner] 放置")
            executed_any_action = True
        
        # 合成（自由文本回退路径：同样要过授权 + 库存优先那道门）
        if "craft(" in plan_lower:
            item_name = self._extract_between(plan_text, "craft(", ")")
            if item_name:
                parts = [part.strip() for part in item_name.split(",") if part.strip()]
                normalized_item = self._normalize_item_name(parts[0])
                craft_count = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 1
                if self._dispatch_craft({"item": normalized_item, "count": craft_count},
                                        context, command="craft_via_recipe"):
                    logger.info("[Planner] 合成 %s x%d", normalized_item, craft_count)
                    executed_any_action = True
        
        # 收集
        if "collect(" in plan_lower:
            args = self._extract_between(plan_text, "collect(", ")")
            if args:
                parts = args.split(",")
                block_type = parts[0].strip() if len(parts) > 0 else ""
                count = int(parts[1].strip()) if len(parts) > 1 else 1
                normalized_block = self._normalize_block_name(block_type)
                if not self._is_duplicate_task(context, {"collect"}, normalized_block):
                    self.skills.collect_blocks(normalized_block, count)
                    logger.info("[Planner] 收集 %s x%d", normalized_block, count)
                    executed_any_action = True
        
        # 装备
        if "equip(" in plan_lower:
            slot = self._extract_between(plan_text, "equip(", ")")
            if slot:
                self.skills.equip(slot.strip())
                logger.info("[Planner] 装备 %s", slot.strip())
                executed_any_action = True
        
        # 查看背包
        if "get_inventory()" in plan_lower:
            self.skills.get_inventory()
            logger.info("[Planner] 查看背包")
            executed_any_action = True
        
        # 探索
        if "explore(" in plan_lower:
            radius_str = self._extract_between(plan_text, "explore(", ")")
            if radius_str:
                try:
                    radius = int(radius_str.strip())
                    self.skills.explore(radius)
                    logger.info("[Planner] 探索 %d 格", radius)
                except ValueError:
                    self.skills.explore(16)
                executed_any_action = True
        
        # 交易
        if "trade(" in plan_lower:
            trade_arg = self._extract_between(plan_text, "trade(", ")")
            if trade_arg:
                # 旧写法 `trade(名称)`：现在按**结果物**理解（模组侧参数就是 item ✓）。
                self.skills.trade(item=trade_arg.strip())
                logger.info("[Planner] 交易（按结果物）%s", trade_arg.strip())
                executed_any_action = True
        
        # 睡觉
        if "sleep()" in plan_lower:
            self.skills.sleep()
            logger.info("[Planner] 睡觉")
            executed_any_action = True
        
        # 吃东西
        if "eat()" in plan_lower:
            self.skills.eat()
            logger.info("[Planner] 吃东西")
            executed_any_action = True
        
        # 丢弃物品
        if "drop(" in plan_lower:
            args = self._extract_between(plan_text, "drop(", ")")
            if args:
                parts = args.split(",")
                item = parts[0].strip() if len(parts) > 0 else ""
                count = int(parts[1].strip()) if len(parts) > 1 else 1
                self.skills.drop_item(item, count)
                logger.info("[Planner] 丢弃 %s x%d", item, count)
                executed_any_action = True
        
        # 整理背包
        if "sort_inventory()" in plan_lower:
            self.skills.sort_inventory()
            logger.info("[Planner] 整理背包")
            executed_any_action = True

        # 建造
        if "build(" in plan_lower:
            args = self._extract_between(plan_text, "build(", ")")
            if args:
                parts = args.split(",")
                if len(parts) >= 4:
                    try:
                        x, y, z = float(parts[0]), float(parts[1]), float(parts[2])
                        structure = parts[3].strip()
                        # 材料现在**必须由模型点名**（本地不做启发式 ✗）⇒ 旧写法没有材料，
                        # 就让它如实拿到 `need_material`，而不是在这里编一种方块 ✗。
                        self.skills.build(x, y, z, structure)
                        logger.info("[Planner] 建造 %s 在 (%.0f, %.0f, %.0f)（材料未点名 ⇒ 会如实要求）",
                                structure, x, y, z)
                        executed_any_action = True
                    except ValueError:
                        pass

        if "stop()" in plan_lower:
            self.skills.stop_all()
            logger.info("[Planner] 停止所有动作")
            executed_any_action = True
        
        # 结束
        if "finish()" in plan_lower:
            logger.info("[Planner] 结束本轮")
            return reply_text

        if executed_any_action:
            return reply_text

        if reply_text:
            return reply_text
        
        # 如果没有匹配到任何动作，将整个文本作为回复
        logger.info("[Planner] 未匹配到动作，作为回复: %s", plan_text[:50])
        return plan_text
    
    def _extract_between(self, text: str, start_marker: str, end_marker: str) -> Optional[str]:
        """提取两个标记之间的内容。"""
        start = text.find(start_marker)
        if start < 0:
            return None
        content_start = start + len(start_marker)
        end = text.find(end_marker, content_start)
        if end < 0:
            return None
        return text[content_start:end]
    
    def _extract_coords(self, text: str, marker: str) -> Optional[list]:
        """提取坐标参数。"""
        content = self._extract_between(text, marker, ")")
        if not content:
            return None
        try:
            coords = [float(c.strip()) for c in content.split(",")]
            return coords
        except ValueError:
            return None

    def _execute_tool_calls(self, text: str, context: dict) -> bool:
        executed = False
        for match in TOOL_CALL_RE.finditer(text):
            tool_name = match.group(1).strip().lower()
            payload_text = match.group(2).strip()
            try:
                payload = json.loads(payload_text)
            except json.JSONDecodeError:
                logger.warning("[Planner] 无法解析 tool 调用参数: %s", payload_text[:120])
                continue

            if self._dispatch_tool_call(tool_name, payload, context):
                executed = True
        return executed

    def _dispatch_tool_call(self, tool_name: str, payload: dict, context: dict) -> bool:
        """工具派发：**注册表驱动**（单一事实来源）。

        1) 查注册表 `TOOL_BY_NAME`（未登记的工具直接拒绝——没有第二份清单可漏）；
        2) 由注册表的权限描述符做一次**统一授权**（`_authorize_tool`）；
        3) 调注册表登记的 handler（指向现有技能函数）。
        """
        spec = TOOL_BY_NAME.get(str(tool_name or "").strip().lower())
        if spec is None:
            logger.debug("[Planner] 未登记的工具：%s", tool_name)
            return False
        payload = payload if isinstance(payload, dict) else {}
        # 这一条调用的"被哪道门拦下"从零开始记（复合体要把门的原因如实转发出来）。
        self._last_tool_gate = ""
        if not self._authorize_tool(spec, payload, context):
            return False
        # 资源仲裁（同资源互斥、异资源并行、P0 优先）：只**仲裁**，不新增能力。
        proceed, lease_owner = self._begin_tool_lease(spec, payload, context)
        if not proceed:
            return False
        handler = getattr(self, spec.handler, None)
        if handler is None:
            logger.warning("[Planner] 工具 %s 登记的 handler %s 不存在", spec.name, spec.handler)
            self._end_tool_lease(lease_owner)
            return False
        before_label = self.action_manager.current_label if self.action_manager is not None else None
        try:
            handled = bool(handler(payload, context))
        finally:
            self._end_tool_lease(lease_owner)
        if handled:
            self._maybe_register_job(spec, payload, context, before_label)
        return handled

    # ── job 句柄接线（只登记"模型已请求的调用"；卡死判定交给 state_governor）──

    def _maybe_register_job(self, spec: ToolSpec, payload: dict, context: dict,
                            before_label: Optional[str]) -> None:
        """长任务调用 → 登记成 job（四样门槛齐备；缺一即不登记，绝不半成品）。

        进度来源 / 终止条件都读**现成**的 action_manager（不新造通道）：
        终止条件到点就把真实结果（含失败 code）落进 job；取消路径就是 ``action_manager.stop``。
        """
        registry = self.job_registry
        action_manager = self.action_manager
        if registry is None or action_manager is None or not spec.job:
            return
        if spec.name in COMPOSITE_BY_NAME:
            # 复合体**自己**登记 job（进度按步、取消到步、失败 code = 那一步的 code）——
            # 不在这里再登记一个"整条调用"的 job，免得同一个调用出现两条账。
            return
        label = action_manager.current_label
        if not label or label == before_label or not action_manager.is_busy:
            return
        started = time.time()
        _tier, actor = self._actor_of(context)

        def _progress(_label: str = label) -> Optional[float]:
            current = action_manager.current
            if current is not None and current.label == _label:
                return current.progress
            return None

        def _terminal(_label: str = label, _started: float = started) -> Optional[dict]:
            record = action_manager.outcome_since(_label, _started)
            if record is None:
                return None
            if record.get("success"):
                return {"status": JOB_DONE, "result": {"detail": "ok"}}
            return {"status": JOB_FAILED, "code": "tool_failed",
                    "result": {"error": str(record.get("error") or "tool failed")}}

        def _cancel(_reason: str = "") -> None:
            action_manager.stop()

        try:
            registry.register(
                spec.name, progress=_progress, terminal=_terminal,
                failure="tool_failed", cancel=_cancel,
                resources=resources_for_tool(spec.name),
                priority=self._lease_priority(context), actor=actor,
                detail=f"{spec.name} {json.dumps(payload, ensure_ascii=False)}"[:200])
        except JobRejected as exc:
            logger.info("[Planner] %s 未登记为 job：%s", spec.name, exc.reason)

    # ── 资源租约接线（只仲裁；P0 反射走 survival 通道，永远不被本批挡住）────

    def _begin_tool_lease(self, spec: ToolSpec, payload: dict, context: dict) -> tuple[bool, Optional[str]]:
        """派发前按工具声明的资源请求租约。

        返回 ``(是否放行, 持有的 owner 或 None)``。被挡时把真实原因写进 ``_action_note``
        （诚实优先，绝不假装做了），并**一帧都不发**。
        """
        governor = self.state_governor
        if governor is not None and not governor.enabled():
            return True, None              # 总开关关掉 ⇒ 行为回到现在
        target = spec.name
        if governor is not None and governor.is_backed_off(target):
            wait = max(0.0, governor.backoff_until(target) - time.time())
            self._last_tool_gate = "backoff"
            self._action_note = f"（{target} 刚被判定卡死，退避中（还剩约 {wait:.0f}s），先不重试）"
            logger.info("[Planner] %s 在治理退避窗口内，暂不下发", target)
            return False, None
        resources = resources_for_tool(spec.name)
        if not resources:
            return True, None
        owner = f"tool:{spec.name}"
        result = self.resource_lease.request(
            owner, resources, priority=self._lease_priority(context),
            reason=f"工具 {spec.name}", kind=spec.name, target=target, actor="planner")
        if not result.get("granted"):
            blocked = "、".join(result.get("blocked_by") or []) or "别的状态"
            self._last_tool_gate = "resource"
            self._action_note = f"（{spec.name} 要用的资源正被 {blocked} 占用，本次已排队，暂不下发）"
            logger.info("[Planner] %s 资源冲突（被 %s 占用）→ 排队", spec.name, blocked)
            return False, None
        return True, owner

    def _end_tool_lease(self, owner: Optional[str]) -> None:
        """一次派发结束（成功或失败）就释放占用——工具是一次性下发，不长期占资源。"""
        if not owner:
            return
        try:
            self.resource_lease.release(owner, "工具派发结束")
        except Exception as exc:  # noqa: BLE001 — 释放失败不拖垮主链路
            logger.warning("[Planner] 释放资源租约失败: %s", exc)

    @staticmethod
    def _lease_priority(context: dict) -> int:
        """模型任务的优先级（按 task_list 的归属/优先级；缺省取普通值）。"""
        context = context if isinstance(context, dict) else {}
        raw = context.get("task_priority")
        if isinstance(raw, (int, float)) and not isinstance(raw, bool):
            return max(0, min(1000, int(raw)))
        task = context.get("task_state")
        if isinstance(task, dict):
            raw = task.get("priority")
            if isinstance(raw, (int, float)) and not isinstance(raw, bool):
                return max(0, min(1000, int(raw)))
        return PRIORITY_DEFAULT

    # ── 复合体（声明式，`agent/composites.py`；AGENT_ARCH §10.2 #4）──────
    #
    # 复合体在模型眼里就是**一条工具**；**定义**在 `agent/composites.py`（唯一事实来源），
    # 这里只是入口 + 落账：每一步都从**同一个** `_dispatch_tool_call` 出去 ⇒ 权限门
    # （tier / 容器 / 借用 / 区域保护）与资源租约**一步都不跳**；失败**不重试、不换路**，
    # 如实报出「第几步、哪一步、它自己的 code」+ **已完成的前几步**（§10.4 验收③）。

    def _composites_enabled(self) -> bool:
        """复合体总开关（`config.json → composites.enabled`，默认**开**）。"""
        provider = self._composites_enabled_provider
        if provider is None:
            return True
        try:
            return bool(provider())
        except Exception as exc:  # noqa: BLE001 — 开关读不到就按声明的默认（开）走
            logger.warning("[Planner] 复合体开关读取失败: %s", exc)
            return True

    def _run_composite(self, name: str, payload: dict, context: dict) -> bool:
        """跑一个声明式复合体：逐步走各自的门 + 登记 job（四样门槛齐备）+ 失败指到步。

        - 关掉开关 ⇒ **一步都不发**，如实说不干（行为回到"没有复合体"之前）；
        - job 句柄：进度=按步（单调）/ 终止条件=步走完或某步失败 / 失败 code=**那一步自己的
          code** / 取消路径=停当前步 + 不再往下（取消复用 `action_manager.stop`，不另造）；
        - 某步声明了 `await_check` 而条件没满足 ⇒ **挂起**（一步都不发，job 保持 running，
          等**下一轮**本来就要发生的轮次续跑；上限 `await_seconds` 到点就如实失败）；
        - 结果：`run.result()` 逐步账 + 卡在第几步，说明里写全（`self._composite_note`）。
        """
        comp = COMPOSITE_BY_NAME.get(str(name or "").strip())
        if comp is None:
            self._composite_note = f"（没有叫「{name}」的复合体）"
            logger.info("[Planner] 认不出的复合体：%s", name)
            return False
        if not self._composites_enabled():
            self._composite_note = (
                f"（复合体「{comp.name}」被关掉了（config.json → composites.enabled=false）："
                f"我就不按复合体跑，得一步一条地下）")
            logger.info("[Planner] 复合体 %s 被开关关掉：一步都不发", comp.name)
            return False
        if comp.name in self._composite_stack:
            self._composite_note = f"（复合体「{comp.name}」正在跑：不套着自己调自己）"
            logger.info("[Planner] 复合体 %s 递归调用被拒", comp.name)
            return False
        ctx = context if isinstance(context, dict) else {}
        args = {str(key): value for key, value in payload.items()} \
            if isinstance(payload, dict) else {}
        run = CompositeRun(comp, args, clock=self._composite_clock)
        job = self._register_composite_job(comp, run, args, ctx)
        if job is not None:
            run.job_id = job.id
        self._composite_stack.append(comp.name)
        try:
            run_composite(run, dispatch=self._composite_dispatch, context=ctx,
                          cancel_current=self._stop_current_action)
        except Exception as exc:  # noqa: BLE001 — 复合体自己坏掉也要留账，绝不假装成功
            logger.exception("[Planner] 复合体 %s 异常", comp.name)
            abort_composite_run(run, "composite_crashed", f"（复合体自己跑挂了：{exc}）")
        finally:
            self._composite_stack.pop()
        self._composite_last = run.result()
        self._composite_note = run.note or composite_note(run)
        if run.parked:
            # 挂起：**一步都不发了**，job 保持 running，记在册上等下一轮续跑（有界 ✓）。
            self._composite_runs[self._composite_run_key(run)] = run
            logger.info("[Planner] 复合体 %s 挂起在第 %d 步（等 %s）", comp.name,
                        run.waiting_index + 1, run.waiting_check)
        else:
            self._settle_composite_job(job, run)
        # 挂起也算"受理了"（与 haul 起一趟跨轮搬运同一个口径）；失败/取消才是 False。
        return run.status in (STATUS_DONE, "")

    def _composite_run_key(self, run: CompositeRun) -> str:
        """挂起账的键：有 job 就用 job id（**允许多个挂起并行** ✓），没有就自己发号。"""
        if run.job_id:
            return str(run.job_id)
        self._composite_seq += 1
        return f"{run.composite.name}#{self._composite_seq}"

    def home_bed_step(self, context: Optional[dict] = None) -> bool:
        """推进"把床绑成家"的链路（走过去 → 右键 → 用原版重生点核对）。

        用户纠正的语义：**右键一次床就会绑重生点**（任何时候都行，白天晴天一样）；
        "现在不能睡"只是不睡、不影响绑定。所以"记下床位"不算数，右键 + 核对才算数。
        结论只有两种：`home_bed_bound` 说重生点就是这张床（真的绑上了），或者如实报失败。
        """
        state = getattr(self, "_home_bed", None)
        if not isinstance(state, dict):
            return False

        outcome = home_bed.step(state, context or {})
        action = str(outcome.get("action") or "")
        skills = getattr(self, "skills", None)
        if skills is not None and action == "move_to":
            move = getattr(skills, "move_to", None)
            if callable(move):
                move(float(state["x"]), float(state["y"]), float(state["z"]))
        elif skills is not None and action == "use_on":
            look = getattr(skills, "look_at", None)
            if callable(look):
                look(float(state["x"]), float(state["y"]) + 0.5, float(state["z"]))
            use = getattr(skills, "use_on", None)
            if callable(use):
                use()

        note = str(outcome.get("note") or "")
        if outcome.get("done"):
            self._home_bed = None                     # 一次性账：结论出来就清
            self._territory_note = note
            logger.info("[Planner] 绑定床位结论：%s", note)
            return True
        if note:
            self._territory_note = note               # 中途也让模型看到"正在走过去/正在右键"
        return False

    def resume_composites(self, context: Optional[dict] = None) -> list[str]:
        """把**挂起**的复合体往前推（跨轮推进；**不加线程/定时器** ✗）。

        复用**既有轮次**语义：这一轮本来就要发生（玩家说话/状态变化触发），顺手把已经被
        模型调用过、正等条件的复合体推下去 —— 用**当轮的新 context** 重判那个 check：
        满足 ⇒ 从等那一步接着按声明往下发（每步照旧走 `_composite_dispatch` ⇒ 门一个不跳）；
        超时 ⇒ 如实失败（`await_timeout`）；被取消 ⇒ 不再往下。
        返回这一趟**收尾**（跑完/失败/取消/到期）的复合体名；仍在挂起的不算。
        """
        ctx = context if isinstance(context, dict) else {}
        advanced: list[str] = []
        for key in list(self._composite_runs):
            run = self._composite_runs[key]
            try:
                self._advance_parked_composite(run, ctx)
            except Exception as exc:  # noqa: BLE001 — 续跑坏了也要留账，不拖垮主链路
                logger.warning("[Planner] 复合体续跑失败(%s): %s", key, exc)
            if not run.parked:        # 跑完了/失败了/取消了/到期了 ⇒ 不再挂账（无僵尸 ✓）
                self._composite_runs.pop(key, None)
                advanced.append(run.composite.name)
        return advanced

    def _advance_parked_composite(self, run: CompositeRun, context: dict) -> None:
        """一个挂起 run 接着往下跑（跑到再挂起/失败/走完为止）或如实收尾。"""
        if run.status:                # 已被别处结算（例：jobs.refresh 判到到期）⇒ 只收尾
            self._composite_last = run.result()
            self._composite_note = run.note or composite_note(run)
            return
        if expire_run(run):           # 等超时 ⇒ 如实失败（第几步/哪个 check/等了多久）
            self._composite_last = run.result()
            self._composite_note = run.note
            self._settle_composite_job(self._job_of_composite(run), run)
            return
        run_composite(run, dispatch=self._composite_dispatch, context=context,
                      cancel_current=self._stop_current_action)
        self._composite_last = run.result()
        self._composite_note = run.note or composite_note(run)
        if not run.parked:
            self._settle_composite_job(self._job_of_composite(run), run)

    def _job_of_composite(self, run: CompositeRun):
        """这条挂起 run 对应的 job 句柄（没登记/没台账就是 None）。"""
        registry = self.job_registry
        if registry is None or not run.job_id:
            return None
        return registry.get(run.job_id)

    def parked_composites(self) -> list[dict]:
        """当前挂起的复合体（只读快照，供查看/测试；没有就空表）。"""
        return [run.result() for run in self._composite_runs.values()]

    def _abort_parked_composites(self, reason: str = "") -> int:
        """一条**新的指令**要停（或中断）⇒ 挂着的复合体也不许留僵尸 ✗。

        走**已有**那条停的路（`_stop_current_action`）；取消后**一步都不再发**，
        job 如实记 cancelled。
        """
        count = 0
        for key in list(self._composite_runs):
            run = self._composite_runs.pop(key, None)
            if run is None:
                continue
            self._stop_current_action()          # 复用既有停止路径（不新造取消通道）
            run.cancel(reason or "新的指令")
            run_composite(run, dispatch=self._composite_dispatch, context={},
                          cancel_current=None)   # 只把"取消"记进账：一步都不发
            self._composite_last = run.result()
            self._composite_note = run.note or composite_note(run)
            self._settle_composite_job(self._job_of_composite(run), run)
            count += 1
        if count:
            logger.info("[Planner] 中止了 %d 个挂起的复合体（%s）", count, reason)
        return count

    def _register_composite_job(self, comp, run: CompositeRun, args: dict, context: dict):
        """登记复合体的 job（四样门槛齐备；缺一就干脆不登记，绝不半成品）。"""
        registry = self.job_registry
        if registry is None:
            return None
        _tier, actor = self._actor_of(context)
        try:
            return registry.register(
                comp.name, progress=run.progress, terminal=run.terminal,
                failure=DEFAULT_FAILURE_CODE, cancel=self._composite_cancel_path(run),
                priority=self._lease_priority(context), actor=actor,
                detail=f"{comp.name} {json.dumps(args, ensure_ascii=False)}"[:200])
        except JobRejected as exc:
            logger.info("[Planner] 复合体 %s 未登记为 job：%s", comp.name, exc.reason)
            return None

    def _composite_cancel_path(self, run: CompositeRun):
        """job 的**取消路径**：停当前步（复用已有那条停路）+ 标取消 ⇒ 挂起的也不留僵尸。"""

        def _cancel(reason: str = "") -> None:
            if run.cancelled:            # 已经取消过（例子：新指令那条路先停过了）⇒ 别再停一次
                return
            run.cancel(reason)
            run.take_cancel()            # 这里已经停过了，别让引擎再停一次
            self._stop_current_action()

        return _cancel

    def _settle_composite_job(self, job, run: CompositeRun) -> None:
        """把跑完的复合体落进 job 台账（失败带**那一步的 code**；结果带逐步账）。"""
        registry = self.job_registry
        if job is None or registry is None:
            return
        if run.status == JOB_DONE:
            registry.finish(job.id, result=run.result())
        elif run.status == JOB_FAILED:
            registry.fail(job.id, code=run.failure(),
                          message=f"卡在第 {run.failed_index} 步 {run.failed_step}",
                          result=run.result())
        else:
            registry.cancel(job.id, reason=run.cancel_reason or "cancelled")

    def _composite_dispatch(self, tool: str, payload: dict, context: dict):
        """复合体的一步：走**同一个**派发口（注册表 → 权限门 → 资源租约 → handler）。

        返回 `True` 或 `{ok: False, code, detail}`——`detail` 是**那一步自己**新写的如实说明
        （门/工具的原话，一字不改地转发；不重试、不换路）。
        """
        before = self._notes_snapshot()
        ok = self._dispatch_tool_call(tool, payload, context)
        if ok:
            return True
        return {"ok": False, "code": self._composite_failure_code(tool),
                "detail": self._note_delta(before)}

    def _composite_failure_code(self, tool: str) -> str:
        """失败那一步**自己的 code**：先看被哪道门拦下，再看工具自己的回执（沿用它的口径）。"""
        gate = GATE_CODES.get(str(self._last_tool_gate or ""))
        if gate:
            return gate
        receipt = self._haul_last if str(tool) == "haul" else None
        if isinstance(receipt, dict) and receipt.get("reason"):
            return f"{tool}:{receipt['reason']}"
        return CODE_TOOL

    def _notes_snapshot(self) -> tuple:
        """本轮五条"如实说明"的当前值（比对一步前后，取**这一步自己**新写的那条）。"""
        return (self._craft_note, self._action_note, self._territory_note,
                self._haul_note, self._farm_note)

    def _note_delta(self, before: tuple) -> str:
        """这一步**自己**新写的如实说明（取"变了的那条"；一步没写就给空串，绝不抄旧话 ✗）。"""
        now = self._notes_snapshot()
        for old, new in zip(before, now):
            if new and new != old:
                return str(new)
        return ""

    def _stop_current_action(self) -> None:
        """复合体取消时"停当前步"的落点：复用既有停止路径（不新造取消通道）。"""
        manager = self.action_manager
        if manager is not None:
            manager.stop()
            return
        stop = getattr(self.skills, "stop_all", None)
        if callable(stop):
            stop()

    # 入口（`agent/tool_registry.py` 按复合体声明生成 handler 名；一条声明 = 一个两行入口）
    def _dispatch_composite_make_bed(self, payload: dict, context: dict) -> bool:
        return self._run_composite("make_bed", payload, context)

    def _dispatch_composite_deposit_all(self, payload: dict, context: dict) -> bool:
        return self._run_composite("deposit_all", payload, context)

    # ── 统一授权（注册表权限字段驱动）──────────────────────────
    def _authorize_tool(self, spec: ToolSpec, payload: dict, context: dict) -> bool:
        """派发前的**唯一**授权点：tier 门 + 容器访问授权。

        权限由注册表 `spec.permission` 声明，这里统一执行——不再散在各 handler 里。
        被拒时把原因写成一句如实说明（回复会带上，绝不假装做了）。
        """
        perm = spec.permission
        if perm.tier and not self._has_capability(perm.tier, context):
            self._last_tool_gate = "permission"
            if perm.tier == "craft":
                self._craft_note = self._auth_note(self._craft_policy(context), "造东西")
            else:
                self._territory_note = self._no_authority_note(
                    perm.deny_label or spec.name, context)
            logger.info("[Planner] 工具 %s 被拒：%s 权限不足", spec.name, perm.tier)
            return False
        if perm.container:
            access = self._container_read_access if perm.read_only else self._container_access
            ok, note, _cpolicy, crow = access(context)
            if not ok:
                self._last_tool_gate = "container"
                self._craft_note = note
                logger.info("[Planner] 工具 %s 被拒：容器访问未授权 %s",
                            spec.name, (crow or {}).get("name"))
                return False
        return True

    def _has_capability(self, kind: str, context: dict) -> bool:
        """查权限表的能力位（kind ∈ craft | containers | territory | zones）。

        没权限表来源 → 一律不放行（默认拒绝）；认不出的 kind 也按拒绝处理。
        """
        tier, _ = self._actor_of(context)
        store = self.permissions
        if store is None:
            return False
        checks = {
            "craft": store.can_craft,
            "containers": store.can_authorize_containers,
            "territory": store.can_authorize_territory,
            "zones": store.can_define_zones,
        }
        check = checks.get(str(kind or ""))
        if check is None:
            logger.warning("[Planner] 未知能力位：%s", kind)
            return False
        try:
            return bool(check(tier))
        except Exception as exc:  # noqa: BLE001
            logger.warning("[Planner] 能力位 %s 查询失败: %s", kind, exc)
            return False

    def _container_access(self, context: dict) -> tuple[bool, str, str, Optional[dict]]:
        """容器访问的**唯一**判定：no_touch 拒、free 随取、其余看翻箱授权。

        返回 `(是否放行, 被拒时的如实说明, 该箱策略, 该箱登记行)`。
        """
        cpolicy, crow = self._container_policy(context)
        if cpolicy == POLICY_NO_TOUCH:
            return False, self._container_policy_note(crow), cpolicy, crow
        policy = self._craft_policy(context)
        if cpolicy != POLICY_FREE and not policy.may_open_containers:
            return False, self._auth_note(policy, "翻箱子"), cpolicy, crow
        return True, "", cpolicy, crow

    def _container_read_access(self, context: dict) -> tuple[bool, str, str, Optional[dict]]:
        """**只读**容器访问的判定（`view_container` 用）：no_touch 拒、登记过的只读放行。

        与取用路径（`_container_access`）**只差一条**：只读不改变任何东西，所以
        `free` / `borrow_only` 都给看；没登记的箱子仍要翻箱授权 —— 只读也不去开
        别人没让我看的箱子。被拒时给的是**带 code** 的如实说明。
        """
        cpolicy, crow = self._container_policy(context)
        if not container_is_open(self._open_container(context)):
            # 根本没有开着的容器：没有可读的东西，也没可判的箱子 —— 放行到 handler，
            # 由它如实说"没开着的容器，我不知道里面有什么"（比报"未授权"更贴事实）。
            return True, "", cpolicy, crow
        if cpolicy == POLICY_NO_TOUCH:
            return (False, container_view_denied_note(CODE_NO_TOUCH, container=crow),
                    cpolicy, crow)
        if cpolicy in (POLICY_FREE, POLICY_BORROW_ONLY):
            return True, "", cpolicy, crow
        policy = self._craft_policy(context)
        if not policy.may_open_containers:
            return (False, container_view_denied_note(CODE_UNAUTHORIZED, tier=policy.tier),
                    cpolicy, crow)
        return True, "", cpolicy, crow

    # ── handler（注册表登记，指向现有技能函数）────────────────
    def _tool_move_to(self, payload: dict, context: dict) -> bool:
        if {"x", "y", "z"}.issubset(payload):
            self.skills.move_to(float(payload["x"]), float(payload["y"]), float(payload["z"]))
            return True
        return False

    def _tool_follow(self, payload: dict, context: dict) -> bool:
        player = payload.get("player") or payload.get("player_name")
        if player and not self._is_duplicate_task(context, {"follow"}, str(player)):
            self.skills.follow_player(str(player))
            return True
        return False

    @staticmethod
    def _player_cell(context: dict, name: str) -> str:
        """在线玩家快照里某玩家**当前所在格**（"x,y,z"，取整到格）；快照里没有返回 ""。"""
        players = context.get("players") if isinstance(context, dict) else None
        if not isinstance(players, list):
            return ""
        wanted = str(name or "").strip().lower()
        for row in players:
            if not isinstance(row, dict) or str(row.get("name", "")).strip().lower() != wanted:
                continue
            coords = [row.get(k) for k in ("x", "y", "z")]
            if all(isinstance(c, (int, float)) for c in coords):
                return "{:.0f},{:.0f},{:.0f}".format(*coords)
            return ""
        return ""

    def _tool_come_to_player(self, payload: dict, context: dict) -> bool:
        """一次性走到某玩家**现在**的位置（「过来 / 来我这」）——与长驻的 `follow` 语义不同。

        因此**不**套 `_is_duplicate_task({"follow"})` 那道门：那会把"到不了之后再叫一次"或
        "先走到 A 又要求走到 B"静默吞掉。这里只压掉**同一格上的紧邻重复请求**——键 = 玩家名 +
        该玩家**当前所在格**（从 context 的在线玩家快照解析），窗口 `COME_TO_PLAYER_REPEAT_WINDOW`：
        玩家挪到别处、或隔几秒后重叫，都照发；解析不出目标格时干脆不压（宁可多发，不许吞）。
        真正的"到没到 / 在不在走"由模组侧按 `come_to_player` 自己收敛。
        """
        player = payload.get("player") or payload.get("player_name")
        if not player or not str(player).strip():
            return False
        name = str(player).strip()
        cell = self._player_cell(context, name)
        key = f"{name.lower()}@{cell}" if cell else ""
        now = time.monotonic()
        last = getattr(self, "_come_to_player_last", None)
        if key and last and last[0] == key and (now - last[1]) < COME_TO_PLAYER_REPEAT_WINDOW:
            logger.info("[Planner] come_to_player 同一格紧邻重复（%s @ %s，%.1fs 内）→ 忽略",
                        name, cell, now - last[1])
            return False
        self.skills.come_to_player(name)
        self._come_to_player_last = (key, now) if key else None
        logger.info("[Planner] 一次性走到 %s 当前位置", name)
        return True

    def _tool_explore(self, payload: dict, context: dict) -> bool:
        self.skills.explore(int(payload.get("radius", 16)))
        return True

    def _tool_stop(self, payload: dict, context: dict) -> bool:
        self.skills.stop_all()
        # 新的指令说"停" ⇒ 还**挂着等**的复合体也不许留僵尸（走已有那条停的路）。
        self._abort_parked_composites("模型让我停")
        return True

    def _tool_where_is(self, payload: dict, context: dict) -> bool:
        """问位置：**只答坐标候选，绝不移动**（移动由模型另外调 move_to/go_to_block/follow）。

        来源全是现成的：state 里的在线玩家 + world 层记忆（记忆点/站点/容器/区域/基地）。
        问不到就返回空清单 + 一句如实的说明——**绝不编坐标**。
        """
        target = str(payload.get("target") or payload.get("name")
                     or payload.get("query") or "").strip()
        kind = str(payload.get("kind") or "").strip().lower() or where.KIND_AUTO
        sources = where.gather_sources(
            context,
            poi=getattr(self, "poi", None),
            places=getattr(self, "places", None),
            territory=self._territory_store(),
        )
        result = where.find(target, kind=kind, player_pos=self._player_pos(context),
                            sources=sources)
        self._action_note = str(result.get("note") or where.describe(result))
        logger.info("[Planner] where_is %s（kind=%s）：%d 个候选",
                    target or "(空)", kind, len(result.get("candidates") or []))
        return True

    def _dispatch_scan(self, radius: int, kind: str, limit: int) -> tuple[bool, str]:
        """发一次按需扫描（**唯一出口**）：记下等回执的请求，如实说明"还没回来"。

        回执回来走 `absorb_command_response`（由 session 的 `command_response` 转过来），
        落成 L2 里那行"最近一次按需扫描"的**事实**。
        """
        scanner = getattr(self.skills, "scan_entities", None)
        if not callable(scanner):
            self._scan_pending = None
            return False, f"（scan_entities 没接上（{perception.SCAN_ENTITY_TOOL} 不可用）：扫不了）"
        result = scanner(radius=int(radius), kind=str(kind), limit=int(limit))
        req_id = str((result or {}).get("req_id") or "") if isinstance(result, dict) else ""
        if not req_id:
            self._scan_pending = None
            return False, ("（scan_entities 没发出去（没有连接）：扫不了 —— "
                           "别把「没发出去」当成「附近没有」）")
        self._scan_pending = {"req_id": req_id, "radius": int(radius), "kind": str(kind),
                              "limit": int(limit), "at": time.time()}
        return True, (f"（已按需扫描附近实体：radius={radius} kind={kind} limit={limit}；"
                      f"回执回来我再说 total/returned/truncated —— "
                      f"**回执到之前别当成「附近只有这些」**）")

    def _tool_scan_entities(self, payload: dict, context: dict) -> bool:
        """按需扫描附近实体（**唯一的坐标出口**）：只读，扫完由回执如实说明。

        为什么需要它：`entities` 快照是**分级摘要**（只有威胁 / 玩家带坐标），被动生物等档位
        只报计数 —— 要某一只的坐标 / 血量 / 实体号，只能现场扫一趟。参数先本地拦一遍
        （code 与模组逐字同名），**不许**把"参数不对 / 没发出去"含糊成"附近没有"。
        """
        args, code, text = _scan_args(payload)
        if args is None:
            self._action_note = f"（scan_entities 没发：{text}）"
            logger.info("[Planner] scan_entities 参数被拒（%s）：%s", code, payload)
            return False
        ok, note = self._dispatch_scan(args["radius"], args["kind"], args["limit"])
        self._action_note = note
        logger.info("[Planner] scan_entities radius=%s kind=%s limit=%s → %s",
                    args["radius"], args["kind"], args["limit"], ok)
        return ok

    def absorb_command_response(self, data: Any) -> bool:
        """`command_response` 里属于**按需扫描**的那一条 → 落成 L2 的如实说明。

        返回是否消费了这条回执（接线处据此断言）。**只认按 req_id 配对上的那一次**：
        别的回执（find_block / 借还 / 路径导出…）一律不碰。
        """
        if not isinstance(data, dict):
            return False
        pending = self._scan_pending
        req_id = str(data.get("id") or "")
        if not isinstance(pending, dict) or not req_id \
                or str(pending.get("req_id") or "") != req_id:
            return False
        self._scan_pending = None
        self._scan_note = _scan_receipt_note(data, pending)
        self._scan_at = time.time()
        # 把**带坐标的行**留下来（「需要的时候再取」的落点）：`find_entity`/`use_on` 之后
        # 就能对摘要里没有坐标的档位动手了。只认这条配对上的回执，别把别人的负载当实体表；
        # **扫失败的回执不带实体表**（`ok:false` 只有 code/reason）⇒ 不收它的 entities。
        rows = data.get("entities")
        if data.get("ok") is not False and isinstance(rows, list):
            self._scan_rows = [row for row in rows if isinstance(row, dict)][:self._SCAN_ROW_CAP]
        logger.info("[Planner] scan_entities 回执：%s", self._scan_note)
        return True

    def _entity_rows_for_tools(self, context: Any) -> list:
        """给 `find_entity` / `use_on` 用的实体行：**快照 + 最近一次按需扫描的结果**（按身份去重）。

        快照是分级摘要（只有威胁 / 玩家 / 最近掉落物带坐标）；摘要里没有的档位
        **不等于附近没有** —— 要它们就先 `scan_entities` 扫一趟，扫回来的行并进来。
        扫描行**只在配对上的回执里取**（见 `absorb_command_response`），不编、不猜。
        """
        rows = list(perception.entities_rows(context).get("rows") or [])
        seen = {_entity_row_key(row) for row in rows}
        for row in (self._scan_rows or []):
            if not isinstance(row, dict):
                continue
            key = _entity_row_key(row)
            if key in seen:
                continue
            seen.add(key)
            rows.append(row)
        return rows

    def _tool_find_entity(self, payload: dict, context: dict) -> bool:
        """找附近实体：**只答候选，绝不移动、绝不扫描**（数据只来自 state 快照）。

        快照里没有就如实说没有（并说明它只覆盖周围约 16 格），**绝不编坐标** ——
        要过去是模型另外调 `move_to`/`go_to_block`/`follow`。

        快照是**分级摘要**时（`partial=True`：只有威胁 / 玩家带坐标），"没找到"**绝不等于**
        "附近没有" —— 结果里带 `partial`，说明文字会把这一点说白，并指向按需扫描
        `scan_entities`（要是那只/那个档位压根没坐标，**只有扫描能拿到**）。
        """
        kind = str(payload.get("kind") or payload.get("target")
                   or payload.get("name") or "").strip()
        view = perception.entities_rows(context)
        result = find_entities(kind, limit=payload.get("limit"),
                               player_pos=self._player_pos(context),
                               entities=self._entity_rows_for_tools(context),
                               partial=view["partial"])
        self._action_note = str(result.get("note") or entity_note(result))
        logger.info("[Planner] find_entity %s：%d 个候选（partial=%s）",
                    kind or "(全部)", len(result.get("candidates") or []), view["partial"])
        return True

    def _tool_find_block(self, payload: dict, context: dict) -> bool:
        """扫附近有没有某一种方块（**按需**的方块出口，只查不走）。

        没说扫哪种就不发帧 —— 但**如实说清为什么**（不许沉默地失败）：方块的逐条位置
        现在默认不进快照（摘要是"哪种几个 + 最近一种的距离"），所以要具体位置只能点名扫。
        """
        block = payload.get("block") or payload.get("block_type")
        if block and str(block).strip():
            self.skills.find_block(
                self._normalize_block_name(str(block)),
                int(payload.get("radius", 32)),
                int(payload.get("limit", 8)),
            )
            return True
        self._action_note = ("（find_block 没发：得点名扫**哪一种**方块（block=…，如 "
                             "minecraft:chest / *_bed）—— 方块的逐条位置默认不进快照，"
                             f"要具体位置只能按需扫描（{perception.SCAN_BLOCK_TOOL}），"
                             "我不会漫无目的地扫一圈）")
        return False

    def _tool_go_to_block(self, payload: dict, context: dict) -> bool:
        block = payload.get("block") or payload.get("block_type")
        if block and str(block).strip():
            normalized_block = self._normalize_block_name(str(block))
            if not self._is_duplicate_task(context, {"go_to_block"}, normalized_block):
                self.skills.go_to_block(
                    normalized_block,
                    int(payload.get("radius", 32)),
                    bool(payload.get("remember", True)),
                )
                return True
        return False

    def _tool_locate_structure(self, payload: dict, context: dict) -> bool:
        name = (payload.get("name") or payload.get("structure")
                or payload.get("biome") or payload.get("target"))
        kind = str(payload.get("kind") or ("biome" if payload.get("biome") else "structure"))
        result = self.locate_structure(str(name or ""), kind=kind)
        return bool(result.get("command"))

    def _tool_mine_block(self, payload: dict, context: dict) -> bool:
        self.skills.mine_block()
        return True

    def _tool_collect_blocks(self, payload: dict, context: dict) -> bool:
        block_type = payload.get("block_type") or payload.get("block")
        normalized_block = self._normalize_block_name(str(block_type)) if block_type else ""
        if block_type and not self._is_duplicate_task(context, {"collect"}, normalized_block):
            self.skills.collect_blocks(normalized_block, int(payload.get("count", 1)))
            return True
        return False

    def _tool_drop_item(self, payload: dict, context: dict) -> bool:
        item = payload.get("item")
        if item:
            self.skills.drop_item(str(item), int(payload.get("count", 1)))
            return True
        return False

    def _tool_sort_inventory(self, payload: dict, context: dict) -> bool:
        self.skills.sort_inventory()
        return True

    def _tool_get_inventory(self, payload: dict, context: dict) -> bool:
        self.skills.get_inventory()
        return True

    def _tool_craft_item(self, payload: dict, context: dict) -> bool:
        # 两条合成命令**同一道门**：授权（注册表统一校验）+ 库存优先。
        return self._dispatch_craft(payload, context, command="craft_item")

    def _tool_craft_via_recipe(self, payload: dict, context: dict) -> bool:
        return self._dispatch_craft(payload, context, command="craft_via_recipe")

    def _tool_place_block(self, payload: dict, context: dict) -> bool:
        self.skills.place_block()
        return True

    def _tool_build(self, payload: dict, context: dict) -> bool:
        """真建造：参数与模组侧 `build` 命令**一一对应** ✓；**材料由模型点名** ✓。

        不点名材料 ⇒ **如实拒**（`need_material` ✓），**不编**一种方块 ✗ —— 与 `place_block` 同一条口径 ✓。
        """
        if not {"x", "y", "z", "structure"}.issubset(payload):
            return False
        material = str(payload.get("material") or "")
        if not material:
            self._action_note = "build: 得点名 material（用哪种方块）—— 本地不做材料启发式 ✗"
            return False
        try:
            size = int(payload.get("size") or 3)
            height = int(payload.get("height") or 1)
        except (TypeError, ValueError):
            self._action_note = "build: size/height 必须是整数"
            return False
        self.skills.build(float(payload["x"]), float(payload["y"]), float(payload["z"]),
                          str(payload["structure"]), material, size, height)
        return True

    def _tool_container_close(self, payload: dict, context: dict) -> bool:
        self.skills.container_close()
        return True

    def _tool_view_container(self, payload: dict, context: dict) -> bool:
        """**只读**看容器里有什么：列清单 + 关掉界面，**什么都不拿**（授权在注册表那道门）。

        只认**已经开着**的容器（Java state 的 `open_container` 快照）——没开着就如实说
        不知道里面有什么（并告诉它先 `go_to_block` + `use_on` 打开），**绝不猜内容**。
        这是 haul「取多少 / 搬什么」之前那一步：先看清有几格、各多少，再决定怎么搬。
        """
        if self._haul_job is not None:
            # 搬运正用着容器界面（对账要快照）—— 不去搅它，如实说忙。
            self._action_note = container_view_denied_note(CONTAINER_VIEW_BUSY)
            logger.info("[Planner] view_container 拒绝：haul 正用着容器")
            return False
        container = self._open_container(context)
        target = str(payload.get("target") or payload.get("name") or "").strip()
        if container_is_open(container) and not container_matches(container, target):
            self._action_note = container_view_other_note(target, container)
            logger.info("[Planner] view_container：开着的是别的容器（code=%s)",
                        CODE_OTHER_CONTAINER)
            return False
        result = container_view_snapshot(container, limit=payload.get("limit"))
        self._action_note = str(result.get("note") or container_view_note(result))
        if not result.get("known"):
            logger.info("[Planner] view_container：看不成（code=%s）", result.get("code"))
            return False
        self.skills.container_close()
        logger.info("[Planner] view_container「%s」：%s 格非空（只读，什么都没拿）",
                    container_title(container) or "容器", result.get("total"))
        return True

    def _tool_use_on(self, payload: dict, context: dict) -> bool:
        """对目标使用手里的物品（右键）：判定在 `use_on` 模块，发帧复用**已有原语**。

        实体目标走 `skills.use_on_entity(id)`（Java 侧自带瞄准 + 通用右键）；坐标/方块目标
        走 `skills.look_at(...)` + `skills.use_on()`。判定不过（没有目标/够不着/手上不对）
        一律**带 code 如实说**，**一帧都不发**。
        """
        result = plan_use_on(payload, context=context,
                             entities_rows=self._entity_rows_for_tools(context))
        self._action_note = use_on_note(result)
        if not result.get("ok"):
            logger.info("[Planner] use_on 拒绝（%s）：%s", result.get("code"),
                        result.get("reason"))
            return False
        target = result.get("target") if isinstance(result.get("target"), dict) else {}
        entity_id = target.get("id")
        if (result.get("mode") == USE_ON_ENTITY
                and isinstance(entity_id, int) and not isinstance(entity_id, bool)):
            self.skills.use_on_entity(int(entity_id))
        else:
            self.skills.look_at(float(target["x"]), float(target["y"]), float(target["z"]))
            self.skills.use_on()
        logger.info("[Planner] use_on %s（%s）", target.get("name") or "?", result.get("mode"))
        return True

    def _tool_attack(self, payload: dict, context: dict) -> bool:
        self.skills.attack()
        return True

    def _tool_equip_armor(self, payload: dict, context: dict) -> bool:
        return self._dispatch_equip_armor(context)

    def _tool_equip_weapon(self, payload: dict, context: dict) -> bool:
        self.skills.equip_weapon()
        return True

    def _tool_select_hotbar(self, payload: dict, context: dict) -> bool:
        index = payload.get("index")
        if index is not None:
            self.skills.select_hotbar(int(index))
            return True
        return False

    def _tool_eat(self, payload: dict, context: dict) -> bool:
        """吃：工具驱动（LLM 决定用不用）；下发前仍过 **BackendEatYield**（让路/退避/冷却）。"""
        allowed, note = self.eat_yield.allow(context)
        if not allowed:
            self._action_note = note
            logger.info("[Planner] eat 被闸门挡下：%s", note)
            return False
        self.skills.eat()
        self.eat_yield.mark_sent()
        return True

    def _tool_sleep(self, payload: dict, context: dict) -> bool:
        self.skills.sleep()
        return True

    def _tool_trade(self, payload: dict, context: dict) -> bool:
        """村民交易：参数与模组侧 `trade` 命令一一对应（序号 / 结果物 / 只看报价 ✓）。

        什么都不给 ⇒ 跟最近的可交易村民交易（模组侧默认 `radius=8` ✓）—— 不编"村民类型"这种东西 ✗。
        """
        offer = payload.get("offer")
        item = str(payload.get("item") or "")
        list_only = bool(payload.get("list_only"))
        radius = payload.get("radius")
        entity_id = payload.get("entity_id")
        try:
            offer = int(offer) if offer is not None else None
            radius = float(radius) if radius is not None else None
            entity_id = int(entity_id) if entity_id is not None else None
        except (TypeError, ValueError):
            self._action_note = "trade: offer/radius/entity_id 必须是数字"
            return False
        self.skills.trade(offer=offer, item=item, list_only=list_only,
                          radius=radius, entity_id=entity_id)
        return True

    def _tool_sleep_in_bed(self, payload: dict, context: dict) -> bool:
        self.skills.sleep_in_bed()
        return True

    def _tool_guard_off(self, payload: dict, context: dict) -> bool:
        self.skills.guard_off()
        return True

    def _tool_set_hud_note(self, payload: dict, context: dict) -> bool:
        if "text" not in payload:
            return False
        seconds = payload.get("seconds")
        try:
            seconds = float(seconds) if seconds is not None else 30.0
        except (TypeError, ValueError):
            seconds = 30.0
        self.skills.set_hud_note(str(payload.get("text") or ""), seconds)
        return True

    def _tool_run_server_command(self, payload: dict, context: dict) -> bool:
        if self.server_commands is None:
            return False
        target_tool = str(payload.get("tool") or payload.get("command")
                          or payload.get("name") or "")
        result = self.server_commands(target_tool, payload.get("args"),
                                      confirm=str(payload.get("confirm") or ""))
        self._action_note = self._server_command_note(target_tool, result)
        return bool(result.get("ok"))

    def _tool_send_whisper(self, payload: dict, context: dict) -> bool:
        if self.whisper_sender is None:
            return False
        player = payload.get("player") or payload.get("to") or payload.get("target")
        text = payload.get("text") or payload.get("message") or payload.get("content")
        if not player or text is None or str(text).strip() == "":
            return False
        result = self.whisper_sender(str(player), str(text))
        self._action_note = self._server_command_note("私信", result)
        return bool(result.get("ok"))

    def _tool_tpa_respond(self, payload: dict, context: dict) -> bool:
        if self.tpa_responder is None:
            return False
        action = payload.get("action") or payload.get("decision") or payload.get("answer")
        result = self.tpa_responder(str(action or ""), str(payload.get("player") or ""))
        self._action_note = self._server_command_note("传送请求回应", result)
        return bool(result.get("ok"))

    def _tool_tpa_request(self, payload: dict, context: dict) -> bool:
        if self.tpa_requester is None:
            return False
        player = payload.get("player") or payload.get("target")
        if not player:
            return False
        result = self.tpa_requester(str(player))
        self._action_note = self._server_command_note("主动传送请求", result)
        return bool(result.get("ok"))

    def _tool_remember_poi(self, payload: dict, context: dict) -> bool:
        name = str(payload.get("name") or "").strip()
        category = str(payload.get("category") or "").strip() or "waypoint"
        self.skills.remember_poi(
            name,
            category,
            _as_optional_float(payload.get("x")),
            _as_optional_float(payload.get("y")),
            _as_optional_float(payload.get("z")),
        )
        return True

    def _tool_list_territory(self, payload: dict, context: dict) -> bool:
        return self._dispatch_list_territory(context)

    # ── 长期目标（模型的入口；写进现有 task_list，不另建第二套存储）──

    def _tool_set_goal(self, payload: dict, context: dict) -> bool:
        """立一个长期目标：写进 `task_list`（归属/优先级/审计沿用现有字段）。

        标 `model_created=True` —— 这是**编排轮次**（§10.2 #3）的唯一触发条件：
        只有模型自己创建的目标才会在事件发生时把模型请回来。
        """
        tasks = self.task_list
        text = str(payload.get("text") or payload.get("goal")
                   or payload.get("detail") or "").strip()
        if tasks is None or not text:
            return False
        tier, actor = self._actor_of(context)
        try:
            priority = None if payload.get("priority") is None else int(payload.get("priority"))
        except (TypeError, ValueError):
            priority = None
        try:
            goal = tasks.add("goal", owner=actor, owner_tier=tier, detail=text,
                             status="active", model_created=True, priority=priority)
            # 已有同名活跃目标时 `add` 会去重返回旧条目：把文字/优先级更新成这一次的。
            if str(goal.detail or "") != text or not goal.model_created:
                tasks.update(goal.id, detail=text, status="active", model_created=True,
                             priority=priority)
        except Exception as exc:  # noqa: BLE001 — 登记失败如实返回 False
            logger.warning("[Planner] set_goal 失败: %s", exc)
            return False
        return True

    def _tool_end_goal(self, payload: dict, context: dict) -> bool:
        """显式结束长期目标（省 id ＝结束当前那条活跃目标）。

        目标**不显式结束就会一直占着**（会一直触发编排轮次），所以做完/放弃时必须调它。
        """
        tasks = self.task_list
        if tasks is None:
            return False
        goal_id = str(payload.get("id") or payload.get("goal") or "").strip()
        item = tasks.get(goal_id) if goal_id else None
        if item is None:
            item = next((i for i in tasks.items()
                         if i.kind == "goal" and i.status not in {"done", "failed", "cancelled"}),
                        None)
        if item is None:
            return False
        reason = str(payload.get("reason") or "").strip()
        if reason:
            tasks.update(item.id, failure_reason=reason)
        tasks.complete(item.id, success=True)
        return True

    @staticmethod
    def _server_command_note(what: str, result: Any) -> Optional[str]:
        """把闸门/解析的结论翻成一句**如实**的补充（被拒就说被拒，不假装做了）。"""
        if not isinstance(result, dict):
            return None
        if result.get("ok"):
            command = str(result.get("command") or "")
            if command:
                return f"（已下发 /{command.lstrip('/')}）"
            return None
        reason = str(result.get("blocked") or result.get("reason") or "unknown")
        if reason == "not_whitelisted":
            return f"（{what}没被勾选进白名单，我没发）"
        if reason == "hard_blacklist":
            return f"（{what}在黑名单里，绝对不能发）"
        if reason == "needs_confirm":
            token = str(result.get("confirm") or "")
            return (f"（{what}风险高：要再确认一次 —— 把同一条动作重发一遍，"
                    f"args 里带上 confirm=\"{token}\"）")
        if reason == "invalid" or reason == "invalid_text":
            return f"（{what}的参数不合法，我没发）"
        if reason == "disabled":
            return f"（服务器指令自动化被关掉了，{what}没发）"
        if reason == "tier_not_allowed":
            return f"（{what}你的层级不够，我没做）"
        if reason in {"not_found", "ambiguous", "empty"}:
            candidates = "、".join(str(item) for item in (result.get("candidates") or []))
            tail = f"，候选：{candidates}" if candidates else ""
            return f"（{what}的目标没锁定（{reason}）{tail}，我不猜）"
        return f"（{what}没成：{reason}）"

    # ── 制作 / 容器：授权 + 库存优先（用户口径） ──────────────────

    def _craft_policy(self, context: dict) -> CraftPolicy:
        """本轮的制作/翻箱授权（**只查能力表**，不看 tier 分支）。

        没人指挥的轮次（self-prompt / 事件注入）没有发话者 → 按 guest 处理（默认拒绝）；
        没有权限表来源时同样默认**拒绝**（宁可不做，也不越权动别人的箱子）。
        """
        ctx = context if isinstance(context, dict) else {}
        tier = str(ctx.get("actor_tier") or "")
        store = self.permissions
        if store is None:
            return CraftPolicy(tier=tier or TIER_GUEST,
                               may_craft=False, may_open_containers=False,
                               reason="no_permissions")
        return store.craft_policy(tier)

    def _track_craft_need(self, missing: Any) -> None:
        """登记本次制作的材料缺口（正数才算）：容器取用以它为上限，绝不搬空。"""
        need = {}
        for item, amount in (missing or {}).items() if isinstance(missing, dict) else ():
            try:
                value = int(amount)
            except (TypeError, ValueError):
                continue
            if value > 0:
                need[normalize_item(item)] = value
        self._craft_need = need
        self._craft_tracked = set(need)

    def _dispatch_craft(self, payload: dict, context: dict, *, command: str) -> bool:
        """合成：先过授权，再按**自己背包**评估材料，最后才下发。

        - 没被允许造 → 一句如实说明，**一帧都不发**；
        - 材料齐（或配方不在我们的表里）→ 就地用 `craft_via_recipe` 造（容器 0）；
        - 材料不齐：
            · 获授权翻箱 → 记下缺口（后续 `container_move` 以它为上限）、去已知容器拿；
            · 没获授权 → **如实说缺什么**，绝不开箱。
        """
        item = payload.get("item") or payload.get("item_name")
        if not item:
            return False
        item_id = self._normalize_item_name(str(item))
        try:
            count = int(payload.get("count", 1))
        except (TypeError, ValueError):
            count = 1
        count = max(1, count)

        policy = self._craft_policy(context)
        if not policy.may_craft:
            self._craft_note = self._auth_note(policy, "造东西")
            logger.info("[Planner] 合成 %s 被拒：%s", item_id, policy.reason)
            return False
        if self._is_duplicate_task(context, {"craft"}, item_id):
            return False

        inventory = context.get("inventory") if isinstance(context, dict) else None
        plan = evaluate_craft(item_id, count, inventory)
        if plan.known and plan.missing:
            missing = describe_missing(plan)
            if not policy.may_open_containers:
                self._track_craft_need({})
                self._craft_note = (f"（我自己背包里的材料不够：还缺 {missing}；"
                                    f"没被授权翻箱子，我不去翻）")
                logger.info("[Planner] 合成 %s 缺料且未授权翻箱：%s", item_id, missing)
                return False
            picked = route_containers(plan.missing,
                                      context.get("known_containers") if isinstance(context, dict) else None)
            if not picked:
                self._track_craft_need({})
                self._craft_note = f"（材料不够：还缺 {missing}；已知的容器里也没有，就没去动）"
                logger.info("[Planner] 合成 %s 缺料且已知容器补不上：%s", item_id, missing)
                return False
            # 缺口登记：后面的 container_move 只能按"还缺的量"取，绝不搬空。
            self._track_craft_need(plan.missing)
            pick = picked[0]
            self._craft_note = (f"（背包里的材料不够：还缺 {missing}；"
                                f"我记得 {self._pick_label(pick)} 那边有，我去拿）")
            self._dispatch_container_trip(pick, context)
            logger.info("[Planner] 合成 %s 缺料 → 去 %s 取", item_id, self._pick_label(pick))
            return True

        self._track_craft_need({})
        self._craft_note = None
        if command == "craft_via_recipe":
            self.skills.craft_via_recipe(item_id, count)
        else:
            self.skills.craft_item(item_id, count)
        logger.info("[Planner] 合成 %s x%d（库存优先，就地）", item_id, count)
        return True

    def _auth_note(self, policy: CraftPolicy, what: str) -> str:
        """如实说明为什么没动手（缺授权时的统一话术）。"""
        return f"（{policy.tier} 没被允许让我{what}，所以没动手）"

    @staticmethod
    def _pick_label(pick: ContainerPick) -> str:
        where = (f"({pick.x:.0f},{pick.y:.0f},{pick.z:.0f})"
                 if pick.x is not None and pick.z is not None else "记忆里")
        return f"{pick.block_id}{where}"

    def _dispatch_container_trip(self, pick: ContainerPick, context: dict) -> bool:
        """去已知容器的路上：走已有的 `go_to_block`（Java 侧负责寻路 + 到达后记 POI）。

        容器只有走到跟前、右键打开后才谈得上取用，所以这里只负责"去哪"这一步。
        """
        block = str(pick.block_id or "").strip() or "minecraft:chest"
        radius = int(max(32, (pick.distance or 0.0) + 8))
        if self._is_duplicate_task(context, {"go_to_block", "collect"}, block):
            return False
        self.skills.go_to_block(block, radius, True)
        return True

    def _dispatch_container_move(self, payload: dict, context: dict) -> bool:
        """容器取用：必须获授权，且**用多少取多少**（有缺口登记时绝不超、绝不整组搬空）。

        先看这个箱子登记的策略：`no_touch` 一律不许动；`free`（所有权已交给它）随取、
        不记欠账；剩下的（`borrow_only` / 没登记）落回借用台账那条保守路径。
        """
        allowed, note, cpolicy, crow = self._container_access(context)
        if not allowed:
            self._craft_note = note
            logger.info("[Planner] container_move 被拒：%s", note)
            return False
        from_slot = _as_optional_int(payload.get("from_slot"))
        if from_slot is None:
            return False
        to_slot = _as_optional_int(payload.get("to_slot"))
        count = _as_optional_int(payload.get("count"))
        item = str(payload.get("item") or payload.get("name") or "").strip()
        if not item:
            item = self._item_at_slot(context, from_slot)
        item_id = normalize_item(item) if item else ""
        if item_id and item_id in self._craft_tracked:
            # 这一次制作登记过的材料：只按"还缺的量"取（已经够了就不许再拿）。
            remaining = self._craft_need.get(item_id, 0)
            if remaining <= 0:
                self._craft_note = f"（{item_id} 已经够了，不用再从箱子里拿）"
                logger.info("[Planner] container_move 被拒：%s 已够", item_id)
                return False
            take = clamp_take(item_id, count, self._craft_need)
            if take <= 0:
                self._craft_note = f"（{item_id} 已经够了，不用再从箱子里拿）"
                return False
            count = take
            left = remaining - take
            if left > 0:
                self._craft_need[item_id] = left
            else:
                del self._craft_need[item_id]
        elif self._craft_tracked:
            # 有缺口但认不出这一格是什么（Java 的 open_container 只报槽位数）：
            # 只允许**明确给 count**，且不超过总缺口 —— 整组搬空一律不放行。
            total = sum(self._craft_need.values())
            if count is None or count <= 0:
                self._craft_note = "（不确定这格是什么，不能整组搬；说清要拿哪件材料、拿多少）"
                logger.info("[Planner] container_move 被拒：缺口中不让整组搬")
                return False
            if count > total:
                self._craft_note = f"（只需要补 {total} 个，不能多拿）"
                logger.info("[Planner] container_move 被拒：%d > 缺口 %d", count, total)
                return False
        self._record_take(item_id or f"slot:{from_slot}", count or 0, from_slot, to_slot)
        if cpolicy == POLICY_FREE:
            # 所有权已经交给它：随取，不记欠账 —— 但**仍记审计**（"这铁怎么没了"要能查）。
            store = self._territory_store()
            if store is not None:
                store.audit("take_free", target=str((crow or {}).get("name") or ""),
                            details={"item": item_id or f"slot:{from_slot}",
                                     "count": count, "from_slot": from_slot})
        self.skills.container_move(from_slot, to_slot, count)
        logger.info("[Planner] 容器取用 %s x%s（slot %s → %s）",
                    item_id or "?", count if count else "整组", from_slot, to_slot if to_slot is not None else "快速移动")
        return True

    def _dispatch_container_click(self, payload: dict, context: dict) -> bool:
        """点容器里的一格（"点容器找盔甲"）：同样是容器操作，没授权不动。"""
        allowed, note, _cpolicy, crow = self._container_access(context)
        if not allowed:
            self._craft_note = note
            logger.info("[Planner] container_click 被拒：%s", note)
            return False
        slot = _as_optional_int(payload.get("slot"))
        if slot is None:
            return False
        click_type = str(payload.get("click_type") or "pickup").strip().lower() or "pickup"
        button = _as_optional_int(payload.get("button"))
        self.skills.container_click(slot, button if button is not None else 0, click_type)
        return True

    # ── 借用台账：从仓库拿东西＝借（授权 → 上限 → 归还 → 校验 → 审计）──

    @staticmethod
    def _open_container(context: dict) -> dict:
        """当前打开的容器快照（没有／不是 dict → 空 dict）。"""
        ctx = context if isinstance(context, dict) else {}
        container = ctx.get("open_container")
        return container if isinstance(container, dict) else {}

    def _container_policy(self, context: dict) -> tuple[str, Optional[dict]]:
        """当前这个箱子登记的策略（free / borrow_only / no_touch）。

        用户口径：「范围决定在哪动手，容器策略决定哪个箱子能动」——先按名字（title/name）
        对，再按坐标对（打开的快照带 x/y/z 时），对不上就是**没登记**（一律落回借用台账那条
        保守路径）。
        """
        store = self._territory_store()
        if store is None:
            return "", None
        container = self._open_container(context)
        name = str(container.get("title") or container.get("name") or "").strip()
        row = store.container(name) if name else None
        if row is None:
            try:
                if all(isinstance(container.get(axis), (int, float))
                       and not isinstance(container.get(axis), bool)
                       for axis in ("x", "y", "z")):
                    row = store.container_by_position(container.get("x"), container.get("y"),
                                                      container.get("z"))
            except (TypeError, ValueError):
                row = None
        return (str((row or {}).get("policy") or ""), row)

    def _container_policy_note(self, row: Optional[dict]) -> str:
        name = str((row or {}).get("name") or "这个箱子")
        return f"（「{name}」是 no_touch：什么都不许动）"

    def _borrow_grant(self, item: str) -> Optional[dict]:
        """这件物品有没有被口头授权借用（查规则记忆；没权限表来源 → 一律没有）。"""
        store = self.borrow_rules
        if store is None:
            return None
        try:
            return store.match(item)
        except Exception as exc:
            logger.warning("[Planner] 借用规则读取失败: %s", exc)
            return None

    def _borrow_need(self, item: str) -> Optional[int]:
        """这次制作这件物品还缺多少（没有登记就返回 None＝不限需求，只按 256 上限）。"""
        if item in self._craft_tracked:
            return self._craft_need.get(item)
        return None

    def _borrow_refusal_note(self, item: str, reason: str, cap: int) -> str:
        if reason == "whole_container_forbidden":
            return "（不给数量的取用＝整箱搬，不允许；说清要借多少）"
        if reason == "invalid_count":
            return "（借的数量得是正整数）"
        if reason == "over_limit":
            return f"（{item} 一次最多借 {cap} 个，不能整箱搬）"
        return f"（这次借不成：{reason}）"

    def _borrow_go_to_container(self, item: str, raw: str, count: int, context: dict) -> bool:
        """容器没开（或里面没有这件东西）：去**已知容器**拿，而不是凭空冒出来。"""
        containers = context.get("known_containers") if isinstance(context, dict) else None
        picks = route_containers({item: count}, containers)
        if not picks:
            self._craft_note = (f"（{raw or item} 这里没有：箱子里没找着，也没有打开的容器可以拿）")
            logger.info("[Planner] borrow_item：找不到 %s", item)
            return False
        pick = picks[0]
        self._craft_note = (f"（我记得 {self._pick_label(pick)} 里有 {raw or item}，"
                            f"过去拿 {count} 个，拿完就放回去）")
        return self._dispatch_container_trip(pick, context)

    def _dispatch_borrow_item(self, payload: dict, context: dict) -> bool:
        """从仓库**借**物品：先过翻箱授权，再过借用规则，最后卡上限。

        整箱搬（不给 count）一律拒绝；一次最多 `min(256, 需求×2)`。借出去的东西会被记进
        台账 —— 用完/任务结束/被叫停时由 `settle_borrows` 自动放回并核对。
        """
        policy = self._craft_policy(context)
        allowed, note, cpolicy, crow = self._container_access(context)
        if not allowed:
            self._craft_note = note
            logger.info("[Planner] borrow_item 被拒：%s", note)
            return False
        raw = str(payload.get("item") or payload.get("name") or "").strip()
        item = normalize_item(raw)
        if not item:
            self._craft_note = "（要借什么得说清楚）"
            return False
        if self._borrow_grant(item) is None:
            self._craft_note = f"（没人授权我取用 {raw or item}；要用得先明说「可以用仓库里的……」）"
            logger.info("[Planner] borrow_item 被拒：无授权 %s", item)
            return False
        need = self._borrow_need(item)
        count = _as_optional_int(payload.get("count"))
        allowed, reason, cap = self.borrow_ledger.check(item, count, need=need)
        if not allowed:
            self._craft_note = self._borrow_refusal_note(item, reason, cap)
            logger.info("[Planner] borrow_item 被拒：%s %s", item, reason)
            return False
        container = self._open_container(context)
        plans = self.borrow_ledger.plan_moves_out(container, item, count) if container else []
        if not plans:
            return self._borrow_go_to_container(item, raw, count, context)
        reason_text = str(payload.get("reason") or payload.get("why")
                          or ("制作" if need is not None else "任务"))
        taken = 0
        for plan in plans:
            self.skills.container_move(plan["from_slot"], plan["to_slot"], plan["count"])
            taken += plan["count"]
        first = plans[0]
        hint = payload.get("container") if isinstance(payload.get("container"), dict) else {}
        self.borrow_ledger.record_borrow(
            item, taken, container=container,
            from_slot=first["from_slot"], to_slot=first["to_slot"], reason=reason_text,
            container_pos=self._container_pos(payload, context),
            container_block=str(hint.get("block_id") or hint.get("name") or ""))
        where = str(container.get("title") or "容器")
        short = "" if taken >= count else f"（箱里只有 {taken} 个，先拿这些）"
        self._craft_note = (f"（从「{where}」借了 {item} x{taken}{short}；"
                            f"用完/任务结束会放回去）")
        logger.info("[Planner] 借出 %s x%s（%s 条移动）", item, taken, len(plans))
        return True

    @staticmethod
    def _container_pos(payload: dict, context: dict) -> Optional[tuple]:
        """借出时记下容器坐标（payload 给了就用；没有就不编）。"""
        source = payload.get("container") if isinstance(payload, dict) else None
        source = source if isinstance(source, dict) else payload if isinstance(payload, dict) else {}
        try:
            if all(isinstance(source.get(axis), (int, float)) and not isinstance(source.get(axis), bool)
                   for axis in ("x", "y", "z")):
                return (float(source["x"]), float(source["y"]), float(source["z"]))
        except (TypeError, ValueError):
            return None
        return None

    def _send_return_trip(self, entry) -> bool:
        """回原容器的路上：走 `go_to_block`（Java 负责寻路 + 到达后记 POI）。"""
        if entry.trip_sent:
            return False
        entry.trip_sent = True
        self.skills.go_to_block(entry.container_block or "minecraft:chest", 16, True)
        return True

    def _container_in_reach(self, entry, context: dict) -> bool:
        """容器就在跟前？用 nearby_storage / known_containers 的距离与坐标判断（绝不猜）。"""
        ctx = context if isinstance(context, dict) else {}
        rows = []
        for key in ("nearby_storage", "known_containers"):
            value = ctx.get(key)
            if isinstance(value, (list, tuple)):
                rows.extend(row for row in value if isinstance(row, dict))
        for row in rows:
            title = str(row.get("title") or row.get("name") or "")
            if entry.container_title and title and title != entry.container_title:
                continue
            distance = row.get("distance")
            if isinstance(distance, (int, float)) and not isinstance(distance, bool) and distance <= 4.0:
                return True
            if entry.container_pos:
                try:
                    dx = float(row.get("x")) - entry.container_pos[0]
                    dy = float(row.get("y")) - entry.container_pos[1]
                    dz = float(row.get("z")) - entry.container_pos[2]
                except (TypeError, ValueError):
                    continue
                if abs(dx) <= 4 and abs(dy) <= 4 and abs(dz) <= 4:
                    return True
        return False

    def _return_entries(self, entries: list, context: dict, *, reason: str,
                        count: Optional[int] = None, final: bool = False) -> dict:
        """把一批借用放回原容器；返回 `{sent, pending, failed}`（都是 BorrowEntry）。

        - 容器开着且是原容器 → `container_move` 放回，等快照核对；
        - 容器没开但有坐标 → 走一趟 `go_to_block`，等到了再放；
        - 真的回不去（`final=True`）→ 记账为失败并**报出来**。

        **不查翻箱授权表**：把东西还回去是保守方向，也是用户口径里"必须做"的那一半；
        借出去那一步已经卡过授权了。显式的 `return_item` 工具才走授权门。
        """
        container = self._open_container(context)
        out: dict = {"sent": [], "pending": [], "failed": []}
        for entry in entries:
            if entry.awaiting_snapshot:
                out["pending"].append(entry)
                continue
            want = min(int(count), entry.remaining) if count else entry.remaining
            if want <= 0:
                continue
            if container and self.borrow_ledger.same_container(entry, container):
                plan = self.borrow_ledger.plan_move_back(container, entry.item, want)
                if plan:
                    self.skills.container_move(plan["from_slot"], plan["to_slot"], plan["count"])
                    self.borrow_ledger.begin_return(entry.id, count=plan["count"],
                                                    from_slot=plan["from_slot"],
                                                    to_slot=plan["to_slot"], reason=reason)
                    out["sent"].append(entry)
                    continue
                if final and self.borrow_ledger.holds(container, entry.item):
                    # 东西还在手上，可原容器塞不回去了（格子满了）→ 报出来。
                    self.borrow_ledger.mark_unreturnable(entry.id, f"{reason}:container_full")
                    out["failed"].append(entry)
                    continue
            if entry.container_pos and self._send_return_trip(entry):
                out["pending"].append(entry)
                continue
            if self._container_in_reach(entry, context):
                self.skills.open_container()
                out["pending"].append(entry)
                continue
            if final:
                self.borrow_ledger.mark_unreturnable(entry.id, f"{reason}:no_container")
                out["failed"].append(entry)
            else:
                out["pending"].append(entry)
        return out

    def _dispatch_return_item(self, payload: dict, context: dict) -> bool:
        """显式归还（工具 `return_item`）：放回原容器，数量缺省＝这笔借用的全部剩余。"""
        policy = self._craft_policy(context)
        if not policy.may_open_containers:
            self._craft_note = self._auth_note(policy, "翻箱子")
            return False
        item = normalize_item(payload.get("item") or payload.get("name"))
        targets = [e for e in self.borrow_ledger.outstanding() if not item or e.item == item]
        if not targets:
            self._craft_note = "（没有借了还没还的东西）"
            return False
        result = self._return_entries(targets, context, reason="tool",
                                      count=_as_optional_int(payload.get("count")), final=True)
        self._craft_note = self._return_note(result)
        return bool(result["sent"] or result["pending"] or result["failed"])

    @staticmethod
    def _return_note(result: dict) -> str:
        sent = ", ".join(f"{e.item} x{e.count - e.returned}" for e in result.get("sent", []))
        pending = ", ".join(f"{e.item} x{e.remaining}" for e in result.get("pending", []))
        failed = ", ".join(f"{e.item} x{e.remaining}（{e.failure or '回不去'}）"
                          for e in result.get("failed", []))
        parts = []
        if sent:
            parts.append(f"已放回原箱：{sent}")
        if pending:
            parts.append(f"正在放回/路上：{pending}")
        if failed:
            parts.append(f"**没能放回去**：{failed}")
        return "（" + "；".join(parts) + "）" if parts else "（没有要还的）"

    def settle_borrows(self, reason: str, context: Optional[dict] = None) -> dict:
        """任务结束/失败/取消/被中断/玩家接管时结算借用：归还剩余并报出回不去的。

        `final=True`：这一步是真结算，回不去的一律记账为失败并放进 `failed`（要报出来）。
        """
        if context is None:
            context = {}
        entries = self.borrow_ledger.outstanding()
        if not entries:
            return {"reason": reason, "sent": [], "pending": [], "failed": [], "note": ""}
        result = self._return_entries(entries, context, reason=reason, final=True)
        note = self._return_note(result)
        result["reason"] = reason
        result["note"] = note
        logger.info("[Planner] 借用结算（%s）：%s", reason, note)
        return result

    def auto_return_step(self, context: Optional[dict] = None) -> bool:
        """空闲时推进一步归还（开箱/放回）——不设 final，绝不误判失败。"""
        if context is None:
            context = {}
        entries = self.borrow_ledger.outstanding()
        if not entries:
            return False
        result = self._return_entries(entries, context, reason="auto", final=False)
        if result["sent"] or result["pending"]:
            self._craft_note = self._return_note(result)
            return True
        return False

    def verify_borrow_returns(self, open_container: Any) -> list[dict]:
        """用一份新的 `open_container` 快照核对归还（真的回去了才算数）。"""
        try:
            rows = self.borrow_ledger.verify(open_container)
        except Exception as exc:
            logger.warning("[Planner] 归还核对失败: %s", exc)
            return []
        for row in rows:
            if row.get("result") == "failed":
                self._craft_note = (f"（{row['item']} x{row.get('count')} **没能放回原箱**："
                                    f"{row.get('detail') or row.get('reason')}）")
        return rows

    def borrow_status(self) -> dict:
        """借用总览（欠账 + 审计尾部），供 /api/status 与 prompt。"""
        return self.borrow_ledger.status()

    def borrow_note(self) -> str:
        """借出未还的摘要（prompt 用）。"""
        return self.borrow_ledger.describe()

    # ── 领地 / 基地 / 容器 / 区域 / 回家（world 层；授权全查表） ────

    def _actor_of(self, context: dict) -> tuple[str, str]:
        """这一轮的动手人（tier + 名字）：优先显式 `actor_*`，退回最近发话者。"""
        ctx = context if isinstance(context, dict) else {}
        speaker = ctx.get("last_speaker") if isinstance(ctx.get("last_speaker"), dict) else {}
        name = str(ctx.get("actor_name") or speaker.get("name") or "").strip()
        tier = str(ctx.get("actor_tier") or TIER_GUEST).strip().lower() or TIER_GUEST
        return tier, name

    @staticmethod
    def _player_pos(context: dict) -> Optional[tuple[float, float, float]]:
        """玩家当前坐标（缺一个轴就不猜，返回 None）。"""
        player = (context or {}).get("player") if isinstance(context, dict) else None
        player = player if isinstance(player, dict) else {}
        try:
            if any(not isinstance(player.get(axis), (int, float))
                   or isinstance(player.get(axis), bool) for axis in ("x", "y", "z")):
                return None
            return (float(player["x"]), float(player["y"]), float(player["z"]))
        except (TypeError, ValueError):
            return None

    def _no_authority_note(self, what: str, context: dict) -> str:
        tier, name = self._actor_of(context)
        who = f"{name}（{tier}）" if name else tier
        return f"（{who} 没资格{what}，我没动）"

    def _territory_store(self):
        return self.territory

    def _base_by_name(self, name: Any, context: dict) -> Optional[dict]:
        store = self._territory_store()
        if store is None:
            return None
        clean = str(name or "").strip()
        if clean:
            return store.base(clean)
        pos = self._player_pos(context)
        if pos is not None:
            inside = current_base(store.bases(), *pos)
            if inside is not None:
                return inside
        bases = store.bases()
        return bases[0] if len(bases) == 1 else None

    def _dispatch_list_territory(self, context: dict) -> bool:
        """列出手上的授权与保护项（诚实清单：没有就说没有）。"""
        store = self._territory_store()
        if store is None:
            self._territory_note = "（我这边还没有领地/基地/容器的记录）"
            return False
        snapshot = store.snapshot()
        parts: list[str] = []
        for base in snapshot["bases"]:
            anchor = point_of(base.get("anchor")) or (0.0, 0.0, 0.0)
            bed = point_of(base.get("home_bed"))
            where = f"({anchor[0]:.0f},{anchor[1]:.0f},{anchor[2]:.0f})"
            bed_text = f"，床位 ({bed[0]:.0f},{bed[1]:.0f},{bed[2]:.0f})" if bed else "，还没找床"
            parts.append(f"基地 {base.get('name')}（{base.get('kind')}）{where} 半径"
                         f"{float(base.get('radius') or 0):.0f}{bed_text}")
        for row in snapshot["territories"]:
            parts.append(f"领地 {row.get('name')}：授权 {'/'.join(normalize_grants(row.get('grants')))}")
        for row in snapshot["containers"]:
            parts.append(f"容器 {row.get('name')}（{row.get('policy')}）"
                         f"@({float(row.get('x') or 0):.0f},{float(row.get('y') or 0):.0f},"
                         f"{float(row.get('z') or 0):.0f})")
        for row in snapshot["zones"]:
            protect = "/".join(str(item) for item in (row.get("protect") or [])) or "无保护项"
            parts.append(f"区域 {row.get('name')}（{row.get('kind')}）：保护 {protect}")
        if not parts:
            self._territory_note = "（还没有任何领地、基地、容器或区域）"
            return True
        self._territory_note = "（已知：" + "；".join(parts) + "）"
        return True

    def _dispatch_grant_territory(self, payload: dict, context: dict) -> bool:
        """把一片区域的所有权授予它（**只有 master**）。授权由注册表统一校验。"""
        store = self._territory_store()
        if store is None:
            self._territory_note = "（没有领地存储，我记不下来）"
            return False
        pos = self._player_pos(context)
        x = _as_optional_float(payload.get("x"))
        y = _as_optional_float(payload.get("y"))
        z = _as_optional_float(payload.get("z"))
        if x is None or y is None or z is None:
            if pos is None:
                self._territory_note = "（不知道这片地在哪，也没给坐标，我没记）"
                return False
            x, y, z = pos
        radius = _as_optional_float(payload.get("radius"))
        row = store.set_territory(
            payload.get("name"), kind=str(payload.get("kind") or ""), x=x, y=y, z=z,
            radius=radius, grants=payload.get("grants"), granted_by=self._granted_by(context))
        if row is None:
            self._territory_note = "（领地得有名字和形状（半径），这条没记）"
            return False
        grants = "/".join(normalize_grants(row.get("grants"))) or "无"
        self._territory_note = (f"（记下了领地「{row['name']}」，这一片由我自己使唤，"
                                f"授权：{grants}）")
        logger.info("[Planner] 领地授权 %s：%s", row.get("name"), row.get("grants"))
        return True

    def _dispatch_revoke_territory(self, payload: dict, context: dict) -> bool:
        store = self._territory_store()
        name = str(payload.get("name") or "").strip()
        if store is None or not name:
            self._territory_note = "（要收回哪块地得说清名字）"
            return False
        removed = store.revoke_territory(name)
        if removed is None:
            self._territory_note = f"（没有叫「{name}」的领地）"
            return False
        self._territory_note = f"（已收回领地「{name}」，立刻生效）"
        return True

    def _dispatch_set_base(self, payload: dict, context: dict) -> bool:
        store = self._territory_store()
        name = str(payload.get("name") or "").strip()
        if store is None or not name:
            self._territory_note = "（基地得有个名字）"
            return False
        pos = self._player_pos(context)
        x = _as_optional_float(payload.get("x"))
        y = _as_optional_float(payload.get("y"))
        z = _as_optional_float(payload.get("z"))
        if x is None or y is None or z is None:
            if pos is None:
                self._territory_note = "（不知道锚点，也没给坐标，基地没设）"
                return False
            x, y, z = pos
        radius = _as_optional_float(payload.get("radius"))
        row = store.set_base(name, kind=str(payload.get("kind") or "base"),
                             anchor={"x": x, "y": y, "z": z},
                             radius=radius if radius is not None else 32.0,
                             granted_by=self._granted_by(context))
        if row is None:
            self._territory_note = "（基地的锚点/半径不合法，没设）"
            return False
        self._territory_note = (f"（基地「{row['name']}」设在 ({x:.0f},{y:.0f},{z:.0f})，"
                                f"半径 {float(row['radius']):.0f}）")
        return True

    def _dispatch_del_base(self, payload: dict, context: dict) -> bool:
        store = self._territory_store()
        name = str(payload.get("name") or "").strip()
        if store is None or not name:
            self._territory_note = "（要删哪个基地得说清名字）"
            return False
        removed = store.del_base(name)
        if removed is None:
            self._territory_note = f"（没有叫「{name}」的基地）"
            return False
        self._territory_note = f"（已删掉基地「{name}」）"
        return True

    def _granted_by(self, context: dict) -> dict:
        tier, name = self._actor_of(context)
        return {"tier": tier, "name": name}

    def _dispatch_remember_container(self, payload: dict, context: dict) -> bool:
        """**记住某个箱子的确切位置** + 访问策略（friend 只能标自己的，且只到传递权）。"""
        return self._container_grant(payload, context, require_existing=False)

    def _dispatch_grant_container(self, payload: dict, context: dict) -> bool:
        return self._container_grant(payload, context, require_existing=True)

    def _container_grant(self, payload: dict, context: dict, *, require_existing: bool) -> bool:
        store = self._territory_store()
        tier, actor = self._actor_of(context)
        name = str(payload.get("name") or "").strip()
        if store is None or not name:
            self._territory_note = "（要给哪个箱子设策略得说清名字）"
            return False
        existing = store.container(name)
        if require_existing and existing is None:
            self._territory_note = f"（还没记住「{name}」在哪，先告诉我它的坐标）"
            return False
        owner = str(payload.get("owner") or "").strip()
        if not owner:
            owner = str((existing or {}).get("owner") or actor or "")
        if (existing or {}).get("owner") and tier != "master":
            # friend 不能借重新登记把别人的箱子划到自己名下。
            owner = str(existing.get("owner"))
        policy = normalize_policy(payload.get("policy"), default=POLICY_BORROW_ONLY)
        permissions = self.permissions
        if permissions is None:
            self._territory_note = "（没有权限表，我不能给容器设策略）"
            return False
        try:
            decision = permissions.container_grant_policy(tier, actor=actor, owner=owner,
                                                          policy=policy)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[Planner] 容器授权查询失败: %s", exc)
            decision = {"ok": False, "reason": "no_permissions", "policy": "", "clamped": False}
        if not decision.get("ok"):
            why = {
                "tier_not_allowed": f"{actor or tier} 没资格给我设容器的取用策略",
                "not_owner": "friend 只能标**自己的**箱子，这个不是你的",
            }.get(str(decision.get("reason")), "这条授权不成立")
            self._territory_note = f"（{why}，我没记）"
            logger.info("[Planner] 容器授权被拒：%s（%s）", name, decision.get("reason"))
            return False
        final_policy = str(decision.get("policy") or POLICY_BORROW_ONLY)
        if require_existing:
            row = store.set_container_policy(name, final_policy,
                                             granted_by=self._granted_by(context))
        else:
            pos = self._player_pos(context)
            x = _as_optional_float(payload.get("x"))
            y = _as_optional_float(payload.get("y"))
            z = _as_optional_float(payload.get("z"))
            if x is None or y is None or z is None:
                if pos is None:
                    self._territory_note = "（得给我这个箱子的确切坐标，我才能记住）"
                    return False
                x, y, z = pos
            row = store.remember_container(
                name, x=x, y=y, z=z, kind=str(payload.get("kind") or "chest"),
                policy=final_policy, owner=owner,
                granted_by=self._granted_by(context))
        if row is None:
            self._territory_note = "（这个箱子的信息不完整，我没记）"
            return False
        tail = ""
        if decision.get("clamped"):
            tail = "；你要的随取权超了 friend 的授权，我按传递权（借了要还）记"
        policy_text = {"free": "随取（所有权已交给我）",
                       "borrow_only": "只能借（借多少还多少）",
                       "no_touch": "不许碰"}.get(row["policy"], row["policy"])
        self._territory_note = (f"（记住了箱子「{row['name']}」@({row['x']:.0f},{row['y']:.0f},"
                                f"{row['z']:.0f})，策略：{policy_text}{tail}）")
        logger.info("[Planner] 容器策略 %s → %s", row["name"], row["policy"])
        return True

    def _dispatch_revoke_container(self, payload: dict, context: dict) -> bool:
        """收回箱子的访问授权（**立即生效**）：master 任意，friend 只能收回自己的。"""
        store = self._territory_store()
        name = str(payload.get("name") or "").strip()
        if store is None or not name:
            self._territory_note = "（要收回哪个箱子得说清名字）"
            return False
        tier, actor = self._actor_of(context)
        existing = store.container(name)
        if existing is None:
            self._territory_note = f"（没有叫「{name}」的容器授权）"
            return False
        permissions = self.permissions
        if permissions is None or not permissions.can_authorize_containers(tier):
            self._territory_note = self._no_authority_note("收回容器授权", context)
            return False
        if tier != "master" and str(existing.get("owner") or "").casefold() != actor.casefold():
            self._territory_note = "（friend 只能收回自己箱子的授权，这个不是你的）"
            return False
        removed = store.revoke_container(name)
        if removed is None:
            self._territory_note = f"（没有叫「{name}」的容器授权）"
            return False
        self._territory_note = f"（已收回箱子「{name}」的访问授权，立刻生效）"
        return True

    def _dispatch_define_zone(self, payload: dict, context: dict) -> bool:
        """声明区域：**语义 + 硬保护**（调 Java 硬拦，同时 world 层存盘以便回灌）。授权由注册表统一校验。"""
        store = self._territory_store()
        position = self._player_pos(context)
        x = _as_optional_float(payload.get("x"))
        y = _as_optional_float(payload.get("y"))
        z = _as_optional_float(payload.get("z"))
        if x is None or y is None or z is None:
            if position is None:
                self._territory_note = "（区域得有中心坐标）"
                return False
            x, y, z = position
        radius = _as_optional_float(payload.get("radius"))
        box = payload.get("box") if isinstance(payload.get("box"), dict) else None
        if radius is None and box is None:
            self._territory_note = "（区域得有形状：给半径或 box）"
            return False
        if radius is not None and radius <= 0:
            self._territory_note = "（半径得是正数）"
            return False
        protect = payload.get("protect")
        row = store.set_zone({
            "name": str(payload.get("name") or "").strip(),
            "kind": str(payload.get("kind") or "").strip().lower(),
            "center": {"x": x, "y": y, "z": z},
            "radius": radius,
            "box": box,
            "protect": protect,
        }, granted_by=self._granted_by(context)) if store is not None else None
        if row is None:
            self._territory_note = "（区域得有名字和形状，这条没记）"
            return False
        zone_args = {"name": row["name"], "kind": str(row.get("kind") or "")}
        zone_args.update({"x": x, "y": y, "z": z})
        if radius is not None:
            zone_args["radius"] = radius
        if box is not None:
            zone_args["box"] = box
        if row.get("protect"):
            zone_args["protect"] = list(row["protect"])
        skills = getattr(self, "skills", None)
        define = getattr(skills, "define_zone", None) if skills is not None else None
        if callable(define):
            define(**zone_args)
        protect_text = "/".join(str(item) for item in (row.get("protect") or [])) or "无"
        self._territory_note = (f"（已声明区域「{row['name']}」（{row.get('kind') or 'protect'}），"
                                f"保护：{protect_text}；这儿的这些动作会被硬拦）")
        logger.info("[Planner] 区域声明 %s protect=%s", row["name"], row.get("protect"))
        return True

    def _dispatch_clear_zone(self, payload: dict, context: dict) -> bool:
        store = self._territory_store()
        name = str(payload.get("name") or "").strip()
        if store is None or not name:
            self._territory_note = "（要删哪个区域得说清名字）"
            return False
        removed = store.clear_zone(name)
        skills = getattr(self, "skills", None)
        clear = getattr(skills, "clear_zone", None) if skills is not None else None
        if callable(clear):
            clear(name)
        if removed is None:
            self._territory_note = f"（本地没有叫「{name}」的区域，已让模组删除）"
            return True
        self._territory_note = f"（已删除区域「{name}」）"
        return True

    @staticmethod
    def _bed_in_base(base: dict, beds: Any) -> Optional[tuple[float, float, float]]:
        """基地半径内的床（按锚点距离筛，取最近的一张）。"""
        anchor = point_of(base.get("anchor"))
        radius = base.get("radius")
        if anchor is None or not isinstance(radius, (int, float)):
            return None
        best: Optional[tuple[float, tuple[float, float, float]]] = None
        for bed in beds or []:
            if not isinstance(bed, dict):
                continue
            x, y, z = bed.get("x"), bed.get("y"), bed.get("z")
            if any(not isinstance(value, (int, float)) or isinstance(value, bool)
                   for value in (x, y, z)):
                continue
            distance = ((float(x) - anchor[0]) ** 2 + (float(y) - anchor[1]) ** 2
                        + (float(z) - anchor[2]) ** 2) ** 0.5
            if distance > float(radius):
                continue
            if best is None or distance < best[0]:
                best = (distance, (float(x), float(y), float(z)))
        return best[1] if best is not None else None

    def _claim_pending_home_bed(self, context: dict) -> None:
        """上一轮在造床、这一轮床边出现了 → 认成 `home_bed`（造 + 放 + 认领的最后一步）。"""
        name = self._pending_home_bed_base
        store = self._territory_store()
        if not name or store is None:
            return
        base = store.base(name)
        if base is None:
            self._pending_home_bed_base = None
            return
        bed = self._bed_in_base(base, (context or {}).get("nearby_beds"))
        if bed is None:
            return
        row = store.set_home_bed(name, {"x": bed[0], "y": bed[1], "z": bed[2]})
        self._pending_home_bed_base = None
        if row is not None:
            self._territory_note = (f"（床造好了，认成基地「{name}」的床位 "
                                    f"({bed[0]:.0f},{bed[1]:.0f},{bed[2]:.0f})，以后回这儿睡）")

    def _dispatch_set_home(self, payload: dict, context: dict) -> bool:
        """在基地范围内**自己找床**；找不到就**自己造**（缺料/不安全如实报，不硬凑）。"""
        store = self._territory_store()
        if store is None:
            self._territory_note = "（没有基地/领地记录，先给我设一个基地）"
            return False
        base = self._base_by_name(payload.get("base"), context)
        if base is None:
            self._territory_note = "（不知道是哪个基地：先设一个基地，或说清名字）"
            return False
        bed = self._bed_in_base(base, (context or {}).get("nearby_beds"))
        if bed is not None:
            store.set_home_bed(base["name"], {"x": bed[0], "y": bed[1], "z": bed[2]})
            # 只记坐标**不等于**绑上了：原版要**右键一次**才会把重生点绑到这张床
            # （用户纠正的语义：白天/晴天也行，"现在不能睡"只是不睡、不影响绑定）。
            # 所以这里起一条"走过去 → 右键 → 用重生点核对"的链路，成功了才算家。
            self._home_bed = home_bed.start(bed, base=str(base["name"]))
            self._territory_note = (f"（基地「{base['name']}」里找到床 "
                                    f"({bed[0]:.0f},{bed[1]:.0f},{bed[2]:.0f})；现在走过去**右键一次**"
                                    "把重生点绑到它 —— 不绑的话死了还是回世界出生点）")
            return True
        entity_view = perception.entities_rows(context)
        decision = perception.conservative_bed_decision(
            bed_attempt((context or {}).get("inventory"), entity_view["rows"]), entity_view)
        if decision.get("action") == "craft":
            skills = getattr(self, "skills", None)
            craft = getattr(skills, "craft_via_recipe", None) if skills is not None else None
            if callable(craft):
                craft("white_bed", 1)
            self._pending_home_bed_base = str(base["name"])
            self._territory_note = f"（基地「{base['name']}」里没床，材料够，我造一张）"
            return True
        self._territory_note = (perception.bed_caution_note(hostiles=decision.get("hostiles"))
                                if decision.get("partial") else bed_note(decision))
        logger.info("[Planner] 造床判定：%s", decision)
        return False

    @staticmethod
    def _memory_home_point(context: Any) -> Optional[tuple[float, float, float]]:
        """模组报的**家位置**（带 `home` 标签的记忆点，由 session 归一化进 `home_status`）。

        只作 store 没有可用坐标时的兜底；坐标永远取**这一轮**的 L2 上下文（现读，不吃历史值）。
        """
        situation = (context or {}).get("home_status") if isinstance(context, dict) else None
        if not isinstance(situation, dict):
            return None
        return point_of(situation.get("home_point"))

    def _death_points(self, context: Any) -> list[dict]:
        """现读记着的死亡点（最新在前，最多 3 条）——不吃上一轮快照。

        世界记忆（`WorldPoiMemory`）是真相；没注入它时（例如只拿了 L2 上下文的情形）
        才退到这一轮上下文里那份，**不混用两个来源**。
        """
        reader = getattr(getattr(self, "poi", None), "death_points", None)
        if callable(reader):
            try:
                rows = reader()
            except Exception as e:  # noqa: BLE001
                logger.debug("[Planner] 死亡点读取失败: %s", e)
                rows = None
            if isinstance(rows, list):
                return [dict(row) for row in rows if isinstance(row, dict)]
        rows = context.get("death_points") if isinstance(context, dict) else None
        if not isinstance(rows, list):
            return []
        return [dict(row) for row in rows if isinstance(row, dict)]

    def _render_death_point(self, context: Any) -> str:
        """L2 的死亡点那**一行**；没有记录 ⇒ 空串（整段不出现，绝不编一个）。"""
        points = self._death_points(context)
        if not points:
            return ""
        return death_point_line(points[0], older=len(points) - 1)

    def _dispatch_go_to_death_point(self, payload: dict, context: dict) -> bool:
        """回**上次死亡点**（不是回家 —— 回家是 go_home）：走已有 `move_to` 那条路。

        **每轮现读**世界记忆；没有任何死亡记录就**如实拒绝**（code=no_death_point，一帧都
        不发 —— 绝不假装走了一趟）。纯读：不改写任何记忆，也不自动往回走。
        """
        payload = payload if isinstance(payload, dict) else {}
        points = self._death_points(context)
        if not points:
            self._territory_note = ("（go_to_death_point：code=no_death_point —— 我不知道你上次"
                                    "死在哪（没有任何死亡记录），**我没动**；回家用 go_home）")
            return False
        raw = payload.get("index")
        chosen: Optional[int] = 0
        if raw not in (None, ""):
            if isinstance(raw, bool) or (isinstance(raw, float) and not raw.is_integer()):
                chosen = None
            else:
                try:
                    chosen = int(raw)
                except (TypeError, ValueError):
                    chosen = None
        if chosen is None or not 0 <= chosen < len(points):
            self._territory_note = (
                f"（go_to_death_point：code=no_such_death_point —— 只记着最近 {len(points)} 次"
                f"死亡，没有 index={raw} 这一次；省 index 走最近的那次）")
            return False
        target = point_of(points[chosen])
        if target is None:
            # 记着的条目坐标不合法（理论上不该有）：宁可如实说不知道，也不乱走。
            self._territory_note = ("（go_to_death_point：code=no_death_point —— 记着的那次死亡"
                                    "没有可用坐标，**我没动**）")
            return False
        self.skills.move_to(target[0], target[1], target[2])
        coords = ",".join(where._fmt(value) for value in target)
        dimension = str(points[chosen].get("dimension") or "").strip()
        when = _death_ago(points[chosen].get("at"))
        facts = "，".join(part for part in (dimension, when) if part)
        label = ("上次死亡点" if chosen == 0
                 else "上上次死亡点" if chosen == 1
                 else f"更早的死亡点（index={chosen}）")
        facts_text = f"（{facts}）" if facts else ""
        self._territory_note = (f"（往{label}走：({coords}){facts_text} —— "
                                f"那里的东西走过去就会自动捡）")
        return True

    def _dispatch_go_home(self, payload: dict, context: dict) -> bool:
        """回基地：有床位就去床位，没床位就去锚点（**每轮现读 store**，不吃上一轮快照）。

        store 里没有可用坐标（还没设基地 / 锚点坏了）时退到模组报的**家记忆**：家还在的事实
        由模组核对，位置就是那个 `home` 标签点的坐标（不猜、不造）。
        """
        store = self._territory_store()
        base = self._base_by_name(payload.get("base"), context) if store is not None else None
        if base is not None:
            bed = point_of(base.get("home_bed"))
            target = bed or point_of(base.get("anchor"))
            if target is not None:
                self.skills.move_to(target[0], target[1], target[2])
                where = "床位" if bed else "锚点"
                self._territory_note = (f"（往基地「{base['name']}」的{where}走："
                                        f"({target[0]:.0f},{target[1]:.0f},{target[2]:.0f})）")
                return True
        memory_home = self._memory_home_point(context)
        if memory_home is not None:
            self.skills.move_to(memory_home[0], memory_home[1], memory_home[2])
            self._territory_note = (f"（往记着的家走：({memory_home[0]:.0f},{memory_home[1]:.0f},"
                                    f"{memory_home[2]:.0f})）")
            return True
        if store is None:
            self._territory_note = "（没有基地记录，不知道回哪）"
        elif base is None:
            self._territory_note = "（不知道是哪个基地：先设一个基地，或说清名字）"
        else:
            self._territory_note = f"（基地「{base['name']}」没有可用坐标）"
        return False

    # ── 搬运 haul（§4）：算量 → 取 → 送 → 两侧对账（跨轮推进）────────

    SELF_NAMES = ("self", "me", "inventory", "backpack", "自己", "背包")

    @classmethod
    def _is_self_side(cls, name: Any) -> bool:
        """这一侧是不是"我自己背包"（`from/to` 里的 `self`）。"""
        return str(name or "").strip().lower() in cls.SELF_NAMES

    @staticmethod
    def _container_block(row: Optional[dict]) -> str:
        """记住的容器 → 走过去用的方块 id（kind 认不出就按箱子）。"""
        kind = str((row or {}).get("kind") or "").strip() or "chest"
        return kind if ":" in kind else f"minecraft:{kind}"

    def _haul_moved_text(self, moved: Any) -> str:
        rows = {str(item): int(count) for item, count in (moved or {}).items()
                if int(count or 0) > 0}
        return "、".join(f"{item} x{count}" for item, count in rows.items()) or "0 件"

    @staticmethod
    def _haul_kinds(job: HaulJob) -> str:
        """台账里的"搬的是什么"（多物品/整盒/盒内取都说得清）。"""
        if job.items:
            return "+".join(sorted(job.items))
        if job.mode == MODE_WHOLE and job.item:
            return f"整盒:{job.item}"
        if job.mode == MODE_INSIDE and job.item:
            return f"盒内:{job.item}"
        return job.item or "*"

    def _haul_want_text(self, job: HaulJob) -> str:
        if job.items:
            return "、".join(f"{item} x{job.items[item]}" if job.items[item] else item
                            for item in job.items)
        if job.mode == MODE_WHOLE and job.item:
            return f"{job.item} 整盒（连内容一起）"
        if job.mode == MODE_INSIDE and job.item:
            return f"{job.item}（只取盒里的）"
        if job.item:
            return f"{job.item} x{job.want}" if job.want else f"{job.item}（能搬多少算多少）"
        return "全部可搬的"

    def _haul_container_in_reach(self, name: str, row: Optional[dict], context: Any) -> bool:
        """这个容器就在跟前吗（用 nearby_storage / known_containers 的距离与坐标判，绝不猜）。"""
        ctx = context if isinstance(context, dict) else {}
        rows: list[dict] = []
        for key in ("nearby_storage", "known_containers"):
            value = ctx.get(key)
            if isinstance(value, (list, tuple)):
                rows.extend(item for item in value if isinstance(item, dict))
        pos = point_of(row) if row else None
        for item in rows:
            title = str(item.get("title") or item.get("name") or "")
            if name and title and title != name:
                continue
            distance = item.get("distance")
            if (isinstance(distance, (int, float)) and not isinstance(distance, bool)
                    and distance <= 4.0):
                return True
            if pos is None:
                continue
            try:
                dx = float(item.get("x")) - pos[0]
                dy = float(item.get("y")) - pos[1]
                dz = float(item.get("z")) - pos[2]
            except (TypeError, ValueError):
                continue
            if abs(dx) <= 4 and abs(dy) <= 4 and abs(dz) <= 4:
                return True
        return False

    def _haul_open_matches(self, open_container: Any, name: str, row: Optional[dict],
                           player_pos: Any = None) -> bool:
        """现在开着的就是这个名字的容器吗：标题对得上，或人已经站在它记录的位置上。

        Java 的 `open_container` 只报 `title`（界面标题），所以默认名箱子（"Chest"）
        对不上我们记的名字 —— 这时用"人就在它记录的坐标上"判，**不猜**别的容器。
        """
        if not isinstance(open_container, dict) or open_container.get("id") is None:
            return False
        title = str(open_container.get("title") or open_container.get("name") or "").strip()
        if title and name and title.casefold() == str(name).strip().casefold():
            return True
        pos = point_of(row) if row else None
        if pos is None or player_pos is None:
            return False
        return (abs(player_pos[0] - pos[0]) <= 3 and abs(player_pos[1] - pos[1]) <= 3
                and abs(player_pos[2] - pos[2]) <= 3)

    def _dispatch_haul(self, payload: dict, context: dict) -> bool:
        """`haul {from,to,item?,count?}`：算量 → 取 → 走到 B → 放 → **两侧对账**（§4）。

        硬约束（**入口先挡，绝不"试一下"**）：`no_touch` 一律不碰；`borrow_only` 作为
        **源**必须走借用台账（haul 不许绕过借还）；没被授权翻箱就不动手。
        """
        store = self._territory_store()
        src_name = str(payload.get("from") or payload.get("source") or "").strip()
        dst_name = str(payload.get("to") or payload.get("target") or "").strip()
        src_self = self._is_self_side(src_name)
        dst_self = self._is_self_side(dst_name)
        if not src_name or not dst_name:
            return self._haul_refuse("", "", STEP_OPEN_SOURCE, "need_endpoints",
                                     "（搬运要说清 from/to：容器名或 self）")
        if src_self and dst_self:
            return self._haul_refuse(src_name, dst_name, STEP_OPEN_SOURCE, "self_to_self",
                                     "（from/to 都是我自己：那就等于没搬）")
        if src_name.casefold() == dst_name.casefold():
            return self._haul_refuse(src_name, dst_name, STEP_OPEN_SOURCE, "same_container",
                                     "（from/to 是同一个容器：没得搬）")
        src_row = None if src_self else (store.container(src_name) if store is not None else None)
        dst_row = None if dst_self else (store.container(dst_name) if store is not None else None)
        if not src_self and src_row is None:
            return self._haul_refuse(src_name, dst_name, STEP_OPEN_SOURCE, "unknown_container",
                                     f"（没记住「{src_name}」在哪：先让我记住这个箱子）")
        if not dst_self and dst_row is None:
            return self._haul_refuse(src_name, dst_name, STEP_OPEN_TARGET, "unknown_container",
                                     f"（没记住「{dst_name}」在哪：先让我记住这个箱子）")
        for row, step in ((src_row, STEP_OPEN_SOURCE), (dst_row, STEP_OPEN_TARGET)):
            if row is not None and normalize_policy(row.get("policy")) == POLICY_NO_TOUCH:
                return self._haul_refuse(src_name, dst_name, step, "no_touch",
                                         self._container_policy_note(row))
        if src_row is not None and normalize_policy(src_row.get("policy")) == POLICY_BORROW_ONLY:
            return self._haul_refuse(
                src_name, dst_name, STEP_TAKE, "borrow_only_use_borrow_item",
                f"（「{src_name}」是 borrow_only：取东西只能走 borrow_item/return_item 借还，"
                f"haul 不许绕过它）")
        policy = self._craft_policy(context)
        if not policy.may_open_containers:
            for row in (src_row, dst_row):
                if row is not None and normalize_policy(row.get("policy")) != POLICY_FREE:
                    return self._haul_refuse(src_name, dst_name, STEP_OPEN_SOURCE,
                                             "container_not_authorized",
                                             self._auth_note(policy, "翻箱子"))
        count = payload.get("count")
        want = _as_optional_int(count)
        if count is not None and str(count).strip() not in ("", "None") and want is None:
            return self._haul_refuse(src_name, dst_name, STEP_TAKE, "bad_count",
                                     "（count 得是个数；要么别给，要么给个正整数）")
        raw_item = str(payload.get("item") or "").strip()
        raw_box = str(payload.get("box") or "").strip()
        wants = normalize_items(payload.get("items"))
        mode = normalize_mode(payload.get("mode"), MODE_ITEM)
        item_id = normalize_item(raw_item) if raw_item else ""
        # 没说 mode、但点名要搬的就是个"盒子" → 按**整盒搬**理解（连内容一起走，不拆散）。
        if mode == MODE_ITEM and item_id and is_container_item(item_id) \
                and str(payload.get("mode") or "").strip() == "":
            mode = MODE_WHOLE
        if mode == MODE_WHOLE and not item_id:
            return self._haul_refuse(src_name, dst_name, STEP_TAKE, "whole_needs_item",
                                     "（整盒搬要说清是哪个盒子（item=盒子 id））")
        if mode == MODE_INSIDE and not item_id:
            return self._haul_refuse(src_name, dst_name, STEP_TAKE, "inside_needs_item",
                                     "（从盒里取东西要说清取哪件（item=物品 id））")
        if wants and mode != MODE_ITEM:
            return self._haul_refuse(src_name, dst_name, STEP_TAKE, "mode_items_conflict",
                                     "（items 是普通多物品搬运，别和 mode=whole/inside 一起用）")
        job = HaulJob(src=src_name, dst=dst_name, item=item_id, want=max(0, want or 0),
                      mode=mode, box=normalize_item(raw_box) if raw_box else "",
                      items=dict(wants),
                      src_side=SIDE_PLAYER if src_self else SIDE_CONTAINER,
                      dst_side=SIDE_PLAYER if dst_self else SIDE_CONTAINER)
        if src_self:
            job.wanted, job.phase, job.step = "dst", "put", STEP_WALK_TARGET
        elif dst_self:
            job.wanted, job.phase, job.step = "src", "take", STEP_WALK_SOURCE
        else:
            # 两边都是箱子：先**探**一眼目标箱（算量要受"目标箱剩余空间"约束）。
            job.wanted, job.phase, job.step = "dst", "probe", STEP_WALK_TARGET
        self._haul_job = job
        self._haul_rows = {"src": src_row, "dst": dst_row}
        self._haul_audit.append({"ts": time.time(), "from": job.src, "to": job.dst,
                                 "item": self._haul_kinds(job), "want": int(job.want),
                                 "moved": {}, "result": "started", "reason": ""})
        del self._haul_audit[:-50]
        logger.info("[Planner] haul 开始：%s → %s（%s）", job.src, job.dst,
                    self._haul_want_text(job))
        return self.haul_step(context)

    def _haul_refuse(self, src: Any, dst: Any, step: str, reason: str, note: str) -> bool:
        """入口就挡下的一次搬运：回执照契约记，人话照实说（一帧都不发）。"""
        self._haul_last = failure_receipt(step, src=src, dst=dst, moved=None, reason=reason)
        self._haul_note = note
        self._haul_audit.append({"ts": time.time(), "from": str(src or ""), "to": str(dst or ""),
                                 "item": "", "want": 0, "moved": {},
                                 "result": f"refused:{reason}", "reason": reason})
        del self._haul_audit[:-50]
        logger.info("[Planner] haul 被拒（%s）：%s → %s", reason, src, dst)
        return False

    def _haul_fail(self, job: HaulJob, step: str, reason: str, note: str) -> bool:
        """如实报失败（卡在哪一步 + 手头还剩什么没放好），绝不假装搬完。"""
        short = job.shortfall()
        tail = f"（{self._haul_moved_text(short)} 还留在我背包里）" if short else ""
        job.awaiting = ""
        job.receipt = failure_receipt(step, src=job.src, dst=job.dst, moved=job.moved,
                                      reason=reason,
                                      nested=job.nested if job.nested_active else None)
        job.step = step
        self._haul_last = job.receipt
        self._haul_note = f"{note}{tail}"
        self._haul_audit.append({"ts": time.time(), "from": job.src, "to": job.dst,
                                 "item": self._haul_kinds(job), "want": int(job.want),
                                 "moved": dict(job.moved), "result": f"failed:{reason}",
                                 "reason": reason})
        del self._haul_audit[:-50]
        logger.info("[Planner] haul 失败（%s，step=%s）：%s → %s", reason, step, job.src, job.dst)
        self._haul_job = None
        return False

    def _haul_finish(self, job: HaulJob) -> bool:
        """对账通过才算搬完：记回执 + 台账（源箱留底、没搬空）。"""
        job.receipt = success_receipt(job.src, job.dst, job.moved,
                                      nested=job.nested if job.nested_active else None)
        job.step = STEP_DONE
        job.awaiting = ""
        self._haul_last = job.receipt
        if job.nested_active:
            inner = dict((job.nested or {}).get("inner") or {})
            extra = f"；盒里内容物 {self._haul_moved_text(inner)} 也一并对上了" if inner else ""
            self._haul_note = (f"（搬好了：从「{job.src}」到「{job.dst}」共 "
                               f"{self._haul_moved_text(job.moved)}（整盒没拆散）{extra}；"
                               f"源箱照旧留底没搬空）")
        else:
            self._haul_note = (f"（搬好了：从「{job.src}」到「{job.dst}」共 "
                               f"{self._haul_moved_text(job.moved)}；两侧已对账，源箱照旧留底没搬空）")
        self._haul_audit.append({"ts": time.time(), "from": job.src, "to": job.dst,
                                 "item": self._haul_kinds(job), "want": int(job.want),
                                 "moved": dict(job.moved), "result": "done", "reason": ""})
        del self._haul_audit[:-50]
        logger.info("[Planner] haul 完成：%s → %s %s", job.src, job.dst, job.moved)
        self._haul_job = None
        return True

    def _haul_plan_note(self, job: HaulJob, plan: dict) -> str:
        reason = str(plan.get("reason") or "")
        what = self._haul_want_text(job)
        src = "我背包" if job.src_side == SIDE_PLAYER else f"「{job.src}」"
        if reason == "source_empty":
            return f"（{src}里没有可搬的东西）"
        if reason == "item_absent":
            return f"（{src}里没有 {job.item}）"
        if reason == "source_floor":
            return f"（{src}的 {what} 只剩底了：要留底，我不搬空）"
        if reason == "target_full":
            return f"（「{job.dst}」放不下 {what}）"
        return "（这一趟没有可搬的量）"

    def _haul_box_note(self, job: HaulJob, result: dict) -> str:
        """整盒搬取不出来时的**如实**说法。"""
        reason = str(result.get("reason") or "")
        if reason == "box_absent":
            return f"（「{job.src}」里没有 {job.item} 这个盒子，整盒搬不了）"
        if reason == "source_floor":
            return f"（「{job.src}」的 {job.item} 只剩底了：要留底，我不搬空整盒）"
        if reason == "no_space":
            return "（我背包放不下这些盒子）"
        if reason == "no_layout":
            return "（源箱快照没给出槽位布局，整盒搬不了）"
        return "（整盒搬没成）"

    def _haul_inside_note(self, job: HaulJob, result: dict) -> str:
        """盒内取取不出来时的**如实**说法。"""
        reason = str(result.get("reason") or "")
        where = f"「{job.box}」" if job.box else "盒子"
        if reason == "not_in_box":
            return f"（{where}里没有 {job.item}，盒内取不到）"
        if reason == "nested_not_visible":
            return (f"（{where}里可能有 {job.item}，但快照没报能取的内容槽位 —— "
                    f"我取不出来，不假装取到了）")
        if reason == "source_floor":
            return f"（{where}里的 {job.item} 只剩底了：要留底，我不拿空）"
        if reason == "no_layout":
            return "（源箱快照没给出槽位布局，盒内取不了）"
        return "（盒内取没成）"

    def _haul_capacity_for_take(self, job: HaulJob, oc: Any) -> Optional[dict]:
        """算量用的**目标侧剩余空间**：目标是自己背包就按背包侧算，是箱子就用探测到的快照。

        探测不到（目标箱还没开过）→ None（只按需求/上限节流，放不放得下在 put 那一步
        用**真实快照**再卡一次）。
        """
        kinds = list(job.items) if job.items else ([job.item] if job.item
                                                   else sorted(side_items(oc, job.src_side)))
        if not kinds:
            return {}
        if job.dst_side == SIDE_PLAYER:
            return {item: side_free_space(oc, item, SIDE_PLAYER) for item in kinds}
        if isinstance(job.dst_before, dict):
            return {item: side_free_space(job.dst_before, item, SIDE_CONTAINER)
                    for item in kinds}
        return None

    def haul_step(self, context: dict) -> bool:
        """把当前搬运推进一步（空闲时 session 也会调它；等对账时不重复下发）。"""
        job = self._haul_job
        if job is None or job.done:
            return False
        if job.awaiting:
            # 已下发取/放，等一份**新**快照来对账（Java 侧内容一变就立刻上报）。
            if time.time() - float(job.awaiting_since or 0.0) > HAUL_VERIFY_TIMEOUT:
                return self._haul_fail(job, STEP_VERIFY, "verify_no_snapshot",
                                       "（搬前搬后的快照没等到，对账做不了 —— 我不假装搬完了）")
            return False
        oc = self._open_container(context)
        rows = self._haul_rows if isinstance(self._haul_rows, dict) else {}
        key = job.wanted
        row = rows.get(key)
        name = job.src if key == "src" else job.dst
        if row is None:
            # 这一侧是"我自己"（背包）：从一个开着的菜单里读它的背包侧就够了。
            return self._haul_on_open(job, oc, context)
        if self._haul_open_matches(oc, name, row, self._player_pos(context)):
            return self._haul_on_open(job, oc, context)
        return self._haul_approach(job, key, context)

    def _haul_approach(self, job: HaulJob, key: str, context: dict) -> bool:
        """走到容器跟前并右键打开（只用 go_to_block / look_at / open_container 三个原语）。"""
        rows = self._haul_rows if isinstance(self._haul_rows, dict) else {}
        row = rows.get(key)
        name = job.src if key == "src" else job.dst
        if row is None:
            self._haul_note = f"（「{name}」不是记过的箱子，我不知道它在哪）"
            return False
        step = STEP_OPEN_SOURCE if key == "src" else STEP_OPEN_TARGET
        if key not in job.walked:
            job.walked.add(key)
            job.walked_since = time.time()
            self.skills.go_to_block(self._container_block(row), 32, True)
            self._haul_note = f"（走去找「{name}」）"
            return True
        if (self._haul_container_in_reach(name, row, context)
                and time.time() - float(job.walked_since or 0.0) <= HAUL_WALK_TIMEOUT):
            pos = point_of(row)
            if pos is not None:
                self.skills.look_at(pos[0], pos[1], pos[2])
            self.skills.open_container()
            job.attempts += 1
            if job.attempts > HAUL_MAX_ATTEMPTS:
                return self._haul_fail(job, step, f"cannot_open_{key}",
                                       f"（走到「{name}」边上了，但它一直没打开）")
            self._haul_note = f"（到「{name}」跟前了，右键打开）"
            return True
        if not job.walked_since or time.time() - float(job.walked_since) > HAUL_WALK_TIMEOUT:
            return self._haul_fail(
                job, STEP_WALK_SOURCE if key == "src" else STEP_WALK_TARGET, "no_path",
                f"（走不到「{name}」：寻路失败或卡住了 —— 我不搭方块硬凑）")
        self._haul_note = f"（还没走到「{name}」）"
        return False

    def _haul_on_open(self, job: HaulJob, oc: Any, context: dict) -> bool:
        """容器开着（或这一侧就是背包）时该干的事：探测 / 取 / 放 / 对账。"""
        if job.phase == "probe":
            if not has_layout(oc):
                return self._haul_fail(job, STEP_OPEN_TARGET, "no_layout",
                                       f"（「{job.dst}」的快照没给出槽位布局，算不出剩余空间）")
            job.dst_before = copy.deepcopy(oc)
            self.skills.container_close()
            job.walked.clear()
            job.attempts = 0
            job.phase, job.wanted, job.step = "take", "src", STEP_WALK_SOURCE
            self._haul_note = f"（先看清楚「{job.dst}」还剩多少空间，再去「{job.src}」取）"
            return True
        if job.phase == "take":
            return self._haul_take(job, oc, context)
        if job.phase == "put":
            return self._haul_put(job, oc, context)
        return False

    def _nested_blocked_names(self) -> list[str]:
        """已登记的 `no_touch` 容器名（递归 no_touch 判定用）。"""
        store = self._territory_store()
        if store is None:
            return []
        return [str(row.get("name") or "") for row in store.containers()
                if normalize_policy(row.get("policy")) == POLICY_NO_TOUCH]

    def _haul_nested_gate(self, job: HaulJob, oc: Any) -> Optional[tuple[str, str]]:
        """整盒搬 / 盒内取之前的嵌套检查：`(reason, note)`；没问题返回 None。

        - **递归 `no_touch`**：盒内任一层命中 no_touch 容器 → **连盒带物都不能动**；
        - **深度/总量上限**：超了**如实拒结并说明**（绝不静默截断）。
        """
        rows = [row for row in container_rows(oc)
                if (not job.item or row.get("item") == job.item)]
        hit = recursive_no_touch(rows, blocked=self._nested_blocked_names())
        if hit:
            return ("nested_no_touch",
                    f"（「{job.item}」里有 no_touch 的东西（第 {hit['depth']} 层"
                    f"{('：' + hit['name']) if hit.get('name') else ''}）——"
                    f"连盒带物我都不动）")
        limits = nested_limits(rows)
        if not limits.get("ok"):
            return (str(limits.get("reason") or "nested_limit"),
                    f"（「{job.item}」的嵌套{limits.get('detail') or ''}：超上限了，"
                    f"我如实拒结，不硬搬也不截断）")
        return None

    def _haul_take(self, job: HaulJob, oc: Any, context: dict) -> bool:
        """算量并取出（受目标侧剩余空间 + 本次需求 + 单趟上限约束，源箱留底）。

        `mode=whole` → **整盒搬**（连内容一起走，不拆散，对账含内容物）；
        `mode=inside` → **先取出盒里的这件**再搬（不搬整盒）；
        否则按普通物品（可多物品 `items`）搬。
        """
        if not has_layout(oc):
            return self._haul_fail(job, STEP_TAKE, "no_layout",
                                   "（源箱快照没给出槽位布局，算不出取多少）")
        if job.mode in (MODE_WHOLE, MODE_INSIDE):
            gate = self._haul_nested_gate(job, oc)
            if gate is not None:
                return self._haul_fail(job, STEP_TAKE, gate[0], gate[1])
        job.src_before = copy.deepcopy(oc)
        if job.mode == MODE_WHOLE:
            return self._haul_take_whole(job, oc)
        if job.mode == MODE_INSIDE:
            return self._haul_take_inside(job, oc)
        plan = plan_haul(oc, src_side=job.src_side, item=job.item, want=job.want,
                         wants=(job.items or None),
                         capacity=self._haul_capacity_for_take(job, oc))
        if plan.get("reason"):
            return self._haul_fail(job, STEP_TAKE, str(plan["reason"]),
                                   self._haul_plan_note(job, plan))
        moved: dict[str, int] = {}
        for item_id, take in plan["items"].items():
            item_moves = plan_take_moves(oc, item_id, take)
            if not item_moves:
                continue
            for move in item_moves:
                self.skills.container_move(move["from_slot"], move["to_slot"], move["count"])
            moved[item_id] = sum(int(move["count"]) for move in item_moves)
        if not moved:
            return self._haul_fail(job, STEP_TAKE, "no_carry_space",
                                   "（这些量一件都没挪出来：背包放不下，或者箱子侧没料）")
        return self._haul_after_take_down(job, moved)

    def _haul_take_whole(self, job: HaulJob, oc: Any) -> bool:
        """**整盒搬**：盒子当整体搬（连内容一起），对账要含内容物。"""
        result = plan_box_take(oc, job.item, job.want, src_side=job.src_side)
        if result.get("reason"):
            return self._haul_fail(job, STEP_TAKE, str(result["reason"]),
                                   self._haul_box_note(job, result))
        for move in result["moves"]:
            self.skills.container_move(move["from_slot"], move["to_slot"], move["count"])
        boxes = int(result.get("boxes") or 0)
        contents = dict(result.get("contents") or {})
        job.nested_active = True
        job.nested = {"outer": {job.item: boxes}, "inner": contents}
        job.carried = {job.item: boxes}
        inner_text = self._haul_moved_text(contents) if contents else "空盒"
        self._haul_note = (f"（把「{job.src}」的 {job.item} x{boxes} **整盒**取出（盒里："
                           f"{inner_text}，不拆散），对账要连内容物一起比）")
        job.note_done = True
        return self._haul_after_take_down(job, job.carried)

    def _haul_take_inside(self, job: HaulJob, oc: Any) -> bool:
        """**盒内取**：把要的东西从盒里取出来再搬（不搬整盒）。"""
        result = plan_inside_take(oc, job.item, job.want, src_side=job.src_side, box=job.box)
        if result.get("reason"):
            return self._haul_fail(job, STEP_TAKE, str(result["reason"]),
                                   self._haul_inside_note(job, result))
        moved = {job.item: 0}
        for move in result["moves"]:
            self.skills.container_move(move["from_slot"], move["to_slot"], move["count"])
            moved[job.item] += int(move["count"])
        job.nested_active = True
        # 盒内取：源侧只跌**内层**、我这侧只涨**外层**（对账口径见 `reconcile_haul`）。
        job.nested = {"outer": {job.item: moved[job.item]},
                      "inner": {job.item: moved[job.item]}}
        self._haul_note = (f"（从「{job.src}」的盒子里取出 {job.item} x"
                           f"{moved[job.item]}，不搬整盒）")
        job.note_done = True
        return self._haul_after_take_down(job, moved)

    def _haul_after_take_down(self, job: HaulJob, moved: dict) -> bool:
        """取出之后的共用收尾：等对账 → 送去目标。"""
        job.carried = dict(moved)
        job.awaiting = "take"
        job.awaiting_since = time.time()
        job.attempts = 0
        job.step = STEP_VERIFY
        if job.dst_side == SIDE_PLAYER:
            job.phase, job.wanted = "verify", "src"
        else:
            job.phase, job.wanted = "put", "dst"
        if not job.note_done:
            self._haul_note = (f"（已从「{job.src}」取出 {self._haul_moved_text(moved)}，"
                               f"对账后送去「{job.dst}」）")
            job.note_done = True
        logger.info("[Planner] haul 取出 %s（%s 条移动）", moved, len(moved))
        return True

    def _haul_capacity_for_put(self, job: HaulJob, oc: Any) -> Optional[dict]:
        """放进目标箱时，算量用的**目标箱剩余空间**（就是眼下这份快照的真实值）。"""
        kinds = list(job.items) if job.items else ([job.item] if job.item
                                                   else sorted(side_items(oc, job.src_side)))
        if not kinds:
            return {}
        return {item: side_free_space(oc, item, SIDE_CONTAINER) for item in kinds}

    def _haul_put(self, job: HaulJob, oc: Any, context: dict) -> bool:
        """把要送的量放进目标箱（放不下的**留在背包里**并如实报，绝不假装放进去了）。"""
        if not has_layout(oc):
            return self._haul_fail(job, STEP_PUT, "no_layout",
                                   "（目标箱快照没给出槽位布局，放不进去）")
        if job.dst_before is None:
            job.dst_before = copy.deepcopy(oc)
        if job.src_side == SIDE_PLAYER and not job.carried:
            # 源就是我自己背包：要送多少得从**背包侧**算（受目标箱剩余空间 + 需求 + 上限）。
            # 「源箱留底」是防**搬空别人的箱子**；从自己背包往外搬不受这条限制。
            plan = plan_haul(oc, src_side=SIDE_PLAYER, item=job.item, want=job.want,
                             wants=(job.items or None),
                             capacity=self._haul_capacity_for_put(job, oc),
                             source_floor=0)
            if plan.get("reason"):
                return self._haul_fail(job, STEP_PUT, str(plan["reason"]),
                                       self._haul_plan_note(job, plan))
            job.carried = dict(plan["items"])
        short = job.shortfall()
        if not short:
            job.put_expected = {}
            job.awaiting = "put"
            job.awaiting_since = time.time()
            job.step = STEP_VERIFY
            return False
        moves: list[dict] = []
        planned: dict[str, int] = {}
        for item_id, count in short.items():
            item_moves = plan_put_moves(oc, item_id, count)
            if not item_moves:
                continue
            moves.extend(item_moves)
            planned[item_id] = sum(int(move["count"]) for move in item_moves)
        if not moves:
            return self._haul_fail(job, STEP_PUT, "target_full",
                                   f"（「{job.dst}」已经放不下了）")
        for move in moves:
            self.skills.container_move(move["from_slot"], move["to_slot"], move["count"])
        job.put_expected = planned
        job.awaiting = "put"
        job.awaiting_since = time.time()
        job.attempts = 0
        job.step = STEP_VERIFY
        self._haul_note = f"（往「{job.dst}」放进 {self._haul_moved_text(planned)}，等对账）"
        return True

    def verify_haul(self, open_container: Any, player_pos: Any = None) -> Optional[dict]:
        """一份**新**的容器快照到了 → 给当前搬运做两侧对账（真的对上才算数）。

        由 session 的 `state_update` 钩子调用（和借用归还核对同一个位置）。
        """
        job = self._haul_job
        if job is None or job.done or not job.awaiting:
            return None
        rows = self._haul_rows if isinstance(self._haul_rows, dict) else {}
        key = "src" if job.awaiting == "take" else "dst"
        name = job.src if key == "src" else job.dst
        if not self._haul_open_matches(open_container, name, rows.get(key), player_pos):
            return None
        job.attempts = 0
        if job.awaiting == "take":
            return self._haul_after_take(job, open_container)
        return self._haul_after_put(job, open_container)

    def _haul_after_take(self, job: HaulJob, oc: Any) -> bool:
        dst_side = job.dst_side if job.dst_side == SIDE_PLAYER else None
        moved = job.nested if job.nested_active else job.carried
        result = reconcile_haul(job.mode, "take", job.src_before, oc, moved,
                                src_side=job.src_side, dst_side=dst_side)
        if not result.get("ok"):
            return self._haul_fail(job, STEP_VERIFY, str(result.get("reason") or "verify_failed"),
                                   f"（取的时候对账不符：{result.get('detail') or ''}）")
        job.awaiting = ""
        self.skills.container_close()
        if job.dst_side == SIDE_PLAYER:
            job.moved = dict(job.carried)
            return self._haul_finish(job)
        job.walked.discard("dst")
        job.attempts = 0
        job.phase, job.wanted, job.step = "put", "dst", STEP_WALK_TARGET
        self._haul_note = (f"（源箱「{job.src}」已对账：取出 {self._haul_moved_text(job.carried)}，"
                           f"这就送到「{job.dst}」）")
        job.note_done = True
        return True

    def _haul_after_put(self, job: HaulJob, oc: Any) -> bool:
        planned = dict(job.put_expected or {})
        src_side = SIDE_PLAYER if job.src_side == SIDE_PLAYER else None
        moved = job.nested if (job.nested_active and job.mode == MODE_WHOLE) else planned
        result = reconcile_haul(job.mode, "put", job.dst_before, oc, moved,
                                src_side=src_side, dst_side=job.dst_side)
        if not result.get("ok"):
            return self._haul_fail(job, STEP_PUT, str(result.get("reason") or "verify_failed"),
                                   f"（放进目标箱时对账不符：{result.get('detail') or ''}）")
        job.awaiting = ""
        for item, count in planned.items():
            job.moved[item] = int(job.moved.get(item, 0)) + int(count)
        self.skills.container_close()
        short = job.shortfall()
        if short:
            return self._haul_fail(job, STEP_PUT, "put_short",
                                   f"（「{job.dst}」只放下了 "
                                   f"{self._haul_moved_text(planned)}，放不下的还在我背包里）")
        return self._haul_finish(job)

    def haul_last(self) -> Optional[dict]:
        """最近一次搬运的回执（`{ok, step, from, to, moved:{item,count}, reason}`）。"""
        return dict(self._haul_last) if self._haul_last else None

    def haul_status(self) -> dict:
        """当前搬运的进度（没在搬就是 `{}`）—— 面板/排查用，不含坐标。"""
        return self._haul_job.status() if self._haul_job is not None else {}

    def haul_audit(self, n: int = 10) -> list[dict]:
        """最近 n 条搬运台账（发起/完成/失败都记账）。"""
        rows = self._haul_audit[-int(n):] if n and int(n) > 0 else list(self._haul_audit)
        return [dict(row) for row in rows]

    # ── 种田 farm（§5、§11）：只在**授权农田**里，全走右键与挖 ──────

    def _farm_plot(self, name: str, store: Any) -> Optional[tuple]:
        """给名字就找那块地（区域或领地）的中心；认不出返回 None。"""
        if not name or store is None:
            return None
        zone = store.zone(name)
        if zone is not None:
            return point_of(zone)
        territory = store.territory(name)
        if territory is not None:
            return point_of(territory)
        return None

    def _dispatch_farm(self, payload: dict, context: dict) -> bool:
        """`farm {plot?,action?}`：只在**授权地块**里锄地/播种/收割/补种（§5、§11）。

        * 没有任何授权地块 → 如实拒绝（**不换地方偷干**）；
        * 命中区域保护 → 如实回报"区域名 + 规则"，**绝不绕过**（Java 侧也会硬拦）；
        * **别人家的田** → 只看权限表那一档（默认 none）：`help` 才准帮忙种、`mischief`
          才准（人设允许时）搞无伤大雅的恶作剧，且**每次动手都留审计**；
        * 缺种子/锄头 → 如实报缺什么（种子在背包里还是借来的都算）；
        * 动手**全走通用右键**（`use_item`）与 `mine_block` —— 没有特殊交互分支。
        """
        ctx = context if isinstance(context, dict) else {}
        store = self._territory_store()
        territories = store.territories() if store is not None else []
        zones = store.zones() if store is not None else []
        plot_name = str(payload.get("plot") or "").strip()
        position = self._player_pos(ctx)
        if plot_name:
            spot = self._farm_plot(plot_name, store)
            if spot is None:
                self._farm_note = f"（没有叫「{plot_name}」的农田/领地记录）"
                return False
        else:
            if position is None:
                self._farm_note = "（不知道我在哪，农活没法干）"
                return False
            spot = position
        verdict = may_act(territories, zones, spot[0], spot[1], spot[2], ACTION_FARM)
        # 没被授权、又不是区域硬拦 → 可能落在**别人家的田**上：看权限表那一档。
        others: Optional[dict] = None
        if not verdict.get("allowed") and verdict.get("reason") != "zone_protection":
            land = self._farm_land(store, spot)
            mode, owner = self._farm_others_mode(land)
            if mode != "own" and land is not None:
                others = {"land": land, "mode": mode, "owner": owner}
        if not verdict.get("allowed") and others is None:
            return self._farm_refuse(plot_name, verdict, "no_grant")
        if verdict.get("allowed"):
            plot_label = str(verdict.get("territory") or plot_name or "")
            owner = {"mine": True, "tier": "", "name": ""}
        else:
            owner = dict(others["owner"])
            plot_label = str((others["land"].get("row") or {}).get("name") or plot_name or "")
        counts = counts_of(ctx.get("inventory"))
        # 附近方块：形状交给 `perception.blocks_rows` 统一读（摘要只有计数 + 最近一种的距离，
        # **没有逐条坐标** ⇒ 挑不到具体目标时**必须**说"摘要里没有，得按需扫描"，不许说"附近没有"）。
        block_view = perception.blocks_rows(ctx)
        blocks = block_view["rows"]
        requested = str(payload.get("action") or "").strip()
        chosen = choose_action(requested, blocks, counts)
        action = str(chosen.get("action") or "")
        if not action:
            self._farm_note = farm_note(requested, plot_label, None,
                                        reason=str(chosen.get("reason") or "nothing_to_do"),
                                        partial=block_view["partial"],
                                        missing=missing_items(normalize_action(requested),
                                                              "", counts))
            logger.info("[Planner] farm 没活干：%s（partial=%s）",
                        chosen.get("reason"), block_view["partial"])
            return False
        target = chosen.get("target")
        tverdict = may_act(territories, zones, target["x"], target["y"], target["z"], ACTION_FARM)
        if not tverdict.get("allowed") \
                and not (others is not None and tverdict.get("reason") != "zone_protection"):
            # 最近的作物可能落在**别人**的保护区里 → 那就不碰（不换地方偷干）。
            return self._farm_refuse(plot_label, tverdict, "no_grant")
        # 别人家的田：这次允不允许动手、算不算捣乱 —— 全查表，**用不用由 LLM 决定**。
        others_text = ""
        if others is not None:
            ov = farm_others_verdict(others["mode"], action,
                                     mischievous=bool(payload.get("mischief")))
            self._farm_others_audit_row(others["land"], owner, action, others["mode"], ov)
            if not ov.get("allowed"):
                self._farm_note = farm_others_note(owner, action, ov)
                logger.info("[Planner] farm 别人家田被拒：%s（%s）", ov.get("reason"), plot_label)
                return False
            others_text = farm_others_note(owner, action, ov)
        seed = str(chosen.get("seed") or "")
        missing = missing_items(action, seed, counts)
        if missing:
            self._farm_note = farm_note(action, plot_label, target,
                                        reason="missing_hoe" if action == ACTION_TILL
                                        else "missing_seeds", missing=missing)
            logger.info("[Planner] farm 缺料：%s", missing)
            return False
        ok = self._farm_act(action, target, seed, plot_label, ctx)
        if ok and others_text:
            self._farm_note = f"{others_text}{self._farm_note or ''}"
        return ok

    def _farm_land(self, store: Any, spot: Any) -> Optional[dict]:
        """这块地上是谁家的田/畜栏（区域优先于领地；认不出返回 None）。"""
        if store is None or spot is None:
            return None
        for zone in store.zones():
            if zone_contains(zone, spot[0], spot[1], spot[2]):
                return {"kind": "zone", "row": zone}
        territory = territory_containing(store.territories(), spot[0], spot[1], spot[2])
        if territory is not None:
            return {"kind": "territory", "row": territory}
        return None

    def _farm_others_mode(self, land: Optional[dict]) -> tuple[str, dict]:
        """别人家这一档（查权限表，**默认 none**）＋归属；自家 → `("own", owner)`。"""
        row = (land or {}).get("row")
        owner = farm_land_owner(row) if row is not None else {"mine": False,
                                                              "tier": "friend", "name": ""}
        if owner.get("mine"):
            return "own", owner
        store = self.permissions
        mode = store.farm_others(str(owner.get("tier") or "friend")) if store is not None else "none"
        return mode, owner

    def _farm_others_audit_row(self, land: Any, owner: dict, action: str, mode: str,
                               verdict: dict) -> None:
        """动别人家的田（含被拒）都留审计 —— "这谁让他动的"必须能查（**不带坐标**）。"""
        self._farm_others_audit.append({
            "ts": time.time(),
            "plot": str(((land or {}).get("row") or {}).get("name") or ""),
            "holder": str((land or {}).get("kind") or ""),
            "owner": str(owner.get("name") or owner.get("tier") or ""),
            "mode": str(mode or "none"),
            "action": str(action or ""),
            "mischief": bool(verdict.get("mischief")),
            "allowed": bool(verdict.get("allowed")),
            "reason": str(verdict.get("reason") or ""),
        })
        del self._farm_others_audit[:-50]

    def farm_others_audit(self, n: int = 10) -> list[dict]:
        """最近 n 条"别人家的田"台账（每次动手/被拒都记账）。"""
        rows = self._farm_others_audit[-int(n):] if n and int(n) > 0 else list(self._farm_others_audit)
        return [dict(row) for row in rows]

    def _farm_refuse(self, plot: str, verdict: dict, reason: str) -> bool:
        if verdict.get("reason") == "zone_protection":
            blocked = {"zone": verdict.get("zone"), "rule": verdict.get("rule")}
            self._farm_note = farm_note("", plot, None, blocked=blocked)
            logger.info("[Planner] farm 被区域硬拦：%s(%s) —— 不绕过",
                        verdict.get("zone"), verdict.get("rule"))
        else:
            self._farm_note = farm_note("", plot, None, reason=reason)
        return False

    def _farm_act(self, action: str, target: Any, seed: str, plot: str, ctx: dict) -> bool:
        """真正动手：收割走 `mine_block`，锄地/播种走**通用右键**（`use_item`）。"""
        x, y, z = float(target["x"]), float(target["y"]), float(target["z"])
        inventory = ctx.get("inventory")
        if action in (ACTION_HARVEST, ACTION_REPLANT):
            if action == ACTION_REPLANT:
                # 补种要**同一作物**：种子现在就缺的话，宁可先不收（收了就补不上）。
                missing = missing_items(ACTION_REPLANT, seed, counts_of(inventory))
                if missing:
                    self._farm_note = farm_note(action, plot, target, reason="missing_seeds",
                                                missing=missing)
                    return False
            self.skills.look_at(x, y, z)
            self.skills.mine_block()
            if action == ACTION_REPLANT:
                self._farm_pending = {"action": ACTION_PLANT, "seed": seed, "crop": crop_of(
                    target.get("block_id")), "plot": plot, "tries": 0}
            self._farm_note = farm_note(action, plot, target,
                                        seed=seed if action == ACTION_REPLANT else "")
            logger.info("[Planner] farm %s @(%.0f,%.0f,%.0f)", action, x, y, z)
            return True
        item = self._farm_hold_item(inventory, action, seed)
        if not item:
            self._farm_note = farm_note(action, plot, target,
                                        reason="missing_hoe" if action == ACTION_TILL
                                        else "missing_seeds",
                                        missing=missing_items(action, seed, counts_of(inventory)))
            return False
        index = hold_index(inventory, item)
        if index is None:
            self._farm_note = (f"（{item} 不在快捷栏（热栏 0-8）里，我拿不到手上 —— "
                               f"先把 {item} 放到快捷栏）")
            return False
        self.skills.select_hotbar(int(index))
        self.skills.look_at(x, y, z)
        self.skills.use_item()
        self._farm_note = farm_note(action, plot, target, seed=seed)
        logger.info("[Planner] farm %s 手持 %s @(%.0f,%.0f,%.0f)", action, item, x, y, z)
        return True

    @staticmethod
    def _farm_hold_item(inventory: Any, action: str, seed: str) -> str:
        """这个动作该手持什么：锄地＝锄头（背包里第一把），播种/补种＝种子。"""
        if action == ACTION_TILL:
            for entry in inventory or []:
                if isinstance(entry, dict):
                    item = normalize_item(entry.get("name") or entry.get("item_id")
                                          or entry.get("item"))
                    if is_hoe(item):
                        return item
            return ""
        return normalize_item(seed)

    def _claim_pending_replant(self, context: dict) -> None:
        """上一轮收完了、这一轮看到耕地了 → 补种**同一作物**（看到才认，绝不凭空种）。"""
        pending = self._farm_pending
        if not pending:
            return
        ctx = context if isinstance(context, dict) else {}
        store = self._territory_store()
        territories = store.territories() if store is not None else []
        zones = store.zones() if store is not None else []
        counts = counts_of(ctx.get("inventory"))
        seed = str(pending.get("seed") or "")
        if not seed or counts.get(seed, 0) <= 0:
            self._farm_note = farm_note("plant", pending.get("plot") or "", None,
                                        reason="missing_seeds",
                                        missing=[f"种子（{seed or '同一作物的种子'}）"])
            self._farm_pending = None
            return
        block_view = perception.blocks_rows(ctx)
        farmland = farm_nearest(block_view["rows"],
                                lambda row: str(row.get("block_id") or "").endswith("farmland")
                                or str(row.get("block_id") or "") == "minecraft:farmland")
        if farmland is None:
            # 摘要形状里方块没有逐条坐标 ⇒ 这里**看不出来**（≠ 附近没有）：说清、指向按需扫描，
            # 并且**不**推进 tries（那不是"补种失败"，是"这一帧看不到"）。
            if block_view["partial"]:
                self._farm_note = farm_note("plant", pending.get("plot") or "", None,
                                            reason="no_farmland", partial=True)
                self._farm_pending = None
                return
            pending["tries"] = int(pending.get("tries") or 0) + 1
            if pending["tries"] >= 3:
                self._farm_note = farm_note("plant", pending.get("plot") or "", None,
                                            reason="no_farmland")
                self._farm_pending = None
            return
        verdict = may_act(territories, zones, farmland["x"], farmland["y"], farmland["z"],
                          ACTION_FARM)
        if not verdict.get("allowed"):
            self._farm_pending = None
            self._farm_refuse(str(pending.get("plot") or ""), verdict, "no_grant")
            return
        self._farm_pending = None
        self._farm_act(ACTION_PLANT, farmland, seed, str(pending.get("plot") or ""), ctx)

    # ── 复合链 farm_cycle（§5 扩展）：收割 → 补种 → 取料 → 合成 → 喂 ────

    def _dispatch_farm_cycle(self, payload: dict, context: dict) -> bool:
        """`farm_cycle {plot?,feed?,craft?,animal?}`：**一连套**的农场活。

        用户口径：「种田和喂养动物是可以**一连套**的东西……收割了再种下去，再把收割出来的
        东西直接去喂动物，也可能要**合成**再喂」。顺序：**收割 → 补种 → （田边取料）→
        可选就地合成（走 `craft` 工具）→ 喂动物**（喂食走**通用右键**）。
        每一步的产物/缺口都**如实入账**；中间缺料就**如实报缺什么**，绝不硬凑 / 不假装。
        """
        ctx = context if isinstance(context, dict) else {}
        store = self._territory_store()
        territories = store.territories() if store is not None else []
        zones = store.zones() if store is not None else []
        steps: list[dict] = []
        plot = str(payload.get("plot") or "").strip()
        want_feed = bool(payload.get("feed"))
        want_craft = bool(payload.get("craft"))
        animal_filter = farm_animal_kind({"type": payload.get("animal")}) \
            if payload.get("animal") else ""

        # ① 收割 + ② 补种：走同一条 farm 路（收割成功顺带把"同一作物补种"挂上待办）。
        harvested = self._dispatch_farm({"plot": plot, "action": ACTION_REPLANT}, ctx)
        steps.append(farm_cycle_step(
            "harvest", harvested, reason="" if harvested else "no_harvest",
            detail="" if harvested else (self._farm_note or "附近没有可收的作物")))
        pending = self._farm_pending or {}
        steps.append(farm_cycle_step(
            "replant", bool(pending), reason="" if pending else "replant_not_scheduled",
            detail=(f"已挂上补种同一作物 {pending.get('seed')}" if pending
                    else (self._farm_note or "没种子就先不收/没补上"))))

        fed: dict[str, int] = {}
        missing_food: list[str] = []
        if want_feed:
            inventory = ctx.get("inventory")
            counts = counts_of(inventory)
            entity_view = perception.entities_rows(ctx)
            targets = farm_feed_targets(entity_view["rows"], zones, territories)
            if animal_filter:
                targets["feedable"] = [row for row in targets["feedable"]
                                       if row.get("kind") == animal_filter]
            # ③ 取料：先从背包看有没有能喂的；没有就看看能不能就地合成（田边的箱子取料
            #    要翻箱授权，缺授权就不动 —— 如实报缺口，别硬凑）。
            need = [row for row in targets["feedable"] if not farm_feed_food(counts, row)]
            if need and want_craft:
                food_guess = self._feed_craft_target(need)
                if food_guess:
                    crafted = self._dispatch_craft({"item": food_guess, "count": 1}, ctx)
                    steps.append(farm_cycle_step(
                        "craft", crafted, reason="" if crafted else "craft_failed",
                        detail=f"合成 {food_guess}" if crafted else (self._craft_note or "")))
                else:
                    steps.append(farm_cycle_step("craft", False, reason="no_recipe",
                                                 detail="不知道这些动物要的食物怎么合"))
            elif need:
                steps.append(farm_cycle_step("craft", False, reason="craft_not_requested",
                                             detail="没要合成，就不自己造"))
            counts = counts_of(ctx.get("inventory"))
            # ④ 喂动物：只喂**授权区域**内的；走通用右键（select_hotbar + look_at + use_item）。
            for row in targets["feedable"]:
                food = farm_feed_food(counts, row)
                if not food:
                    missing_food.append(str(row.get("kind") or "?"))
                    continue
                if self._farm_feed_one(row, food, ctx.get("inventory")):
                    kind = str(row.get("kind") or "?")
                    fed[kind] = fed.get(kind, 0) + 1
                    counts[food] = max(0, int(counts.get(food, 0)) - 1)
            feed_detail = farm_feed_note(targets, fed=fed, missing=missing_food,
                                         partial=entity_view["partial"])
            if not fed and entity_view["partial"] and not targets["feedable"]:
                # 摘要里被动生物**没有坐标** ⇒ 这一轮挑不出要喂的动物。**自己按需扫一趟**
                # （只读、有界：一次一条命令），回执下一轮把坐标/实体号摆进 L2 给模型 —— 这样
                # "摘要里看不到"有正规出口，而不是变成"附近没有动物"的假结论。
                scan_kind = animal_filter or "passive"
                scanned, _ = self._dispatch_scan(16, scan_kind, SCAN_DEFAULT_LIMIT)
                if scanned:
                    feed_detail += (f"（摘要里没有动物坐标：已按需扫描（scan_entities "
                                    f"kind={scan_kind}）—— 回执（坐标/实体号）下一轮见）")
            steps.append(farm_cycle_step(
                "feed", bool(fed), reason="" if fed else "no_feed",
                detail=feed_detail, items=fed))

        self._cycle_steps = steps
        self._farm_note = farm_cycle_note(steps)
        logger.info("[Planner] farm_cycle：%s", [row.get("step") for row in steps
                                                if row.get("ok")])
        return any(row.get("ok") for row in steps)

    @staticmethod
    def _feed_craft_target(need: list) -> str:
        """这些动物要的、能就地合成的食物（取第一个对得上的；认不出返回空）。"""
        kinds = {str(row.get("kind") or "") for row in need}
        if kinds & {"cow", "mooshroom", "sheep", "goat"}:
            return "bread"
        if kinds & {"chicken", "parrot"}:
            return "wheat_seeds"
        if kinds & {"pig", "rabbit"}:
            return "carrot"
        return ""

    def _farm_feed_one(self, entity: Any, food: str, inventory: Any) -> bool:
        """喂一只动物：把食物切到手上 + **通用右键**（不看特殊交互分支）。"""
        index = hold_index(inventory, food)
        if index is None:
            return False
        self.skills.select_hotbar(int(index))
        entity_id = entity.get("id", entity.get("entity_id"))
        if isinstance(entity_id, int) and not isinstance(entity_id, bool):
            self.skills.look_at_entity(int(entity_id))
        else:
            self.skills.look_at(float(entity.get("x")), float(entity.get("y")),
                                float(entity.get("z")))
        self.skills.use_item()
        logger.info("[Planner] farm_cycle 喂 %s 用 %s", entity.get("kind"), food)
        return True

    def cycle_status(self) -> dict:
        """最近一次复合链的**逐步**账（每一格成了什么、缺什么）。"""
        return {"steps": [dict(row) for row in self._cycle_steps], "last": None}

    @staticmethod
    def _container_pos_text(row: dict) -> str:
        return (f"@({float(row.get('x') or 0):.0f},{float(row.get('y') or 0):.0f},"
                f"{float(row.get('z') or 0):.0f})")

    def _render_territory(self, context: dict) -> str:
        """L2 的领地/基地/容器/区域/回家段（**只进 L2**，绝不进 L1 静态前缀）。

        用户口径：「范围决定在哪自由动手，容器策略决定哪个箱子能动」——两道一起给 LLM 看：
        区域保护是**事前**提醒（Java 硬拦是兜底）；没进任何基地就明说"野外，按最低权限"。
        """
        ctx = context if isinstance(context, dict) else {}
        lines: list[str] = []
        snapshot = ctx.get("territory") if isinstance(ctx.get("territory"), dict) else {}
        bases = snapshot.get("bases") if isinstance(snapshot.get("bases"), list) else []
        base_line = ""
        current = ctx.get("current_base") if isinstance(ctx.get("current_base"), dict) else None
        if current is not None:
            base_line = f"- 当前在基地「{current.get('name')}」（{current.get('kind')}）里"
        elif bases:
            base_line = "- 当前**不在任何基地里**（野外：一切按最低权限，别顺手拿别人的东西）"
        if base_line:
            lines.append(base_line)
        shown_points: set[tuple[int, int, int]] = set()
        for base in bases:
            anchor = point_of(base.get("anchor")) or (0.0, 0.0, 0.0)
            bed = point_of(base.get("home_bed"))
            shown_points.add((int(round(anchor[0])), int(round(anchor[1])), int(round(anchor[2]))))
            if bed is not None:
                shown_points.add((int(round(bed[0])), int(round(bed[1])), int(round(bed[2]))))
            bed_text = f"床位({bed[0]:.0f},{bed[1]:.0f},{bed[2]:.0f})" if bed else "还没找床"
            lines.append(f"- 基地「{base.get('name')}」({base.get('kind')}) "
                         f"@({anchor[0]:.0f},{anchor[1]:.0f},{anchor[2]:.0f}) "
                         f"半径{float(base.get('radius') or 0):.0f}，{bed_text}")
        for row in snapshot.get("territories") or []:
            if not isinstance(row, dict):
                continue
            grants = "/".join(normalize_grants(row.get("grants"))) or "无"
            lines.append(f"- 领地「{row.get('name')}」授权：{grants}（范围外要用就走借用工具）")
        for row in snapshot.get("containers") or []:
            if not isinstance(row, dict):
                continue
            lines.append(f"- 已知箱子「{row.get('name')}」{self._container_pos_text(row)}"
                         f" 策略：{row.get('policy')}")
        for row in snapshot.get("zones") or []:
            if not isinstance(row, dict):
                continue
            protect = "/".join(str(item) for item in (row.get("protect") or [])) or "无保护项"
            lines.append(f"- 保护区「{row.get('name')}」({row.get('kind')})："
                         f"{protect} —— 这里这些动作会被硬拦，**别去试**")
        situation = ctx.get("home_status") if isinstance(ctx.get("home_status"), dict) else None
        if situation and situation.get("base"):
            distance = situation.get("distance")
            if isinstance(distance, (int, float)):
                if situation.get("away"):
                    seconds = situation.get("away_seconds")
                    how_long = (f"，已经离开 {float(seconds) / 60:.0f} 分钟"
                                if isinstance(seconds, (int, float)) else "")
                    tail = ("（离家太远太久了 —— **只上报**，要不要回去你自己判断）"
                            if situation.get("over") else "")
                    lines.append(f"- 离基地「{situation['base']}」{float(distance):.0f} 格{how_long}{tail}")
                else:
                    lines.append(f"- 在基地「{situation['base']}」范围内")
        if situation:
            # 家的**记忆事实**（模组主动记的 `home`/`bed` 标签）：床没了是事实，不是猜。
            # 坐标只报一次：上面基地行已经打过的点就不再重复（别让坐标刷屏）。
            home_point = point_of(situation.get("home_point"))
            home_bed = str(situation.get("home_bed") or "")
            if home_bed in {"present", "missing"}:
                where = "（坐标未知）"
                if home_point is not None:
                    key = (int(round(home_point[0])), int(round(home_point[1])),
                           int(round(home_point[2])))
                    where = (f"({home_point[0]:.0f},{home_point[1]:.0f},{home_point[2]:.0f})"
                             if key not in shown_points else "（坐标见上面的基地行）")
                    shown_points.add(key)
                if home_bed == "missing":
                    lines.append(
                        f"- 家的床没了（模组按原版重生点 + 方块核对过，不是猜的）：家还在{where}"
                        "，可以先 go_home 走过去，到了自己找一张床，或 make_bed 做一张")
                else:
                    lines.append(f"- 家完好（模组刚核对过）：床还在{where}")
        blocks = ctx.get("zone_blocks") if isinstance(ctx.get("zone_blocks"), list) else []
        for row in blocks[-3:]:
            if not isinstance(row, dict):
                continue
            lines.append(f"- 刚才被区域拦下：区域「{row.get('zone')}」的「{row.get('rule')}」规则"
                         f"（动作 {row.get('action')}）—— 这里不许做这个")
        store = self.permissions
        if store is not None:
            levels = "、".join(f"{tier}={store.farm_others(tier)}"
                              for tier in ("master", "friend", "guest"))
            lines.append("- 别人家的田这一档（权限表）：" + levels
                         + "（none＝不碰；help＝只帮忙种；mischief＝人设允许时可无伤大雅地"
                           "恶作剧，绝不破坏/伤生物）")
        if not lines:
            return ""
        return "领地 / 基地 / 容器 / 区域（你自己记下的；坐标只在同一世界同一维度有效）：\n" + \
            "\n".join(lines)

    def _dispatch_equip_armor(self, context: dict) -> bool:
        """穿甲：自己背包里有盔甲就直接穿；没有就去**已知容器**里找（"点容器找盔甲"）。

        翻箱那一步要 `containers` 授权 —— 没授权就如实说，绝不偷偷开箱。
        """
        inventory = context.get("inventory") if isinstance(context, dict) else None
        has_armor = any(is_armor(entry.get("name") or entry.get("item_id"))
                        for entry in (inventory or []) if isinstance(entry, dict))
        if has_armor:
            self.skills.equip_armor()
            return True
        picks = armor_in_containers(context.get("known_containers") if isinstance(context, dict) else None)
        if not picks:
            self._craft_note = "（我背包里没有盔甲，已知的容器里也没翻到）"
            return False
        policy = self._craft_policy(context)
        if not policy.may_open_containers:
            self._craft_note = "（背包里没有盔甲；没被授权翻箱子，我不去翻）"
            return False
        pick = picks[0]
        pieces = ", ".join(f"{name} x{amount}" for name, amount in pick.take.items())
        self._craft_note = f"（背包里没有盔甲；我记得 {self._pick_label(pick)} 里有 {pieces}，我过去拿）"
        self._dispatch_container_trip(pick, context)
        return True

    @staticmethod
    def _item_at_slot(context: dict, slot: int) -> str:
        """打开的容器里某格是什么（尽力而为：state 没报内容就返回空串，绝不猜物品）。"""
        ctx = context if isinstance(context, dict) else {}
        entries = ctx.get("container_items")
        if entries is None:
            container = ctx.get("open_container")
            container = container if isinstance(container, dict) else {}
            entries = container.get("items") or container.get("contents")
        if isinstance(entries, dict):
            entry = entries.get(str(slot)) or entries.get(slot)
            return str((entry or {}).get("item") or (entry or {}).get("item_id")
                       or (entry or {}).get("name") or "") if isinstance(entry, dict) else ""
        for entry in (entries or []):
            if not isinstance(entry, dict):
                continue
            if _as_optional_int(entry.get("slot")) == slot:
                return str(entry.get("item") or entry.get("item_id") or entry.get("name") or "")
        return ""

    def _record_take(self, item: str, count: int, from_slot: int, to_slot: Optional[int]) -> None:
        """取用审计：记下拿了什么、多少、从哪到哪（"用多少取多少"要能事后查）。"""
        self._take_audit.append({
            "ts": time.time(),
            "item": item,
            "count": int(count or 0),
            "from_slot": int(from_slot),
            "to_slot": int(to_slot) if to_slot is not None else None,
            "remaining_need": dict(self._craft_need),
        })
        del self._take_audit[:-50]

    def take_audit(self, n: int = 10) -> list[dict]:
        """最近 n 条取用审计（时间从早到晚）。"""
        rows = self._take_audit[-int(n):] if n and int(n) > 0 else list(self._take_audit)
        return [dict(row) for row in rows]

    def _dispatch_guard(self, payload: dict, context: dict) -> bool:
        """驻守：坐标缺省用**最近发话者发言时站的位置**（用户口径），否则不下发。

        编坐标＝让它守一个不存在的地方，所以宁可什么都不做（返回 False，日志直说）。
        """
        speaker = context.get("last_speaker") if isinstance(context, dict) else None
        speaker = speaker if isinstance(speaker, dict) else {}
        x = _as_optional_float(payload.get("x"))
        y = _as_optional_float(payload.get("y"))
        z = _as_optional_float(payload.get("z"))
        if x is None:
            x = _as_optional_float(speaker.get("x"))
        if y is None:
            y = _as_optional_float(speaker.get("y"))
        if z is None:
            z = _as_optional_float(speaker.get("z"))
        if x is None or z is None:
            logger.warning("[Planner] guard 没有可用坐标（也没人刚说过话），不下发")
            return False
        radius = int(payload.get("radius") or GUARD_DEFAULT_RADIUS)
        self.skills.guard(x, y, z, radius)
        logger.info("[Planner] 驻守 (%.0f, %.0f, %.0f) r=%d", x, y if y is not None else 0.0, z, radius)
        return True

    def locate_structure(self, name: str, kind: str = "structure",
                         timeout: Optional[float] = None) -> dict:
        """用原版 `/locate` 查结构/群系坐标：发命令 → 等游戏回话 → 解析坐标。

        返回 {"ok", "reason", "command", "query", "text", "x"/"y"/"z"}。
        **查不到/超时一律 ok=False**，如实把原因交给 `_with_locate_note()` 说出去 ——
        编一个坐标比说"没查到"坏得多（它会照着一个假坐标走丢）。

        `kind` 为 "biome" 时走 `/locate biome`（结构/群系是同一个能力的两种查询）。
        """
        query = normalize_locate_name(name, kind)
        which = "biome" if str(kind).strip().lower() == "biome" else "structure"
        result: dict = {"ok": False, "reason": "empty_name", "command": "", "query": query, "text": ""}
        if not query:
            self._last_locate_result = result
            return result

        command = f"locate {which} {query}"
        result["command"] = command
        inbox = self.chat_inbox
        # 游标要在发命令**之前**取：只认这条命令之后到达的回复。
        cursor = inbox.cursor if inbox is not None else 0
        self.skills.run_command(command)
        if inbox is None:
            result["reason"] = "no_chat_inbox"
            self._last_locate_result = result
            logger.warning("[Planner] locate 没有聊天通道，等不到回复: /%s", command)
            return result

        wait = LOCATE_TIMEOUT_SECONDS if timeout is None else float(timeout)
        entry = inbox.wait_after(cursor, looks_like_locate_reply, wait)
        if entry is None:
            result["reason"] = "timeout"
            self._last_locate_result = result
            logger.warning("[Planner] locate 等回复超时(%.1fs): /%s", wait, command)
            return result

        text = str(entry.get("message") or "")
        result["text"] = text
        coords = parse_locate_reply(text)
        if coords is None:
            # 谓词只认"能抠出坐标"或"游戏的失败话术"，所以到这儿基本只剩后者。
            result["reason"] = "not_found"
            self._last_locate_result = result
            logger.info("[Planner] locate 没给出坐标(not_found): %s", text[:80])
            return result

        result.update({
            "ok": True,
            "reason": "ok",
            "x": coords[0],
            "y": coords[1],
            "z": coords[2],
        })
        self._last_locate_result = result
        logger.info("[Planner] locate %s → (%.0f, %.0f, %.0f)",
                    query, coords[0], coords[1], coords[2])
        return result

    def _normalize_item_name(self, item_name: str) -> str:
        stripped = item_name.strip().replace(" ", "")
        return ITEM_ALIASES.get(stripped, item_name.strip())

    def _normalize_block_name(self, block_name: str) -> str:
        stripped = block_name.strip().replace(" ", "")
        return BLOCK_ALIASES.get(stripped, block_name.strip())

    def _is_duplicate_task(self, context: dict, kinds: set[str], target: str) -> bool:
        task_state = context.get("task_state", {}) if isinstance(context, dict) else {}
        if not isinstance(task_state, dict):
            return False
        kind = str(task_state.get("kind", "")).strip().lower()
        status = str(task_state.get("status", "")).strip().lower()
        active_target = self._normalize_task_target(str(task_state.get("target", "")))
        requested_target = self._normalize_task_target(target)
        return kind in kinds and status not in {"", "idle", "done", "failed", "cancelled"} and active_target == requested_target

    def _normalize_task_target(self, target: str) -> str:
        return target.strip().replace(" ", "").lower()
    
    def interrupt(self):
        """中断当前规划（顺带把**挂起**的复合体一并中止，不留僵尸）。"""
        self._is_planning = False
        self._abort_parked_composites("规划被中断")
        logger.info("[Planner] 规划被中断")
    
    def get_status(self) -> dict:
        return {
            "is_planning": self._is_planning,
            "last_plan_time": self._last_plan_time,
            "last_decision": self._last_decision,
            # 最近一次 /locate 的结果（查不到也如实记下来，便于排查"它怎么走错方向了"）。
            "last_locate": self._last_locate_result,
            # 还没补上的材料缺口 + 容器取用审计（"用多少取多少"要能事后查）。
            "craft_need": dict(self._craft_need),
            "take_audit": self.take_audit(10),
            # 搬运：当前进度 + 最近一次回执 + 台账（"搬没搬完、卡在哪一步"要能事后查）。
            "haul": self.haul_status(),
            "haul_last": self.haul_last() or {},
            "haul_audit": self.haul_audit(5),
            # 农田：还没补上的那一次补种（有就说明收割完了在等耕地）。
            "farm_pending": dict(self._farm_pending) if self._farm_pending else {},
            # 复合链：每一步的产物/缺口（收割/补种/取料/合成/喂）。
            "farm_cycle": self.cycle_status(),
            # 别人家的田：每次动手（含被拒）的审计 —— "这谁让他动的"查这里。
            "farm_others_audit": self.farm_others_audit(5),
        }
