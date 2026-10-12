"""内存上界的回归测试（第二轮性能治理）。

这些结构长期跑会随运行时间 / 世界规模线性增长；修完后都封了顶，但**功能不变**
（记忆、摘要、POI、贴图照常）。这里把每条上界钉住，防止以后又退化回无界。
"""

import base64
import tempfile
import unittest
from pathlib import Path

from agent.conversation import PENDING_SUMMARY_LIMIT, ConversationStore
from agent.memory import (
    MAX_EVENTS,
    MAX_POI_ENTRIES,
    MAX_RECENT_MESSAGES,
    PROFILE_WORD_LIMIT,
    Memory,
    WorldPoiMemory,
)


class MemoryBoundsTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _memory(self):
        return Memory(path=str(self.root / "memory.json"))

    def test_record_action_respects_event_cap(self):
        """动作记录此前**没有**走 `max_events`，长期跑会一直长。"""
        memory = self._memory()
        for i in range(MAX_EVENTS + 250):
            memory.record_action(f"cmd:probe{i}", True)
        self.assertEqual(len(memory.events), MAX_EVENTS)
        # 最新的留下（最旧的被挤掉）
        self.assertIn("probe%d" % (MAX_EVENTS + 249), memory.events[-1]["description"])

    def test_load_trims_oversized_files(self):
        """老文件没有上界：载入时就地裁到上界，别把整份旧账重新常驻。"""
        path = self.root / "memory.json"
        big = {
            "recent_messages": [{"sender": "a", "message": f"m{i}"} for i in range(500)],
            "events": [{"type": "action", "description": f"e{i}"} for i in range(3000)],
            "player_profiles": {"Alice": {"common_words": {f"w{i}": 1 for i in range(900)}}},
            "locations": {},
            "interaction_count": 0,
            "total_actions": 0,
        }
        import json
        path.write_text(json.dumps(big), encoding="utf-8")

        memory = Memory(path=str(path))
        self.assertEqual(len(memory.recent_messages), MAX_RECENT_MESSAGES)
        self.assertEqual(len(memory.events), MAX_EVENTS)
        self.assertEqual(len(memory.player_profiles["Alice"]["common_words"]), PROFILE_WORD_LIMIT)

    def test_profile_word_table_is_capped(self):
        memory = self._memory()
        for i in range(PROFILE_WORD_LIMIT + 300):
            memory.add_interaction("Alice", f"word{i} filler")
        self.assertLessEqual(len(memory.player_profiles["Alice"]["common_words"]), PROFILE_WORD_LIMIT)


class PoiBoundsTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_entry_cap_evicts_oldest(self):
        poi = WorldPoiMemory(root=str(self.root))
        poi.use_namespace("s", "w")
        for i in range(MAX_POI_ENTRIES + 40):
            poi.observe("storage", {"block_id": "minecraft:chest", "x": i, "y": 64, "z": i})
        self.assertEqual(len(poi._entries), MAX_POI_ENTRIES)
        # 最近的还在、最旧的被淘汰
        keys = set(poi._entries)
        self.assertIn("|%d,64,%d" % (MAX_POI_ENTRIES + 39, MAX_POI_ENTRIES + 39), keys)
        self.assertNotIn("|0,64,0", keys)


class ConversationBoundsTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self._tmp.cleanup()

    def test_pending_is_capped_when_summaries_never_run(self):
        """没有 LLM 时 pending 只增不减（慢漏）：安全阀应把它按住。"""
        store = ConversationStore(root=self._tmp.name, single_window=10, multi_window=8)
        store.use_namespace("s", "w")
        for i in range(PENDING_SUMMARY_LIMIT + 400):
            store.record_chat("Alice", f"刷屏{i}")
        self.assertEqual(len(store.pending_summary()), PENDING_SUMMARY_LIMIT)
        self.assertEqual(len(store._window), 10)  # 窗口口径不变（单人=10）


class IconCacheBoundsTests(unittest.TestCase):
    def setUp(self):
        from api import overlay
        overlay.reset_icon_cache()
        self.overlay = overlay

    def tearDown(self):
        self.overlay.reset_icon_cache()

    def test_total_byte_budget_evicts_oldest(self):
        png = b"\x89PNG\r\n\x1a\n" + b"0" * (256 * 1024)
        encoded = base64.b64encode(png).decode()
        # 每张 256KB → 预算 32MB 只装得下约 128 张；灌 400 张必须被按住。
        for i in range(400):
            self.overlay.note_item_icons({f"minecraft:probe{i}": encoded})
        total = sum(len(v) for v in self.overlay._icon_pngs.values())
        self.assertLessEqual(total, self.overlay.MAX_ICON_TOTAL_BYTES)
        self.assertLess(len(self.overlay._icon_pngs), 400)
        # 最近收到的仍然在（淘汰的是最早收到的）
        self.assertIn("minecraft:probe399", self.overlay.cached_icon_ids())


if __name__ == "__main__":
    unittest.main()
