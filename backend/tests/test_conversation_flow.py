"""M3-19 集成测试：在场门 / 对话记忆分区 / 人设三段 / 权限与回复门 / 任务列表 / 主动参与。

这些都跑在 `Session` 上（不是单个模块），验的是"接线对不对"：
- 不在世界里**零 token、零下发**，但事件照样进对话记忆；
- 回到世界自动恢复，且**不碰 autonomy 开关**；
- 权限门：私人服人人可聊，公开服路人静默（消息仍进记忆），主人/朋友照常；
- 1 秒抗抖：连发时先攒着，静下来（tick 复查）才真正回；
- 事件注入走同一个语义门，语义门说 no_action 就**一个字都不发**（没有"事件→必发消息"的短路）。
"""

import json
import os
import tempfile
import time
import unittest
from types import SimpleNamespace

from agent.conversation import ConversationStore
from agent.permissions import PermissionsStore, ReplyGate
from agent.persona_store import PersonaStore
from agent.planner import Planner
from agent.session import ChatInbox, Session
from agent.task_list import TaskList


# ── 假件 ────────────────────────────────────────────────────────

class FakeLLM:
    """按角色返回预置内容；记录每次调用（用来断言"到底花没花 token"）。"""

    def __init__(self, verdicts=None):
        self.api_key = "test-key"
        self.verdicts = dict(verdicts or {})
        self.calls = []

    def chat(self, messages, agent=None, **kwargs):
        self.calls.append({"agent": agent, "kwargs": kwargs,
                           "messages": messages})
        content = self.verdicts.get(agent)
        if content is None:
            content = '{"decision": "continue"}'
        return {"content": content}

    def agents(self):
        return [call["agent"] for call in self.calls]

    def get_usage(self):
        return {"total_tokens": 0, "request_count": len(self.calls)}


class FakePlanner:
    def __init__(self, reply="好"):
        self.calls = []
        self.reply = reply
        self._last_decision = {}

    def plan_and_execute(self, sender, message, context, bot_name="AI"):
        self.calls.append({"sender": sender, "message": message,
                           "context": dict(context or {})})
        return self.reply

    def get_status(self):
        return {"is_planning": False, "last_decision": self._last_decision,
                "last_plan_time": 0.0, "last_locate": None}


