"""Conversation Memory — 按服务器分区的三层对话记忆（M3-19 §2）。

设计动机：AI 进出世界、跨存档来回跑时，聊过的内容不能丢；但不同服务器的
"人际印象"也不该串在一起（在 A 服被谁坑过，不该拿去做 B 服的人设判断）。
所以记忆按 **服务器** 分区，而不是按进程寿命：

  L1 原始事件   → data/conversations/<key>.jsonl（append-only，重启可恢复）
  L2 滑动窗口   → 进程内 list，直接喂 prompt；单人/私聊最多 60 条，多人 40 条
  L3 中期摘要   → 窗口裁切出来的旧条目交给 LLM 压成 {summary, keywords}，
                  append 到 <key>.summary.jsonl

关键取舍：**盘上只存原始事件和摘要，不序列化任何运行时状态**。重启后要恢复
上下文，就把 L1 尾部最近 N 条原始事件重新回灌进窗口即可 —— 这样状态机不会
因为忘记写盘而错位（少写一个字段就永久坏掉的那种 bug 在这里不存在）。

死亡/进出的识别见 `classify_system_message`（中英文话术都要认，规则在那儿）。
分区键：`world.server`（服务器 ip）优先，否则 `world.world_name`（单人存档），
否则 `unknown`。同一 key 复用同一份文件，于是"进出世界不丢上下文"。
"""

import json
import logging
import os
import re
import time
from pathlib import Path

logger = logging.getLogger("conversation")


# 分区名兜底：连世界信息都拿不到时也不能不落盘。
UNKNOWN_KEY = "unknown"
# 允许落进 L1 的事件类型；别的（打错的 type）一律降级成普通 chat。
EVENT_TYPES = ("chat", "death", "join", "leave", "system")
# 摘要走独立角色，方便按角色配便宜的小模型，且不与 planner 的 prompt 缓存互相干扰。
SUMMARY_AGENT = "summarizer"


def server_key(server, world_name) -> str:
    """算分区人类的可读键：服务器优先，其次存档名，都没有就 unknown。

    `world.server` 只在连服务器时非空，单人存档通常只有 world_name；两者都做
    strip，全空（刚连上、还没拿到 world 快照）回退 UNKNOWN_KEY 而不是抛异常。
    """
    for value in (server, world_name):
        text = str(value).strip() if value is not None else ""
        if text:
            return text
    return UNKNOWN_KEY


def sanitize_key(key: str) -> str:
    """把分区键转成文件名安全的 slug。

    允许 [A-Za-z0-9._-]，其余（':'、'/'、空格、中文……）一律换成 '_'；连续 '_'
    压成一个、去首尾 '_'，避免出现 `..jsonl`、`/` 导致的路径穿越或空文件名。
    例：'127.0.0.1:25565' -> '127.0.0.1_25565'。空结果回退 UNKNOWN_KEY。
    """
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", str(key or ""))
    cleaned = re.sub(r"_+", "_", cleaned).strip("_")
    return cleaned or UNKNOWN_KEY


# ── 死亡 / 进出判定 ─────────────────────────────────────────────
# 模组的 player_chat 只会给一串文本 + is_system，没有结构化字段，所以只能从
# 话术里抠。规则覆盖原版中英文本地化的常见句式；认不出的就当普通 system，
# 宁可漏判也不能把人聊天误判成死亡（误判会污染 L1，且没法回滚）。

# EN: "<player> joined the game" / "<player> left the game"（原版进服/退服提示）
_EN_JOIN = re.compile(r"^(?P<player>\S+)\s+joined the game\b", re.I)
_EN_LEAVE = re.compile(r"^(?P<player>\S+)\s+left the game\b", re.I)

