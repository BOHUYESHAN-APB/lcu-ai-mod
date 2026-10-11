"""Job Registry —— 长任务句柄（`docs/dev/AGENT_ARCH.md` §10.2 #2）与编排轮次（§10.2 #3）。

用户口径（硬）：

* **能力=工具**：job 的**来源只能是模型已请求的调用**（工具调用 / 任务）——
  后台**绝不自己发起** ✗（本模块只**登记 / 查看 / 取消**，不产生新能力）；
* **硬门槛**：一个东西要登记成 job，**必须同时具备四样**——
  ① 进度来源 ② 终止条件 ③ 失败原因（带 code）④ 取消路径；
  **四样不全的一律不许登记** ✗（否则又变成"卡住没人管"）→ ``JobRejected``；
* **卡死走已有 ``state_governor``**（取消 + 审计 + 退避），本模块**不另造**卡死判定 ✗
  （注册 job 时把取消回调交给同一个治理器）；
* **编排轮次**：当且仅当 ``task_list`` 里有**模型自己创建的活跃任务**时，在**事件**发生时
  （进度变化 / 出结果 / 卡住 / 玩家说话）把模型请回来做下一步 —— **不是定时器** ✗、
  **不是后台自主引擎** ✗；每轮只喂**紧凑进展摘要**，且**只进 L2（当轮 user）** ✗（不进 L0/L1）。

边界（硬）✗：不新增能力、不改线协议、不碰 L0/L1、不做 composite tools（那是下一批）。
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

from .resource_lease import PRIORITY_DEFAULT

logger = logging.getLogger("job_registry")


# ── job 状态 ────────────────────────────────────────────────────
JOB_RUNNING = "running"
JOB_DONE = "done"
JOB_FAILED = "failed"
JOB_CANCELLED = "cancelled"
JOB_TERMINAL: frozenset[str] = frozenset({JOB_DONE, JOB_FAILED, JOB_CANCELLED})
JOB_STATUSES: frozenset[str] = frozenset({JOB_RUNNING, *JOB_TERMINAL})

# ── 编排事件类别（用户口径点名的四种）───────────────────────────
ORCH_EVENT_PROGRESS = "progress"        # 进度变化
ORCH_EVENT_OUTCOME = "outcome"          # 出结果（终态 outcome）
ORCH_EVENT_STUCK = "stuck"              # 卡住（治理器取消）
ORCH_EVENT_PLAYER = "player_spoke"      # 玩家说话
ORCH_EVENT_KINDS: frozenset[str] = frozenset({
    ORCH_EVENT_PROGRESS, ORCH_EVENT_OUTCOME, ORCH_EVENT_STUCK, ORCH_EVENT_PLAYER,
})

# 编排轮次总开关的默认值（用户明确要"默认开"）。
DEFAULT_ENABLED = True
# 两次编排轮之间的最小间隔（秒）：防止"一轮又产生新事件 → 下一轮"变成自激循环。
ORCH_MIN_INTERVAL = 2.0
# 审计环形缓冲长度（与 `resource_lease.AUDIT_MAX` 同口径）。
AUDIT_MAX = 80
# 同屏 job 台账上限（超出淘汰最旧的终态）。
DEFAULT_MAX_JOBS = 50


def _clamp01(value: Any, default: float = 0.0) -> float:
    """进度夹到 [0, 1]；非法值退回 default（不让 NaN/字符串污染快照）。"""
    try:
        num = float(value)
    except (TypeError, ValueError):
        return float(default)
    if num != num:  # NaN
        return float(default)
    return 0.0 if num < 0.0 else (1.0 if num > 1.0 else num)


def _describe(value: Any) -> str:
    """把进度来源 / 终止条件渲染成人读串（callable 取名字，字符串原样）。"""
    if callable(value):
        return str(getattr(value, "__name__", "") or "callable")
    return str(value or "")


def _is_declared(value: Any) -> bool:
    """进度来源 / 终止条件：可调用的实现，或非空的人读描述。"""
    if callable(value):
        return True
    return isinstance(value, str) and bool(value.strip())


class JobRejected(Exception):
    """登记被拒（四样硬门槛不全，或治理器退避中）。"""

    def __init__(self, reason: str, missing: Optional[list[str]] = None):
        super().__init__(str(reason))
        self.reason = str(reason)
        self.missing = list(missing or [])


def missing_requirements(*, progress: Any = None, terminal: Any = None,
                         failure: Any = None, cancel: Any = None) -> list[str]:
    """四样硬门槛里缺了哪些（空列表＝齐备）。**唯一的判定处**。"""
    missing: list[str] = []
    if not _is_declared(progress):
        missing.append("progress")      # ① 进度来源
    if not _is_declared(terminal):
        missing.append("terminal")      # ② 终止条件
    if not _is_declared(failure):
        missing.append("failure")       # ③ 失败原因（带 code）
    if not callable(cancel):
        missing.append("cancel")        # ④ 取消路径（必须可执行）
    return missing


@dataclass
class Job:
    """一个长任务句柄：``{id, tool, status, progress, started_at, result}`` + 四样门槛。

    字段刻意扁平、可 JSON 直出（`/api/status` 与仪表盘直接消费）。
    """

    id: str
    tool: str
    status: str = JOB_RUNNING
    progress: float = 0.0
    started_at: float = 0.0
    result: Optional[dict] = None
    # ── 四样硬门槛（登记时齐备；缺一即 JobRejected）──
    progress_source: Any = None
    terminal_condition: Any = None
    failure_code: str = ""
    cancel_path: Any = None
    # ── 归属 / 仲裁信息 ──
    resources: tuple[str, ...] = ()
    priority: int = PRIORITY_DEFAULT
    actor: str = ""
    detail: str = ""
    updated_at: float = 0.0
    finished_at: float = 0.0

    @property
    def terminal(self) -> bool:
        return self.status in JOB_TERMINAL

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "tool": self.tool,
            "status": self.status,
            "progress": self.progress,
            "started_at": self.started_at,
            "result": self.result,
            "failure_code": self.failure_code,
            "progress_source": _describe(self.progress_source),
            "terminal_condition": _describe(self.terminal_condition),
            "has_cancel_path": bool(callable(self.cancel_path)),
            "resources": list(self.resources),
            "priority": self.priority,
            "actor": self.actor,
            "detail": self.detail,
            "updated_at": self.updated_at,
            "finished_at": self.finished_at,
        }


class JobRegistry:
    """长任务句柄台账：登记（四样门槛）/ 查看 / 进度 / 结束 / **取消**。

    线程安全（mod 事件线程 + asyncio 主循环两条写入路径）。
    卡死判定**不在这里**：注册时把取消回调交给同一个 ``state_governor``（复用，不另造）。
    """

    def __init__(self, *, governor: Any = None, clock: Callable[[], float] = time.time,
                 id_factory: Optional[Callable[[], str]] = None,
                 audit: Optional[Callable[[dict], None]] = None,
                 max_jobs: int = DEFAULT_MAX_JOBS):
        self._governor = governor
        self._clock = clock
        self._id_factory = id_factory
        self._audit_sink = audit
        self._max_jobs = max(1, int(max_jobs))
        self._lock = threading.RLock()
        self._jobs: dict[str, Job] = {}
        self._seq = 0
        self._events: list[dict] = []

    # ── 内部 ──

    def _next_id(self) -> str:
        self._seq += 1
        return f"job_{self._seq}"

    def _new_id(self) -> str:
        return str(self._id_factory()) if self._id_factory is not None else self._next_id()

    def _record(self, kind: str, *, job_id: str, reason: str = "",
                detail: Optional[dict] = None) -> dict:
        event = {
            "seq": len(self._events) + 1,
            "kind": str(kind),
            "at": self._clock(),
            "job": str(job_id),
            "reason": str(reason or ""),
            "detail": dict(detail or {}),
        }
        self._events.append(event)
        del self._events[:-AUDIT_MAX]
        if self._audit_sink is not None:
            try:
                self._audit_sink(dict(event))
            except Exception as exc:  # noqa: BLE001 — 审计 sink 坏掉绝不拖垮主链路
                logger.warning("[JobRegistry] 审计 sink 失败: %s", exc)
        return event

    def _make_room(self) -> None:
        """容量已满时先裁最旧的**终态** job（活跃 job 不动）。"""
        if len(self._jobs) < self._max_jobs:
            return
        overflow = len(self._jobs) - self._max_jobs + 1
        terminal = sorted((j for j in self._jobs.values() if j.terminal),
                          key=lambda j: (j.finished_at, j.started_at))
        for job in terminal[:overflow]:
            self._jobs.pop(job.id, None)
            overflow -= 1
        if overflow <= 0:
            return
        rest = sorted((j for j in self._jobs.values() if not j.terminal),
                      key=lambda j: j.started_at)
        for job in rest[:overflow]:
            self._jobs.pop(job.id, None)

    def _governor_key(self, job_id: str) -> str:
        return f"job:{job_id}"

    def _unregister_governor(self, job: Job) -> None:
        if self._governor is None:
            return
        try:
            self._governor.unregister(self._governor_key(job.id), reason="job 结束")
        except Exception as exc:  # noqa: BLE001 — 治理器注销失败不拖垮主链路
            logger.debug("[JobRegistry] 治理器注销失败(%s): %s", job.id, exc)

    # ── 登记（唯一入口，四样硬门槛在这里拦）──

    def register(self, tool: str, *, progress: Any, terminal: Any, failure: Any,
                 cancel: Any, resources: Any = (), priority: int = PRIORITY_DEFAULT,
                 actor: str = "", detail: str = "", result: Optional[dict] = None) -> Job:
        """登记一个 job；四样门槛不全 → ``JobRejected``（**绝不登记半成品**）。

        ``progress`` / ``terminal`` 可为可调用实现或非空描述串；
        ``failure`` 是失败原因 code（非空串）；``cancel`` 必须是**可执行的取消路径**。
        """
        missing = missing_requirements(progress=progress, terminal=terminal,
                                       failure=failure, cancel=cancel)
        if missing:
            raise JobRejected(
                f"job 登记被拒：缺 {'、'.join(missing)}（进度来源/终止条件/失败原因/取消路径必须齐备）",
                missing=missing)
        now = self._clock()
        with self._lock:
            self._make_room()
            job = Job(
                id=self._new_id(),
                tool=str(tool or ""),
                status=JOB_RUNNING,
                progress=0.0,
                started_at=now,
                result=dict(result) if isinstance(result, dict) else result,
                progress_source=progress,
                terminal_condition=terminal,
                failure_code=str(failure),
                cancel_path=cancel,
                resources=tuple(sorted(str(r) for r in (resources or ()))),
                priority=int(priority),
                actor=str(actor or ""),
                detail=str(detail or ""),
                updated_at=now,
            )
            self._jobs[job.id] = job
        self._register_governor(job)
        self._record("register", job_id=job.id, reason=str(tool or ""),
                     detail={"tool": job.tool, "priority": job.priority})
        logger.info("[JobRegistry] 登记 job %s（%s）", job.id, job.tool)
        return job

    def _register_governor(self, job: Job) -> None:
        """把 job 的**卡死判定**交给已有治理器（复用；资源由 planner/lease 仲裁，故为空）。"""
        governor = self._governor
        if governor is None:
            return
        progress_source = job.progress_source
        progress = ((lambda _state, src=progress_source: src())
                    if callable(progress_source) else None)

        def _cancel(payload: dict, _id: str = job.id) -> None:
            data = payload if isinstance(payload, dict) else {}
            self.cancel(_id, reason=str(data.get("reason") or "stuck"))

        try:
            governor.register(self._governor_key(job.id), resources=(),
                              priority=job.priority, kind=job.tool,
                              target=self._governor_key(job.id),
                              reason=f"job {job.tool}", cancel=_cancel, progress=progress)
        except Exception as exc:  # noqa: BLE001 — 治理器注册失败不拖垮登记
            logger.warning("[JobRegistry] 治理器注册失败(%s): %s", job.id, exc)

    # ── 查看 ──

    def get(self, job_id: str) -> Optional[Job]:
        with self._lock:
            return self._jobs.get(str(job_id))

    def jobs(self, *, limit: Optional[int] = None) -> list[Job]:
        """排序：活跃在前（按 started_at），终态在后（按 finished_at 倒序）。"""
        with self._lock:
            rows = list(self._jobs.values())
        active = sorted((j for j in rows if not j.terminal), key=lambda j: j.started_at)
        done = sorted((j for j in rows if j.terminal),
                      key=lambda j: (j.finished_at, j.started_at), reverse=True)
        ordered = active + done
        return ordered[:max(0, int(limit))] if limit is not None else ordered

    def active(self) -> list[Job]:
        return [j for j in self.jobs() if not j.terminal]

    # ── 生命周期 ──

    def set_progress(self, job_id: str, value: Any = None, *, detail: str = "") -> Optional[Job]:
        with self._lock:
            job = self._jobs.get(str(job_id))
            if job is None or job.terminal:
                return job
            if value is not None:
                job.progress = _clamp01(value, job.progress)
            if detail:
                job.detail = str(detail)
            job.updated_at = self._clock()
            return job

    def refresh(self) -> list[dict]:
        """按 job 自带的**进度来源 / 终止条件**更新一次；返回发生变化的台账。"""
        changed: list[dict] = []
        for job in self.jobs():
            if job.terminal:
                continue
            now = self._clock()
            if callable(job.progress_source):
                try:
                    value = job.progress_source()
                except Exception as exc:  # noqa: BLE001 — 进度源坏了不拖垮台账
                    logger.debug("[JobRegistry] %s 进度源失败: %s", job.id, exc)
                    value = None
                if value is not None:
                    clamped = _clamp01(value, job.progress)
                    if clamped != job.progress:
                        job.progress = clamped
                        job.updated_at = now
                        changed.append({"job": job.id, "kind": "progress",
                                        "progress": clamped})
            if callable(job.terminal_condition):
                try:
                    verdict = job.terminal_condition()
                except Exception as exc:  # noqa: BLE001
                    logger.debug("[JobRegistry] %s 终止条件失败: %s", job.id, exc)
                    verdict = None
                if verdict:
                    self._settle(job, verdict)
                    changed.append({"job": job.id, "kind": "terminal",
                                    "status": job.status})
        return changed

    def _settle(self, job: Job, verdict: Any) -> None:
        """把终止条件的返回折算成终态：dict 走详细；True 当作成功完成。"""
        if isinstance(verdict, dict):
            status = str(verdict.get("status") or JOB_DONE)
            if status == JOB_FAILED:
                self.fail(job.id, code=str(verdict.get("code") or job.failure_code),
                          message=str(verdict.get("message") or ""),
                          result=verdict.get("result"))
                return
            if status == JOB_CANCELLED:
                self.cancel(job.id, reason=str(verdict.get("message") or "cancelled"))
                return
            self.finish(job.id, result=verdict.get("result"))
            return
        self.finish(job.id)

    def finish(self, job_id: str, *, result: Optional[dict] = None) -> Optional[Job]:
        return self._close(job_id, JOB_DONE, result=result)

    def fail(self, job_id: str, *, code: str = "", message: str = "",
             result: Optional[dict] = None) -> Optional[Job]:
        """失败必须带 code（诚实优先：失败原因不许静默）。"""
        payload = dict(result) if isinstance(result, dict) else {}
        if message:
            payload.setdefault("message", str(message))
        return self._close(job_id, JOB_FAILED, result=payload or None,
                           failure_code=str(code or "job_failed"))

    def cancel(self, job_id: str, *, reason: str = "") -> tuple[bool, str]:
        """走 job 自己的**取消路径**（真正的副作用在回调里），再标 cancelled。"""
        with self._lock:
            job = self._jobs.get(str(job_id))
            if job is None:
                return False, "not_found"
            if job.terminal:
                return False, "already_terminal"
        callback = job.cancel_path
        if callable(callback):
            try:
                callback(str(reason or "cancelled"))
            except TypeError:
                try:
                    callback()
                except Exception as exc:  # noqa: BLE001 — 取消回调坏掉不拖垮取消本身
                    logger.warning("[JobRegistry] 取消回调失败(%s): %s", job.id, exc)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[JobRegistry] 取消回调失败(%s): %s", job.id, exc)
        self._close(job.id, JOB_CANCELLED, result={"reason": str(reason or "cancelled")})
        logger.info("[JobRegistry] 取消 job %s（%s）：%s", job.id, job.tool, reason)
        return True, "cancelled"

    def _close(self, job_id: str, status: str, *, result: Optional[dict] = None,
               failure_code: str = "") -> Optional[Job]:
        with self._lock:
            job = self._jobs.get(str(job_id))
            if job is None:
                return None
            if job.terminal:
                return job
            now = self._clock()
            job.status = status
            job.result = result if result is not None else job.result
            if status == JOB_DONE:
                job.progress = 1.0
            if failure_code:
                job.failure_code = str(failure_code)
            job.updated_at = now
            job.finished_at = now
        self._unregister_governor(job)
        self._record(status, job_id=job.id, reason=str(job.failure_code or ""),
                     detail={"tool": job.tool, "status": status})
        return job

    def cancel_all(self, reason: str = "cancelled") -> int:
        """取消所有活跃 job（离场/停摆时用）。返回取消条数。"""
        count = 0
        for job in self.active():
            ok, _why = self.cancel(job.id, reason=reason)
            count += 1 if ok else 0
        return count

    # ── 快照 / 摘要 ──

    def snapshot(self) -> dict[str, Any]:
        rows = self.jobs()
        return {
            "jobs": [j.as_dict() for j in rows],
            "active": sum(1 for j in rows if not j.terminal),
            "count": len(rows),
            "events": len(self._events),
        }

    def events(self, n: int = 0) -> list[dict]:
        with self._lock:
            rows = list(self._events) if not n else list(self._events[-int(n):])
        return [dict(row, detail=dict(row["detail"])) for row in rows]

    def digest_lines(self, limit: int = 6) -> list[str]:
        """活跃 job 的紧凑行（供编排摘要用；**只进 L2**）。"""
        lines: list[str] = []
        for job in self.active()[:limit]:
            pct = int(round(_clamp01(job.progress) * 100))
            lines.append(f"{job.tool}#{job.id} {job.status} {pct}%")
        return lines


# ── 编排轮次（§10.2 #3）─────────────────────────────────────────

class OrchestrationTurns:
    """事件驱动的编排轮：**只在有事件且模型自己有活跃任务时**请模型回来。

    本类**不做任何模型调用**（那由 session 负责）——它只回答三个问题：
    "该不该请"（``should_run``）、"喂什么摘要"（``build_digest``）、"事件清了没"（``consume``）。
    没有事件 ⇒ ``should_run`` 恒 False ⇒ **系统安静时零额外模型调用** ✓。
    """

    def __init__(self, *, enabled_provider: Optional[Callable[[], Any]] = None,
                 clock: Callable[[], float] = time.time,
                 min_interval: float = ORCH_MIN_INTERVAL,
                 audit: Optional[Callable[[dict], None]] = None):
        self._enabled_provider = enabled_provider
        self._clock = clock
        self._min_interval = max(0.0, float(min_interval))
        self._audit_sink = audit
        self._lock = threading.RLock()
        self._events: list[dict] = []
        self._last_turn_at: float = 0.0

    # ── 开关 ──

    def enabled(self) -> bool:
        src = self._enabled_provider
        if src is None:
            return DEFAULT_ENABLED
        try:
            value = src() if callable(src) else src
        except Exception as exc:  # noqa: BLE001 — 配置读失败用默认
            logger.debug("[Orchestration] 开关读取失败: %s", exc)
            return DEFAULT_ENABLED
        if isinstance(value, bool):
            return value
        if value is None:
            return DEFAULT_ENABLED
        return bool(value)

    # ── 事件队列 ──

    def note_event(self, kind: str, detail: Optional[dict] = None) -> bool:
        """记一个编排事件（进度变化 / 出结果 / 卡住 / 玩家说话）。"""
        name = str(kind or "").strip()
        if name not in ORCH_EVENT_KINDS:
            return False
        with self._lock:
            self._events.append({
                "kind": name,
                "detail": dict(detail) if isinstance(detail, dict) else {},
                "at": self._clock(),
            })
            del self._events[:-20]
        return True

    def has_event(self) -> bool:
        with self._lock:
            return bool(self._events)

    def pending(self) -> list[dict]:
        with self._lock:
            return [dict(row, detail=dict(row["detail"])) for row in self._events]

    def within_interval(self) -> bool:
        with self._lock:
            last = self._last_turn_at
        return bool(last) and (self._clock() - last) < self._min_interval

    def should_run(self, model_created_active: bool) -> bool:
        """该不该请模型回来：开关开 + **模型自己**有活跃任务 + 有事件 + 过了最小间隔。"""
        if not self.enabled():
            return False
        if not model_created_active:
            return False
        if not self.has_event():
            return False
        if self.within_interval():
            return False
        return True

    def consume(self) -> list[dict]:
        """取走并清空事件（一次编排轮只喂一份摘要）。"""
        with self._lock:
            taken = self._events
            self._events = []
            self._last_turn_at = self._clock()
        rows = [dict(row, detail=dict(row["detail"])) for row in taken]
        if self._audit_sink is not None:
            try:
                self._audit_sink({"kind": "turn", "at": self._clock(),
                                  "events": [r["kind"] for r in rows]})
            except Exception as exc:  # noqa: BLE001
                logger.debug("[Orchestration] 审计 sink 失败: %s", exc)
        return rows

    def consume_kind(self, kind: str) -> int:
        """只取走某一类事件（如"玩家说话"已由当轮对话处理掉，别重复触发编排）。"""
        name = str(kind or "").strip()
        with self._lock:
            kept = [row for row in self._events if row.get("kind") != name]
            removed = len(self._events) - len(kept)
            self._events = kept
        return removed

    # ── 紧凑进展摘要（只进 L2）──

    def build_digest(self, *, tasks: Any = None, jobs: Any = None,
                     note: str = "", events: Optional[list[dict]] = None,
                     limit: int = 6) -> str:
        """活跃任务 + 活跃 job + 最近结果 + 卡住原因 —— 紧凑、可读、**只进当轮 user**。"""
        lines: list[str] = ["编排进展摘要（只作当轮判断，不是新指令）："]
        goal_lines = self._task_lines(tasks, limit=limit)
        lines.extend(goal_lines or ["- 没有活跃目标"])
        job_lines = []
        if jobs is not None:
            try:
                job_lines = list(jobs.digest_lines(limit=limit))
            except Exception as exc:  # noqa: BLE001 — 摘要失败不影响轮次
                logger.debug("[Orchestration] job 摘要失败: %s", exc)
        lines.append("- 活跃 job：" + ("、".join(job_lines) if job_lines else "无"))
        outcome_lines = self._outcome_lines(tasks)
        if outcome_lines:
            lines.append("- 最近结果：" + "；".join(outcome_lines))
        stuck = [str(e.get("detail", {}).get("reason") or "") for e in (events or [])
                 if e.get("kind") == ORCH_EVENT_STUCK]
        stuck = [s for s in stuck if s]
        if stuck:
            lines.append("- 卡住：" + "；".join(stuck))
        if note:
            lines.append(f"- 补充：{str(note).strip()}")
        return "\n".join(lines)

    @staticmethod
    def _task_lines(tasks: Any, *, limit: int = 6) -> list[str]:
        if tasks is None:
            return []
        try:
            items = tasks.items()
        except Exception:  # noqa: BLE001
            return []
        out: list[str] = []
        for item in items:
            status = str(getattr(item, "status", ""))
            if status in {"done", "failed", "cancelled"}:
                continue
            pct = int(round(_clamp01(getattr(item, "progress", 0.0)) * 100))
            detail = str(getattr(item, "detail", "") or "")
            out.append(f"{getattr(item, 'kind', '?')}#{getattr(item, 'id', '?')} "
                       f"{status} {pct}%" + (f"（{detail}）" if detail else ""))
            if len(out) >= limit:
                break
        return out

    @staticmethod
    def _outcome_lines(tasks: Any, *, limit: int = 4) -> list[str]:
        if tasks is None:
            return []
        try:
            items = tasks.items()
        except Exception:  # noqa: BLE001
            return []
        out: list[str] = []
        for item in items:
            status = str(getattr(item, "status", ""))
            if status not in {"done", "failed", "cancelled"}:
                continue
            reason = str(getattr(item, "failure_reason", "") or "")
            out.append(f"{getattr(item, 'kind', '?')} {status}" + (f"（{reason}）" if reason else ""))
            if len(out) >= limit:
                break
        return out


def has_model_created_active_task(task_list: Any) -> bool:
    """`task_list` 里有没有**模型自己创建的活跃任务**（编排轮次的唯一触发条件）。"""
    if task_list is None:
        return False
    try:
        items = task_list.items()
    except Exception:  # noqa: BLE001
        return False
    for item in items:
        if not getattr(item, "model_created", False):
            continue
        if str(getattr(item, "status", "")) not in {"done", "failed", "cancelled"}:
            return True
    return False
