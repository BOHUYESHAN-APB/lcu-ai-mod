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
# 错误 / 决策全淹掉。这里只对"周期性状态"事件做去重：内容没变就不打行，只在变化时
# 打一行并交代"上一条相同了多久"。**绝不静音关键信息**：命令、回执、错误、决策、
# 聊天一律不走这条路（见 `is_periodic_event`）。
PERIODIC_EVENT_NAMES = frozenset({
    "state_update", "behavior_state", "control_state", "task_state",
})

# 参与去重键的字段里"每次都变"的时间/序号/计时类字段：不剔除它们就永远判为"变了"。
# 注意**只剔除时间与计数器**；业务计数字段（count/progress/health…）一律保留。
_VOLATILE_KEYS = frozenset({
    "ts", "timestamp", "time", "now", "server_time", "sent_at", "received_at",
    "day_time", "game_time", "world_time", "uptime", "uptime_ms",
    "tick", "ticks", "seq", "seq_id", "sequence", "frame", "elapsed",
})
_VOLATILE_SUFFIXES = ("_ms", "_at", "_ts", "_tick", "_ticks")

# 单行里回显"新内容"的最大长度；超出用 `…(+N 字)` 诚实标注（不隐藏"被截断"这件事）。
MAX_LOG_CONTENT_CHARS = 1500


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
    """周期性事件的去重指纹：规范化 JSON 的哈希（字段顺序无关、剔除时间戳/计数器）。"""
    normalized = _normalize_for_key(obj)
    blob = json.dumps(normalized, ensure_ascii=False, sort_keys=True,
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


def _nearest_sample(items: list) -> Any:
    """数组里距离最近的一条作样本：带 distance 的取最小，否则第一条 dict、再退到首个非 dict。"""
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
    if best is not None:
        return best
    for item in items:
        if not isinstance(item, dict):
            return item
    return None


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


def format_event_content(obj: dict, limit: int = MAX_LOG_CONTENT_CHARS) -> str:
    """把事件负载压成单行（紧凑 JSON），用于变化日志里的"新内容"。

    周期状态负载先过 `summarize_periodic_payload` 压成摘要（数组只留条数 + 最近样本），
    避免"内容一变就打印满屏坐标"；**判变化的指纹仍用完整负载**（`event_fingerprint`），
    所以灵敏度不减、"到底变没变"照样能答。
    """
    payload = obj.get("data") if isinstance(obj, dict) else None
    if payload is None:
        payload = obj
    payload = summarize_periodic_payload(payload)
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

    每个事件类型**各自**维护一份状态（上次指纹 + 抑制计数），互不串味；内存有上限
    （只保留最近 `MAX_KEYS` 类，超出按 LRU 淘汰）。`emitted` / `suppressed` 两个计数
    保证"信息没丢"：即使压掉了行，也能从计数把见过的总条数复原出来。
    """

    MAX_KEYS = 32

    def __init__(self, enabled: bool = True, heartbeat_seconds: float = 0.0, clock=time.monotonic):
        self.enabled = bool(enabled)
        self.heartbeat_seconds = self._as_seconds(heartbeat_seconds)
        self._clock = clock
        self._states: "collections.OrderedDict[str, dict]" = collections.OrderedDict()
        self.emitted = 0        # 真的打出来的"内容变化"行
        self.suppressed = 0     # 因内容相同被压掉的行（每压一行 +1）
        self.heartbeats = 0     # 心跳行（不参与 emitted+suppressed 的复原等式）

    @staticmethod
    def _as_seconds(value: Any) -> float:
        try:
            return max(0.0, float(value or 0.0))
        except (TypeError, ValueError):
            return 0.0

    def total_events(self) -> int:
        """见过的周期性事件总数（= 变化行 + 被压掉的行）；信息没丢的复原依据。"""
        return self.emitted + self.suppressed

    def feed(self, name: str, fingerprint: str, content: str,
             now: float | None = None) -> str | None:
        """观察一条事件，返回要打印的一行；内容与上次相同则返回 None（只 +1 抑制计数）。"""
        if not self.enabled:
            return None
        timestamp = self._clock() if now is None else now
        entry = self._states.get(name)
        if entry is None:
            self._states[name] = {"fp": fingerprint, "since": timestamp, "seen": timestamp,
                                  "repeats": 0, "emitted_at": timestamp}
            self._trim()
            self.emitted += 1
            return f"← {name}: {content}"

        self._states.move_to_end(name)
        if entry["fp"] == fingerprint:
            entry["repeats"] += 1
            entry["seen"] = timestamp
            heartbeat = self.heartbeat_seconds
            if heartbeat > 0 and (timestamp - entry["emitted_at"]) >= heartbeat:
                entry["emitted_at"] = timestamp
                self.heartbeats += 1
                elapsed = timestamp - entry["since"]
                return (f"← {name}（未变，已持续 {elapsed:.1f} 秒，"
                        f"累计相同 ×{entry['repeats']}）")
            self.suppressed += 1
            return None

        # 内容变了：先交代"上一条相同了多久"，再给新内容。
        repeats = entry["repeats"]
        held = max(0.0, entry["seen"] - entry["since"])
        self._states[name] = {"fp": fingerprint, "since": timestamp, "seen": timestamp,
                              "repeats": 0, "emitted_at": timestamp}
        self._states.move_to_end(name)
        self.emitted += 1
        if repeats > 0:
            return f"← {name}（与上一条相同 ×{repeats}，持续 {held:.1f} 秒）: {content}"
        return f"← {name}: {content}"

    def _trim(self) -> None:
        while len(self._states) > self.MAX_KEYS:
            self._states.popitem(last=False)


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
                             heartbeat_seconds: float | None = None) -> None:
        """配置入站周期性状态日志（对应 config.json 的 `log_*` 键）。

        `enabled=False` → 逐字回到旧行为（每个事件一行 `← type: name`）；
        `heartbeat_seconds>0` → 内容长时间没变也每隔 N 秒强打一行"未变"，用于区分
        "链路还活着但状态没变" 与 "链路断了"。默认两个值都不动。
        """
        deduper = self._log_deduper
        if enabled is not None:
            deduper.enabled = bool(enabled)
        if heartbeat_seconds is not None:
            deduper.heartbeat_seconds = _LogDeduper._as_seconds(heartbeat_seconds)

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
