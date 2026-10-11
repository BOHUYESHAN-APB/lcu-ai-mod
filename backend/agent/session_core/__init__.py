"""session_core —— 只追加会话日志 + 树/分支 + 纯投影 + 分段提示词（新地基，未接线）。

对应 ``docs/dev/AGENT_ARCH.md`` 与 ``docs/dev/PROMPT_MODEL.md`` 的定案：

* 一条**只追加**的日志是唯一真相（``session_store`` / ``session_manager``）；
* 上下文是它上面的**纯投影**（``context_builder``）：同一日志 + 同一 leaf → 逐字节相同；
* 系统提示词**命名分段 + 只追加 patch**（``system_prompt``）：稳定前缀字节不变；
* 把投影结果转成现有 ``context`` 形状的只读适配（``adapter``）。

本批**只新增**，不接线到现有链路（``session.py`` / ``memory.py`` / ``planner.py`` /
``llm_service.py`` 纹丝未动）。
"""

from .adapter import to_legacy_context
from .bridge import (
    MemoryStore,
    entries_from_turns,
    project_history,
    project_turns,
    server_scope,
    world_scope_key,
)
from .context_builder import (
    ContextBuilder,
    active_branch,
    build_messages,
    project,
)
from .entries import (
    BRANCH_SUMMARY,
    COMPACTION,
    CONTEXT_EDIT,
    MESSAGE,
    MODEL_CHANGE,
    ROLE_ASSISTANT,
    ROLE_TOOL,
    ROLE_TOOL_RESULT,
    ROLE_USER,
    SYSTEM_MESSAGE,
    TOOL_CALL,
    USAGE,
    BranchSummaryEntry,
    CompactionEntry,
    ContextEditEntry,
    Entry,
    MessageEntry,
    ModelChangeEntry,
    SystemMessageEntry,
    ToolCallEntry,
    UsageEntry,
    make,
    new_id,
)
from .session_manager import SessionManager
from .session_store import SessionStore, SessionStoreError
from .system_prompt import (
    AUTHZ,
    L0_CONTRACT,
    MECHANICS,
    PERSONA,
    RULES,
    SECTION_ORDER,
    TOOL_CONTRACT,
    apply_patch,
    build_sections,
    diff_sections,
    render,
    replay,
)

__all__ = [
    "AUTHZ",
    "BRANCH_SUMMARY",
    "COMPACTION",
    "CONTEXT_EDIT",
    # system prompt
    "L0_CONTRACT",
    "MECHANICS",
    "MESSAGE",
    "MODEL_CHANGE",
    "PERSONA",
    "ROLE_ASSISTANT",
    "ROLE_TOOL",
    "ROLE_TOOL_RESULT",
    "ROLE_USER",
    "RULES",
    "SECTION_ORDER",
    "SYSTEM_MESSAGE",
    "TOOL_CALL",
    "TOOL_CONTRACT",
    "USAGE",
    "BranchSummaryEntry",
    "CompactionEntry",
    "ContextBuilder",
    "ContextEditEntry",
    # entries
    "Entry",
    "MessageEntry",
    "ModelChangeEntry",
    "SessionManager",
    # store / tree
    "SessionStore",
    "SessionStoreError",
    "SystemMessageEntry",
    "ToolCallEntry",
    "UsageEntry",
    "active_branch",
    "apply_patch",
    "build_messages",
    "build_sections",
    "diff_sections",
    "make",
    "new_id",
    # projection
    "project",
    "render",
    "replay",
    # adapter
    "to_legacy_context",
    # read-side bridge (wiring)
    "MemoryStore",
    "entries_from_turns",
    "project_history",
    "project_turns",
    "server_scope",
    "world_scope_key",
]
