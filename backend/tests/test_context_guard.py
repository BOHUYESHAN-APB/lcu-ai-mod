"""玩家语境 / 守卫 / `/locate` / 饱食度：本轮新增能力的回归测试。

分四块：
  1. 技能层真要发出去的 wire 帧（参数名写错 Java 只会落进 default，静默不生效）；
  2. 有没有/怎么把"最近发话者 + 他发言那一刻的坐标 + 近期聊天"喂给 LLM 与仪表盘；
  3. `/locate` 的回复解析（成功/找不到/超时）与"绝不编坐标"的诚实回报；
  4. 吃/饱食度：缺血先吃、选食物按饱和度/营养值分档。
"""

import json
import unittest

from agent.modes_engine import (AutoEatMode, FOOD_PROPERTIES, HARMFUL_FOOD,
                                RAW_FOOD, select_food)
from agent.planner import (GUARD_DEFAULT_RADIUS, Planner, is_locate_failure,
                           looks_like_locate_reply, normalize_locate_name,
                           parse_locate_reply)
from agent.session import ChatInbox, Session
from agent.skills import Skills
from protocol.wire_client import WireClient


# ── 假件 ────────────────────────────────────────────────────────

class FakeSocket:
    """只实现 sendall/recv 的假 socket：让真实的编码/换行/读取逻辑照常跑。"""

    def __init__(self, chunks=None):
        self.chunks: list[bytes] = []
        self.recv_chunks: list[bytes] = list(chunks or [])

    def sendall(self, data: bytes) -> None:
        self.chunks.append(data)

    def recv(self, _size: int) -> bytes:
        return self.recv_chunks.pop(0) if self.recv_chunks else b""

    def close(self) -> None:
        pass


class DummyMemory:
    def build_context(self):
        return {}

    def get_recent_context(self, _count):
        return []


class DummySkills:
    """记录被调用的技能；`on_run_command` 用来模拟"模组立刻回话"。"""

    def __init__(self, on_run_command=None):
        self.calls = []
        self._on_run_command = on_run_command

    def guard(self, x=None, y=None, z=None, radius=32):
        self.calls.append(("guard", x, y, z, radius))

    def guard_off(self):
        self.calls.append(("guard_off",))

    def set_hud_note(self, text, seconds=30.0):
        self.calls.append(("set_hud_note", text, seconds))

    def run_command(self, command):
        self.calls.append(("run_command", command))
        if self._on_run_command:
            self._on_run_command(command)


class FakeLLM:
    def __init__(self, responses):
        self.api_key = "test-key"
        self._responses = responses
        self.calls = []

    def chat(self, messages, agent=None, **kwargs):
        self.calls.append({"agent": agent, "kwargs": kwargs})
        value = self._responses.get(agent)
        if isinstance(value, Exception):
            raise value
        return {"content": value}


# ── 1. wire 帧 ──────────────────────────────────────────────────

