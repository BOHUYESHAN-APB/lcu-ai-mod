"""会话树：leaf = 最后一行；分支 = leaf → root（**读时算**）；分支 = **移动 leaf**。

对应 ``docs/dev/AGENT_ARCH.md`` §1：

* **leaf = 最后追加的那个节点**（= 文件最后一行）；
* **活动分支 = 从 leaf 一路向上回溯到 root 的路径**，**读时算出来、绝不另存一份**；
* "切分支" = **移动 leaf**（把下一次追加的 parent 指到另一个已有节点）——
  **不是复制、也不是编辑**（日志一行都不动）。

本类只缓存 **id → Entry 索引**（树的形状直接从日志重算，不是第二份真相）。
"""

from __future__ import annotations

import time

from .entries import Entry, new_id
from .session_store import SessionStore


class SessionManager:
    """管理一个 ``SessionStore`` 上的树/分支读操作。"""

    def __init__(self, store: SessionStore, *, session_id: str | None = None,
                 scope: str = "", version: int = 1, autostart: bool = True):
        self.store = store
        self._head: str | None = None  # 切分支时的"当前 leaf"指针（None = 最后一行）
        self._reload()
        if autostart and not self.store.exists() and session_id is not None:
            self.store.write_header(session_id, scope=scope, version=version)

    # ── 索引（从日志重算） ──
    def _reload(self) -> None:
        entries = self.store.read_entries()
        self._index: dict[str, Entry] = {e.id: e for e in entries}
        self._children: dict[str | None, list[str]] = {}
        for e in entries:
            self._children.setdefault(e.parent_id, []).append(e.id)
        self._order: list[str] = [e.id for e in entries]

    def reload(self) -> None:
        """从磁盘重算索引（用于外部追加后同步）。"""
        self._reload()

    # ── 读 ──
    def by_id(self, id: str | None) -> Entry | None:
        return self._index.get(id) if id is not None else None

    def leaf_id(self) -> str | None:
        """**最后一行**的 id（= 文件里最后一个节点）。"""
        return self._order[-1] if self._order else None

    def head_id(self) -> str | None:
        """下一次追加的默认 parent（切分支后为被 checkout 的节点，否则 = leaf）。"""
        return self._head if self._head is not None else self.leaf_id()

    def branch(self, id: str | None = None) -> list[Entry]:
        """活动分支：``id``（默认当前 leaf）→ root，**读时算**，返回 leaf→root 顺序。"""
        start = id if id is not None else self.leaf_id()
        chain: list[Entry] = []
        seen: set[str] = set()
        cur = start
        while cur is not None and cur in self._index and cur not in seen:
            seen.add(cur)
            node = self._index[cur]
            chain.append(node)
            cur = node.parent_id
        return chain

    def get_branch(self, id: str | None = None) -> list[Entry]:
        """同 ``branch`` 但返回 root→leaf 顺序（投影更方便）。"""
        return list(reversed(self.branch(id)))

    def get_children(self, id: str | None) -> list[Entry]:
        return [self._index[c] for c in self._children.get(id, [])]

    # ── 写（只增） ──
    def append(self, entry: Entry, *, parent_id: str | None = None) -> Entry:
        """追加一个条目；``parent_id`` 默认指向当前 leaf（切分支后 = 被 checkout 的节点）。"""
        entry.parent_id = parent_id if parent_id is not None else self.head_id()
        if not entry.id:
            entry.id = new_id()
        if not entry.timestamp:
            entry.timestamp = time.time()
        self.store.append(entry)
        self._index[entry.id] = entry
        self._children.setdefault(entry.parent_id, []).append(entry.id)
        self._order.append(entry.id)
        # 新追加 = 新的最后一行 → 指针归位（head 恒等于最后一行）
        self._head = None
        return entry

    def checkout(self, id: str | None) -> str | None:
        """**移动 leaf**：下一次追加的 parent 指向 ``id``（= 切分支，不复制不编辑）。"""
        if id is not None and id not in self._index:
            raise KeyError(f"checkout 目标不存在：{id}")
        self._head = id
        return self.head_id()
