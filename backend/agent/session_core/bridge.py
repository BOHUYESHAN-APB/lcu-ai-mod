"""只读桥：现有「对话历史」读取源 → session_core 纯投影 → 现有 ``context`` 形状。

对应 ``docs/dev/AGENT_ARCH.md`` §1：**一条只追加的日志是唯一真相，上下文是它上面的投影**。

本批**只接线读取**（先证后接）：

* **写侧不动**：落盘仍由现有链路负责（``session.py`` 的 ``_absorb_*`` /
  ``conversation.py`` / ``memory.py``）。这里**不写**任何 ``session_core`` 日志、
  **不双写**、不改真相 —— 只是把现有读取源（``Memory.recent_messages`` /
  ``ConversationStore`` 窗口）**在内存里**按真实形状拼成一条只追加链，跑
  ``project`` + ``to_legacy_context``，得到与旧链路等价的上下文片段。
* **分区不串层**：分区键复用现有口径 —— ``server:<key>`` 走
  ``conversation.server_key`` / ``sanitize_key``；``world:<...>`` 走
  ``world_memory.world_scope``。投影按 scope 逐次重算，**绝不跨层复用**。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from .adapter import to_legacy_context
from .context_builder import project
from .entries import (
    ROLE_ASSISTANT,
    ROLE_USER,
    MessageEntry,
    SystemMessageEntry,
    make,
)

# scope 前缀（与 docs/dev/AGENT_ARCH.md §4 的记忆四层一致）。
SERVER_PREFIX = "server:"
WORLD_PREFIX = "world:"


def server_scope(server, world_name) -> str:
    """server 层分区键 ``server:<key>``（复用 ``conversation`` 的 key 口径）。"""
    from ..conversation import sanitize_key, server_key

    return f"{SERVER_PREFIX}{sanitize_key(server_key(server, world_name))}"


def world_scope_key(server, world_name, dimension) -> str:
    """world 层分区键 ``world:<key>``（复用 ``world_memory`` 的 key 口径）。"""
    from ..world_memory import world_scope

    return f"{WORLD_PREFIX}{world_scope(server, world_name, dimension)}"


class MemoryStore:
    """内存态存储：只实现投影需要的 ``read_entries()``（**不落盘、只读**）。

    ``project`` 只调用 ``store.read_entries()``（见 ``context_builder.active_branch``），
    因此这个最小实现就够用，且天然满足"投影不改写真源"。
    """

    def __init__(self, entries: Iterable):
        self._entries = list(entries)

    def exists(self) -> bool:
        return bool(self._entries)

    def read_entries(self) -> list:
        return list(self._entries)


def entries_from_turns(turns: Iterable[Mapping],
                       *, system_sections: Mapping[str, str] | None = None,
                       ) -> tuple[list, str | None]:
    """把 memory 形状的 turns（``{sender, message, response}``）拼成一条只追加链。

    * 每条 turn → user 节点（``name=sender``）；有回复时再接一个 assistant 节点
      （与 ``Memory._build_interaction_summary`` 只在该 turn 有 ``response`` 时才打
      ``AI:`` 行**对齐**）；
    * 可选 ``system_sections`` 作为首个 ``SystemMessageEntry``（首次全量）。
    """
    entries: list = []
    parent: str | None = None
    if system_sections:
        node = make(SystemMessageEntry, sections=dict(system_sections), parent_id=parent)
        entries.append(node)
        parent = node.id
    for turn in turns or []:
        sender = str((turn or {}).get("sender") or "")
        message = str((turn or {}).get("message") or "")
        response = str((turn or {}).get("response") or "")
        user = make(MessageEntry, role=ROLE_USER, content=message,
                    name=sender or None, parent_id=parent)
        entries.append(user)
        parent = user.id
        if response:
            assistant = make(MessageEntry, role=ROLE_ASSISTANT, content=response,
                             parent_id=parent)
            entries.append(assistant)
            parent = assistant.id
    leaf = entries[-1].id if entries else None
    return entries, leaf


def project_turns(turns: Iterable[Mapping],
                  *, system_sections: Mapping[str, str] | None = None) -> list[dict]:
    """turns → session_core 投影（LLM messages；纯函数、不落盘）。"""
    entries, leaf = entries_from_turns(turns, system_sections=system_sections)
    return project(MemoryStore(entries), leaf)


def project_history(turns: Iterable[Mapping], *, scope: str = "",
                    system_sections: Mapping[str, str] | None = None) -> dict:
    """按 scope 读「对话历史」→ 现有 ``context`` 形状（**只读**）。

    ``scope`` 不参与投影内容，只作不串层的标记（同一份 turns + 同一 scope → 逐字节相同）；
    分区隔不隔离由调用方按 scope 选源决定（见 ``server_scope`` / ``world_scope_key``）。
    """
    messages = project_turns(turns, system_sections=system_sections)
    ctx = to_legacy_context(messages, system_sections=system_sections)
    ctx["scope"] = scope
    return ctx


__all__ = [
    "MemoryStore",
    "SERVER_PREFIX",
    "WORLD_PREFIX",
    "entries_from_turns",
    "project_history",
    "project_turns",
    "server_scope",
    "world_scope_key",
]
