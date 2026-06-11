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
        self._buffer = ""
        self._event_queue: Queue[WireMessage] = Queue()
        self._running = False
        self._reader_thread: threading.Thread | None = None
        self._id_counter = 0

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

    def _send_line(self, line: str):
        if self.sock:
            try:
                self.sock.sendall((line + "\n").encode("utf-8"))
            except Exception as e:
                print(f"[WireClient] Send error: {e}")
                self.disconnect()

    def _read_loop(self):
        while self._running and self.sock:
            try:
                data = self.sock.recv(65536).decode("utf-8")
                if not data:
                    break
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
                        except json.JSONDecodeError:
                            pass
            except Exception as e:
                print(f"[WireClient] Read error: {e}")
                break
        self._running = False
