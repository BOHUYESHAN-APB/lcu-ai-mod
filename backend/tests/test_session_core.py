"""session_core 验收测试：只追加日志 + 树/分支 + 纯投影 + 分段提示词 + 只读适配。

对应 ``docs/dev/AGENT_ARCH.md`` / ``docs/dev/PROMPT_MODEL.md``。七条不变量 + 适配等价性：

① 同一 leaf 两次投影**字节相同**（缓存命中与未命中两条路径）
② 追加新记录**不改动**旧前缀字节（前缀缓存前提）
③ ``ContextEditEntry`` **不影响已发生的投影**（旧 leaf 重算结果不变）
④ 压缩是**节点**：压缩后旧记录仍在日志里、可回到旧 leaf 得到原样投影
⑤ 分支 = 移 leaf：两个 leaf 各自投影出各自的分支（互不污染）
⑥ **1 assistant = 1 节点** + 每个 tool_result 各自成节点
⑦ 分段提示词只在变更时追加 patch，未变时**逐字节相同**
"""

import json
import os
import tempfile
import unittest

from agent.memory import Memory
from agent.session_core import (
    AUTHZ,
    COMPACTION,
    CONTEXT_EDIT,
    L0_CONTRACT,
    MECHANICS,
    MESSAGE,
    PERSONA,
    ROLE_ASSISTANT,
    ROLE_TOOL_RESULT,
    ROLE_USER,
    RULES,
    SECTION_ORDER,
    TOOL_CALL,
    BranchSummaryEntry,
    CompactionEntry,
    ContextBuilder,
    ContextEditEntry,
    Entry,
    MessageEntry,
    ModelChangeEntry,
    SessionManager,
    SessionStore,
    SessionStoreError,
    SystemMessageEntry,
    ToolCallEntry,
    UsageEntry,
    apply_patch,
    build_sections,
    diff_sections,
    make,
    project,
    render,
    replay,
)


def _bytes(messages) -> str:
    """把投影结果折成可逐字节比较的字符串。"""
    return json.dumps(messages, ensure_ascii=False, sort_keys=True)


class _Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = SessionStore(os.path.join(self._tmp.name, "session.jsonl"))
        self.mgr = SessionManager(self.store, session_id="sess-1", scope="server:x")

    def tearDown(self):
        self._tmp.cleanup()

    # 便捷构造
    def _user(self, content, name=None):
        return self.mgr.append(make(MessageEntry, role=ROLE_USER, content=content, name=name))

    def _assistant(self, content, parent_id=None):
        return self.mgr.append(make(MessageEntry, role=ROLE_ASSISTANT, content=content),
                                  parent_id=parent_id)


