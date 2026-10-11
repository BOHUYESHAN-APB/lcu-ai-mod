"""私信（悄悄话）—— 通讯录线程 + 接收识别。

用户口径（原话）：

    「**私信**原版有悄悄话指令 `/msg`…… 后台以**通讯录**形式做，像微信那样左边联系人
      右边对话框」，且「模型可以选择输入输出的时候说悄悄话」。

两条纪律（都是硬约束）：

1. **识别不出则退化为普通聊天**（不猜）：只有命中可配的识别规则（默认给中英文的
   原版/常见插件措辞）才算私信；认不出就照旧走普通聊天链路。
2. **私信 = 定向** → 在回复门里走**硬旁路**（等同被 @），但仍受层级策略约束
   （公开服 guest 默认不回）。识别在 `session` 里做，这里只负责"文本 → 是不是私信"。

线程落盘复用四层记忆的分区口径：`server:<server_key>` + `person:<uuid>` 两层，
**不能串** —— 路径是 `data/contacts/<server_key>/<person_key>.json`，
`person_key` 直接复用 `PersonMemoryStore.key_for`（uuid 优先，没 uuid 才降级到名字）。
每条消息记：方向（in/out）、时间、是否已读、是否已回。
"""

from __future__ import annotations

import copy
import json
import logging
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

from .conversation import sanitize_key, server_key as build_server_key
from .person_memory import PersonMemoryStore

logger = logging.getLogger("contacts")

# 默认落盘根目录：与 `data/conversations/`、`data/people/` 同级。
DEFAULT_CONTACTS_ROOT = Path(__file__).parent.parent / "data" / "contacts"

# 单条文本长度上限（聊天框本来也塞不下更长；过长的当异常输入截断入库）。
MAX_TEXT_LEN = 1000

# 默认识别规则 = **空**。
#
# 判私信的主口径是 Java 侧的结构化字段 `chat_kind`（翻译键判定，与语言/服务器话术无关）
# —— 那套本地化正则口径已作废。这里只留一个**兜底钩子**：老模组不发 `chat_kind`、
# 或者某台服的话术很特殊时，操作者可以自己加规则；没配就一律按公屏处理（保守，不猜）。
DEFAULT_WHISPER_PATTERNS: tuple[str, ...] = ()


def _extra_patterns(value: Any) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple, set)):
        return []
    return [str(item) for item in value if isinstance(item, str) and item.strip()]


def normalize_whisper_config(raw: Any) -> dict[str, Any]:
    """`.local/commands.json` 的 `whisper` 段归一化：用户加的话术**追加**在默认之后。"""
    data = raw if isinstance(raw, dict) else {}
    return {
        "incoming_patterns": list(DEFAULT_WHISPER_PATTERNS) + _extra_patterns(
            data.get("incoming_patterns")),
    }


class WhisperDetector:
    """把一条聊天文本判成私信（不猜：认不出返回 None）。"""

    def __init__(self, config_provider: Optional[Callable[[], dict]] = None):
        self._config_provider = config_provider

    def config(self) -> dict[str, Any]:
        raw: Any = None
        if self._config_provider is not None:
            try:
                raw = self._config_provider()
            except Exception as exc:  # noqa: BLE001
                logger.warning("[Contacts] 读取私信配置失败，用默认规则: %s", exc)
        return normalize_whisper_config(raw)

    def detect(self, text: Any) -> Optional[dict]:
        """命中识别规则 → `{"player", "text"}`；否则 None（调用方按普通聊天处理）。"""
        raw = str(text or "").strip()
        if not raw:
            return None
        for pattern in self.config()["incoming_patterns"]:
            try:
                match = re.match(pattern, raw, re.IGNORECASE)
            except re.error as exc:
                logger.warning("[Contacts] 私信正则非法，已忽略 %r: %s", pattern, exc)
                continue
            if match:
                groups = match.groupdict()
                player = str(groups.get("player") or "").strip()
                if not player:
                    continue
                return {"player": player, "text": str(groups.get("text") or "").strip()}
        return None