# EN 死因：被某物所杀。只认原版用过的动词，**不做**通用的 "was X by Y" ——
# 后者会把 "The server was restarted by admin" 之类系统广播误判成死亡。
_EN_DEATH_BY = re.compile(
    r"^(?P<player>\S+)\s+was\s+"
    r"(?:slain|shot|blown up|killed|fireballed|impaled|pummeled|squashed|squished|"
    r"stung to death|poked to death|spitted|struck|frozen to death|doomed to fall|"
    r"flattened|pricked to death|skewered)\s+"
    r"(?:by|with)\s+(?P<cause>.+)$",
    re.I,
)
# EN 特例：死亡信息里 "by <killer>" 被拆到了从句里。
_EN_DEATH_CLAUSE = re.compile(
    r"^(?P<player>\S+)\s+was\s+(?:killed while trying to hurt|"
    r"killed whilst trying to escape|burnt to a crisp whilst fighting)\s+(?P<cause>.+)$",
    re.I,
)
# EN 无凶手被动死亡（淹死/摔死/饿死……）：只有一个固定短语，没有 "by"。
_EN_DEATH_PASSIVE = re.compile(
    r"^(?P<player>\S+)\s+"
    r"(?:drowned|died|starved to death|suffocated in a wall|hit the ground too hard|"
    r"fell from a high place|fell from some high place|fell out of the world|"
    r"fell too far|tried to swim in lava|went up in flames|burned to death|"
    r"burnt to death|froze to death|withered away|experienced kinetic energy|"
    r"blew up|went off with a bang|walked into a cactus|walked into fire|"
    r"discovered the floor was lava)\b",
    re.I,
)

# CN: "<player> 加入了游戏" / "<player> 离开了游戏" / "<player> 退出了游戏"
_CN_JOIN = re.compile(r"^(?P<player>.+?)\s*加入了游戏")
_CN_LEAVE = re.compile(r"^(?P<player>.+?)\s*(?:离开了游戏|退出了游戏)")
# CN 被某物所杀："<player> 被僵尸杀死了" / 被雷劈死 / 被骷髅射死。
_CN_DEATH_BY = re.compile(
    r"^(?P<player>.+?)\s*被(?P<cause>.+?)"
    r"(?:杀死|杀害|击杀|射杀|射死|砍死|炸死|打死|劈死|烧死|毒死|刺死|砸死)(?:了)?[。！!\s]*$"
)
# CN 无凶手被动死亡（淹死/摔死/饿死/岩浆/普通死亡）。
_CN_DEATH_PASSIVE = re.compile(
    r"^(?P<player>.+?)\s*(?:淹死|摔死|烧死|饿死|冻死|窒息而死|窒息|"
    r"掉进了岩浆|掉进岩浆|掉入了岩浆|掉入岩浆|落入了岩浆|落入岩浆|"
    r"死了|死掉了|死亡|阵亡)(?:了)?[。！!\s]*$"
)


def classify_system_message(text: str) -> dict | None:
    """判定一条 system 型 player_chat 文本是不是死亡/进服/退服公告。

    规则（全部锚定首尾，避免"玩家聊天里提到某词"被误判）：

    - join  : EN `'<player> joined the game'`；CN `'<player> 加入了游戏'`。
    - leave : EN `'<player> left the game'`；CN `'<player> 离开了游戏'` /
              `'<player> 退出了游戏'`。
    - death : EN `'<player> was slain by Zombie'` / `'was shot by Skeleton'` /
              `'drowned'` / `'fell from a high place'`；
              CN `'<player> 被僵尸杀死了'` / `'被雷劈死了'` / `'淹死了'` / `'死了'`。
              带凶手的填 cause，无凶手的 cause 留空字符串。

    玩家名允许中文/数字/下划线（EN 用首个非空白 token，CN 用"被/动词"之前的整段），
    因此 `'张三_99 被僵尸杀死了'` 也能正确切出 `'张三_99'`。

    返回 `{'type': 'death'|'join'|'leave', 'player': str, 'cause': str, 'text': str}`；
    认不出返回 None —— 调用方按普通 system 处理（绝不臆测）。
    """
    if not text:
        return None
    raw = str(text)
    stripped = raw.strip()
    if not stripped:
        return None

    for pattern, kind in ((_EN_JOIN, "join"), (_CN_JOIN, "join"),
                          (_EN_LEAVE, "leave"), (_CN_LEAVE, "leave")):
        match = pattern.match(stripped)
        if match:
            player = match.group("player").strip()
            if player:
                return {"type": kind, "player": player, "cause": "", "text": raw}

    for pattern in (_EN_DEATH_BY, _EN_DEATH_CLAUSE, _CN_DEATH_BY):
        match = pattern.match(stripped)
        if match:
            player = match.group("player").strip()
            cause = match.group("cause").strip()
            if player:
                return {"type": "death", "player": player, "cause": cause, "text": raw}

    for pattern in (_EN_DEATH_PASSIVE, _CN_DEATH_PASSIVE):
        match = pattern.match(stripped)
        if match:
            player = match.group("player").strip()
            if player:
                return {"type": "death", "player": player, "cause": "", "text": raw}

    return None


