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
from .conversation import ConversationStore, server_key
from .borrow import BorrowRulesStore, parse_borrow_grant, parse_borrow_revoke
from .person_memory import PersonMemoryStore
from .world_memory import WorldMemoryStore, plan_replay, route_args
from .territory import (TerritoryStore, current_base, home_situation, plan_zone_replay,
                        point_of)
from .permissions import CraftPolicy, PermissionsStore, ReplyGate
from .persona_store import PersonaStore
from .task_list import TaskList
from .audit_log import AuditLog
from .command_gate import CommandGate, CommandsStore
from .contacts import ContactStore, WhisperDetector
from .outbound import sanitize_outbound
from .player_directory import resolve_player as resolve_player_name
from .tpa import TpaCoordinator, TpaDetector

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
        self.commands = Commands(self.skills, authorizer=self._autonomous_craft_policy)
        self.modes_engine = ModesEngine(self.skills, self.memory,
                                        action_manager=self.action_manager)
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
        # 借用规则记忆（按 server 分区）：对话里的"可以用仓库里的铁"落成带条件的规则。
        self.borrow_rules = BorrowRulesStore()
        self._borrow_rule_audit: list[dict] = []
        # 四层记忆（global / person / server / world）里属于 world 层的两份：
        # 站点、路径（坐标只在同一世界同一维度里有效，见 world_memory）。
        self.places = WorldMemoryStore()
        # 领地 / 基地 / 容器 / 区域（同样的 world 层分区：坐标只在这里）。
        self.territories = TerritoryStore()
        # 回灌时"模组只报点数、后端没几何"的路径 → 记下 route_export 的请求 id，
        # 等回执（`command_response`）拿到点列再推回去（拿不到就维持 no_geometry）。
        self._route_exports: dict[str, dict] = {}
        # 最近被区域硬拦的记录（`blocked_by_zone`）：进 L2 上下文 + 归入任务失败原因。
        self._zone_blocks: list[dict] = []
        # "离家多久"的计时（进基地范围就清零；只上报，绝不自己偷跑回去）。
        self._home_away_since: Optional[float] = None
        # person 层：跨服的"同一个人"稳定事实（uuid 优先，名字兜底 + 降级打标）。
        self.people = PersonMemoryStore()
        # 已回灌/已激活的层标记（`world:<key>` / `server:<key>` / `person:<key>` / global）：
        # 同一 session 内同一层只推一次。
        self._replay_layers: set[str] = set()
        # 这一 session 里"从聊天认出来的"（名字 → (uuid, 当时的 server_key)）。
        self._people_seen: dict[str, tuple[str, str]] = {}
        # 当前在场玩家的 person 稳定事实（名字 → facts），供 prompt 用（只读）。
        self._present_person_facts: dict[str, dict] = {}
        # 任务列表（多任务 + 归属 + 优先级 + 状态 + 进度）
        self.tasks = TaskList()

        # ── 出站/指令安全（这一批新增）──
        # 统一安全审计：出站清洗 / 指令闸门 / TPA 都写到这里（内存环形 + 落盘）。
        self._security_audit: list[dict] = []
        self._security_log: Optional[AuditLog] = None
        # 出站清洗接到本 session（Skills 的每个出站函数都会调用它）。
        self.skills.set_outbound_observer(self._on_security_audit)
        # 指令白名单（唯一来源 `.local/commands.json`，**默认空**）+ 硬黑名单 + 二次确认。
        self.commands_store = CommandsStore()
        self.command_gate = CommandGate(self.commands_store, audit=self._on_security_audit)
        # 私信通讯录（server:<key> + person:<uuid> 两层分区）与接收识别。
        self.contacts = ContactStore()
        self.whisper_detector = WhisperDetector(
            config_provider=lambda: (self.commands_store.get().get("whisper") or {}))
        # TPA 两路径 + 分层策略（策略读 `.local/permissions.json` 的 `tpa` 段）。
        self.tpa_detector = TpaDetector(config_provider=self.commands_store.tpa_config)
        self.tpa = TpaCoordinator(
            self.tpa_detector, policy_provider=self.permissions.tpa_policy,
            command=self.run_server_command,
            click=self._click_screen_button,
            resolve=self.resolve_player,
            audit=self._on_security_audit,
        )
        # 本轮的 TPA 上下文：最近一次收到的请求（触发词不带人名时用它）+
        # 本轮是否已经私信过（避免"工具发了 + 回复又自动发一遍"）。
        self._last_tpa_request: Optional[dict] = None
        self._whisper_sent_this_turn: set[str] = set()
        self._reply_whisper_to: str = ""
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
            # 能力位（craft / containers / autonomy_craft）：合成与翻箱都要先过这张表。
            permissions=self.permissions,
            # 借用规则（对话授权"仓库里的铁可以用"）：计划器只查表，不看 tier 分支。
            borrow_rules=self.borrow_rules,
            # 领地 / 基地 / 容器 / 区域（world 层分区）：授权判定与回灌都在计划器里。
            territory=self.territories,
            # 单一指令口 + 私信 + TPA：全部走 session 的方法（规则只在这里实现一次）。
            server_commands=self.run_server_command,
            whisper_sender=self.send_whisper,
            tpa_responder=self.respond_tpa,
            tpa_requester=self.request_tpa,
            player_resolver=self.resolve_player,
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
                # 站点/路径（world 层）：落盘 + 模组侧空了就回灌。
                self._absorb_places(data)
                # 领地/基地/容器/区域（同样的 world 层）：区域声明吸收 + 按需回灌。
                self._absorb_territory(data)
                # person 层：把在场的人与已认出的 uuid 对上号（只读，不写一次性上下文）。
                self._absorb_people(data)
                # 容器内容变了（含归还后的快照）→ 用真实快照核对借用是否真的放回去了。
                if isinstance(data, dict) and "open_container" in data:
                    snapshot = data.get("open_container") or {}
                    self._verify_borrow_returns(snapshot)
                    # 搬运的**两侧对账**也在这条路上：搬前/搬后 items 对比，不符就如实报。
                    self._verify_haul(snapshot)
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
                # 区域硬拦（`blocked_by_zone`）：如实转达"哪个区域、哪条规则"，并归入任务失败原因。
                self._note_zone_block(data)
                # `route_export` 的回执：拿到点列就补存盘并推回去（拿不到维持 no_geometry）。
                self._absorb_route_export(data)
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
                # 任务真的结束/失败/被取消 → 借用的东西该还了（"不要随身携带"）。
                # idle 不算：Java 在 craft/collect 各步之间也会报 idle。
                if status in {"done", "failed", "cancelled", "error"}:
                    self._settle_borrows(f"task_{status}")
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
    # 收到 TPA 请求后，多久之内说"接受/拒绝"还算是在回应它（超了就当普通聊天）。
    TPA_REQUEST_TTL = 120.0
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

    # ── 借用：规则性记忆 / 结算 / 核对 ─────────────────────────────

    def _absorb_borrow_speech(self, sender: str, message: str) -> None:
        """把一句口头**授权/收回**落成规则（按 server 分区持久化）。

        "你可以使用仓库里所有铁，因为我们有刷铁机" → 授权物品＝铁类；
        条件＝用完归还 / 不随身携带（规则里写死，prompt 与台账都按它执行）。
        "别用仓库里的铁了" → 收回那条授权（只认能授权翻箱的人说的话，查能力表）。
        """
        store = getattr(self, "borrow_rules", None)
        if store is None:
            return
        change = parse_borrow_revoke(message)
        action = "revoked"
        if change is None:
            change = parse_borrow_grant(message)
            action = "granted"
        if change is None:
            return
        tier = self._tier_of(sender)
        permissions = getattr(self, "permissions", None)
        allowed = False
        if permissions is not None:
            try:
                allowed = permissions.can_authorize_containers(tier)
            except Exception as e:
                logger.warning("[Borrow] 授权人能力位查询失败: %s", e)
        if not allowed:
            logger.info("[Borrow] %s(%s) 说了仓库%s，但没有翻箱授权，不落规则",
                        sender, tier, "收回" if action == "revoked" else "授权")
            self._borrow_rule_audit.append({
                "ts": time.time(), "sender": sender, "tier": tier,
                "family": change.get("family"), "result": "denied",
                "reason": "no_container_authority", "source": change.get("source", ""),
            })
            del self._borrow_rule_audit[:-20]
            return
        try:
            if action == "revoked":
                rule = store.revoke(change)
                if rule is None:
                    return  # 本来就没这条授权，不用记一条空动作
            else:
                rule = store.grant(change, granted_by=sender, granted_tier=tier,
                                   source=change.get("source", ""))
        except Exception as e:
            logger.warning("[Borrow] 落规则失败(%s): %s", action, e)
            return
        self._borrow_rule_audit.append({
            "ts": time.time(), "sender": sender, "tier": tier,
            "family": rule.get("family"), "result": action,
            "rule_id": rule.get("id"), "source": change.get("source", ""),
        })
        del self._borrow_rule_audit[:-20]
        logger.info("[Borrow] %s(%s) %s 借用规则 %s", sender, tier, action, rule.get("id"))

    def _borrow_context(self) -> dict:
        """结算/核对用的小上下文（只带台账与归还需要的字段，不惊动 LLM）。"""
        runtime = getattr(self, "runtime", {}) or {}
        context = {
            "open_container": runtime.get("open_container") or {},
            "inventory": runtime.get("inventory", []),
            "known_containers": [],
        }
        try:
            context["known_containers"] = self.poi.find("storage")
        except Exception as e:
            logger.debug("[Session] 借用结算读容器清单失败: %s", e)
        context["nearby_storage"] = runtime.get("nearby_storage", [])
        # 玩家位置：搬运算"到没到箱子跟前"要看它（借用那条路不看，多给一份无副作用）。
        context["player"] = runtime.get("player", {})
        context["actor_tier"] = str((getattr(self, "_actor", {}) or {}).get("tier") or "guest")
        return context

    def _settle_borrows(self, reason: str) -> dict:
        """任务结束/失败/取消/被中断/玩家接管 → 自动归还剩余并报出回不去的。"""
        if getattr(self, "_borrow_settling", False):
            # 结算里下发 container_move 会再回到命令观察者，防重入。
            return {}
        planner = getattr(self, "planner", None)
        if planner is None:
            return {}
        self._borrow_settling = True
        try:
            result = planner.settle_borrows(reason, self._borrow_context())
        except Exception as e:
            logger.warning("[Session] 借用结算失败(%s): %s", reason, e)
            return {}
        finally:
            self._borrow_settling = False
        alive = [row for row in ("sent", "pending", "failed") if result.get(row)]
        if alive:
            logger.info("[Session] 借用结算(%s)：%s", reason, result.get("note"))
        return result

    def _auto_return_step(self) -> bool:
        """空闲时替未还清的借用推进一步（没打开容器就过去/开箱，开着就放回）。"""
        planner = getattr(self, "planner", None)
        if planner is None or getattr(self, "_borrow_settling", False):
            return False
        try:
            if not planner.borrow_ledger.outstanding():
                return False
            self._borrow_settling = True
            return bool(planner.auto_return_step(self._borrow_context()))
        except Exception as e:
            logger.warning("[Session] 自动归还推进一步失败: %s", e)
            return False
        finally:
            self._borrow_settling = False

    def _haul_step(self) -> bool:
        """空闲时把还没搬完的搬运推进一步（走到箱边/开箱/取/放；对账走事件那条路）。"""
        planner = getattr(self, "planner", None)
        if planner is None:
            return False
        try:
            return bool(planner.haul_step(self._borrow_context()))
        except Exception as e:
            logger.warning("[Session] 搬运推进一步失败: %s", e)
            return False

    def _verify_haul(self, snapshot: dict) -> None:
        """新容器快照到了 → 给当前搬运做两侧对账（真的对上才算数）。"""
        planner = getattr(self, "planner", None)
        verify = getattr(planner, "verify_haul", None)
        if not callable(verify):
            return
        try:
            player = self.runtime.get("player") if isinstance(self.runtime, dict) else None
            verify(snapshot, point_of(player))
        except Exception as e:
            logger.warning("[Session] 搬运对账失败: %s", e)

    def _borrow_ledger_status(self) -> dict:
        """借用台账快照（planner 可能是测试替身，缺方法时给空值）。"""
        planner = getattr(self, "planner", None)
        getter = getattr(planner, "borrow_status", None)
        if not callable(getter):
            return {"outstanding": [], "total_outstanding": 0, "audit": []}
        try:
            return getter()
        except Exception as e:
            logger.debug("[Session] 借用台账状态读取失败: %s", e)
            return {"outstanding": [], "total_outstanding": 0, "audit": []}

    def _verify_borrow_returns(self, snapshot: dict) -> None:
        """新容器快照到了 → 核对归还（真的回去了才算数）。"""
        planner = getattr(self, "planner", None)
        if planner is None:
            return
        try:
            rows = planner.verify_borrow_returns(snapshot)
        except Exception as e:
            logger.warning("[Session] 归还核对失败: %s", e)
            return
        for row in rows:
            logger.info("[Borrow] 归还核对 %s: %s %s", row.get("item"),
                        row.get("result"), row.get("detail", ""))

    def _absorb_conversation_namespace(self, data: dict) -> None:
        """按 `world.server`（ip）→ `world.world_name`（单人）→ unknown 切换记忆分区。

        进出世界都走这里：同一分区复用同一份记忆文件（"把上下文带走"），换服就换文件。
        """
        store = getattr(self, "conversation", None)
        if store is None:
            return
        # global 层是单实例（人设/规则/权限/.local），没有分区也没有回灌动作，
        # 但"这一层已经就位"要留个痕，status 里才看得出来。
        self._mark_layer("global", "全局层：人设 / 规则 / 权限（单实例，跨服常驻）")
        world = data.get("world") if isinstance(data, dict) else None
        world = world if isinstance(world, dict) else {}
        try:
            store.use_namespace(world.get("server", ""), world.get("world_name", ""))
        except Exception as e:
            logger.warning("[Session] 对话记忆分区切换失败: %s", e)
        # 借用规则与审计也按同一个 server 分区（换服换文件，重启不丢授权）。
        rules = getattr(self, "borrow_rules", None)
        if rules is not None:
            try:
                rules.use_namespace(world.get("server", ""), world.get("world_name", ""))
            except Exception as e:
                logger.warning("[Session] 借用规则分区切换失败: %s", e)
            planner = getattr(self, "planner", None)
            ledger = getattr(planner, "borrow_ledger", None) if planner is not None else None
            if ledger is not None:
                try:
                    ledger.use_audit_path(rules.audit_path)
                    # 欠账台账：与规则/审计同目录同分区；换分区＝按那个分区的盘恢复欠账。
                    ledger.use_ledger_path(rules.ledger_path)
                except Exception as e:
                    logger.warning("[Session] 借用审计/台账路径切换失败: %s", e)
            self._mark_layer(f"server:{rules.namespace}", "server 层（对话记忆 / 借用规则）")

    # ── 分层回灌（global / person / server / world）────────────────

    def _mark_layer(self, layer: str, label: str = "") -> bool:
        """记下某一层这次 session 已经"推过"了；第一次返回 True（幂等标记按层分开）。"""
        seen = getattr(self, "_replay_layers", None)
        if seen is None:
            seen = self._replay_layers = set()
        if layer in seen:
            return False
        seen.add(layer)
        if label:
            logger.info("[Memory] 激活 %s（%s）", layer, label)
        return True

    def _absorb_places(self, data: dict) -> None:
        """吸收 state 里的站点/路径（原样落盘），并按需把后端存盘的回灌回模组。"""
        store = getattr(self, "places", None)
        if store is None or not isinstance(data, dict):
            return
        world = data.get("world") if isinstance(data.get("world"), dict) else {}
        dimension = (data.get("player") or {}).get("dimension", "") if isinstance(data.get("player"), dict) else ""
        try:
            store.use_namespace(world.get("server", ""), world.get("world_name", ""), dimension)
            store.absorb_state(data)
        except Exception as e:
            logger.warning("[Session] 站点/路径记忆吸收失败: %s", e)
            return
        self._replay_places(data, store)

    def _replay_places(self, data: dict, store) -> dict:
        """把 world 层的站点/路径回灌回模组（同一个 world **只推一次**）。

        触发：进入某个 world（本 session 第一次见到这个 `world:<key>` 分区）、后端有存盘、
        模组侧这份记忆是空的（`stations`/`routes` 报了空列表）**或**有同名但内容不同的条目。
        模组已有同名同内容的条目不推（幂等）；不在世界里不下发（在场门）。
        """
        key = store.namespace
        layer = f"world:{key}"
        seen = getattr(self, "_replay_layers", None)
        if seen is None:
            seen = self._replay_layers = set()
        if layer in seen:
            return {"server": key, "replayed": False, "reason": "already_replayed"}
        saved_stations, saved_routes = store.stations(), store.routes()
        if not saved_stations and not saved_routes:
            return {"server": key, "replayed": False, "reason": "nothing_saved"}
        if not self.llm_active():
            return {"server": key, "replayed": False, "reason": "not_in_world"}
        reported_stations = data.get("stations")
        reported_routes = data.get("routes")
        if not isinstance(reported_stations, list) and not isinstance(reported_routes, list):
            # 模组没上报这两类（旧模组）：不能确认"它那边是空的"，就不推。
            return {"server": key, "replayed": False, "reason": "mod_not_reporting"}

        plan = plan_replay(
            saved_stations, saved_routes,
            reported_stations if isinstance(reported_stations, list) else None,
            reported_routes if isinstance(reported_routes, list) else None)

        sent_stations, sent_routes = [], []
        skills = getattr(self, "skills", None)
        remember_station = getattr(skills, "remember_station", None)
        for args in plan["stations"]:
            if callable(remember_station):
                remember_station(**args)
                sent_stations.append(args)
        remember_route = getattr(skills, "remember_route", None)
        for args in plan["routes"]:
            if callable(remember_route):
                remember_route(args["name"], args["points"])
                sent_routes.append(args)
        if plan["stations"] or plan["routes"]:
            # 内容不同才推的条目要留痕（"为什么又覆盖了一次"）。
            logger.info("[Memory] 回灌 %s：站点 %d 条、路径 %d 条（模组已有同名的 %d 条不推）",
                        key, len(sent_stations), len(sent_routes), len(plan["skipped"]))
        for item in plan["blocked"]:
            logger.info("[Memory] 回灌受阻 %s %s：%s", item["kind"], item["name"], item["reason"])
        # 只想推、却因为"模组只报了点数（无几何）"推不出去的路径 → **按需**取一次点列
        # （`route_export {name}`）；拿到就存盘并推回去，拿不到就维持诚实的 no_geometry。
        self._request_route_exports(plan["blocked"], store)
        self._mark_layer(layer)
        return {"server": key, "replayed": True, "stations": sent_stations,
                "routes": sent_routes, "skipped": plan["skipped"], "blocked": plan["blocked"]}

    def _request_route_exports(self, blocked: Any, store) -> list[str]:
        """对"缺几何"的路径发 `route_export`（异步：点列在回执里才拿得到）。

        点列**不进每帧 state** —— 只有这一条按需出口；回执回来后由
        `_absorb_route_export` 存盘并推 `remember_route`，失败就维持 `no_geometry`。
        """
        skills = getattr(self, "skills", None)
        export = getattr(skills, "route_export", None) if skills is not None else None
        if not callable(export):
            return []
        requested: list[str] = []
        for item in blocked or []:
            if not isinstance(item, dict) or item.get("kind") != "route":
                continue
            if item.get("reason") != "no_geometry":
                continue
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            result = export(name) or {}
            req_id = str(result.get("req_id") or "")
            if req_id:
                self._route_exports[req_id] = {"name": name, "namespace": store.namespace}
                while len(self._route_exports) > 20:
                    self._route_exports.pop(next(iter(self._route_exports)))
            requested.append(name)
        if requested:
            logger.info("[Memory] 路径缺几何，向模组按需取点列：%s", ", ".join(requested))
        return requested

    def _absorb_route_export(self, data: dict) -> bool:
        """`route_export` 的回执：拿到点列就存盘并推回模组，拿不到就维持 no_geometry。"""
        req_id = str(data.get("id") or "")
        pending_map = getattr(self, "_route_exports", None)
        if not pending_map:
            return False
        pending = pending_map.pop(req_id, None)
        if not pending:
            return False
        store = getattr(self, "places", None)
        payload = data.get("data") if isinstance(data.get("data"), dict) else {}
        name = str(pending.get("name") or payload.get("name") or "")
        points = payload.get("points")
        if not bool(data.get("success")) or not isinstance(points, list) or len(points) < 2:
            reason = str(payload.get("reason") or data.get("error") or "no_geometry")
            logger.info("[Memory] 路径 '%s' 取不回点列（%s），维持 no_geometry（不编）", name, reason)
            return False
        if store is None:
            return False
        store.absorb_state({"routes": [{"name": name, "points": points}]})
        row = store.route(name) or {}
        args = route_args(row) if isinstance(row, dict) else None
        if args is None:
            logger.info("[Memory] 路径 '%s' 的回执点列不完整，没推", name)
            return False
        skills = getattr(self, "skills", None)
        remember = getattr(skills, "remember_route", None) if skills is not None else None
        if callable(remember):
            remember(args["name"], args["points"])
        logger.info("[Memory] 路径 '%s' 取回 %d 个点并灌回模组",
                    args["name"], len(args["points"]))
        return True

    # ── 领地 / 基地 / 容器 / 区域（world 层分区）──────────────────

    def _note_zone_block(self, data: dict) -> Optional[dict]:
        """被区域硬拦（`blocked_by_zone`）→ 记下"哪个区域、哪条规则"，并归入任务失败原因。

        用户口径：「被拦住时**如实上报"哪个区域、哪条保护规则"**」——所以这条要同时
        进 L2 上下文（下一轮 LLM 事前就不去试）**和**当前任务的 `failure_reason`。
        """
        payload = data.get("data") if isinstance(data.get("data"), dict) else {}
        zone = str(payload.get("blocked_by_zone") or "").strip()
        if not zone:
            return None
        rule = str(payload.get("rule") or "").strip()
        action = str(payload.get("action") or "").strip()
        row = {
            "ts": time.time(),
            "zone": zone,
            "rule": rule,
            "action": action,
            "reason": f"blocked_by_zone: {zone}(rule={rule}, action={action})",
            "req_id": str(data.get("id") or ""),
        }
        blocks = getattr(self, "_zone_blocks", None)
        if blocks is None:
            blocks = self._zone_blocks = []
        blocks.append(row)
        del blocks[:-20]
        self._fail_active_task_for_zone(row)
        logger.info("[Session] 区域硬拦：区域「%s」规则「%s」（动作 %s）", zone, rule, action)
        recorder = getattr(self, "_on_security_audit", None)
        if callable(recorder):
            try:
                recorder({"kind": "zone_blocked", "zone": zone, "rule": rule,
                          "action": action, "req_id": row["req_id"],
                          "outcome": "blocked"})
            except Exception as e:  # noqa: BLE001
                logger.debug("[Session] 区域拦截审计失败: %s", e)
        return row

    def _fail_active_task_for_zone(self, row: dict) -> None:
        """把这条拦截归到**当前正在跑的任务**上（失败原因带区域名与规则）。"""
        tasks = getattr(self, "tasks", None)
        if tasks is None:
            return
        cmd = ""
        manager = getattr(self, "action_manager", None)
        current = getattr(manager, "current", None) if manager is not None else None
        if current is not None:
            cmd = str(getattr(current, "label", "") or "")
        if not cmd:
            cmd = str(getattr(self, "_manual_task_kind", "") or "")
        target = None
        for item in tasks.items():
            if item.status == "active" and (not cmd or item.kind == cmd):
                target = item
                break
        if target is None:
            for item in tasks.items():
                if item.status == "active":
                    target = item
                    break
        if target is None:
            return
        try:
            tasks.complete(target.id, success=False, reason=row["reason"])
        except Exception as e:  # noqa: BLE001
            logger.debug("[Session] 区域拦截归入任务失败原因失败: %s", e)

    def _absorb_territory(self, data: dict) -> None:
        """吸收 state 里的 `zones[]`（Java 是硬拦真相）+ 按需把后端存的区域回灌回模组。

        分区与站点/路径完全一致（坐标只在同一世界同一维度有效）：**换服/换世界/换维度
        各自一份文件，绝不串号**。Java 的 `ZoneGuard` 是内存态、重启即丢，所以后端这份
        存盘 + 进世界时的幂等回灌才是它不丢的原因。
        """
        store = getattr(self, "territories", None)
        if store is None or not isinstance(data, dict):
            return None
        world = data.get("world") if isinstance(data.get("world"), dict) else {}
        player = data.get("player") if isinstance(data.get("player"), dict) else {}
        try:
            store.use_namespace(world.get("server", ""), world.get("world_name", ""),
                                player.get("dimension", ""))
            store.absorb_state(data)
        except Exception as e:
            logger.warning("[Session] 区域吸收失败: %s", e)
            return None
        return self._replay_zones(data, store)

    def _replay_zones(self, data: dict, store) -> dict:
        """把 world 层的区域声明回灌回模组（同一个 world **只推一次**，幂等）。"""
        key = store.namespace
        layer = f"zones:{key}"
        seen = getattr(self, "_replay_layers", None)
        if seen is None:
            seen = self._replay_layers = set()
        if layer in seen:
            return {"namespace": key, "replayed": False, "reason": "already_replayed"}
        saved = store.zones()
        if not saved:
            return {"namespace": key, "replayed": False, "reason": "nothing_saved"}
        if not self.llm_active():
            return {"namespace": key, "replayed": False, "reason": "not_in_world"}
        reported = data.get("zones")
        if not isinstance(reported, list):
            return {"namespace": key, "replayed": False, "reason": "mod_not_reporting"}
        report = plan_zone_replay(saved, reported)
        skills = getattr(self, "skills", None)
        define = getattr(skills, "define_zone", None) if skills is not None else None
        sent: list[dict] = []
        for args in report["zones"]:
            if callable(define):
                define(**args)
                sent.append(args)
        for row in report["adopted"]:
            # 只在模组侧存在的区域（Java 是硬拦真相）→ 直接采纳落盘，下次也能回灌。
            store.set_zone(row.get("zone"), granted_by={"tier": "", "name": ""})
        if sent:
            logger.info("[Memory] 回灌区域 %s：%d 条", key, len(sent))
        for item in report["blocked"]:
            logger.info("[Memory] 区域回灌受阻 %s：%s", item.get("name"), item.get("reason"))
        self._mark_layer(layer)
        return {"namespace": key, "replayed": True, "zones": sent,
                "skipped": report["skipped"], "blocked": report["blocked"],
                "adopted": [row["name"] for row in report["adopted"]]}

    def _zone_block_context(self) -> list[dict]:
        """最近被区域硬拦的记录（进 L2：让 LLM 知道"这里是农场，不能动"）。"""
        return [dict(row) for row in getattr(self, "_zone_blocks", [])[-5:]]

    def _absorb_people(self, data: dict) -> None:
        """把在场玩家与 person 层对上号（**只读**，不写新条目）。

        真正落 person 记录的时机是"他说了话"（`_note_person`）—— 那里才有 uuid。
        在场的人里能从聊天认出 uuid 的用 uuid 取件，其余按"名字 + 本服"取降级件。
        """
        store = getattr(self, "people", None)
        if store is None or not isinstance(data, dict):
            return
        rows = data.get("players")
        if not isinstance(rows, list):
            return
        server = self._current_server_key()
        facts: dict[str, dict] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            name = str(row.get("name") or "").strip()
            if not name:
                continue
            try:
                person_facts = store.facts(self._person_uuid(name), name, server_key=server)
            except Exception as e:
                logger.debug("[Session] person 记忆读取失败 %s: %s", name, e)
                continue
            if person_facts:
                facts[name] = person_facts
        self._present_person_facts = facts

    def current_server_key(self) -> str:
        """当前 server 分区键（通讯录、按服白名单等对外接口用）。"""
        return self._current_server_key()

    def _current_server_key(self) -> str:
        """当前 server 分区键（供 person 层降级键/现役玩家取件用）。"""
        rules = getattr(self, "borrow_rules", None)
        namespace = getattr(rules, "namespace", "") if rules is not None else ""
        if namespace:
            return str(namespace)
        runtime = getattr(self, "runtime", None)
        world = runtime.get("world") if isinstance(runtime, dict) else None
        world = world if isinstance(world, dict) else {}
        return server_key(world.get("server", ""), world.get("world_name", ""))

    def _person_uuid(self, name: str) -> str:
        """这个名字对应的 uuid：**只有**在本服亲耳听过他说话（聊天带 uuid）才认。"""
        if not name:
            return ""
        seen = getattr(self, "_people_seen", None) or {}
        row = seen.get(name)
        if not row:
            return ""
        uuid, server = row
        return uuid if server and server == self._current_server_key() else ""

    def _note_person(self, data: dict) -> None:
        """一条聊天事件 → person 层：uuid↔名字绑定（稳定事实）。

        **只记身份**：聊天内容、坐标、在哪遇到的这类服务器内的一次性上下文一概不进来
        （`PersonMemoryStore` 的白名单会把它们挡掉）。
        """
        if not isinstance(data, dict):
            return
        name = str(data.get("sender") or "").strip()
        if not name or name == "?":
            return
        store = getattr(self, "people", None)
        if store is None:
            return
        uuid = str(data.get("uuid") or "").strip()
        server = self._current_server_key()
        try:
            entry = store.identify(uuid, name, server_key=server)
        except Exception as e:
            logger.debug("[Session] person 记忆写入失败: %s", e)
            return
        if not isinstance(entry, dict):
            return
        if uuid:
            seen = getattr(self, "_people_seen", None)
            if seen is None:
                seen = self._people_seen = {}
            seen[name] = (uuid, server)
        self._mark_layer(f"person:{entry.get('key')}",
                         f"person 层（{name} 的稳定事实）")

    def _record_conversation_chat(self, data: dict, whisper: Optional[dict] = None,
                                  kind: str = "") -> None:
        """把一条聊天事件写进对话记忆（L1 落盘 + L2 窗口）。

        **权限过滤之前**就记：guest 在公开服只记录不回复，正是设计要的"听得见、不接话"。
        `whisper` 非空表示这条是**私信**：落成 `whisper` 类型、发送者用识别出的人；
        `kind` 是模组已经判明的类型（目前只有 `"emote"`）——给了就照它落。
        """
        store = getattr(self, "conversation", None)
        if store is None:
            return
        try:
            if whisper:
                entry = store.record_chat(
                    str(whisper.get("player") or data.get("sender") or "?"),
                    str(whisper.get("text") or data.get("message") or ""),
                    kind="whisper",
                    is_system=bool(data.get("is_system")),
                    x=data.get("sender_x"),
                    y=data.get("sender_y"),
                    z=data.get("sender_z"),
                    distance=data.get("sender_distance"),
                )
            else:
                entry = store.record_chat(
                    str(data.get("sender") or "?"),
                    str(data.get("message") or ""),
                    kind=str(kind or ""),
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

    # ── 出站/指令安全：审计 / 单一指令口 / 私信 / TPA ──
    # 规则只在这些方法里实现一次：planner 调它们，别的出口不直接拼指令。

    def _on_security_audit(self, row: dict) -> None:
        """统一安全审计（出站清洗 / 指令闸门 / TPA / 私信都进这里）：内存环形 + 落盘。

        落盘用一个坏掉也不会拖垮链路的 AuditLog；建不出来就只留内存（并只 warning 一次）。
        """
        entry = dict(row or {})
        entry.setdefault("ts", time.time())
        log = getattr(self, "_security_audit", None)
        if log is None:
            self._security_audit = log = []
        log.append(entry)
        del log[:-100]

        if getattr(self, "_security_log_failed", False):
            return
        store = getattr(self, "_security_log", None)
        if store is None:
            try:
                store = AuditLog("data/audit/security.jsonl")
            except Exception as exc:  # noqa: BLE001
                logger.warning("[Session] 安全审计落盘不可用: %s", exc)
                self._security_log_failed = True
                return
            self._security_log = store
        try:
            store.append(
                category="security",
                action=str(entry.get("kind") or "event"),
                target=str(entry.get("command") or entry.get("player")
                           or entry.get("target") or ""),
                actor=str(entry.get("actor") or entry.get("tier") or ""),
                outcome=str(entry.get("outcome") or entry.get("blocked") or ""),
                details=entry,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("[Session] 安全审计落盘失败: %s", exc)

    def security_audit(self, n: int = 20) -> list[dict]:
        """最近 n 行安全审计（时间从早到晚）。"""
        rows = list(getattr(self, "_security_audit", []) or [])
        return rows[-int(n):] if n else rows

    def _actor_identity(self) -> tuple[str, str]:
        actor = getattr(self, "_actor", None) or {}
        return str(actor.get("name") or ""), str(actor.get("tier") or "guest")

    def resolve_player(self, name: str) -> dict:
        """玩家名解析（不猜）：精确优先、歧义回候选、不在线/不在本服一律拒。"""
        players = self.runtime.get("players") if isinstance(self.runtime, dict) else []
        return resolve_player_name(name, players)

    # 需要"玩家名 → 唯一在线玩家"解析的指令族（**不猜**：歧义/不在线一律拒）。
    TARGET_FAMILIES = frozenset({"tp", "tpa", "tpaccept", "tpdeny"})
    _COORD_RE = re.compile(r"^[~^]?-?\d+(?:\.\d+)?$")
    _NAME_RE = re.compile(r"^[A-Za-z0-9_]{1,16}$")
    # 原版世界边界（`/tp` 到边界外＝无效目标；更细的区域保护由 Java 侧硬拦）。
    WORLD_BORDER = 30_000_000

    @classmethod
    def _looks_like_player_name(cls, token: Any) -> bool:
        """像玩家名的 token（不是坐标）：`tp 100 64 100` 里的数字不该被当成玩家。"""
        text = str(token or "").strip()
        if not text or cls._COORD_RE.fullmatch(text):
            return False
        return bool(cls._NAME_RE.fullmatch(text))

    @classmethod
    def _coordinate_in_bounds(cls, token: Any) -> bool:
        """绝对坐标要在原版世界边界内（相对坐标 `~` 不检查，由服务端自己算）。"""
        text = str(token or "").strip()
        if not text or text[0] in "~^":
            return True
        try:
            return abs(float(text)) <= cls.WORLD_BORDER
        except ValueError:
            return True

    def _resolve_command_targets(self, tool: Any, args: Any) -> tuple[Any, Optional[dict]]:
        """把 `tp/tpa/tpaccept/tpdeny` 里的玩家名解析成**唯一在线玩家**。

        返回 `(新 args, None)` 或 `(None, 拒绝结论)`。歧义（多个相似名）**回候选、不猜**；
        不在线/不在本服直接拒 —— 传错人比不做更糟。绝对坐标越界也拒
        （更细的"超距/区域保护"是 Java 侧的硬拦，见 `click_screen_button`/区域保护那批）。
        """
        family = str(tool or "").strip().casefold().lstrip("/")
        family = family.split(":")[-1]
        if family not in self.TARGET_FAMILIES or args is None:
            return args, None
        if isinstance(args, str):
            tokens = args.split()
        elif isinstance(args, dict):
            tokens = [value for value in args.values()]
        elif isinstance(args, (list, tuple, set)):
            tokens = list(args)
        else:
            return args, None

        resolved: list[Any] = []
        for token in tokens:
            text = str(token).strip()
            if self._COORD_RE.fullmatch(text):
                if not self._coordinate_in_bounds(text):
                    self._on_security_audit({"kind": "command", "tool": family,
                                             "target": text, "outcome": "blocked",
                                             "blocked": "out_of_bounds"})
                    return None, {"ok": False, "blocked": "out_of_bounds",
                                  "reason": "out_of_bounds"}
                resolved.append(token)
                continue
            if not self._looks_like_player_name(token):
                resolved.append(token)
                continue
            found = self.resolve_player(text)
            if not found.get("ok"):
                reason = str(found.get("reason") or "not_found")
                self._on_security_audit({"kind": "command", "tool": family,
                                         "target": text, "outcome": "blocked",
                                         "blocked": reason})
                return None, {"ok": False, "blocked": reason, "reason": reason,
                              "candidates": found.get("candidates", [])}
            resolved.append(str(found["name"]))
        return resolved, None

    def run_server_command(self, tool: str, args: Any = None, *, confirm: str = "",
                           free_text: str = "", tier: str = "",
                           actor: str = "", reply: bool = False) -> dict:
        """**单一指令口**：所有服务器指令都从这里出去。

        顺序：玩家名解析（tp 类指令，不猜）→ 白名单（唯一来源，默认空）→ 硬黑名单
        （配置放不开）→ 高风险二次确认 → 真正下发。没勾选的一律拒，如实回
        `blocked=not_whitelisted`。
        """
        args, refusal = self._resolve_command_targets(tool, args)
        if refusal is not None:
            return refusal
        name, default_tier = self._actor_identity()
        result = self.command_gate.evaluate(
            tool, args, server_key=self._current_server_key(),
            actor=actor or name, tier=tier or default_tier,
            confirm=confirm, free_text=free_text, reply=reply,
        )
        if not result.get("ok"):
            return result
        result["sent"] = self.skills.run_command(result["command"])
        return result

    def _click_screen_button(self, match: str) -> dict:
        """路径 B：在**已打开**的界面里做控件级点击（Java 命令 `click_screen_button`）。

        后端**不**做任何界面/鼠标模拟：只把 `match` 交给 Java 侧。
        """
        target = str(match or "").strip()
        if not target:
            return {"ok": False, "blocked": "empty_match"}
        sent = self.skills.click_screen_button(target)
        self._on_security_audit({"kind": "click_screen_button", "target": target,
                                 "outcome": "sent"})
        return {"ok": True, "match": target, "sent": sent}

    def send_whisper(self, player: str, text: str, *, tier: str = "") -> dict:
        """给某人发私信（悄悄话）：独立于 `send_chat`，走白名单里的 msg/tell/w。

        约束：文本过出站清洗；**guest 不能借 AI 去私信别人**（只允许回那个 guest
        自己发来的私信 —— 那是"回"，不是"去私信别人"）；目标必须能被解析成唯一在线玩家。
        """
        _, actor_tier = self._actor_identity()
        effective_tier = str(tier or actor_tier)
        cleaned = sanitize_outbound(text, kind="whisper", observer=self._on_security_audit)
        if not cleaned.strip():
            return {"ok": False, "blocked": "empty_text"}
        target = self.resolve_player(player)
        if not target.get("ok"):
            reason = str(target.get("reason") or "not_found")
            self._on_security_audit({"kind": "whisper", "player": str(player or ""),
                                     "tier": effective_tier, "outcome": "blocked",
                                     "blocked": reason})
            return {"ok": False, "blocked": reason, "reason": reason,
                    "candidates": target.get("candidates", [])}
        name = str(target["name"])
        reply_to = str(getattr(self, "_reply_whisper_to", "") or "")
        # "回他发来的私信" vs "去私信别人"：只有前者允许 guest 触发。判定同时落在闸门
        # （§2c）与这里 —— 这样无论走 send_whisper 还是 run_server_command 都逃不掉。
        is_reply = bool(reply_to) and name.casefold() == reply_to.casefold()
        if effective_tier == "guest" and not is_reply:
            self._on_security_audit({"kind": "whisper", "player": name, "tier": effective_tier,
                                     "outcome": "blocked", "blocked": "tier_not_allowed"})
            return {"ok": False, "blocked": "tier_not_allowed", "reason": "tier_not_allowed"}
        family = self.commands_store.whisper_family()
        result = self.run_server_command(family, [name], free_text=cleaned,
                                        tier=effective_tier, reply=is_reply)
        if result.get("ok"):
            self._whisper_sent_this_turn.add(name.casefold())
            try:
                self.contacts.record_out(self._current_server_key(),
                                         str(target.get("uuid") or ""), name, cleaned)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[Session] 私信落盘失败: %s", exc)
        return result

    def respond_tpa(self, action: str, player: str = "", *, tier: str = "",
                    via: str = "auto") -> dict:
        """显式回应传送请求（模型调 `tpa_respond` / 人说了"接受/拒绝"）。"""
        _, actor_tier = self._actor_identity()
        target = str(player or "")
        if not target:
            pending = getattr(self, "_last_tpa_request", None) or {}
            target = str(pending.get("player") or "")
        result = self.tpa.respond(action, player=target, tier=str(tier or actor_tier),
                                  via=via, server_key=self._current_server_key())
        return {"ok": bool(result.get("ok")), **result}

    def request_tpa(self, player: str, *, tier: str = "") -> dict:
        """主动发起 tpa（默认只有 master；guest 一律不行）。"""
        _, actor_tier = self._actor_identity()
        result = self.tpa.initiate(str(player or ""), tier=str(tier or actor_tier),
                                   server_key=self._current_server_key())
        return {"ok": bool(result.get("ok")), **result}

    def tpa_status(self) -> dict:
        """TPA 快照：策略 + 最近审计（供 /api/status）。"""
        coordinator = getattr(self, "tpa", None)
        permissions = getattr(self, "permissions", None)
        policy: dict[str, Any] = {}
        if permissions is not None:
            try:
                policy = permissions.tpa_policy()
            except Exception:
                policy = {}
        return {
            "policy": policy,
            "recent": coordinator.audit_log(10) if coordinator is not None else [],
            "pending_request": dict(getattr(self, "_last_tpa_request", None) or {}),
        }

    def _security_status(self) -> dict:
        """安全块（`/api/status`）：白名单/最近审计/待确认。半成品实例也不炸。"""
        store = getattr(self, "commands_store", None)
        gate = getattr(self, "command_gate", None)
        key = self._current_server_key()
        whitelist: dict[str, Any] = {"enabled": False, "global": [], "server": [],
                                     "whisper_family": "msg"}
        if store is not None:
            try:
                whitelist = {
                    "enabled": store.enabled(),
                    "global": store.whitelist_for(""),
                    "server": store.whitelist_for(key),
                    "whisper_family": store.whisper_family(),
                }
            except Exception:
                pass
        return {
            "whitelist": whitelist,
            "audit": self.security_audit(20),
            "pending_confirms": gate.pending_confirms() if gate is not None else [],
        }

    def _contacts_status(self) -> dict:
        """通讯录块（`/api/status`）：当前分区 + 联系人数。"""
        contacts = getattr(self, "contacts", None)
        key = self._current_server_key()
        count = 0
        if contacts is not None:
            try:
                count = len(contacts.contacts(key))
            except Exception:
                count = 0
        return {"server": key, "count": count}

    def _detect_whisper(self, sender: str, message: str, is_system: bool,
                        chat_kind: str = "", whisper_flag: bool = False,
                        sender_unknown: bool = False) -> Optional[dict]:
        """把一条聊天判成私信；认不出返回 None（**退化为普通聊天**，不猜）。

        判据以 Java 侧给的结构化字段为准（**翻译键**判定，与语言/服务器话术无关）：

        - `chat_kind == "whisper_in"`（或旧模组只给了 `whisper: true`）→ 就是私信，
          发话人用 `sender`；`sender_unknown` 时名字留空（**绝不从文本里抠名字** ✗）。
        - `chat_kind` 是别的明确值（public_text / announcement / system / emote / …）→
          一律**不是**私信（哪怕文本长得像）。
        - `chat_kind` 缺省或 `unknown` 时才退回**可配的兜底正则**（默认可以不配）。
          兜底路径要防冒名：玩家自己发的公屏，只有检测出的名字与说话人一致才算私信 ——
          否则谁都能打一句 "Admin whispers to you: ..." 来冒充。
        """
        kind = str(chat_kind or "").strip().lower()
        if kind == "whisper_in" or (not kind and whisper_flag):
            return {"player": "" if sender_unknown else str(sender or "").strip(),
                    "text": str(message or ""), "source": "chat_kind",
                    "sender_unknown": bool(sender_unknown)}
        if kind and kind != "unknown":
            return None

        detector = getattr(self, "whisper_detector", None)
        if detector is None:
            return None
        found = detector.detect(message)
        if not found:
            return None
        name = str(found.get("player") or "")
        if not is_system:
            known = str(sender or "").strip()
            if not known or known == "?" or known.casefold() != name.casefold():
                return None
        found["source"] = "pattern"
        return found

    def _handle_whisper(self, whisper: dict, data: dict) -> None:
        """一条私信的完整路径：落通讯录 → 回复门硬旁路（等同被 @）→ 回私信。

        `sender_unknown=True`（模组拿不到私信者名字）时**不编名字**：只记审计与对话记忆，
        不落通讯录（没有键）、也不回（没有收件人）。
        """
        partner = str(whisper.get("player") or "")
        text = str(whisper.get("text") or "")
        server = self._current_server_key()
        if not partner:
            self._on_security_audit({"kind": "whisper_in", "player": "", "outcome": "sender_unknown",
                                     "text": text[:200]})
            self._audit_chat({"sender": "", "tier": "", "decision": "none",
                              "reason": "whisper_sender_unknown", "effective": 0.0,
                              "threshold": 0, "pending": 0})
            return
        uuid = ""
        if str(data.get("sender") or "").casefold() == partner.casefold():
            uuid = str(data.get("uuid") or "")
        if not uuid:
            uuid = self._person_uuid(partner)
        try:
            self.contacts.record_in(server, uuid, partner, text)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[Session] 私信落盘失败: %s", exc)
        # 交互库/记忆：私信也用**真实的发信人**当 sender（系统转达时原始 sender 可能是 "?"）。
        try:
            self.message_db.add_message(sender=partner, message=text,
                                        is_system=False, is_ai=False)
            self.memory.add_interaction(sender=partner, message=text,
                                        response=str(data.get("response") or ""))
        except Exception as exc:  # noqa: BLE001
            logger.warning("[Session] 私信入库失败: %s", exc)
        self._on_security_audit({"kind": "whisper_in", "player": partner,
                                 "tier": self.permissions.tier_of(partner),
                                 "outcome": "received", "text": text[:200]})

        if not self.llm_active():
            self._audit_chat({"sender": partner, "tier": "", "decision": "silent",
                              "reason": "not_in_world", "effective": 0.0,
                              "threshold": 0, "pending": 0})
            return
        gate = getattr(self, "reply_gate", None)
        if gate is None:
            return
        # 私信 = **定向** → 回复门硬旁路（等同被 @）；但仍受层级策略约束：
        # 公开服 guest 的 effective<=0 在 submit 里先于点名旁路判定，所以照样静默。
        decision = gate.submit(partner, text, is_system=False, mentioned=True)
        self._audit_chat({"sender": partner, "tier": decision.tier,
                          "effective": decision.effective, "threshold": decision.threshold,
                          "pending": decision.pending, "decision": decision.decision,
                          "reason": f"whisper:{decision.reason}"})
        if decision.decision in {"silent", "drop", "cooldown", "none"}:
            return
        self._run_chat_turn(partner, text, whisper_to=partner)

    def _handle_tpa_text(self, message: str, *, sender: str, is_system: bool) -> bool:
        """TPA 文本的两条入口：收到请求 / 我们这边的人说"接受·拒绝"。

        返回 True 表示这条文本已经被 TPA 消费（不再当普通聊天走语义门）。
        """
        tpa = getattr(self, "tpa", None)
        if tpa is None or not message:
            return False

        request = tpa.detector.detect_request(message)
        if request is not None:
            player = str(request.get("player") or sender or "")
            tier = self.permissions.tier_of(player) if player and player != "?" else "guest"
            via = "popup" if str(getattr(self, "ui_screen", "") or "") else "chat"
            result = tpa.handle_request(message, tier=tier, via=via, requester=player,
                                        server_key=self._current_server_key())
            action = str(result.get("action") or "none")
            if action == "none":
                return False
            if player:
                self._last_tpa_request = {"player": player, "ts": time.time(), "tier": tier,
                                          "path": result.get("path", "")}
            if action == "ask" and self.llm_active():
                # 先问：走**同一个**语义门（可以 no_action），绝不"事件→必发消息"。
                try:
                    self.inject_event(
                        "tpa_request",
                        intent=f"{player} 请求传送到你这里，要不要接受？",
                        event={"tpa": {"player": player, "path": result.get("path", "chat")}})
                except Exception as exc:  # noqa: BLE001
                    logger.warning("[Session] TPA 询问注入失败: %s", exc)
            return True

        if is_system:
            return False  # 系统消息里的触发词不算（只有人说的才算）
        intent = tpa.detector.detect_intent(message)
        if intent is None:
            return False
        player = str(intent.get("player") or "")
        if not player:
            pending = getattr(self, "_last_tpa_request", None) or {}
            if not pending or time.time() - float(pending.get("ts", 0.0)) > self.TPA_REQUEST_TTL:
                return False
            player = str(pending.get("player") or "")
        if not player:
            return False
        tier = self.permissions.tier_of(sender)
        result = tpa.respond(str(intent.get("action") or ""), player=player, tier=tier,
                             text=message, server_key=self._current_server_key())
        return str(result.get("action") or "none") in {"accept", "deny"}

    # ── 聊天 → 回复决策（权限 + 频率门 + 抗抖 + 语义门）──

    def _handle_player_chat(self, data: dict) -> None:
        """一条聊天事件的完整路径：记忆 → 语境 → 在场门 → 权限/频率门 → 语义门。

        tier **不**在这里做分支：能不能下指令、任务权重、记忆可见性都由
        `ReplyGate` 查 `TierPolicy` 表得出（设计文档 §4："别把等级写进决策主链"）。
        """
        sender = str(data.get("sender") or "?")
        message = str(data.get("message") or "")
        is_system = bool(data.get("is_system"))
        # Java 侧的结构化分类（翻译键判定，与语言/服务器话术无关）——
        # 有它就信它；缺省/unknown 才退回可配的兜底正则（见 `_detect_whisper`）。
        chat_kind = str(data.get("chat_kind") or "").strip().lower()
        whisper_flag = bool(data.get("whisper"))
        sender_unknown = bool(data.get("sender_unknown"))

        # L0 硬过滤：ban 词/正则 → 整条丢弃（连记忆都不进）。
        if self._is_banned(message):
            self._audit_chat({"sender": sender, "tier": "", "decision": "drop",
                              "reason": "ban_match", "effective": 0.0, "threshold": 0,
                              "pending": 0})
            return

        # `whisper_out`＝**我自己发出去的**私信回显：不是"有人在跟我说话" ✗
        # 不触发回复、不进"最近聊天"当对方发言（线程里我方气泡在 send_whisper 时就记过了）。
        if chat_kind == "whisper_out":
            self._on_security_audit({"kind": "whisper_out", "player": str(sender or ""),
                                     "outcome": "echo"})
            return

        # 私信（悄悄话）：必须在"系统消息只进记忆"那条早退**之前**判 ——
        # 私信常常是以服务器消息的形式到达的。
        whisper = self._detect_whisper(sender, message, is_system, chat_kind,
                                       whisper_flag, sender_unknown)
        if whisper is not None:
            self._record_conversation_chat(data, whisper=whisper)
            if not is_system:
                self._note_person(data)
            if not getattr(self, "_chat_tap_registered", False):
                self._note_chat(data)
            self._handle_whisper(whisper, data)
            return

        # 对话记忆（L1+L2）与交互库：静默模式 / guest 也要"听见"。
        # 表情落成 emote 类型（别的类型照原样走系统消息分类）。
        self._record_conversation_chat(
            data, kind="emote" if chat_kind == "emote" and not is_system else "")
        # person 层：认人（uuid↔名字）——只记身份，聊天内容留在 server 层。
        if not is_system:
            self._note_person(data)
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

        # TPA 请求/触发词：同样可能在系统消息/公告里（服务器弹窗/插件播报）。
        if self._handle_tpa_text(message, sender=sender, is_system=is_system):
            return

        # 系统消息（死亡/进出/命令回话）与公告/系统/表情：只进记忆，**不触发回复**。
        if is_system or chat_kind in {"announcement", "system", "emote"}:
            return

        # 规则性记忆：这句人话里如果有仓库授权（"可以用仓库里所有铁"），落成带条件的规则。
        # 只有**能授权翻箱**的 tier 说的话才算数（查能力表，不写 tier 分支）。
        self._absorb_borrow_speech(sender, message)

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

    def _run_chat_turn(self, sender: str, message: str, retry: bool = False,
                       whisper_to: str = "") -> Optional[str]:
        """过语义门并（可能）回复。语义门 = 本轮唯一"看语境"的廉价 LLM 调用。

        `whisper_to` 非空表示这一轮是**私信**：回复也要走私信（不能当着全服说）。
        """
        self._reply_whisper_to = str(whisper_to or "")
        self._whisper_sent_this_turn = set()
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
            self._reply_whisper_to = ""
            logger.info("[Chat] 语义门判定 no_action（8s 冷却），本轮不回复")
            return None
        if verdict == "wait":
            if not retry:
                self._pending_chat = {"sender": sender, "message": message,
                                      "due_at": time.time() + self.WAIT_RETRY_SECONDS,
                                      "retries": 0, "whisper_to": str(whisper_to or "")}
            else:
                self._pending_chat = None
            self._reply_whisper_to = ""
            logger.info("[Chat] 语义门判定 wait（可能还在打字）")
            return None

        self._pending_chat = None
        self._actor = {"name": sender, "tier": self._tier_of(sender)}
        self._manual_command_until = max(self._manual_command_until, time.time() + 20.0)
        try:
            response = self.handle_chat(sender, message, command_context="manual_chat")
        except Exception as e:
            logger.error("[Chat] LLM error: %s", e)
            self._reply_whisper_to = ""
            return None
        if not response:
            self._reply_whisper_to = ""
            return None
        logger.info("[Chat] AI response: %s", response[:80])
        try:
            if whisper_to:
                # 私信：同一轮里模型可能已经用 send_whisper 发过了（那就不再发一遍）。
                if whisper_to.casefold() in {key.casefold()
                                             for key in self._whisper_sent_this_turn}:
                    logger.info("[Chat] 私信已由工具发出，跳过自动回私信")
                else:
                    result = self.send_whisper(whisper_to, response)
                    if not result.get("ok"):
                        # 私信回不出去就**不发公开聊天**（那等于把私聊内容公开），只如实记日志。
                        logger.warning("[Chat] 私信回复未发出: %s",
                                       result.get("blocked") or result.get("reason"))
            else:
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
        self._reply_whisper_to = ""
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
            self._run_chat_turn(pending["sender"], pending["message"], retry=True,
                                whisper_to=str(pending.get("whisper_to") or ""))
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
        self._run_chat_turn(pending["sender"], pending["message"],
                            whisper_to=str(pending.get("whisper_to") or ""))

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
        """吸收一次状态里的 POI 观测，并按当前「服务器/世界/维度」切换命名空间。

        维度是必需的一层：同一服务器里主世界与下界的 xyz 不是一回事。
        """
        world = data.get("world")
        world = world if isinstance(world, dict) else {}
        player = data.get("player") if isinstance(data.get("player"), dict) else {}
        self.poi.use_namespace(world.get("server", ""), world.get("world_name", ""),
                               player.get("dimension", ""))
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
        # 结构化容器清单（POI storage）：制作缺料时"先查已知容器决定去哪"看这份。
        try:
            context["known_containers"] = self.poi.find("storage")
        except Exception as e:
            logger.debug("[Session] 容器清单读取失败: %s", e)
            context["known_containers"] = []
        # 当前打开的容器（Java state 上报的 open_container；含槽位内容时供取用定位）。
        context["open_container"] = self.runtime.get("open_container") or {}
        # 借用规则（对话里授权的"仓库里的铁可以用"）+ 当前欠账（借了还没还的）。
        rules = getattr(self, "borrow_rules", None)
        if rules is not None:
            try:
                context["borrow_rules"] = rules.describe()
            except Exception as e:
                logger.debug("[Session] 借用规则读取失败: %s", e)
                context["borrow_rules"] = ""
        planner = getattr(self, "planner", None)
        if planner is not None:
            try:
                context["borrow_pending"] = planner.borrow_note()
            except Exception as e:
                logger.debug("[Session] 借用欠账读取失败: %s", e)
                context["borrow_pending"] = ""
        # 能力位：这一轮是谁在说话（制作/翻箱/领地授权按它查表）；自主轮由 planner 侧改写。
        actor = getattr(self, "_actor", None) or {}
        context["actor_tier"] = str(actor.get("tier") or "guest")
        context["actor_name"] = str(actor.get("name") or "")
        context["autonomous"] = False
        # 领地 / 基地 / 容器 / 区域（world 层）：授权范围 + 当前基地 + 回家事实 + 刚被拦的记录。
        self._fill_territory_context(context)

        # 语境：谁在线、谁刚说过话（以及他说那话时站在哪）、最近的聊天顺序。
        context["players"] = self._players_for_status()
        context["last_speaker"] = dict(getattr(self, "last_speaker", {}) or {})
        # person 层：在场玩家关于自己的**稳定事实**（跨服复用；服务器内的一次性上下文不在这一层）。
        context["person_facts"] = dict(getattr(self, "_present_person_facts", {}) or {})
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

    # ── 领地 / 基地 / 容器 / 区域：进 L2 的动态上下文 ─────────────

    def _fill_territory_context(self, context: dict) -> None:
        """把领地快照、当前基地、回家事实与刚被拦的记录放进上下文（**只进 L2**）。"""
        store = getattr(self, "territories", None)
        if store is None:
            return
        try:
            snapshot = store.snapshot()
        except Exception as e:  # noqa: BLE001
            logger.debug("[Session] 领地快照读取失败: %s", e)
            snapshot = {"territories": [], "bases": [], "containers": [], "zones": []}
        context["territory"] = snapshot
        player = context.get("player") if isinstance(context.get("player"), dict) else {}
        position = point_of(player)
        bases = snapshot.get("bases") or []
        if position is None or not bases:
            context["current_base"] = None
            context["home_status"] = {}
        else:
            context["current_base"] = current_base(bases, *position)
            context["home_status"] = self._home_facts(bases, position)
        context["zone_blocks"] = self._zone_block_context()

    def _home_facts(self, bases: list, position: tuple) -> dict:
        """离家多远/多久的**事实**（只上报；回来与否交给 LLM 判断，绝不自己偷跑）。"""
        inside = current_base(bases, *position)
        if inside is not None:
            self._home_away_since = None
        elif getattr(self, "_home_away_since", None) is None:
            self._home_away_since = time.time()
        return home_situation(bases, position[0], position[1], position[2],
                              away_since=getattr(self, "_home_away_since", None),
                              now=time.time())

    def _territory_status(self) -> dict:
        """领地/基地/容器/区域的仪表盘快照（store 在测试替身里可能是空的）。"""
        store = getattr(self, "territories", None)
        snapshot: dict = {}
        if store is not None:
            try:
                snapshot = store.snapshot()
            except Exception as e:  # noqa: BLE001
                logger.debug("[Session] 领地快照读取失败: %s", e)
                snapshot = {}
        snapshot["zone_blocks"] = self._zone_block_context()
        return snapshot

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
        whisper_from = str(getattr(self, "_reply_whisper_to", "") or "")
        if whisper_from:
            context["whisper_from"] = whisper_from
        if event:
            context["event"] = event
            # 事件注入＝**没有人下指令**：制作/翻箱只看 autonomy_craft，且一律不许翻箱。
            context["autonomous"] = True
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
        # 有借出未还、手头又闲着 → 自己推进一步归还（走到箱边开箱、把剩余放回）。
        if is_idle and autonomy_enabled and not manual_override_active:
            self._auto_return_step()
            # 还没搬完的搬运：走到箱边/开箱/取/放各推进一步（对账走 state_update 那条路）。
            self._haul_step()
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
        self._reply_whisper_to = ""  # 事件不是私信，别把上一轮的私信上下文串进来
        # 外部事件同样"没人授权"：层级归零（事件不是主人下的指令）。
        self._actor = {"name": "", "tier": "guest"}
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
        "craft_item": "craft", "craft_via_recipe": "craft", "build": "build",
        "explore": "explore",
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
        # 任务 detail 也是 AI 生成的出站文本（会进 UI），同样过防注入清洗。
        detail = sanitize_outbound(detail, kind="task_detail",
                                   observer=self._on_security_audit)
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

    def _autonomous_craft_policy(self) -> CraftPolicy:
        """自主行为（self-prompt / 事件）的制作授权：只看 `autonomy_craft`，且不许翻箱。"""
        permissions = getattr(self, "permissions", None)
        if permissions is None:
            return CraftPolicy(tier="guest", autonomous=True, may_craft=False,
                               may_open_containers=False, reason="no_permissions")
        return permissions.craft_policy("", autonomous=True)

    def _handle_self_prompt(self, prompt: str):
        """Handle a self-prompt by routing through LLM to generate actions."""
        if not self.llm_active():
            # 在场门的第二道保险：不在世界里连自我提示都不许花 token。
            logger.info("[Session] 不在世界 → 跳过 self-prompt")
            return
        # 自主轮＝**没有任何人授权**：改回 guest，免得上一轮主人说话留下的层级
        # 让"没人指挥"的自发行为也能下白名单指令（与 autonomy_craft 默认关同一口径）。
        self._actor = {"name": "", "tier": "guest"}
        self.self_prompter.on_prompt_sent()
        # Build system context
        context = self._runtime_context()
        system_prompt = self.llm.build_system_prompt(context, self.commands.get_docs())
        messages = [
            {"role": "system", "content": system_prompt},
            # L2：动态上下文只进当轮 user 消息（与 planner 口径一致，绝不允许混进 system）。
            {"role": "user", "content": self.llm.build_dynamic_context(context, prompt)},
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

    def _memory_layers_status(self) -> dict:
        """四层记忆的现状（global / person / server / world）—— 仪表盘与排障用。"""
        places = getattr(self, "places", None)
        people = getattr(self, "people", None)
        stations = routes = 0
        world_namespace = ""
        if places is not None:
            try:
                stations = len(places.stations())
                routes = len(places.routes())
                world_namespace = places.namespace
            except Exception as e:
                logger.debug("[Session] world 层状态读取失败: %s", e)
        known_people: list[dict] = []
        if people is not None:
            try:
                known_people = [{
                    "key": row.get("key"),
                    "name": row.get("name"),
                    "degraded": bool(row.get("degraded")),
                    "facts": sorted((row.get("facts") or {}).keys()),
                } for row in people.known()[:20]]
            except Exception as e:
                logger.debug("[Session] person 层状态读取失败: %s", e)
        return {
            # global 层是单实例（人设/规则/权限文件），没有"分区"也没有回灌动作。
            "global": {"active": True, "note": "人设/规则/权限单实例，跨服常驻"},
            "world": {"namespace": world_namespace, "stations": stations, "routes": routes},
            "person": {"people": known_people},
            "replayed_layers": sorted(getattr(self, "_replay_layers", set())),
        }

    def get_status(self) -> dict:
        tasks = getattr(self, "tasks", None)
        gate = getattr(self, "reply_gate", None)
        store = getattr(self, "conversation", None)
        persona = getattr(self, "persona", None)
        rules = getattr(self, "borrow_rules", None)
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
            # 借用：授权规则（按 server 分区落盘）+ 欠账 + 借/还审计。
            "borrow": {
                **({"rules": rules.status()} if rules is not None else {}),
                "ledger": self._borrow_ledger_status(),
                "rule_audit": list(getattr(self, "_borrow_rule_audit", []) or [])[-10:],
            },
            # 领地 / 基地 / 容器 / 区域（world 层分区）+ 最近的区域硬拦记录。
            "territory": self._territory_status(),
            # 主动参与：最近注入的事件及结果（含 no_action）。
            "events": {
                "recent": list(getattr(self, "_event_audit", []) or [])[-10:],
                "pending_death": bool(getattr(self, "_pending_death", None)),
            },
            "persona_sections": persona.status() if persona is not None else {},
            # 出站/指令安全：白名单（唯一来源）+ 最近清洗/指令/点击审计 + TPA 策略与审计。
            # 全部防御式取值：有测试用 `Session.__new__` 拼半成品实例再打 get_status。
            "security": self._security_status(),
            "tpa": self.tpa_status(),
            "contacts": self._contacts_status(),
            "vision": self.vision.get_status(),
            "poi_memory": {
                "namespace": self.poi.namespace,
                "storage": len(self.poi.find("storage")),
                "workstation": len(self.poi.find("workstation")),
            },
            # 分层记忆（global / person / server / world）：world 层的站点/路径 +
            # person 层的稳定事实 + 本 session 已回灌的层标记。
            "memory_layers": self._memory_layers_status(),
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
            # 玩家接管/叫停 → 借来的东西立刻归位（"不要随身携带"）。
            self._settle_borrows("player_takeover")
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
