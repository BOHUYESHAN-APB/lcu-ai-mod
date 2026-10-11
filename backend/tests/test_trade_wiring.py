"""trade 的参数接线：后端发出去的必须与模组侧 `trade` 命令**一一对应**。

模组侧（`ActionExecutor.handleTrade`，2026-10-12 落地）读的是
`entity_id | radius, offer, item, list_only`；旧参数 `villager_type` 已废弃 ✗。
这里测"真的发出去什么"（不是读源码文本 ✗）。
"""

from __future__ import annotations

import unittest

from agent.skills import Skills
from agent.tool_registry import TOOL_BY_NAME


class TradeWiringTests(unittest.TestCase):
    def _sent(self, **kwargs) -> dict:
        """不跑 `Skills.__init__`（它会去连 wire ✗），只截住真正下发的那一条命令。"""
        skills = Skills.__new__(Skills)
        sent: dict = {}
        skills._send_cmd = lambda cmd, args: (sent.update(cmd=cmd, args=args), {})[1]
        skills.trade(**kwargs)
        return sent

    def test_tool_schema_lists_the_new_arguments(self):
        params = [p.strip() for p in TOOL_BY_NAME["trade"].args.split(",") if p.strip()]
        for name in ("entity_id", "radius", "offer", "item", "list_only"):
            self.assertIn(name, params)
        self.assertNotIn("villager_type", params, "旧参数已废弃：模组侧不再接受它")

    def test_list_only_sends_only_that(self):
        sent = self._sent(list_only=True)
        self.assertEqual(sent["cmd"], "trade")
        self.assertEqual(sent["args"], {"list_only": True})

    def test_offer_and_item_are_passed_through(self):
        sent = self._sent(offer=2, item="emerald", radius=8, entity_id=17)
        self.assertEqual(sent["cmd"], "trade")
        self.assertEqual(sent["args"],
                         {"entity_id": 17, "radius": 8.0, "offer": 2, "item": "emerald"})

    def test_no_arguments_means_nearest(self):
        """什么都不给 ⇒ 模组侧按 `radius=8` 找最近的可交易村民 ✓（不编"村民类型" ✗）。"""
        self.assertEqual(self._sent()["args"], {})

    def test_empty_item_is_not_sent(self):
        self.assertEqual(self._sent(item="")["args"], {})


if __name__ == "__main__":
    unittest.main()