class ConversationStore:
    """一个进程内的对话记忆管理器，可切换服务器分区。

    线程安全说明：本类假定由单一线程（后端事件循环）调用，内部不做加锁；
    LLM 调用可能慢，调用方自己决定要不要放到线程池里跑。
    """

    def __init__(self, root="data/conversations", llm=None,
                 single_window=60, multi_window=40, clock=time.time):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        # 摘要用的 LLM（可后注入；没有就保留 pending 不摘要，绝不抛）。
        self.llm = llm
        self.single_window = int(single_window)
        self.multi_window = int(multi_window)
        # 注入时钟便于测试；生产直接用 time.time。
        self.clock = clock

        self.namespace: str = ""
        self._window: list[dict] = []
        self._pending: list[dict] = []
        self._summary: dict = {}
        # 本分区「激活以来」记了多少条 / 攒了几条摘要；进 /api/status 用。
        self._events_logged = 0
        self._summaries = 0

    # ── 路径 / 分区 ──

    @property
    def events_path(self) -> Path:
        """L1 原始事件文件（按分区命名）。"""
        return self.root / f"{self.namespace or UNKNOWN_KEY}.jsonl"

    @property
    def summary_path(self) -> Path:
        """L3 摘要文件（一行一条摘要）。"""
        return self.root / f"{self.namespace or UNKNOWN_KEY}.summary.jsonl"

    def use_namespace(self, server, world_name) -> str:
        """切换到该服务器分区，并从 L1 回灌最近若干条原始事件重建窗口。

        切换会清空运行时窗口 / pending，再从盘上重建 —— 这就是"重启不丢上下文、
        进出世界上下文跟着走"的全部机制：同一 key 落到同一份 jsonl。
        同一分区重复调用直接返回，不做第二次回灌（否则窗口会被重复灌满）。
        """
        key = sanitize_key(server_key(server, world_name))
        if key == self.namespace:
            return key

        self.namespace = key
        self._window = []
        self._pending = []
        self._summary = {}
        self._events_logged = 0
        self._summaries = 0

        # 回灌上限取两个窗口里较大的那个：无论下次按单人还是多人算口径，
        # 窗口一开始都是满的（后续 record 会按实际口径继续裁切）。
        for entry in self._load_recent(max(self.single_window, self.multi_window)):
            self._window.append(entry)

        summaries = self._load_summaries()
        self._summaries = len(summaries)
        if summaries:
            self._summary = summaries[-1]
        return key

    # ── 记录 ──

    def record(self, event: dict) -> dict | None:
        """规范化一条事件：补 ts/type/sender/message，写 L1，推进 L2 窗口。

        ts 缺省用注入的时钟；只有非 None 的坐标/距离/死因才写进 JSONL（避免塞一堆
        null 让文件又大又难读）。窗口溢出时被挤出的条目进 pending 摘要队列。
        任何异常都吞掉只 warning —— 后端主循环不能因为一次落盘失败就挂掉。
        """
        try:
            if not isinstance(event, dict):
                return None
            if not self.namespace:
                # 没显式切分区（例如裸调用）时兜底到 unknown，总比丢数据强。
                self.use_namespace("", "")

            entry: dict = {"ts": event.get("ts") or self.clock()}
            etype = str(event.get("type") or "chat")
            entry["type"] = etype if etype in EVENT_TYPES else "chat"
            entry["sender"] = str(event.get("sender") or "")
            entry["message"] = str(event.get("message") or "")
            for key in ("x", "y", "z", "distance", "cause"):
                value = event.get(key)
                if value is not None:
                    entry[key] = value

            self._append(self.events_path, entry)
            self._events_logged += 1
            self._push_window(entry)
            return entry
        except Exception as exc:  # 兜底：record 永远不能让调用方炸掉
            logger.warning("[Conversation] 记录事件失败: %s", exc)
            return None

    def _push_window(self, entry: dict) -> None:
        """把事件压进 L2 窗口；超限就从头部挤出并丢进 pending。"""
        self._window.append(dict(entry))
        limit = self.window_limit()
        while len(self._window) > limit:
            self._pending.append(self._window.pop(0))

    def record_chat(self, sender, message, *, is_system=False, x=None, y=None, z=None,
                    distance=None, ts=None) -> dict | None:
        """聊天事件入口。

        is_system=True 时先过 `classify_system_message`：认出 death/join/leave 就
        落成对应类型（并让玩家名当 sender，因为"谁死了/谁进服"是事件主体），
        认不出才落成普通 system。普通玩家聊天（is_system=False）原样落 chat。
        """
        event: dict = {"type": "chat", "sender": sender, "message": message}
        for key, value in (("x", x), ("y", y), ("z", z), ("distance", distance), ("ts", ts)):
            if value is not None:
                event[key] = value

        if is_system:
            classified = classify_system_message(message)
            if classified:
                event["type"] = classified["type"]
                if classified.get("player"):
                    event["sender"] = classified["player"]
                if classified.get("cause"):
                    event["cause"] = classified["cause"]
            else:
                event["type"] = "system"

        return self.record(event)

    def record_death(self, player="", cause="", text="") -> dict | None:
        """显式记录一次死亡（由后端从事件流判定后直接调用时的入口）。"""
        event: dict = {"type": "death", "sender": player, "message": text}
        if cause:
            event["cause"] = cause
        return self.record(event)

    def record_system(self, text="", sender="", event_type="system", **extra) -> dict | None:
        """通用系统事件入口；event_type 非法时降级成 system，extra 里可带坐标等。"""
        etype = event_type if event_type in EVENT_TYPES else "system"
        event: dict = {"type": etype, "sender": sender, "message": text}
        event.update(extra)
        return self.record(event)

    # ── 窗口 / 查询 ──

    def window_limit(self, players_count=1) -> int:
        """按在场人数选窗口口径：≥2 人算多人（40），否则单人/私聊（60）。"""
        return self.multi_window if players_count >= 2 else self.single_window

    def recent(self, n=None, players_count=1) -> list[dict]:
        """窗口内最近 n 条（缺省整窗），时间从早到晚。返回浅拷贝，防外部改内存。"""
        window = self._window[-self.window_limit(players_count):]
        if n is None:
            return [dict(entry) for entry in window]
        if n <= 0:
            return []
        return [dict(entry) for entry in window[-n:]]

    def pending_summary(self) -> list[dict]:
        """当前待摘要（已被窗口挤出、还没压成 L3）的条目。"""
        return [dict(entry) for entry in self._pending]

    def latest_summary(self) -> dict:
        """最近一条 L3 摘要：`{'summary','keywords','ts'}`；没有则 `{}`。"""
        if not self._summary:
            return {}
        return {
            "summary": self._summary.get("summary", ""),
            "keywords": list(self._summary.get("keywords") or []),
            "ts": self._summary.get("ts"),
        }

    def describe(self, players_count=1) -> dict:
        """喂 prompt 的结构：分区名 + 中期摘要/关键词 + 最近窗口。"""
        summary = self.latest_summary()
        return {
            "namespace": self.namespace,
            "summary": summary.get("summary", ""),
            "keywords": summary.get("keywords", []),
            "recent": self.recent(players_count=players_count),
        }

    def status(self) -> dict:
        """供 /api/status：分区窗口与落盘概况。

        `events_logged`/`summaries` 是本分区「激活以来」的计数（不是历史总量，
        统计历史总量要整文件扫描，不值得）。
        """
        return {
            "namespace": self.namespace,
            "window": len(self._window),
            "window_limit": self.window_limit(),
            "events_logged": self._events_logged,
            "summaries": self._summaries,
            "last_summary": self.latest_summary(),
        }

    # ── 摘要 ──

    def summarize_pending(self, llm=None) -> dict | None:
        """把 pending 交给 LLM 压成 `{summary, keywords}` 并 append 到摘要文件。

        成功才清空 pending；LLM 报错 / 返回解析不出 JSON 都返回 None 并**保留**
        pending（下次窗口再裁切时会并进来一起摘要），绝不向外抛。
        """
        if not self._pending:
            return None
        client = llm or self.llm
        if client is None:
            # 没有 LLM 就别硬编码假摘要 —— 留着 pending 等有模型再压。
            return None

        items = list(self._pending)
        prompt = self._build_summary_prompt(items)
        try:
            result = client.chat(
                [{"role": "user", "content": prompt}],
                agent=SUMMARY_AGENT, temperature=0.2, max_tokens=200,
                thinking=False, timeout=20.0,
            )
            content = (result or {}).get("content") or ""
            data = self._parse_summary(content)
            if not isinstance(data, dict):
                return None

            summary = str(data.get("summary") or "").strip()
            keywords = self._normalize_keywords(data.get("keywords"))
            if not summary and not keywords:
                return None

            stored = {
                "summary": summary,
                "keywords": keywords,
                "ts": self.clock(),
                "count": len(items),
            }
            self._append(self.summary_path, stored)
            self._pending = []
            self._summary = {"summary": summary, "keywords": keywords, "ts": stored["ts"]}
            self._summaries += 1
            return {"summary": summary, "keywords": keywords}
        except Exception as exc:
            logger.warning("[Conversation] 摘要失败，保留 pending: %s", exc)
            return None

    @staticmethod
    def _build_summary_prompt(items: list[dict]) -> str:
        """把被裁切的原始事件拼成一段中文提示，要求只吐 JSON。"""
        lines = []
        for entry in items:
            etype = entry.get("type", "?")
            who = entry.get("sender") or "?"
            msg = entry.get("message") or ""
            extra = f"（{entry['cause']}）" if entry.get("cause") else ""
            lines.append(f"[{etype}] {who}: {msg}{extra}")
        body = "\n".join(lines)
        return (
            "以下是一段已经滑出短期窗口的 Minecraft 多人服对话记录。"
            "请用一两句中文概括其中的关键信息（谁和谁发生了什么、气氛如何）。\n"
            "只输出 JSON，不要解释：{\"summary\": \"...\", \"keywords\": [\"...\"]}\n\n"
            f"{body}"
        )

    @staticmethod
    def _parse_summary(content) -> dict | None:
        """从 LLM 回复里抠 JSON：允许 ```json 包裹或前后有解释文字。"""
        if not content:
            return None
        text = str(content)
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end <= start:
            return None
        try:
            data = json.loads(text[start:end + 1])
        except Exception:
            return None
        return data if isinstance(data, dict) else None

    @staticmethod
    def _normalize_keywords(value) -> list[str]:
        """关键词归一化成字符串列表（LLM 可能给字符串或带空项的列表）。"""
        if value is None:
            return []
        if isinstance(value, str):
            parts = re.split(r"[,，、;；\s]+", value)
        elif isinstance(value, (list, tuple, set)):
            parts = [str(item) for item in value]
        else:
            parts = [str(value)]
        out = []
        for part in parts:
            word = str(part).strip()
            if word and word not in out:
                out.append(word)
        return out[:12]

    # ── 持久化 ──

    def _append(self, path: Path, record: dict) -> None:
        """append 一行 JSON（ensure_ascii=False，中文别变成 \\uXXXX）。失败只 warning。"""
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception as exc:
            logger.warning("[Conversation] 落盘失败 %s: %s", path, exc)

    def _load_recent(self, limit: int) -> list[dict]:
        """从 L1 文件尾读取最近 limit 条有效事件（坏行/半行直接跳过）。"""
        events = []
        for line in self._tail_lines(self.events_path, limit):
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except Exception:
                continue  # 容错：崩过一半的写入不该让整个回灌失败
            if isinstance(entry, dict):
                events.append(entry)
        return events

    def _load_summaries(self) -> list[dict]:
        """读取该分区全部 L3 摘要（按落盘顺序，最后一条最新）。摘要文件很小，整读。"""
        out = []
        path = self.summary_path
        if not path.exists():
            return out
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except Exception:
                    continue
                if isinstance(data, dict):
                    out.append(data)
        except Exception as exc:
            logger.warning("[Conversation] 载入摘要失败 %s: %s", path, exc)
        return out

    @staticmethod
    def _tail_lines(path: Path, limit: int) -> list[str]:
        """从文件尾往回读最多 limit 行，避免日志长了以后每次切换分区都整读。

        读到的首行可能被从中间截断（按 4096 字节块回溯），若文件没从头读全就
        丢掉首行 —— 调用方本来就靠 JSON 解析失败兜底，这里再省一层。
        """
        if limit <= 0 or not path.exists():
            return []
        try:
            with path.open("rb") as handle:
                handle.seek(0, os.SEEK_END)
                pos = handle.tell()
                chunk = b""
                # 留够 limit+1 个换行，保证切掉可能的半行后仍有 limit 条完整行。
                while pos > 0 and chunk.count(b"\n") <= limit:
                    step = min(4096, pos)
                    pos -= step
                    handle.seek(pos)
                    chunk = handle.read(step) + chunk
            text = chunk.decode("utf-8", errors="replace")
        except Exception as exc:
            logger.warning("[Conversation] 读取失败 %s: %s", path, exc)
            return []

        lines = text.splitlines()
        if pos > 0 and lines:
            lines = lines[1:]
        return lines[-limit:]
