"""
TCP JSONL wire protocol client.
Connects to the LCUMod's wire server and sends/receives JSON messages.

Usage:
    wire = WireClient("127.0.0.1", 27586)
    wire.connect()
    wire.send_command("move_to", {"x": 100, "y": 64, "z": -200})
    for msg in wire.events():
        print(msg)
"""

import collections
import hashlib
import json
import math
import socket
import threading
import time
import logging
import os
from pathlib import Path
from queue import Queue
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger("wire")


# ── 周期性入站事件的"变化才打行"日志 ───────────────────────────────
# 背景（用户实机口径）：`← event: behavior_state` / `← event: control_state` 这类
# **周期上报、内容却基本不变**的事件会每秒刷好几行，把真正要看的 `→ command` /
# 错误 / 决策全淹掉。这里只对"周期性状态"事件做去重，一共三道闸：
#   ① 内容没变 ⇒ 不打行（只 +1 抑制计数）；
#   ② **抖动不算变化**（`_JITTER_TOLERANCE`：`afk.drift`、最近样本距离的格内来回）；
#   ③ **同类事件两行至少隔 `PERIODIC_MIN_INTERVAL_SECONDS`**（默认 5s），间隔内的
#      变化被合并但**不丢** —— 下一行如实交代"（期间另有 N 条变化被合并）"。
# **绝不静音关键信息**：命令、回执、错误、决策、聊天一律不走这条路（见 `is_periodic_event`）。
PERIODIC_EVENT_NAMES = frozenset({
    "state_update", "behavior_state", "control_state", "task_state",
})

# 参与去重键的字段里"每次都变"的时间/序号/计时类字段：不剔除它们就永远判为"变了"。
# 注意**只剔除时间与计数器**；业务计数字段（count/progress/health…）一律保留。
_VOLATILE_KEYS = frozenset({
    "ts", "timestamp", "time", "now", "server_time", "sent_at", "received_at",
    "day_time", "game_time", "world_time", "uptime", "uptime_ms",
    "tick", "ticks", "seq", "seq_id", "sequence", "frame", "elapsed",
    # 朝向：转 1 度不该算"状态变了"（用户实机：日志被这类抖动刷屏）。模型照旧拿得到 yaw/pitch，
    # 只是**日志视图**不看它们。
    "yaw", "pitch", "head_yaw", "body_yaw", "rotation",
})
_VOLATILE_SUFFIXES = ("_ms", "_at", "_ts", "_tick", "_ticks")

# 单行里回显"新内容"的最大长度；超出用 `…(+N 字)` 诚实标注（不隐藏"被截断"这件事）。
MAX_LOG_CONTENT_CHARS = 400

# **同类**周期事件两行之间的最小间隔（秒）。用户实机：`state_update` 每 0.5 秒一行、
# 内容几乎不变，一屏就被它刷满 —— 光靠"变了才打"还不够，因为一两处抖动也够触发打行。
# 这里再兜一层：同一类周期事件**最多**每 N 秒打一行；间隔内发生的变化**不丢**，
# 到点的那一行会如实带上"（期间另有 N 条变化被合并）"。0 = 关（回到"变了就打"）。
# 可配置：`config.json` → `log_min_interval_seconds`（见 `configure_log_dedupe`）。
PERIODIC_MIN_INTERVAL_SECONDS = 5.0

# 抖动的**忽略阈值**表（只作用于**指纹**，打印内容照旧带原值）。
# 用户实机：`afk.drift` 每帧浮点、最近一条样本的**距离**在 1 格内来回 —— 这类"抖动"
# **单独**出现时不构成"内容变了"，不该触发打行。
# 粗化方式 = "落到第几格"（`floor(v / tol)`，见 `_coarsen_jitter`）：同一格内的来回
# 指纹不变；跨格（真挪出去了）才算变化。信息不丢：真实数值仍在打印内容里。
_JITTER_TOLERANCE = {
    "drift": 1.0,      # afk.drift：每帧浮点，±1 以内不算状态变化（用户实机刷屏源）
    "distance": 1.0,   # 最近一条样本的距离：1 格内的来回不算变化（与"取整到格"同口径）
}


