"""线协议契约回归：4 个新工具**真实发出的 JSONL 帧**必须与 Java 端逐字对齐。

为什么需要这条：Java `ActionExecutor` 读的是固定字段名 ——
  - `find_block`   → `args["block"] / radius? / limit?`
  - `go_to_block`  → `args["block"] / radius? / remember?`
  - `sleep`        → 无参数（旧命令名沿用，已被改成"寻路过去 + 点床"）
  - `remember_poi` → `args["name"] / category? / x? / y? / z?`

字段名写错（例如发 `block_id` 而不是 `block`、发 `save` 而不是 `remember`）**不会报错**：
Java 只会回 "Need block" 或落进 `default -> Unknown:` —— 表现为"模型说做了、其实没做"。
这里用假 socket 走**真实的 `_send_line`**（含 UTF-8 编码与换行），抓真正上线的字节。
"""
import json
import unittest

from agent.skills import Skills
from protocol.wire_client import WireClient


class FakeSocket:
    """只实现 sendall 的假 socket：让真实编码/换行逻辑照常跑。"""

    def __init__(self):
        self.chunks: list[bytes] = []

    def sendall(self, data: bytes) -> None:
        self.chunks.append(data)

    def close(self) -> None:
        pass


class WireContractBlockToolsTests(unittest.TestCase):

    def setUp(self):
        self.wire = WireClient("127.0.0.1", 25568)
        self.fake = FakeSocket()
        self.wire.sock = self.fake
        self.wire._running = True
        self.skills = Skills(wire=self.wire)

    def _raw(self) -> str:
        return b"".join(self.fake.chunks).decode("utf-8")

    def _frames(self):
        return [json.loads(line) for line in self._raw().splitlines()]

    def test_find_block_frame_matches_java_contract(self):
        self.skills.find_block("minecraft:white_bed", radius=24, limit=3)

        frame = self._frames()[-1]
        self.assertEqual(frame["type"], "command")
        self.assertEqual(frame["cmd"], "find_block")
        self.assertEqual(frame["args"], {"block": "minecraft:white_bed", "radius": 24, "limit": 3})
        self.assertTrue(frame["id"].startswith("req_"))

    def test_find_block_defaults(self):
        self.skills.find_block("*_bed")
        self.assertEqual(self._frames()[-1]["args"], {"block": "*_bed", "radius": 32, "limit": 8})

    def test_go_to_block_frame_matches_java_contract(self):
        self.skills.go_to_block("*_bed")
        # remember 默认 True（用户说"找床"就该记住它），字段名必须是 remember
        self.assertEqual(
            self._frames()[-1]["args"], {"block": "*_bed", "radius": 32, "remember": True})

        self.skills.go_to_block("minecraft:chest", radius=16, remember=False)
        self.assertEqual(
            self._frames()[-1]["args"],
            {"block": "minecraft:chest", "radius": 16, "remember": False})

    def test_sleep_in_bed_uses_existing_sleep_command(self):
        """Java 侧只有 `sleep` 一个命令（已改成寻路+点床）：技能名不同，命令名不能发错。"""
        self.skills.sleep_in_bed()
        frame = self._frames()[-1]
        self.assertEqual(frame["cmd"], "sleep")
        self.assertEqual(frame["args"], {})

    def test_remember_poi_omits_missing_coordinates(self):
        self.skills.remember_poi("home", category="waypoint")
        # 缺省坐标 = 当前脚下：Java 靠"没有 x/y/z"判断，不能发 null
        self.assertEqual(self._frames()[-1]["args"], {"name": "home", "category": "waypoint"})

        self.skills.remember_poi("bed", category="bed", x=10.0, y=64.0, z=-20.0)
        self.assertEqual(
            self._frames()[-1]["args"],
            {"name": "bed", "category": "bed", "x": 10.0, "y": 64.0, "z": -20.0},
        )

    def test_remember_poi_defaults(self):
        self.skills.remember_poi()
        self.assertEqual(self._frames()[-1]["args"], {"name": "", "category": "waypoint"})

    def test_all_new_commands_are_newline_terminated_utf8_jsonl(self):
        """模组侧按行读（readLine）：漏换行会让命令卡在缓冲区里永远不执行。"""
        self.skills.find_block("*_bed")
        self.skills.go_to_block("minecraft:white_bed")
        self.skills.sleep_in_bed()
        self.skills.remember_poi("这里")  # 中文必须按 UTF-8 编码（GBK 会让模组侧解析失败）

        self.assertEqual(len(self.fake.chunks), 4)
        for chunk in self.fake.chunks:
            self.assertTrue(chunk.endswith(b"\n"), "每条命令必须以换行结尾")
            json.loads(chunk.decode("utf-8"))  # 必须是合法 UTF-8 JSON 行


if __name__ == "__main__":
    unittest.main()
