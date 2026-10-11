"""控制租约 —— 双脑分工与仲裁的**接口 + 常量**（对应 ``docs/dev/AGENT_ARCH.md`` §8）。

用户口径（硬）：

* **左脑 = VLA / 本地闭环**（毫秒级：移动、瞄准、按住右键、P0 生存反射）—— **持控制租约**，
  独占底层输入；
* **右脑 = LLM**（现在唯一启用的）—— 负责对话/规划/工具调用，**只能下发意图**
  （``go_to`` / ``craft`` / ``guard`` / ``go_afk`` …），**不能直接写控制** ✗；
* **生存反射永远可抢占**（命 > 计划），每次抢占必须带**理由**与**时限**；
* **租约的每一次获取 / 转移 / 抢占 / 释放都留可读审计**（谁、何时、为何、被抢的是什么）。

本模块**只定义接口与常量**，不启用双脑仲裁 ✗、不接 VLA ✗（没有 endpoint/key）、
**不改变任何现有行为** ✗ —— 唯一的接线是：本地 P0 反射抢占时往这里登记一行审计
（见 ``register_reflex_preemption``，由 ``modes_engine.EatYield`` 调用）。

设计要点与"做不到就如实说"的边界：

* 本模块不实现"抢占到期后自动把控制还给上一个持租人"——那属于**启用仲裁**（本批不做 ✗）。
  TTL 到期只是让租约**回到空闲**并留一行 ``expire`` 审计；交接由调用方决定（诚实缺口）。
* 右脑**不是合法持租人**：``acquire`` / ``transfer`` / ``preempt`` 传入右脑身份一律拒绝
  （``LeaseDenied``），这条不做"看起来接了"的假放行。
* 能力位（``CAPABILITY_DEFAULTS``）如实声明当前状态：没有 VLA＝``vla_attached=False``，
  仲裁未启用＝``dual_brain_arbitration=False``，**降级要读能力位、不许假装**。
"""

from __future__ import annotations

import copy
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Optional

logger = logging.getLogger("control_lease")


# ── 脑 / 角色 ────────────────────────────────────────────────
# 左脑：实时控制（毫秒级闭环）——唯一合法持租人一方。
BRAIN_LEFT = "left"
# 右脑：语言与规划（LLM）——只能下发意图，不能持控制租约、不能直写控制。
BRAIN_RIGHT = "right"

# 左脑一方的具体持租人（字符串即身份；未来 VLA 接入时用 HOLDER_LEFT_VLA）。
HOLDER_LEFT_VLA = "left:vla"                 # VLA（未接入）
HOLDER_LEFT_LOCAL = "left:local-loop"        # 本地闭环（Java 侧）
HOLDER_REFLEX_COMBAT = "left:reflex-combat"  # P0 生存反射：战斗
HOLDER_REFLEX_SWIM = "left:reflex-swim"      # P0 生存反射：上浮
HOLDER_REFLEX_EAT = "left:reflex-eat"        # P0 生存反射：进食

# 右脑身份（明确**不在**合法持租人集合里）。
HOLDER_RIGHT_LLM = "right:llm"

# 合法持租人：左脑一方。右脑进来就被拒。
CONTROL_HOLDERS: frozenset[str] = frozenset({
    HOLDER_LEFT_VLA,
    HOLDER_LEFT_LOCAL,
    HOLDER_REFLEX_COMBAT,
    HOLDER_REFLEX_SWIM,
    HOLDER_REFLEX_EAT,
})

# 生存反射（可抢占的控制方）→ 抢占理由码。抢占必须带理由 ✓。
REASON_SURVIVAL_COMBAT = "survival:combat"
REASON_SURVIVAL_SWIM = "survival:swim"
REASON_SURVIVAL_EAT = "survival:eat"

SURVIVAL_REASONS: frozenset[str] = frozenset({
    REASON_SURVIVAL_COMBAT,
    REASON_SURVIVAL_SWIM,
    REASON_SURVIVAL_EAT,
})

# 反射持有者 ↔ 理由码（Java 侧上报的 ``reflex_combat`` / ``reflex_swim`` / ``reflex_eat``）。
REFLEX_HOLDERS: Mapping[str, str] = {
    HOLDER_REFLEX_COMBAT: REASON_SURVIVAL_COMBAT,
    HOLDER_REFLEX_SWIM: REASON_SURVIVAL_SWIM,
    HOLDER_REFLEX_EAT: REASON_SURVIVAL_EAT,
}