# ── 只追加存储 ────────────────────────────────────────────────
class AppendOnlyStoreTests(_Base):
    def test_header_is_first_line_and_not_part_of_tree(self):
        header = self.store.read_header()
        self.assertIsNotNone(header)
        self.assertEqual(header["type"], "session")
        self.assertEqual(header["id"], "sess-1")
        self.assertEqual(header["scope"], "server:x")
        self.assertIn("version", header)
        self.assertIn("created_at", header)
        # header 不参与树：还没有任何条目
        self.assertEqual(self.store.read_entries(), [])

    def test_header_is_never_rewritten(self):
        with self.assertRaises(SessionStoreError):
            self.store.write_header("other")

    def test_store_exposes_no_update_or_delete_api(self):
        for name in ("update", "delete", "edit", "remove", "rewrite", "pop", "set"):
            self.assertFalse(hasattr(self.store, name), f"存储层不应暴露 {name}")

    def test_each_record_has_id_parent_timestamp_type_payload(self):
        self._user("hi")
        records = self.store.read_all()
        entry_record = records[1]
        self.assertEqual(
            set(entry_record.keys()),
            {"id", "parent_id", "timestamp", "type", "payload"},
        )
        self.assertEqual(entry_record["type"], MESSAGE)
        self.assertIsNone(entry_record["parent_id"])  # 第一个节点挂在 root

    def test_entry_records_round_trip_all_types(self):
        cases = [
            make(MessageEntry, role=ROLE_USER, content="u", name="A"),
            make(ToolCallEntry, tool_call_id="c1", name="craft", arguments={"i": 1}),
            make(SystemMessageEntry, sections={"L0_CONTRACT": "x"}, tools_added=["a"],
                 tools_removed=["b"]),
            make(ContextEditEntry, target_id="t1", replacement="r"),
            make(ContextEditEntry, target_id="t1", omit=True),
            make(CompactionEntry, summary="s", first_kept_id="f", tokens_before=9),
            make(BranchSummaryEntry, summary="bs", from_id="a", to_id="b"),
            make(ModelChangeEntry, provider="p", api="a", model="m"),
            make(UsageEntry, input_tokens=1, output_tokens=2, model="m"),
        ]
        for entry in cases:
            record = entry.to_record()
            self.assertEqual(
                set(record.keys()),
                {"id", "parent_id", "timestamp", "type", "payload"},
            )
            back = Entry.from_record(record)
            self.assertIs(type(back), type(entry))
            self.assertEqual(back.to_record(), record)

    def test_unknown_type_is_preserved(self):
        record = {"id": "z", "parent_id": None, "timestamp": 1.0,
                  "type": "future_thing", "payload": {"k": [1, 2]}}
        back = Entry.from_record(record)
        self.assertEqual(back.to_record()["payload"], {"k": [1, 2]})
        self.assertEqual(back.to_record()["type"], "future_thing")


# ── ① 同一 leaf 两次投影逐字节相同 ─────────────────────────────
class ProjectionPurityTests(_Base):
    def test_same_leaf_projection_is_byte_identical(self):
        self._user("hi", name="Alice")
        self._assistant("hello")
        leaf = self.mgr.leaf_id()

        builder = ContextBuilder(self.store)
        first = builder.project(leaf)     # 未命中
        second = builder.project(leaf)    # 命中
        stats = builder.stats()
        self.assertEqual(stats["misses"], 1)
        self.assertEqual(stats["hits"], 1)
        self.assertEqual(_bytes(first), _bytes(second))
        # 缓存不得改变字节结果：直接重算（无缓存）也同字节
        self.assertEqual(_bytes(first), _bytes(project(self.store, leaf)))
        # 命中路径返回副本，不泄漏可变状态
        self.assertIsNot(first, second)

    def test_projection_is_pure_and_does_not_write(self):
        self._user("hi")
        before = self.store.raw_bytes()
        leaf = self.mgr.leaf_id()
        a = project(self.store, leaf)
        b = project(self.store, leaf)
        self.assertEqual(_bytes(a), _bytes(b))
        self.assertEqual(self.store.raw_bytes(), before)  # 投影只读


# ── ② 追加不改动旧前缀字节 ────────────────────────────────────
class PrefixImmutableTests(_Base):
    def test_append_does_not_change_existing_prefix_bytes(self):
        self._user("u1")
        leaf_old = self.mgr.leaf_id()
        before_text = self.store.raw_text()
        before_projection = _bytes(project(self.store, leaf_old))

        self._assistant("a1")

        after_text = self.store.raw_text()
        self.assertTrue(after_text.startswith(before_text))            # 旧前缀字节不变
        self.assertEqual(before_text, after_text[:len(before_text)])
        # 旧 leaf 的投影也随之逐字节不变
        self.assertEqual(before_projection, _bytes(project(self.store, leaf_old)))
        self.assertEqual(len(self.store.read_entries()), 2)


