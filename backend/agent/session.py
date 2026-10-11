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
  └── runtime: dict (玩家/世界/背包快照)
"""

import asyncio
import logging
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from protocol import WireClient, ControlStream
from .config_store import DEFAULT_CONFIG_PATH, get_config_store
from .action_manager import ActionManager
from .memory import Memory, WorldPoiMemory
from .skills import Skills
from .modes_engine import ModesEngine
from .commands import Commands
from .llm_service import LLMService
from .self_prompter import SelfPrompter
from .message_db import MessageDB
from .conversation import ConversationStore
from .permissions import PermissionsStore, ReplyGate
from .persona_store import PersonaStore
from .task_list import TaskList

logger = logging.getLogger("session")


class ChatInbox:
    """近期聊天事件缓冲 + 等待原语（线程安全）。

    谁在往里投：chat 事件会在 **wire 读取线程**里被投进来（见 `Session.__init__`
    注册的 listener），所以即便 orchestrator 主循环正阻塞在 planner 等 `/locate`
    的回复，也不会死锁 —— 这是 `Planner.locate_structure` 能"等到回复"的前提。

    为什么不用 `Memory.recent_messages`：那份是"交互记录"（带 AI 自己的回复、
    受记忆策略影响），而语境判断需要的是**原始事件流**（谁、什么时候、说了什么、
    发送者当时在哪）。
    """

    def __init__(self, limit: int = 50):
        self.limit = limit
        self._entries: list[dict] = []
        self._total = 0                     # 累计投递条数（游标基准）
        self._cond = threading.Condition()

    def push(self, entry: dict) -> None:
        with self._cond:
            self._entries.append(entry)
            self._total += 1
            overflow = len(self._entries) - self.limit
            if overflow > 0:
                del self._entries[:overflow]
            self._cond.notify_all()

    @property
    def cursor(self) -> int:
        """当前累计条数。发命令前取一次，用它保证"只看这之后到达的回复"。"""
        with self._cond:
            return self._total

    def recent(self, n: int = 8) -> list[dict]:
        """最近 n 条（时间从早到晚），供 LLM 判断语境。"""
        with self._cond:
            return [dict(entry) for entry in self._entries[-max(0, n):]]

    def wait_after(self, cursor: int, predicate, timeout: float) -> Optional[dict]:
        """等 cursor 之后第一条满足 predicate 的聊天；超时返回 None。"""
        deadline = time.monotonic() + max(0.0, float(timeout))
        with self._cond:
            while True:
                # 环形缓冲里保留的第一条 = 第 (_total - len(_entries)) 条
                first = self._total - len(self._entries)
                start = max(0, int(cursor) - first)
                for entry in self._entries[start:]:
                    if predicate(entry):
                        return dict(entry)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._cond.wait(remaining)


class Session:
    """
    Central session that manages an AI instance.
    """

    # Command completion policy (closed action loop):
    # cmd -> (busy_timeout_s, completes_on_response)
    # - Long tasks: first wire response only queues the task; completion
    #   arrives via task_state terminal status (grace-debounced, see tick).
    # - Instant commands: the wire response IS the completion.
    # - Queries: tracked briefly only to keep the busy window honest.
    LONG_TASK_CMDS = {
        "craft_item": (600.0, False),
        "collect_blocks": (600.0, False),
        "follow_player": (3600.0, False),
        "eat": (60.0, False),
        "sleep": (60.0, False),
        "build": (300.0, False),
        "trade": (60.0, False),
        "sort_inventory": (30.0, False),
        "explore": (120.0, False),
        "move_to": (120.0, False),
    }

    def __init__(self, wire: WireClient, session_id: str | None = None):
        self.id = session_id or str(uuid.uuid4())[:8]
        self.wire = wire

        # Core subsystems
        self.action_manager = ActionManager()
        self.skills = Skills(wire)
        self.skills.set_command_observer(self._on_skill_command)
        # 高频控制流（P0 反射层 / VLA 出口）；tick 里心跳保持按键。
        self.control = ControlStream(wire)
        self.skills.set_control_stream(self.control)
        self.memory = Memory(path=f"data/memory/session_{self.id}.json")
        # POI 记忆：按「服务器 + 世界」命名空间持久化，跨局记住箱子位置与内容
        self.poi = WorldPoiMemory()
        self.commands = Commands(self.skills)
        self.modes_engine = ModesEngine(self.skills, self.memory)
        self.llm = LLMService()

        # Message database (SQLite persistence)
        self.message_db = MessageDB(db_path=f"data/messages_{self.id}.db")

        # Register default modes
        self.modes_engine.add_defaults()

        # 聊天语境：原始聊天事件流（谁在说话、发言瞬间在哪）+ 最近发话者。
        self.chat_inbox = ChatInbox()
        self.last_speaker: dict = {}
        # 本地记下我们刚让 HUD 显示的描述词，供旧版模组（state_update 不带 hud_note）兜底。
        self._hud_note_local = ""

        # 聊天事件在 **wire 读取线程**上旁路一份（见 ChatInbox 注释）：
        # planner 等 `/locate` 回复时会阻塞 orchestrator 主循环，只有读取线程还活着。
        # 假 wire（测试）没有这个钩子，就退回 handle_event 那条路，只投一次、不重复。
        self._chat_tap_registered = False
        add_listener = getattr(wire, "add_chat_listener", None)
        if callable(add_listener):
            try:
                add_listener(self._note_chat)
                self._chat_tap_registered = True
            except Exception as e:
                logger.warning("[Session] 注册聊天监听失败: %s", e)

        # ── M3-19：在场门 / 对话记忆 / 人设 / 权限与回复门 / 任务列表 ──
        # 在场门：模组在"不在世界"时会发最小状态 {in_world:false, ui_screen}。
        # 旧模组不发这个字段 → 一律视为在世界（不能因为字段缺失就永久停摆）。
        self.in_world: bool = True
        self.ui_screen: str = ""
        self._presence_changed_at = time.time()
        # 对话记忆（按服务器分区；L1 落盘 / L2 窗口 / L3 LLM 摘要）
        self.conversation = ConversationStore(llm=self.llm)
        # 人设三段（.local/persona.json）：与 legacy 的 personality/speaking_style 分开存
        self.persona = PersonaStore()
        # 权限 + 回复决策门（tier 只决定"能否下指令/任务权重/记忆可见性"，不写进决策链）
        self.permissions = PermissionsStore()
        self.reply_gate = ReplyGate(self.permissions)
        # 任务列表（多任务 + 归属 + 优先级 + 状态 + 进度）
        self.tasks = TaskList()
        # 当前对话的发起者（供任务归属/权限判定；没有对话时是空）
        self._actor: dict = {"name": "", "tier": "guest"}
        # 抗抖窗里攒下的那一条（tick 复查时才真正回）
        self._pending_chat: Optional[dict] = None
        # 对话决策审计（"他为什么不理我"看这里）
        self._chat_audit: list[dict] = []
        # 事件注入审计 + 频率（主动参与：绝不提供"事件→必发消息"的短路）
        self._event_audit: list[dict] = []
        self._event_cooldowns: dict[str, float] = {}
        self._last_event_at = 0.0
        self._pending_death: Optional[dict] = None
        self._seen_drop_ids: set[str] = set()
        self._last_biome = ""
        self._last_task_event: dict = {}

        # Planner (MaiBot-style two-stage reasoning)
        # 阈值走共享 ConfigStore → UI 改完即时生效（同一实例，不会各持副本）。
        from .planner import Planner
        self.planner = Planner(
            self.llm, self.memory, self.skills,
            threshold_provider=get_config_store(DEFAULT_CONFIG_PATH).get_decision_threshold,
            chat_inbox=self.chat_inbox,
        )

        # Vision (Python 侧直接抓屏 -> VLM，后台线程低频刷新，不阻塞动作链路)
        from .vision import VisionInterpreter
        self.vision = VisionInterpreter(self.llm)

        # State snapshot
        self.runtime: dict = {
            "player": {},
            "world": {},
            "inventory": [],
            "entities": [],
            "nearby_blocks": [],
        }

        # Grace timer: set when task_state goes terminal, cleared on active
        # status. Long actions complete only after the grace elapses (Java
        # emits intermediate clearTaskState between craft/collect steps).
        self._task_idle_since: float | None = None

        # Self-prompter (mindcraft-style autonomous prompting)
        self.self_prompter = SelfPrompter(cooldown=15.0, max_idle_cycles=3)

        # Auto-save
        self._last_save = time.monotonic()
        self._manual_command_until = 0.0
        self._manual_action_reqs: set[str] = set()
        self._manual_task_kind: Optional[str] = None

        # 夜晚自动睡觉：开关走共享 ConfigStore（UI 即时生效）；同一夜只发一次。
        self._night_auto_sleep_provider = get_config_store(DEFAULT_CONFIG_PATH).get_night_auto_sleep
        self._last_auto_sleep = 0.0
        self._auto_sleep_sent = False

    # ── Event handlers ──

    def _note_chat(self, data: dict):
        """吸收一条聊天事件：进近期聊天缓冲 + 更新"最近发话者"及其**发言瞬间**坐标。

        坐标取自事件的 `sender_x/y/z`（Java 在发消息那一刻抓的），而不是 AI 自己的位置 ——
        用户说"在这儿守着"时，要守的是**他站的地方**。
        """
        if not isinstance(data, dict):
            return
        sender = str(data.get("sender") or "?")
        is_system = bool(data.get("is_system"))
        entry: dict = {
            "sender": sender,
            "message": str(data.get("message") or ""),
            "is_system": is_system,
            "time": time.time(),
        }
        for key in ("sender_x", "sender_y", "sender_z", "sender_distance"):
            value = data.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                entry[key] = float(value)

        inbox = getattr(self, "chat_inbox", None)
        if inbox is not None:
            inbox.push(entry)

        # 系统消息（/locate 的回复等）不是"人在说话"，不更新最近发话者。
        if is_system or sender in ("", "?"):
            return
        self.last_speaker = {
            "name": sender,
            "message": entry["message"],
            "time": entry["time"],
            "x": entry.get("sender_x"),
            "y": entry.get("sender_y"),
            "z": entry.get("sender_z"),
            "distance": entry.get("sender_distance"),
        }
        logger.debug("[Session] 最近发话者: %s @(%s,%s,%s)", sender,
                     entry.get("sender_x"), entry.get("sender_y"), entry.get("sender_z"))

    def handle_event(self, event_type: str, data: dict):
        """Route an event from the mod."""
        match event_type:
            case "state_update":
                self._apply_presence(data)
                self.runtime.update(data)
                self._absorb_poi(data)
                self._absorb_conversation_namespace(data)
            case "player_chat":
                self._handle_player_chat(data)
            case "player_death":
                logger.info("[Session] Player died at %s", data.get("position"))
                self.memory.add_interaction("system", "Player died", action="death")
                self.memory.record_event("death", "玩家死亡")
                self.message_db.add_event("death", "玩家死亡")
                self._record_death(data)
            case "command_response":
                req_id = data.get("id", "?")
                success = data.get("success", False)
                self.action_manager.handle_response(req_id, success)
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
                if req_id in self._manual_action_reqs:
                    self._manual_command_until = max(self._manual_command_until, time.time() + 15.0)
                    if progress <= 0.0 or progress >= 1.0:
                        self._manual_action_reqs.discard(req_id)
            case "behavior_state":
                self.runtime["behavior_state"] = data
                if self._manual_behavior_active():
                    self._manual_command_until = max(self._manual_command_until, time.time() + 8.0)
                elif not self._manual_action_reqs:
                    self._manual_task_kind = None
            case "task_state":
                self.runtime["task_state"] = data
                status = str(data.get("status", "idle")).strip().lower()
                if status in {"", "idle", "done", "failed", "cancelled", "error"}:
                    # Terminal (Java also emits idle between craft/collect
                    # steps) — start grace window; tick() completes the
                    # tracked action only if no active status follows.
                    if self._task_idle_since is None:
                        self._task_idle_since = time.time()
                else:
                    self._task_idle_since = None
                if self._manual_behavior_active():
                    self._manual_command_until = max(self._manual_command_until, time.time() + 8.0)
            case "control_state":
                self.runtime["control_state"] = data
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

    def _absorb_poi(self, data: dict):
        """吸收一次状态里的 POI 观测，并按当前服务器/世界切换命名空间。"""
        world = data.get("world")
        world = world if isinstance(world, dict) else {}
        self.poi.use_namespace(world.get("server", ""), world.get("world_name", ""))
        self.poi.observe_state(data)

    def _hud_note(self) -> str:
        """HUD 状态词后面的描述词：以模组上报的为准，旧版模组没有该字段时用本地回声。"""
        reported = self.runtime.get("hud_note")
        if isinstance(reported, str):
            return reported
        return str(getattr(self, "_hud_note_local", "") or "")

    def _players_for_status(self) -> list[dict]:
        """在线玩家（模组按距离给的最近 ≤8 个），标出"最近发话者"供高亮。

        模组已经按"最近发过话的人"给了 `speaking`；这里再兜一层：旧版模组没这个字段时，
        用后端自己记的最近发话者补上。**只加不减** —— 不推翻模组的判断。
        """
        players = self.runtime.get("players")
        if not isinstance(players, list):
            players = []
        speaker = str((getattr(self, "last_speaker", {}) or {}).get("name") or "")
        rows: list[dict] = []
        for player in players:
            if not isinstance(player, dict):
                continue
            row = dict(player)
            if speaker and str(row.get("name") or "") == speaker:
                row["speaking"] = True
            rows.append(row)
        return rows

    def _task_for_status(self) -> dict:
        """当前任务（kind/status/target/detail/progress），没有就是空 dict。"""
        task = self.runtime.get("task_state")
        if not isinstance(task, dict) or not task:
            return {}
        return {
            "kind": str(task.get("kind") or "idle"),
            "status": str(task.get("status") or "idle"),
            "target": str(task.get("target") or ""),
            "detail": str(task.get("detail") or ""),
            "progress": float(task.get("progress") or 0.0),
        }

    def _runtime_context(self) -> dict:
        """把 runtime 快照整理成 LLM 上下文（聊天与自我提示两条路共用，避免字段漂移）。"""
        context = self.memory.build_context()
        context["persona"] = self.runtime.get("persona", {})
        context["task_state"] = self.runtime.get("task_state", {})
        context["behavior_state"] = self.runtime.get("behavior_state", {})
        context["control_state"] = self.runtime.get("control_state", {})
        context["player"] = self.runtime.get("player", {})
        context["world"] = self.runtime.get("world", {})
        context["inventory"] = self.runtime.get("inventory", [])
        context["entities"] = self.runtime.get("entities", [])
        context["nearby_blocks"] = self.runtime.get("nearby_blocks", [])
        context["nearby_workstations"] = self.runtime.get("nearby_workstations", [])
        context["nearby_storage"] = self.runtime.get("nearby_storage", [])
        # 夜晚/睡眠/持久记录点：直接来自 state_update 顶层字段。
        context["nearby_beds"] = self.runtime.get("nearby_beds", [])
        context["remembered_pois"] = self.runtime.get("remembered_pois", [])
        context["is_night"] = self.runtime.get("is_night", False)
        context["day_time"] = self.runtime.get("day_time")
        context["sky_darken"] = self.runtime.get("sky_darken")
        context["can_sleep"] = self.runtime.get("can_sleep", False)
        context["vision"] = self.vision.latest()
        context["known_poi"] = self.poi.describe()

        # 语境：谁在线、谁刚说过话（以及他说那话时站在哪）、最近的聊天顺序。
        context["players"] = self._players_for_status()
        context["last_speaker"] = dict(getattr(self, "last_speaker", {}) or {})
        inbox = getattr(self, "chat_inbox", None)
        context["recent_chat"] = inbox.recent(8) if inbox is not None else []
        context["hud_note"] = self._hud_note()

        # 饥饿/饱和度：饱食度（food level）与隐藏的饱和度分开给，回血判断要看两者。
        player = context["player"] if isinstance(context["player"], dict) else {}
        context["saturation"] = float(player.get("saturation") or 0.0)
        context["food"] = player.get("hunger")
        return context

    def handle_chat(self, sender: str, message: str, command_context: str = "chat") -> Optional[str]:
        """
        Process a chat message through the LLM.
        Uses Planner for intelligent action planning.
        """
        context = self._runtime_context()

        # Use Planner to plan and execute
        with self.skills.command_context(command_context):
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

    # ── Lifecycle ──

    def tick(self):
        """Called every event loop cycle."""
        # Action manager tick (check timeouts, process completions)
        self.action_manager.tick()

        # Long-task completion: task_state went terminal and stayed terminal
        # through the grace window → the tracked action is really done.
        self._complete_long_task_if_idle()

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
        task_active = bool(task_state) and str(task_state.get("kind", "idle")).strip().lower() != "idle" \
            and str(task_state.get("status", "idle")).strip().lower() not in {"", "idle", "done", "failed", "cancelled"}
        is_idle = (active_mode is None
                   and not self.action_manager.is_busy
                   and not task_active)

        # Modes engine (priority behaviors that don't need LLM)
        behavior_state = self.runtime.get("behavior_state", {}) if isinstance(self.runtime.get("behavior_state"), dict) else {}
        control_state = self.runtime.get("control_state", {}) if isinstance(self.runtime.get("control_state"), dict) else {}
        autonomy_enabled = control_state.get("ai_controlled", True) and behavior_state.get("behaviors_enabled", True)
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

        # 夜晚自动睡觉：自主开启、无任务、天黑且能睡 → 限频下发一次。
        self._maybe_auto_sleep(
            autonomy_enabled and not manual_override_active, is_idle, task_active)

        # 控制流心跳：按住期间必须持续下发，否则 Java 侧 500ms 超时会自动松手。
        self.control.flush()

        # Auto-save
        now = time.monotonic()
        if now - self._last_save > 120:
            self._last_save = now
            self.memory.save()
            self.poi.save()

    def _handle_self_prompt(self, prompt: str):
        """Handle a self-prompt by routing through LLM to generate actions."""
        self.self_prompter.on_prompt_sent()
        # Build system context
        context = self._runtime_context()
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

    # 同一夜最多自动睡一次；两次尝试之间至少间隔这么久（避免刷屏/抖动手）。
    AUTO_SLEEP_COOLDOWN = 60.0

    def _maybe_auto_sleep(self, autonomy_enabled: bool, is_idle: bool,
                          task_active: bool, now: Optional[float] = None) -> bool:
        """夜晚自动睡觉：满足条件时下发一次 sleep_in_bed，返回是否真的下发。

        限频：天亮复位；同一夜只发一次；且两次尝试至少间隔 AUTO_SLEEP_COOLDOWN。
        wire 命令是 `sleep`，Java 侧负责寻路 + 点床，进度由 task_state 反映。
        """
        runtime = self.runtime if isinstance(self.runtime, dict) else {}
        if not bool(runtime.get("is_night")):
            # 天亮：复位，下一夜可以再睡。
            self._auto_sleep_sent = False
            return False
        if not (autonomy_enabled and is_idle and not task_active):
            return False
        if getattr(self, "_auto_sleep_sent", False):
            return False
        provider = getattr(self, "_night_auto_sleep_provider", None)
        if provider is not None:
            try:
                if not provider():
                    return False
            except Exception as e:
                logger.warning("[Session] night_auto_sleep 读取失败(%s)，按开启处理", e)
        if not bool(runtime.get("can_sleep")):
            return False
        if not self._has_known_bed(runtime):
            # 附近和记忆里都没床 → 不发 sleep，免得反复空转（Java 只会诚实回 no_bed）。
            return False
        current = time.time() if now is None else now
        if current - getattr(self, "_last_auto_sleep", 0.0) < self.AUTO_SLEEP_COOLDOWN:
            return False
        with self.skills.command_context("auto_sleep"):
            result = self.skills.sleep_in_bed()
        self._last_auto_sleep = current
        self._auto_sleep_sent = True
        logger.info("[Session] 夜晚自动睡觉已下发: %s", (result or {}).get("message", "sleep sent"))
        return True

    @staticmethod
    def _is_bed_poi(poi: dict) -> bool:
        """recorded POI 是否是床（category=bed，或方块 id 是 *_bed / bed）。"""
        if str(poi.get("category") or "").strip().lower() == "bed":
            return True
        local = str(poi.get("id") or poi.get("block_id") or "").strip().lower().rsplit(":", 1)[-1]
        return local == "bed" or local.endswith("_bed")

    @classmethod
    def _has_known_bed(cls, runtime: dict) -> bool:
        """视野附近有床，或持久记忆里有床 → 才值得尝试 sleep。"""
        if runtime.get("nearby_beds"):
            return True
        for poi in runtime.get("remembered_pois") or []:
            if isinstance(poi, dict) and cls._is_bed_poi(poi):
                return True
        return False

    def stop(self):
        """Gracefully stop the session."""
        self.action_manager.stop()
        self.modes_engine.reset()
        # 兜底松手：任何退出路径都不允许按键卡住。
        self.control.release()
        self.vision.stop()
        self.poi.save()
        self.memory.save()
        self.message_db.close()
        logger.info("[Session] Stopped: %s", self.id)

    # ── Status ──

    def get_status(self) -> dict:
        return {
            "id": self.id,
            "action": self.action_manager.get_status(),
            "planner": self.planner.get_status(),
            "modes": self.modes_engine.get_status(),
            "self_prompter": self.self_prompter.get_status(),
            "control_holding": self.control.holding,
            "memory_size": self.memory.interaction_count,
            "database": self.message_db.get_stats(),
            "player": self.runtime.get("player", {}),
            "world": self.runtime.get("world", {}),
            "task_state": self.runtime.get("task_state", {}),
            # 仪表盘面板用的三个字段（旧版模组没这些字段时给安全的空值）。
            "players": self._players_for_status(),
            "task": self._task_for_status(),
            "hud_note": self._hud_note(),
            "last_speaker": dict(getattr(self, "last_speaker", {}) or {}),
            "vision": self.vision.get_status(),
            "poi_memory": {
                "namespace": self.poi.namespace,
                "storage": len(self.poi.find("storage")),
                "workstation": len(self.poi.find("workstation")),
            },
            "llm_usage": self.llm.get_usage(),
        }

    def _complete_long_task_if_idle(self):
        """Complete a tracked long task once task_state stayed terminal for
        the full grace window (Java emits intermediate clears between steps)."""
        if self._task_idle_since is None or not self.action_manager.is_busy:
            return
        cur = self.action_manager.current
        if cur is None or cur.complete_on_response:
            return
        if time.time() - self._task_idle_since >= 3.0:
            status = str(self.runtime.get("task_state", {}).get("status", "idle"))
            ok = status.strip().lower() == "done"
            self.action_manager.complete_current(ok, f"task_state={status}")
            self._task_idle_since = None

    def _on_skill_command(self, cmd: str, req_id: str, context: str, args: dict):
        self.self_prompter.mark_action()
        if cmd == "set_hud_note":
            # 本地回声：模组下一次 state_update 会带权威值（含过期清空），这里只作兜底。
            self._hud_note_local = str((args or {}).get("text") or "")
        if cmd == "stop_all":
            # Stopping frees whatever long action is in flight
            self.action_manager.stop()
            self._task_idle_since = None
        if cmd in self.LONG_TASK_CMDS and self.action_manager.is_busy:
            # A long task is running; don't let a second one orphan its tracking
            logger.warning("[Session] Ignoring %s while %s in flight", cmd, self.action_manager.current_label)
        else:
            timeout, completes_on_response = self.LONG_TASK_CMDS.get(cmd, (10.0, True))
            self.action_manager.track(cmd, req_id, timeout=timeout,
                                      complete_on_response=completes_on_response)
            self._task_idle_since = None
        if context != "manual_chat" or cmd == "send_chat":
            return
        self._manual_action_reqs.add(req_id)
        self._manual_task_kind = cmd
        extension = 20.0
        if cmd in {"follow_player", "craft_item", "eat", "collect_blocks", "move_to", "explore", "build"}:
            extension = 45.0
        elif cmd == "stop_all":
            self._manual_action_reqs.clear()
            self._manual_task_kind = None
            extension = 6.0
        self._manual_command_until = max(self._manual_command_until, time.time() + extension)

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
