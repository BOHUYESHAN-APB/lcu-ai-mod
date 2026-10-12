"""Append-only local audit log for administrative configuration changes."""

from __future__ import annotations

import copy
import json
import threading
import time
import uuid
from pathlib import Path
from typing import Any


SENSITIVE_KEYS = {"api_key", "authorization", "token", "secret", "password"}


def redact_sensitive(value: Any) -> Any:
    redacted = copy.deepcopy(value)

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, item in list(node.items()):
                if str(key).lower() in SENSITIVE_KEYS and item:
                    node[key] = "***"
                else:
                    walk(item)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(redacted)
    return redacted


class AuditLog:
    # 反向扫描的**内存上限**：从文件尾部往前按块读，最多读这么多字节就停。
    # 以前是 `read_text().splitlines()`：**整份文件读进内存**（审计文件 append-only、只增不减）
    # ⇒ 跑到几万条以后，每查一次 `/api/v2/audit` 就吃掉几十 MB 并卡住。现在读的量有界。
    SCAN_CHUNK_BYTES = 256 * 1024
    MAX_SCAN_BYTES = 8 * 1024 * 1024

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def append(self, category: str, action: str, *, target: str = "", actor: str = "operator",
               outcome: str = "succeeded", details: dict[str, Any] | None = None) -> dict[str, Any]:
        event = {
            "id": str(uuid.uuid4()),
            "occurred_at": time.time(),
            "category": str(category),
            "action": str(action),
            "target": str(target),
            "actor": str(actor),
            "outcome": str(outcome),
            "details": redact_sensitive(details or {}),
        }
        encoded = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
        with self._lock:
            with self.path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(encoded + "\n")
        return copy.deepcopy(event)

    def _iter_tail_lines(self, max_lines: int | None = None):
        """**从文件尾部往前**逐行 yield（新 → 旧），读量以 `MAX_SCAN_BYTES` 为上限。

        为什么不是"先切一大段再 split"：调用方要能**边读边判**（类别过滤命中得少就继续
        往前读），所以这里做成生成器；`max_lines` 给到就停（普通查询读一小段就够）。
        每行是完整的（跨块的行首用 `carry` 拼回去，不 yield 半行）。
        """
        try:
            size = self.path.stat().st_size
        except OSError:
            return
        end = size
        floor = max(0, size - self.MAX_SCAN_BYTES)
        carry = b""
        produced = 0
        while end > floor:
            start = max(floor, end - self.SCAN_CHUNK_BYTES)
            try:
                with self.path.open("rb") as handle:
                    handle.seek(start)
                    chunk = handle.read(end - start)
            except OSError:
                return
            end = start
            data = chunk + carry
            if end > 0:
                cut = data.find(b"\n")
                if cut == -1:                 # 这一块里没有换行 ⇒ 全是"某行的尾巴"，再往前读
                    carry = data
                    continue
                carry, data = data[:cut], data[cut + 1:]
            else:
                carry = b""                   # 读到文件头：左边没有了，第一行也完整
            for raw in reversed(data.split(b"\n")):
                if not raw.strip():
                    continue
                yield raw.decode("utf-8", errors="replace")
                produced += 1
                if max_lines is not None and produced >= max_lines:
                    return

    def _tail_lines(self, max_lines: int) -> list[str]:
        """尾部若干行（新 → 旧）；自检/排障看的口径，就是 `_iter_tail_lines` 的前 N 行。"""
        return list(self._iter_tail_lines(max_lines))

    def list(self, *, category: str = "", limit: int = 100) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        if not self.path.exists():
            return []
        records: list[dict[str, Any]] = []
        # 边读边判：命中够了就停（普通查询只读尾部一小段）；过滤命中在很旧的位置时会继续
        # 往前读（最多 `MAX_SCAN_BYTES`）—— 语义不因为"只读尾部"而缩水。
        with self._lock:
            for line in self._iter_tail_lines():
                try:
                    event = json.loads(line)
                except (TypeError, ValueError):
                    continue
                if not isinstance(event, dict) or category and event.get("category") != category:
                    continue
                records.append(event)
                if len(records) >= limit:
                    break
        return records
