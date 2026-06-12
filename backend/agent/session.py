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
import logging
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from protocol import WireClient
from .config_store import DEFAULT_CONFIG_PATH
from .action_manager import ActionManager
from .memory import Memory
from .skills import Skills
from .modes_engine import ModesEngine
from .commands import Commands
from .llm_service import LLMService
from .self_prompter import SelfPrompter
from .message_db import MessageDB

logger = logging.getLogger("session")


class Session:
    """
    Central session that manages an AI instance.
    """

    def __init__(self, wire: WireClient, session_id: str | None = None):
        self.id = session_id or str(uuid.uuid4())[:8]
        self.wire = wire

        # Core subsystems
        self.action_manager = ActionManager()
        self.skills = Skills(wire)
        self.memory = Memory(path=f"data/memory/session_{self.id}.json")
        self.commands = Commands(self.skills)
        self.modes_engine = ModesEngine(self.skills, self.memory)
        self.llm = LLMService()

        # Message database (SQLite persistence)
        self.message_db = MessageDB(db_path=f"data/messages_{self.id}.db")

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

        # Task queue
        self.task_queue: list[dict] = []

        # Self-prompter (mindcraft-style autonomous prompting)
        self.self_prompter = SelfPrompter(cooldown=15.0, max_idle_cycles=3)

        # Auto-save
        self._last_save = time.monotonic()
        self._manual_command_until = 0.0

    # ── Event handlers ──

    def handle_event(self, event_type: str, data: dict):
        """Route an event from the mod."""
        match event_type:
            case "state_update":
                self.runtime.update(data)
                # Run modes engine on state update
                self.modes_engine.tick(self.runtime, action_busy=self.action_manager.is_busy)
            case "player_chat":
                sender = data.get("sender", "?")
                message = data.get("message", "")
                is_system = data.get("is_system", False)

                # Chat permission check (whitelist)
                if not self._check_chat_permission(sender, is_system):
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

                if reason.startswith("direct_command") or reason.startswith("mentioned") or reason == "private_chat":
                    self._manual_command_until = time.time() + 20.0
                
                # 记录回复
                self.timing_gate.record_reply()
                
                # 使用 Planner 规划回复
                try:
                    response = self.handle_chat(sender, message)
                    if response:
                        logger.info("[Chat] AI response: %s", response[:80])
                        # Send response as chat message
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
            case "player_death":
                logger.info("[Session] Player died at %s", data.get("position"))
                self.memory.add_interaction("system", "Player died", action="death")
                self.memory.record_event("death", "玩家死亡")
                self.message_db.add_event("death", "玩家死亡")
            case "command_response":
                req_id = data.get("id", "?")
                success = data.get("success", False)
                self.action_manager.handle_response(req_id, success)
            case "command_progress":
                req_id = data.get("id", "?")
                progress = float(data.get("progress", 0.0) or 0.0)
                message = data.get("message", "")
                self.action_manager.handle_progress(req_id, progress, message)
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

    def _check_chat_permission(self, sender: str, is_system: bool) -> bool:
        """Check if a chat sender is allowed to talk to this AI."""
        # Load whitelist from config
        try:
            import json
            config_path = DEFAULT_CONFIG_PATH
            if config_path.exists():
                config = json.loads(config_path.read_text(encoding="utf-8"))
            else:
                config = {}
        except Exception:
            config = {}

        whitelist = config.get("whitelist", [])
        listen_public = config.get("listen_public", True)

        # Empty whitelist = listen to everyone
        if not whitelist:
            return True

        # Check if sender is in whitelist
        if sender in whitelist:
            return True

        # Check public chat permission
        if listen_public and not is_system:
            return True

        return False

    def handle_chat(self, sender: str, message: str) -> Optional[str]:
        """
        Process a chat message through the LLM.
        Uses Planner for intelligent action planning.
        """
        # Get context
        context = self.memory.build_context()
        context["persona"] = self.runtime.get("persona", {})
        
        # Use Planner to plan and execute
        response = self.planner.plan_and_execute(
            sender, message, context, bot_name=self._get_bot_name()
        )
        
        if response:
            # Record interaction
            self.memory.add_interaction(
                sender=sender,
                message=message,
                response=response,
            )
        
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

    def tick(self):
        """Called every event loop cycle."""
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
        is_idle = (active_mode is None
                   and not self.task_queue
                   and not self.action_manager.is_busy)

        # Modes engine (priority behaviors that don't need LLM)
        manual_override_active = time.time() < self._manual_command_until
        mode_action = None if manual_override_active else self.modes_engine.tick(self.runtime, action_busy=self.action_manager.is_busy)
        if mode_action:
            self.self_prompter.mark_action()

        # Self-prompter check (mindcraft-style: trigger LLM when idle too long)
        if not manual_override_active and self.self_prompter.should_prompt(is_idle, self.action_manager.is_busy):
            prompt = self.self_prompter.build_prompt()
            logger.info("[Session] Self-prompt triggered: %s", prompt[:60])
            # Route through LLM service to generate commands
            self._handle_self_prompt(prompt)

        # Task queue
        self.tick_tasks()

        # Auto-save
        now = time.monotonic()
        if now - self._last_save > 120:
            self._last_save = now
            self.memory.save()

    def _handle_self_prompt(self, prompt: str):
        """Handle a self-prompt by routing through LLM to generate actions."""
        self.self_prompter.on_prompt_sent()
        # Build system context
        context = self.memory.build_context()
        context["persona"] = self.runtime.get("persona", {})
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
        self.action_manager.stop()
        self.modes_engine.reset()
        self.memory.save()
        self.message_db.close()
        logger.info("[Session] Stopped: %s", self.id)

    # ── Status ──

    def get_status(self) -> dict:
        return {
            "id": self.id,
            "action": self.action_manager.get_status(),
            "modes": self.modes_engine.get_status(),
            "self_prompter": self.self_prompter.get_status(),
            "task_queue_len": len(self.task_queue),
            "memory_size": self.memory.interaction_count,
            "database": self.message_db.get_stats(),
            "player": self.runtime.get("player", {}),
            "world": self.runtime.get("world", {}),
            "llm_usage": self.llm.get_usage(),
        }