class ContactStore:
    """按 `server:<key>` + `person:<uuid>` 两层分区的私信线程（线程安全）。"""

    def __init__(self, root: str | Path = DEFAULT_CONTACTS_ROOT, clock: Callable[[], float] = time.time):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._clock = clock if callable(clock) else time.time
        self._lock = threading.RLock()

    # ── 键 / 路径 ──

    @staticmethod
    def person_key(uuid: Any, name: Any, server: Any = "") -> str:
        """person 层键：**复用** `PersonMemoryStore.key_for`（uuid 优先、名字兜底降级）。"""
        key, _ = PersonMemoryStore.key_for(uuid, name, server)
        return key

    @staticmethod
    def server_slug(server: Any, world_name: Any = "") -> str:
        """server 分区键：与对话记忆同一套算法（server 优先、存档名兜底）。"""
        return sanitize_key(build_server_key(server, world_name))

    def server_dir(self, server_key: str) -> Path:
        return self.root / (sanitize_key(str(server_key or "")) or "unknown")

    def thread_path(self, server_key: str, person_key: str) -> Path:
        return self.server_dir(server_key) / f"{sanitize_key(str(person_key or '')) or 'unknown'}.json"

    # ── 读写 ──

    def _load(self, path: Path) -> dict:
        if not path.exists():
            return {"uuid": "", "name": "", "messages": []}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("[Contacts] 线程损坏，已忽略 %s: %s", path, exc)
            return {"uuid": "", "name": "", "messages": []}
        if not isinstance(data, dict):
            return {"uuid": "", "name": "", "messages": []}
        messages = data.get("messages")
        return {
            "uuid": str(data.get("uuid") or ""),
            "name": str(data.get("name") or ""),
            "messages": [row for row in messages if isinstance(row, dict)]
            if isinstance(messages, list) else [],
        }

    def _save(self, path: Path, data: dict) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as exc:
            logger.warning("[Contacts] 写盘失败 %s: %s", path, exc)

    @staticmethod
    def _clip(text: Any) -> str:
        value = str(text or "")
        return value[:MAX_TEXT_LEN]

    def _append(self, server_key: str, uuid: str, name: str, row: dict) -> dict:
        key = self.person_key(uuid, name, server_key)
        path = self.thread_path(server_key, key)
        with self._lock:
            data = self._load(path)
            if uuid:
                data["uuid"] = str(uuid)
            if name:
                data["name"] = str(name)
            data["messages"].append(row)
            self._save(path, data)
        return {"key": key, **row}

    # ── 记录 ──

    def record_in(self, server_key: str, uuid: Any, name: Any, text: Any,
                  ts: Optional[float] = None) -> dict:
        """记一条**收到**的私信：未读、未回。"""
        row = {"ts": float(ts if ts is not None else self._clock()), "direction": "in",
               "text": self._clip(text), "read": False, "replied": False}
        return self._append(server_key, str(uuid or ""), str(name or ""), row)

    def record_out(self, server_key: str, uuid: Any, name: Any, text: Any,
                   ts: Optional[float] = None) -> dict:
        """记一条**发出**的私信，并把最近尚未回复的来信标记为已回。"""
        key = self.person_key(uuid, name, server_key)
        path = self.thread_path(server_key, key)
        at = float(ts if ts is not None else self._clock())
        with self._lock:
            data = self._load(path)
            for row in reversed(data["messages"]):
                if row.get("direction") == "in" and not row.get("replied"):
                    row["replied"] = True
                    row["replied_ts"] = at
                    break
            if uuid:
                data["uuid"] = str(uuid)
            if name:
                data["name"] = str(name)
            data["messages"].append({"ts": at, "direction": "out", "text": self._clip(text),
                                     "read": True, "replied": True})
            self._save(path, data)
        return {"key": key, "ts": at, "direction": "out", "text": self._clip(text),
                "read": True, "replied": True}

    # ── 查询 ──

    def _threads(self, server_key: str) -> list[dict]:
        """列出该服的线程：`[{"key", "uuid", "name", "messages"}]`（坏的跳过）。"""
        directory = self.server_dir(server_key)
        if not directory.exists():
            return []
        out: list[dict] = []
        for path in sorted(directory.glob("*.json")):
            data = self._load(path)
            out.append({"key": path.stem, "uuid": data["uuid"], "name": data["name"],
                        "messages": data["messages"]})
        return out

    def find_key(self, server_key: str, target: Any) -> Optional[str]:
        """把 uuid / 名字 / 分区键 统一解析成线程键（找不到返回 None）。"""
        wanted = str(target or "").strip()
        if not wanted:
            return None
        candidates = [self.person_key(wanted, "", server_key), sanitize_key(wanted)]
        for thread in self._threads(server_key):
            if thread["key"] in candidates:
                return thread["key"]
            if thread["uuid"] and thread["uuid"].casefold() == wanted.casefold():
                return thread["key"]
            if thread["name"] and thread["name"].casefold() == wanted.casefold():
                return thread["key"]
        return None

    def messages(self, server_key: str, target: Any, limit: int = 50) -> list[dict]:
        """某个联系人在**本服**的往来消息（时间从早到晚）。"""
        key = self.find_key(server_key, target)
        if key is None:
            return []
        for thread in self._threads(server_key):
            if thread["key"] == key:
                rows = [dict(row) for row in thread["messages"]]
                return rows[-int(limit):] if limit else rows
        return []

    def contacts(self, server_key: str, tier_of: Optional[Callable[[str], str]] = None,
                 online_names: Any = None) -> list[dict]:
        """通讯录：每个联系人的名字 / uuid / 层级 / 在线 / 未读 / 未回 / 最后一条。

        `online_names` 由调用方给（当前在世界的玩家名），不认识就是空 —— **不猜**在线状态。
        """
        online = {str(name or "").strip().casefold()
                  for name in (online_names or []) if str(name or "").strip()}
        rows: list[dict] = []
        for thread in self._threads(server_key):
            messages = thread["messages"]
            if not messages:
                continue
            name = thread["name"] or ""
            last = messages[-1]
            tier = ""
            if callable(tier_of) and name:
                try:
                    tier = str(tier_of(name) or "")
                except Exception as exc:  # noqa: BLE001
                    logger.debug("[Contacts] tier 查询失败 %s: %s", name, exc)
            rows.append({
                "uuid": thread["uuid"],
                "key": thread["key"],
                "name": name,
                "tier": tier,
                "online": name.casefold() in online if online else False,
                "unread": sum(1 for row in messages
                              if row.get("direction") == "in" and not row.get("read")),
                "unreplied": sum(1 for row in messages
                                 if row.get("direction") == "in" and not row.get("replied")),
                "count": len(messages),
                "last_ts": float(last.get("ts") or 0.0),
                "last_direction": str(last.get("direction") or ""),
                "last_text": str(last.get("text") or ""),
            })
        rows.sort(key=lambda row: -row["last_ts"])
        return rows

    def mark_read(self, server_key: str, target: Any) -> int:
        """把某人的来信全部标为已读；返回改动条数。"""
        key = self.find_key(server_key, target)
        if key is None:
            return 0
        path = self.thread_path(server_key, key)
        with self._lock:
            data = self._load(path)
            changed = 0
            for row in data["messages"]:
                if row.get("direction") == "in" and not row.get("read"):
                    row["read"] = True
                    changed += 1
            if changed:
                self._save(path, data)
            return changed

    # ── 分区切换（订阅/回读用）──

    def use_namespace(self, server: Any, world_name: Any = "") -> str:
        return self.server_slug(server, world_name)

    def export_thread(self, server_key: str, target: Any) -> dict:
        """导出线程（端点回包用；无此联系人时空结构）。"""
        key = self.find_key(server_key, target)
        if key is None:
            return {"uuid": "", "name": "", "key": "", "messages": []}
        for thread in self._threads(server_key):
            if thread["key"] == key:
                return copy.deepcopy(thread)
        return {"uuid": "", "name": "", "key": "", "messages": []}