def _is_volatile_key(key: str) -> bool:
    lowered = str(key).lower()
    return lowered in _VOLATILE_KEYS or lowered.endswith(_VOLATILE_SUFFIXES)


def _normalize_for_key(value: Any) -> Any:
    """递归剔除易变字段（字典键序无关由后续 `sort_keys` 保证）。"""
    if isinstance(value, dict):
        return {k: _normalize_for_key(v) for k, v in value.items() if not _is_volatile_key(k)}
    if isinstance(value, list):
        return [_normalize_for_key(v) for v in value]
    return value


def event_fingerprint(obj: dict) -> str:
    """周期性事件的去重指纹 = **打印内容的哈希**（与 `format_event_content` 共用 `_log_view`）。

    以前指纹走**完整负载**：目标挪 0.05 格、视角转 1 度都算"变了"，于是"变化才打"退化成
    每分钟好几行、每行上千字坐标（用户实机："后台频繁在刷这些坐标位置，我们得限定"）。

    现在指纹 = `_log_view`（打印视图）**再按抖动阈值粗化**（`_JITTER_TOLERANCE`）：
    `afk.drift`、最近样本的 `distance` 这类每帧浮点**单独**变化不再算"内容变了"。
    打印内容仍走 `_log_view`（原值），所以"判变化时看的东西"和"打出来的东西"只在
    **抖动幅度内**允许不一致 —— 真跨过阈值时两者一起变，不会自相矛盾。
    """
    blob = json.dumps(_fingerprint_view(obj), ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), default=str)
    return hashlib.sha1(blob.encode("utf-8", "replace")).hexdigest()


def is_periodic_event(msg_type: str, label: Any) -> bool:
    """该入站消息是否为"周期上报的状态"事件（唯一允许做变化去重的类别）。"""
    if msg_type != "event":
        return False
    name = str(label or "")
    # 已知的四类 + 任何 `<something>_state` 快照（同类一并处理）。
    return name in PERIODIC_EVENT_NAMES or name.endswith("_state")


# ── 周期状态摘要（**只影响打印内容**；指纹仍走完整负载 `event_fingerprint`）──────────
# 用户实机口径："后端传递了好多坐标位的位置" —— `state_update` 这类负载里 `nearby_blocks`
# （32 条 × xyz）、`entities`/`players`（× xyz）、`nearby_storage`/`nearby_beds`/
# `remembered_pois`/`stations`/`routes`… 全带坐标，整包逐字打出来就是满屏坐标。这里把
# **打印内容**换成紧凑摘要：标量原样（位置/血量/饥饿/维度…），数组只留**条数**，
# 空间类数组（方块/实体/玩家/站点…）再附**距离最近的一条**作样本。判"变没变"仍用完整负载，
# 所以"到底变没变"照样能答，"变了什么"看摘要里的关键字段与条数。
_SAMPLE_LIST_KEYS = frozenset({
    "nearby_blocks", "nearby_workstations", "nearby_storage", "nearby_beds",
    "entities", "players", "remembered_pois", "known_containers",
    "stations", "routes",
})

# 样本里**只保留**这些字段：身份 + 距离 + 血量/数量。坐标（x/y/z/uuid…）不进日志视图 ——
# 那是用户点名要压掉的东西，而模型照旧从完整负载里拿得到。
_SAMPLE_KEEP = frozenset({
    "name", "block_id", "item_id", "display", "type", "kind",
    "distance", "health", "count", "id",
})


def _nearest_sample(items: list) -> Any:
    """数组里距离最近的一条作样本：带 distance 的取最小，否则第一条 dict、再退到首个非 dict。

    **只留身份 + 距离**（`_SAMPLE_KEEP`）：坐标/血量之类逐字打出来就是用户说的"刷坐标"，
    而模型那边照旧拿得到完整负载 —— 日志只需要回答"最近的那个是什么、多远"。
    """
    best = None
    best_distance = None
    for item in items:
        if not isinstance(item, dict):
            continue
        distance = item.get("distance")
        if isinstance(distance, (int, float)):
            if best is None or distance < best_distance:
                best, best_distance = item, distance
        elif best is None:
            best = item
    if best is None:
        for item in items:
            if not isinstance(item, dict):
                return item
        return None
    return {k: v for k, v in best.items() if k in _SAMPLE_KEEP}


