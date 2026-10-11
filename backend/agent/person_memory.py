"""Person 层记忆 —— 跨服务器的「同一个人」稳定事实（uuid 优先，名字兜底 + 降级打标）。

四层口径（用户："每个各个层级有对应的记忆系统，对应的内容不能串"）：

    global                                          人设/规则/回复策略/权限（跨服通用）
    person:<uuid>                                   ★ 同一个人：身份与稳定事实（**可以跨服串一点点**）
    server:<server_key>                             对话记忆、服务器内的社交上下文
    world:<server_key>/<world_name>/<dimension>     站点/路径/POI/容器内容（坐标只在同世界同维度有效）

这一层**允许跨服务器复用** —— 同一个 uuid 在哪台服都是同一个人。但**必须有边界**：
只存关于这个人的**稳定事实/偏好**（是不是我的主人、他对我/对自己的称呼、他给我起的名字、
他教过的通用规则）。**绝不**存服务器内的一次性上下文（刚才聊了什么、在哪遇到的、和谁在一起）
—— 那些属于 server 层；提升到这一层就会串，正是用户最担心的那种串。

边界不是靠"记得别写"，而是靠 `STABLE_FACT_KEYS` 白名单：别的键**写不进来**。

识别：**uuid 优先、名字兜底**（同名不同人是常事）。没有 uuid 时按 `server+name` 落盘，
并在文件里 `degraded: true` 打标 —— 降级键只在一台服里有效，不假装是"同一个人"。
"""

from __future__ import annotations

import copy
import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Optional

from .conversation import sanitize_key

logger = logging.getLogger("person_memory")

# 落盘根目录：与 `data/conversations/`、`data/rules/`、`data/poi/` 同级；**按人**一件，不按服。
DEFAULT_PEOPLE_ROOT = Path(__file__).parent.parent / "data" / "people"

UNKNOWN = "unknown"

# 允许进 person 层的**稳定事实**键（白名单即边界）。
# 服务器内的一次性上下文（chat / last_message / position / where / together / dimension /
# server / nearby / seen_at）刻意**不在**这里 → 写不进来，也就串不到别的服。
STABLE_FACT_KEYS = frozenset({
    "is_master",      # 是不是我的主人
    "role",           # 身份：管理员 / 朋友 / 陌生人……
    "calls_me",       # 他管我叫什么
    "bot_name",       # 他给我起的名字
    "nickname",       # 我该怎么称呼他
    "language",       # 他说什么语言
    "prefers",        # 偏好
    "rules",          # 他教过的**通用**规则
    "notes",          # 其它稳定备注
})


