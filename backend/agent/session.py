"""
Session — 核心管理单元。
集中管理一个 AI 实例的所有状态、任务、动作、模式和记忆。

架构:
  Session (集中管理器)
  ├── action_manager: ActionManager (动作执行 + 超时/恢复/循环检测)
  ├── modes_engine: ModesEngine (优先级行为模式循环)
  ├── self_prompter: SelfPrompter (空闲时自动触发 LLM 自我提示)
  ├── memory: Memory (对话历史 + 持久化统计)
  ├── commands: Commands (!command 解析器)
  ├── skills: Skills (Python 技能层 → 发命令到 mod)
  ├── llm: LLMService (Token Plan API)
  ├── task_queue: list[dict] (Python 端任务队列)
  └── runtime: dict (玩家/世界/背包快照)
"""

import asyncio
from collections import deque
import logging
import re
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from protocol import BodyAdapter
from .access_policy import classify_skill, evaluate as evaluate_access, load_policy
from .config_store import DEFAULT_CONFIG_PATH
from .action_manager import ActionManager
from .memory import Memory
from .memory_overlay import MemoryOverlayStore
from .skills import Skills
from .modes_engine import ModesEngine
from .commands import Commands
from .llm_service import LLMService
from .self_prompter import SelfPrompter
from .message_db import MessageDB
from .identity import CompanionIdentity, DEFAULT_LEGACY_ROOT, DEFAULT_STORAGE_ROOT, migrate_legacy_sessions
from .planner import SkillProposal
from .world_model import WorldModel

logger = logging.getLogger("session")