class FakeSkills:
    def __init__(self):
        self.calls = []

    def command_context(self, _name):
        import contextlib
        return contextlib.nullcontext()

    def send_chat(self, message):
        self.calls.append(("send_chat", message))
        return {"success": True}

    def __getattr__(self, name):
        # 其它技能（guard / stop_all / …）一律记录调用，不真发 wire。
        def _record(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            return {"success": True}
        return _record


class FakeMemory:
    def __init__(self):
        self.interactions = []
        self.interaction_count = 0

    def add_interaction(self, sender, message, response="", action="", success=None):
        self.interactions.append((sender, message, response))
        self.interaction_count += 1

    def record_event(self, *_args, **_kwargs):
        pass

    def save(self):
        pass

    def build_context(self):
        return {}


class FakeMessageDB:
    def __init__(self):
        self.messages = []

    def add_message(self, sender, message, is_system=False, is_ai=False, **_kw):
        self.messages.append((sender, message, is_ai))

    def add_event(self, *_args, **_kwargs):
        pass

    def get_stats(self):
        return {}


class FakeControl:
    def __init__(self):
        self.cleared = 0
        self.flushed = 0
        self.holding = False

    def clear(self):
        self.cleared += 1

    def flush(self, force=False):
        self.flushed += 1
        return False


class FakeActionManager:
    def __init__(self):
        self.is_busy = False
        self.current = None
        self.current_label = None
        self.tracked = []
        self.stopped = 0

    def tick(self):
        pass

    def track(self, label, req_id, timeout=0.0, complete_on_response=True):
        self.tracked.append(label)

    def stop(self):
        self.stopped += 1
        self.current = None

    def pop_resume(self):
        return None

    def complete_current(self, *_args, **_kwargs):
        pass

    def get_status(self):
        return {"label": None}


class FakeModes:
    def __init__(self):
        self.ticks = 0

    def tick(self, *_args, **_kwargs):
        self.ticks += 1
        return None

    def get_status(self):
        return {"active": None}

    def reset(self):
        pass


class FakePrompter:
    def __init__(self):
        self.prompts = 0
        self.consulted = 0

    def mark_action(self):
        pass

    def should_prompt(self, *_args, **_kwargs):
        self.consulted += 1
        return False

    def build_prompt(self):
        self.prompts += 1
        return "self-prompt"

    def on_prompt_sent(self):
        pass

    def get_status(self):
        return {"enabled": False}


def make_session(tmp, *, in_world=True, server_mode="private", master=(),
                 friends=(), clock=None, conversation_llm=None, window=None):
    """搭一个"接线完整但没有任何 I/O"的 Session（沿用仓库里 `Session.__new__` 的测试惯例）。"""
    session = Session.__new__(Session)
    session.id = "t"
    session.wire = SimpleNamespace(send_command=lambda cmd, args=None: "req_1")
    session.runtime = {
        "world": {"server": "127.0.0.1:25565", "world_name": "world"},
        "player": {},
        "players": [],
    }
    session.in_world = in_world
    session.ui_screen = ""
    session._presence_changed_at = 0.0
    session._chat_tap_registered = True
    session.chat_inbox = ChatInbox()
    session.last_speaker = {}
    session._hud_note_local = ""
    session.memory = FakeMemory()
    session.message_db = FakeMessageDB()
    session.skills = FakeSkills()
    conversation_kwargs = {"root": os.path.join(tmp, "conversations"),
                           "llm": conversation_llm}
    if window is not None:
        conversation_kwargs.update(single_window=window, multi_window=window)
    session.conversation = ConversationStore(**conversation_kwargs)
    session.persona = PersonaStore(path=os.path.join(tmp, "persona.json"))
    session.permissions = PermissionsStore(path=os.path.join(tmp, "permissions.json"))
    session.permissions.set({"server_mode": server_mode, "master": list(master),
                             "friends": list(friends)})
    session.reply_gate = ReplyGate(session.permissions, clock=clock or time.time)
    session.tasks = TaskList()
    session.llm = FakeLLM()
    session.planner = FakePlanner()
    session.control = FakeControl()
    session.action_manager = FakeActionManager()
    session.modes_engine = FakeModes()
    session.self_prompter = FakePrompter()
    session.poi = SimpleNamespace(namespace="", find=lambda _kind: [],
                                  observe_state=lambda _d: None, save=lambda: None,
                                  use_namespace=lambda *_a: "",
                                  describe=lambda: "")
    session.vision = SimpleNamespace(latest=lambda: "", get_status=lambda: {},
                                     stop=lambda: None, refresh=lambda: None)
    session._actor = {"name": "", "tier": "guest"}
    session._pending_chat = None
    session._chat_audit = []
    session._event_audit = []
    session._event_cooldowns = {}
    session._last_event_at = 0.0
    session._pending_death = None
    session._seen_drop_ids = set()
    session._last_biome = ""
    session._last_task_event = {}
    session._manual_command_until = 0.0
    session._manual_action_reqs = set()
    session._manual_task_kind = None
    session._task_idle_since = None
    session._last_save = time.monotonic()
    session._last_memory_summary = 0.0
    return session


class Clock:
    """可控时钟（ReplyGate 注入用）。"""

    def __init__(self, value=1000.0):
        self.value = float(value)

    def __call__(self):
        return self.value


# ── 1. 在场门 ──────────────────────────────────────────────────

class PresenceGateTests(unittest.TestCase):
    def test_out_of_world_blocks_llm_and_wire_but_keeps_memory(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = make_session(tmp, in_world=False)
            session.handle_event("state_update", {
                "in_world": False, "ui_screen": "net.minecraft.client.gui.screens.PauseScreen",
                "world": {"server": "127.0.0.1:25565", "world_name": "world"},
            })
            session.handle_event("player_chat", {"sender": "Steve", "message": "小A 过来"})
            session.tick()

            # ① 不调任何 LLM（planner / 语义门 / vision 全停）
            self.assertEqual(session.planner.calls, [])
            self.assertEqual(session.llm.calls, [])
            # ② 不下发任何 wire 指令（含 modes_engine / 自主行为）
            self.assertEqual(session.skills.calls, [])
            self.assertEqual(session.modes_engine.ticks, 0)
            self.assertEqual(session.control.flushed, 0)
            # 自主行为（self-prompter / 夜晚自动睡觉）同样不跑
            self.assertEqual(session.self_prompter.consulted, 0)
            self.assertFalse(any(call[0] == "sleep" for call in session.skills.calls))
            # ③ 事件照样进对话记忆（静默模式，零 token）
            recent = session.conversation.recent()
            self.assertTrue(any(entry.get("message") == "小A 过来" for entry in recent))
            presence = session.get_status()["presence"]
            self.assertFalse(presence["in_world"])
            self.assertIn("未在游戏中", presence["label"])

    def test_status_reports_presence_and_ui_screen(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = make_session(tmp)
            session.handle_event("state_update", {
                "in_world": False, "ui_screen": "GuiConnecting",
                "world": {"server": "1.2.3.4:25565", "world_name": "w"},
            })
            status = session.get_status()

            self.assertFalse(status["in_world"])
            self.assertEqual(status["ui_screen"], "GuiConnecting")
            self.assertTrue(status["presence"]["llm_paused"])

    def test_legacy_mod_without_in_world_field_stays_active(self):
        """旧模组不发 in_world：按"在世界里"处理，不能因为字段缺失就永久停摆。"""
        with tempfile.TemporaryDirectory() as tmp:
            session = make_session(tmp)
            session.handle_event("state_update", {"player": {"x": 1}})

            self.assertTrue(session.in_world)
            self.assertTrue(session.llm_active())

    def test_entering_world_resumes_and_keeps_autonomy_switches(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = make_session(tmp, in_world=False)
            session.runtime["control_state"] = {"ai_controlled": True}
            session.runtime["behavior_state"] = {"behaviors_enabled": True}
            session.handle_event("state_update", {
                "in_world": False, "ui_screen": "GuiConnecting",
                "world": {"server": "srv", "world_name": "w"}})

            session.handle_event("state_update", {
                "in_world": True, "ui_screen": "",
                "world": {"server": "srv", "world_name": "w"}})
            session.handle_event("player_chat", {"sender": "Steve", "message": "过来"})

            # 自动恢复：planner 又开始工作
            self.assertEqual(len(session.planner.calls), 1)
            # 而且没有把 autonomy 开关改掉
            self.assertEqual(session.runtime["control_state"], {"ai_controlled": True})
            self.assertEqual(session.runtime["behavior_state"], {"behaviors_enabled": True})

    def test_leaving_world_clears_held_keys_without_sending(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = make_session(tmp)
            session.handle_event("state_update", {"in_world": False, "ui_screen": "x"})

            self.assertEqual(session.control.cleared, 1)
            self.assertEqual(session.control.flushed, 0)
            self.assertEqual(session.action_manager.stopped, 1)

    def test_out_of_world_handle_chat_refuses_llm(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = make_session(tmp, in_world=False)

            self.assertIsNone(session.handle_chat("web", "在干嘛"))
            self.assertEqual(session.planner.calls, [])


# ── 2. 对话记忆分区 / 回灌 / 摘要 ────────────────────────────────

class ConversationMemoryTests(unittest.TestCase):
    def test_namespace_follows_world_server_then_world_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = make_session(tmp)
            session.handle_event("state_update", {
                "in_world": True, "world": {"server": "1.2.3.4:25565", "world_name": "w"}})
            self.assertEqual(session.conversation.namespace, "1.2.3.4_25565")

            session.handle_event("state_update", {
                "in_world": True, "world": {"server": "", "world_name": "MyWorld"}})
            self.assertEqual(session.conversation.namespace, "MyWorld")

            session.handle_event("state_update", {"in_world": True, "world": {}})
            self.assertEqual(session.conversation.namespace, "unknown")

    def test_partition_isolation_and_restart_rehydrate(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = make_session(tmp)
            session.handle_event("state_update", {
                "in_world": True, "world": {"server": "serverA", "world_name": "w"}})
            session.handle_event("player_chat", {"sender": "A", "message": "这是A服的秘密"})
            session.handle_event("state_update", {
                "in_world": True, "world": {"server": "serverB", "world_name": "w"}})
            session.handle_event("player_chat", {"sender": "B", "message": "B服的事"})

            # 分区隔离：换服不串号
            self.assertFalse(any(entry.get("message") == "这是A服的秘密"
                                 for entry in session.conversation.recent()))
            # 切回 A 服：从 L1 原始事件回灌（不需要任何运行时状态序列化）
            session.conversation.use_namespace("serverA", "w")
            self.assertTrue(any(entry.get("message") == "这是A服的秘密"
                                for entry in session.conversation.recent()))

            # 模拟重启：全新 store 指向同一目录
            fresh = ConversationStore(root=os.path.join(tmp, "conversations"))
            fresh.use_namespace("serverA", "w")
            self.assertTrue(any(entry.get("message") == "这是A服的秘密"
                                for entry in fresh.recent()))

    def test_death_and_join_are_classified_into_memory(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = make_session(tmp)
            session.handle_event("state_update", {
                "in_world": True, "world": {"server": "srv", "world_name": "w"}})

            session.handle_event("player_chat", {
                "sender": "Steve", "message": "Steve was slain by Zombie", "is_system": True})
            session.handle_event("player_chat", {
                "sender": "Alex", "message": "Alex 加入了游戏", "is_system": True})

            kinds = [entry.get("type") for entry in session.conversation.recent()]
            self.assertIn("death", kinds)
            self.assertIn("join", kinds)
            # 死亡进了待注入队列（主动参与的入口一）
            self.assertEqual(session._pending_death["player"], "Steve")

    def test_window_trim_produces_summary_and_persists(self):
        with tempfile.TemporaryDirectory() as tmp:
            llm = FakeLLM({"summarizer": '{"summary": "两人在挖矿", "keywords": ["挖矿", "僵尸"]}'})
            session = make_session(tmp, conversation_llm=llm, window=2)
            session.handle_event("state_update", {
                "in_world": True, "world": {"server": "srv", "world_name": "w"}})
            for index in range(4):
                session.handle_event("player_chat", {"sender": "S", "message": f"第{index}句"})

            self.assertTrue(session.conversation.pending_summary())
            result = session._tick_memory_summary()

            self.assertIsNotNone(result)
            self.assertIn("summarizer", llm.agents())
            self.assertEqual(session.conversation.pending_summary(), [])
            summary = session.conversation.latest_summary()
            self.assertEqual(summary["summary"], "两人在挖矿")
            # 落盘且能被新实例读回
            fresh = ConversationStore(root=os.path.join(tmp, "conversations"))
            fresh.use_namespace("srv", "w")
            self.assertEqual(fresh.latest_summary()["summary"], "两人在挖矿")

    def test_window_limit_is_60_solo_and_40_multiplayer(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = make_session(tmp)
            session.runtime["players"] = [{"name": "A"}]
            self.assertEqual(session.conversation.window_limit(session._players_count()), 60)
            session.runtime["players"] = [{"name": "A"}, {"name": "B"}]
            self.assertEqual(session.conversation.window_limit(session._players_count()), 40)


# ── 3. 人设三段块 ───────────────────────────────────────────────

class _DummyMemory:
    def build_context(self):
        return {}

    def get_recent_context(self, _count):
        return []


class _DummySkills:
    def command_context(self, _name):
        import contextlib
        return contextlib.nullcontext()


class PersonaBlockTests(unittest.TestCase):
    def test_prompt_has_four_separate_blocks_and_state_is_not_identity(self):
        planner = Planner(llm_service=None, memory=_DummyMemory(), skills=_DummySkills())
        blocks = {
            "identity": "你是一个陪朋友玩 MC 的搭档。",
            "reply_style": "句子短，口语化。",
            "scene_rules": "- 私聊：随便聊\n- 公开服：少说话",
            "state_block": "生命 3/20 ｜ 饥饿 4/20\n正在听你说的是：Steve",
        }
        prompt = planner._build_planner_prompt("Steve", "hi", {"persona_blocks": blocks}, "AI")

        for marker in ("【身份】", "【表达习惯】", "【场景规则】", "【此刻状态"):
            self.assertIn(marker, prompt)
        identity_segment = prompt[prompt.index("【身份】"):prompt.index("【表达习惯】")]
        self.assertIn("陪朋友玩 MC", identity_segment)
        # state_block 的内容不得混进 identity 块
        self.assertNotIn("生命 3/20", identity_segment)
        self.assertIn("生命 3/20", prompt[prompt.index("【此刻状态"):])

    def test_prompt_falls_back_to_legacy_persona_when_blocks_missing(self):
        planner = Planner(llm_service=None, memory=_DummyMemory(), skills=_DummySkills())
        prompt = planner._build_planner_prompt(
            "Steve", "hi", {"persona": {"personality": "温和", "speaking_style": "简短"}}, "AI")

        self.assertIn("性格：温和", prompt)
        self.assertIn("说话风格：简短", prompt)

    def test_session_exposes_blocks_and_state_block_tracks_runtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = make_session(tmp)
            session.runtime["player"] = {"health": 7, "max_health": 20, "hunger": 12,
                                         "saturation": 2.5}
            session.runtime["task_state"] = {"kind": "collect", "status": "collecting",
                                             "detail": "oak_log 3/8", "progress": 0.4}
            session.last_speaker = {"name": "Steve"}

            blocks = session.persona_blocks("private")

            self.assertTrue(blocks["identity"])
            self.assertIn("7/20", blocks["state_block"])
            self.assertIn("Steve", blocks["state_block"])
            self.assertNotIn("7/20", blocks["identity"])
            self.assertNotEqual(blocks["scene_rules"], blocks["reply_style"])

    def test_scene_selection_follows_server_mode_and_mention(self):
        with tempfile.TemporaryDirectory() as tmp:
            private = make_session(tmp, server_mode="private")
            public = make_session(tmp, server_mode="public")
            self.assertEqual(private._scene_for("路人"), "private")
            self.assertEqual(public._scene_for("路人"), "public")
            self.assertEqual(public._scene_for("路人", mentioned=True), "mentioned")

    def test_missing_persona_file_falls_back_to_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = make_session(tmp)
            blocks = session.persona_blocks()

            self.assertGreater(len(blocks["identity"]), 0)
            self.assertNotEqual(blocks["identity"], blocks["state_block"])


# ── 4. 权限 + 回复门 ────────────────────────────────────────────

class PermissionGateTests(unittest.TestCase):
    def test_private_server_everyone_can_talk(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = make_session(tmp, server_mode="private")
            session.handle_event("player_chat", {"sender": "路人甲", "message": "在干嘛"})

            self.assertEqual(len(session.planner.calls), 1)

    def test_public_server_guest_is_silent_but_still_recorded(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = make_session(tmp, server_mode="public")
            session.handle_event("player_chat", {"sender": "路人甲", "message": "带我一个"})

            # 静默：零 LLM 调用、零下发
            self.assertEqual(session.planner.calls, [])
            self.assertEqual(session.llm.calls, [])
            self.assertEqual(session.skills.calls, [])
            # 但消息进了记忆（guest 在公开服仍要"听得见"）
            self.assertTrue(any(entry.get("message") == "带我一个"
                                for entry in session.conversation.recent()))
            self.assertEqual(session._chat_audit[-1]["decision"], "silent")

    def test_public_server_master_and_friend_still_work(self):
        with tempfile.TemporaryDirectory() as tmp:
            clock = Clock()
            session = make_session(tmp, server_mode="public", clock=clock,
                                   master=["Owner"], friends=["Buddy"])
            session.handle_event("player_chat", {"sender": "Owner", "message": "过来"})
            self.assertEqual(len(session.planner.calls), 1)   # master：立刻接
            clock.value += 5                                  # 隔开抗抖窗
            session.handle_event("player_chat", {"sender": "Buddy", "message": "挖点木头"})
            self.assertEqual(len(session.planner.calls), 1)   # friend 在公开服要攒够频率
            clock.value += 5
            session.handle_event("player_chat", {"sender": "Buddy", "message": "再挖点石头"})

            self.assertEqual(len(session.planner.calls), 2)
            self.assertEqual(session.planner.calls[1]["sender"], "Buddy")

    def test_public_guest_can_command_only_when_explicitly_allowed(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = make_session(tmp, server_mode="public")
            self.assertFalse(session.reply_gate.permissions.policy("guest").can_command)
            self.assertTrue(session.reply_gate.permissions.policy("master").can_command)
            self.assertGreater(session.reply_gate.permissions.task_priority("master"),
                               session.reply_gate.permissions.task_priority("friend"))
            self.assertGreater(session.reply_gate.permissions.task_priority("friend"),
                               session.reply_gate.permissions.task_priority("guest"))

    def test_frequency_threshold_waits_for_enough_messages(self):
        with tempfile.TemporaryDirectory() as tmp:
            clock = Clock()
            session = make_session(tmp, clock=clock)
            # 0.4 → 阈值 3（1/min(1,0.4)=2.5 → ceil=3）
            session.reply_gate.set_session_adjust(0.4)

            session.handle_event("player_chat", {"sender": "Steve", "message": "一"})
            clock.value += 5
            session.handle_event("player_chat", {"sender": "Steve", "message": "二"})
            self.assertEqual(session.planner.calls, [])  # 还没到阈值

            clock.value += 5
            session.handle_event("player_chat", {"sender": "Steve", "message": "三"})
            self.assertEqual(len(session.planner.calls), 1)
            self.assertEqual(session.planner.calls[0]["message"], "三")

    def test_mention_bypasses_the_frequency_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            clock = Clock()
            session = make_session(tmp, clock=clock)
            session.reply_gate.set_session_adjust(0.2)   # 阈值 5

            session.handle_event("player_chat", {"sender": "Steve", "message": "小A 在吗"})

            self.assertEqual(len(session.planner.calls), 1)
            self.assertTrue(any(record["reason"] == "mention_bypass"
                                for record in session._chat_audit))

    def test_debounce_window_defers_the_burst_then_tick_fires(self):
        with tempfile.TemporaryDirectory() as tmp:
            clock = Clock()
            session = make_session(tmp, clock=clock)

            session.handle_event("player_chat", {"sender": "Steve", "message": "一"})
            self.assertEqual(len(session.planner.calls), 1)   # 静默之后的头一条立刻进语义门
            clock.value += 0.4                                 # 0.4s 后还在打字 → 抗抖
            session.handle_event("player_chat", {"sender": "Steve", "message": "二"})

            self.assertEqual(len(session.planner.calls), 1)
            self.assertIsNotNone(session._pending_chat)

            clock.value += 1.6                                 # 抗抖窗过完
            session._tick_conversation_gate()

            self.assertEqual(len(session.planner.calls), 2)
            self.assertEqual(session.planner.calls[1]["message"], "二")
            self.assertIsNone(session._pending_chat)

    def test_no_action_puts_the_gate_in_cooldown(self):
        with tempfile.TemporaryDirectory() as tmp:
            clock = Clock()
            session = make_session(tmp, clock=clock)
            session.llm.verdicts["gate"] = '{"decision": "no_action"}'

            session.handle_event("player_chat", {"sender": "Steve", "message": "随便说点什么"})
            self.assertEqual(session.planner.calls, [])

            clock.value += 3.0
            session.llm.verdicts["gate"] = '{"decision": "continue"}'
            session.handle_event("player_chat", {"sender": "Steve", "message": "再来"})
            self.assertEqual(session.planner.calls, [])       # 冷却中

            clock.value += 6.0                                 # 8s 冷却过完
            session.handle_event("player_chat", {"sender": "Steve", "message": "现在呢"})
            self.assertEqual(len(session.planner.calls), 1)

    def test_ban_pattern_drops_the_whole_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = make_session(tmp)
            session.permissions.set({"ban_patterns": ["外挂|代练"]})

            session.handle_event("player_chat", {"sender": "Steve", "message": "卖外挂啦"})

            self.assertEqual(session.planner.calls, [])
            self.assertFalse(any("外挂" in str(entry.get("message"))
                                 for entry in session.conversation.recent()))
            self.assertEqual(session._chat_audit[-1]["decision"], "drop")

    def test_gate_status_is_exposed_for_the_dashboard(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = make_session(tmp)
            gate = session.get_status()["gate"]

            self.assertIn("effective", gate)
            self.assertIn("threshold", gate)
            self.assertIn("last_decisions", gate)
            self.assertIn("server_mode", gate)


# ── 5. 主动参与（事件注入）──────────────────────────────────────

class ProactiveEventTests(unittest.TestCase):
    def test_death_event_is_injected_and_can_be_spoken(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = make_session(tmp)
            session.handle_event("state_update", {
                "in_world": True, "world": {"server": "srv", "world_name": "w"}})
            session.handle_event("player_chat", {
                "sender": "Steve", "message": "Steve was slain by Zombie", "is_system": True})

            session._tick_events()

            self.assertEqual(len(session.planner.calls), 1)
            self.assertEqual(session.planner.calls[0]["context"]["event"]["kind"], "event")
            self.assertTrue(session.planner.calls[0]["context"]["event"]["intent"])
            self.assertTrue(any(entry[0] == "send_chat" for entry in session.skills.calls))

    def test_no_action_event_sends_nothing(self):
        """绝不提供"事件 → 必发消息"的短路。"""
        with tempfile.TemporaryDirectory() as tmp:
            session = make_session(tmp)
            session.llm.verdicts["gate"] = '{"decision": "no_action"}'

            spoke = session.inject_event("death", "路人死了", event={"player": "路人"})

            self.assertFalse(spoke)
            self.assertEqual(session.planner.calls, [])
            self.assertEqual(session.skills.calls, [])
            self.assertEqual(session._event_audit[-1]["verdict"], "no_action")

    def test_events_are_throttled_per_kind_and_never_fire_out_of_world(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = make_session(tmp)
            self.assertTrue(session.inject_event("death", "有人死了", now=1000.0))
            self.assertFalse(session.inject_event("death", "又有人死了", now=1005.0))
            self.assertEqual(len(session.planner.calls), 1)

            session.in_world = False
            self.assertFalse(session.inject_event("rare_drop", "掉到钻石", now=2000.0))
            self.assertEqual(len(session.planner.calls), 1)

    def test_plain_system_message_is_not_an_event(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = make_session(tmp)
            session.handle_event("player_chat", {
                "sender": "Server", "message": "欢迎来到服务器", "is_system": True})

            session._tick_events()

            self.assertEqual(session.planner.calls, [])

    def test_event_state_is_only_consumed_when_it_is_actually_injected(self):
        """被语义门挡掉的事件不该被"吃掉"：下一拍还能再试（同类冷却会挡住重复烧 token）。"""
        with tempfile.TemporaryDirectory() as tmp:
            session = make_session(tmp)
            session.llm.verdicts["gate"] = '{"decision": "no_action"}'
            session.handle_event("player_chat", {
                "sender": "Steve", "message": "Steve was slain by Zombie", "is_system": True})

            session._tick_events()
            self.assertIsNotNone(session._pending_death)      # 没注入 → 状态还在

            session.llm.verdicts["gate"] = '{"decision": "continue"}'
            session._event_cooldowns.clear()                  # 模拟冷却过后再试
            session._last_event_at = 0.0
            session._tick_events()
            self.assertIsNone(session._pending_death)         # 注入成功 → 消费掉

    def test_task_failure_becomes_an_event_candidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = make_session(tmp)
            session.runtime["task_state"] = {"kind": "collect", "status": "failed",
                                             "reason": "stuck: 挖不到"}
            session._last_task_event = {"kind": "collect", "status": "failed",
                                        "reason": "stuck: 挖不到"}

            candidates = session._event_candidates()

            self.assertTrue(any(candidate[0] == "task_failed" for candidate in candidates))


# ── 6. 任务列表 ─────────────────────────────────────────────────

class TaskListFlowTests(unittest.TestCase):
    def test_dispatched_command_registers_task_with_owner(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = make_session(tmp, master=["Owner"])
            session._actor = {"name": "Owner", "tier": "master"}

            session._on_skill_command("collect_blocks", "req_1", "manual_chat",
                                      {"block_type": "oak_log", "count": 5})

            items = session.tasks.items()
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0].kind, "collect")
            self.assertEqual(items[0].owner, "Owner")
            self.assertEqual(items[0].owner_tier, "master")

    def test_task_state_updates_progress_and_failure_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = make_session(tmp)
            session._actor = {"name": "Owner", "tier": "master"}
            session._on_skill_command("collect_blocks", "req_1", "manual_chat",
                                      {"block_type": "oak_log"})

            session.handle_event("task_state", {"kind": "collect", "status": "collecting",
                                                "progress": 0.5, "detail": "3/8"})
            self.assertAlmostEqual(session.tasks.items()[0].progress, 0.5)

            session.handle_event("task_state", {"kind": "collect", "status": "failed",
                                                "reason": "stuck: 挖不到"})
            item = session.tasks.items()[0]
            self.assertEqual(item.status, "failed")
            self.assertIn("stuck", item.failure_reason)

    def test_master_can_cancel_and_activate_any_task_friend_only_own(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = make_session(tmp, master=["Owner"], friends=["Buddy"])
            session._actor = {"name": "Buddy", "tier": "friend"}
            session._on_skill_command("craft_item", "req_1", "manual_chat",
                                      {"item": "stick", "count": 4})
            task_id = session.tasks.items()[0].id

            self.assertEqual(session.cancel_task(task_id, actor="路人"), (False, "denied"))
            self.assertTrue(session.cancel_task(task_id, actor="Owner")[0])

    def test_activate_preempts_and_keeps_a_single_active_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = make_session(tmp, master=["Owner"])
            session._actor = {"name": "Owner", "tier": "master"}
            session._on_skill_command("craft_item", "req_1", "manual_chat", {"item": "stick"})
            session._on_skill_command("collect_blocks", "req_2", "manual_chat",
                                      {"block_type": "stone"})
            first, second = session.tasks.items()[0], session.tasks.items()[1]

            ok, _reason = session.activate_task(second.id, actor="Owner")

            self.assertTrue(ok)
            self.assertEqual(session.tasks.active().id, second.id)
            self.assertEqual(session.tasks.get(first.id).status, "pending")

    def test_autonomous_command_is_not_registered_as_a_player_task(self):
        """自主/反射行为没有主人，不该被算成"某个玩家下的任务"。"""
        with tempfile.TemporaryDirectory() as tmp:
            session = make_session(tmp)
            session._actor = {"name": "Owner", "tier": "master"}

            session._on_skill_command("collect_blocks", "req_1", "mode", {"block_type": "stone"})
            self.assertEqual(session.tasks.items(), [])

            # 模组回报后仍然会出现在列表里，但明确是"无主"、最低权重
            session.handle_event("task_state", {"kind": "collect", "status": "collecting",
                                                "progress": 0.2})
            item = session.tasks.items()[0]
            self.assertEqual(item.owner, "")
            self.assertEqual(item.owner_tier, "guest")

    def test_task_state_progress_links_back_to_the_ordered_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = make_session(tmp, master=["Owner"])
            session._actor = {"name": "Owner", "tier": "master"}
            session._on_skill_command("craft_item", "req_1", "manual_chat", {"item": "stick"})
            task_id = session.tasks.items()[0].id

            session.handle_event("task_state", {"kind": "craft", "status": "crafting",
                                                "progress": 0.6, "detail": "2/4"})

            self.assertEqual(len(session.tasks.items()), 1)   # 不产生第二条
            updated = session.tasks.get(task_id)
            self.assertAlmostEqual(updated.progress, 0.6)
            self.assertEqual(updated.status, "active")

    def test_status_exposes_task_list_and_keeps_legacy_task_field(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = make_session(tmp)
            session.runtime["task_state"] = {"kind": "craft", "status": "crafting",
                                             "progress": 0.25, "detail": "stick"}
            session._actor = {"name": "Owner", "tier": "master"}
            session._on_skill_command("craft_item", "req_1", "manual_chat", {"item": "stick"})

            status = session.get_status()

            self.assertIn("task_list", status)
            self.assertTrue(status["task_list"]["items"])
            self.assertEqual(status["task"]["kind"], "craft")
            json.dumps(status)   # 必须可序列化（/api/status 直接吐出去）


if __name__ == "__main__":
    unittest.main()
