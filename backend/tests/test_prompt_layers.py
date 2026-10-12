"""分层提示词模型（L0/L1/L2/L3）的验收测试。

对应 docs/dev/PROMPT_MODEL.md 与用户口径："系统提示词只有每轮第一次注入，除非内容变更；
用户要求的提示词（人设/规则/授权）变更时才重新注入；每轮的动态上下文只进当轮 user 消息。"

六条验收：
  ① 连续两轮 state 不同、配置未变 → system 消息**字节相等**（且是同一份缓存字符串）；
  ② 改 persona / 规则 / 借用授权 → 版本键变、system 重组且含新内容；
  ③ 动态值（坐标/饥饿/时间/玩家名/欠账/HUD）**绝不出现**于 system 段；
  ④ 当轮 user 消息只含 L2（动态），不含 L0/L1；
  ⑤ 工具定义走 API `tools` 参数，**不内联**进提示词；
  ⑥ 预算断言（静态前缀新值 + 工具 schema 稳定）。
"""

import contextlib
import json
import unittest
from unittest import mock

from agent.llm_service import L0_BASE_CONTRACT, LLMService
from agent.planner import (
    ACTION_GROUPS,
    L0_PLANNER_CONTRACT,
    PLANNER_MECHANICS,
    PLANNER_RULES,
    Planner,
    build_tool_schemas,
)

# 动态哨兵：出现在任何动态字段里，一旦混进 system 段就会被测出来。
STATE_SENTINEL = "SENTINEL_STATE_BLOCK"
HUD_SENTINEL = "SENTINEL_HUD_NOTE"
DEBT_SENTINEL = "SENTINEL_DEBT"
TASK_SENTINEL = "SENTINEL_TASK_DETAIL"
PLAYER_SENTINEL = "SENTINEL_PLAYER_NAME"
SPEAKER_SENTINEL = "SENTINEL_SPEAKER_NAME"
CHAT_SENTINEL = "SENTINEL_RECENT_CHAT"
BLOCK_SENTINEL = "SENTINEL_NEARBY_BLOCK"
VISION_SENTINEL = "SENTINEL_VISION"
INVENTORY_SENTINEL = "SENTINEL_INVENTORY_ITEM"
MEMORY_SENTINEL = "SENTINEL_MEMORY_SUMMARY"
MESSAGE_SENTINEL = "SENTINEL_MESSAGE"


class DummyMemory:
    def build_context(self):
        return {"interaction_summary": MEMORY_SENTINEL}

    def get_recent_context(self, _count):
        return [{"sender": PLAYER_SENTINEL, "message": CHAT_SENTINEL}]


class DummySkills:
    def __init__(self):
        self.calls: list[tuple] = []

    def move_to(self, x, y, z):
        self.calls.append(("move_to", x, y, z))


def dynamic_context(**overrides) -> dict:
    """一份"每个动态字段都带哨兵值"的上下文（模拟真实 state_update）。"""
    context = {
        "task_state": {"kind": "collect", "status": "collecting", "detail": TASK_SENTINEL},
        "player": {"health": 11, "max_health": 20, "hunger": 13, "saturation": 2.5},
        "inventory": [{"name": INVENTORY_SENTINEL, "count": 2}],
        "nearby_blocks": [{"block_id": BLOCK_SENTINEL, "distance": 3.5}],
        "entities": [{"type": "player", "name": PLAYER_SENTINEL, "distance": 2.0}],
        "players": [{"name": PLAYER_SENTINEL, "distance": 2.0, "speaking": True}],
        "last_speaker": {"name": SPEAKER_SENTINEL, "x": 10.0, "y": 64.0, "z": -3.0},
        "recent_chat": [{"sender": PLAYER_SENTINEL, "message": CHAT_SENTINEL,
                         "time": 1700000000.0, "sender_distance": 2.0}],
        "hud_note": HUD_SENTINEL,
        "borrow_pending": DEBT_SENTINEL,
        "is_night": True,
        "day_time": 15000,
        "can_sleep": True,
        "vision": VISION_SENTINEL,
        "known_poi": "minecraft:chest@(1,2,3)",
        "persona_blocks": {
            "identity": "陪朋友玩 MC 的搭档",
            "reply_style": "短句、口语化",
            "scene_rules": "- 私聊：随便聊",
            "state_block": STATE_SENTINEL,
        },
    }
    context.update(overrides)
    return context


def planner() -> Planner:
    return Planner(llm_service=None, memory=DummyMemory(), skills=DummySkills())