class Session:
    """
    Central session that manages an AI instance.
    """

    def __init__(self, body: BodyAdapter, session_id: str | None = None, *,
                 companion_id: str = "default", persistence_scope: str = "global",
                 server_id: str = "default", world_id: str = "default",
                 storage_root: Path = DEFAULT_STORAGE_ROOT, legacy_root: Path | None = DEFAULT_LEGACY_ROOT):
        self.id = session_id or str(uuid.uuid4())[:8]
        self.body = body
        self.identity = CompanionIdentity(companion_id, persistence_scope, server_id, world_id)
        self.storage_dir = self.identity.storage_dir(storage_root)
        if legacy_root is not None and persistence_scope == "global":
            migrate_legacy_sessions(self.storage_dir, legacy_root)
        self.storage_dir.mkdir(parents=True, exist_ok=True)

        # Core subsystems
        self.action_manager = ActionManager()
        self.skills = Skills(body)
        self.skills.set_command_observer(self._on_skill_command)
        self.memory = Memory(
            path=self.storage_dir / "memory.json",
            server_id=self.identity.server_id,
            world_id=self.identity.world_id,
        )
        self.commands = Commands(self.skills)
        self.modes_engine = ModesEngine(self.skills, self.memory)
        self.llm = LLMService()

        # Message database (SQLite persistence)
        self.message_db = MessageDB(db_path=self.storage_dir / "messages.db")
        self.memory_overlay = MemoryOverlayStore(self.storage_dir / "memory_management.db")

        # Register default modes
        self.modes_engine.add_defaults()

        # Timing Gate (MaiBot-style rhythm control)
        from .timing_gate import TimingGate
        self.timing_gate = TimingGate(self.llm)

        # Planner (MaiBot-style two-stage reasoning)
        from .planner import Planner
        self.planner = Planner(self.llm, self.memory, self.skills)

        # State snapshot
        self.runtime: dict = {
            "player": {},
            "world": {},
            "inventory": [],
            "entities": [],
            "nearby_blocks": [],
        }
        self.world_model = WorldModel(self.runtime)

        # Task queue
        self.task_queue: list[dict] = []

        # Self-prompter (mindcraft-style autonomous prompting)
        self.self_prompter = SelfPrompter(cooldown=15.0, max_idle_cycles=3)

        self._manual_command_until = 0.0
        self._manual_action_reqs: set[str] = set()
        self._manual_task_kind: Optional[str] = None
        self._stopped = False
        self._current_requester: tuple[str, str] | None = None
        self._current_request_channel: str | None = None
        self._planner_proposal_dispatcher = None
        self._pending_commands: dict[str, dict] = {}
        self.control_mode = "builtin"
        self.control_fencing_token = 0
        self._external_task_busy = False
        self._body_connected = False
        self._last_state_at: float | None = None
        self._state_sequence = 0
        # 1G working context: process-local and intentionally not persisted.
        self._working_context: deque[dict[str, Any]] = deque(maxlen=24)
        self._pending_chat_actions: deque[dict[str, Any]] = deque(maxlen=20)

    # ── Event handlers ──

    def handle_event(self, event_type: str, data: dict):
        """Route an event from the mod."""
        match event_type:
            case "state_update":
                world_model = self._ensure_world_model()
                observed_at = time.time()
                world_model.ingest_snapshot(data, observed_at=observed_at)
                self.runtime = world_model.legacy_projection(self.runtime)
                self._last_state_at = observed_at
                self._state_sequence = getattr(self, "_state_sequence", 0) + 1
                if hasattr(self, "memory") and self.control_mode != "external":
                    self.memory.observe_world(self.runtime)
            case "player_chat":
                if self.control_mode == "external":
                    return
                sender = data.get("sender", "?")
                message = data.get("message", "")
                is_system = data.get("is_system", False)
                sender_id = str(data.get("uuid", ""))

                # Chat permission check (whitelist)
                if not self._check_chat_permission(sender, is_system, sender_id):
                    logger.debug("[Chat] Ignored: %s (not in whitelist)", sender)
                    return

                logger.info("[Chat] %s: %s", sender, message[:80])
                
                # Save to database
                self.message_db.add_message(
                    sender=sender,
                    message=message,
                    is_system=is_system,
                    is_ai=False
                )
                
                self.memory.add_interaction(
                    sender=sender,
                    message=message,
                    response=data.get("response", ""),
                )
                self.memory.observe_player(sender, sender_id, message)

                if data.pop("_lcu_priority_stop_admitted", False):
                    with self.skills.command_context("chat_reply"):
                        self.skills.send_chat("停下了")
                    return

                if self._external_task_busy:
                    if self.is_stop_intent(message) and self.check_chat_skill_permission(
                        sender, is_system, sender_id, "safety.stop"
                    ) and self.dispatch_stop_intent():
                        with self.skills.command_context("chat_reply"):
                            self.skills.send_chat("停下了")
                    return

                # Timing Gate: 判断是否应该回复（参考 MaiBot）
                should_respond, reason = self.timing_gate.should_respond(
                    sender, message, 
                    recent_messages=self.memory.get_recent_context(10),
                    bot_name=self._get_bot_name(),
                    wake_names=self._get_wake_names()
                )
                
                if not should_respond:
                    logger.info("[Chat] Timing Gate: 不回复（%s）", reason)
                    return
                
                logger.info("[Chat] Timing Gate: 回复（%s）", reason)

                planner_context = "chat"
                if reason.startswith("direct_command") or reason.startswith("mentioned") or reason == "private_chat":
                    self._manual_command_until = time.time() + 20.0
                    planner_context = "manual_chat"
                
                # 记录回复
                self.timing_gate.record_reply()
                
                # 使用 Planner 规划回复
                try:
                    response = self.handle_chat(
                        sender,
                        message,
                        command_context=planner_context,
                        sender_id=sender_id,
                        requester_channel="chat.public",
                        record_interaction=False,
                    )
                    if response:
                        logger.info("[Chat] AI response: %s", response[:80])
                        # Send response as chat message
                        with self.skills.command_context("chat_reply"):
                            self.skills.send_chat(response)
                        # Save AI response to database
                        self.message_db.add_message(
                            sender="AI",
                            message=response,
                            is_system=False,
                            is_ai=True
                        )
                        # 记录到记忆
                        self.memory.add_interaction(
                            sender="AI", message=response, response=""
                        )
                except Exception as e:
                    logger.error("[Chat] LLM error: %s", e)
            case "chat_clicks":
                if self.control_mode == "external":
                    return
                now = time.time()
                for index, action in enumerate(data.get("actions", [])):
                    if not isinstance(action, dict):
                        continue
                    self._pending_chat_actions.append({
                        "id": f"chat-action-{int(now * 1000)}-{index}",
                        "message": str(data.get("message", "")),
                        "action": str(action.get("action", "")),
                        "value": str(action.get("value", "")),
                        "text": str(action.get("text", "")),
                        "created_at": now,
                    })
                self.runtime["chat_actions"] = list(self._pending_chat_actions)
            case "player_death":
                logger.info("[Session] Player died at %s", data.get("position"))
                if self.control_mode != "external":
                    self.memory.add_interaction("system", "Player died", action="death")
                    self.memory.record_death("玩家死亡")
                    self.message_db.add_event("death", "玩家死亡")
            case "command_response":
                req_id = data.get("id", "?")
                success = data.get("success", False)
                self.action_manager.handle_response(req_id, success)
                pending = self._pending_commands.get(req_id)
                if pending and (not success or pending["command"] not in self._deferred_terminal_commands()):
                    self._finish_pending_command(req_id, "success" if success else "failed", str(data.get("error", "")))
                if req_id in self._manual_action_reqs:
                    if success and self._manual_task_kind in {"follow_player", "craft_item", "eat", "collect_blocks", "move_to", "explore", "build"}:
                        self._manual_command_until = max(self._manual_command_until, time.time() + 30.0)
                    elif not success:
                        self._manual_action_reqs.discard(req_id)
                        if not self._manual_action_reqs:
                            self._manual_task_kind = None
            case "command_progress":
                req_id = data.get("id", "?")
                progress = float(data.get("progress", 0.0) or 0.0)
                message = data.get("message", "")
                self.action_manager.handle_progress(req_id, progress, message)
                pending = self._pending_commands.get(req_id)
                waits_for_outcome = bool(pending and pending["command"] in self._outcome_commands())
                if progress >= 1.0 and not waits_for_outcome:
                    self._finish_pending_command(req_id, "success", message)
                elif progress <= 0.0 and message and not waits_for_outcome:
                    self._finish_pending_command(req_id, "failed", message)
                if req_id in self._manual_action_reqs:
                    self._manual_command_until = max(self._manual_command_until, time.time() + 15.0)
                    if progress <= 0.0 or progress >= 1.0:
                        self._manual_action_reqs.discard(req_id)
            case "command_outcome":
                req_id = data.get("id", "?")
                status = str(data.get("status", "failed"))
                message = str(data.get("message", data.get("code", "")))
                outcome = {
                    "succeeded": "success",
                    "failed": "failed",
                    "cancelled": "cancelled",
                }.get(status, "unknown")
                self._finish_pending_command(req_id, outcome, message)
                self._manual_action_reqs.discard(req_id)
            case "behavior_state":
                self._set_world_overlay("behavior_state", data)
                if self._manual_behavior_active():
                    self._manual_command_until = max(self._manual_command_until, time.time() + 8.0)
                elif not self._manual_action_reqs:
                    self._manual_task_kind = None
            case "task_state":
                self._set_world_overlay("task_state", data)
                if self._manual_behavior_active():
                    self._manual_command_until = max(self._manual_command_until, time.time() + 8.0)
            case "command_interrupted":
                reason = str(data.get("reason", "interrupted"))
                for req_id in list(self._pending_commands):
                    self._finish_pending_command(req_id, "cancelled", reason)
            case "control_state":
                self._set_world_overlay("control_state", data)
            case _:
                logger.debug("[Session] Unhandled event: %s", event_type)

    # ── LLM-driven conversation ──

    def _get_bot_name(self) -> str:
        """获取 AI 的名字。"""
        persona = self.runtime.get("persona", {})
        if isinstance(persona, dict) and persona.get("name"):
            return str(persona["name"])
        player = self.runtime.get("player", {})
        return player.get("name", "AI")

    def _get_wake_names(self) -> list[str]:
        persona = self.runtime.get("persona", {})
        bot_name = self._get_bot_name()
        if isinstance(persona, dict):
            wake_names = persona.get("wake_names") or []
            if isinstance(wake_names, list):
                merged = [str(name).strip() for name in wake_names if str(name).strip()]
                if bot_name not in merged:
                    merged.append(bot_name)
                return merged
        return [bot_name]

    def _check_chat_permission(self, sender: str, is_system: bool, sender_id: str = "") -> bool:
        """Check if a chat sender is allowed to talk to this AI."""
        return self.check_chat_skill_permission(sender, is_system, sender_id, "chat.reply")

    def check_chat_skill_permission(self, sender: str, is_system: bool, sender_id: str, skill: str) -> bool:
        decision = evaluate_access(
            load_policy(DEFAULT_CONFIG_PATH),
            {"name": sender, "uuid": sender_id},
            channel="chat.system" if is_system else "chat.public",
            skill=skill,
            server_id=self.identity.server_id,
            body_id=self.identity.companion_id,
        )
        return bool(decision["allowed"])

    def handle_chat(self, sender: str, message: str, command_context: str = "chat",
                    sender_id: str = "", requester_channel: str | None = None,
                    record_interaction: bool = True) -> Optional[str]:
        """
        Process a chat message through the LLM.
        Uses Planner for intelligent action planning.
        """
        if self.control_mode == "external":
            return None
        if self._external_task_busy:
            return "停下了" if self.is_stop_intent(message) and self.dispatch_stop_intent() else None
        if record_interaction:
            self.memory.add_interaction(sender=sender, message=message)
            self.memory.observe_player(sender, sender_id, message)
            self.message_db.add_message(sender=sender, message=message, is_ai=False)
        self._working_context.append({
            "role": "user", "sender": sender, "player_id": sender_id,
            "content": message, "at": time.time(),
        })

        context = self.build_planner_context(current_player=sender, current_player_id=sender_id)
        
        # Use Planner to plan and execute
        previous_requester = self._current_requester
        previous_channel = self._current_request_channel
        self._current_requester = (sender, sender_id)
        self._current_request_channel = requester_channel
        try:
            with self.skills.command_context(command_context):
                response = self.planner.plan_and_execute(
                    sender, message, context, bot_name=self._get_bot_name()
                )
        finally:
            self._current_requester = previous_requester
            self._current_request_channel = previous_channel
        
        if response and record_interaction:
            self.memory.attach_response(sender, message, response)
            self.message_db.add_message(sender="AI", message=response, is_ai=True)
        if response:
            self._working_context.append({"role": "assistant", "content": response, "at": time.time()})
        
        return response

    # ── Task management ──

    def enqueue_task(self, task_def: dict):
        self.task_queue.append(task_def)
        logger.debug("[Session] Task queued: %s", task_def.get("name", "?"))

    def tick_tasks(self):
        """Tick task queue. Tasks are executed one at a time."""
        if not self.task_queue:
            return
        task = self.task_queue[0]
        if task.get("done"):
            self.task_queue.pop(0) if self.task_queue else None
            return

    # ── Lifecycle ──

    def tick(self, external_task_busy: bool = False):
        """Called every event loop cycle."""
        self._external_task_busy = external_task_busy
        # Action manager tick (check timeouts, process completions)
        self.action_manager.tick()

        # Check for resume actions
        if not self.action_manager.is_busy:
            resume_action = self.action_manager.pop_resume()
            if resume_action:
                self.action_manager.run_action(
                    resume_action.label, resume_action.fn,
                    timeout=resume_action.timeout, resume=resume_action.resume
                )

        # Determine if we're in idle state (no active mode, no task, no busy action)
        active_mode = self.modes_engine._active_mode
        task_state = self.runtime.get("task_state", {}) if isinstance(self.runtime.get("task_state"), dict) else {}
        task_active = external_task_busy or bool(task_state) and str(task_state.get("kind", "idle")).strip().lower() != "idle" \
            and str(task_state.get("status", "idle")).strip().lower() not in {"", "idle", "done", "failed", "cancelled"}
        is_idle = (active_mode is None
                   and not self.task_queue
                   and not self.action_manager.is_busy
                   and not task_active)

        # Modes engine (priority behaviors that don't need LLM)
        behavior_state = self.runtime.get("behavior_state", {}) if isinstance(self.runtime.get("behavior_state"), dict) else {}
        control_state = self.runtime.get("control_state", {}) if isinstance(self.runtime.get("control_state"), dict) else {}
        autonomy_enabled = self.control_mode != "external" \
            and control_state.get("ai_controlled") is True \
            and behavior_state.get("behaviors_enabled") is True
        manual_override_active = time.time() < self._manual_command_until or self._manual_behavior_active()
        mode_action = None
        if autonomy_enabled and not manual_override_active:
            with self.skills.command_context("mode"):
                mode_action = self.modes_engine.tick(self.runtime, action_busy=self.action_manager.is_busy or task_active)
        if mode_action:
            self.self_prompter.mark_action()

        # Self-prompter check (mindcraft-style: trigger LLM when idle too long)
        if autonomy_enabled and not manual_override_active and self.self_prompter.should_prompt(is_idle, self.action_manager.is_busy or task_active):
            prompt = self.self_prompter.build_prompt()
            logger.info("[Session] Self-prompt triggered: %s", prompt[:60])
            # Route through LLM service to generate commands
            self._handle_self_prompt(prompt)

        # Task queue
        self.tick_tasks()

        cutoff = time.time() - 600
        for req_id, pending in list(self._pending_commands.items()):
            if pending.get("started_at", 0) < cutoff:
                self._finish_pending_command(req_id, "unknown", "no terminal event")

        self.memory.flush_if_due()

    def _handle_self_prompt(self, prompt: str):
        """Handle a self-prompt by routing through LLM to generate actions."""
        self.self_prompter.on_prompt_sent()
        context = self.build_planner_context()
        system_prompt = self.llm.build_system_prompt(context, self.commands.get_docs())
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt},
        ]
        # Non-blocking LLM call (runs on background thread)
        try:
            result = self.llm.chat(messages, agent="self_prompter")
            response_text = result.get("content", "")
            if response_text:
                # Parse and execute commands from LLM output
                with self.skills.command_context("self_prompt"):
                    cmd_results = self.commands.parse_and_execute(response_text, self.runtime)
                for cr in cmd_results:
                    self.memory.record_action(f"cmd:{cr.message.split()[0] if cr.message else '?'}", cr.success)
                    if cr.success:
                        self.self_prompter.mark_action()
                logger.info("[Session] Self-prompt generated %d commands", len(cmd_results))
        except Exception as e:
            logger.error("[Session] Self-prompt LLM error: %s", e)

    def stop(self):
        """Gracefully stop the session."""
        if self._stopped:
            return
        self._stopped = True
        self.action_manager.stop()
        self.modes_engine.reset()
        for req_id in list(self._pending_commands):
            self._finish_pending_command(req_id, "unknown", "backend stopped before terminal event")
        self.memory.save()
        self.message_db.close()
        self.memory_overlay.close()
        close_llm = getattr(self.llm, "close", None)
        if callable(close_llm):
            close_llm()
        logger.info("[Session] Stopped: %s", self.id)

    # ── Status ──

    def get_status(self) -> dict:
        now = time.time()
        state_age = None if self._last_state_at is None else max(0.0, now - self._last_state_at)
        control_state = self.runtime.get("control_state", {})
        armed = isinstance(control_state, dict) and control_state.get("ai_controlled") is True
        return {
            "id": self.id,
            "identity": self.identity.public_dict(),
            "action": self.action_manager.get_status(),
            "modes": self.modes_engine.get_status(),
            "self_prompter": self.self_prompter.get_status(),
            "planner": self.planner.get_status(),
            "task_queue_len": len(self.task_queue),
            "memory_size": self.memory.interaction_count,
            "database": self.message_db.get_stats(),
            "player": self.runtime.get("player", {}),
            "world": self.runtime.get("world", {}),
            "inventory": self.runtime.get("inventory", []),
            "equipment": self.runtime.get("equipment", {}),
            "integrations": self.runtime.get("integrations", {}),
            "runtime_context": self.runtime.get("runtime_context", {}),
            "nearby_workstations": self.runtime.get("nearby_workstations", []),
            "nearby_storage": self.runtime.get("nearby_storage", []),
            "entities": self.runtime.get("entities", []),
            "online_players": self.runtime.get("online_players", []),
            "control_state": control_state,
            "behavior_state": self.runtime.get("behavior_state", {}),
            "task_state": self.runtime.get("task_state", {}),
            "llm_usage": self.llm.get_usage(),
            "control_mode": self.control_mode,
            "body": {
                "connected": self._body_connected,
                "armed": armed,
                "observed_at": self._last_state_at,
                "state_age_seconds": state_age,
                "stale": not self._body_connected or state_age is None or state_age > 3.0,
                "sequence": self._state_sequence,
            },
            "world_model": self._ensure_world_model().status(now=now),
        }

    def set_body_connected(self, connected: bool) -> None:
        self._body_connected = connected
        self._ensure_world_model().set_connected(connected)

    def build_planner_context(self, current_player: str | None = None,
                              current_player_id: str = "",
                              observation_max_chars: int = 8000) -> dict[str, Any]:
        context = self.memory.build_context(
            current_player=current_player,
            player_id=current_player_id,
            working_context=list(self._working_context),
        )
        context.update(self._ensure_world_model().observation_slice(
            self.runtime, max_chars=observation_max_chars,
        ))
        context["persona"] = self.runtime.get("persona", {})
        return context

    def pending_decision_triggers(self, limit: int = 20) -> list[dict[str, Any]]:
        return self._ensure_world_model().pending_decision_triggers(limit)

    def acknowledge_decision_triggers(self, through_sequence: int) -> int:
        return self._ensure_world_model().acknowledge_decision_triggers(through_sequence)

    def set_planner_proposal_dispatcher(self, dispatcher) -> None:
        self._planner_proposal_dispatcher = dispatcher
        self.planner.set_proposal_dispatcher(self._dispatch_authorized_proposal if dispatcher else None)

    def _dispatch_authorized_proposal(self, proposal: SkillProposal) -> bool:
        if self._planner_proposal_dispatcher is None:
            return False
        channel = self._current_request_channel
        requester = self._current_requester
        if channel and requester:
            decision = evaluate_access(
                load_policy(DEFAULT_CONFIG_PATH),
                {"name": requester[0], "uuid": requester[1]},
                channel=channel,
                skill=classify_skill(proposal.skill_id),
                server_id=self.identity.server_id,
                body_id=self.identity.companion_id,
            )
            if not decision["allowed"]:
                logger.info("[Access] Denied %s for %s (%s)", proposal.skill_id, requester[0], decision["role"])
                return False
        return bool(self._planner_proposal_dispatcher(proposal))

    def dispatch_stop_intent(self) -> bool:
        return self.planner.dispatch_proposal(SkillProposal("core.stop", {}, "direct_stop_intent"))

    @staticmethod
    def is_stop_intent(message: str) -> bool:
        compact = re.sub(r"\s+", "", str(message)).casefold()
        if any(phrase in compact for phrase in ("不要停", "别停", "不用停", "无需停止")) \
                or re.search(r"\b(?:do\s+not|don't|dont)\s+stop\b", str(message), re.IGNORECASE):
            return False
        return any(phrase in compact for phrase in ("停下", "停止", "别跟了", "先别做了")) \
            or re.search(r"\bstop\b", str(message), re.IGNORECASE) is not None

    def _set_world_overlay(self, name: str, data: dict[str, Any]) -> None:
        world_model = self._ensure_world_model()
        world_model.ingest_overlay(name, data)
        self.runtime = world_model.legacy_projection(self.runtime)

    def _ensure_world_model(self) -> WorldModel:
        world_model = getattr(self, "world_model", None)
        if world_model is None:
            world_model = WorldModel(getattr(self, "runtime", {}))
            self.world_model = world_model
        return world_model

    def is_busy_for_external_task(self) -> bool:
        task_state = self.runtime.get("task_state", {})
        task_status = str(task_state.get("status", "idle")).lower() if isinstance(task_state, dict) else "idle"
        return self.action_manager.is_busy or self.modes_engine._active_mode is not None \
            or task_status not in {"", "idle", "done", "failed", "cancelled"} \
            or self._manual_behavior_active()

    def set_external_task_busy(self, busy: bool) -> None:
        self._external_task_busy = busy

    def set_control_mode(self, mode: str, fencing_token: int = 0) -> None:
        if mode not in {"builtin", "hybrid", "external"}:
            raise ValueError("invalid control mode")
        if self.control_mode == mode and (mode != "external" or self.control_fencing_token == fencing_token):
            return
        self.control_mode = mode
        self.control_fencing_token = fencing_token if mode == "external" else 0
        if mode == "external":
            self.modes_engine.reset()
            self.self_prompter.disable()
        else:
            self.self_prompter.enable()
        logger.info("[Session] Control mode changed to %s", mode)

    def _on_skill_command(self, cmd: str, req_id: str, context: str, args: dict):
        self.self_prompter.mark_action()
        if cmd == "stop_all":
            for pending_id in list(self._pending_commands):
                self._finish_pending_command(pending_id, "cancelled", "stopped by command")
        requester, requester_id = self._current_requester or ("", "")
        if cmd not in {"send_chat", "get_state", "get_inventory"}:
            self._pending_commands[req_id] = {
                "command": cmd,
                "args": dict(args),
                "requester": requester,
                "requester_id": requester_id,
                "started_at": time.time(),
                "context": context,
            }
        if context != "manual_chat" or cmd == "send_chat":
            return
        self._manual_action_reqs.add(req_id)
        self._manual_task_kind = cmd
        extension = 20.0
        if cmd in {"follow_player", "craft_item", "eat", "collect_blocks", "move_to", "explore", "build",
                   "harvest_crop_at", "break_block_at", "use_block_at", "place_block_at"}:
            extension = 45.0
        elif cmd == "stop_all":
            self._manual_action_reqs.clear()
            self._manual_task_kind = None
            extension = 6.0
        self._manual_command_until = max(self._manual_command_until, time.time() + extension)

    @staticmethod
    def _outcome_commands() -> set[str]:
        return {"craft_item", "eat", "collect_blocks", "move_to", "mine_block", "mine_block_at", "follow_player",
                "harvest_crop_at", "break_block_at", "use_block_at", "place_block_at"}

    @classmethod
    def _deferred_terminal_commands(cls) -> set[str]:
        return cls._outcome_commands()

    def register_external_command(self, command: str, req_id: str, args: dict,
                                  requester: str = "sdk") -> None:
        if command in {"send_chat", "get_state", "get_inventory"}:
            return
        self._pending_commands[req_id] = {
            "command": command,
            "args": dict(args),
            "requester": requester,
            "requester_id": "",
            "started_at": time.time(),
            "context": "actuator",
        }

    def unregister_external_command(self, req_id: str) -> None:
        self._pending_commands.pop(req_id, None)

    def _finish_pending_command(self, req_id: str, outcome: str, detail: str = "") -> None:
        pending = self._pending_commands.pop(req_id, None)
        if not pending:
            return
        args = pending.get("args", {})
        target = str(
            args.get("item") or args.get("block_type") or args.get("player")
            or args.get("structure") or ""
        )
        if self.control_mode != "external":
            self.memory.record_task_outcome(
                pending["command"],
                outcome,
                target=target,
                requester=pending.get("requester", ""),
                requester_id=pending.get("requester_id", ""),
                detail=detail,
                duration=time.time() - pending.get("started_at", time.time()),
            )

    def _manual_behavior_active(self) -> bool:
        behavior = self.runtime.get("behavior_state", {})
        task_state = self.runtime.get("task_state", {})
        if not isinstance(behavior, dict):
            return False
        task_kind = self._manual_task_kind
        if task_kind == "follow_player":
            return bool(behavior.get("follow_target"))
        if task_kind == "craft_item":
            return bool(behavior.get("pending_craft_item")) or (
                isinstance(task_state, dict) and task_state.get("kind") == "craft" and task_state.get("status") != "idle"
            )
        if task_kind == "eat":
            return bool(behavior.get("pending_eat"))
        if task_kind == "collect_blocks":
            return (
                bool(behavior.get("navigating"))
                or (isinstance(task_state, dict) and task_state.get("kind") in {"collect", "craft"}
                    and task_state.get("status") in {"searching", "moving", "collecting", "mining"})
            )
        if task_kind in {"move_to", "explore", "build"}:
            return bool(behavior.get("navigating"))
        return False
