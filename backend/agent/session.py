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
import json
import logging
import re
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


def _wake_names_from(persona: dict, bot_name: str) -> list[str]:
    """唤醒名 = 人设里的 wake_names + AI 自己的名字（去重、保序）。

    抽成模块级函数是为了让 `_get_wake_names()` 既能吃 `.local/persona.json`，
    也能吃 legacy 的 `runtime["persona"]`（合并视图由调用方给）。
    """
    merged: list[str] = []
    if isinstance(persona, dict):
        for name in persona.get("wake_names") or []:
            text = str(name or "").strip()
            if text and text not in merged:
                merged.append(text)
    if bot_name and bot_name not in merged:
        merged.append(bot_name)
    return merged or ["AI"]


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
        # `llm_active` 是在场门：不在世界里视觉线程照常活着但一次都不抓屏/不调模型。
        from .vision import VisionInterpreter
        self.vision = VisionInterpreter(self.llm, activity_provider=self.llm_active)

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
                self._absorb_task_state(data)
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

    # ── M3-19：在场门 / 对话记忆 / 权限与回复门 / 任务列表 / 主动参与 ──

    # 事件注入的每类冷却 + 全局最小间隔：主动参与是"可以有"，不是"必须有"，
    # 没有这两道限频，事件注入就退化成刷屏（设计文档 §5 明确禁止"事件→必发消息"的短路）。
    EVENT_COOLDOWNS = {
        "death": 45.0, "besieged": 60.0, "rare_drop": 60.0,
        "new_biome": 90.0, "task_done": 30.0, "task_failed": 30.0,
    }
    EVENT_MIN_GAP = 15.0
    # 语义门是"低成本的一眼判断"，给它一个短超时，别让它变成新的延迟瓶颈。
    SEMANTIC_GATE_TIMEOUT = 6.0
    # 语义门说 wait 时的自续窗口与次数上限（设计文档 §5 的第二个入口：wait 超时自续）。
    WAIT_RETRY_SECONDS = 2.0
    WAIT_MAX_RETRIES = 2
    # "稀有掉落"只看这些 id（不是"任何掉落物都要起哄"）。
    RARE_DROP_IDS = (
        "diamond", "emerald", "netherite", "ancient_debris", "enchanted_golden_apple",
        "elytra", "nether_star", "dragon_egg", "totem_of_undying", "shulker_shell",
    )

    # ── 在场门 ──

    def llm_active(self) -> bool:
        """在场门的唯一判据：**只有**在世界里才允许调 LLM / 下发 wire 指令。

        planner / decider / 语义门 / vision / modes_engine / self_prompter / 夜晚自动睡觉
        全部以它为前置；不在世界时事件照收（进对话记忆），但零 token、零下发。
        """
        return bool(getattr(self, "in_world", True))

    def _apply_presence(self, data: dict) -> None:
        """吸收 `in_world` / `ui_screen`，并在状态翻转时做进入/离开世界的处理。"""
        if not isinstance(data, dict):
            return
        if "in_world" in data:
            new_state = bool(data.get("in_world"))
        else:
            # 旧模组不发 in_world：视为在世界（宁可多做事，也不能因为字段缺失就永久停摆）。
            new_state = True
        screen = data.get("ui_screen")
        self.ui_screen = screen if isinstance(screen, str) else ""
        previous = bool(getattr(self, "in_world", True))
        self.in_world = new_state
        if new_state != previous:
            self._presence_changed_at = time.time()
            if new_state:
                self._on_enter_world()
            else:
                self._on_leave_world()

    def _on_leave_world(self) -> None:
        """离开世界：停 LLM、停一切下发，只留"消费事件"这条路。

        注意**不动 autonomy 开关**（control_state.ai_controlled / behaviors_enabled 原样），
        回到世界要能自动恢复 —— 开关是用户的意图，不该被在场门顺手改掉。
        """
        logger.info("[Session] 离开世界（界面 %s）→ 静默模式：停 LLM / 停下发，事件照收",
                    self.ui_screen or "?")
        # 松手：本地清空按键/视角，**不发任何帧**（不在世界里发指令既无意义也违约）。
        control = getattr(self, "control", None)
        clear = getattr(control, "clear", None)
        if callable(clear):
            try:
                clear()
            except Exception as e:
                logger.warning("[Session] 控制流本地清空失败: %s", e)
        # 在途动作不可能完成（世界都没了）→ 如实中止跟踪，不要等它超时。
        action_manager = getattr(self, "action_manager", None)
        if action_manager is not None:
            try:
                action_manager.stop()
            except Exception as e:
                logger.warning("[Session] 中止在途动作失败: %s", e)
        self._task_idle_since = None
        self._pending_chat = None
        # 离开世界的这段时间攒下的事件不再补说（回来再调侃几分钟前的死亡很怪）。
        self._pending_death = None
        self._last_task_event = {}
        store = getattr(self, "conversation", None)
        if store is not None:
            try:
                store.record_system("离开世界")
            except Exception as e:
                logger.warning("[Session] 记录离开世界失败: %s", e)
        self._audit_chat({"sender": "", "tier": "", "effective": 0.0, "threshold": 0,
                          "pending": 0, "decision": "paused", "reason": "left_world"})

    def _on_enter_world(self) -> None:
        """回到世界：自动恢复决策（什么都不用重启，tick 下一拍就照常跑）。"""
        logger.info("[Session] 回到世界 → 恢复决策与下发")
        store = getattr(self, "conversation", None)
        if store is not None:
            try:
                store.record_system("回到世界")
            except Exception as e:
                logger.warning("[Session] 记录回到世界失败: %s", e)
        self._audit_chat({"sender": "", "tier": "", "effective": 0.0, "threshold": 0,
                          "pending": 0, "decision": "resumed", "reason": "entered_world"})

    # ── 对话记忆（按服务器分区）──

    def _absorb_conversation_namespace(self, data: dict) -> None:
        """按 `world.server`（ip）→ `world.world_name`（单人）→ unknown 切换记忆分区。

        进出世界都走这里：同一分区复用同一份记忆文件（"把上下文带走"），换服就换文件。
        """
        store = getattr(self, "conversation", None)
        if store is None:
            return
        world = data.get("world") if isinstance(data, dict) else None
        world = world if isinstance(world, dict) else {}
        try:
            store.use_namespace(world.get("server", ""), world.get("world_name", ""))
        except Exception as e:
            logger.warning("[Session] 对话记忆分区切换失败: %s", e)

    def _record_conversation_chat(self, data: dict) -> None:
        """把一条聊天事件写进对话记忆（L1 落盘 + L2 窗口）。

        **权限过滤之前**就记：guest 在公开服只记录不回复，正是设计要的"听得见、不接话"。
        """
        store = getattr(self, "conversation", None)
        if store is None:
            return
        try:
            entry = store.record_chat(
                str(data.get("sender") or "?"),
                str(data.get("message") or ""),
                is_system=bool(data.get("is_system")),
                x=data.get("sender_x"),
                y=data.get("sender_y"),
                z=data.get("sender_z"),
                distance=data.get("sender_distance"),
            )
        except Exception as e:
            logger.warning("[Session] 对话记忆写入失败: %s", e)
            return
        # 死亡话术（system 型 player_chat）→ 记成一条待注入事件（主动参与入口一）。
        if isinstance(entry, dict) and entry.get("type") == "death":
            self._pending_death = {
                "player": str(entry.get("player") or entry.get("sender") or ""),
                "cause": str(entry.get("cause") or ""),
                "text": str(entry.get("message") or ""),
            }

    def _record_death(self, data: dict) -> None:
        """`player_death` 事件（模组的结构化死亡上报）→ 对话记忆 + 待注入事件。"""
        store = getattr(self, "conversation", None)
        position = data.get("position") if isinstance(data, dict) else None
        player = ""
        if isinstance(position, dict):
            player = str(position.get("name") or "")
        if store is not None:
            try:
                store.record_death(player=player, cause=str(data.get("cause") or ""))
            except Exception as e:
                logger.warning("[Session] 记录死亡失败: %s", e)
        self._pending_death = {"player": player, "cause": str(data.get("cause") or ""),
                               "text": "玩家死亡"}

    # ── 聊天 → 回复决策（权限 + 频率门 + 抗抖 + 语义门）──

    def _handle_player_chat(self, data: dict) -> None:
        """一条聊天事件的完整路径：记忆 → 语境 → 在场门 → 权限/频率门 → 语义门。

        tier **不**在这里做分支：能不能下指令、任务权重、记忆可见性都由
        `ReplyGate` 查 `TierPolicy` 表得出（设计文档 §4："别把等级写进决策主链"）。
        """
        sender = str(data.get("sender") or "?")
        message = str(data.get("message") or "")
        is_system = bool(data.get("is_system"))

        # L0 硬过滤：ban 词/正则 → 整条丢弃（连记忆都不进）。
        if self._is_banned(message):
            self._audit_chat({"sender": sender, "tier": "", "decision": "drop",
                              "reason": "ban_match", "effective": 0.0, "threshold": 0,
                              "pending": 0})
            return

        # 对话记忆（L1+L2）与交互库：静默模式 / guest 也要"听见"。
        self._record_conversation_chat(data)
        if not getattr(self, "_chat_tap_registered", False):
            self._note_chat(data)
        try:
            self.message_db.add_message(sender=sender, message=message,
                                        is_system=is_system, is_ai=False)
            if not is_system:
                self.memory.add_interaction(sender=sender, message=message,
                                            response=data.get("response", ""))
        except Exception as e:
            logger.warning("[Session] 聊天入库失败: %s", e)

        # 系统消息（死亡/进出/命令回话）只进记忆：它们不是"人在跟 AI 说话"。
        if is_system:
            return

        # 在场门：不在世界 → 只记录、零 token（设计文档 §1 的静默模式）。
        if not self.llm_active():
            self._audit_chat({"sender": sender, "tier": "", "decision": "silent",
                              "reason": "not_in_world", "effective": 0.0,
                              "threshold": 0, "pending": 0})
            return

        gate = getattr(self, "reply_gate", None)
        if gate is None:
            return
        decision = gate.submit(sender, message, is_system=False,
                               mentioned=self._is_mentioned(message))
        logger.info("[Chat] %s: %s → %s(%s) eff=%.2f thr=%d pending=%d",
                    sender, message[:60], decision.decision, decision.reason,
                    decision.effective, decision.threshold, decision.pending)
        self._audit_chat({
            "sender": sender, "tier": decision.tier, "effective": decision.effective,
            "threshold": decision.threshold, "pending": decision.pending,
            "decision": decision.decision, "reason": decision.reason,
        })
        if decision.decision in {"silent", "drop", "cooldown", "none"}:
            return
        # wait（抗抖窗/频率未达）→ 攒着，由 tick 复查后再说；act → 走语义门。
        self._pending_chat = {"sender": sender, "message": message,
                              "due_at": 0.0, "retries": 0}
        if decision.decision == "act":
            self._pending_chat = None
            self._run_chat_turn(sender, message)

    def _is_mentioned(self, message: str) -> bool:
        """被直接点名（@ 或喊唤醒名）→ 强制进语义门（跳过频率与抗抖）。"""
        text = str(message or "").lower()
        if not text:
            return False
        for name in self._get_wake_names():
            candidate = str(name or "").strip().lower()
            if candidate and (candidate in text or f"@{candidate}" in text):
                return True
        return False

    def _is_banned(self, text: str) -> bool:
        """L0 硬过滤：ban 词/正则命中即整条丢弃（配置在 permissions.json 的 ban_patterns）。"""
        permissions = getattr(self, "permissions", None)
        if permissions is None or not text:
            return False
        try:
            patterns = permissions.get().get("ban_patterns") or []
        except Exception:
            return False
        for pattern in patterns:
            try:
                if re.search(str(pattern), text, re.IGNORECASE):
                    logger.info("[Chat] 命中 ban 规则，丢弃整条: %s", pattern)
                    return True
            except re.error:
                continue
        return False

    def _audit_chat(self, record: dict) -> None:
        """记一轮对话决策（effective/threshold/decision/reason）——"他为什么不理我"不用猜。"""
        entry = dict(record or {})
        entry.setdefault("ts", time.time())
        log = getattr(self, "_chat_audit", None)
        if log is None:
            self._chat_audit = log = []
        log.append(entry)
        del log[:-30]

    def _conversation_recent_lines(self, n: int = 6) -> str:
        """对话记忆窗口的紧凑文本（给语义门看"这话是对谁说的"）。"""
        store = getattr(self, "conversation", None)
        if store is None:
            return ""
        try:
            recent = store.recent(n)
        except Exception:
            return ""
        lines = []
        for entry in recent:
            if not isinstance(entry, dict):
                continue
            kind = str(entry.get("type") or "chat")
            who = str(entry.get("sender") or "?")
            text = str(entry.get("message") or "")
            if kind == "chat":
                lines.append(f"{who}: {text}")
            else:
                lines.append(f"[{kind}] {text}")
        return "\n".join(lines)

    def _run_chat_turn(self, sender: str, message: str, retry: bool = False) -> Optional[str]:
        """过语义门并（可能）回复。语义门 = 本轮唯一"看语境"的廉价 LLM 调用。"""
        verdict = self._semantic_gate(sender, message)
        decision = self.reply_gate
        if decision is not None and verdict == "no_action":
            try:
                decision.mark_no_action()
            except Exception as e:
                logger.warning("[Session] 置 no_action 冷却失败: %s", e)
        self._audit_chat({"sender": sender, "tier": "", "decision": verdict,
                          "reason": "semantic_gate", "effective": 0.0,
                          "threshold": 0, "pending": 0})
        if verdict == "no_action":
            self._pending_chat = None
            logger.info("[Chat] 语义门判定 no_action（8s 冷却），本轮不回复")
            return None
        if verdict == "wait":
            if not retry:
                self._pending_chat = {"sender": sender, "message": message,
                                      "due_at": time.time() + self.WAIT_RETRY_SECONDS,
                                      "retries": 0}
            else:
                self._pending_chat = None
            logger.info("[Chat] 语义门判定 wait（可能还在打字）")
            return None

        self._pending_chat = None
        self._actor = {"name": sender, "tier": self._tier_of(sender)}
        self._manual_command_until = max(self._manual_command_until, time.time() + 20.0)
        try:
            response = self.handle_chat(sender, message, command_context="manual_chat")
        except Exception as e:
            logger.error("[Chat] LLM error: %s", e)
            return None
        if not response:
            return None
        logger.info("[Chat] AI response: %s", response[:80])
        try:
            with self.skills.command_context("chat_reply"):
                self.skills.send_chat(response)
            self.message_db.add_message(sender="AI", message=response,
                                        is_system=False, is_ai=True)
            self.memory.add_interaction(sender="AI", message=response, response="")
            store = getattr(self, "conversation", None)
            if store is not None:
                store.record_chat(self._get_bot_name(), response)
        except Exception as e:
            logger.warning("[Chat] 回复落库/落记忆失败: %s", e)
        return response

    def _semantic_gate(self, sender: str, message: str,
                       event: Optional[dict] = None) -> str:
        """L4 语义门：{continue, wait, no_action} —— "他是在跟我说话还是在跟别人说话"。

        - 外部事件注入（`event` 非空）也走这里：**绝不**"事件 → 必发消息"。
        - 失败/未配置 LLM 一律回 continue（门坏了不该把回话也一起堵死）。
        """
        llm = getattr(self, "llm", None)
        if llm is None or not getattr(llm, "api_key", None):
            return "continue"
        if not self._semantic_gate_enabled():
            return "continue"
        bot_name = self._get_bot_name()
        history = self._conversation_recent_lines(6)
        if event:
            payload = json.dumps(event, ensure_ascii=False)
            subject = (f"这不是玩家直接发的话，而是后端注入的外部事件：{payload}\n"
                       f"你可以继续（真的想说点什么）或 no_action（不说话）。")
            rules = ("continue=值得说一句；wait=先看看；no_action=没什么好说的，别说。")
        else:
            subject = f"新消息 —— {sender}: {message}"
            rules = ("continue=他现在在跟 {bot} 说话，该接；"
                     "wait=他可能还在打字/话没说完，等一下；"
                     "no_action=他不是在跟 {bot} 说话，或现在不该插话。").format(bot=bot_name)
        prompt = (
            f'你是"{bot_name}"的接话判断器。只输出一行 JSON，不要解释：\n'
            '{"decision": "continue|wait|no_action"}\n'
            f"{rules}\n\n"
            f"最近对话（按时间从早到晚）：\n{history or '（还没人说过话）'}\n\n"
            f"{subject}\n"
        )
        try:
            result = llm.chat([{"role": "user", "content": prompt}], agent="gate",
                              temperature=0.2, max_tokens=24, thinking=False,
                              timeout=self.SEMANTIC_GATE_TIMEOUT)
        except Exception as e:
            logger.warning("[Session] 语义门调用失败(%s)，按 continue 处理", e)
            return "continue"
        return self._parse_semantic_verdict((result or {}).get("content", ""))

    @staticmethod
    def _parse_semantic_verdict(text: Any) -> str:
        """解析语义门输出；认不出 → continue（宁可多接一句，也别把门卡死）。"""
        lowered = str(text or "").strip().lower()
        if not lowered:
            return "continue"
        for verdict in ("no_action", "wait", "continue"):
            if f'"{verdict}"' in lowered or f"'{verdict}'" in lowered:
                return verdict
        if "no_action" in lowered:
            return "no_action"
        if lowered.startswith("wait") or " wait" in lowered:
            return "wait"
        return "continue"

    def _semantic_gate_enabled(self) -> bool:
        """语义门开关（config.json → decision.semantic_gate，默认开）。"""
        try:
            return bool(get_config_store(DEFAULT_CONFIG_PATH).get_semantic_gate())
        except Exception:
            return True

    def _tier_of(self, name: str) -> str:
        permissions = getattr(self, "permissions", None)
        if permissions is None:
            return "guest"
        try:
            return permissions.tier_of(name)
        except Exception:
            return "guest"

    def _tick_conversation_gate(self) -> None:
        """抗抖窗过完 + 频率达标时补触发；语义门说 wait 时按上限自续。"""
        pending = self._pending_chat
        if not pending:
            return
        due_at = float(pending.get("due_at") or 0.0)
        if due_at and time.time() >= due_at:
            # 语义门 wait 的自续（有上限，绝不无限重试）。
            if int(pending.get("retries") or 0) >= self.WAIT_MAX_RETRIES:
                self._pending_chat = None
                return
            pending["retries"] = int(pending.get("retries") or 0) + 1
            pending["due_at"] = 0.0
            self._run_chat_turn(pending["sender"], pending["message"], retry=True)
            return
        if due_at:
            return
        gate = getattr(self, "reply_gate", None)
        if gate is None:
            return
        decision = gate.poll()
        if decision is None or decision.decision != "act":
            return
        self._pending_chat = None
        self._audit_chat({
            "sender": pending.get("sender", ""), "tier": decision.tier,
            "effective": decision.effective, "threshold": decision.threshold,
            "pending": decision.pending, "decision": "act",
            "reason": decision.reason,
        })
        self._run_chat_turn(pending["sender"], pending["message"])

    # ── LLM-driven conversation ──

    def _merged_persona(self) -> dict:
        """人设合并视图：`.local/persona.json`（三段） + `runtime["persona"]`（legacy/UI）。

        两边重叠的键（name / personality / speaking_style）以 runtime 为准 —— 它就是
        server 端按同一优先级合成后放进去的；`.local/persona.json` 提供 identity /
        reply_style / scene_rules 三段的正文。
        """
        merged: dict = {}
        store = getattr(self, "persona", None)
        if store is not None:
            try:
                merged.update(store.get())
            except Exception as e:
                logger.debug("[Session] 人设读取失败: %s", e)
        runtime = getattr(self, "runtime", None)
        persona = runtime.get("persona") if isinstance(runtime, dict) else None
        if isinstance(persona, dict):
            merged.update({k: v for k, v in persona.items()
                           if v not in (None, "", [], {})})
        return merged

    def _scene_for(self, sender: str = "", mentioned: bool = False) -> str:
        """当前场景：被点名 > 私聊（private 服） > 公开服。"""
        if mentioned or self._is_mentioned(sender):
            return "mentioned"
        permissions = getattr(self, "permissions", None)
        try:
            mode = permissions.server_mode() if permissions is not None else "private"
        except Exception:
            mode = "private"
        return "private" if mode == "private" else "public"

    def _state_for_persona(self) -> dict:
        """state_block 的原料：生命/饥饿/饱和度/当前任务/在听谁说话（与 identity 分离）。"""
        runtime = getattr(self, "runtime", None)
        runtime = runtime if isinstance(runtime, dict) else {}
        player = runtime.get("player") if isinstance(runtime.get("player"), dict) else {}
        speaker = getattr(self, "last_speaker", None)
        speaker = speaker if isinstance(speaker, dict) else {}
        return {
            "health": player.get("health"),
            "max_health": player.get("max_health", 20),
            "hunger": player.get("hunger", runtime.get("food")),
            "saturation": runtime.get("saturation", player.get("saturation")),
            "task": runtime.get("task_state") if isinstance(runtime.get("task_state"), dict) else {},
            "listening_to": speaker.get("name", ""),
        }

    def persona_blocks(self, scene: Optional[str] = None) -> dict:
        """人设四块（identity / reply_style / scene_rules / state_block）—— 各自独立。

        取不到 persona store 时给空块（prompt 侧有 legacy 兜底），绝不抛。
        """
        store = getattr(self, "persona", None)
        if store is None:
            return {"identity": "", "reply_style": "", "scene_rules": "", "state_block": ""}
        try:
            return store.prompt_blocks(self._state_for_persona(), scene or self._scene_for())
        except Exception as e:
            logger.warning("[Session] 人设块构建失败: %s", e)
            return {"identity": "", "reply_style": "", "scene_rules": "", "state_block": ""}

    def _get_bot_name(self) -> str:
        """获取 AI 的名字。"""
        persona = self._merged_persona()
        if persona.get("name"):
            return str(persona["name"])
        player = self.runtime.get("player", {}) if isinstance(self.runtime, dict) else {}
        return player.get("name", "AI")

    def _get_wake_names(self) -> list[str]:
        persona = self._merged_persona()
        return _wake_names_from(persona, self._get_bot_name())

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

        # 人设：三段自由文本 + 与 identity 分离的 state_block（prompt 里是独立块）。
        context["persona_blocks"] = self.persona_blocks()
        # 对话记忆（按服务器分区）：L3 摘要 + 关键词 + L2 窗口。
        store = getattr(self, "conversation", None)
        if store is not None:
            try:
                context["conversation"] = store.describe(self._players_count())
            except Exception as e:
                logger.debug("[Session] 对话记忆读取失败: %s", e)
                context["conversation"] = {}
        context["in_world"] = self.llm_active()
        return context

    def _players_count(self) -> int:
        """在线人数（决定窗口口径：单人/私聊 60，多人 40）。"""
        players = self.runtime.get("players") if isinstance(self.runtime, dict) else None
        return len(players) if isinstance(players, list) else 1

    def handle_chat(self, sender: str, message: str, command_context: str = "chat",
                    event: Optional[dict] = None) -> Optional[str]:
        """
        Process a chat message through the LLM.
        Uses Planner for intelligent action planning.

        在场门：不在世界里一律不动 LLM（返回 None）。`event` 非空表示这是外部事件注入，
        会以"自行决定是否回复"的形式交给同一个 planner（不是"必发消息"）。
        """
        if not self.llm_active():
            logger.info("[Session] 不在世界（界面 %s）→ 跳过 LLM 规划", self.ui_screen or "?")
            return None
        context = self._runtime_context()
        if event:
            context["event"] = event
        # 场景：这条消息点名了 AI 就用"被点名"那一段规则，否则按私聊/公开服。
        mentioned = (not event) and self._is_mentioned(message)
        context["persona_blocks"] = self.persona_blocks(self._scene_for("", mentioned))

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
        # 在场门最先判：不在世界时只做"零 token、零下发"的事（收事件由 handle_event 负责）。
        if not self.llm_active():
            self._tick_suspended()
            return

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

        # 对话门：抗抖窗过完 / 频率达标时补触发（回复决策的第二拍）。
        self._tick_conversation_gate()

        # 主动参与：外部事件注入（death/被围攻/稀有掉落/新群系/任务完成或失败）。
        self._tick_events()

        # 对话记忆 L3：窗口裁切过就生成一条中期摘要（落盘，重启靠它 + 原始事件回灌）。
        self._tick_memory_summary()

        # Auto-save
        now = time.monotonic()
        if now - self._last_save > 120:
            self._last_save = now
            self.memory.save()
            self.poi.save()

    def _tick_suspended(self) -> None:
        """不在世界时的 tick：**零 token、零下发**，只维持落盘与在途动作的如实中止。

        - 人已经在界面里（暂停菜单/连接界面），任何在途动作都不可能完成 → 直接中止；
        - 不发任何 wire 帧（含控制流心跳）：按键已由 `_on_leave_world` 本地清空；
        - 聊天/死亡/进出仍由 handle_event 收进对话记忆（静默模式）。
        """
        action_manager = getattr(self, "action_manager", None)
        if action_manager is not None:
            try:
                if action_manager.is_busy:
                    action_manager.stop()
            except Exception as e:
                logger.debug("[Session] 静默模式中止动作失败: %s", e)
        self._pending_chat = None
        memory = getattr(self, "memory", None)
        poi = getattr(self, "poi", None)
        now = time.monotonic()
        if now - getattr(self, "_last_save", 0.0) > 120:
            self._last_save = now
            for saver in (memory, poi):
                save = getattr(saver, "save", None)
                if callable(save):
                    try:
                        save()
                    except Exception as e:
                        logger.debug("[Session] 静默模式落盘失败: %s", e)

    # ── 主动参与（外部事件注入）──

    def inject_event(self, kind: str, intent: str, event: Optional[dict] = None,
                     context: Optional[dict] = None, now: Optional[float] = None) -> bool:
        """外部事件注入：包装成 `{kind:"event", intent, event, context}` 走**同一个语义门**。

        返回是否真的产生了一次（可能说话的）规划。语义门可以判 no_action —— 这是设计
        明确要求的"绝不提供事件→必发消息的短路"，否则必然刷屏。
        """
        if not self.llm_active():
            return False
        current = time.time() if now is None else now
        cooldown = float(self.EVENT_COOLDOWNS.get(kind, 60.0))
        last_same = float(self._event_cooldowns.get(kind, 0.0))
        if current - last_same < cooldown or current - float(self._last_event_at) < self.EVENT_MIN_GAP:
            return False
        payload = {
            "kind": "event",
            "intent": str(intent or ""),
            "event": event if isinstance(event, dict) else {},
            "context": context if isinstance(context, dict) else {},
        }
        self._event_cooldowns[kind] = current
        self._last_event_at = current
        verdict = self._semantic_gate("(事件)", str(intent or ""), event=payload)
        response = None
        if verdict == "continue":
            response = self._run_event_turn(kind, payload)
        self._audit_event({"kind": kind, "intent": str(intent or ""), "verdict": verdict,
                           "spoke": bool(response), "reason": "semantic_gate",
                           "ts": current})
        return bool(response)

    def _run_event_turn(self, kind: str, payload: dict) -> Optional[str]:
        """事件过完语义门（continue）后才真正交给 planner —— 依旧可以不说话。"""
        try:
            response = self.handle_chat("(事件)", str(payload.get("intent") or ""),
                                        command_context="event", event=payload)
        except Exception as e:
            logger.warning("[Session] 事件注入规划失败: %s", e)
            return None
        if not response:
            return None
        try:
            with self.skills.command_context("chat_reply"):
                self.skills.send_chat(response)
            store = getattr(self, "conversation", None)
            if store is not None:
                store.record_chat(self._get_bot_name(), response)
        except Exception as e:
            logger.warning("[Session] 事件回复下发失败: %s", e)
        return response

    def _audit_event(self, record: dict) -> None:
        entry = dict(record or {})
        entry.setdefault("ts", time.time())
        log = getattr(self, "_event_audit", None)
        if log is None:
            self._event_audit = log = []
        log.append(entry)
        del log[:-20]

    def _tick_events(self) -> None:
        """每拍看一遍有没有值得"说一句"的事件（每拍最多注入一个，全部可 no_action）。

        只有真的注入出去了才消费事件状态（`consume()`）：否则被限频/语义门挡掉的那条会在
        下一拍重试（此时已被同类冷却拦住，不会再烧 token），而**不会**因为排在前面就把
        后面的候选（新群系/任务失败）吃掉。
        """
        candidates = self._event_candidates()
        if not candidates:
            return
        kind, intent, event, consume = candidates[0]
        if self.inject_event(kind, intent, event=event,
                             context={"recent_chat": self._conversation_recent_lines(4)}):
            consume()

    def _event_candidates(self) -> list[tuple[str, str, dict, Any]]:
        """候选事件 [(kind, intent, event, consume)]，按优先级排序，**本函数不改任何状态**。

        判定规则：
          - death：模组 `player_death` 事件，或 system 型聊天里的死亡话术（中英文，见
            `conversation.classify_system_message`）；自己死了不调侃。
          - besieged：战斗反射被触发（`reflex_combat`）且血量不健康。
          - rare_drop：附近掉落物里有稀有 id（每个 id 只报一次）。
          - new_biome：`world.biome` / 顶层 `biome` 变化（每换一个新群系一次）。
          - task_done / task_failed：task_state 的终态迁移（含失败原因）。
        """
        runtime = self.runtime if isinstance(self.runtime, dict) else {}
        bot_name = self._get_bot_name()
        out: list[tuple[str, str, dict, Any]] = []

        death = getattr(self, "_pending_death", None)
        if isinstance(death, dict) and death.get("player"):
            who = str(death["player"])
            if who != bot_name:
                cause = str(death.get("cause") or "").strip()
                intent = f"{who} 死了" + (f"（被{cause}）" if cause else "")

                def _consume_death() -> None:
                    self._pending_death = None

                out.append(("death", intent, {"player": who, "cause": cause}, _consume_death))

        player = runtime.get("player") if isinstance(runtime.get("player"), dict) else {}
        health = player.get("health")
        if runtime.get("reflex_combat") and isinstance(health, (int, float)) and health <= 10:
            out.append(("besieged", "我正被围攻，血不多了", {"health": health}, lambda: None))

        for entity in runtime.get("entities") or []:
            if not isinstance(entity, dict) or entity.get("type") != "item":
                continue
            item_id = str(entity.get("item_id") or entity.get("name") or "")
            local = item_id.rsplit(":", 1)[-1].lower()
            if any(rare in local for rare in self.RARE_DROP_IDS) and item_id not in self._seen_drop_ids:
                def _consume_drop(dropped: str = item_id) -> None:
                    self._seen_drop_ids.add(dropped)

                out.append(("rare_drop", f"掉到了稀有的 {item_id}", {"item_id": item_id},
                            _consume_drop))
                break

        world = runtime.get("world") if isinstance(runtime.get("world"), dict) else {}
        biome = str(runtime.get("biome") or world.get("biome") or "")
        if biome and biome != self._last_biome:
            def _consume_biome(seen: str = biome) -> None:
                self._last_biome = seen

            out.append(("new_biome", f"第一次来到 {biome}", {"biome": biome}, _consume_biome))

        task_event = getattr(self, "_last_task_event", None)
        if isinstance(task_event, dict) and task_event.get("status") in {"done", "failed"}:
            failed = task_event.get("status") == "failed"
            reason = str(task_event.get("reason") or "")
            intent = (f"任务 {task_event.get('kind')} 失败了" + (f"（{reason}）" if reason else "")
                      if failed else f"任务 {task_event.get('kind')} 完成了")

            def _consume_task() -> None:
                self._last_task_event = {}

            out.append(("task_failed" if failed else "task_done", intent, dict(task_event),
                        _consume_task))

        return out

    # ── 对话记忆摘要（L3）──

    # 摘要是最贵的一步（要过 LLM），所以限频：窗口裁切后最多每 60 秒合并一次。
    MEMORY_SUMMARY_INTERVAL = 60.0

    def _tick_memory_summary(self) -> Optional[dict]:
        """窗口裁切攒下的旧事件 → 一次中期摘要（{summary, keywords}）落盘。"""
        store = getattr(self, "conversation", None)
        if store is None:
            return None
        try:
            pending = store.pending_summary()
        except Exception as e:
            logger.debug("[Session] 读取待摘要失败: %s", e)
            return None
        if not pending:
            return None
        now = time.time()
        if now - float(getattr(self, "_last_memory_summary", 0.0)) < self.MEMORY_SUMMARY_INTERVAL:
            return None
        self._last_memory_summary = now
        try:
            result = store.summarize_pending()
        except Exception as e:
            logger.warning("[Session] 对话记忆摘要失败: %s", e)
            return None
        if result:
            logger.info("[Session] 对话记忆摘要已生成(%d 条): %s", len(pending),
                        str(result.get("summary") or "")[:60])
        return result

    # ── 任务列表（多任务 + 归属 + 优先级 + 状态 + 进度）──

    # wire 指令 → 任务列表里的 kind。只登记"有始有终"的活儿；
    # 瞬时/查询类指令（stop_all / get_inventory / set_hud_note / remember_poi…）不登记，
    # 否则列表会被一闪而过的小动作刷满。
    TASK_KINDS = {
        "move_to": "move", "follow_player": "follow", "collect_blocks": "collect",
        "craft_item": "craft", "build": "build", "explore": "explore",
        "eat": "eat", "sleep": "sleep", "trade": "trade",
        "go_to_block": "collect", "mine_block": "mine", "guard": "guard",
        "locate_structure": "locate",
    }

    def _register_task_for_command(self, cmd: str, args: dict, context: str = "") -> None:
        """把一条**玩家下令**的动作登记成任务（归属 = 当前对话发起者的 tier + 名字）。

        自主行为（mode / self_prompt / auto_sleep / 事件）不在这里登记 —— 那些没有"主人"，
        硬套上一个名字会把别人的任务列表弄脏；它们只在模组回报 task_state 时才出现在列表里。
        """
        tasks = getattr(self, "tasks", None)
        if tasks is None or context not in {"chat", "manual_chat"}:
            return
        kind = self.TASK_KINDS.get(str(cmd))
        if not kind:
            return
        actor = getattr(self, "_actor", None) or {}
        detail = ", ".join(f"{key}={value}" for key, value in (args or {}).items())[:200]
        try:
            # 刚下发的指令按"进行中"登记：这样 task_state 的进度/终态能挂回同一条任务
            # （停在 pending 的话，终态事件会找不到它，进度也挂不上）。
            tasks.add(kind, owner=str(actor.get("name") or ""),
                      owner_tier=str(actor.get("tier") or "guest"), detail=detail,
                      status="active")
        except Exception as e:
            logger.debug("[Session] 任务登记失败(%s): %s", cmd, e)

    def _absorb_task_state(self, data: dict) -> None:
        """模组上报的 task_state → 任务列表（进度/终态/失败原因），供 /api/status 展示。"""
        tasks = getattr(self, "tasks", None)
        if tasks is None or not isinstance(data, dict):
            return
        kind = str(data.get("kind") or "").strip()
        status = str(data.get("status") or "").strip().lower()
        if not kind or kind == "idle":
            return
        detail = str(data.get("detail") or "")
        reason = str(data.get("reason") or data.get("error") or "")
        progress = data.get("progress")
        try:
            progress_value = None if progress is None else float(progress)
        except (TypeError, ValueError):
            progress_value = None
        # 按 kind 找"当前这条活跃任务"：不用 owner 匹配 —— 自主/反射行为没有主人，
        # 硬拿对话发起者去配会凭空多出一条重复任务。
        target = None
        for item in tasks.items():
            if item.kind == kind and item.status == "active":
                target = item
                break
        terminal = {"done": "done", "failed": "failed", "error": "failed",
                    "cancelled": "cancelled"}.get(status)
        try:
            if terminal:
                if target is None:
                    # 列表里没有对应任务（比如自主行为）：不为一个终态凭空造一条已完成任务。
                    return
                tasks.update(target.id, status=terminal,
                             progress=1.0 if terminal == "done" else progress_value,
                             failure_reason=reason if terminal == "failed" else "")
                self._last_task_event = {"kind": kind, "status": terminal, "reason": reason}
                return
            if target is not None:
                fields: dict = {"status": "active"}
                if progress_value is not None:
                    fields["progress"] = progress_value
                if detail:
                    fields["detail"] = detail
                tasks.update(target.id, **fields)
            else:
                # 玩家没直接下过令（自主/反射/控制台）→ 无主任务、最低权重；
                # 硬套"最近发话者"会把别人的任务列表弄脏。
                tasks.add(kind, owner="", owner_tier="guest", detail=detail,
                          status="active", progress=progress_value or 0.0)
        except Exception as e:
            logger.debug("[Session] 任务列表同步失败: %s", e)

    def cancel_task(self, task_id: str, *, actor: str = "",
                    tier: Optional[str] = None) -> tuple[bool, str]:
        """取消任务（权限由 TaskList 的 tier 规则判定：master 任意 / friend 只管自己的）。

        `tier` 只有本地控制台（仪表盘）才显式传 master；其它调用一律按 `actor` 查权限。
        """
        tasks = getattr(self, "tasks", None)
        if tasks is None:
            return False, "no_task_list"
        return tasks.cancel(task_id, actor_tier=tier or self._tier_of(actor), actor=actor)

    def activate_task(self, task_id: str, *, actor: str = "",
                      tier: Optional[str] = None) -> tuple[bool, str]:
        """插队（master 可插队任意任务；friend 只能动自己的）。"""
        tasks = getattr(self, "tasks", None)
        if tasks is None:
            return False, "no_task_list"
        return tasks.activate(task_id, actor_tier=tier or self._tier_of(actor), actor=actor)

    def _handle_self_prompt(self, prompt: str):
        """Handle a self-prompt by routing through LLM to generate actions."""
        if not self.llm_active():
            # 在场门的第二道保险：不在世界里连自我提示都不许花 token。
            logger.info("[Session] 不在世界 → 跳过 self-prompt")
            return
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
        tasks = getattr(self, "tasks", None)
        gate = getattr(self, "reply_gate", None)
        store = getattr(self, "conversation", None)
        persona = getattr(self, "persona", None)
        in_world = bool(getattr(self, "in_world", True))
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
            # M3-19：多任务列表（保留上面的 task 字段，前端两者都用）。
            "task_list": tasks.status() if tasks is not None else {"items": [], "active": None,
                                                                   "count": 0},
            "hud_note": self._hud_note(),
            "last_speaker": dict(getattr(self, "last_speaker", {}) or {}),
            # 在场门：仪表盘要能直接看出"未在游戏中（界面名）"，别显示假的动作状态。
            "in_world": in_world,
            "ui_screen": str(getattr(self, "ui_screen", "") or ""),
            "presence": {
                "in_world": in_world,
                "ui_screen": str(getattr(self, "ui_screen", "") or ""),
                "since": getattr(self, "_presence_changed_at", None),
                "llm_paused": not in_world,
                # 仪表盘直接显示的文案：能一眼看出"未在游戏中（界面名）"。
                "label": ("在世界里" if in_world else
                          f"未在游戏中（界面：{str(getattr(self, 'ui_screen', '') or '?')}）"),
            },
            # 回复决策门：effective/threshold/decision/reason 全在这儿（"他为什么不理我"）。
            "gate": gate.status() if gate is not None else {},
            "chat_decisions": list(getattr(self, "_chat_audit", []) or [])[-10:],
            # 对话记忆分区与窗口（L1/L2/L3）。
            "conversation": store.status() if store is not None else {},
            # 主动参与：最近注入的事件及结果（含 no_action）。
            "events": {
                "recent": list(getattr(self, "_event_audit", []) or [])[-10:],
                "pending_death": bool(getattr(self, "_pending_death", None)),
            },
            "persona_sections": persona.status() if persona is not None else {},
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
        # 任务列表：把玩家下发的活儿登记成带归属/优先级的任务（/api/status 的 task_list）。
        self._register_task_for_command(cmd, args, context)
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
