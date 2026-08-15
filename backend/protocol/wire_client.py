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
import os
import socket
import threading
import logging
import codecs
import uuid
from queue import Queue
from typing import Callable

from .body import BodyEvent

logger = logging.getLogger("wire")
WireMessage = BodyEvent
MAX_WIRE_FRAME_BYTES = 1024 * 1024
MAX_AUTH_FRAME_BYTES = 64 * 1024


class WireClient:
    """Connects to the LCU Mod via TCP and communicates with JSONL."""

    def __init__(self, host: str = "127.0.0.1", port: int = 25568, token: str | None = None):
        self.host = host
        self.port = port
        self.token = os.getenv("MOD_WIRE_TOKEN", "") if token is None else token
        self.sock: socket.socket | None = None
        self._event_queue: Queue[BodyEvent] = Queue()
        self._running = False
        self._reader_thread: threading.Thread | None = None
        self._id_counter = 0
        self._id_prefix = uuid.uuid4().hex[:12]
        self._connection_lock = threading.RLock()
        self._send_lock = threading.Lock()
        self._id_lock = threading.Lock()
        self._epoch_lock = threading.Lock()
        self._event_callback: Callable[[BodyEvent], None] | None = None
        self.peer_info: dict = {}
        self._body_world_epoch: int | None = None
        self._body_control_epoch: int | None = None

    def connect(self) -> bool:
        """Connect to the mod's wire server."""
        self.disconnect()
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(5.0)
        try:
            sock.connect((self.host, self.port))
            peer_info = self._authenticate(sock)
            sock.settimeout(None)
            with self._connection_lock:
                self._clear_event_queue()
                self.sock = sock
                self._running = True
                self.peer_info = peer_info
            self._clear_body_epochs()
            self._reader_thread = threading.Thread(target=self._read_loop, args=(sock,), daemon=True)
            self._reader_thread.start()
            return True
        except Exception as e:
            try:
                sock.close()
            except OSError:
                pass
            print(f"[WireClient] Connection failed: {e}")
            return False

    def _authenticate(self, sock: socket.socket) -> dict:
        payload = json.dumps({"type": "auth", "token": self.token}) + "\n"
        sock.sendall(payload.encode("utf-8"))
        response = bytearray()
        while len(response) < MAX_AUTH_FRAME_BYTES:
            chunk = sock.recv(1)
            if not chunk:
                raise ConnectionError("wire authentication connection closed")
            if chunk == b"\n":
                break
            response.extend(chunk)
        else:
            raise ConnectionError("wire authentication response is too large")
        result = json.loads(response.decode("utf-8"))
        if result.get("type") != "auth" or not result.get("success"):
            raise ConnectionError("wire authentication failed")
        return result

    @property
    def tool_catalog(self) -> list[dict]:
        tools = self.peer_info.get("tools", [])
        return [tool for tool in tools if isinstance(tool, dict)] if isinstance(tools, list) else []

    def disconnect(self, expected_socket: socket.socket | None = None):
        with self._connection_lock:
            if expected_socket is not None and self.sock is not expected_socket:
                return
            self._running = False
            sock = self.sock
            self.sock = None
            reader_thread = self._reader_thread
            self._reader_thread = None
            self.peer_info = {}
        self._clear_body_epochs()
        if sock:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass
        if reader_thread and reader_thread is not threading.current_thread() and reader_thread.is_alive():
            reader_thread.join(timeout=1.0)

    @property
    def is_connected(self) -> bool:
        with self._connection_lock:
            return self._running and self.sock is not None

    def send_command(self, command: str, args: dict | None = None, request_id: str | None = None) -> str:
        """Send a command to the mod. Returns the request ID."""
        if request_id is None:
            with self._id_lock:
                self._id_counter += 1
                req_id = f"req_{self._id_prefix}_{self._id_counter}"
        else:
            req_id = request_id
        payload_args = dict(args or {})
        with self._epoch_lock:
            world_epoch = self._body_world_epoch
            control_epoch = self._body_control_epoch
        if world_epoch is not None and control_epoch is not None:
            payload_args.setdefault("__lcu_world_epoch", world_epoch)
            payload_args.setdefault("__lcu_control_epoch", control_epoch)
        msg = {
            "type": "command",
            "cmd": command,
            "args": payload_args,
            "id": req_id,
        }
        if not self._send_line(json.dumps(msg)):
            raise ConnectionError("Minecraft client body is not connected")
        logger.debug(f"→ {command} {args} [{req_id}]")
        return req_id

    def send_chat(self, message: str):
        """Send a chat message as the AI player."""
        self.send_command("send_chat", {"message": message})

    def set_event_callback(self, callback: Callable[[BodyEvent], None] | None) -> None:
        self._event_callback = callback

    def events(self, timeout: float = 0.5):
        """Yield incoming messages from the mod. Blocks up to `timeout` seconds."""
        while self._running:
            try:
                msg = self._event_queue.get(timeout=timeout)
                yield msg
            except Exception:
                # Timeout — just continue waiting, don't break
                continue

    def drain(self) -> list[BodyEvent]:
        """Return all pending events without blocking (non-blocking drain)."""
        msgs = []
        while True:
            try:
                msg = self._event_queue.get_nowait()
                msgs.append(msg)
            except Exception:
                break
        return msgs

    def wait_for_response(self, req_id: str, timeout: float = 30.0) -> dict | None:
        """Wait for a response with a matching ID."""
        import time
        deadline = time.time() + timeout
        deferred: list[BodyEvent] = []
        try:
            while time.time() < deadline:
                try:
                    msg = self._event_queue.get(timeout=min(0.1, max(0.0, deadline - time.time())))
                    if msg.type == "response" and msg.data.get("id") == req_id:
                        return msg.data
                    deferred.append(msg)
                except Exception:
                    continue
            return None
        finally:
            for msg in deferred:
                self._event_queue.put(msg)

    def _send_line(self, line: str) -> bool:
        with self._send_lock:
            with self._connection_lock:
                sock = self.sock if self._running else None
            if sock:
                try:
                    sock.sendall((line + "\n").encode("utf-8"))
                    return True
                except Exception as e:
                    print(f"[WireClient] Send error: {e}")
                    self.disconnect(expected_socket=sock)
        return False

    def _read_loop(self, sock: socket.socket):
        buffer = ""
        decoder = codecs.getincrementaldecoder("utf-8")()
        while self.is_connected and self.sock is sock:
            try:
                raw = sock.recv(65536)
                data = decoder.decode(raw, final=not raw)
                if not data:
                    break
                buffer += data
                if len(buffer.encode("utf-8")) > MAX_WIRE_FRAME_BYTES and "\n" not in buffer:
                    raise ConnectionError("wire frame exceeds maximum size")
                while "\n" in buffer:
                    line, buffer = buffer.split("\n", 1)
                    if len(line.encode("utf-8")) > MAX_WIRE_FRAME_BYTES:
                        raise ConnectionError("wire frame exceeds maximum size")
                    line = line.strip()
                    if line:
                        try:
                            obj = json.loads(line)
                            self._learn_body_epochs(obj)
                            msg_type = obj.get("type", "unknown")
                            logger.debug(f"← {msg_type}: {obj.get('event') or obj.get('id') or '?'}")
                            event = BodyEvent(
                                type=msg_type,
                                data=obj,
                            )
                            callback = self._event_callback
                            if callback:
                                try:
                                    callback(event)
                                except Exception:
                                    logger.exception("Body event callback failed")
                            self._event_queue.put(event)
                        except json.JSONDecodeError:
                            pass
            except Exception as e:
                with self._connection_lock:
                    active = self._running and self.sock is sock
                if active:
                    print(f"[WireClient] Read error: {e}")
                break
        self.disconnect(expected_socket=sock)

    def _clear_event_queue(self) -> None:
        while True:
            try:
                self._event_queue.get_nowait()
            except Exception:
                return

    def _clear_body_epochs(self) -> None:
        with self._epoch_lock:
            self._body_world_epoch = None
            self._body_control_epoch = None

    def _learn_body_epochs(self, message: dict) -> None:
        if message.get("type") != "event":
            return
        data = message.get("data")
        if not isinstance(data, dict):
            return
        if message.get("event") == "state_update":
            runtime_context = data.get("runtime_context")
            if not isinstance(runtime_context, dict):
                return
            world_epoch = self._parse_epoch(runtime_context.get("body_world_epoch"))
            control_epoch = self._parse_epoch(runtime_context.get("body_control_epoch"))
        elif message.get("event") == "control_state":
            world_epoch = self._parse_epoch(data.get("body_world_epoch"))
            control_epoch = self._parse_epoch(data.get("body_control_epoch"))
        else:
            return
        if world_epoch is None or control_epoch is None:
            return
        with self._epoch_lock:
            current_world = self._body_world_epoch
            current_control = self._body_control_epoch
            if current_world is None or world_epoch > current_world \
                    or world_epoch == current_world and (current_control is None or control_epoch >= current_control):
                self._body_world_epoch = world_epoch
                self._body_control_epoch = control_epoch

    @staticmethod
    def _parse_epoch(value) -> int | None:
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            return value if value >= 0 else None
        if isinstance(value, str) and value.isdecimal():
            return int(value)
        return None