# 反射抢占的默认时限（秒）：抢占必须有时限 ✓（到期租约回到空闲并留一行 expire）。
DEFAULT_REFLEX_TTL = 8.0


# ── 意图（右脑唯一的下发通道）────────────────────────────────
# 与"直接控制"相对：意图只表达**要去哪/要做什么**，由控制层（左脑/本地闭环）执行。
INTENT_GO_TO = "go_to"
INTENT_CRAFT = "craft"
INTENT_GUARD = "guard"
INTENT_GO_AFK = "go_afk"

INTENT_KINDS: frozenset[str] = frozenset({
    INTENT_GO_TO,
    INTENT_CRAFT,
    INTENT_GUARD,
    INTENT_GO_AFK,
})


# ── 能力位（供将来**如实降级**）──────────────────────────────
# 默认值＝当前真实状态：没有 VLA、双脑仲裁未启用。改这里才是"启用"，本批不动 ✗。
CAPABILITY_DEFAULTS: dict[str, bool] = {
    "vla_attached": False,           # VLA 是否接入（没有 endpoint/key → False）
    "left_brain_active": False,      # 左脑本地闭环是否接管底层输入
    "dual_brain_arbitration": False,  # 双脑仲裁是否启用（本批不启用）
    "intent_channel": True,          # 右脑**意图通道**是否就绪（接口已在，只认意图）
    "reflex_preemption": True,       # 生存反射可抢占（本地已存在）
    "preempt_audit": True,           # 抢占/转移留审计（本模块保证）
}


# ── 审计事件类别 ─────────────────────────────────────────────
LEASE_GRANT = "grant"
LEASE_TRANSFER = "transfer"
LEASE_PREEMPT = "preempt"
LEASE_RELEASE = "release"
LEASE_EXPIRE = "expire"
LEASE_INTENT = "intent"

EVENT_KINDS: frozenset[str] = frozenset({
    LEASE_GRANT, LEASE_TRANSFER, LEASE_PREEMPT,
    LEASE_RELEASE, LEASE_EXPIRE, LEASE_INTENT,
})

# 审计环形缓冲长度（与 ``permissions.AUDIT_MAX`` / ``tpa`` 同口径：够查"刚才谁抢了"）。
AUDIT_MAX = 50


# ── 错误 ─────────────────────────────────────────────────────
class LeaseError(Exception):
    """租约相关错误的基类。"""


class LeaseConflict(LeaseError):
    """非法转移：当前持租人不是发起方，或租约已被他人持有。"""


class LeaseDenied(LeaseError):
    """右脑越界：LLM 不能持控制租约 / 不能直写底层控制（只能下发意图）。"""


def is_right_brain(holder: Any) -> bool:
    """是否右脑身份（LLM）。右脑永远不是合法持租人。"""
    return isinstance(holder, str) and holder.startswith(f"{BRAIN_RIGHT}:")


def _validate_control_holder(holder: Any) -> str:
    """校验持租人：必须是左脑一方的已知身份；右脑/未知一律拒绝。"""
    if not isinstance(holder, str) or not holder.strip():
        raise LeaseDenied("持租人必须是非空身份串（左脑一方）")
    if is_right_brain(holder):
        raise LeaseDenied(f"右脑（{holder}）不能持控制租约，只能下发意图")
    if holder not in CONTROL_HOLDERS:
        raise LeaseDenied(f"未知持租人 {holder!r}：不在左脑合法持租人集合内")
    return holder


# ── 审计记录 ─────────────────────────────────────────────────
@dataclass(frozen=True)
class LeaseEvent:
    """一条只读的租约审计记录（谁 / 何时 / 为何 / 被抢的是什么）。"""

    seq: int
    kind: str
    at: float
    actor: str
    holder: Optional[str]        # 事件后的持租人（意图事件＝当时的持租人，不变）
    previous: Optional[str]      # 事件前的持租人（＝"被抢的东西"）
    reason: str
    ttl: Optional[float] = None
    expires_at: Optional[float] = None
    detail: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        """可读字典（拷贝；改它不影响租约内部状态）。"""
        return {
            "seq": self.seq,
            "kind": self.kind,
            "at": self.at,
            "actor": self.actor,
            "holder": self.holder,
            "previous": self.previous,
            "reason": self.reason,
            "ttl": self.ttl,
            "expires_at": self.expires_at,
            "detail": copy.deepcopy(dict(self.detail)),
        }


