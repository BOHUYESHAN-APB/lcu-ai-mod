"""复合体（composite tools）——**声明式**的能力复合（`docs/dev/AGENT_ARCH.md` §10.2 #4）。

用户口径（本批）：

- **能力=工具** ✓：一个复合体 = `{name, description, args, steps[]}`，**每一步只声明**
  「调哪个**已注册**的工具 + 参数从哪来」；
- **不内联新逻辑、不发 wire、不绕门** ✗：复合体内部**只**调用注册表里已登记的工具，
  每一步都**照走它自己的门**（tier / 白名单 / 区域保护 / 容器策略 / 借用台账 / 资源租约
  —— 一步都不许跳 ✗）。本模块**不认识** wire，也不认识技能函数：唯一的执行口是
  调用方传进来的 `dispatch(tool, payload, context)`；
- **失败指到步** ✓：任一步失败 ⇒ 如实说出**第几步、哪一步、它自己的 code**，并如实说明
  **前面哪几步已经做完**（沿用 `haul` / `farm_cycle` 的既有口径：同一种逐步账
  `{step, ok, reason, detail}`，`reason` 就是那一步自己的 code，`detail` 原样转发门/工具的说法）；
- **长任务 ⇒ job 句柄** ✓：`CompositeRun` 自带 job 的四样门槛（进度=按步 ✓ / 终止条件=
  步走完或某步失败 ✓ / 失败 code=那一步的 code ✓ / 取消路径=停当前步 + 不再往下 ✓）；
- **只做声明，不搞后台自主** ✓：跑不跑由模型调用决定，复合体自己不发起任何东西。

本模块是**复合体定义的唯一事实来源**；`agent/tool_registry.py` 从 `COMPOSITES` 生成 L3
工具表（工具清单的唯一事实来源仍是注册表 ✓）。将来「用户自己定义复合体」只需往
`COMPOSITES` 里加一条声明（UI 是将来件；注册表仍是唯一事实来源 ✓）。

**如实记下的边界**（不假装）：
- 复合体是**一次调用里把声明的几步依次下发**（与模型自己连发几个 tool call 同义）；
  每一步的「完成」就是**该步被成功下发**（各工具自己的长任务语义不变）；
- 跨轮推进的步（如 `haul`）**保留它自己的逐步账/回执**，复合体不重复记账、也不假装它已搬完；
- 复合体**不重试**、**不换路绕过**：某一步被门拦下就停在那里如实回报（§10.4 验收③）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Optional

from .craft import inventory_counts, normalize_item
from .farm import cycle_step, hold_index  # 逐步账 / 热栏槽位解析：沿用既有口径，不另造
from .territory import bed_attempt, bed_note  # 造床判定：既有纯函数，不重写一份

logger = logging.getLogger("composites")

# 一次运行的终态：与 `job_registry.JOB_*` **同值**（那份台账认的就是这三个词）。
# **刻意不 import job_registry** ✗：`tool_registry` 在 import 期必须保持零依赖，否则
# `resource_lease` 会在注册表加载到一半时去读 `TOOL_SPECS` 而退化成空表（真回归）。
# 两边同值由测试钉死（`tests/test_composites.py`）。
STATUS_DONE = "done"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"


# ── 状态与 code（`reason` 字段的值；沿用 farm_cycle 的"reason 就是 code"口径）──

WHEN_FAIL = "fail"          # 前置条件不成立 ⇒ 停在这一步（默认）
WHEN_SKIP = "skip"          # 前置条件不成立 ⇒ 跳过这一步，继续往下
WHEN_KINDS: frozenset[str] = frozenset({WHEN_FAIL, WHEN_SKIP})

# job 登记时的**兜底**失败 code：真失败时会被"那一步自己的 code"覆盖（诚实优先）。
DEFAULT_FAILURE_CODE = "composite_failed"

CODE_UNREGISTERED = "unregistered_tool"       # 这一步要调的工具不在注册表里
CODE_MISSING_ARG = "missing_arg"             # 声明的绑定取不到值（缺参数/认不出的来源）
CODE_NO_HOTBAR_ITEM = "no_such_item_in_hotbar"
CODE_CONDITION = "condition_not_met"         # 前置条件不成立（且这一步声明为"停了"）
CODE_SKIPPED = "skipped"                     # 前置条件不成立（这一步声明为"跳过"）
CODE_PERMISSION = "permission_denied"        # 被 tier 门拦下（原因原文在 detail）
CODE_CONTAINER = "container_denied"          # 被容器策略门拦下（原因原文在 detail）
CODE_RESOURCE = "resource_busy"              # 资源被占，本次排队（没下发）
CODE_BACKOFF = "backoff"                     # 刚判定卡死，处于退避窗口
CODE_TOOL = "tool_failed"                    # 该工具自己失败（它的说法在 detail）
CODE_CANCELLED = "step_cancelled"            # 取消：当前步已停，后面的步骤不走

# 派发口回来的"门"名 → 步级 code（planner 只报门名，不在这里判权限）。
GATE_CODES: dict[str, str] = {
    "permission": CODE_PERMISSION,
    "container": CODE_CONTAINER,
    "resource": CODE_RESOURCE,
    "backoff": CODE_BACKOFF,
}


# ── 前置条件（**具名谓词**，不是内联代码）────────────────────────

CHECK_ALWAYS = "always"
CHECK_HAS_ITEM = "has_item"
CHECK_MISSING_ITEM = "missing_item"
# "背包里没有床时，按既有判定（`territory.bed_attempt`）看能不能造"：三种结论
# —— 已有床（跳过合成）/ 材料够（去合成）/ 缺料或周围不安全（**如实报**，停在这一步）。
CHECK_BED_CRAFTABLE = "bed_craftable"
CHECK_KINDS: frozenset[str] = frozenset({CHECK_ALWAYS, CHECK_HAS_ITEM, CHECK_MISSING_ITEM,
                                         CHECK_BED_CRAFTABLE})


@dataclass(frozen=True)
class Check:
    """一步的前置条件：**具名的**谓词 + 参数（写死在下面这张表里，不写代码）。

    `has_item` / `missing_item` 读**当轮状态快照**里我自己的背包（不看别人的箱子 ✗）。
    """

    kind: str = CHECK_ALWAYS
    item: str = ""
    count: int = 1


def has_item(item: str, count: int = 1) -> Check:
    return Check(CHECK_HAS_ITEM, item=item, count=count)


def missing_item(item: str, count: int = 1) -> Check:
    return Check(CHECK_MISSING_ITEM, item=item, count=count)


def bed_craftable() -> Check:
    """「没有床就按配方合成（羊毛+木板），缺料/不安全如实报」——判定复用领地模块的纯函数。"""
    return Check(CHECK_BED_CRAFTABLE)


# ── 参数绑定（**只说"参数从哪来"**）─────────────────────────────

SOURCE_ARG = "arg"
SOURCE_HOTBAR = "hotbar_slot"
SOURCES: frozenset[str] = frozenset({SOURCE_ARG, SOURCE_HOTBAR})


def arg(name: str, default: Any = None) -> dict:
    """从**调用参数**取（复合体入参）；没 `default` 又没给 ⇒ 这一步带 `missing_arg` 停下。"""
    binding = {"from": SOURCE_ARG, "name": str(name)}
    if default is not None:
        binding["default"] = default
    return binding


def slot(item: str) -> dict:
    """从**热栏**取槽位（`select_hotbar` 要 0-8）：按名或物品族（如 `bed` → `*_bed`）找。"""
    return {"from": SOURCE_HOTBAR, "item": str(item)}


# ── 声明式结构 ──────────────────────────────────────────────────

@dataclass(frozen=True)
class CompositeStep:
    """复合体的一步：**只**声明"调哪个已注册工具 + 参数从哪来（+ 可选前置条件）"。"""

    tool: str
    args: Mapping[str, Any] = field(default_factory=dict)
    name: str = ""                                  # 步名（账目/引用用；缺省＝工具名）
    when: Optional[Check] = None                    # 前置条件（None = 总是跑）
    when_missing: str = WHEN_FAIL                    # 条件不成立时：停（fail）还是跳过（skip）
    optional: bool = False                           # 失败了也不终止（只如实入账，继续）
    note: str = ""                                   # 人读说明

    @property
    def label(self) -> str:
        return self.name or self.tool


def step(tool: str, args: Optional[Mapping[str, Any]] = None, *, name: str = "",
         when: Optional[Check] = None, when_missing: str = WHEN_FAIL, optional: bool = False,
         note: str = "") -> CompositeStep:
    """声明一步（复合体里**只**能用这个 —— 不许内联逻辑、不许直接发帧 ✗）。"""
    return CompositeStep(tool=str(tool), args=dict(args or {}), name=str(name or ""),
                         when=when, when_missing=str(when_missing), optional=bool(optional),
                         note=str(note or ""))


@dataclass(frozen=True)
class Composite:
    """一个复合体的声明（名字 / 说明 / 入参名 / 步骤）。"""

    name: str
    description: str
    steps: tuple[CompositeStep, ...]
    args: tuple[str, ...] = ()                       # 声明入参名（顺序即 L3 schema 顺序）


class CompositeRejected(Exception):
    """声明被拒：复合体内部**只准**引用已注册的工具，且绑定/条件必须是认得的写法。"""


def validate_composite(comp: Composite, *, is_registered: Callable[[str], bool]) -> None:
    """校验一条声明（唯一判定处）：步名不重复、工具**必须已注册**、绑定/条件认得。

    由 `agent/tool_registry.py` 在生成工具表后调用（那时才有注册表全集）；
    测试也用它对**临时构造**的声明做同一条校验。
    """
    if not str(comp.name or "").strip():
        raise CompositeRejected("复合体必须有名字")
    if not comp.steps:
        raise CompositeRejected(f"复合体「{comp.name}」至少要有一个步骤")
    seen: set[str] = set()
    for index, item in enumerate(comp.steps, start=1):
        if not str(item.tool or "").strip():
            raise CompositeRejected(f"复合体「{comp.name}」第 {index} 步没有工具名")
        if not is_registered(item.tool):
            raise CompositeRejected(
                f"复合体「{comp.name}」第 {index} 步引用了**未注册**的工具「{item.tool}」"
                f"（复合体只准调已注册的工具）")
        if item.label in seen:
            raise CompositeRejected(f"复合体「{comp.name}」第 {index} 步的步名「{item.label}」重复")
        seen.add(item.label)
        if item.when is not None and item.when.kind not in CHECK_KINDS:
            raise CompositeRejected(f"复合体「{comp.name}」第 {index} 步的前置条件不认得："
                                    f"{item.when.kind}")
        if item.when_missing not in WHEN_KINDS:
            raise CompositeRejected(f"复合体「{comp.name}」第 {index} 步的 when_missing 不认得："
                                    f"{item.when_missing}")
        for key, spec in item.args.items():
            if isinstance(spec, dict) and "from" in spec and str(spec["from"]) not in SOURCES:
                raise CompositeRejected(f"复合体「{comp.name}」第 {index} 步的参数「{key}」"
                                        f"来源不认得：{spec.get('from')}")


# ── 落地的复合体（用户口径点名的原例 + 一个真用得上的）──────────────
#
# ① `make_bed`（§10.2 的原例）：看背包 → 没有床就按配方合成（羊毛+木板；缺料是
#    **craft 这一自己**的事，如实报）→ 把床放到手上 → 找地方放（`place_block`，
#    受区域保护门）→ `sleep_in_bed`。
# ② `deposit_all`：先**只读**看一眼目标箱（看不清也如实报，继续走），再把身上的东西
#    搬进这个**已记住**的箱子（`haul` 自己的容器策略/借用/留底规则一步都不跳）。

MAKE_BED = Composite(
    name="make_bed",
    description="造一张床并睡下（复合体，逐步走各自的权限门）：先看背包 → **没有床才**按配方"
                "合成（羊毛 3 + 木板 3；缺料/周围不安全就如实报、绝不自作聪明）→ 把床切到"
                "手上 → 就地放下（place_block，受区域保护）→ sleep_in_bed。哪一步被拦/失败"
                "就说清是第几步、哪个 code，前面做完的步骤不抹掉",
    args=(),
    steps=(
        step("get_inventory", name="check_backpack", note="看背包有没有床"),
        step("craft_via_recipe", {"item": "white_bed", "count": 1}, name="craft_bed",
             when=bed_craftable(), when_missing=WHEN_FAIL,
             note="没有床才合成（缺料/未授权由这一步自己报）"),
        step("select_hotbar", {"index": slot("bed")}, name="hold_bed",
             note="把床切到热栏里（不在热栏就如实报，不假装拿在手上）"),
        step("place_block", name="place_bed", note="就地放下（受区域硬保护约束）"),
        step("sleep_in_bed", name="sleep", note="躺下睡到天亮"),
    ),
)

DEPOSIT_ALL = Composite(
    name="deposit_all",
    description="把身上的东西放进仓库（复合体）：先用**只读**的 view_container 看清目标箱"
                "（看不成也如实报，不挡后面的搬运）→ 再用 haul 从「self」搬到这个**已记住**的"
                "箱子（容器名先 remember_container）。箱子的策略门（no_touch/borrow_only）、"
                "翻箱授权、单趟上限与源箱留底都由每一步自己把；item/count 可选（省＝能搬的都搬）",
    args=("container", "item", "count"),
    steps=(
        step("view_container", {"target": arg("container")}, name="peek_target", optional=True,
             note="只读看清目标箱（no_touch 不给看、borrow_only 只读可以）"),
        step("haul", {"from": "self", "to": arg("container"), "item": arg("item", default=""),
                      "count": arg("count", default="")}, name="haul_to_container"),
    ),
)

COMPOSITES: tuple[Composite, ...] = (MAKE_BED, DEPOSIT_ALL)
COMPOSITE_BY_NAME: dict[str, Composite] = {comp.name: comp for comp in COMPOSITES}


def is_registered_tool(name: Any) -> bool:
    """这个词条是不是**已注册**的工具（懒取注册表：避免与 `tool_registry` 循环导入）。"""
    from .tool_registry import TOOL_BY_NAME

    return str(name or "").strip() in TOOL_BY_NAME


# ── 物品匹配（"族名"也认：`bed` → `*_bed`）────────────────────────

def item_matches(candidate: Any, want: Any) -> bool:
    """这件物品算不算 `want`：同名，或名字以 `_<want>` 结尾（`bed` 认所有 `*_bed`）。"""
    have, target = normalize_item(candidate), normalize_item(want)
    if not have or not target:
        return False
    return have == target or have.endswith("_" + target)


def family_count(counts: Mapping[str, int], want: Any) -> int:
    """背包里 `want`（含族名）的总数。"""
    return sum(int(num) for item, num in (counts or {}).items() if item_matches(item, want))


def hotbar_slot(inventory: Any, want: Any) -> Optional[int]:
    """热栏（0-8）里能代表 `want` 的那件物品的槽位；没有就 None（如实说，不编）。"""
    exact = hold_index(inventory, want)
    if exact is not None:
        return exact
    for item in inventory_counts(inventory):
        if item_matches(item, want):
            found = hold_index(inventory, item)
            if found is not None:
                return found
    return None


def evaluate(check: Optional[Check], context: Mapping[str, Any]) -> tuple[bool, str, str, str]:
    """前置条件是否成立：`(成立?, code, 如实说明, 不成立时怎么办)`。

    `how` 为空 ⇒ 用这一步声明的 `when_missing`；`bed_craftable` 会自己给出结论
    （已有床 ⇒ `skip`；缺料/不安全 ⇒ `fail`）。**只读快照，不动手** ✗。
    """
    if check is None or check.kind == CHECK_ALWAYS:
        return True, "", "", ""
    if check.kind in (CHECK_HAS_ITEM, CHECK_MISSING_ITEM):
        have = family_count(inventory_counts((context or {}).get("inventory")), check.item)
        want = max(1, int(check.count or 1))
        if check.kind == CHECK_HAS_ITEM:
            return (have >= want, "",
                    "" if have >= want else f"（背包里没有 {check.item}（要 {want} 个））", "")
        absent = have < want
        return (absent, "", "" if absent else f"（背包里已经有 {check.item} 了，不用再合成）", "")
    if check.kind == CHECK_BED_CRAFTABLE:
        ctx = context or {}
        counts = inventory_counts(ctx.get("inventory"))
        if family_count(counts, "bed") > 0:
            return False, CODE_SKIPPED, "（背包里已经有床了，不用再合成）", WHEN_SKIP
        decision = bed_attempt(ctx.get("inventory"), ctx.get("entities"))
        action = str((decision or {}).get("action") or "")
        if action == "craft":
            return True, "", "", ""
        code = "bed_unsafe" if action == "unsafe" else "bed_missing"
        return False, code, bed_note(decision), WHEN_FAIL
    logger.warning("[Composites] 认不出的前置条件：%s", check.kind)
    return False, CODE_CONDITION, f"（认不出的前置条件「{check.kind}」）", WHEN_FAIL


def resolve_value(spec: Any, args: Mapping[str, Any],
                  context: Mapping[str, Any]) -> tuple[Any, str, str]:
    """一个参数值：绑定（`{"from": ...}`）或字面量。返回 `(值, code, detail)`。"""
    if not isinstance(spec, dict) or "from" not in spec:
        return spec, "", ""
    source = str(spec.get("from") or "")
    if source == SOURCE_ARG:
        name = str(spec.get("name") or "")
        if name in (args or {}):
            return args[name], "", ""
        if "default" in spec:
            return spec.get("default"), "", ""
        return None, CODE_MISSING_ARG, f"（复合体缺参数「{name}」：说清它再叫我）"
    if source == SOURCE_HOTBAR:
        item = str(spec.get("item") or "")
        index = hotbar_slot((context or {}).get("inventory"), item)
        if index is None:
            return None, CODE_NO_HOTBAR_ITEM, f"（热栏里没有「{item}」：拿不到手上，就不动手）"
        return index, "", ""
    logger.warning("[Composites] 认不出的参数来源：%s", source)
    return None, CODE_MISSING_ARG, f"（认不出的参数来源「{source}」）"


def resolve_step_args(item: CompositeStep, args: Mapping[str, Any],
                      context: Mapping[str, Any]) -> tuple[Optional[dict], str, str]:
    """把这一步声明的参数解析成**真正的调用参数**（顺序即声明顺序）。"""
    payload: dict = {}
    for key, spec in item.args.items():
        value, code, detail = resolve_value(spec, args, context)
        if code:
            return None, code, detail
        payload[str(key)] = value
    return payload, "", ""


def outcome_of(value: Any) -> tuple[bool, str, str]:
    """派发口的返回值 → `(ok, code, detail)`：`True/False` 或 `{ok, code, detail}`。"""
    if isinstance(value, dict):
        ok = bool(value.get("ok"))
        return ok, ("" if ok else str(value.get("code") or CODE_TOOL)), str(value.get("detail") or "")
    if isinstance(value, bool):
        return value, ("" if value else CODE_TOOL), ""
    logger.warning("[Composites] 派发口返回了认不出的东西：%r", type(value).__name__)
    return False, CODE_TOOL, ""


# ── 一次运行（job 句柄的四样门槛都在这里）─────────────────────────

@dataclass
class CompositeRun:
    """一次复合体运行：逐步账 + 卡在哪一步 + 四样门槛（进度/终止条件/失败 code/取消路径）。"""

    composite: Composite
    args: dict = field(default_factory=dict)
    steps: list = field(default_factory=list)        # 逐步账（`farm.cycle_step` 形状）
    index: int = 0                                   # 已评估的步数（进度按它算，单调 ✓）
    status: str = ""                                 # "" 运行中；done | failed | cancelled
    failed_index: int = 0                            # 卡在第几步（1-based；0 = 没卡）
    failed_step: str = ""
    failure_code: str = ""
    failure_detail: str = ""
    cancelled: bool = False                          # 取消标记（取消路径只写它）
    cancel_reason: str = ""                          # 取消原因（人读）
    _cancel_pending: bool = False                    # 还没把"停当前步"落到当前步上
    note: str = ""

    # ── job 的四样门槛 ──

    def progress(self) -> float:
        """① 进度：按步（已评估步数 / 总步数），**单调不减** ✓。"""
        total = max(1, len(self.composite.steps))
        return min(1.0, self.index / total)

    def terminal(self) -> Optional[dict]:
        """② 终止条件：步走完 ⇒ done；某步失败 ⇒ failed（带**那一步自己的 code**）；取消 ⇒ cancelled。"""
        if not self.status:
            return None
        if self.status == STATUS_FAILED:
            return {"status": STATUS_FAILED, "code": self.failure_code or DEFAULT_FAILURE_CODE,
                    "message": self.failure_detail, "result": self.result()}
        if self.status == STATUS_CANCELLED:
            return {"status": STATUS_CANCELLED, "message": "取消：当前步已停，不再往下走",
                    "result": self.result()}
        return {"status": STATUS_DONE, "result": self.result()}

    def failure(self) -> str:
        """③ 失败 code：**那一步自己的 code**（还没跑完就用兜底 code）。"""
        return self.failure_code or DEFAULT_FAILURE_CODE

    def cancel(self, reason: str = "") -> None:
        """④ 取消路径：只记标记 —— 真正"停当前步"由引擎在安全的点上做（不打断 handler）。"""
        self.cancelled = True
        self._cancel_pending = True
        self.cancel_reason = str(reason or "cancelled")

    # ── 结果 ──

    def take_cancel(self) -> bool:
        """取一次"要不要停当前步"（取过就清）。"""
        pending, self._cancel_pending = self._cancel_pending, False
        return pending

    def result(self) -> dict:
        """如实的结果：逐步账 + 卡在第几步/哪一步/哪个 code + 已做完的几步（跳过另列）。"""
        done = [row["step"] for row in self.steps
                if row.get("ok") and row.get("reason") != CODE_SKIPPED]
        skipped = [row["step"] for row in self.steps if row.get("reason") == CODE_SKIPPED]
        return {
            "composite": self.composite.name,
            "ok": self.status == STATUS_DONE,
            "status": self.status or "running",
            "index": self.index,
            "total": len(self.composite.steps),
            "step_index": self.failed_index,
            "step": self.failed_step,
            "code": self.failure_code,
            "detail": self.failure_detail,
            "done": done,
            "skipped": skipped,
            "steps": [dict(row) for row in self.steps],
            "note": self.note,
        }


def _stop(run: CompositeRun, index: int, label: str, code: str, detail: str,
          status: str) -> None:
    """记下"停在第几步、为什么"（失败/取消共用一处，格式只此一份）。"""
    run.index = max(run.index, index + 1)
    run.failed_index = index + 1
    run.failed_step = label
    run.failure_code = code
    run.failure_detail = detail
    run.status = status


def run_composite(run: CompositeRun, *, dispatch: Callable[[str, dict, dict], Any],
                  context: Optional[dict] = None,
                  is_registered: Optional[Callable[[str], bool]] = None,
                  cancel_current: Optional[Callable[[], None]] = None) -> CompositeRun:
    """按声明**依次**跑完几步（每一步都从**同一个派发口**出去 ⇒ 自己的门一个都不跳）。

    - `dispatch(tool, payload, context)`：**唯一**的执行口（planner 传的是注册表驱动的
      `_dispatch_tool_call`：查注册表 → `_authorize_tool` → 资源租约 → handler）；
      返回 `bool` 或 `{ok, code, detail}`（后者能把"被哪道门拦下 + 原话"如实带回来）；
    - `cancel_current()`：取消时"停当前步"的落点（planner 传的是 `action_manager.stop`）；
    - 失败/被拦**不重试、不换路**：停在那一步，把已完成的前几步如实留在账上。
    """
    ctx = context if isinstance(context, dict) else {}
    registered = is_registered or is_registered_tool

    def _record(_label: str, row: dict) -> None:
        run.steps.append(row)

    def _fire_cancel() -> None:
        """④ 取消路径：停掉当前这一步（只做一次；停不下来也不许假装成功）。"""
        if cancel_current is not None and run.take_cancel():
            try:
                cancel_current()
            except Exception as exc:  # noqa: BLE001 — 停不下来也不许假装成功
                logger.warning("[Composites] 取消当前步失败：%s", exc)

    def _cancelled_here(index: int, label: str) -> None:
        _fire_cancel()
        _record(label, cycle_step(label, False, reason=CODE_CANCELLED,
                                  detail="（复合体被取消：当前步已停，后面的步骤不再走）"))
        _stop(run, index, label, CODE_CANCELLED, "当前步已停，不再往下走", STATUS_CANCELLED)

    for index, item in enumerate(run.composite.steps):
        label = item.label
        if run.cancelled:
            _cancelled_here(index, label)
            break
        # ① 只认注册表里的工具（复合体绝不调未注册的东西）
        if not registered(item.tool):
            row = cycle_step(label, False, reason=CODE_UNREGISTERED,
                             detail=f"（复合体只调已注册的工具：「{item.tool}」不在注册表里）")
            _record(label, row)
            if not item.optional:
                _stop(run, index, label, CODE_UNREGISTERED, row["detail"], STATUS_FAILED)
                break
            run.index = max(run.index, index + 1)
            continue
        # ② 前置条件（具名谓词；只读快照，不动手）
        met, code, detail, how = evaluate(item.when, ctx)
        if not met:
            run.index = max(run.index, index + 1)
            mode = how or item.when_missing
            if mode == WHEN_SKIP:
                _record(label, cycle_step(label, True, reason=CODE_SKIPPED, detail=detail))
                continue
            _record(label, cycle_step(label, False, reason=code or CODE_CONDITION, detail=detail))
            if not item.optional:
                _stop(run, index, label, code or CODE_CONDITION, detail, STATUS_FAILED)
                break
            continue
        # ③ 参数从哪来（声明式绑定）
        payload, code, detail = resolve_step_args(item, run.args, ctx)
        if payload is None:
            _record(label, cycle_step(label, False, reason=code, detail=detail))
            if not item.optional:
                _stop(run, index, label, code, detail, STATUS_FAILED)
                break
            run.index = max(run.index, index + 1)
            continue
        # ④ 走**同一个**派发口（它自己的门：注册表 → 权限 → 区域/容器/借用 → 资源租约）
        ok, code, detail = outcome_of(dispatch(item.tool, payload, ctx))
        _record(label, cycle_step(label, ok, reason="" if ok else code, detail=detail))
        run.index = max(run.index, index + 1)
        if not ok and not item.optional:
            _stop(run, index, label, code, detail, STATUS_FAILED)
            break
        if run.cancelled:
            _fire_cancel()
            _stop(run, index, label, CODE_CANCELLED, "当前步已停，不再往下走", STATUS_CANCELLED)
            break
    else:
        run.status = STATUS_DONE           # 声明里的几步都评估过了

    if run.cancelled and run.status != STATUS_CANCELLED:
        run.status = STATUS_CANCELLED
    run.note = composite_note(run)
    logger.info("[Composites] %s %s（%s）", run.composite.name, run.status,
                [row.get("step") for row in run.steps])
    return run


def abort_run(run: CompositeRun, code: str, detail: str) -> CompositeRun:
    """复合体**自己**出岔子时的如实收尾：记成"卡在下一条要跑的步"上（绝不假装成功）。"""
    total = len(run.composite.steps)
    index = min(run.index, max(0, total - 1))
    run.failed_index = (index + 1) if total else 0
    run.failed_step = run.composite.steps[index].label if total else ""
    run.failure_code = str(code)
    run.failure_detail = str(detail)
    run.status = STATUS_FAILED
    run.note = composite_note(run)
    return run


def composite_note(run: CompositeRun) -> str:
    """一句**如实**的话：第几步卡住、哪一步、它自己的 code、前面哪几步做完了。

    沿用 `farm_cycle` 的说法口径（括号里、缺什么说什么），另外**点名第几步**（§10.4 验收③）。
    """
    name = run.composite.name
    done = [row["step"] for row in run.steps
            if row.get("ok") and row.get("reason") != CODE_SKIPPED]
    skipped = [row["step"] for row in run.steps if row.get("reason") == CODE_SKIPPED]
    # 卡住/取消那一步永远是最后一条账；前面的"没成"（含可选步）也要如实带上，不因为不挡路就瞒着。
    earlier = run.steps[:-1] if run.status in (STATUS_FAILED, STATUS_CANCELLED) else run.steps
    gaps = [row for row in earlier if not row.get("ok")]
    did = f"已经做完 {'、'.join(done)}" if done else "一步都还没做成"
    if skipped:
        did += f"（跳过 {'、'.join(skipped)}）"
    for row in gaps:
        did += f"；{row['step']} 没成（{row['reason']}：{row['detail']}）"
    if run.status == STATUS_CANCELLED:
        where = f"第 {run.failed_index} 步 {run.failed_step}" if run.failed_index else "开跑前"
        return f"（复合体 {name} 被取消（{where}起不再往下）；{did}）"
    if run.status == STATUS_FAILED:
        return (f"（复合体 {name} 卡在第 {run.failed_index} 步 {run.failed_step}"
                f"（{run.failure_code}：{run.failure_detail}）；{did}）")
    if run.status == STATUS_DONE:
        walked = "、".join(done) or "没有要做的步"
        tail_bits: list[str] = []
        if skipped:
            tail_bits.append(f"跳过 {'、'.join(skipped)}")
        for row in gaps:
            tail_bits.append(f"{row['step']} 没成（{row['reason']}：{row['detail']}）")
        tail = ("；" + "；".join(tail_bits)) if tail_bits else ""
        return f"（复合体 {name}：{walked} 都走完了{tail}）"
    return f"（复合体 {name}：跑到第 {run.index} 步，还没结束）"