def summarize_periodic_payload(payload: Any) -> Any:
    """周期状态负载的**打印摘要**（不改指纹）：数组 → 条数（空间类附最近一条样本），标量原样。"""
    if not isinstance(payload, dict):
        return payload
    summary: dict[str, Any] = {}
    for key, value in payload.items():
        if isinstance(value, list):
            entry: dict[str, Any] = {"count": len(value)}
            if key in _SAMPLE_LIST_KEYS:
                sample = _nearest_sample(value)
                if sample is not None:
                    entry["nearest"] = sample
            summary[key] = entry
        else:
            summary[key] = value
    return summary


# 空间/数值标量在**日志视图**里的取整位数（0 = 取整到格）：避免"挪了 0.05 格"被当成"变化"。
# 只影响日志（指纹 + 打印），发给模型的数据一字不动。
_QUANTIZE_DECIMALS = {
    "x": 0, "y": 0, "z": 0, "distance": 0,
    "health": 1, "max_health": 1, "saturation": 1, "fall_distance": 1,
}


def _quantize(value: Any) -> Any:
    """按 `_QUANTIZE_DECIMALS` 递归取整（**只影响日志视图**，不动发给模型的数据）。"""
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            decimals = _QUANTIZE_DECIMALS.get(str(key).lower())
            if decimals is not None and isinstance(item, (int, float)) and not isinstance(item, bool):
                out[key] = round(float(item), decimals)
            else:
                out[key] = _quantize(item)
        return out
    if isinstance(value, list):
        return [_quantize(v) for v in value]
    return value


def _log_view(obj: Any) -> Any:
    """周期性事件在**日志里**的样子：剔除易变字段 → 数组压成条数(+最近一条) → 空间标量取整。

    指纹与打印**共用这个视图** ⇒ "变了才打"就等于"**打出来的东西**变了才打"：
    · 易变字段（时间戳 / 计数器 / `yaw` / `pitch` …）不进视图；
    · `x/y/z/distance` 取整到**格**，`health/saturation` 到 0.1；
    · 大数组（`nearby_blocks`/`entities`/`players`/…）只留**条数 + 距离最近的一条**。

    诚实边界：日志回答"**摘要**变了没变"，不回答"哪一格方块出现了" —— 完整负载照旧喂给模型，
    能力不减，只是不再逐字刷坐标。
    """
    payload = obj.get("data") if isinstance(obj, dict) else None
    if payload is None:
        payload = obj
    return _quantize(summarize_periodic_payload(_normalize_for_key(payload)))


def _coarsen_jitter(value: Any) -> Any:
    """按 `_JITTER_TOLERANCE` 把抖动字段粗化成"落到第几格"（**只用于指纹**）。

    用户实机：`afk.drift` 每帧浮点、最近一条样本的距离在 1 格内来回 —— 这类抖动**单独**出现
    不该触发打行。粗化 = `floor(v / tol)`，所以"同一格内的来回"指纹不变；跨格（真动了）才算
    变化。**打印内容不粗化**（`_log_view` 原样），信息不会被藏掉。
    """
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            tolerance = _JITTER_TOLERANCE.get(str(key).lower())
            if (tolerance and isinstance(item, (int, float))
                    and not isinstance(item, bool)):
                out[key] = math.floor(float(item) / tolerance)
            else:
                out[key] = _coarsen_jitter(item)
        return out
    if isinstance(value, list):
        return [_coarsen_jitter(v) for v in value]
    return value