class NewCommandFrameTests(unittest.TestCase):
    """新技能的 JSONL 帧必须与 Java `ActionExecutor` 的字段名逐字对齐。"""

    def setUp(self):
        self.wire = WireClient("127.0.0.1", 25568)
        self.fake = FakeSocket()
        self.wire.sock = self.fake
        self.skills = Skills(wire=self.wire)

    def _frames(self):
        raw = b"".join(self.fake.chunks).decode("utf-8")
        return [json.loads(line) for line in raw.splitlines()]

    def test_run_command_frame_strips_leading_slash(self):
        self.skills.run_command("/locate structure minecraft:village")

        frame = self._frames()[-1]
        self.assertEqual(frame["cmd"], "run_command")
        # 带 `/` 会被原版当成聊天内容（sendCommand 自己会再加一个）→ 必须剥掉。
        self.assertEqual(frame["args"], {"command": "locate structure minecraft:village"})

    def test_run_command_strips_repeated_slashes_and_spaces(self):
        self.skills.run_command("  //locate biome minecraft:desert  ")

        self.assertEqual(self._frames()[-1]["args"], {"command": "locate biome minecraft:desert"})

    def test_guard_frame_matches_java_contract(self):
        self.skills.guard(10.5, 64.0, -3.5, radius=32)

        frame = self._frames()[-1]
        self.assertEqual(frame["cmd"], "guard")
        self.assertEqual(frame["args"], {"radius": 32, "x": 10.5, "y": 64.0, "z": -3.5})

    def test_guard_without_coordinates_sends_only_radius(self):
        """省略坐标＝Java 侧回退到"当前脚下"。"""
        self.skills.guard(radius=16)

        self.assertEqual(self._frames()[-1]["args"], {"radius": 16})

    def test_guard_off_uses_enabled_false(self):
        self.skills.guard_off()

        frame = self._frames()[-1]
        self.assertEqual(frame["cmd"], "guard")
        self.assertEqual(frame["args"], {"enabled": False})

    def test_set_hud_note_frame_matches_java_contract(self):
        self.skills.set_hud_note("找床睡觉中，天亮继续挖", 30)

        frame = self._frames()[-1]
        self.assertEqual(frame["cmd"], "set_hud_note")
        self.assertEqual(frame["args"], {"text": "找床睡觉中，天亮继续挖", "seconds": 30.0})


# ── 2. 聊天语境：读取线程挂钩 + 最近发话者 ───────────────────────

class WireChatTapTests(unittest.TestCase):
    """聊天事件必须在 **wire 读取线程**就被旁路出去。

    否则等 `/locate` 回复的那个线程（orchestrator 主循环）正阻塞在 planner 里，
    没人消费事件队列 → 永远超时。这里用假 socket 走真实 `_read_loop`。
    """

    def _wire_with(self, lines):
        wire = WireClient("127.0.0.1", 25568)
        wire.sock = FakeSocket([line.encode("utf-8") + b"\n" for line in lines])
        wire._running = True
        return wire

    def test_chat_events_are_delivered_to_listener(self):
        wire = self._wire_with([
            json.dumps({"type": "event", "event": "player_chat",
                        "data": {"sender": "Alice", "message": "在这儿守着"}}),
        ])
        seen = []
        wire.add_chat_listener(seen.append)

        wire._read_loop()

        self.assertEqual(seen, [{"sender": "Alice", "message": "在这儿守着"}])

    def test_non_chat_events_are_not_delivered(self):
        wire = self._wire_with([
            json.dumps({"type": "event", "event": "state_update", "data": {"player": {}}}),
            json.dumps({"type": "response", "id": "req_1", "success": True}),
            json.dumps({"type": "event", "event": "system_chat",
                        "data": {"sender": "[系统]", "message": "x", "is_system": True}}),
        ])
        seen = []
        wire.add_chat_listener(seen.append)

        wire._read_loop()

        # state_update/response 不进监听器；system_chat 也算聊天事件（/locate 的回复走这条）。
        self.assertEqual(seen, [{"sender": "[系统]", "message": "x", "is_system": True}])

    def test_failing_listener_does_not_kill_the_read_loop(self):
        wire = self._wire_with([
            json.dumps({"type": "event", "event": "player_chat", "data": {"sender": "A"}}),
            json.dumps({"type": "event", "event": "player_chat", "data": {"sender": "B"}}),
        ])
        seen = []

        def boom(data):
            seen.append(data["sender"])
            raise RuntimeError("listener exploded")

        wire.add_chat_listener(boom)

        wire._read_loop()

        self.assertEqual(seen, ["A", "B"])


