"""掉落物的「该不该捡」事实链：模组报事实（谁丢的/掉多久了/还有多久消失），模型自己判断。

用户口径原话：**"掉落物要判断该不该捡"**、**"谁丢的要报上来，判断归模型"**。
所以这里测的是「事实有没有如实到模型眼前」，**不是**"它有没有去捡"（没有自动拾取这回事）。
"""

import unittest

from agent.planner import Planner, _dropped_by_note


def _item(**over) -> dict:
    item = {
        "type": "item",
        "item_id": "minecraft:diamond",
        "item_count": 3,
        "distance": 2.1,
        "owner": "",
        "owner_type": "",
        "age": 0,
        "despawn_in": 0,
    }
    item.update(over)
    return item


class DroppedByNoteTests(unittest.TestCase):
    def test_player_drop_names_the_player(self):
        note = _dropped_by_note(_item(owner="_Fire00Fly_", owner_type="player", age=240,
                                      despawn_in=5760))
        self.assertIn("_Fire00Fly_丢的", note)
        self.assertIn("掉了12秒", note)
        self.assertIn("还有288秒消失", note)

    def test_non_player_drop_says_dropped_not_thrown_by(self):
        self.assertIn("僵尸掉的", _dropped_by_note(_item(owner="僵尸", owner_type="other")))

    def test_no_owner_means_no_invented_blame(self):
        # 方块/发射器掉的：owner 为空 ⇒ 只说时间，不许编一个"谁丢的"。
        note = _dropped_by_note(_item(age=20, despawn_in=5980))
        self.assertNotIn("丢的", note)
        self.assertIn("掉了1秒", note)

    def test_old_client_without_the_new_fields_renders_nothing(self):
        self.assertEqual("", _dropped_by_note({"type": "item", "item_id": "minecraft:dirt"}))

    def test_negative_and_missing_values_are_skipped(self):
        self.assertEqual("", _dropped_by_note(_item(age=-1, despawn_in=None)))


class ItemFactsReachTheModelTests(unittest.TestCase):
    """L2 文本里真的带上这些事实（否则上面那层渲染等于没接）。"""

    def _l2(self, entities: list) -> str:
        planner = object.__new__(Planner)

        class _Memory:
            def build_context(self):
                return {}

            def get_recent_context(self, limit=5):
                return []

        planner.memory = _Memory()
        planner.permissions = None
        planner._home_facts = lambda *a, **k: {}      # type: ignore[attr-defined]
        return Planner._build_l2(planner, "BoHuYeShan", "你好",
                                 {"entities": entities, "inventory": []}, "LCU")

    def test_dropped_item_line_carries_owner_and_timer(self):
        text = self._l2([_item(owner="_Fire00Fly_", owner_type="player", age=240,
                               despawn_in=5760)])
        self.assertIn("minecraft:diamondx3", text)
        self.assertIn("_Fire00Fly_丢的", text)
        self.assertIn("还有288秒消失", text)

    def test_the_line_tells_the_model_that_the_choice_is_its_own(self):
        text = self._l2([_item()])
        self.assertIn("捡不捡", text)
        self.assertIn("走过去踩到就会自动捡起", text)
        self.assertIn("背包满了", text)


if __name__ == "__main__":
    unittest.main()