# ── ③ context_edit 只影响未来投影 ─────────────────────────────
class ContextEditTests(_Base):
    def test_context_edit_does_not_affect_past_projection(self):
        self._user("q", name="Bob")
        a1 = self._assistant("wrong")
        leaf_old = a1.id
        before = _bytes(project(self.store, leaf_old))

        edit = self.mgr.append(make(ContextEditEntry, target_id=a1.id, replacement="fixed"))
        self.assertEqual(edit.type, CONTEXT_EDIT)
        # 旧 leaf 重算 → 编辑节点不在它的分支上 → 结果不变
        self.assertEqual(before, _bytes(project(self.store, leaf_old)))
        # 新 leaf → 编辑生效（只影响未来）
        after = project(self.store, edit.id)
        self.assertIn({"role": "assistant", "content": "fixed"}, after)
        self.assertNotIn("wrong", _bytes(after))

    def test_context_edit_omit_removes_target(self):
        self._user("q")
        a1 = self._assistant("secret")
        omit = self.mgr.append(make(ContextEditEntry, target_id=a1.id, omit=True))
        msgs = project(self.store, omit.id)
        self.assertNotIn("secret", _bytes(msgs))
        # 原始记录仍在日志里（日志只增不减）
        self.assertIn(a1.id, {e.id for e in self.store.read_entries()})


# ── ④ 压缩是节点 ──────────────────────────────────────────────
class CompactionTests(_Base):
    def test_compaction_is_a_node_and_old_leaf_reprojects_unchanged(self):
        u1 = self._user("u1")
        a1 = self._assistant("a1")
        u2 = self._user("u2")
        a2 = self._assistant("a2")
        leaf_old = a2.id
        before = _bytes(project(self.store, leaf_old))

        comp = self.mgr.append(make(CompactionEntry, summary="SUMMARY",
                                    first_kept_id=u2.id, tokens_before=123))
        self.assertEqual(comp.type, COMPACTION)
        # 压缩是节点：原始记录全部仍在日志里
        ids = {e.id for e in self.store.read_entries()}
        self.assertTrue({u1.id, a1.id, u2.id, a2.id}.issubset(ids))
        # 回到旧 leaf → 得到原样投影
        self.assertEqual(before, _bytes(project(self.store, leaf_old)))
        # 新 leaf → 摘要 + first_kept 起；被压掉的不再出现
        after = project(self.store, comp.id)
        self.assertEqual(after[0], {"role": "system", "content": "SUMMARY"})
        joined = _bytes(after)
        self.assertNotIn("u1", joined)
        self.assertNotIn("a1", joined)
        self.assertIn("u2", joined)
        self.assertIn("a2", joined)


# ── ⑤ 分支 = 移 leaf ──────────────────────────────────────────
class BranchMoveTests(_Base):
    def test_branch_is_leaf_move_two_leaves_project_separately(self):
        u1 = self._user("shared")
        a1 = self._assistant("branch-A")
        leaf_a = a1.id

        # 移动 leaf 回 u1，再追加 → 分叉（不是复制/编辑）
        self.mgr.checkout(u1.id)
        a2 = self._assistant("branch-B")
        leaf_b = a2.id

        self.assertNotEqual(leaf_a, leaf_b)
        self.assertEqual(self.mgr.leaf_id(), leaf_b)  # leaf = 最后一行

        bytes_a = _bytes(project(self.store, leaf_a))
        bytes_b = _bytes(project(self.store, leaf_b))
        self.assertIn("branch-A", bytes_a)
        self.assertNotIn("branch-B", bytes_a)
        self.assertIn("branch-B", bytes_b)
        self.assertNotIn("branch-A", bytes_b)

        # 两条分支各自成链、共享前缀，互不污染
        self.assertEqual([e.id for e in self.mgr.get_branch(leaf_a)], [u1.id, a1.id])
        self.assertEqual([e.id for e in self.mgr.get_branch(leaf_b)], [u1.id, a2.id])
        self.assertEqual([c.id for c in self.mgr.get_children(u1.id)], [a1.id, a2.id])

        # 移动 leaf 无复制、无编辑：日志里只有 3 条
        self.assertEqual(len(self.store.read_entries()), 3)


