import unittest
import threading

from protocol.wire_client import WireClient


class FailingSocket:
    def sendall(self, _data):
        raise OSError("disconnected")

    def close(self):
        pass

    def shutdown(self, _how):
        pass


class RecordingSocket:
    def __init__(self):
        self.payloads = []

    def sendall(self, data):
        self.payloads.append(data)

    def close(self):
        pass

    def shutdown(self, _how):
        pass


class WireClientTests(unittest.TestCase):
    def test_concurrent_commands_have_unique_ids_and_complete_frames(self):
        wire = WireClient()
        socket = RecordingSocket()
        wire.sock = socket
        wire._running = True
        request_ids = []

        threads = [
            threading.Thread(target=lambda: request_ids.append(wire.send_command("jump")))
            for _ in range(20)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(len(set(request_ids)), 20)
        self.assertEqual(len(socket.payloads), 20)
        self.assertTrue(all(payload.endswith(b"\n") for payload in socket.payloads))

    def test_send_command_raises_when_socket_write_fails(self):
        wire = WireClient()
        wire.sock = FailingSocket()
        wire._running = True

        with self.assertRaisesRegex(ConnectionError, "not connected"):
            wire.send_command("jump")

        self.assertFalse(wire.is_connected)
        self.assertIsNone(wire.sock)


if __name__ == "__main__":
    unittest.main()