class SpeakerTrackingTests(unittest.TestCase):
    def _session(self, tap_registered=True):
        session = Session.__new__(Session)
        session.chat_inbox = ChatInbox()
        session.runtime = {}
        session._chat_tap_registered = tap_registered
        return session

    def test_records_speaker_with_coordinates_at_speech_time(self):
        session = self._session()

        session._note_chat({
            "sender": "Alice", "message": "在这儿守着",
            "sender_x": 10.5, "sender_y": 64.0, "sender_z": -3.5, "sender_distance": 4.2,
        })

        self.assertEqual(session.last_speaker["name"], "Alice")
        # 守卫要守的是**他**站的地方，不是 AI 自己的位置。
        self.assertEqual(
            (session.last_speaker["x"], session.last_speaker["y"], session.last_speaker["z"]),
            (10.5, 64.0, -3.5))
        self.assertEqual(session.last_speaker["distance"], 4.2)

    def test_system_messages_enter_inbox_but_do_not_become_speaker(self):
        session = self._session()
        session._note_chat({"sender": "Alice", "message": "hi"})

        session._note_chat({"sender": "[系统]", "message": "The nearest village is at [1, 2, 3]",
                            "is_system": True})

        self.assertEqual(session.last_speaker["name"], "Alice")
        self.assertEqual(len(session.chat_inbox.recent(8)), 2)
        self.assertTrue(session.chat_inbox.recent(1)[0]["is_system"])

    def test_inbox_keeps_time_order(self):
        session = self._session()
        for name in ("Alice", "Bob", "Carol"):
            session._note_chat({"sender": name, "message": f"hi from {name}"})

        senders = [entry["sender"] for entry in session.chat_inbox.recent(8)]

        self.assertEqual(senders, ["Alice", "Bob", "Carol"])
        self.assertLessEqual(session.chat_inbox.recent(2)[0]["time"], session.chat_inbox.recent(2)[1]["time"])


class ChatInboxTests(unittest.TestCase):
    def test_wait_after_only_accepts_entries_after_the_cursor(self):
        inbox = ChatInbox()
        inbox.push({"message": "[1, 1, 1]"})           # 发命令之前就有的旧消息
        cursor = inbox.cursor

        inbox.push({"message": "The nearest village is at [7, 64, -9]"})

        entry = inbox.wait_after(cursor, looks_like_locate_reply, 0.2)
        self.assertIsNotNone(entry)
        self.assertIn("[7, 64, -9]", entry["message"])

    def test_wait_after_times_out_on_empty_inbox(self):
        inbox = ChatInbox()

        self.assertIsNone(inbox.wait_after(inbox.cursor, looks_like_locate_reply, 0.05))

    def test_recent_returns_chronological_tail(self):
        inbox = ChatInbox(limit=3)
        for index in range(5):
            inbox.push({"message": str(index)})

        self.assertEqual([entry["message"] for entry in inbox.recent(8)], ["2", "3", "4"])


# ── 3. /locate：查询词规范化与回复解析 ──────────────────────────

class LocateParsingTests(unittest.TestCase):
    def test_parses_english_reply(self):
        self.assertEqual(
            parse_locate_reply("The nearest minecraft:village is at [123, 64, -456] (789 blocks away)"),
            (123.0, 64.0, -456.0))

    def test_parses_chinese_reply(self):
        self.assertEqual(
            parse_locate_reply("最近的 minecraft:village 位于 [123, 64, -456]（789 格外）"),
            (123.0, 64.0, -456.0))

    def test_parses_round_brackets_and_fullwidth_commas(self):
        self.assertEqual(parse_locate_reply("Located biome at (12，70，-3)"),
                         (12.0, 70.0, -3.0))

    def test_returns_none_when_no_coordinates(self):
        self.assertIsNone(parse_locate_reply("Unable to locate any structure"))
        self.assertIsNone(parse_locate_reply(""))
        self.assertIsNone(parse_locate_reply(None))

    def test_failure_markers_english_and_chinese(self):
        self.assertTrue(is_locate_failure("Unable to locate any structure of type minecraft:village"))
        self.assertTrue(is_locate_failure("无法找到任何结构"))
        self.assertFalse(is_locate_failure("The nearest village is at [1, 2, 3]"))

    def test_normalizes_chinese_and_bare_ids(self):
        self.assertEqual(normalize_locate_name("村庄"), "minecraft:village")
        self.assertEqual(normalize_locate_name("village"), "minecraft:village")
        # 模型把整句抄进来也要能用（取最后一段）
        self.assertEqual(normalize_locate_name("/locate structure minecraft:fortress"),
                         "minecraft:fortress")
        self.assertEqual(normalize_locate_name("minecraft:fortress"), "minecraft:fortress")
        self.assertEqual(normalize_locate_name("#village"), "#minecraft:village")
        self.assertEqual(normalize_locate_name(""), "")

    def test_normalizes_biome_names(self):
        self.assertEqual(normalize_locate_name("沙漠", kind="biome"), "minecraft:desert")
        self.assertEqual(normalize_locate_name("plains", kind="biome"), "minecraft:plains")

    def test_player_chatter_without_context_is_not_a_locate_reply(self):
        """玩家随手打一句坐标不算 /locate 的回复（系统消息才直接算）。"""
        self.assertFalse(looks_like_locate_reply({"message": "[1, 2, 3] 来这", "is_system": False}))
        self.assertTrue(looks_like_locate_reply({"message": "[1, 2, 3]", "is_system": True}))


