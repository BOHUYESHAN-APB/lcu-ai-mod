"""会话日志的条目模型（只追加树的节点）。

每一行 JSONL 记录的形状**永远**是：

    {"id": ..., "parent_id": ..., "timestamp": ..., "type": ..., "payload": {...}}

具体类型把自己的字段放在 ``payload`` 里（下面对应的 dataclass），
这样"日志格式 = id/parent_id/timestamp/type/payload"这条铁律不随类型增删而变。

关键约束（见 ``docs/dev/AGENT_ARCH.md`` §1）：

* **1 条 assistant = 1 个节点**；**每个 tool_result 单独成节点**，用 ``tool_call_id`` 关联；
* ``context_edit`` 是"改写"唯一被允许的形式（追加一个**重定向节点**，只影响未来投影）；
* ``compaction`` 是**节点**而不是编辑（原始历史永远留在日志里）。
"""

from __future__ import annotations

import dataclasses
import time
import uuid
from dataclasses import dataclass, field
from typing import ClassVar

# ── 条目类型名（写进 JSONL 的 "type" 字段） ──────────────────────
MESSAGE = "message"
TOOL_CALL = "tool_call"
SYSTEM_MESSAGE = "system_message"
CONTEXT_EDIT = "context_edit"
COMPACTION = "compaction"
BRANCH_SUMMARY = "branch_summary"
MODEL_CHANGE = "model_change"
USAGE = "usage"

# ── 消息角色（MessageEntry.role） ───────────────────────────────
ROLE_USER = "user"
ROLE_ASSISTANT = "assistant"
ROLE_TOOL_RESULT = "tool_result"
ROLE_TOOL = "tool"  # 投影成 LLM message 时 tool_result 用 OpenAI 的 "tool" 角色


def new_id() -> str:
    """生成一个条目 id（只用于标识，不承载语义；测试可传显式 id）。"""
    return uuid.uuid4().hex


@dataclass
class Entry:
    """所有条目的基类：只带树坐标 + 类型名；具体字段放 ``payload``。"""

    id: str
    parent_id: str | None = None
    timestamp: float = 0.0

    type: ClassVar[str] = "entry"

    def payload(self) -> dict:
        """本条目写进 JSONL 的 ``payload``。"""
        return {}

    def to_record(self) -> dict:
        """投影成一行 JSONL 记录。"""
        return {
            "id": self.id,
            "parent_id": self.parent_id,
            "timestamp": self.timestamp,
            "type": self.type,
            "payload": self.payload(),
        }

    # ── 反序列化 ──
    @classmethod
    def from_payload(cls, id: str, parent_id: str | None,
                     timestamp: float, payload: dict) -> Entry:
        """按 dataclass 字段名把 ``payload`` 里的键填回具体类型。"""
        names = {f.name for f in dataclasses.fields(cls)} - {"id", "parent_id", "timestamp"}
        kwargs = {k: v for k, v in (payload or {}).items() if k in names}
        return cls(id=id, parent_id=parent_id, timestamp=timestamp, **kwargs)

    @classmethod
    def from_record(cls, record: dict) -> Entry:
        kind = record.get("type")
        payload = record.get("payload") or {}
        target = _REGISTRY.get(kind)
        if target is None:
            return UnknownEntry(
                id=record.get("id"),
                parent_id=record.get("parent_id"),
                timestamp=record.get("timestamp", 0.0),
                kind=kind or "unknown",
                data=dict(payload),
            )
        return target.from_payload(
            record.get("id"),
            record.get("parent_id"),
            record.get("timestamp", 0.0),
            payload,
        )


@dataclass
class MessageEntry(Entry):
    """一条对话消息（user / assistant / tool_result）。

    * ``role="assistant"`` 时，out of band 的 ``name`` 可选；
    * ``role="tool_result"`` 时 ``tool_call_id`` 关联到对应的 ``ToolCallEntry``；
    * ``name`` 走 OpenAI 消息的合法字段（不额外发明协议）。
    """

    type: ClassVar[str] = MESSAGE
    role: str = ROLE_USER
    content: str = ""
    name: str | None = None
    tool_call_id: str | None = None
    source: str | None = None

    def payload(self) -> dict:
        p: dict = {"role": self.role, "content": self.content}
        if self.name is not None:
            p["name"] = self.name
        if self.tool_call_id is not None:
            p["tool_call_id"] = self.tool_call_id
        if self.source is not None:
            p["source"] = self.source
        return p


@dataclass
class ToolCallEntry(Entry):
    """一次工具调用（作为 assistant 节点的子节点，可多个）。"""

    type: ClassVar[str] = TOOL_CALL
    tool_call_id: str = ""
    name: str = ""
    arguments: dict = field(default_factory=dict)

    def payload(self) -> dict:
        return {
            "tool_call_id": self.tool_call_id,
            "name": self.name,
            "arguments": dict(self.arguments or {}),
        }