# ── ⑥ 1 assistant = 1 节点 + 每个 tool_result 单独成节点 ──────
class NodeGranularityTests(_Base):
    def test_one_assistant_one_node_and_each_tool_result_separate(self):
        self._user("do it")
        asst = self.mgr.append(make(MessageEntry, role=ROLE_ASSISTANT, content=""))
        self.mgr.append(make(ToolCallEntry, tool_call_id="c1", name="craft",
                             arguments={"item": "stick"}))
        self.mgr.append(make(ToolCallEntry, tool_call_id="c2", name="eat", arguments={}))
        self.mgr.append(make(MessageEntry, role=ROLE_TOOL_RESULT, content="ok1",
                             tool_call_id="c1"))
        self.mgr.append(make(MessageEntry, role=ROLE_TOOL_RESULT, content="ok2",
                             tool_call_id="c2"))

        entries = self.store.read_entries()
        self.assertEqual(len(entries), 6)
        assistants = [e for e in entries
                      if isinstance(e, MessageEntry) and e.role == ROLE_ASSISTANT]
        results = [e for e in entries
                   if isinstance(e, MessageEntry) and e.role == ROLE_TOOL_RESULT]
        self.assertEqual(len(assistants), 1)      # 1 assistant = 1 节点
        self.assertEqual(len(results), 2)         # 每个 tool_result 单独成节点
        self.assertEqual({e.type for e in entries if isinstance(e, ToolCallEntry)}, {TOOL_CALL})

        msgs = project(self.store, self.mgr.leaf_id())
        asst_msgs = [m for m in msgs if m.get("role") == "assistant"]
        self.assertEqual(len(asst_msgs), 1)       # 投影里也只有一个 assistant
        self.assertEqual([c["id"] for c in asst_msgs[0]["tool_calls"]], ["c1", "c2"])
        tool_msgs = [m for m in msgs if m.get("role") == "tool"]
        self.assertEqual([t["tool_call_id"] for t in tool_msgs], ["c1", "c2"])
        self.assertTrue(asst.id in {e.id for e in entries})

    def test_metadata_nodes_do_not_enter_messages(self):
        self._user("hi")
        self.mgr.append(make(ModelChangeEntry, provider="p", api="a", model="m"))
        self.mgr.append(make(UsageEntry, input_tokens=3, output_tokens=4, model="m"))
        self.mgr.append(make(BranchSummaryEntry, summary="bs"))
        msgs = project(self.store, self.mgr.leaf_id())
        roles = [m["role"] for m in msgs]
        self.assertEqual(roles, ["user"])


# ── ⑦ 分段提示词只在变更时追加 patch ──────────────────────────
class SegmentedPromptTests(_Base):
    def test_system_prompt_patches_only_on_change_and_is_byte_stable(self):
        initial = build_sections(**{
            L0_CONTRACT: "BASE-CONTRACT",
            RULES: "RULE-1",
            PERSONA: "PERSONA-1",
        })
        self.mgr.append(make(SystemMessageEntry, sections=initial))
        self.assertEqual(_sys(self.store), initial)
        rendered_before = _sys_render(self.store)
        msgs_before = _bytes(project(self.store, self.mgr.leaf_id()))

        # 内容未变 → diff 为空 → 不追加变更 → 结果逐字节相同
        unchanged = diff_sections(initial, dict(initial))
        self.assertEqual(unchanged, {})
        self.mgr.append(make(SystemMessageEntry, sections=unchanged))
        self.assertEqual(_sys(self.store), initial)              # 未变 → 同一 sections
        self.assertEqual(_sys_render(self.store), rendered_before)   # 渲染逐字节相同
        self.assertEqual(_bytes(project(self.store, self.mgr.leaf_id())), msgs_before)

        # 内容变更 → 只追加变更的那一段
        new = dict(initial)
        new[RULES] = "RULE-2"
        patch = diff_sections(initial, new)
        self.assertEqual(patch, {RULES: "RULE-2"})
        self.mgr.append(make(SystemMessageEntry, sections=patch))

        current = _sys(self.store)
        self.assertEqual(current[RULES], "RULE-2")
        self.assertEqual(current[L0_CONTRACT], "BASE-CONTRACT")   # 稳定前缀未受影响
        rendered = _sys_render(self.store)
        self.assertTrue(rendered.startswith("BASE-CONTRACT"))      # L0 在最前
        self.assertIn("RULE-2", rendered)
        self.assertNotIn("RULE-1", rendered)

    def test_render_order_is_stable_and_removal_supported(self):
        sections = build_sections(**{PERSONA: "P", AUTHZ: "A", MECHANICS: "M",
                                     L0_CONTRACT: "L0", RULES: "R"})
        rendered = render(sections)
        self.assertTrue(rendered.startswith("L0"))
        self.assertLess(rendered.index("R"), rendered.index("P"))
        self.assertEqual(tuple(sections), (PERSONA, AUTHZ, MECHANICS, L0_CONTRACT, RULES))
        # 删段：patch 值为 None
        after = apply_patch(sections, {AUTHZ: None})
        self.assertNotIn(AUTHZ, after)
        self.assertEqual(render(replay([sections, {AUTHZ: None}])), render(after))
        self.assertIn(PERSONA, SECTION_ORDER)

    def test_system_sections_appear_in_projection_as_single_system_message(self):
        self.mgr.append(make(SystemMessageEntry,
                             sections=build_sections(**{L0_CONTRACT: "L0X"})))
        self.mgr.append(make(SystemMessageEntry, sections={PERSONA: "P1"}))
        self._user("hi")
        msgs = project(self.store, self.mgr.leaf_id())
        system_msgs = [m for m in msgs if m["role"] == "system"]
        self.assertEqual(len(system_msgs), 1)             # 重放 patch → 单条 system
        self.assertTrue(system_msgs[0]["content"].startswith("L0X"))
        self.assertIn("P1", system_msgs[0]["content"])


