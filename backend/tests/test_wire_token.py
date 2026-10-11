"""线协议可选 token 握手（后端侧）。

模组侧 `wireToken` **默认空 = 不鉴权** ⇒ 老链路一字不变；设了才要求后端**首帧**发 hello。
这里只测后端侧的两件事：token 从哪来、hello 是不是真作为首帧发出去。
跨语言那一半（模组真会拒绝坏 token）由 tests/integration 用真 Java 夹具跑。
"""

from __future__ import annotations

import json
import os
import unittest
from unittest import mock

from protocol.wire_client import WireClient, _resolve_wire_token


class _FakeSocket:
    """记录写出的字节；`recv` 立刻 EOF，好让读线程干净退出。"""

    def __init__(self):
        self.sent: list[bytes] = []
        self.closed = False

    def connect(self, addr):
        self.addr = addr

    def sendall(self, data: bytes):
        self.sent.append(data)

    def recv(self, n: int) -> bytes:
        return b""

    def settimeout(self, t):
        pass

    def close(self):
        self.closed = True

    def lines(self) -> list[str]:
        return [chunk.decode("utf-8").strip() for chunk in self.sent]


class _FakePath:
    """够用的 Path 替身：支持 `Path(...).resolve().parent.parent / ".local" / "wire_token"`。"""

    text = ""
    error: Exception | None = None

    def __init__(self, *args, **kwargs):
        pass

    def resolve(self):
        return self

    @property
    def parent(self):
        return self

    def __truediv__(self, other):
        return self

    def read_text(self, encoding=None):
        if _FakePath.error is not None:
            raise _FakePath.error
        return _FakePath.text


class TokenResolutionTests(unittest.TestCase):
    def setUp(self):
        _FakePath.text = ""
        _FakePath.error = OSError("no such file")

    def test_env_var_wins(self):
        with mock.patch.dict(os.environ, {"LCU_WIRE_TOKEN": "  envtok  "}):
            self.assertEqual(_resolve_wire_token(), "envtok")

    def test_file_used_when_env_absent(self):
        env = {k: v for k, v in os.environ.items() if k != "LCU_WIRE_TOKEN"}
        _FakePath.text, _FakePath.error = "filetok\n", None
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch("protocol.wire_client.Path", _FakePath):
            self.assertEqual(_resolve_wire_token(), "filetok")

    def test_empty_when_neither_is_present(self):
        env = {k: v for k, v in os.environ.items() if k != "LCU_WIRE_TOKEN"}
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch("protocol.wire_client.Path", _FakePath):
            self.assertEqual(_resolve_wire_token(), "")

    def test_blank_env_is_ignored(self):
        with mock.patch.dict(os.environ, {"LCU_WIRE_TOKEN": "   "}):
            self.assertEqual(_resolve_wire_token(), "")


class HelloHandshakeTests(unittest.TestCase):
    def _connect_and_capture(self, token: str) -> _FakeSocket:
        fake = _FakeSocket()
        with mock.patch("protocol.wire_client.socket.socket", return_value=fake):
            client = WireClient("127.0.0.1", 25568, token=token)
            self.assertTrue(client.connect())
            client.disconnect()
        return fake

    def test_hello_is_the_first_frame_and_carries_the_token(self):
        fake = self._connect_and_capture("s3cret")
        self.assertTrue(fake.sent, "nothing was written to the socket")
        first = json.loads(fake.lines()[0])
        self.assertEqual(first["type"], "hello")
        self.assertEqual(first["token"], "s3cret")

    def test_hello_still_sent_when_no_token_is_configured(self):
        """协议自描述：没 token 也发 hello（空串），老模组不认识这个 type 会忽略它。"""
        fake = self._connect_and_capture("")
        first = json.loads(fake.lines()[0])
        self.assertEqual(first["type"], "hello")
        self.assertEqual(first["token"], "")

    def test_hello_precedes_any_command(self):
        fake = _FakeSocket()
        with mock.patch("protocol.wire_client.socket.socket", return_value=fake):
            client = WireClient("127.0.0.1", 25568, token="t")
            client.connect()
            client.send_command("look_at", {"x": 1})
            client.disconnect()
        types = [json.loads(line)["type"] for line in fake.lines()]
        self.assertEqual(types[0], "hello")
        self.assertIn("command", types)
        self.assertLess(types.index("hello"), types.index("command"))


if __name__ == "__main__":
    unittest.main()