@dataclass
class SystemMessageEntry(Entry):
    """系统提示词：首次全量（``sections``），之后只追加变更的那一段（patch）。"""

    type: ClassVar[str] = SYSTEM_MESSAGE
    sections: dict = field(default_factory=dict)
    tools_added: list = field(default_factory=list)
    tools_removed: list = field(default_factory=list)

    def payload(self) -> dict:
        return {
            "sections": dict(self.sections or {}),
            "tools_added": list(self.tools_added or []),
            "tools_removed": list(self.tools_removed or []),
        }


@dataclass
class ContextEditEntry(Entry):
    """★ "改写"唯一被允许的形式：追加一个重定向节点，只影响**未来**的投影。"""

    type: ClassVar[str] = CONTEXT_EDIT
    target_id: str = ""
    replacement: str | None = None
    omit: bool = False

    def payload(self) -> dict:
        p: dict = {"target_id": self.target_id, "omit": bool(self.omit)}
        if self.replacement is not None:
            p["replacement"] = self.replacement
        return p


@dataclass
class CompactionEntry(Entry):
    """压缩是**节点**，不是编辑；``first_kept_id`` 之前的历史靠投影裁剪。"""

    type: ClassVar[str] = COMPACTION
    summary: str = ""
    first_kept_id: str | None = None
    tokens_before: int = 0

    def payload(self) -> dict:
        return {
            "summary": self.summary,
            "first_kept_id": self.first_kept_id,
            "tokens_before": self.tokens_before,
        }


@dataclass
class BranchSummaryEntry(Entry):
    """分支摘要（把一条旁支压成一段话供当前分支引用）。"""

    type: ClassVar[str] = BRANCH_SUMMARY
    summary: str = ""
    from_id: str | None = None
    to_id: str | None = None

    def payload(self) -> dict:
        return {"summary": self.summary, "from_id": self.from_id, "to_id": self.to_id}


@dataclass
class ModelChangeEntry(Entry):
    """只记事实：这条分支上用的是哪个模型（不存客户端）。"""

    type: ClassVar[str] = MODEL_CHANGE
    provider: str = ""
    api: str = ""
    model: str = ""

    def payload(self) -> dict:
        return {"provider": self.provider, "api": self.api, "model": self.model}


@dataclass
class UsageEntry(Entry):
    """token 用量事实（不参与投影，仅作审计/预算）。"""

    type: ClassVar[str] = USAGE
    input_tokens: int = 0
    output_tokens: int = 0
    model: str = ""

    def payload(self) -> dict:
        return {
            "input_tokens": int(self.input_tokens or 0),
            "output_tokens": int(self.output_tokens or 0),
            "model": self.model,
        }


@dataclass
class UnknownEntry(Entry):
    """未知/未来类型：原样保留类型名与 ``payload``，绝不丢数据。"""

    type: ClassVar[str] = "unknown"
    kind: str = "unknown"
    data: dict = field(default_factory=dict)

    def to_record(self) -> dict:
        record = super().to_record()
        record["type"] = self.kind
        return record

    def payload(self) -> dict:
        return dict(self.data or {})


_REGISTRY = {
    cls.type: cls
    for cls in (
        MessageEntry,
        ToolCallEntry,
        SystemMessageEntry,
        ContextEditEntry,
        CompactionEntry,
        BranchSummaryEntry,
        ModelChangeEntry,
        UsageEntry,
    )
}


def make(entry_cls, *, id: str | None = None, timestamp: float | None = None,
         parent_id: str | None = None, **fields) -> Entry:
    """构造一个条目（自动补 ``id`` / ``timestamp``）；``parent_id`` 由管理器决定。"""
    return entry_cls(
        id=id or new_id(),
        parent_id=parent_id,
        timestamp=timestamp if timestamp is not None else time.time(),
        **fields,
    )


__all__ = [
    "BRANCH_SUMMARY",
    "COMPACTION",
    "CONTEXT_EDIT",
    "MESSAGE",
    "MODEL_CHANGE",
    "ROLE_ASSISTANT",
    "ROLE_TOOL",
    "ROLE_TOOL_RESULT",
    "ROLE_USER",
    "SYSTEM_MESSAGE",
    "TOOL_CALL",
    "USAGE",
    "BranchSummaryEntry",
    "CompactionEntry",
    "ContextEditEntry",
    "Entry",
    "MessageEntry",
    "ModelChangeEntry",
    "SystemMessageEntry",
    "ToolCallEntry",
    "UnknownEntry",
    "UsageEntry",
    "make",
    "new_id",
]
