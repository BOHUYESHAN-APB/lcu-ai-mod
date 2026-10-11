import json
import unittest

from protocol.wire_client import ControlStream, WireClient


class DummyWire:
    def __init__(self, connected=True):
        self.connected = connected
        self.sent = []

    def send_control(self, state):
        self.sent.append(dict(state))


class FakeSocket:
    def __init__(self):
        self.lines = []

    def sendall(self, data):
        self.lines.append(data.decode("utf-8"))

    def close(self):
        pass


class ControlStreamTests(unittest.TestCase):
    def test_flush_sends_keys_and_look(self):
        wire = DummyWire()
        stream = ControlStream(wire, interval=0)
        stream.set(forward=True, sprint=True).look(dx=10, dy=-5)

        self.assertTrue(stream.flush())

        self.assertTrue(wire.sent[0]["forward"])
        self.assertTrue(wire.sent[0]["sprint"])
        self.assertEqual(wire.sent[0]["look_dx"], 10)
        self.assertEqual(wire.sent[0]["look_dy"], -5)

    def test_look_delta_is_consumed_after_flush(self):
        wire = DummyWire()
        stream = ControlStream(wire, interval=0)
        stream.look(dx=10)
        stream.flush()

        stream.set(forward=True)
        stream.flush()

        self.assertEqual(wire.sent[1]["look_dx"], 0)

    def test_holding_key_keeps_heartbeat(self):
        wire = DummyWire()
        stream = ControlStream(wire, interval=0)
        stream.set(forward=True)

        self.assertTrue(stream.flush())
        self.assertTrue(stream.flush())

        self.assertEqual(len(wire.sent), 2)

    def test_idle_stream_does_not_send(self):
        wire = DummyWire()
        stream = ControlStream(wire, interval=0)

        self.assertFalse(stream.flush())
        self.assertEqual(wire.sent, [])

    def test_release_sends_all_keys_false(self):
        wire = DummyWire()
        stream = ControlStream(wire, interval=0)
        stream.set(forward=True, jump=True)
        stream.flush()

        stream.release()

        self.assertFalse(wire.sent[-1]["forward"])
        self.assertFalse(wire.sent[-1]["jump"])
        self.assertFalse(stream.holding)

    def test_flush_returns_false_when_disconnected(self):
        stream = ControlStream(DummyWire(connected=False), interval=0)
        stream.set(forward=True)

        self.assertFalse(stream.flush())

    def test_interval_rate_limits_repeats(self):
        wire = DummyWire()
        stream = ControlStream(wire, interval=60.0)
        stream.set(forward=True)

        self.assertTrue(stream.flush())
        self.assertFalse(stream.flush())

    def test_look_at_sends_absolute_angles(self):
        wire = DummyWire()
        stream = ControlStream(wire, interval=0)
        stream.look_at(90.0, -30.0)

        stream.flush()

        self.assertEqual(wire.sent[0]["yaw"], 90.0)
        self.assertEqual(wire.sent[0]["pitch"], -30.0)


class SendControlTests(unittest.TestCase):
    def _wire(self):
        wire = WireClient()
        wire.sock = FakeSocket()
        wire._running = True
        return wire

    def test_send_control_emits_newline_delimited_fire_and_forget_message(self):
        wire = self._wire()

        wire.send_control({"forward": True, "look_dx": 5.0})

        raw = wire.sock.lines[0]
        self.assertTrue(raw.endswith("\n"))
        obj = json.loads(raw)
        self.assertEqual(obj["type"], "control")
        self.assertEqual(obj["data"]["forward"], True)
        self.assertNotIn("id", obj)

    def test_send_command_still_carries_an_id(self):
        wire = self._wire()

        req_id = wire.send_command("move_to", {"x": 1, "y": 2, "z": 3})

        obj = json.loads(wire.sock.lines[0])
        self.assertEqual(obj["type"], "command")
        self.assertEqual(obj["id"], req_id)


if __name__ == "__main__":
    unittest.main()