def _fingerprint_view(obj: Any) -> Any:
    """指纹视图 = 打印视图（`_log_view`）→ **抖动粗化**（`_coarsen_jitter`）。

    与打印视图的差别只有"抖动字段按阈值落到第几格"这一处：判"变没变"比打出来的更宽容一点，
    但真变化（计数 27→23、目标挪出一格…）照样一起变 ⇒ 打出来的行永远对得上判据。
    """
    payload = obj.get("data") if isinstance(obj, dict) else None
    if payload is None:
        payload = obj
    return _quantize(_coarsen_jitter(
        summarize_periodic_payload(_normalize_for_key(payload))))


def format_event_content(obj: dict, limit: int = MAX_LOG_CONTENT_CHARS) -> str:
    """把事件负载压成单行（紧凑 JSON），用于变化日志里的"新内容"。

    与指纹**同源**（`_log_view`）：数组只留条数 + 最近一条样本，空间标量取整到格，
    易变字段（时间戳/计数器/yaw/pitch…）不进视图。所以打印出来的就是"判变化时看的东西"，
    不会再出现"内容变了但打印的摘要一模一样"这种自相矛盾的行。
    """
    payload = _log_view(obj)
    try:
        text = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), default=str)
    except (TypeError, ValueError):
        text = repr(payload)
    if len(text) > limit:
        text = f"{text[:limit]}…(+{len(text) - limit} 字)"
    return text