class FakeLLM:
    """按角色回固定内容（默认空候选 JSON，不触发 decider）；记录每次请求。"""

    api_key = "test-key"

    def __init__(self, content: str = '{"reply":"好","candidates":[]}', extra: dict | None = None):
        self.content = content
        self.responses = dict(extra or {})
        self.calls: list[dict] = []

    def chat(self, messages, agent=None, **kwargs):
        self.calls.append({"messages": messages, "agent": agent, "kwargs": kwargs})
        if agent in self.responses:
            return self.responses[agent]
        return {"content": self.content}


# ── ① 同配置跨轮：system 字节相等 ────────────────────────────────


class SystemPrefixStabilityTests(unittest.TestCase):
    def test_two_turns_with_different_state_share_byte_identical_system(self):
        planner_ = planner()

        first = planner_.build_prompt_layers("Alice", "第一轮", dynamic_context(), "AI")
        second = planner_.build_prompt_layers(
            "Alice", "第二轮", dynamic_context(day_time=18000, hud_note="变了"), "AI")

        self.assertEqual(first.system, second.system, "配置未变 → system 必须逐字节相同")
        self.assertIs(first.system, second.system, "同版本键要复用同一份缓存字符串")
        self.assertEqual(first.l1_version, second.l1_version)
        self.assertNotEqual(first.user, second.user, "动态部分只应体现在 user 上")

    def test_l0_is_a_byte_stable_prefix_of_every_system(self):
        planner_ = planner()

        persona_a = planner_.build_prompt_layers("Alice", "hi", {"persona_blocks": {"identity": "甲"}}, "AI")
        persona_b = planner_.build_prompt_layers("Alice", "hi", {"persona_blocks": {"identity": "乙"}}, "AI")

        for layers in (persona_a, persona_b):
            self.assertTrue(layers.system.startswith(L0_PLANNER_CONTRACT))
        # L0 本身不随人设变化。
        self.assertEqual(persona_a.system[:len(L0_PLANNER_CONTRACT)],
                         persona_b.system[:len(L0_PLANNER_CONTRACT)])

    def test_end_to_end_messages_reuse_the_same_system(self):
        llm = FakeLLM()
        planner_ = Planner(llm_service=llm, memory=DummyMemory(), skills=DummySkills())

        planner_.plan_and_execute("Alice", "第一轮", dynamic_context(), "AI")
        planner_.plan_and_execute("Alice", "第二轮", dynamic_context(day_time=18000), "AI")

        first, second = llm.calls[0]["messages"], llm.calls[1]["messages"]
        self.assertEqual(first[0]["role"], "system")
        self.assertEqual(first[1]["role"], "user")
        self.assertEqual(first[0]["content"], second[0]["content"])


# ── ② 内容变更 → 版本键变 + system 重组 ──────────────────────────


class SystemVersionKeyTests(unittest.TestCase):
    def test_persona_change_bumps_the_version_key_and_rebuilds(self):
        planner_ = planner()

        before = planner_.build_prompt_layers("Alice", "hi", {"persona_blocks": {"identity": "旧身份"}}, "AI")
        after = planner_.build_prompt_layers("Alice", "hi", {"persona_blocks": {"identity": "新身份"}}, "AI")

        self.assertNotEqual(before.l1_version, after.l1_version, "人设变了版本键必须变")
        self.assertIn("新身份", after.system)
        self.assertNotIn("新身份", before.system)

    def test_borrow_authorization_change_bumps_the_version_key(self):
        planner_ = planner()

        none = planner_.build_prompt_layers("Alice", "hi", {}, "AI")
        granted = planner_.build_prompt_layers(
            "Alice", "hi", {"borrow_rules": "- 铁类（Steve 授权）：用完必须放回原箱"}, "AI")

        self.assertNotEqual(none.l1_version, granted.l1_version)
        self.assertIn("铁类", granted.system)
        self.assertNotIn("铁类", none.system)

    def test_rules_change_bumps_the_version_key(self):
        planner_ = planner()
        before = planner_.build_prompt_layers("Alice", "hi", {}, "AI")

        with mock.patch("agent.planner.PLANNER_RULES", PLANNER_RULES + ("新规则 SENTINEL_RULE",)):
            after = planner().build_prompt_layers("Alice", "hi", {}, "AI")

        self.assertNotEqual(before.l1_version, after.l1_version, "规则变了版本键必须变")
        self.assertIn("SENTINEL_RULE", after.system)
        self.assertNotIn("SENTINEL_RULE", before.system)

    def test_version_key_is_the_content_hash(self):
        from agent.llm_service import prompt_version

        a = planner().build_prompt_layers("Alice", "hi", {}, "AI")
        b = planner().build_prompt_layers("Alice", "hi", {"hud_note": "无关动态"}, "AI")

        self.assertEqual(a.l1_version, b.l1_version)
        self.assertEqual(a.l1_version, prompt_version(L0_PLANNER_CONTRACT,
                                                      planner()._build_l1({}, "AI")))


