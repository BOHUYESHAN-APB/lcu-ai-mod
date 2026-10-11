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

import json
import socket
import threading
import time
import logging
from queue import Queue
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger("wire")


@dataclass
class WireMessage:
    """A message from the mod (event, response, or progress)."""
    type: str           # "event" | "response" | "progress"
    data: dict


class WireClient:
    """Connects to the LCU Mod via TCP and communicates with JSONL."""

    def __init__(self, host: str = "127.0.0.1", port: int = 25568):
        self.host = host
        self.port = port
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

    def connect(self) -> bool:
        """Connect to the mod's wire server."""
        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.sock.connect((self.host, self.port))
            self._running = True
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
                            logger.debug(f"← {msg_type}: {obj.get('event') or obj.get('id') or '?'}")
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
        self._last_sent = 0.0

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
