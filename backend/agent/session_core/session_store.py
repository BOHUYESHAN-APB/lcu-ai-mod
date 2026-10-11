"""只追加的会话日志存储：一行一个 JSON 记录（JSONL）。

铁律（``docs/dev/AGENT_ARCH.md`` §1）：

* 第一行是 **header**（``type=session``：版本 / 会话 id / 创建时间 / scope），**不参与树**；
* 之后每行是一个 entry（``id / parent_id / timestamp / type / payload``）；
* **永不修改已写的行**（改写 = 追加一个重定向节点）；本类**不暴露** update/delete。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from .entries import Entry

HEADER_TYPE = "session"


class SessionStoreError(RuntimeError):
    """存储层的硬约束被违反（例如重复写 header）。"""


class SessionStore:
    """一个会话 = 一个 JSONL 文件；只追加、只读取。"""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    # ── 读取 ──
    def exists(self) -> bool:
        return self.path.is_file() and self.path.stat().st_size > 0

    def raw_text(self) -> str:
        """整份日志的原始文本（测试用来验证"旧前缀字节不变"）。"""
        if not self.path.is_file():
            return ""
        return self.path.read_text(encoding="utf-8")

    def raw_bytes(self) -> bytes:
        if not self.path.is_file():
            return b""
        return self.path.read_bytes()

    def read_all(self) -> list[dict]:
        """所有行（含 header），按追加顺序。文件不存在 → 空列表。"""
        if not self.path.is_file():
            return []
        records: list[dict] = []
        text = self.path.read_text(encoding="utf-8")
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            records.append(json.loads(line))
        return records

    def read_header(self) -> dict | None:
        """第一行 header（不是 header → None）。"""
        for record in self.read_all():
            if record.get("type") == HEADER_TYPE:
                return record
            break  # 只看第一行
        return None

    def read_entries(self) -> list[Entry]:
        """树里的条目（header **不参与树**）。"""
        out: list[Entry] = []
        for record in self.read_all():
            if record.get("type") == HEADER_TYPE:
                continue
            out.append(Entry.from_record(record))
        return out

    # ── 写入（只增） ──
    def write_header(self, session_id: str, *, scope: str = "", version: int = 1,
                     created_at: float | None = None, session_key: str = "") -> dict:
        """写第一行 header；文件已有内容则**拒绝**（绝不改写）。"""
        if self.exists():
            raise SessionStoreError("header 已存在，拒绝改写（只追加日志）")
        header = {
            "type": HEADER_TYPE,
            "version": int(version),
            "id": str(session_id),
            "scope": str(scope),
            "created_at": float(created_at if created_at is not None else time.time()),
        }
        if session_key:
            header["session_key"] = str(session_key)
        self._write_line(header)
        return header

    def append(self, entry: Entry | dict) -> dict:
        """追加一个条目（一行 JSON + LF）；返回写下的记录。"""
        record = entry.to_record() if isinstance(entry, Entry) else dict(entry)
        self._write_line(record)
        return record

    # ── 内部 ──
    def _write_line(self, obj: dict) -> None:
        line = json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8", newline="\n") as fh:
            fh.write(line + "\n")
