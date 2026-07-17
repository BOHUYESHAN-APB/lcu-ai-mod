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
        self._event_queue: Queue[WireMessage] = Queue()
        self._running = False
        self._reader_thread: threading.Thread | None = None
        self._id_counter = 0
        self._connection_lock = threading.RLock()
        self._send_lock = threading.Lock()
        self._id_lock = threading.Lock()

    def connect(self) -> bool:
        """Connect to the mod's wire server."""
        self.disconnect()
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(5.0)
        try:
            sock.connect((self.host, self.port))
            sock.settimeout(None)
            with self._connection_lock:
                self.sock = sock
                self._running = True
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

    def disconnect(self, expected_socket: socket.socket | None = None):
        with self._connection_lock:
            if expected_socket is not None and self.sock is not expected_socket:
                return
            self._running = False
            sock = self.sock
            self.sock = None
            reader_thread = self._reader_thread
            self._reader_thread = None
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

    def send_command(self, command: str, args: dict | None = None) -> str:
        """Send a command to the mod. Returns the request ID."""
        with self._id_lock:
            self._id_counter += 1
            req_id = f"req_{self._id_counter}"
        msg = {
            "type": "command",
            "cmd": command,
            "args": args or {},
            "id": req_id,
        }
        if not self._send_line(json.dumps(msg)):
            raise ConnectionError("Minecraft client body is not connected")
        logger.debug(f"→ {command} {args} [{req_id}]")
        return req_id

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

    def wait_for_response(self, req_id: str, timeout: float = 30.0) -> dict | None:
        """Wait for a response with a matching ID."""
        import time
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                msg = self._event_queue.get(timeout=0.1)
                if msg.type == "response" and msg.data.get("id") == req_id:
                    return msg.data
            except:
                continue
        return None

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
        while self.is_connected and self.sock is sock:
            try:
                data = sock.recv(65536).decode("utf-8")
                if not data:
                    break
                buffer += data
                while "\n" in buffer:
                    line, buffer = buffer.split("\n", 1)
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
                        except json.JSONDecodeError:
                            pass
            except Exception as e:
                print(f"[WireClient] Read error: {e}")
                break
        self.disconnect(expected_socket=sock)