class LocateStructureTests(unittest.TestCase):
    """端到端：发命令 → 模组回话 → 解析坐标；查不到就诚实失败。"""

    def _planner(self, reply=None, is_system=True, inbox=None, timeout=0.3):
        inbox = inbox if inbox is not None else ChatInbox()

        def on_run_command(_command):
            if reply is not None:
                inbox.push({"sender": "[系统]", "message": reply,
                            "is_system": is_system, "time": 0.0})

        skills = DummySkills(on_run_command=on_run_command)
        planner = Planner(None, DummyMemory(), skills, chat_inbox=inbox)
        return planner, skills, inbox

    def test_success_parses_coordinates(self):
        planner, skills, _ = self._planner(
            "The nearest minecraft:village is at [123, 64, -456] (789 blocks away)")

        result = planner.locate_structure("村庄", timeout=0.3)

        self.assertTrue(result["ok"])
        self.assertEqual((result["x"], result["y"], result["z"]), (123.0, 64.0, -456.0))
        self.assertEqual(skills.calls, [("run_command", "locate structure minecraft:village")])

    def test_success_parses_chinese_system_reply(self):
        planner, _, _ = self._planner("最近的 minecraft:village 位于 [10, 70, -20]（42 格外）")

        result = planner.locate_structure("minecraft:village", timeout=0.3)

        self.assertTrue(result["ok"])
        self.assertEqual((result["x"], result["z"]), (10.0, -20.0))

    def test_biome_query_uses_locate_biome(self):
        planner, skills, _ = self._planner("Located biome at [1, 64, 2]")

        result = planner.locate_structure("沙漠", kind="biome", timeout=0.3)

        self.assertTrue(result["ok"])
        self.assertEqual(skills.calls, [("run_command", "locate biome minecraft:desert")])

    def test_not_found_is_reported_honestly(self):
        planner, _, _ = self._planner("Unable to locate any structure of type minecraft:village")

        result = planner.locate_structure("village", timeout=0.3)

        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "not_found")
        self.assertNotIn("x", result)

    def test_timeout_is_reported_honestly_and_never_invents_coordinates(self):
        planner, skills, _ = self._planner(reply=None)

        result = planner.locate_structure("village", timeout=0.05)

        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "timeout")
        self.assertNotIn("x", result)
        self.assertEqual(skills.calls, [("run_command", "locate structure minecraft:village")])

    def test_unrecognized_reply_waits_then_reports_timeout_without_coordinates(self):
        """回复格式不认识（不是游戏的标准话术）→ 等满超时，如实说没拿到，且不编坐标。"""
        planner, _, _ = self._planner("村庄大概在西边吧", is_system=True)

        result = planner.locate_structure("village", timeout=0.05)

        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "timeout")
        self.assertNotIn("x", result)
        self.assertNotIn("y", result)
        self.assertNotIn("z", result)

    def test_empty_name_sends_nothing(self):
        planner, skills, _ = self._planner()

        result = planner.locate_structure("", timeout=0.05)

        self.assertEqual(result["reason"], "empty_name")
        self.assertEqual(skills.calls, [])

    def test_without_chat_inbox_it_says_so(self):
        skills = DummySkills()
        planner = Planner(None, DummyMemory(), skills, chat_inbox=None)

        result = planner.locate_structure("village")

        self.assertEqual(result["reason"], "no_chat_inbox")
        self.assertEqual(skills.calls, [("run_command", "locate structure minecraft:village")])

    def test_reply_includes_real_coordinates_from_the_game(self):
        """坐标只有游戏回话才知道：必须由后端补进回复，而不是让模型瞎写。"""
        candidates = json.dumps({
            "reply": "我去查一下村庄在哪",
            "candidates": [{"tool": "locate_structure",
                            "args": {"name": "minecraft:village"}, "why": "用户在找村庄"}],
        })
        inbox = ChatInbox()

        def on_run_command(_command):
            inbox.push({"sender": "[系统]",
                        "message": "The nearest minecraft:village is at [123, 64, -456] (789 blocks away)",
                        "is_system": True})

        skills = DummySkills(on_run_command=on_run_command)
        planner = Planner(FakeLLM({"planner": candidates, "decider": '{"choice": 0, "confidence": 0.9}'}),
                          DummyMemory(), skills, chat_inbox=inbox)

        reply = planner.plan_and_execute("Alice", "村庄在哪", {})

        self.assertIn("我去查一下村庄在哪", reply)
        self.assertIn("123", reply)
        self.assertIn("-456", reply)

    def test_reply_is_honest_when_locate_times_out(self):
        candidates = json.dumps({
            "reply": "我去查一下",
            "candidates": [{"tool": "locate_structure", "args": {"name": "village"}, "why": "找村庄"}],
        })
        skills = DummySkills()
        planner = Planner(FakeLLM({"planner": candidates, "decider": '{"choice": 0, "confidence": 0.9}'}),
                          DummyMemory(), skills, chat_inbox=ChatInbox())
        planner._last_locate_result = {"ok": False, "reason": "timeout",
                                       "command": "locate structure minecraft:village"}

        reply = planner._with_locate_note("我去查一下")

        self.assertIn("没等到", reply)
        self.assertNotIn("[", reply)