class PersonMemoryStore:
    """四层记忆里的 person 层：按人落盘的稳定事实（读写线程安全，uuid 优先）。"""

    def __init__(self, root: str | Path = DEFAULT_PEOPLE_ROOT, clock=time.time):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._clock = clock
        self._lock = threading.RLock()

    # ── 键 / 路径 ──

    @staticmethod
    def key_for(uuid: Any, name: Any, server_key: Any = "") -> tuple[str, bool]:
        """算出这个人的分区键 → `(key, degraded)`。

        uuid 优先（`uuid:<sanitized>`，跨服同一件）；没有 uuid → `name:<server>__<name>`
        并 `degraded=True`（同名不同人是常事，降级键不跨服合并）。
        """
        uuid_text = str(uuid or "").strip()
        if uuid_text:
            return f"uuid_{sanitize_key(uuid_text)}", False
        name_text = str(name or "").strip()
        if name_text:
            server = sanitize_key(str(server_key or "").strip() or UNKNOWN)
            return f"name_{server}__{sanitize_key(name_text)}", True
        return UNKNOWN, True

    @property
    def namespace(self) -> str:
        """person 层没有"当前分区"（按人取件），这里给个稳定标识供调试。"""
        return "person"

    def path(self, key: str) -> Path:
        return self.root / f"{key}.json"

    # ── 读 ──

    def entry(self, uuid: Any = "", name: Any = "", server_key: Any = "") -> Optional[dict]:
        key, _ = self.key_for(uuid, name, server_key)
        if key == UNKNOWN:
            return None
        return self._read(key)

    def facts(self, uuid: Any = "", name: Any = "", server_key: Any = "") -> dict:
        """这个人的稳定事实（没有记录就是空 dict）。"""
        entry = self.entry(uuid, name, server_key)
        return dict(entry.get("facts") or {}) if isinstance(entry, dict) else {}

    def describe(self, uuid: Any = "", name: Any = "", server_key: Any = "") -> str:
        """给 prompt 的一句话（没有记录 → 空串）。"""
        entry = self.entry(uuid, name, server_key)
        if not isinstance(entry, dict):
            return ""
        facts = entry.get("facts") or {}
        if not facts:
            return ""
        who = str(entry.get("name") or name or "他")
        bits = ", ".join(f"{k}={v}" for k, v in sorted(facts.items()))
        suffix = "（按名字认的，可能不是同一个人）" if entry.get("degraded") else ""
        return f"{who}: {bits}{suffix}"

    def known(self) -> list[dict]:
        """所有已记下的人（供 `/api/status`；按最近更新排序）。"""
        rows: list[dict] = []
        try:
            paths = sorted(self.root.glob("*.json"))
        except OSError:
            return rows
        for path in paths:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if isinstance(data, dict) and data.get("key"):
                rows.append(data)
        rows.sort(key=lambda row: float(row.get("updated_at") or 0.0), reverse=True)
        return rows

    # ── 写 ──

    def identify(self, uuid: Any, name: Any, server_key: Any = "") -> Optional[dict]:
        """记下 uuid↔名字绑定（这本身就是一条稳定事实）；没有 uuid 就落降级键并打标。"""
        key, degraded = self.key_for(uuid, name, server_key)
        if key == UNKNOWN:
            return None
        uuid_text, name_text = str(uuid or "").strip(), str(name or "").strip()
        with self._lock:
            entry = self._read(key) or self._blank(key, uuid_text, name_text, degraded)
            changed = not self.path(key).exists()
            if name_text and entry.get("name") != name_text:
                entry["name"] = name_text
                changed = True
            if uuid_text and entry.get("uuid") != uuid_text:
                entry["uuid"] = uuid_text
                changed = True
            if changed:
                entry["updated_at"] = float(self._clock())
                self._write(key, entry)
            return copy.deepcopy(entry)

    def remember_fact(self, uuid: Any, name: Any, fact: Any, value: Any,
                      server_key: Any = "") -> tuple[bool, str]:
        """记一条**稳定事实** → `(ok, reason)`。

        非白名单键（`chat`/`last_message`/`position`/`where`/`server`……）一律拒绝，
        理由 `not_stable`：这条边界挡的就是"把服务器内的一次性上下文提升到 person 层"。
        """
        fact_key = str(fact or "").strip()
        if fact_key not in STABLE_FACT_KEYS:
            return False, "not_stable"
        key, degraded = self.key_for(uuid, name, server_key)
        if key == UNKNOWN:
            return False, "no_identity"
        with self._lock:
            entry = self._read(key) or self._blank(key, uuid, name, degraded)
            facts = entry.setdefault("facts", {})
            if facts.get(fact_key) == value:
                return True, "unchanged"
            facts[fact_key] = value
            if name and not entry.get("name"):
                entry["name"] = str(name)
            entry["updated_at"] = float(self._clock())
            self._write(key, entry)
        return True, "saved"

    def remember_facts(self, uuid: Any, name: Any, facts: Any,
                       server_key: Any = "") -> dict:
        """批量记稳定事实 → `{"saved": [...], "refused": {key: reason}}`。"""
        saved, refused = [], {}
        for fact, value in (facts or {}).items():
            ok, reason = self.remember_fact(uuid, name, fact, value, server_key=server_key)
            if ok and reason == "saved":
                saved.append(str(fact))
            elif not ok:
                refused[str(fact)] = reason
        return {"saved": saved, "refused": refused}

    def forget(self, uuid: Any = "", name: Any = "", server_key: Any = "") -> bool:
        """删掉这个人的 person 记录（只影响这一层）。"""
        key, _ = self.key_for(uuid, name, server_key)
        if key == UNKNOWN:
            return False
        with self._lock:
            path = self.path(key)
            if not path.exists():
                return False
            try:
                path.unlink()
            except OSError as exc:
                logger.warning("[PersonMemory] 删除失败 %s: %s", path, exc)
                return False
        return True

    # ── 盘 ──

    @staticmethod
    def _blank(key: str, uuid: str, name: str, degraded: bool) -> dict:
        return {
            "version": 1,
            "key": key,
            "uuid": str(uuid or ""),
            "name": str(name or ""),
            "degraded": bool(degraded),
            "facts": {},
            "updated_at": 0.0,
        }

    def _read(self, key: str) -> Optional[dict]:
        path = self.path(key)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("[PersonMemory] 读取失败 %s: %s", path, exc)
            return None
        if not isinstance(data, dict):
            return None
        data.setdefault("version", 1)
        data.setdefault("key", key)
        facts = data.get("facts")
        data["facts"] = facts if isinstance(facts, dict) else {}
        return data

    def _write(self, key: str, entry: dict) -> None:
        path = self.path(key)
        temporary = path.with_suffix(path.suffix + ".tmp")
        try:
            temporary.write_text(json.dumps(entry, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(path)
        except Exception as exc:
            logger.warning("[PersonMemory] 写盘失败 %s: %s", path, exc)