class _LogDeduper:
    """周期性状态事件的变化驱动日志器。

    每个事件类型**各自**维护一份状态（上次**打出来的**指纹 + 计数），互不串味；内存有上限
    （只保留最近 `MAX_KEYS` 类，超出按 LRU 淘汰）。

    三层约束（对**周期状态事件**；命令/回执/错误/聊天根本不进这里）：

    1. **变了才打**：内容与上次打出来的相同 ⇒ 不打行，只 +1 `suppressed`；
    2. **抖动不算变化**：`event_fingerprint` 已按 `_JITTER_TOLERANCE` 粗化，
       `afk.drift` / 最近样本距离的"格内来回"不触发（见该函数注释）；
    3. **最小间隔**（`min_interval_seconds`，默认 `PERIODIC_MIN_INTERVAL_SECONDS`）：
       同类事件两行至少隔这么久。间隔内的变化**不丢** —— 每一条都记进 `merged`，
       到点的那一行如实带上"（期间另有 N 条变化被合并）"。

    "信息没丢"的复原等式：`emitted + suppressed + merged == 见过的周期事件总数`
    （`total_events()`）。心跳行本身不额外占一个事件额度，所以等式照旧成立。
    """

    MAX_KEYS = 32

    def __init__(self, enabled: bool = True, heartbeat_seconds: float = 0.0,
                 min_interval_seconds: float = PERIODIC_MIN_INTERVAL_SECONDS,
                 clock=time.monotonic):
        self.enabled = bool(enabled)
        self.heartbeat_seconds = self._as_seconds(heartbeat_seconds)
        self.min_interval_seconds = self._as_seconds(min_interval_seconds)
        self._clock = clock
        self._states: "collections.OrderedDict[str, dict]" = collections.OrderedDict()
        self.emitted = 0        # 真的打出来的"内容变化"行
        self.suppressed = 0     # 内容与上次相同（或被间隔压住）而没打行的条数
        self.merged = 0         # 最小间隔内**变了**、被合并进下一行的条数（信息不丢的凭证）
        self.heartbeats = 0     # 心跳行（0 间隔下的"未变"强打，默认关）
        self._merged_by_name: dict[str, int] = {}

    @staticmethod
    def _as_seconds(value: Any) -> float:
        try:
            return max(0.0, float(value or 0.0))
        except (TypeError, ValueError):
            return 0.0

    def total_events(self) -> int:
        """见过的周期性事件总数（= 打出来的 + 压掉的 + 被合并的）；信息没丢的复原依据。"""
        return self.emitted + self.suppressed + self.merged

    def merged_count(self, name: str) -> int:
        """某类事件累计被"最小间隔"合并掉的**变化**条数（快照/排障用）。"""
        return int(self._merged_by_name.get(str(name), 0))

    def feed(self, name: str, fingerprint: str, content: str,
             now: float | None = None) -> str | None:
        """观察一条事件，返回要打印的一行；被压掉/被合并时返回 None（计数照加）。

        两个参照，各司其职，绝不虚报：

        * `delta_printed`（与**上次打出来的**内容比）→ 决定"要不要打行"（相同的没必要打）；
        * `changed`（与**上一条事件**比）→ 决定"这条算不算一次变化"（连续相同的不算）。
        """
        if not self.enabled:
            return None
        timestamp = self._clock() if now is None else now
        entry = self._states.get(name)
        if entry is None:
            self._states[name] = {"fp": fingerprint, "prev_fp": fingerprint, "content": content,
                                  "since": timestamp, "seen": timestamp, "repeats": 0,
                                  "merged": 0, "emitted_at": timestamp}
            self._trim()
            self.emitted += 1
            return f"← {name}: {content}"

        self._states.move_to_end(name)
        entry["seen"] = timestamp
        changed = entry["prev_fp"] != fingerprint
        delta_printed = entry["fp"] != fingerprint
        entry["prev_fp"] = fingerprint
        interval = self.min_interval_seconds
        gated = interval > 0 and (timestamp - entry["emitted_at"]) < interval

        if gated:
            # 最小间隔内：一行都不打。真变化记进 `merged`（下一行如实交代"另有 N 条"）；
            # 连续相同的只是被压掉（不算变化）。
            if changed:
                entry["merged"] += 1
                self.merged += 1
                self._merged_by_name[name] = self._merged_by_name.get(name, 0) + 1
            else:
                entry["repeats"] += 1
                self.suppressed += 1
            return None

        if not delta_printed:
            # 与上次打出来的内容相同 → 这次不该单独占一行。
            entry["repeats"] += 1
            if entry["merged"] <= 0 and self._heartbeat_due(entry, timestamp):
                entry["emitted_at"] = timestamp
                self.heartbeats += 1
                self.suppressed += 1
                elapsed = timestamp - entry["since"]
                return (f"← {name}（未变，已持续 {elapsed:.1f} 秒，"
                        f"累计相同 ×{entry['repeats']}）")
            if entry["merged"] <= 0:
                self.suppressed += 1
                return None
            # 间隔已到、且期间有被合并的变化 ⇒ 也必须打一行交代（否则那段变化就丢了）。

        # 到这里就是"内容行"：把这一段（与上一条相同 ×repeats + 被合并的变化 ×merged）交代清楚。
        if delta_printed:
            entry["fp"] = fingerprint
            entry["content"] = content
        repeats, merged = entry["repeats"], entry["merged"]
        held = max(0.0, timestamp - entry["since"])
        shown = entry["content"]
        entry.update({"since": timestamp, "seen": timestamp, "repeats": 0, "merged": 0,
                      "emitted_at": timestamp})
        self.emitted += 1
        notes: list[str] = []
        if repeats > 0:
            notes.append(f"与上一条相同 ×{repeats}，持续 {held:.1f} 秒")
        if merged > 0:
            notes.append(f"期间另有 {merged} 条变化被合并")
        if notes:
            return f"← {name}（{'；'.join(notes)}）: {shown}"
        return f"← {name}: {shown}"

    def _heartbeat_due(self, entry: dict, timestamp: float) -> bool:
        heartbeat = self.heartbeat_seconds
        return heartbeat > 0 and (timestamp - entry["emitted_at"]) >= heartbeat

    def _trim(self) -> None:
        while len(self._states) > self.MAX_KEYS:
            key, _ = self._states.popitem(last=False)
            self._merged_by_name.pop(key, None)


@dataclass
class WireMessage:
    """A message from the mod (event, response, or progress)."""
    type: str           # "event" | "response" | "progress"
    data: dict