# ── 4. 守卫 / HUD 描述词的分发 ──────────────────────────────────

class GuardDispatchTests(unittest.TestCase):
    def _planner(self, skills=None):
        skills = skills or DummySkills()
        return skills, Planner(None, DummyMemory(), skills)

    def test_guard_uses_last_speaker_position_and_default_radius(self):
        skills, planner = self._planner()

        dispatched = planner._dispatch_tool_call(
            "guard", {},
            {"last_speaker": {"name": "Alice", "x": 10.5, "y": 64.0, "z": -3.5}})

        self.assertTrue(dispatched)
        self.assertEqual(skills.calls, [("guard", 10.5, 64.0, -3.5, GUARD_DEFAULT_RADIUS)])
        self.assertEqual(GUARD_DEFAULT_RADIUS, 32)

    def test_guard_without_any_coordinates_is_not_dispatched(self):
        skills, planner = self._planner()

        dispatched = planner._dispatch_tool_call("guard", {}, {})

        self.assertFalse(dispatched)
        self.assertEqual(skills.calls, [])

    def test_guard_accepts_explicit_coordinates_and_radius(self):
        skills, planner = self._planner()

        planner._dispatch_tool_call(
            "guard", {"x": 1, "y": 2, "z": 3, "radius": 16},
            {"last_speaker": {"name": "Alice", "x": 99, "y": 99, "z": 99}})

        self.assertEqual(skills.calls, [("guard", 1.0, 2.0, 3.0, 16)])

    def test_guard_off_dispatches(self):
        skills, planner = self._planner()

        self.assertTrue(planner._dispatch_tool_call("guard_off", {}, {}))

        self.assertEqual(skills.calls, [("guard_off",)])

    def test_set_hud_note_dispatches_with_text(self):
        skills, planner = self._planner()

        self.assertTrue(planner._dispatch_tool_call("set_hud_note", {"text": "挖矿中"}, {}))

        self.assertEqual(skills.calls, [("set_hud_note", "挖矿中", 30.0)])

    def test_set_hud_note_rejects_missing_text(self):
        skills, planner = self._planner()

        self.assertFalse(planner._dispatch_tool_call("set_hud_note", {}, {}))

        self.assertEqual(skills.calls, [])

    def test_locate_structure_dispatches_run_command(self):
        skills, planner = self._planner()

        dispatched = planner._dispatch_tool_call(
            "locate_structure", {"name": "minecraft:village"}, {})

        self.assertTrue(dispatched)
        self.assertEqual(skills.calls, [("run_command", "locate structure minecraft:village")])


