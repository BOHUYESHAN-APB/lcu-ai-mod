"""纯投影：``project(store, leaf) -> messages``。

对应 ``docs/dev/AGENT_ARCH.md`` §1「投影」：

```
active_branch = leaf → root 的路径            （读时算）
projection    = 对 active_branch 应用：最近的 compaction + 每个 target 的 context_edit
messages      = projection → LLM messages     （纯函数，随时可从日志重算）
```

**纯函数**：同一份日志 + 同一个 leaf → **逐字节相同**（不依赖任何隐藏状态）。
``ContextBuilder`` 只是加一层"按 leaf 缓存"，缓存**不改变**字节结果（未命中/命中两条路径同字节）。
"""

from __future__ import annotations

import copy
import dataclasses
import json

from .entries import (
    ROLE_ASSISTANT,
    ROLE_TOOL,
    ROLE_TOOL_RESULT,
    ROLE_USER,
    CompactionEntry,
    ContextEditEntry,
    Entry,
    MessageEntry,
    SystemMessageEntry,
    ToolCallEntry,
)
from .session_store import SessionStore
from .system_prompt import render, replay

_API_ROLE = {
    ROLE_USER: "user",
    ROLE_ASSISTANT: "assistant",
    ROLE_TOOL_RESULT: ROLE_TOOL,   # OpenAI: tool_result → role "tool"
}


# ── 树 → 分支（读时算） ─────────────────────────────────────────
def active_branch(store: SessionStore, leaf: str | None) -> list[Entry]:
    """``leaf`` → root 一路回溯（**读时算**），返回 root→leaf 顺序。"""
    entries = store.read_entries()
    by_id = {e.id: e for e in entries}
    chain: list[Entry] = []
    seen: set[str] = set()
    cur = leaf
    while cur is not None and cur in by_id and cur not in seen:
        seen.add(cur)
        node = by_id[cur]
        chain.append(node)
        cur = node.parent_id
    chain.reverse()
    return chain


# ── compaction（压缩是节点不是编辑） ────────────────────────────
def _apply_compaction(branch: list[Entry]) -> tuple[CompactionEntry | None, list[Entry]]:
    comps = [i for i, e in enumerate(branch) if isinstance(e, CompactionEntry)]
    if not comps:
        return None, list(branch)
    ci = comps[-1]
    compaction = branch[ci]
    pos = {e.id: k for k, e in enumerate(branch)}
    first_kept = compaction.first_kept_id
    if first_kept in pos and pos[first_kept] < ci:
        kept = [e for e in branch[pos[first_kept]:ci] if not isinstance(e, CompactionEntry)]
    else:
        kept = [e for e in branch[ci + 1:] if not isinstance(e, CompactionEntry)]
    return compaction, kept


# ── context_edit（只影响未来投影） ──────────────────────────────
def apply_context_edits(entries: list[Entry]) -> list[Entry]:
    """把分支上所有 ``ContextEditEntry`` 应用到目标节点（omit 删除 / replacement 换文本）。

    编辑节点本身追加在目标之后，因此**旧 leaf 的分支里没有它** → 旧投影逐字节不变。
    """
    edits: dict[str, ContextEditEntry] = {}
    for e in entries:
        if isinstance(e, ContextEditEntry):
            edits[e.target_id] = e
    out: list[Entry] = []
    for e in entries:
        edit = edits.get(e.id)
        if edit is not None:
            if edit.omit:
                continue
            if edit.replacement is not None and isinstance(e, MessageEntry):
                e = dataclasses.replace(e, content=str(edit.replacement))
        out.append(e)
    return out


def build_projection(store: SessionStore, leaf: str | None) -> list[Entry]:
    """分支 → （应用 compaction + context_edit 后）将要渲染的条目列表。"""
    branch = active_branch(store, leaf)
    _, kept = _apply_compaction(branch)
    return apply_context_edits(kept)


# ── 条目 → LLM messages ────────────────────────────────────────
def _dumps(arguments) -> str:
    return json.dumps(arguments or {}, ensure_ascii=False, sort_keys=True)


def build_messages(entries: list[Entry],
                   compaction: CompactionEntry | None = None) -> list[dict]:
    """把（已应用 compaction / edit 的）条目列表渲染成 LLM messages。"""
    messages: list[dict] = []

    if compaction is not None and compaction.summary:
        messages.append({"role": "system", "content": str(compaction.summary)})

    sections = replay(
        e.sections for e in entries if isinstance(e, SystemMessageEntry)
    )
    system_text = render(sections)
    if system_text:
        messages.append({"role": "system", "content": system_text})

    for e in entries:
        if isinstance(e, MessageEntry):
            msg: dict = {"role": _API_ROLE.get(e.role, e.role), "content": e.content}
            if e.role == ROLE_TOOL_RESULT:
                if e.tool_call_id is not None:
                    msg["tool_call_id"] = e.tool_call_id
            elif e.name is not None:
                msg["name"] = e.name
            messages.append(msg)
        elif isinstance(e, ToolCallEntry):
            call = {
                "id": e.tool_call_id,
                "type": "function",
                "function": {"name": e.name, "arguments": _dumps(e.arguments)},
            }
            if messages and messages[-1].get("role") == "assistant":
                messages[-1].setdefault("tool_calls", []).append(call)
            else:
                messages.append({"role": "assistant", "content": "", "tool_calls": [call]})
        # system_message / context_edit / compaction / model_change / usage / 其他：不进入 messages
    return messages


def project(store: SessionStore, leaf: str | None) -> list[dict]:
    """**纯函数**：同一份日志 + 同一个 leaf → 逐字节相同的 messages。"""
    branch = active_branch(store, leaf)
    compaction, kept = _apply_compaction(branch)
    entries = apply_context_edits(kept)
    return build_messages(entries, compaction)


class ContextBuilder:
    """``project`` 的带缓存封装；缓存按 leaf，**不改变**字节结果。"""

    def __init__(self, store: SessionStore):
        self.store = store
        self._cache: dict[str | None, list[dict]] = {}
        self.hits = 0
        self.misses = 0

    def project(self, leaf: str | None) -> list[dict]:
        if leaf in self._cache:
            self.hits += 1
            return copy.deepcopy(self._cache[leaf])
        self.misses += 1
        messages = project(self.store, leaf)
        self._cache[leaf] = copy.deepcopy(messages)
        return messages

    def invalidate(self, leaf: str | None = None) -> None:
        if leaf is None:
            self._cache.clear()
        else:
            self._cache.pop(leaf, None)

    def stats(self) -> dict:
        return {"hits": self.hits, "misses": self.misses, "size": len(self._cache)}


__all__ = [
    "ContextBuilder",
    "active_branch",
    "apply_context_edits",
    "build_messages",
    "build_projection",
    "project",
]