# ── ③ 动态值绝不进 system ────────────────────────────────────────


class NoDynamicValuesInSystemTests(unittest.TestCase):
    def test_planner_system_carries_no_dynamic_values(self):
        planner_ = planner()

        layers = planner_.build_prompt_layers("Alice", MESSAGE_SENTINEL, dynamic_context(), "AI")

        for sentinel in (STATE_SENTINEL, HUD_SENTINEL, DEBT_SENTINEL, TASK_SENTINEL,
                         PLAYER_SENTINEL, SPEAKER_SENTINEL, CHAT_SENTINEL, BLOCK_SENTINEL,
                         VISION_SENTINEL, INVENTORY_SENTINEL, MEMORY_SENTINEL, MESSAGE_SENTINEL):
            self.assertNotIn(sentinel, layers.system, f"{sentinel} 混进了 system（应为 L2）")
        self.assertNotIn("15000", layers.system)
        self.assertNotIn("minecraft:chest@(1,2,3)", layers.system)
        # 反过来：这些动态值确实在当轮 user 里（没有丢能力）。
        # 交互窗口在 L2 里**只渲染一处**（`最近对话`，源=`get_recent_context`）—— P2 合并后
        # 曾经重复的 `记忆上下文`（`build_context()['interaction_summary']`）不再单独出现，
        # 所以这里断言的是那一处真正落地（而不是断言重复的那份）。
        for sentinel in (STATE_SENTINEL, HUD_SENTINEL, DEBT_SENTINEL, TASK_SENTINEL,
                         MESSAGE_SENTINEL, VISION_SENTINEL,
                         PLAYER_SENTINEL, CHAT_SENTINEL):
            self.assertIn(sentinel, layers.user)

    def test_session_system_prompt_carries_no_dynamic_values(self):
        service = LLMService()

        prompt = service.build_system_prompt({
            "persona": {"name": "Maid", "personality": "calm"},
            "persona_blocks": {"state_block": STATE_SENTINEL},
            "action_insights": "SENTINEL_ACTION_HISTORY",
            "interaction_summary": MEMORY_SENTINEL,
        }, "Available commands:\n- help")

        for sentinel in (STATE_SENTINEL, MEMORY_SENTINEL, "SENTINEL_ACTION_HISTORY"):
            self.assertNotIn(sentinel, prompt)
        self.assertIn(L0_BASE_CONTRACT, prompt)
        self.assertIn("Available commands:", prompt)

    def test_session_dynamic_context_only_carries_the_dynamic_bits(self):
        service = LLMService()

        text = service.build_dynamic_context({
            "persona_blocks": {"state_block": STATE_SENTINEL},
            "interaction_summary": MEMORY_SENTINEL,
        }, "自检提示")

        self.assertIn("自检提示", text)
        self.assertIn(STATE_SENTINEL, text)
        self.assertIn(MEMORY_SENTINEL, text)


# ── ④ user 只含 L2 ──────────────────────────────────────────────


class UserMessageScopeTests(unittest.TestCase):
    def test_user_message_has_only_the_dynamic_layer(self):
        layers = planner().build_prompt_layers("Alice", "在干嘛", dynamic_context(), "AI")

        self.assertIn("当前状态：", layers.user)
        self.assertIn("画面观察", layers.user)
        # L0 / L1 的标记一个都不该在 user 里。
        self.assertNotIn("工具调用契约", layers.user)
        self.assertNotIn("规则：", layers.user)
        self.assertNotIn("机制说明", layers.user)
        self.assertNotIn("借用授权", layers.user)
        self.assertNotIn(L0_PLANNER_CONTRACT, layers.user)

    def test_user_message_is_stable_when_nothing_dynamic_changed(self):
        planner_ = planner()

        first = planner_.build_prompt_layers("Alice", "hi", {}, "AI")
        second = planner_.build_prompt_layers("Alice", "hi", {}, "AI")

        self.assertEqual(first.user, second.user)


# ── ⑤ 工具走 tools 参数，不内联 ─────────────────────────────────