# ── 5. prompt：语境 / 饱食度 / 新规则 ───────────────────────────

class ContextPromptTests(unittest.TestCase):
    def _prompt(self, context=None):
        planner = Planner(None, DummyMemory(), DummySkills())
        return planner._build_planner_prompt("Alice", "在干嘛", context or {}, "AI")

    def test_prompt_has_recent_chat_block_with_time_and_distance(self):
        now = __import__("time").time()
        prompt = self._prompt({
            "recent_chat": [
                {"sender": "Alice", "message": "在这儿守着", "time": now - 5,
                 "sender_distance": 4.2},
                {"sender": "Bob", "message": "别管她", "time": now - 120},
            ],
        })

        self.assertIn("最近聊天上下文", prompt)
        self.assertIn("(5秒前) Alice（距你 4.2m）: 在这儿守着", prompt)
        self.assertIn("(2分钟前) Bob: 别管她", prompt)

    def test_prompt_marks_last_speaker_and_position(self):
        prompt = self._prompt({
            "last_speaker": {"name": "Alice", "x": 10.0, "y": 64.0, "z": -3.0},
            "players": [{"name": "Alice", "distance": 4.2, "speaking": True},
                        {"name": "Bob", "distance": 30.0, "speaking": False}],
        })

        self.assertIn("最近发话者：Alice，说话时站在 (10, 64, -3)", prompt)
        self.assertIn("Alice（距你 4.2m） ← 最近跟你说话的就是他", prompt)
        self.assertIn("Bob（距你 30.0m）", prompt)

    def test_prompt_defaults_when_nobody_talked(self):
        prompt = self._prompt({})

        self.assertIn("（还没人说过话）", prompt)
        self.assertIn("（视野里没有别的玩家）", prompt)

    def test_prompt_explains_hunger_and_saturation(self):
        prompt = self._prompt({"player": {"health": 10, "max_health": 20,
                                          "hunger": 16, "saturation": 1.5}})

        self.assertIn("血量 10/20", prompt)
        self.assertIn("饱食度 16/20", prompt)
        self.assertIn("饱和度 1.5", prompt)
        self.assertIn("自然回血条件：饱食度 ≥18", prompt)
        self.assertIn("饱和度 >0", prompt)
        self.assertIn("高饱和度", prompt)

    def test_prompt_rules_cover_guard_and_locate(self):
        prompt = self._prompt({})

        self.assertIn("→ guard", prompt)
        self.assertIn("别自己编坐标", prompt)
        self.assertIn("guard_off", prompt)
        self.assertIn("locate_structure", prompt)
        self.assertIn("别写死成某一个", prompt)
        self.assertIn("set_hud_note", prompt)

    def test_new_tools_are_available_in_every_phase(self):
        from agent.planner import PHASE_GROUPS, _actions_for_phase

        for phase in PHASE_GROUPS:
            names = [name for name, _, _ in _actions_for_phase(phase)]
            for tool in ("guard", "guard_off", "set_hud_note", "locate_structure"):
                self.assertIn(tool, names, f"{phase} 阶段缺少 {tool}")


