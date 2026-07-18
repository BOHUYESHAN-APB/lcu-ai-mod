import json
import socket
import threading
import time
import unittest
from contextlib import closing

from protocol.wire_client import WireClient


class RealJsonlPeer:
    def __init__(self, token="secret"):
        self.token = token
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(1)
        self.port = self.listener.getsockname()[1]
        self.connected = threading.Event()
        self.command_received = threading.Event()
        self.commands = []
        self.connection = None
        self.thread = threading.Thread(target=self._serve, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_exc):
        if self.connection:
            with closing(self.connection):
                try:
                    self.connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
        self.listener.close()
        self.thread.join(timeout=1)

    def _serve(self):
        try:
            connection, _ = self.listener.accept()
            self.connection = connection
            reader = connection.makefile("rb")
            auth = json.loads(reader.readline().decode("utf-8"))
            accepted = auth == {"type": "auth", "token": self.token}
            response = {
                "type": "auth", "success": accepted, "protocol_version": 2,
                "role": "body_client", "capabilities": ["state", "actions"],
            }
            encoded = (json.dumps(response) + "\n").encode("utf-8")
            connection.sendall(encoded[:3])
            connection.sendall(encoded[3:])
            if not accepted:
                return
            self.connected.set()
            line = reader.readline()
            if line:
                self.commands.append(json.loads(line.decode("utf-8")))
                self.command_received.set()
        except OSError:
            pass

    def send_chunks(self, chunks):
        if not self.connected.wait(2):
            raise RuntimeError("peer was not authenticated")
        for chunk in chunks:
            self.connection.sendall(chunk)


class WireClientSocketIntegrationTests(unittest.TestCase):
    def test_real_socket_auth_command_and_peer_metadata(self):
        with RealJsonlPeer() as peer:
            wire = WireClient("127.0.0.1", peer.port, token="secret")
            try:
                self.assertTrue(wire.connect())
                request_id = wire.send_command("jump", {"source": "integration"})
                self.assertTrue(peer.command_received.wait(2))

                self.assertEqual(peer.commands[0]["id"], request_id)
                self.assertEqual(peer.commands[0]["cmd"], "jump")
                self.assertEqual(wire.peer_info["role"], "body_client")
                self.assertIn("actions", wire.peer_info["capabilities"])
            finally:
                wire.disconnect()

    def test_fragmented_unicode_and_coalesced_frames_reach_queue(self):
        with RealJsonlPeer() as peer:
            wire = WireClient("127.0.0.1", peer.port, token="secret")
            try:
                self.assertTrue(wire.connect())
                first = (json.dumps({"type": "event", "event": "player_chat", "message": "你好矿工"}, ensure_ascii=False) + "\n").encode("utf-8")
                second = (json.dumps({"type": "response", "id": "req-2", "success": True}) + "\n").encode("utf-8")
                split = first.index("你".encode("utf-8")) + 1
                peer.send_chunks([first[:split], first[split:] + second])

                deadline = time.time() + 2
                events = []
                while len(events) < 2 and time.time() < deadline:
                    events.extend(wire.drain())
                    time.sleep(0.01)

                self.assertEqual([event.type for event in events], ["event", "response"])
                self.assertEqual(events[0].data["message"], "你好矿工")
            finally:
                wire.disconnect()

    def test_wait_for_response_preserves_unrelated_events(self):
        with RealJsonlPeer() as peer:
            wire = WireClient("127.0.0.1", peer.port, token="secret")
            try:
                self.assertTrue(wire.connect())
                frames = "".join([
                    json.dumps({"type": "event", "event": "state_update", "data": {"health": 20}}) + "\n",
                    json.dumps({"type": "response", "id": "target", "success": True}) + "\n",
                ]).encode("utf-8")
                peer.send_chunks([frames])
                response = wire.wait_for_response("target", timeout=2)
                remaining = wire.drain()

                self.assertTrue(response["success"])
                self.assertEqual(len(remaining), 1)
                self.assertEqual(remaining[0].data["event"], "state_update")
            finally:
                wire.disconnect()

    def test_wrong_token_is_rejected_over_real_socket(self):
        with RealJsonlPeer() as peer:
            wire = WireClient("127.0.0.1", peer.port, token="wrong")
            self.assertFalse(wire.connect())
            self.assertFalse(wire.is_connected)


if __name__ == "__main__":
    unittest.main()
