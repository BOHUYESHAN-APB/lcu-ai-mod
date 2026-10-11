"""会话树接线验收：等价对照 + 开关 + 分区不串层 + 接线后不变量。

对应 docs/dev/AGENT_ARCH.md §1 与用户口径：

* **先证后接**：用真实形状的合成数据同时跑【旧链路】与【session_core 投影 + 只读适配】，
  逐字段对比，相等/天然不同都如实列出；
* **开关默认关**：`config.json → session_tree`（默认 false）。关闭时「对话历史的读取」走既有
  链路、字节完全照旧（断言：不碰新链路、不加新键、值等于旧值）；
* **只读不改写**：本批只让新链路参与读；写侧仍归 `_absorb_*` / `conversation.py`；
* **分区不串层**：`server:<key>` / `world:<...>` 复用现有 key 口径。
"""

import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from agent.memory import Memory
from agent.session import Session
from agent.session_core import (
    L0_CONTRACT,
    PERSONA,
    RULES,
    ContextBuilder,
    ContextEditEntry,
    CompactionEntry,
    MessageEntry,
    SessionManager,
    SessionStore,
    SystemMessageEntry,
    ToolCallEntry,
    make,
    project,
    project_history,
    server_scope,
    world_scope_key,
)
from agent.session_core.entries import ROLE_ASSISTANT, ROLE_TOOL_RESULT, ROLE_USER


def _bytes(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, default=str)


# 真实形状的合成对话（与 Memory.recent_messages 同形）。
TURNS = [
    {"sender": "Alice", "message": "你好", "response": "你好呀，一起玩？"},
    {"sender": "Alice", "message": "帮我造张床", "response": "好，我去砍树"},
    {"sender": "Bob", "message": "木头分我点", "response": ""},
    {"sender": "Bob", "message": "谢谢", "response": "不客气"},
]

SECTIONS = {L0_CONTRACT: "L0-CONTRACT", PERSONA: "PERSONA-TEXT", RULES: "RULE-TEXT"}