# ── 6. 吃 / 饱食度 ──────────────────────────────────────────────

class AutoEatTests(unittest.TestCase):
    def _state(self, **player):
        base = {"health": 20, "max_health": 20, "hunger": 20}
        base.update(player)
        return {"player": base}

    def _skills(self):
        skills = DummySkills()
        skills.select_hotbar = lambda index: skills.calls.append(("select_hotbar", index))
        skills.use_item = lambda: skills.calls.append(("use_item",))
        skills.eat = lambda: skills.calls.append(("eat",)) or {
            "success": True, "message": "eat sent", "req_id": "req_eat"}
        return skills

    def test_hurt_with_unsaturated_hunger_triggers_eat(self):
        """用户口径：健康不满就叫到饱食度 18+，不是只看到 14 才吃。"""
        mode = AutoEatMode()

        self.assertTrue(mode.should_activate(self._state(health=16, hunger=16)))

    def test_full_health_with_hunger_16_does_not_trigger(self):
        mode = AutoEatMode()

        self.assertFalse(mode.should_activate(self._state(health=20, hunger=16)))

    def test_plain_hunger_still_triggers_at_14(self):
        mode = AutoEatMode()

        self.assertTrue(mode.should_activate(self._state(health=20, hunger=13)))

    def test_unknown_player_state_does_not_trigger(self):
        mode = AutoEatMode()

        self.assertFalse(mode.should_activate({"player": {}}))

    def test_hurt_prefers_highest_saturation(self):
        food = select_food([
            {"slot": 0, "name": "minecraft:cooked_beef"},    # 8 / 12.8
            {"slot": 1, "name": "minecraft:golden_carrot"},  # 6 / 14.4（饱和度最高）
        ], hurt=True)

        self.assertEqual(food["slot"], 1)

    def test_plain_hunger_prefers_highest_nutrition(self):
        food = select_food([
            {"slot": 0, "name": "minecraft:cooked_beef"},    # 8 / 12.8
            {"slot": 1, "name": "minecraft:rabbit_stew"},    # 10 / 12.0（营养最高）
        ], hurt=False)

        self.assertEqual(food["slot"], 1)

    def test_harmful_food_is_never_eaten(self):
        for name in ("minecraft:rotten_flesh", "minecraft:pufferfish", "minecraft:spider_eye"):
            self.assertIn(name, HARMFUL_FOOD)
            self.assertIsNone(select_food([{"slot": 0, "name": name}], hurt=True))

    def test_raw_food_is_only_a_fallback(self):
        self.assertIsNone(select_food([{"slot": 0, "name": "minecraft:unknown_stew"}], hurt=True))

        raw_only = select_food([{"slot": 3, "name": "minecraft:beef"}], hurt=True)
        self.assertEqual(raw_only["slot"], 3)
        self.assertIn("minecraft:beef", RAW_FOOD)

        both = select_food([
            {"slot": 3, "name": "minecraft:beef"},
            {"slot": 4, "name": "minecraft:bread"},
        ], hurt=True)
        self.assertEqual(both["slot"], 4)

    def test_tick_dispatches_eat_to_java(self):
        mode = AutoEatMode()
        skills = self._skills()
        state = self._state(health=12, hunger=16)
        state["inventory"] = [{"slot": 2, "name": "minecraft:golden_carrot"}]

        message = mode.tick(skills, state)

        # Java 新 `eat` 自己选格子 + 按住右键直到生效 → Python 不再 select_hotbar/use_item。
        self.assertEqual(skills.calls, [("eat",)])
        self.assertIn("saturation-first", message)

    def test_tick_ignores_food_in_backpack(self):
        mode = AutoEatMode()
        skills = self._skills()
        state = self._state(health=12, hunger=16)
        state["inventory"] = [{"slot": 12, "name": "minecraft:golden_carrot"}]

        message = mode.tick(skills, state)

        self.assertEqual(skills.calls, [])
        self.assertEqual(message, "auto_eat: no food in hotbar")

    def test_food_table_holds_vanilla_values(self):
        # 抽查几个原版数值，防止表被改成"看起来差不多"的猜测值。
        self.assertEqual(FOOD_PROPERTIES["minecraft:golden_carrot"], (6, 14.4))
        self.assertEqual(FOOD_PROPERTIES["minecraft:cooked_beef"], (8, 12.8))
        self.assertEqual(FOOD_PROPERTIES["minecraft:bread"], (5, 6.0))