def _resolve_wire_token() -> str:
    """可选握手 token：环境变量 `LCU_WIRE_TOKEN` → 其次 `.local/wire_token` 文件 → 否则空。

    空 = 不鉴权（与模组侧 `wireToken` 默认空一致）⇒ 老链路一字不变。
    放环境变量/独立文件而不是 config.json，是因为这是密钥：UI 不该有地方把它回显出来。
    """
    env = os.environ.get("LCU_WIRE_TOKEN")
    if env and env.strip():
        return env.strip()
    try:
        return (Path(__file__).resolve().parent.parent / ".local" / "wire_token") \
            .read_text(encoding="utf-8").strip()
    except OSError:
        return ""


class WireClient:
    """Connects to the LCU Mod via TCP and communicates with JSONL."""

    def __init__(self, host: str = "127.0.0.1", port: int = 25568, token: str | None = None):
        self.host = host
        self.port = port
        # 可选握手 token（None = 按环境变量/文件解析；空串 = 明确不鉴权）。
        self.token = _resolve_wire_token() if token is None else str(token)
        self.sock: socket.socket | None = None
        self._buffer = ""
        self._decode_warned = False
        self._event_queue: Queue[WireMessage] = Queue()
        self._running = False
        self._reader_thread: threading.Thread | None = None
        self._id_counter = 0
        # 控制流与命令可能来自不同线程，串行化写入以保证 JSONL 行不被交错破坏
        self._send_lock = threading.Lock()
        # 聊天事件监听器（在**读取线程**里被调用，不走事件队列）。见 add_chat_listener。
        self._chat_listeners: list = []
        # 周期性状态事件的"变化才打行"去重（默认开；开关与心跳见 configure_log_dedupe）。
        self._log_deduper = _LogDeduper()

    def configure_log_dedupe(self, enabled: bool | None = None,
                             heartbeat_seconds: float | None = None,
                             min_interval_seconds: float | None = None) -> None:
        """配置入站周期性状态日志（对应 config.json 的 `log_*` 键）。

        `enabled=False` → 逐字回到旧行为（每个事件一行 `← type: name`）；
        `heartbeat_seconds>0` → 内容长时间没变也每隔 N 秒强打一行"未变"，用于区分
        "链路还活着但状态没变" 与 "链路断了"；
        `min_interval_seconds` → 同类周期事件两行之间的**最小间隔**（默认
        `PERIODIC_MIN_INTERVAL_SECONDS`=5s；0 = 关）。用户实机 `state_update` 每 0.5 秒一行，
        间隔内的变化不丢，会以"期间另有 N 条变化被合并"写进下一行。
        默认三个值都不动。
        """
        deduper = self._log_deduper
        if enabled is not None:
            deduper.enabled = bool(enabled)
        if heartbeat_seconds is not None:
            deduper.heartbeat_seconds = _LogDeduper._as_seconds(heartbeat_seconds)
        if min_interval_seconds is not None:
            deduper.min_interval_seconds = _LogDeduper._as_seconds(min_interval_seconds)

    def connect(self) -> bool:
        """Connect to the mod's wire server."""
        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.sock.connect((self.host, self.port))
            self._running = True
            # 可选握手：模组侧配了 token 时，**首帧**必须是匹配的 hello，否则连接会被关掉。
            # 没配 token 时模组会忽略它；老模组不认识这个 type 也会忽略 ⇒ 双向向后兼容。
            self._send_line(json.dumps({"type": "hello", "token": self.token}))
            self._reader_thread = threading.Thread(target=self._read_loop, daemon=True)
            self._reader_thread.start()
            return True
        except Exception as e:
            print(f"[WireClient] Connection failed: {e}")
            return False

    def disconnect(self):
        self._running = False
        if self.sock:
            try:
                self.sock.close()
            except:
                pass
            self.sock = None

    @property
    def connected(self) -> bool:
        """True while the socket is open and the reader loop is running."""
        return self.sock is not None and self._running

    def send_command(self, cmd: str, args: dict | None = None) -> str:
        """Send a command to the mod. Returns the request ID."""
        self._id_counter += 1
        req_id = f"req_{self._id_counter}"
        msg = {
            "type": "command",
            "cmd": cmd,
            "args": args or {},
            "id": req_id,
        }
        self._send_line(json.dumps(msg))
        logger.debug(f"→ {cmd} {args} [{req_id}]")
        return req_id

    def send_control(self, state: dict):
        """Fire-and-forget 控制流下发：无 id、不等回执。

        用于 P0 反射层与 VLA 这类 20-50Hz 的连续运动/视角，绕开命令通道的
        请求-响应开销。Java 侧超过 500ms 收不到新消息会自动松手。
        """
        self._send_line(json.dumps({"type": "control", "data": state}, ensure_ascii=False))

    def send_chat(self, message: str):
        """Send a chat message as the AI player."""
        self.send_command("send_chat", {"message": message})

    def events(self, timeout: float = 0.5):
        """Yield incoming messages from the mod. Blocks up to `timeout` seconds."""
        while self._running:
            try:
                msg = self._event_queue.get(timeout=timeout)
                yield msg
            except Exception:
                # Timeout — just continue waiting, don't break
                continue

    def drain(self) -> list:
        """Return all pending events without blocking (non-blocking drain)."""
        msgs = []
        while self._running:
            try:
                msg = self._event_queue.get_nowait()
                msgs.append(msg)
            except Exception:
                break
        return msgs

    def add_chat_listener(self, listener) -> None:
        """注册"聊天事件"监听器（在**读取线程**里同步调用，参数是 event 的 data 字典）。

        为什么不走事件队列：`/locate` 这类命令要**等系统回复**，而等回复的线程往往就是
        orchestrator 那条正在跑 planner 的线程 —— 它一旦阻塞，队列就没人消费（死锁），
        只有读取线程还活着。这里把聊天事件旁路给监听器，等待方才能被唤醒。
        监听器抛异常只记录日志，绝不允许把读取线程带崩。
        """
        if listener not in self._chat_listeners:
            self._chat_listeners.append(listener)

    def _notify_chat_listeners(self, data: dict) -> None:
        if not self._chat_listeners:
            return
        for listener in list(self._chat_listeners):
            try:
                listener(data)
            except Exception as e:
                logger.warning("[WireClient] 聊天监听器失败: %s", e)

    def _send_line(self, line: str):
        if not self.sock:
            return
        try:
            with self._send_lock:
                self.sock.sendall((line + "\n").encode("utf-8"))
        except Exception as e:
            print(f"[WireClient] Send error: {e}")
            self.disconnect()

    def _read_loop(self):
        while self._running and self.sock:
            try:
                raw = self.sock.recv(65536)
                if not raw:
                    break
                # 容错解码：模组旧版本用平台默认编码（中文 Windows=GBK）写 JSON，直接
                # decode("utf-8") 会抛异常 → 整条消息（常见是带中文物品名的 state_update）
                # 被丢掉且连接被打断，仪表盘就"连上了却没有数据"。这里宁可个别字符变
                # U+FFFD，也绝不丢消息、绝不断连接。
                data = raw.decode("utf-8", errors="replace")
                if "\ufffd" in data and not self._decode_warned:
                    self._decode_warned = True
                    logger.warning("[WireClient] 收到非 UTF-8 字节（模组侧编码不一致），已按替换字符解码；"
                                   "请用新版模组（WireServer 已强制 UTF-8）")
                self._buffer += data
                while "\n" in self._buffer:
                    line, self._buffer = self._buffer.split("\n", 1)
                    line = line.strip()
                    if line:
                        try:
                            obj = json.loads(line)
                            msg_type = obj.get("type", "unknown")
                            label = obj.get("event") or obj.get("id") or "?"
                            deduper = self._log_deduper
                            if deduper.enabled and is_periodic_event(msg_type, label):
                                log_line = deduper.feed(str(label), event_fingerprint(obj),
                                                        format_event_content(obj))
                                if log_line is not None:
                                    logger.debug(log_line)
                            else:
                                # 非周期性消息（命令/回执/错误/决策/聊天）与关闭开关时：
                                # 逐字沿用旧格式，绝不静音关键信息。
                                logger.debug(f"← {msg_type}: {label}")
                            self._event_queue.put(WireMessage(
                                type=msg_type,
                                data=obj,
                            ))
                            # 聊天事件额外旁路给监听器（player_chat / system_chat 都算）：
                            # 等 /locate 回复的线程通常在 planner 里阻塞着，消费不了队列。
                            if msg_type == "event" and "chat" in str(obj.get("event", "")):
                                data = obj.get("data")
                                self._notify_chat_listeners(data if isinstance(data, dict) else {})
                        except json.JSONDecodeError:
                            pass
            except Exception as e:
                print(f"[WireClient] Read error: {e}")
                break
        self._running = False