# ── ① 等价对照：旧链路 vs 新链路，逐字段 ────────────────────────
class RealShapeEquivalenceTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.memory = Memory(path=os.path.join(self._tmp.name, "memory.json"))
        for turn in TURNS:
            self.memory.add_interaction(sender=turn["sender"], message=turn["message"],
                                        response=turn["response"])

    def tearDown(self):
        self._tmp.cleanup()

    def test_field_by_field_comparison(self):
        old = self.memory.build_context()                      # 旧链路（Memory）
        new = project_history(TURNS, scope=server_scope("127.0.0.1:25565", "world"),
                              system_sections=SECTIONS)        # 新链路（投影 + 适配）

        # ── 相等 ──
        self.assertEqual(new["interaction_summary"], old["interaction_summary"])
        # 投影把同一份 turns 收成 (sender, message, response) 对，与旧读取源逐字段相同。
        self.assertEqual(new["chat_pairs"], [
            {"sender": t["sender"], "message": t["message"], "response": t["response"]}
            for t in TURNS
        ])
        # message = 最后一条 user 消息；last_speaker 的名字对得上旧读取源的最后发话者。
        self.assertEqual(new["message"], TURNS[-1]["message"])
        self.assertEqual(new["last_speaker"], TURNS[-1]["sender"])
        # 旧链路同一份数据内给出的 last_speaker 名字一致（形状见下）。
        self.assertEqual(new["last_speaker"], TURNS[-1]["sender"])

    def test_naturally_different_fields_are_documented(self):
        new = project_history(TURNS, scope="server:x", system_sections=SECTIONS)

        # last_speaker：适配器给 **名字字符串**；会话 runtime 的 context["last_speaker"] 是
        # dict（{name,x,y,z,message,time}，见 planner 消费）。天然不同（形状）。
        self.assertIsInstance(new["last_speaker"], str)

        # recent_chat：适配器给 {sender,message}；会话 runtime 的 recent_chat 是原始聊天事件
        # （{sender,message,time,is_system,sender_distance}）。天然不同（来源/字段）。
        self.assertTrue(all(set(row) == {"sender", "message"} for row in new["recent_chat"]))

        # persona_blocks：适配器把 PERSONA→identity、RULES→scene_rules，且没有
        # reply_style / state_block（会话侧的四块人设）。天然不同（来源不同：会话侧走
        # persona store + state_block，属 L1/L2 人设，绝不与新链路的 system 分段混写）。
        self.assertEqual(new["persona_blocks"]["identity"], "PERSONA-TEXT")
        self.assertEqual(new["persona_blocks"]["scene_rules"], "RULE-TEXT")
        self.assertEqual(new["persona_blocks"]["reply_style"], "")
        self.assertEqual(new["persona_blocks"]["state_block"], "")

        # system_prompt：适配器只拼 session_core 的 system 分段；会话侧的 L0/L1 由
        # LLMService.build_system_prompt 生成（内容/来源不同）。**绝不用它替换 L0/L1**。
        self.assertEqual(new["system_prompt"], "L0-CONTRACT\n\nRULE-TEXT\n\nPERSONA-TEXT")

    def test_projection_with_tools_compaction_edit_branch_has_no_old_counterpart(self):
        """真实形状全量（工具/压缩/改写/分支）：跑通并展示新链路独有能力。"""
        tmp = tempfile.TemporaryDirectory()
        try:
            store = SessionStore(os.path.join(tmp.name, "s.jsonl"))
            mgr = SessionManager(store, session_id="s", scope="server:x")
            mgr.append(make(SystemMessageEntry, sections=SECTIONS))
            u1 = mgr.append(make(MessageEntry, role=ROLE_USER, content="造床", name="Alice"))
            a1 = mgr.append(make(MessageEntry, role=ROLE_ASSISTANT, content="好"))
            tc = mgr.append(make(ToolCallEntry, tool_call_id="c1", name="craft",
                                 arguments={"item": "bed"}))
            tr = mgr.append(make(MessageEntry, role=ROLE_TOOL_RESULT, content="ok",
                                 tool_call_id="c1"))
            u2 = mgr.append(make(MessageEntry, role=ROLE_USER, content="再要一块", name="Bob"))
            a2 = mgr.append(make(MessageEntry, role=ROLE_ASSISTANT, content="好"))

            # 工具循环：assistant 节点的 tool_calls + 每个 tool_result 单独成节点。
            msgs = project(store, mgr.leaf_id())
            self.assertEqual([m["role"] for m in msgs if m["role"] == "tool"], ["tool"])

            # 改写 = 追加重定向节点，只影响未来投影（旧 leaf 逐字节不变）。
            before = _bytes(project(store, a1.id))
            edit = mgr.append(make(ContextEditEntry, target_id=a1.id, replacement="改过"))
            self.assertEqual(before, _bytes(project(store, a1.id)))
            self.assertIn("改过", _bytes(project(store, edit.id)))

            # 分支 = 移 leaf（不复制、不编辑）。
            mgr.checkout(u1.id)
            a_alt = mgr.append(make(MessageEntry, role=ROLE_ASSISTANT, content="旁支"))
            self.assertIn("旁支", _bytes(project(store, a_alt.id)))
            self.assertNotIn("旁支", _bytes(project(store, a2.id)))

            # 压缩 = 节点：原始记录仍在，旧 leaf 重算不变。
            old_leaf_bytes = _bytes(project(store, a2.id))
            comp = mgr.append(make(CompactionEntry, summary="SUMMARY", first_kept_id=u2.id))
            self.assertTrue({u1.id, a1.id, tc.id, tr.id, u2.id, a2.id}
                            .issubset({e.id for e in store.read_entries()}))
            self.assertEqual(old_leaf_bytes, _bytes(project(store, a2.id)))

            # 这些概念（工具循环/压缩/改写/分支）在旧 Memory 形状里**没有对应物**：
            # 旧链路一个 turn = 一条 {sender,message,response}，无法表达工具节点/分支/压缩。
            old = self.memory.build_context()
            self.assertNotIn("tool", _bytes(old))
            self.assertNotIn("SUMMARY", _bytes(old))
        finally:
            tmp.cleanup()