# ── 7. /api/status 的新字段 ─────────────────────────────────────

class StatusShapeTests(unittest.TestCase):
    def _session(self, runtime=None):
        from types import SimpleNamespace

        session = Session.__new__(Session)
        session.id = "t"
        session.action_manager = SimpleNamespace(get_status=lambda: {})
        session.planner = SimpleNamespace(get_status=lambda: {})
        session.modes_engine = SimpleNamespace(get_status=lambda: {})
        session.self_prompter = SimpleNamespace(get_status=lambda: {})
        session.control = SimpleNamespace(holding=False)
        session.memory = SimpleNamespace(interaction_count=0)
        session.message_db = SimpleNamespace(get_stats=lambda: {})
        session.runtime = runtime if runtime is not None else {}
        session.vision = SimpleNamespace(get_status=lambda: {})
        session.poi = SimpleNamespace(namespace="srv__world", find=lambda _kind: [])
        session.llm = SimpleNamespace(get_usage=lambda: {})
        return session

    def test_status_exposes_players_task_hud_note_and_speaker(self):
        session = self._session({
            "players": [
                {"name": "Alice", "x": 10.0, "y": 64.0, "z": -3.0, "distance": 4.2, "speaking": False},
                {"name": "Bob", "x": 30.0, "y": 64.0, "z": 8.0, "distance": 30.0, "speaking": True},
            ],
            "task_state": {"kind": "guard", "status": "running", "target": "10,64,-3",
                           "detail": "guarding r=32m", "progress": 0.5},
            "hud_note": "守着你说的这个点",
        })
        session.last_speaker = {"name": "Alice", "x": 10.0, "y": 64.0, "z": -3.0, "time": 1.0}

        status = session.get_status()

        self.assertEqual(len(status["players"]), 2)
        # 后端自己记的最近发话者也要标上（旧版模组没有 speaking 字段时的兜底）。
        self.assertTrue(status["players"][0]["speaking"])
        self.assertTrue(status["players"][1]["speaking"])
        self.assertEqual(status["task"], {"kind": "guard", "status": "running", "target": "10,64,-3",
                                          "detail": "guarding r=32m", "progress": 0.5})
        self.assertEqual(status["hud_note"], "守着你说的这个点")
        self.assertEqual(status["last_speaker"]["name"], "Alice")

    def test_status_is_safe_with_empty_runtime(self):
        status = self._session().get_status()

        self.assertEqual(status["players"], [])
        self.assertEqual(status["task"], {})
        self.assertEqual(status["hud_note"], "")
        self.assertEqual(status["last_speaker"], {})

    def test_hud_note_falls_back_to_local_echo_only_when_mod_is_silent(self):
        silent_mod = self._session({})
        silent_mod._hud_note_local = "挖矿中"
        self.assertEqual(silent_mod.get_status()["hud_note"], "挖矿中")

        # 模组报了空字符串＝HUD 上确实没有描述词了（比如 TTL 到期），不许再回显旧的。
        reported_empty = self._session({"hud_note": ""})
        reported_empty._hud_note_local = "挖矿中"
        self.assertEqual(reported_empty.get_status()["hud_note"], "")


if __name__ == "__main__":
    unittest.main()
