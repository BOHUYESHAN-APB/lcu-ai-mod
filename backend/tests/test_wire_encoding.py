"""线协议编码回归：模组侧一旦用了平台默认编码（中文 Windows=GBK），后端不能因此丢消息或断连。

这是"仪表盘连上了却什么数据都没有"的根因回归测试：
旧实现直接 `recv(...).decode("utf-8")`，遇到 GBK 字节会抛 UnicodeDecodeError →
整条消息（常见是带中文物品名的 state_update）被丢弃，连接还会重连。
"""
import unittest

from protocol.wire_client import WireClient


class FakeRecvSocket:
    """按块返回预置字节；耗尽后返回 b""（触发读循环收尾）。"""

    def __init__(self, chunks):
        self._chunks = list(chunks)
        self.closed = False

    def recv(self, _n):
        return self._chunks.pop(0) if self._chunks else b""

    def close(self):
        self.closed = True


class WireEncodingTests(unittest.TestCase):
    def _run(self, chunks):
        wire = WireClient("127.0.0.1", 25568)
        wire.sock = FakeRecvSocket(chunks)
        wire._running = True
        wire._read_loop()
        # _read_loop 收尾时会把 _running 置 False，所以直接从队列取（drain() 依赖 _running）
        msgs = []
        while not wire._event_queue.empty():
            msgs.append(wire._event_queue.get_nowait())
        return wire, msgs

    def test_gbk_bytes_do_not_lose_messages(self):
        """GBK 字节（旧模组）绝不能丢消息、断连接。

        注意：GBK 的"圆石"恰好是**合法**的 UTF-8 序列（0xD4 0xB0 …），只会解成别的字符，
        所以这里只断言"消息都到了"。
        """
        gbk_line = '{"type":"event","event":"state_update","data":{"item":"圆石"}}\n'.encode("gbk")
        utf8_line = '{"type":"event","event":"behavior_state"}\n'.encode("utf-8")

        _wire, msgs = self._run([gbk_line, utf8_line])

        self.assertEqual(len(msgs), 2, "GBK 那条消息不能被整条丢掉")
        self.assertEqual(msgs[0].data["event"], "state_update")
        self.assertEqual(msgs[1].data["event"], "behavior_state")

    def test_invalid_utf8_byte_warns_but_keeps_stream(self):
        """真正非法的 UTF-8 字节（0xFF 永不合法）：替换解码 + 打一次告警，流不断。"""
        bad_line = b'{"type":"event","event":"state_update","data":{"n":"\xff"}}\n'
        utf8_line = '{"type":"event","event":"behavior_state"}\n'.encode("utf-8")

        wire, msgs = self._run([bad_line, utf8_line])

        self.assertEqual(len(msgs), 2)
        self.assertEqual(msgs[0].data["event"], "state_update")
        self.assertEqual(msgs[1].data["event"], "behavior_state")
        self.assertTrue(wire._decode_warned, "应记录一次编码不一致告警")

    def test_valid_utf8_chinese_round_trips(self):
        line = '{"type":"event","event":"state_update","data":{"item":"圆石"}}\n'.encode("utf-8")

        wire, msgs = self._run([line])

        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0].data["data"]["item"], "圆石")
        self.assertFalse(wire._decode_warned, "正常 UTF-8 不应触发告警")

    def test_split_multi_byte_sequence_is_tolerated(self):
        """UTF-8 跨块切断时也不能炸（容错解码不会中断流）。"""
        line = '{"type":"event","event":"state_update","data":{"item":"圆石"}}\n'.encode("utf-8")
        _wire, msgs = self._run([line[:7], line[7:]])

        self.assertEqual(len(msgs), 1, "跨块的消息仍应被拼出")


if __name__ == "__main__":
    unittest.main()