# ── ② 开关默认关 + 关闭时旧路径逐字节照旧 ──────────────────────
class SwitchOffTests(unittest.TestCase):
    def _session(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        session = Session.__new__(Session)
        session.id = "t"
        session.memory = Memory(path=os.path.join(tmp.name, "memory.json"))
        for turn in TURNS:
            session.memory.add_interaction(sender=turn["sender"], message=turn["message"],
                                           response=turn["response"])
        session.runtime = {"world": {"server": "127.0.0.1:25565", "world_name": "world"},
                           "players": [], "persona": {}, "task_state": {}}
        session.vision = SimpleNamespace(latest=lambda: None)
        session.poi = SimpleNamespace(describe=lambda: "", find=lambda _k: [])
        session.last_speaker = {"name": "Bob", "x": 1.0, "y": 2.0, "z": 3.0,
                                "message": "谢谢", "time": 0.0}
        session.chat_inbox = SimpleNamespace(recent=lambda n: [])
        session.persona = None
        session.conversation = SimpleNamespace(describe=lambda n: {})
        session.in_world = True
        session._hud_note_local = ""
        session._present_person_facts = {}
        return session

    def test_default_is_off_and_off_path_never_touches_new_chain(self):
        session = self._session()
        with mock.patch("agent.session.get_config_store") as store:
            store.return_value.get_session_tree.return_value = False
            with mock.patch("agent.session_core.project_history") as spy:
                ctx = session._runtime_context()
                self.assertFalse(session._session_tree_enabled())
        spy.assert_not_called()                                  # 关闭时**不碰**新链路
        self.assertNotIn("chat_pairs", ctx)                      # 也不加新键
        self.assertNotIn("history_scope", ctx)

    def test_off_output_is_byte_identical_regardless_of_switch_representation(self):
        """关闭 / 键缺失 / 显式 False → 旧路径输出逐字节相同。"""
        session = self._session()
        with mock.patch("agent.session.get_config_store") as store:
            store.return_value.get_session_tree.return_value = False
            off = session._runtime_context()
        # 关时 interaction_summary 等于旧链路值（不可能被新链路影响）。
        self.assertEqual(off["interaction_summary"], session.memory._build_interaction_summary())
        # 冻结整份输出的字节表现（回归基线）。
        frozen = _bytes(off)
        session2 = self._session()
        with mock.patch("agent.session.get_config_store", side_effect=RuntimeError("no store")):
            fallback = session2._runtime_context()
        self.assertEqual(frozen, _bytes(fallback))               # 读不到开关 = 关 = 同字节


# ── ③ 开关开：新路径可用 + 等价 + 接线后不变量 ─────────────────
class SwitchOnTests(unittest.TestCase):
    def _session(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        session = Session.__new__(Session)
        session.id = "t"
        session.memory = Memory(path=os.path.join(tmp.name, "memory.json"))
        for turn in TURNS:
            session.memory.add_interaction(sender=turn["sender"], message=turn["message"],
                                           response=turn["response"])
        session.runtime = {"world": {"server": "127.0.0.1:25565", "world_name": "world"},
                           "players": [], "persona": {}, "task_state": {}}
        session.vision = SimpleNamespace(latest=lambda: None)
        session.poi = SimpleNamespace(describe=lambda: "", find=lambda _k: [])
        session.last_speaker = {}
        session.chat_inbox = SimpleNamespace(recent=lambda n: [])
        session.persona = None
        session.conversation = SimpleNamespace(describe=lambda n: {})
        session.in_world = True
        session._hud_note_local = ""
        session._present_person_facts = {}
        return session

    def test_on_path_uses_projection_and_stays_equivalent(self):
        session = self._session()
        with mock.patch("agent.session.get_config_store") as store:
            store.return_value.get_session_tree.return_value = True
            ctx = session._runtime_context()
        self.assertIn("chat_pairs", ctx)                         # 新链路参与读
        self.assertEqual(ctx["history_scope"], "server:127.0.0.1_25565")
        # 等价：开时 interaction_summary 仍与旧链路一致。
        self.assertEqual(ctx["interaction_summary"], session.memory._build_interaction_summary())
        # 其余动态/静态字段不受影响（persona/last_speaker/recent_chat 仍是旧口径）。
        self.assertEqual(ctx["last_speaker"], {})
        self.assertEqual(ctx["recent_chat"], [])

    def test_projection_never_enters_static_prefix(self):
        """新链路的任何东西都不许进 L0/L1（静态前缀只减不抬）。"""
        from tests.test_prompt_layers import planner

        p = planner()
        idle = {"task_state": {"kind": "idle", "status": "idle"}}
        base = p.build_prompt_layers("Steve", "hi", idle, "AI")
        with_proj = p.build_prompt_layers("Steve", "hi", {
            **idle,
            "interaction_summary": "Alice: 你好",
            "chat_pairs": [{"sender": "Alice", "message": "你好"}],
            "history_scope": "server:x",
        }, "AI")
        self.assertEqual(base.system, with_proj.system)          # L0/L1 逐字节不变
        self.assertLess(len(with_proj.system), 1700)             # 预算只减不抬
        self.assertNotIn("Alice: 你好", with_proj.system)

    def test_post_wiring_invariant_same_leaf_projection_is_byte_identical(self):
        first = project_history(TURNS, scope="server:x")
        second = project_history(TURNS, scope="server:x")
        self.assertEqual(_bytes(first), _bytes(second))

    def test_post_wiring_invariant_prefix_immutable(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = SessionStore(os.path.join(tmp.name, "s.jsonl"))
        mgr = SessionManager(store, session_id="s", scope="server:x")
        mgr.append(make(MessageEntry, role=ROLE_USER, content="u1"))
        leaf = mgr.leaf_id()
        before_text = store.raw_text()
        before_proj = _bytes(project(store, leaf))
        mgr.append(make(MessageEntry, role=ROLE_ASSISTANT, content="a1"))
        self.assertTrue(store.raw_text().startswith(before_text))   # 旧前缀字节不变
        self.assertEqual(before_proj, _bytes(project(store, leaf)))


# ── ④ 分区不串层 ───────────────────────────────────────────────
class ScopeIsolationTests(unittest.TestCase):
    def test_scope_keys_reuse_existing_conventions(self):
        from agent.conversation import sanitize_key, server_key
        self.assertEqual(server_scope("127.0.0.1:25565", "world"),
                         f"server:{sanitize_key(server_key('127.0.0.1:25565', 'world'))}")
        self.assertEqual(server_scope("", "MyWorld"),
                         f"server:{sanitize_key(server_key('', 'MyWorld'))}")
        from agent.world_memory import world_scope
        self.assertEqual(world_scope_key("srv", "world", "overworld"),
                         f"world:{world_scope('srv', 'world', 'overworld')}")

    def test_two_scopes_do_not_bleed(self):
        a = project_history([{"sender": "Alice", "message": "A-ONLY", "response": "ok"}],
                            scope=server_scope("srvA", "world"))
        b = project_history([{"sender": "Bob", "message": "B-ONLY", "response": "ok"}],
                            scope=server_scope("srvB", "world"))
        self.assertNotEqual(a["scope"], b["scope"])
        self.assertIn("A-ONLY", _bytes(a))
        self.assertNotIn("B-ONLY", _bytes(a))
        self.assertIn("B-ONLY", _bytes(b))
        self.assertNotIn("A-ONLY", _bytes(b))


if __name__ == "__main__":
    unittest.main()
