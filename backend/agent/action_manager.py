"""
Action Manager — execution engine with timeout, resume, and loop detection.

Replicates mindcraft's action_manager.js pattern.
Each action runs with timeout, resume support, and loop detection.
"""

import time
import logging
from dataclasses import dataclass, field
from typing import Callable, Any, Optional

logger = logging.getLogger("action_manager")


@dataclass
class Action:
    label: str
    fn: Callable[[], Any]
    timeout: float = 600.0
    resume: bool = False
    interruptible: bool = True
    start_time: float = 0.0
    completed: bool = False
    interrupted: bool = False
    timed_out: bool = False
    result: Any = None
    error: Optional[str] = None
    req_id: Optional[str] = None
    dispatched: bool = False
    progress: float = 0.0
    progress_message: str = ""
    # False = completion comes from task_state terminal status (long-running
    # commands whose first wire response only acknowledges queueing)
    complete_on_response: bool = True

    @property
    def elapsed(self) -> float:
        return time.time() - self.start_time if self.start_time else 0.0


class ActionManager:
    """
    Manages action execution lifecycle.

    Features:
    - run_action: start a new action (stops current if interruptible)
    - stop: cancel current action immediately
    - cancel_resume: prevent resume-able action from re-queuing
    - tick: check timeouts and completion each cycle
    - loop detection: warns after 3 fast repeats, kills after 5
    """

    def __init__(self):
        self.current: Optional[Action] = None
        self._resume_queue: list[Action] = []
        self._history: list[tuple[str, float]] = []
        self._warnings = 0
        self._max_warnings = 3
        self._max_kills = 5
        self._loop_window = 5.0
        # 连续重复计数：同 label 反复下发而**中间一次都没成功完成** → 才算循环。
        # 这样"打架时每秒攻击一次"（每次都有回执成功）不会被误判成死循环。
        self._repeat_streak: dict[str, int] = {}
        # 退避：label → (解禁时间, 等级)。等级每升一级时长翻倍（2s/4s/8s…，上限 60s）。
        self._backoff: dict[str, tuple[float, int]] = {}
        self._backoff_base = 2.0
        self._backoff_max = 60.0
        # 上报给上层重新规划的循环失败（拉取式：`pop_loop_failure()`）。
        self.last_loop_failure: Optional[dict] = None
        # label → 最近一次**终态**结果（成功/失败/超时）。EatYield（进食闸门）等靠它
        # 判断"我刚下发的动作到底成没成"——`handle_response`/`complete_current`/超时都会写。
        self.last_outcome: dict[str, dict] = {}

    # ── Public API ──────────────────────────────────────────────

    def run_action(self, label: str, fn: Callable, **opts) -> bool:
        """Start executing an action. Returns True if started."""
        if self.current and not self.current.interruptible:
            logger.warning("[ActionMgr] Cannot interrupt non-interruptible: %s", self.current.label)
            return False

        if not self._admit(label):
            return False

        # Save current as resume if applicable
        if self.current and self.current.resume and not self.current.completed:
            self._resume_queue.append(self.current)

        self.stop()

        timeout = opts.get("timeout", 600.0)
        resume = opts.get("resume", False)
        interruptible = opts.get("interruptible", True)
        complete_on_response = opts.get("complete_on_response", True)

        self.current = Action(
            label=label, fn=fn, timeout=timeout,
            resume=resume, interruptible=interruptible,
            start_time=time.time(),
            complete_on_response=complete_on_response,
        )
        self._track_history(label)
        logger.info("[ActionMgr] Started: %s (timeout=%.1fs, resume=%s)", label, timeout, resume)
        return True

    def track(self, label: str, req_id: str, timeout: float = 600.0,
              complete_on_response: bool = True) -> bool:
        """Track an already-dispatched wire command by its request id.

        Unlike run_action(), the command was already sent to the mod — the
        request id is known now, so responses can never race the dispatch."""
        if self.current and not self.current.interruptible:
            logger.warning("[ActionMgr] Cannot interrupt non-interruptible: %s", self.current.label)
            return False

        # 退避准入先判：退避期内**连"更新跟踪"都不做**（下发本身由调用方负责，
        # 这里只表示"我不会再陪着这个 label 转圈了"）。
        if not self._admit(label):
            return False

        # 同一个动作**还在跟踪中**就又来一条 → 只把 req_id 更新到最新一次（不重建动作、
        # 不重复占位、不再打一条 "Stopped + Tracking"）。同时照样计入"没成功完成的重复"，
        # 所以"同一个动作永远完不成、每秒重发"依然会被判成循环、照样退避。
        cur = self.current
        if cur and not cur.completed and cur.label == label:
            cur.req_id = req_id
            self._count_repeat(label)
            logger.debug("[ActionMgr] %s 已在跟踪，本次只更新 req=%s", label, req_id)
            return True

        if self.current and self.current.resume and not self.current.completed:
            self._resume_queue.append(self.current)

        self.stop()
        self.current = Action(
            label=label, fn=None, timeout=timeout,
            start_time=time.time(),
            complete_on_response=complete_on_response,
            req_id=req_id, dispatched=True,
        )
        self._track_history(label)
        logger.info("[ActionMgr] Tracking: %s (req=%s, timeout=%.1fs)", label, req_id, timeout)
        return True

    def is_backed_off(self, label: str) -> bool:
        """该 label 是否还在退避窗口内（模式层下发前查这个，被禁则别发）。"""
        until, _level = self._backoff.get(label, (0.0, 0))
        return time.time() < until

    def _admit(self, label: str) -> bool:
        """退避准入：该 label 还在退避窗口内 → 拒绝下发（返回 False）。"""
        if self.is_backed_off(label):
            until, _level = self._backoff[label]
            logger.debug("[ActionMgr] %s 正在退避（%.1fs 后解禁），本次拒绝", label, until - time.time())
            return False
        return True

    # ── Outcome bookkeeping（模式层用）──────────────────────────

    def outcome_since(self, label: str, since: float) -> Optional[dict]:
        """该 label 在 `since` 之后是否已有终态结果；没有则 None。"""
        record = self.last_outcome.get(label)
        if record and record.get("at", 0.0) >= since:
            return record
        return None

    def _record_outcome(self, label: str, success: bool, error: str = "") -> None:
        self.last_outcome[label] = {
            "success": bool(success),
            "error": error or "",
            "at": time.time(),
        }

    def stop(self):
        """Stop current action immediately."""
        cur = self.current
        if cur and not cur.completed:
            cur.interrupted = True
            logger.info("[ActionMgr] Stopped: %s (elapsed=%.1fs)", cur.label, cur.elapsed)
        self.current = None

    def cancel_resume(self):
        """Prevent all resume-able actions from firing."""
        self._resume_queue.clear()
        logger.debug("[ActionMgr] Resume queue cleared")

    def tick(self):
        """
        Called every event loop cycle.
        Checks timeout and auto-completion.
        """
        cur = self.current
        if not cur or cur.completed:
            return

        # Check timeout
        if cur.elapsed > cur.timeout:
            logger.warning("[ActionMgr] Timeout: %s (%.1fs > %.1fs)", cur.label, cur.elapsed, cur.timeout)
            cur.timed_out = True
            self._record_outcome(cur.label, False, f"timeout after {cur.timeout:.0f}s")
            self.current = None
            return

        # Execute the action function
        try:
            if not cur.dispatched:
                result = cur.fn()
                cur.dispatched = True
                cur.result = result

                if isinstance(result, dict) and result.get("req_id"):
                    cur.req_id = str(result["req_id"])
                    logger.info("[ActionMgr] Dispatched: %s (req=%s)", cur.label, cur.req_id)
                    return

                if result is not None:
                    cur.completed = True
                    logger.info("[ActionMgr] Completed immediately: %s", cur.label)
                    self.current = None
        except Exception as e:
            cur.error = str(e)
            cur.interrupted = True
            self._record_outcome(cur.label, False, str(e))
            logger.error("[ActionMgr] Error in %s: %s", cur.label, e)
            self.current = None

    def handle_response(self, req_id: str, success: bool, detail: str = ""):
        """Handle a response from the Java mod for a pending action.

        `detail` = 模组回执里的原因（命令名在同一处由 session 记下并落日志；这里只把
        "为什么失败"带进在途动作的终态里，免得只留一个 `command failed: req_x`）。
        """
        cur = self.current
        if cur and cur.req_id == req_id:
            logger.debug("[ActionMgr] Response for %s: req=%s success=%s%s", cur.label, req_id,
                         success, f" detail={detail}" if detail else "")
            if success and not cur.complete_on_response:
                # Long-running command: this response only queued the task.
                # Real completion arrives via task_state → complete_current().
                cur.result = {"queued": True}
                return
            if success:
                # 成功完成 = 这个动作不是死循环 → 清掉重复计数（打架时每秒攻击一次
                # 每次都有回执，正是靠这一条不会被误判）。
                self._repeat_streak.pop(cur.label, None)
            cur.completed = success
            if not success:
                cur.error = detail or f"command failed: {req_id}"
                cur.interrupted = True
            self._record_outcome(cur.label, success, cur.error or "")
            self.current = None

    def complete_current(self, success: bool, detail: str = ""):
        """Complete the current action from a task_state terminal signal."""
        cur = self.current
        if not cur or cur.completed:
            return
        logger.info("[ActionMgr] Task %s finished: success=%s (%s)", cur.label, success, detail)
        cur.completed = True
        cur.result = {"success": success, "detail": detail}
        if success:
            self._repeat_streak.pop(cur.label, None)
        else:
            cur.error = detail or "task failed"
        self._record_outcome(cur.label, success, cur.error or "")
        self.current = None

    def handle_progress(self, req_id: str, progress: float, message: str = ""):
        cur = self.current
        if cur and cur.req_id == req_id:
            cur.progress = progress
            cur.progress_message = message

    def pop_resume(self) -> Optional[Action]:
        """Return next resume-able action if idle, or None."""
        if self.is_busy:
            return None
        return self._resume_queue.pop(0) if self._resume_queue else None

    # ── Status ──────────────────────────────────────────────────

    @property
    def is_busy(self) -> bool:
        cur = self.current
        return cur is not None and not cur.completed

    @property
    def current_label(self) -> Optional[str]:
        cur = self.current
        return cur.label if cur else None

    def get_status(self) -> dict:
        cur = self.current
        base = {
            "loop_warnings": self._warnings,
            "backoff": {label: round(until - time.time(), 1)
                        for label, (until, _level) in self._backoff.items()
                        if until > time.time()},
            "last_loop_failure": self.last_loop_failure,
        }
        if cur:
            base.update({
                "label": cur.label,
                "elapsed": round(cur.elapsed, 1),
                "timeout": cur.timeout,
                "completed": cur.completed,
                "interrupted": cur.interrupted,
                "timed_out": cur.timed_out,
                "req_id": cur.req_id,
                "progress": cur.progress,
                "progress_message": cur.progress_message,
            })
            return base
        base["label"] = None
        base["resume_queue"] = len(self._resume_queue)
        return base

    # ── Loop Detection ──────────────────────────────────────────
    #
    # 循环的定义（用户实机日志取证后收敛）：**同一个动作被反复下发，而中间一次都没有
    # 成功完成**。这既覆盖了原来"5 秒内重复 N 次"想抓的抖动，又不会把"打架时每秒攻击
    # 一次、每次都有回执"误判成死循环。
    #
    # 触发后的行为（原实现直接 raise RuntimeError，而它发生在 skills 的命令观察者里，
    # 那里 catch 后只打 debug 日志 → 异常被吞掉、"KILLING" 只剩一行日志）：
    #   1) 先指数退避：同 label 在 2s/4s/8s…（上限 60s）内拒绝下发，日志只打一次；
    #   2) 到 kill 阈值：把当前任务标 failed 并记一条上报（`pop_loop_failure()`），
    #      交给上层重新规划 —— 不再无声抛异常。

    def _track_history(self, label: str):
        now = time.time()
        self._history.append((label, now))
        self._history = [(l, t) for l, t in self._history if now - t < self._loop_window]
        self._count_repeat(label)

    def _count_repeat(self, label: str) -> bool:
        """记一次"同 label 且上次没成功完成"的下发；返回是否已被判失败。"""
        count = self._repeat_streak.get(label, 0) + 1
        self._repeat_streak[label] = count
        if count >= self._max_kills:
            self._report_loop(label, count, kill=True)
            return True
        if count >= self._max_warnings:
            self._warnings += 1
            self._backoff_label(label, count)
        return False

    def _backoff_label(self, label: str, count: int, log: bool = True) -> float:
        """给该 label 加一级退避，返回本次时长（秒）。只在升级时打一条日志。"""
        _until, level = self._backoff.get(label, (0.0, 0))
        level += 1
        delay = min(self._backoff_base * (2 ** (level - 1)), self._backoff_max)
        self._backoff[label] = (time.time() + delay, level)
        if log:
            logger.warning("[ActionMgr] Loop warning #%d: %s 连续 %d 次没有成功完成 → 退避 %.0fs",
                           self._warnings, label, count, delay)
        return delay

    def pop_loop_failure(self) -> Optional[dict]:
        """取走一条"因循环被判失败"的上报（供上层重新规划），没有则 None。"""
        report = self.last_loop_failure
        self.last_loop_failure = None
        return report

    def _report_loop(self, label: str, count: int, kill: bool) -> None:
        # 退避照旧升级，但日志由下面那一行 ERROR 统一承担（避免一次循环打两行）。
        delay = self._backoff_label(label, count, log=False)
        self.last_loop_failure = {
            "label": label,
            "count": count,
            "at": time.time(),
            "backoff": delay,
        }
        cur = self.current
        if kill and cur is not None and cur.label == label and not cur.completed:
            cur.completed = True
            cur.interrupted = True
            cur.error = f"loop detected: {label} x{count}"
            self._record_outcome(label, False, cur.error)
            self.current = None
        logger.error("[ActionMgr] LOOP: %s 连续 %d 次没有成功完成 → 已标失败并上报（退避 %.0fs）",
                     label, count, delay)