class ControlStream:
    """高频控制流：latest-wins、无回执，供 P0 反射层与 VLA 驱动。

    与 send_command 的区别是不带 id、不等响应，因此可以 20-50Hz 下发而不会被
    命令队列的回执拖住。Java 侧超过 500ms 收不到新消息会自动松手，所以保持按键
    期间需要按 interval 持续心跳（flush 会自动处理）。
    """

    KEYS = ("forward", "back", "left", "right", "jump", "sneak", "sprint",
            "attack", "use")

    def __init__(self, wire, interval: float = 0.05):
        self.wire = wire
        self.interval = interval
        self._keys = {key: False for key in self.KEYS}
        self._look_dx = 0.0
        self._look_dy = 0.0
        self._yaw: float | None = None
        self._pitch: float | None = None
        self._dirty = False
        # 用 -inf 而不是 0.0：`time.monotonic()` 在**刚启动的机器**上可能只有几十秒
        # （CI runner 很常见），拿 0.0 当"上次发送时间"会让**第一帧**就被 interval 限流掐掉。
        self._last_sent = float("-inf")

    @property
    def holding(self) -> bool:
        """是否处于"按住"状态（需要持续心跳）。"""
        return any(self._keys.values())

    def set(self, **keys):
        """设置按键，例如 set(forward=True, sprint=True)。"""
        for key, value in keys.items():
            if key in self._keys:
                self._keys[key] = bool(value)
        self._dirty = True
        return self

    def look(self, dx: float = 0.0, dy: float = 0.0):
        """叠加一次视角增量（度）。"""
        self._look_dx += dx
        self._look_dy += dy
        self._dirty = True
        return self

    def look_at(self, yaw: float, pitch: float):
        """设置绝对视角（度）。"""
        self._yaw = float(yaw)
        self._pitch = float(pitch)
        self._dirty = True
        return self

    def release(self):
        """松手：清空按键与视角并立刻下发一次。"""
        for key in self._keys:
            self._keys[key] = False
        self._look_dx = self._look_dy = 0.0
        self._yaw = self._pitch = None
        self.flush(force=True)

    def clear(self):
        """本地清空按键与视角，**不发任何帧**。

        不在世界（暂停菜单/连接界面）时用它：既不能让按键卡在"按住"状态，
        也不允许往世界里发指令（在场门要求"零下发"）。回到世界后按键保持全松。
        """
        for key in self._keys:
            self._keys[key] = False
        self._look_dx = self._look_dy = 0.0
        self._yaw = self._pitch = None
        self._dirty = False

    def flush(self, force: bool = False) -> bool:
        """下发当前状态；返回是否真的发送了。

        按键保持期间按 interval 持续心跳，避免 Java 侧超时松手。
        """
        if not self.wire or not self.wire.connected:
            return False
        if not force and not self._dirty and not self.holding:
            return False
        now = time.monotonic()
        if not force and now - self._last_sent < self.interval:
            return False

        payload = dict(self._keys)
        payload["look_dx"] = self._look_dx
        payload["look_dy"] = self._look_dy
        if self._yaw is not None:
            payload["yaw"] = self._yaw
        if self._pitch is not None:
            payload["pitch"] = self._pitch

        self.wire.send_control(payload)
        self._look_dx = self._look_dy = 0.0
        self._yaw = self._pitch = None
        self._dirty = False
        self._last_sent = now
        return True