def _sys(store: SessionStore) -> dict:
    """重放分支上的所有 system_message patch → 当前 sections。"""
    return replay(
        e.sections for e in store.read_entries() if isinstance(e, SystemMessageEntry)
    )


def _sys_render(store: SessionStore) -> str:
    return render(_sys(store))


# ── 适配器等价性（只读，不接线） ──────────────────────────────
class AdapterEquivalenceTests(_Base):
    def test_adapter_matches_legacy_context_shape(self):
        sections = build_sections(**{L0_CONTRACT: "L0X", PERSONA: "PERSONA-TEXT",
                                     RULES: "RULE-TEXT"})
        self.mgr.append(make(SystemMessageEntry, sections=sections))
        self._user("你好", name="Alice")
        self._assistant("你好呀")
        self._user("帮我造床", name="Bob")
        self._assistant("好的")

        msgs = project(self.store, self.mgr.leaf_id())
        from agent.session_core import to_legacy_context
        ctx = to_legacy_context(msgs, system_sections=sections)

        # 字段对得上：interaction_summary 与现有 Memory 口径逐字节一致
        memory = Memory(path=os.path.join(self._tmp.name, "memory.json"))
        memory.recent_messages = [
            {"sender": "Alice", "message": "你好", "response": "你好呀"},
            {"sender": "Bob", "message": "帮我造床", "response": "好的"},
        ]
        self.assertEqual(ctx["interaction_summary"], memory._build_interaction_summary())

        self.assertTrue(ctx["system_prompt"].startswith("L0X"))
        self.assertEqual(ctx["persona_blocks"]["identity"], "PERSONA-TEXT")
        self.assertEqual(ctx["persona_blocks"]["scene_rules"], "RULE-TEXT")
        self.assertEqual([p["sender"] for p in ctx["chat_pairs"]], ["Alice", "Bob"])
        self.assertEqual(ctx["message"], "帮我造床")
        self.assertEqual(ctx["last_speaker"], "Bob")
        self.assertEqual([m["role"] for m in ctx["messages"]], [m["role"] for m in msgs])

    def test_adapter_handles_empty_projection(self):
        from agent.session_core import to_legacy_context
        ctx = to_legacy_context([])
        self.assertEqual(ctx["interaction_summary"], "暂无对话记录")
        self.assertEqual(ctx["message"], "")
        self.assertEqual(ctx["recent_chat"], [])


if __name__ == "__main__":
    unittest.main()
