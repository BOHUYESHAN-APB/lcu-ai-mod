"""build 的参数接线：后端发出去的必须与模组侧 `build` 命令**一一对应**。

模组侧（`ActionExecutor.handleBuild`，2026-10-12 落地）读的是
`x, y, z, structure, material, size, height`；其中 **`material` 是必需的** ✓
（材料由后端大模型点名 ✓，本地不做材料启发式 ✗）。
这里测"真的发出去什么"（不是读源码文本 ✗）。
"""

from __future__ import annotations

import unittest

from agent.skills import Skills
from agent.tool_registry import TOOL_BY_NAME


class BuildWiringTests(unittest.TestCase):
    def _sent(self, **kwargs) -> dict:
        """不跑 `Skills.__init__`（它会去连 wire ✗），只截住真正下发的那一条命令。"""
        skills = Skills.__new__(Skills)
        sent: dict = {}
        skills._send_cmd = lambda cmd, args: (sent.update(cmd=cmd, args=args), {})[1]
        skills.build(**kwargs)
        return sent

    def test_tool_schema_lists_the_new_arguments(self):
        params = [p.strip() for p in TOOL_BY_NAME["build"].args.split(",") if p.strip()]
        for name in ("x", "y", "z", "structure", "material", "size", "height"):
            self.assertIn(name, params)

    def test_material_size_height_are_passed_through(self):
        sent = self._sent(x=10, y=64, z=-3, structure="pen", material="oak_fence", size=3, height=1)
        self.assertEqual(sent["cmd"], "build")
        self.assertEqual(sent["args"], {"x": 10, "y": 64, "z": -3, "structure": "pen",
                                        "material": "oak_fence", "size": 3, "height": 1})

    def test_without_material_only_the_position_is_sent(self):
        """不点名材料 ⇒ **不发** material ✓（模组侧会如实回 `need_material` ✓，不编方块 ✗）。"""
        sent = self._sent(x=1, y=2, z=3, structure="wall")
        self.assertNotIn("material", sent["args"])
        self.assertEqual(sent["args"]["structure"], "wall")

    def test_defaults_are_sent_as_three_and_one(self):
        sent = self._sent(x=0, y=0, z=0, structure="platform", material="stone")
        self.assertEqual(sent["args"]["size"], 3)
        self.assertEqual(sent["args"]["height"], 1)


if __name__ == "__main__":
    unittest.main()
