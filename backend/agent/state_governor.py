"""状态治理 —— 「卡死即摘掉」：对**已注册**的状态/任务做**进展判定**并按真实原因取消。

用户口径（硬）：

* 「如果当前正在进行的状态**无法正常工作**，那就得**切到下一个状态**，或者**直接打断**」
  —— 本模块就是那个"打断"：**N 秒无进展 ⇒ 摘掉它**；
* 诚实优先：取消**必须带真实原因**（例："位置未变 + 已报告卡住"），并**如实回给模型**
  （走现有的 ``task_state`` 失败通道 / ``_action_note``，**不新造**）；
* 取消后**退避**：同一目标在 ``retry_cooldown_seconds`` 内**不许立刻重试** ✗（否则又成死循环）；
* **绝不取消 P0 反射** ✗（``survival=True`` 的注册只观察、不摘除）；
* 阈值与退避时间**可配置**（``.local/config.json`` 的 ``state_governor`` 段，带默认值）。

进展信号（自行选取并在此说明）：

1. **位置是否变化** —— ``state.player.x/y/z``（默认签名，圆到 0.01）；
2. **同一工具结果是否重复** —— 任务 ``task_state`` 的 ``status|progress|detail`` 组合不变；
3. **Java 的 ``[Stuck]`` / 失败上报** —— ``task_state.detail``/``reason`` 含 ``stuck``/``卡住``，
   或 ``status == "failed"``，或顶层 ``stuck`` 字段；
4. **超时** —— ``registered``/上次进展起 ``no_progress_seconds`` 到点。

**例外（用户口径，硬）**：**读 LLM 期间进展判定暂停** —— 由 session 在调用前后
``begin_model_call`` / ``end_model_call``（见 llm_service 的"模型在飞"通知）。用户在等国外
服务商的大模型时，"位置没变"是**因为我们正在等模型**，不是任务卡住；这期间取消等于
"自己把正在做的事打断"。等待时长按实际时长补平，恢复后照常判定；若这期间真取消了，
理由里必须如实写"在等模型"，**绝不**写"位置未变"。

一个**总开关**：``state_governor.enabled``（默认**开**）。关掉 ⇒ 本批行为完全回到现在
（不注册、不判定、不取消、不占资源）。

边界（硬）✗：**不新增任何能力**、**不自己发起任何工具/动作** —— 只**仲裁 + 取消已存在的**；
不新增工具；不碰 wire 协议；不碰 L0/L1；不改 Java。
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional

from .resource_lease import (RESOURCE_ATTACK, RESOURCE_CHAT, RESOURCE_MOVE, RESOURCE_USE,
                             PRIORITY_DEFAULT, PRIORITY_SURVIVAL, ResourceLease,
                             normalize_resources)

logger = logging.getLogger("state_governor")


# ── 默认值（可被 ``.local/config.json`` 的 ``state_governor`` 段覆盖）──
DEFAULT_ENABLED = True
DEFAULT_NO_PROGRESS_SECONDS = 20.0
DEFAULT_RETRY_COOLDOWN_SECONDS = 30.0
DEFAULT_MAX_TRACKED = 50
# 位置判定的量化精度：圆到 2 位小数，避免亚像素抖动被当成"动了"。
POSITION_ROUND = 2

# 审计事件类别。
GOV_REGISTER = "register"
GOV_RELEASE = "release"
GOV_CANCEL = "cancel"
GOV_AUDIT_MAX = 80


def _as_float(value: Any, default: float) -> float:
    try:
        num = float(value)
    except (TypeError, ValueError):
        return float(default)
    return num if num >= 0 else float(default)


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        low = value.strip().lower()
        if low in {"1", "true", "yes", "on"}:
            return True
        if low in {"0", "false", "no", "off"}:
            return False
    return default


@dataclass(frozen=True)
class GovernorConfig:
    """治理开关与阈值（全部带默认值；非法值退回默认）。"""

    enabled: bool = DEFAULT_ENABLED
    no_progress_seconds: float = DEFAULT_NO_PROGRESS_SECONDS
    retry_cooldown_seconds: float = DEFAULT_RETRY_COOLDOWN_SECONDS
    max_tracked: int = DEFAULT_MAX_TRACKED

    @classmethod
    def from_mapping(cls, raw: Any) -> "GovernorConfig":
        if isinstance(raw, GovernorConfig):
            return raw
        if not isinstance(raw, Mapping):
            return cls()
        try:
            max_tracked = int(raw.get("max_tracked", DEFAULT_MAX_TRACKED))
        except (TypeError, ValueError):
            max_tracked = DEFAULT_MAX_TRACKED
        return cls(
            enabled=_as_bool(raw.get("enabled"), DEFAULT_ENABLED),
            no_progress_seconds=_as_float(raw.get("no_progress_seconds"),
                                          DEFAULT_NO_PROGRESS_SECONDS),
            retry_cooldown_seconds=_as_float(raw.get("retry_cooldown_seconds"),
                                             DEFAULT_RETRY_COOLDOWN_SECONDS),
            max_tracked=max(1, max_tracked),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "no_progress_seconds": self.no_progress_seconds,
            "retry_cooldown_seconds": self.retry_cooldown_seconds,
            "max_tracked": self.max_tracked,
        }


@dataclass
class _Tracked:
    """一个被治理的状态/任务的台账。"""

    key: str
    resources: frozenset[str]
    priority: int
    kind: str
    target: str
    survival: bool
    reason: str
    registered_at: float
    last_progress_at: float
    cancel: Optional[Callable[[dict], None]] = None
    progress: Optional[Callable[[dict], Any]] = None
    last_signature: Any = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "resources": sorted(self.resources),
            "priority": self.priority,
            "kind": self.kind,
            "target": self.target,
            "survival": self.survival,
            "reason": self.reason,
            "registered_at": self.registered_at,
            "last_progress_at": self.last_progress_at,
        }


class StateGovernor:
    """已注册状态/任务的进展判定 → 卡死即取消（带真实原因 + 退避）。"""

    def __init__(self, *, config: Any = None, lease: Optional[ResourceLease] = None,
                 clock: Callable[[], float] = time.time,
                 cancel_hook: Optional[Callable[[dict], None]] = None,
                 audit: Optional[Callable[[dict], None]] = None):
        self._clock = clock
        self._lease = lease
        self._cancel_hook = cancel_hook
        self._audit_sink = audit
        self._config_source = config
        self._lock = threading.RLock()
        self._tracked: dict[str, _Tracked] = {}
        self._backoff: dict[str, float] = {}
        self._events: list[dict] = []
        self._seq = 0
        # "模型在飞"状态：读 LLM（语义门 / 规划 / decider / 视觉）期间进展判定**暂停**。
        # 见 `begin_model_call` 的注释（用户口径：等大模型是"正在做的事"，不是"没进展"）。
        self._model_calls = 0
        self._model_wait_started_at: Optional[float] = None
        self._model_wait_labels: list[str] = []

    # ── 配置 ──

    def config(self) -> GovernorConfig:
        src = self._config_source
        if callable(src):
            try:
                src = src()
            except Exception as exc:  # noqa: BLE001 — 配置读失败绝不拖垮主链路
                logger.warning("[StateGovernor] 配置读取失败，用默认: %s", exc)
                src = None
        return GovernorConfig.from_mapping(src)

    def enabled(self) -> bool:
        return bool(self.config().enabled)

    # ── 审计 ──

    def _record(self, kind: str, *, key: str, reason: str = "",
                detail: Optional[Mapping[str, Any]] = None) -> dict:
        self._seq += 1
        event = {
            "seq": self._seq,
            "kind": kind,
            "at": self._clock(),
            "key": str(key),
            "reason": str(reason or ""),
            "detail": dict(detail or {}),
        }
        self._events.append(event)
        del self._events[:-GOV_AUDIT_MAX]
        if self._audit_sink is not None:
            try:
                self._audit_sink(dict(event))
            except Exception as exc:  # noqa: BLE001
                logger.warning("[StateGovernor] 审计 sink 失败: %s", exc)
        return event

    def events(self, n: int = 0) -> list[dict]:
        with self._lock:
            rows = list(self._events) if not n else list(self._events[-int(n):])
        return [dict(row, detail=dict(row["detail"])) for row in rows]

    def audit_log(self, n: int = 10) -> list[dict]:
        return self.events(int(n))

    # ── 退避 ──

    def is_backed_off(self, target: str) -> bool:
        """同一目标是否还在取消后的退避窗口内（在窗口内**不许立刻重试**）。"""
        with self._lock:
            until = self._backoff.get(str(target or ""), 0.0)
        return bool(until) and self._clock() < until

    def backoff_until(self, target: str) -> float:
        with self._lock:
            return float(self._backoff.get(str(target or ""), 0.0))

    def note_backoff(self, target: str, seconds: Optional[float] = None) -> float:
        """手动给某目标加退避（返回解禁时刻）。"""
        cfg = self.config()
        window = cfg.retry_cooldown_seconds if seconds is None else max(0.0, float(seconds))
        with self._lock:
            until = self._clock() + window
            self._backoff[str(target or "")] = until
        return until

    # ── 注册 / 注销 ──

    def register(self, key: str, *, resources: Any = (), priority: int = PRIORITY_DEFAULT,
                 kind: str = "", target: str = "", survival: bool = False,
                 reason: str = "", cancel: Optional[Callable[[dict], None]] = None,
                 progress: Optional[Callable[[dict], Any]] = None) -> bool:
        """注册一个要治理的状态/任务。

        ``survival=True`` ⇒ **P0 反射**：只观察，**绝不取消** ✗。
        目标在退避窗口内 ⇒ 拒绝注册（返回 ``False``），调用方据此别立刻重试。
        """
        cfg = self.config()
        if not cfg.enabled:
            return False
        key = str(key or "").strip()
        if not key:
            raise ValueError("key 必须是非空身份串")
        target = str(target or key)
        if self.is_backed_off(target):
            logger.debug("[StateGovernor] %s 在退避窗口内，暂不注册", target)
            return False
        try:
            res = normalize_resources(resources)
        except ValueError as exc:
            logger.warning("[StateGovernor] %s 资源非法（%s）→ 按空资源注册", key, exc)
            res = frozenset()
        priority = PRIORITY_SURVIVAL if survival else int(priority)
        now = self._clock()
        with self._lock:
            existing = self._tracked.get(key)
            if existing is not None:
                existing.resources = res
                existing.priority = priority
                existing.kind = str(kind or existing.kind)
                existing.target = target
                existing.survival = bool(survival)
                existing.reason = str(reason or existing.reason)
                if cancel is not None:
                    existing.cancel = cancel
                if progress is not None:
                    existing.progress = progress
                self._lease_request(existing)
                return True
            while len(self._tracked) >= cfg.max_tracked:
                victim = next((k for k, r in self._tracked.items() if not r.survival), None)
                if victim is None:
                    break  # 全是生存反射 → 绝不为了腾地方摘除 P0
                self.unregister(victim, reason="max_tracked 淘汰")
            rec = _Tracked(key=key, resources=res, priority=priority, kind=str(kind or ""),
                           target=target, survival=bool(survival), reason=str(reason or ""),
                           registered_at=now, last_progress_at=now, cancel=cancel,
                           progress=progress, last_signature=None)
            self._tracked[key] = rec
            self._lease_request(rec)
        self._record(GOV_REGISTER, key=key, reason=rec.reason,
                     detail={"resources": sorted(res), "priority": priority, "kind": rec.kind,
                             "target": target, "survival": rec.survival})
        return True

    def unregister(self, key: str, *, reason: str = "") -> bool:
        """注销（正常结束）：释放它占的资源，留一行 ``release`` 审计。"""
        with self._lock:
            rec = self._tracked.pop(str(key), None)
        if rec is None:
            return False
        if self._lease is not None:
            try:
                self._lease.release(rec.key, reason or "governor release")
            except Exception as exc:  # noqa: BLE001
                logger.warning("[StateGovernor] 释放资源失败: %s", exc)
        self._record(GOV_RELEASE, key=rec.key, reason=reason,
                     detail={"kind": rec.kind, "target": rec.target})
        return True

    def release_all(self, reason: str = "governor released") -> None:
        """全部注销（离场/停摆时用；只释放，不取消）。"""
        with self._lock:
            keys = list(self._tracked.keys())
        for key in keys:
            self.unregister(key, reason=reason)

    def _lease_request(self, rec: _Tracked) -> None:
        if self._lease is None:
            return
        try:
            self._lease.request(rec.key, rec.resources, priority=rec.priority,
                                reason=rec.reason or "governor tracked", survival=rec.survival,
                                kind=rec.kind, target=rec.target, actor="governor")
        except Exception as exc:  # noqa: BLE001 — 资源仲裁失败不拖垮治理
            logger.warning("[StateGovernor] 资源请求失败（%s）: %s", rec.key, exc)

    # ── "模型在飞"：读 LLM 期间暂停进展判定 ──

    def begin_model_call(self, label: str = "") -> None:
        """标记"开始等模型"（语义门 / 规划 / decider / 视觉的读调用）。

        用户口径（原话）："模型**超时**还是有可能的，所以说得等有些时候不得不等后台，不得不等
        **大**模型，因为现在我配的服务商是国外的，所以说肯定第一，一方面有网络延迟，另外一方面
        每轮的**首字延迟**肯定也高。"

        等大模型是**正在做的事**，不是"没进展" —— 若这期间看门狗按"位置未变"把任务掐掉，
        等于**自己把正在做的事打断**。所以读模型期间进展判定整体暂停；等待时长由
        `end_model_call` 按**实际等待时长**从"无进展"计时里扣掉（`label` 用于如实交代"在等谁"）。
        """
        label = str(label or "")
        with self._lock:
            if self._model_calls <= 0:
                self._model_wait_started_at = self._clock()
                self._model_wait_labels = []
            self._model_calls += 1
            if label and label not in self._model_wait_labels:
                self._model_wait_labels.append(label)

    def end_model_call(self) -> None:
        """标记"模型回来了"：把这次等待的时长从"无进展"计时里补平（暂停结束）。"""
        with self._lock:
            if self._model_calls <= 0:
                self._model_calls = 0
                return
            self._model_calls -= 1
            if self._model_calls > 0:
                return
            started = self._model_wait_started_at
            self._model_wait_started_at = None
            self._model_wait_labels = []
            if started is None:
                return
            now = self._clock()
            waited = max(0.0, now - started)
            if waited <= 0:
                return
            # 等待时长**不**计进"无进展"：基线整体后移，但绝不超过"现在"
            # （等待期间才注册的项，其注册时刻之后才算数）。
            for rec in self._tracked.values():
                rec.last_progress_at = min(now, rec.last_progress_at + waited)

    def model_in_flight(self) -> bool:
        """是否有 LLM 调用正在飞（读模型期间不判"卡死"）。"""
        with self._lock:
            return self._model_calls > 0

    def model_wait_label(self) -> str:
        """正在等的是哪个角色的模型（空串 = 没在等）。"""
        with self._lock:
            return "、".join(self._model_wait_labels)

    def wait_note(self) -> str:
        """取消理由里如实交代"当时在等模型"的那一段（没在等 = 空串）。

        只写"在等谁"，**不**写任何"位置未变/无进展"的说法 —— 用户口径：在等大模型期间，
        "没动"不是任务卡住，不许把"自己打断自己"包装成"任务卡死"。
        """
        if not self.model_in_flight():
            return ""
        label = self.model_wait_label()
        who = f"：{label}" if label else ""
        return f"（在等模型{who}）"

    # ── 进展判定 → 取消 ──

    def tick(self, state: Mapping[str, Any] | None) -> list[dict]:
        """按进展信号判定已注册项；无进展/已报卡住 ⇒ 取消（带真实原因）。返回取消台账。"""
        cfg = self.config()
        if not cfg.enabled:
            return []
        state = state if isinstance(state, Mapping) else {}
        now = self._clock()
        cancelled: list[dict] = []
        with self._lock:
            keys = list(self._tracked.keys())
        inflight = self.model_in_flight()
        for key in keys:
            with self._lock:
                rec = self._tracked.get(key)
            if rec is None or rec.survival:
                continue  # P0 反射永不被取消 ✗
            stuck = self._stuck_reason(rec, state)
            if stuck:
                # 上游**明确**报了卡住 → 照取消；但若正等着模型，理由里如实说"在等模型"
                # （绝不写"位置未变"这种"自己打断自己"的说法）。
                cancelled.append(self._cancel(rec, f"{stuck}{self.wait_note()}", cfg))
                continue
            if inflight:
                # 读模型期间：进展判定**暂停**（等大模型 = 正在做的事，不是没进展）。
                # 这里既不推进签名、也不取消；等 `end_model_call` 按实际等待时长补平。
                continue
            signature = self._signature(rec, state)
            if rec.last_signature is None:
                rec.last_signature = signature   # 首次观测＝基线，不算"进展"
            elif signature != rec.last_signature:
                rec.last_signature = signature
                rec.last_progress_at = now
                continue
            if now - rec.last_progress_at >= cfg.no_progress_seconds:
                if self.model_in_flight():
                    # 这一拍刚好又进入"等模型" ⇒ 先不取消（等待时长稍后补平）。
                    continue
                # 如实说**没用的是哪个信号**：自定义信号 vs 默认的"位置 + 任务状态"。
                # 老文案一律写"位置未变"，对 `eat`/合成这类**本来就不动**的任务是误导 ——
                # 用户看到的就是"它明明在饭点上却没动"，而真实原因可能是任务状态也没推进。
                what = "它自己的进展信号没变" if rec.progress is not None else "位置与任务状态都没变"
                reason = (f"{now - rec.last_progress_at:.0f}s 无进展"
                          f"（{what}，任务 {rec.kind or key}）")
                cancelled.append(self._cancel(rec, reason, cfg))
        return cancelled

    def _cancel(self, rec: _Tracked, reason: str, cfg: GovernorConfig) -> dict:
        with self._lock:
            current = self._tracked.get(rec.key)
            if current is rec:
                self._tracked.pop(rec.key, None)
            until = self._clock() + cfg.retry_cooldown_seconds
            self._backoff[rec.target or rec.key] = until
        if self._lease is not None:
            try:
                self._lease.revoke(rec.key, reason)
            except Exception as exc:  # noqa: BLE001 — 撤销失败不拖垮取消本身
                logger.warning("[StateGovernor] 撤销资源失败: %s", exc)
        payload = {
            "key": rec.key,
            "kind": rec.kind,
            "target": rec.target,
            "reason": reason,
            "resources": sorted(rec.resources),
            "at": self._clock(),
            "backoff_until": until,
        }
        self._record(GOV_CANCEL, key=rec.key, reason=reason,
                     detail={"kind": rec.kind, "target": rec.target,
                             "resources": sorted(rec.resources), "backoff_until": until})
        cb = rec.cancel or self._cancel_hook
        if cb is not None:
            try:
                cb(payload)
            except Exception as exc:  # noqa: BLE001 — 回调坏掉不拖垮治理
                logger.warning("[StateGovernor] 取消回调失败: %s", exc)
        logger.warning("[StateGovernor] 取消 %s（%s）：%s", rec.key, rec.kind or "?", reason)
        return payload

    # ── 进展签名 ──

    def _signature(self, rec: _Tracked, state: Mapping[str, Any]) -> Any:
        if rec.progress is not None:
            try:
                return rec.progress(state)
            except Exception as exc:  # noqa: BLE001 — 自定义信号坏了退回默认
                logger.debug("[StateGovernor] 自定义进展信号失败: %s", exc)
        player = state.get("player") if isinstance(state, Mapping) else None
        player = player if isinstance(player, Mapping) else {}
        pos = tuple(round(float(player.get(axis, 0) or 0), POSITION_ROUND)
                    for axis in ("x", "y", "z"))
        detail = ""
        task = state.get("task_state") if isinstance(state, Mapping) else None
        if isinstance(task, Mapping) and rec.kind and \
                str(task.get("kind", "")).strip().lower() == rec.kind.strip().lower():
            detail = "|".join(str(task.get(field, "")) for field in
                              ("status", "progress", "detail"))
        return (pos, detail)

    @staticmethod
    def _stuck_reason(rec: _Tracked, state: Mapping[str, Any]) -> str:
        if not isinstance(state, Mapping):
            return ""
        if state.get("stuck") or state.get("stuck_report"):
            return "上游上报卡住"
        task = state.get("task_state")
        if not isinstance(task, Mapping):
            return ""
        kind = str(task.get("kind", "")).strip().lower()
        if rec.kind and kind and kind != rec.kind.strip().lower():
            return ""  # 状态与任务对不上：不是它的事
        combined = f"{task.get('detail') or ''} {task.get('reason') or ''}"
        low = combined.lower()
        if "[stuck]" in low or "stuck" in low or "卡住" in combined:
            return f"已报告卡住：{(task.get('detail') or task.get('reason') or 'stuck')}".strip()
        if str(task.get("status", "")).strip().lower() == "failed":
            return f"任务上报失败：{(task.get('reason') or task.get('detail') or 'failed')}".strip()
        return ""

    # ── 状态同步（session 的接线入口）──

    def sync(self, state: Mapping[str, Any] | None, *, current_label: Optional[str] = None,
             priority: int = PRIORITY_DEFAULT, kind: str = "", target: str = "",
             resources: Any = None) -> list[dict]:
        """把"当前在途动作"同步进治理台账，然后判定一次。

        调用方（session）只需给出 ``current_label``（``action_manager.current_label``）；
        资源从 ``resource_lease.TOOL_RESOURCES`` 查表（唯一声明处），优先级由调用方给。
        台账里不再存在的在途动作会被注销（释放资源）。
        """
        cfg = self.config()
        if not cfg.enabled:
            self.release_all("governor disabled")
            return []
        label = str(current_label or "").strip()
        key = f"action:{label}" if label else ""
        if key and not self.is_backed_off(target or label):
            self.register(key, resources=resources if resources is not None
                          else _resources_for(label), priority=priority, kind=kind or label,
                          target=target or label, reason=f"在途动作 {label}")
        with self._lock:
            stale = [k for k in self._tracked if k.startswith("action:") and k != key]
        for old in stale:
            self.unregister(old, reason="在途动作已结束")
        return self.tick(state)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            now = self._clock()
            return {
                "enabled": self.enabled(),
                "config": self.config().as_dict(),
                "tracked": [rec.as_dict() for rec in self._tracked.values()],
                "backoff": {t: round(until - now, 1)
                            for t, until in self._backoff.items() if until > now},
                "events": len(self._events),
                # "模型在飞"：排障时一眼看出"没进展判定为什么没动手"（用户在等大模型）。
                "model_in_flight": self._model_calls > 0,
                "model_waiting_for": "、".join(self._model_wait_labels),
            }


def _resources_for(label: str) -> frozenset[str]:
    """在途动作标签 → 资源（查工具资源表；未知标签＝不占资源）。"""
    from .resource_lease import resources_for_tool
    res = resources_for_tool(label)
    if res:
        return res
    # 少数非工具标签（技能名）也要能声明：这里只覆盖已知的移动/战斗/进食类。
    fallback = {
        "jump": frozenset({RESOURCE_MOVE}),
        "sneak": frozenset({RESOURCE_MOVE}),
        "sprint": frozenset({RESOURCE_MOVE}),
        "attack": frozenset({RESOURCE_ATTACK}),
        "eat": frozenset({RESOURCE_USE}),
        "stop_all": frozenset(),
        "send_chat": frozenset({RESOURCE_CHAT}),
    }
    return fallback.get(str(label or "").strip().lower(), frozenset())