# ── 租约 ─────────────────────────────────────────────────────
class ControlLease:
    """控制租约：底层输入控制权的**唯一**归属。

    语义（§8 第 1、2、4 条）：

    * **控制权是租约**：左脑持租时独占底层输入；右脑只能通过 ``submit_intent`` 提意图。
    * **生存反射永远可抢占**：``preempt`` 带**理由**（``SURVIVAL_REASONS`` 之一或自定义）与
      **时限**（``ttl>0``）；记录里保留被抢的持租人（``previous``）。
    * **每次事件留审计**：``events()`` / ``audit_log()`` 给"它为什么没听我的"复盘。

    时钟与审计 sink 都可注入，方便无 wire 的测试完整驱动（与 ``tpa`` 同一套路）。
    """

    def __init__(self, *, holder: Optional[str] = None, reason: str = "", ttl: Optional[float] = None,
                 audit: Optional[Callable[[dict], None]] = None,
                 capabilities: Optional[Mapping[str, bool]] = None,
                 clock: Callable[[], float] = time.time):
        self._clock = clock
        self._audit_sink = audit
        self._lock = threading.RLock()
        self._events: list[LeaseEvent] = []
        self._seq = 0
        self._holder: Optional[str] = None
        self._reason = ""
        self._granted_at = 0.0
        self._expires_at: Optional[float] = None
        self._ttl: Optional[float] = None
        self._capabilities = dict(CAPABILITY_DEFAULTS)
        if capabilities:
            self._capabilities.update({str(k): bool(v) for k, v in capabilities.items()})
        if holder is not None:
            self.acquire(holder, reason, ttl=ttl, actor="system")

    # ── 审计 ──

    def _record(self, kind: str, *, actor: str, holder: Optional[str], previous: Optional[str],
                reason: str = "", ttl: Optional[float] = None,
                expires_at: Optional[float] = None,
                detail: Optional[Mapping[str, Any]] = None) -> LeaseEvent:
        self._seq += 1
        event = LeaseEvent(
            seq=self._seq,
            kind=kind,
            at=self._clock(),
            actor=str(actor),
            holder=holder,
            previous=previous,
            reason=str(reason),
            ttl=(float(ttl) if ttl is not None else None),
            expires_at=expires_at,
            detail=dict(detail or {}),
        )
        self._events.append(event)
        del self._events[:-AUDIT_MAX]
        if self._audit_sink is not None:
            try:
                self._audit_sink(event.as_dict())
            except Exception as exc:  # noqa: BLE001 — 审计 sink 坏掉绝不拖垮主链路
                logger.warning("[ControlLease] 审计 sink 失败: %s", exc)
        if kind == LEASE_PREEMPT:
            logger.info("[ControlLease] 抢占：%s → %s（理由=%s，时限=%.1fs，被抢=%s）",
                        previous or "空闲", holder, reason, ttl or 0.0, previous or "无")
        return event

    def events(self, n: int = 0) -> list[dict]:
        """只读审计列表（``n=0`` 全量；否则最近 ``n`` 条）。"""
        with self._lock:
            rows = list(self._events) if not n else list(self._events[-int(n):])
        return [event.as_dict() for event in rows]

    def audit_log(self, n: int = 10) -> list[dict]:
        """最近 ``n`` 条审计（与 ``permissions`` / ``tpa`` 同名口径，便于仪表盘取用）。"""
        return self.events(int(n))

    # ── 状态 ──

    def _expire_if_due(self) -> Optional[LeaseEvent]:
        """惰性到期：到点 → 清空持租人并留一行 ``expire`` 审计（调用方在持锁下用）。"""
        if self._holder is None or self._expires_at is None:
            return None
        if self._clock() < self._expires_at:
            return None
        previous, reason, ttl, expires_at = self._holder, self._reason, self._ttl, self._expires_at
        self._holder = None
        self._reason = ""
        self._granted_at = 0.0
        self._expires_at = None
        self._ttl = None
        return self._record(LEASE_EXPIRE, actor="system", holder=None, previous=previous,
                            reason=f"expired({reason})" if reason else "expired",
                            ttl=ttl, expires_at=expires_at)

    def expire(self) -> Optional[dict]:
        """显式触发到期检查（已到期返回 ``expire`` 事件字典，否则 ``None``）。"""
        with self._lock:
            event = self._expire_if_due()
        return event.as_dict() if event is not None else None

    @property
    def holder(self) -> Optional[str]:
        """当前持租人（到期即视作空闲，并留一行 ``expire`` 审计）。"""
        with self._lock:
            self._expire_if_due()
            return self._holder

    @property
    def reason(self) -> str:
        with self._lock:
            self._expire_if_due()
            return self._reason

    @property
    def expires_at(self) -> Optional[float]:
        with self._lock:
            self._expire_if_due()
            return self._expires_at

    def capabilities(self) -> dict[str, bool]:
        """当前能力位快照（拷贝；降级判断读它，不许假装）。"""
        with self._lock:
            return dict(self._capabilities)

    def snapshot(self) -> dict[str, Any]:
        """只读快照：持租人 + 租约信息 + 能力位 + 审计条数（改返回值不影响内部）。"""
        with self._lock:
            self._expire_if_due()
            return {
                "holder": self._holder,
                "reason": self._reason,
                "granted_at": self._granted_at,
                "expires_at": self._expires_at,
                "ttl": self._ttl,
                "seq": self._seq,
                "events": len(self._events),
                "capabilities": dict(self._capabilities),
            }

    # ── 生命周期：获取 / 转移 / 抢占 / 释放 ─────────────────────

    def acquire(self, holder: str, reason: str = "", *, ttl: Optional[float] = None,
                actor: str = "") -> dict:
        """左脑一方**获取**控制租约（右脑调用 → ``LeaseDenied``）。

        已空闲 → 授予；已是本人 → 续租（刷新理由/时限）；他人持有 → ``LeaseConflict``。
        """
        holder = _validate_control_holder(holder)
        if ttl is not None and float(ttl) <= 0:
            raise ValueError("ttl 必须 > 0（租约必须有界）")
        with self._lock:
            self._expire_if_due()
            previous = self._holder
            if previous is not None and previous != holder:
                raise LeaseConflict(f"控制租约已被 {previous} 持有，不能直接获取")
            renew = previous == holder
            self._holder = holder
            self._reason = str(reason or self._reason)
            now = self._clock()
            self._granted_at = now
            self._expires_at = (now + float(ttl)) if ttl is not None else None
            self._ttl = float(ttl) if ttl is not None else None
            event = self._record(LEASE_GRANT, actor=actor or holder, holder=holder,
                                 previous=previous, reason=self._reason, ttl=ttl,
                                 expires_at=self._expires_at,
                                 detail={"renew": renew})
            return event.as_dict()

    def transfer(self, from_holder: str, to_holder: str, reason: str = "", *,
                 ttl: Optional[float] = None, actor: str = "") -> dict:
        """**显式转移**：当前持租人把租约交给另一个左脑身份（非法转移被拒 ✓）。

        右脑作为 ``to_holder`` → ``LeaseDenied``；``from_holder`` 不是当前持租人 →
        ``LeaseConflict``。
        """
        to_holder = _validate_control_holder(to_holder)
        if ttl is not None and float(ttl) <= 0:
            raise ValueError("ttl 必须 > 0（租约必须有界）")
        with self._lock:
            self._expire_if_due()
            previous = self._holder
            if previous != from_holder:
                raise LeaseConflict(
                    f"非法转移：{from_holder} 不是当前持租人（当前={previous or '空闲'}）")
            now = self._clock()
            self._holder = to_holder
            self._reason = str(reason or "")
            self._granted_at = now
            self._expires_at = (now + float(ttl)) if ttl is not None else None
            self._ttl = float(ttl) if ttl is not None else None
            event = self._record(LEASE_TRANSFER, actor=actor or from_holder, holder=to_holder,
                                 previous=previous, reason=self._reason, ttl=ttl,
                                 expires_at=self._expires_at)
            return event.as_dict()

    def preempt(self, holder: str, reason: str, *, ttl: float, actor: str = "",
                detail: Optional[Mapping[str, Any]] = None) -> dict:
        """**生存反射抢占**：必须带**理由** ✓ 与**时限** ✓；记录被抢的持租人。

        右脑作为 ``holder`` → ``LeaseDenied``（LLM 不能抢控制）。
        """
        holder = _validate_control_holder(holder)
        if not reason or not str(reason).strip():
            raise ValueError("抢占必须带理由（`reason` 不能为空）")
        if ttl is None or float(ttl) <= 0:
            raise ValueError("抢占必须有时限（`ttl` 必须 > 0）")
        with self._lock:
            self._expire_if_due()
            previous = self._holder
            now = self._clock()
            self._holder = holder
            self._reason = str(reason)
            self._granted_at = now
            self._expires_at = now + float(ttl)
            self._ttl = float(ttl)
            event = self._record(LEASE_PREEMPT, actor=actor or holder, holder=holder,
                                 previous=previous, reason=self._reason, ttl=float(ttl),
                                 expires_at=self._expires_at, detail=detail)
            return event.as_dict()

    def release(self, holder: str, reason: str = "", *, actor: str = "") -> dict:
        """持租人**释放**租约 → 回到空闲（非持租人调用 → ``LeaseConflict``）。"""
        with self._lock:
            self._expire_if_due()
            previous = self._holder
            if previous != holder:
                raise LeaseConflict(
                    f"非法释放：{holder} 不是当前持租人（当前={previous or '空闲'}）")
            self._holder = None
            self._reason = ""
            self._granted_at = 0.0
            self._expires_at = None
            self._ttl = None
            event = self._record(LEASE_RELEASE, actor=actor or holder, holder=None,
                                 previous=previous, reason=str(reason or ""))
            return event.as_dict()

    # ── 意图（右脑的唯一通道）──────────────────────────────────

    def submit_intent(self, kind: str, payload: Optional[Mapping[str, Any]] = None, *,
                      actor: str = HOLDER_RIGHT_LLM) -> dict:
        """右脑**提交意图**：只留审计，**绝不改变持租人** ✗（控制由左脑执行）。

        未知意图种类 → ``ValueError``（诚实拒绝，不假装接受）。
        """
        if kind not in INTENT_KINDS:
            raise ValueError(f"未知意图 {kind!r}；合法意图：{sorted(INTENT_KINDS)}")
        with self._lock:
            self._expire_if_due()
            event = self._record(LEASE_INTENT, actor=actor, holder=self._holder,
                                 previous=None, reason=str(kind),
                                 detail=dict(payload or {}))
            return event.as_dict()

    def direct_control(self, actor: str) -> bool:
        """右脑**直接写控制**的闸门：只有当前持租人才放行（右脑 → ``LeaseDenied``）。

        这是接口层面的硬约束（§8 第 3 条）：LLM 只改意图/世界状态，不直接摸按键。
        """
        with self._lock:
            self._expire_if_due()
            if is_right_brain(actor) or actor != self._holder:
                raise LeaseDenied(
                    f"{actor!r} 不能直写底层控制（当前持租人={self._holder or '空闲'}）；"
                    "请走 submit_intent()")
            return True


# ── 接线辅助：把一次 P0 反射抢占如实登记进租约 ────────────────
def register_reflex_preemption(lease: Optional[ControlLease], *, holder: str,
                               reason: str = "", ttl: float = DEFAULT_REFLEX_TTL,
                               actor: str = "system",
                               detail: Optional[Mapping[str, Any]] = None) -> Optional[dict]:
    """把一次 P0 生存反射（战斗/上浮/进食）抢占登记进租约并留审计。

    * ``lease is None``（没有租约 / 没接线）→ 返回 ``None``，**不做任何事**（行为照旧）；
    * 抢占失败（理由/时限缺失等）只 warning，**绝不拖垮主链路**。

    本函数是"落接口但不启用仲裁"的最小接线：唯一的可观测变化是审计输出。
    """
    if lease is None:
        return None
    if not reason and holder in REFLEX_HOLDERS:
        reason = REFLEX_HOLDERS[holder]
    try:
        return lease.preempt(holder, reason, ttl=ttl, actor=actor, detail=detail)
    except Exception as exc:  # noqa: BLE001 — 登记失败绝不影响主链路的让路行为
        logger.warning("[ControlLease] 反射抢占登记失败（不影响主链路）: %s", exc)
        return None