class ToolDefinitionTests(unittest.TestCase):
    def test_tools_are_passed_as_a_separate_argument(self):
        llm = FakeLLM()
        planner_ = Planner(llm_service=llm, memory=DummyMemory(), skills=DummySkills())

        planner_.plan_and_execute("Alice", "hi", dynamic_context(), "AI")

        call = llm.calls[0]
        tools = call["kwargs"].get("tools")
        self.assertTrue(tools, "工具必须走 tools 参数")
        names = {item["function"]["name"] for item in tools}
        self.assertIn("craft_item", names)
        self.assertIn("borrow_item", names)
        # 提示词里不再内联工具表。
        for message in call["messages"]:
            self.assertNotIn("可用动作（", message["content"])

    def test_tool_schema_covers_every_declared_action(self):
        declared = {name for actions in ACTION_GROUPS.values() for name, _, _ in actions}
        built = {item["function"]["name"] for item in build_tool_schemas()}

        self.assertEqual(declared, built)
        for item in build_tool_schemas():
            self.assertEqual(item["type"], "function")
            self.assertIn("parameters", item["function"])
            self.assertEqual(item["function"]["parameters"]["type"], "object")

    def test_tool_schema_is_stable_and_cached(self):
        planner_ = planner()

        first = planner_.build_prompt_layers("Alice", "hi", {}, "AI")
        second = planner_.build_prompt_layers("Alice", "hi", {"day_time": 9}, "AI")

        self.assertIs(first.tools, second.tools, "同一阶段的工具 schema 应复用同一份")
        self.assertEqual(json.dumps(first.tools, ensure_ascii=False, sort_keys=True),
                         json.dumps(build_tool_schemas(), ensure_ascii=False, sort_keys=True))

    def test_tools_are_phase_filtered_exactly_like_the_old_inline_table(self):
        """M3-3：只注入当前阶段相关的动作——现在过滤发生在 `tools` 参数上。"""
        idle = planner().build_prompt_layers(
            "Alice", "hi", {"task_state": {"kind": "idle", "status": "idle"}}, "AI")
        craft = planner().build_prompt_layers(
            "Alice", "做把剑", {"task_state": {"kind": "craft", "status": "crafting"}}, "AI")

        idle_names = {item["function"]["name"] for item in idle.tools}
        craft_names = {item["function"]["name"] for item in craft.tools}

        self.assertIn("attack", idle_names)
        self.assertNotIn("attack", craft_names, "craft 阶段不该广告 combat 工具")
        self.assertIn("craft_item", craft_names)
        self.assertLess(len(craft.tools), len(idle.tools))

    def test_llm_payload_carries_the_tools_field(self):
        service = LLMService()
        tools = build_tool_schemas()

        payload = service._build_payload([{"role": "user", "content": "hi"}], tools=tools)

        self.assertEqual(payload["tools"], tools)
        # 不给工具时不加字段（对不支持 tools 的供应商保持原样）。
        self.assertNotIn("tools", service._build_payload([{"role": "user", "content": "hi"}]))


# ── ⑤b 原生 tool_calls（工具从 tools 参数给出后模型的合法回法）───────


class NativeToolCallTests(unittest.TestCase):
    def test_native_tool_calls_are_converted_to_candidates(self):
        plan = Planner._plan_from_tool_calls([
            {"function": {"name": "move_to", "arguments": '{"x":1,"y":2,"z":3}'}},
            {"function": {"name": ""}},
            "noise",
        ])

        self.assertEqual(plan["candidates"], [
            {"tool": "move_to", "args": {"x": 1, "y": 2, "z": 3}, "why": "模型直接调用"}])

    def test_unparsable_arguments_never_get_invented(self):
        plan = Planner._plan_from_tool_calls([
            {"function": {"name": "move_to", "arguments": "{坏 JSON"}}])

        self.assertEqual(plan["candidates"][0]["args"], {}, "解析不了就给空 args，绝不猜参数")

    def test_native_tool_call_is_dispatched_end_to_end(self):
        llm = FakeLLM(content="", extra={
            "planner": {"content": "", "tool_calls": [
                {"function": {"name": "move_to", "arguments": '{"x":10,"y":64,"z":-20}'}}]},
            "decider": {"content": '{"choice": 0, "confidence": 0.95}'},
        })
        skills = DummySkills()
        planner_ = Planner(llm_service=llm, memory=DummyMemory(), skills=skills)

        planner_.plan_and_execute("Alice", "过来", {}, "AI")

        self.assertEqual(skills.calls, [("move_to", 10.0, 64.0, -20.0)])
        self.assertEqual(llm.calls[0]["agent"], "planner")
        self.assertTrue(llm.calls[0]["kwargs"]["tools"])

    def test_no_content_and_no_tool_calls_still_returns_none(self):
        llm = FakeLLM(content="")
        planner_ = Planner(llm_service=llm, memory=DummyMemory(), skills=DummySkills())

        self.assertIsNone(planner_.plan_and_execute("Alice", "hi", {}, "AI"))


