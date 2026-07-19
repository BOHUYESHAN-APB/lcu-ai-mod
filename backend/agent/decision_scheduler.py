"""Asynchronous, proposal-only model decisions for semantic world boundaries."""

from __future__ import annotations

import copy
import json
import threading
import time
import uuid
from concurrent.futures import Future
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class DecisionProposal:
    decision: str
    skill_id: str | None
    input: dict[str, Any]
    reason: str

    def public_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision,
            "skill_id": self.skill_id,
            "input": copy.deepcopy(self.input),
            "reason": self.reason,
        }


@dataclass(frozen=True)
class DecisionResult:
    request_id: str
    through_sequence: int
    submitted_at: float
    completed_at: float
    scope_id: str
    body_epoch: int
    observation_revision: int
    proposal: DecisionProposal | None = None
    error: str = ""

    def public_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "through_sequence": self.through_sequence,
            "submitted_at": self.submitted_at,
            "completed_at": self.completed_at,
            "scope_id": self.scope_id,
            "body_epoch": self.body_epoch,
            "observation_revision": self.observation_revision,
            "proposal": self.proposal.public_dict() if self.proposal else None,
            "error": self.error,
        }


class DecisionScheduler:
    def __init__(self, llm, *, proposal_ttl_seconds: float = 30.0, max_attempts: int = 3,
                 executor=None):
        self.llm = llm
        self.proposal_ttl_seconds = max(1.0, float(proposal_ttl_seconds))
        self.max_attempts = max(1, int(max_attempts))
        self._executor = executor or _DaemonExecutor()
        self._owns_executor = executor is None
        self._lock = threading.RLock()
        self._future: Future | None = None
        self._request: dict[str, Any] | None = None
        self._result: DecisionResult | None = None
        self._history: list[dict[str, Any]] = []
        self._closed = False
        self._generation = 0
        self._retry_not_before = 0.0
        self._failure_counts: dict[int, int] = {}

    def submit(self, triggers: list[dict[str, Any]], context: dict[str, Any], *,
               scope_id: str, body_epoch: int, observation_revision: int,
               submitted_at: float | None = None) -> bool:
        if not triggers:
            return False
        with self._lock:
            now = time.time() if submitted_at is None else float(submitted_at)
            if self._closed or now < self._retry_not_before or self._future is not None or self._result is not None:
                return False
            retry_sequences = sorted(
                sequence for sequence, attempts in self._failure_counts.items()
                if attempts < self.max_attempts
            )
            selected = triggers
            if retry_sequences:
                retry_through = retry_sequences[0]
                selected = [item for item in triggers if int(item["sequence"]) <= retry_through]
                if not selected:
                    self._failure_counts.pop(retry_through, None)
                    return False
            request = {
                "id": str(uuid.uuid4()),
                "through_sequence": max(int(item["sequence"]) for item in selected),
                "submitted_at": now,
                "triggers": copy.deepcopy(selected),
                "context": copy.deepcopy(context),
                "scope_id": str(scope_id),
                "body_epoch": int(body_epoch),
                "observation_revision": int(observation_revision),
                "generation": self._generation,
            }
            self._request = request
            self._future = self._executor.submit(self._run_decision, request)
            return True

    def poll(self, *, completed_at: float | None = None) -> DecisionResult | None:
        with self._lock:
            if self._result is not None:
                return self._result
            if self._future is None or not self._future.done() or self._request is None:
                return None
            proposal, error, finished_at = self._future.result()
            if self._request.get("invalidated"):
                self._future = None
                self._request = None
                return None
            result = DecisionResult(
                self._request["id"], self._request["through_sequence"],
                self._request["submitted_at"], finished_at, self._request["scope_id"],
                self._request["body_epoch"], self._request["observation_revision"],
                proposal=proposal, error=error,
            )
            self._future = None
            self._request = None
            self._result = result
            return result

    def resolve(self, disposition: str, *, run_id: str | None = None,
                detail: str = "", resolved_at: float | None = None) -> dict[str, Any]:
        with self._lock:
            if self._result is None:
                raise ValueError("no decision result is pending")
            record = {
                **self._result.public_dict(),
                "disposition": str(disposition),
                "run_id": run_id,
                "detail": str(detail)[:500],
                "resolved_at": time.time() if resolved_at is None else float(resolved_at),
            }
            if disposition == "failed":
                attempts = self._failure_counts.get(self._result.through_sequence, 0) + 1
                self._failure_counts[self._result.through_sequence] = attempts
                record["attempts"] = attempts
                record["retryable"] = attempts < self.max_attempts
                if record["retryable"]:
                    self._retry_not_before = record["resolved_at"] + min(30.0, 2.0 ** attempts)
                else:
                    self._failure_counts.pop(self._result.through_sequence, None)
            else:
                self._failure_counts.pop(self._result.through_sequence, None)
                record["retryable"] = False
            self._history.append(record)
            self._history = self._history[-50:]
            self._result = None
            return copy.deepcopy(record)

    def is_expired(self, result: DecisionResult, *, now: float | None = None) -> bool:
        current = time.time() if now is None else float(now)
        return current - result.completed_at > self.proposal_ttl_seconds

    def get_status(self) -> dict[str, Any]:
        with self._lock:
            state = "proposal" if self._result is not None \
                else "invalidated" if self._request and self._request.get("invalidated") \
                else "thinking" if self._future is not None else "idle"
            return {
                "state": state,
                "request_id": self._request["id"] if self._request else self._result.request_id if self._result else None,
                "through_sequence": self._request["through_sequence"] if self._request else self._result.through_sequence if self._result else None,
                "proposal": self._result.proposal.public_dict() if self._result and self._result.proposal else None,
                "error": self._result.error if self._result else "",
                "history": copy.deepcopy(self._history[-10:]),
                "closed": self._closed,
                "retry_not_before": self._retry_not_before or None,
            }

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._generation += 1
            self._future = None
            self._request = None
            self._result = None
        if self._owns_executor:
            self._executor.shutdown(cancel_futures=True)

    def invalidate(self, detail: str) -> dict[str, Any] | None:
        with self._lock:
            if self._future is None and self._result is None:
                return None
            request_id = self._request["id"] if self._request else self._result.request_id
            through_sequence = self._request["through_sequence"] if self._request else self._result.through_sequence
            record = {
                "request_id": request_id,
                "through_sequence": through_sequence,
                "disposition": "invalidated",
                "detail": str(detail)[:500],
                "resolved_at": time.time(),
            }
            if self._request:
                record.update({
                    "scope_id": self._request["scope_id"],
                    "body_epoch": self._request["body_epoch"],
                    "observation_revision": self._request["observation_revision"],
                })
            elif self._result:
                record.update({
                    "scope_id": self._result.scope_id,
                    "body_epoch": self._result.body_epoch,
                    "observation_revision": self._result.observation_revision,
                })
            self._history.append(record)
            self._history = self._history[-50:]
            self._generation += 1
            if self._request is not None:
                self._request["invalidated"] = True
            else:
                self._result = None
            return copy.deepcopy(record)

    def _run_decision(self, request: dict[str, Any]) -> tuple[DecisionProposal | None, str, float]:
        try:
            return self._decide(request), "", time.time()
        except Exception as exc:
            return None, str(exc)[:500], time.time()

    def _decide(self, request: dict[str, Any]) -> DecisionProposal:
        system = (
            "You are a Minecraft companion decision gate. Return exactly one JSON object and no prose. "
            'Use {"decision":"none","reason":"..."} when no immediate safe action is necessary. '
            'Otherwise use {"decision":"run_skill","skill_id":"...","input":{},"reason":"..."}. '
            "Never emit combat, mining, crafting, navigation, inventory transfer, chat, or UI actions. "
            "The executor independently validates and may reject every proposal."
        )
        payload = {
            "decision_boundaries": request["triggers"],
            "context": request["context"],
            "automatic_skill_allowlist": ["general.eat"],
        }
        response = self.llm.chat([
            {"role": "system", "content": system, "required": True},
            {
                "role": "user",
                "content": json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str),
                "required": True,
            },
        ], agent="decision_scheduler", max_output_tokens=512)
        return self.parse_proposal(str(response.get("content", "")))

    @staticmethod
    def parse_proposal(text: str) -> DecisionProposal:
        content = str(text).strip()
        if content.startswith("```"):
            lines = content.splitlines()
            if len(lines) >= 3 and lines[-1].strip() == "```":
                content = "\n".join(lines[1:-1]).strip()
        try:
            payload = json.loads(content)
        except json.JSONDecodeError as exc:
            raise ValueError("decision response is not valid JSON") from exc
        if not isinstance(payload, dict):
            raise ValueError("decision response must be an object")
        decision = str(payload.get("decision", "")).strip().lower()
        reason = str(payload.get("reason", "")).strip()[:500]
        if decision == "none":
            return DecisionProposal("none", None, {}, reason)
        if decision != "run_skill":
            raise ValueError("decision must be none or run_skill")
        skill_id = payload.get("skill_id")
        input_data = payload.get("input", {})
        if not isinstance(skill_id, str) or not skill_id.strip():
            raise ValueError("run_skill decision requires skill_id")
        if not isinstance(input_data, dict):
            raise ValueError("run_skill input must be an object")
        return DecisionProposal("run_skill", skill_id.strip(), copy.deepcopy(input_data), reason)


class _DaemonExecutor:
    """Single-purpose executor whose blocked provider request cannot hold process shutdown."""

    def __init__(self):
        self._closed = False
        self._lock = threading.Lock()

    def submit(self, fn, *args, **kwargs) -> Future:
        with self._lock:
            if self._closed:
                raise RuntimeError("decision executor is closed")
            future: Future = Future()

            def run() -> None:
                if not future.set_running_or_notify_cancel():
                    return
                try:
                    future.set_result(fn(*args, **kwargs))
                except BaseException as exc:
                    future.set_exception(exc)

            threading.Thread(target=run, name="lcu-decision", daemon=True).start()
            return future

    def shutdown(self, cancel_futures: bool = True) -> None:
        with self._lock:
            self._closed = True