# ── ⑥ 预算 ──────────────────────────────────────────────────────


class StaticSizeTests(unittest.TestCase):
    """静态前缀（L0+L1）只降不抬（实测 1690 / 预算 1700）；旧口径见 test_planner 的同名断言。"""

    IDLE_STATIC_BUDGET = 1700

    def test_static_prefix_is_within_budget(self):
        layers = planner().build_prompt_layers(
            "Steve", "hi", {"task_state": {"kind": "idle", "status": "idle"}}, "AI")

        self.assertLess(len(layers.system), self.IDLE_STATIC_BUDGET)

    def test_config_independent_core_is_smaller_than_the_whole_prefix(self):
        """与配置无关的静态核心（L0 + 规则 + 机制）必须小于整个静态前缀。"""
        core = len(L0_PLANNER_CONTRACT) + len(PLANNER_MECHANICS) + len("\n".join(PLANNER_RULES))
        layers = planner().build_prompt_layers("Steve", "hi", {}, "AI")

        self.assertLess(core, len(layers.system))
        self.assertLess(len(L0_PLANNER_CONTRACT) + len(PLANNER_MECHANICS), len(layers.system))


# ── ⑦ 会话侧自主轮（self-prompt）也守同一套分层 ──────────────────


class _FakePrompter:
    def on_prompt_sent(self):
        pass

    def mark_action(self):
        pass


class _FakeCommands:
    def get_docs(self):
        return ""

    def parse_and_execute(self, _text, _runtime):
        return []


class _FakeSkills:
    @contextlib.contextmanager
    def command_context(self, _mode):
        yield


class SelfPromptLayerTests(unittest.TestCase):
    """自主轮（`Session._handle_self_prompt`）不得把 L2 动态塞进 system。

    背景：会话侧原先 `{"role": "user", "content": prompt}` 直接发纯文本，而动态块已从
    `build_system_prompt` 移出（属 L2）→ 不接 `build_dynamic_context` 就会**丢掉**背包/状态/
    动作历史。这条测试同时钉住"接上了"和"没接进 system"。
    """

    def _session(self):
        from agent.session import Session

        session = Session.__new__(Session)  # 不跑 __init__：只装这条路径需要的东西
        session.llm = LLMService()
        session.llm_active = lambda: True  # 在场门：不在世界就不会走到这里
        session.self_prompter = _FakePrompter()
        session.commands = _FakeCommands()
        session.skills = _FakeSkills()
        session.runtime = object()
        session.memory = DummyMemory()
        session._runtime_context = dynamic_context
        captured: dict = {}

        def fake_chat(messages, agent=None, **_kwargs):
            captured["messages"] = messages
            captured["agent"] = agent
            return {"content": ""}

        session.llm.chat = fake_chat
        return session, captured

    def test_self_prompt_puts_dynamic_layer_in_user_message_only(self):
        session, captured = self._session()

        session._handle_self_prompt("SELF_PROMPT_TEXT")

        messages = captured["messages"]
        system, user = messages[0], messages[1]
        self.assertEqual("system", system["role"])
        self.assertEqual("user", user["role"])
        # user = 当轮动态（L2）：self-prompt 文本 + 动态值都在这里
        self.assertIn("SELF_PROMPT_TEXT", user["content"])
        self.assertIn(STATE_SENTINEL, user["content"])
        # system = L0 + L1：与 build_system_prompt 逐字节一致，且不带任何动态哨兵
        self.assertEqual(session.llm.build_system_prompt(dynamic_context(), ""), system["content"])
        for sentinel in (STATE_SENTINEL, HUD_SENTINEL, DEBT_SENTINEL, TASK_SENTINEL,
                         PLAYER_SENTINEL, SPEAKER_SENTINEL, CHAT_SENTINEL, BLOCK_SENTINEL,
                         VISION_SENTINEL, INVENTORY_SENTINEL, MEMORY_SENTINEL):
            if sentinel in user["content"]:
                self.assertNotIn(sentinel, system["content"],
                                 f"{sentinel} 混进了自主轮的 system 段（应为 L2）")

    def test_self_prompt_skips_llm_entirely_when_not_in_world(self):
        """在场门的第二道保险：不在世界连 self-prompt 都不许花 token。"""
        session, captured = self._session()
        session.llm_active = lambda: False

        session._handle_self_prompt("SELF_PROMPT_TEXT")

        self.assertEqual({}, captured, "不在世界却调了模型")


if __name__ == "__main__":
    unittest.main()
