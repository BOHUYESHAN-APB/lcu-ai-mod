"""`state_update` 这类**胖负载**在日志里的硬上限回归。

用户实机截图：`← state_update: {…}(+1925 字)` —— 单条状态日志将近 2000 字，
一屏就被它刷满，真正要看的决策/错误全被淹掉。

本文件锁定四条：
  ① **胖负载也必须 ≤ `MAX_LOG_CONTENT_CHARS`**（允许末尾那句"…(+N 字)"的尾巴，它有信息量）；
  ② **信息不丢**：数组要留**条数**（"远处还有 200 个实体"这件事照样看得见）；
  ③ **坐标不刷屏**：大数组只留最近一条样本，样本里不带 x/y/z；
  ④ **远处的无关变化不刷行**：同一个最近样本、只有远处某个实体挪了三格 ⇒ 打印内容一字不变
     （日志回答"摘要变没变"，完整负载照旧喂给模型）。
"""
import unittest

from protocol.wire_client import (
    MAX_LOG_CONTENT_CHARS, format_event_content, summarize_periodic_payload,
)


def _entity(idx, distance, x=100.0, y=64.0, z=100.0):
    return {"type": "passive", "name": f"牛{idx}", "distance": distance,
            "x": x, "y": y, "z": z, "health": 10.0}


def _fat_state_update(*, n_entities=200, n_blocks=64, n_players=8, move_far=False):
    """一份**真实形状**的胖 state_update：实体/方块/玩家/记忆点全带坐标。"""
    entities = [_entity(i, 30.0 + i, x=200.0 + i) for i in range(n_entities)]
    if move_far:
        entities[-1] = _entity(n_entities - 1, 30.0 + n_entities - 1, x=9999.0)   # 最远那个挪了
    return {
        "event": "state_update",
        "data": {
            "player": {"x": 100.5, "y": 64.0, "z": 100.25, "health": 20.0, "hunger": 18},
            "entities": entities,
            "nearby_blocks": [
                {"block_id": "minecraft:oak_log", "distance": 4.0, "x": 96, "y": 64, "z": 97,
                 "display": "橡木原木", "item_id": "minecraft:oak_log"}
                for _ in range(n_blocks)
            ],
            "remembered_pois": [
                {"name": "家", "category": "home", "x": -176, "y": 72, "z": -80,
                 "distance": 2100.0}
            ],
            "players": [
                {"name": f"p{i}", "distance": float(i), "x": 100 + i, "y": 64, "z": 100}
                for i in range(n_players)
            ],
        },
    }


class FatPayloadLogLimitTests(unittest.TestCase):

    def test_fat_payload_never_exceeds_the_limit(self):
        line = format_event_content(_fat_state_update())
        # 上限 + 末尾"…(+N 字)"那句话本身
        self.assertLessEqual(len(line), MAX_LOG_CONTENT_CHARS + 40, msg=f"长度 {len(line)}")
        self.assertIn("字)", line)

    def test_the_suffix_says_how_much_was_cut(self):
        payload = _fat_state_update()
        line = format_event_content(payload)
        full = format_event_content(payload, limit=10 ** 9)      # 不截断时的真实长度
        tail = line[line.rindex("(+"):]
        cut = int(tail[2:tail.index(" 字)")])
        self.assertEqual(cut, len(full) - MAX_LOG_CONTENT_CHARS)
        self.assertGreater(cut, 0)

    def test_counts_survive_so_information_is_not_lost(self):
        # 打印摘要里每个数组都带**条数**（"远处还有 200 个实体"这件事看得见）
        summary = summarize_periodic_payload(_fat_state_update()["data"])
        self.assertEqual(summary["entities"]["count"], 200)
        self.assertEqual(summary["nearby_blocks"]["count"], 64)
        self.assertEqual(summary["players"]["count"], 8)
        # 行内也确实带着条数（`entities` 排在字母序最前，一定在 400 字以内；
        # 排在后面的键会被截掉 —— 这是硬上限的代价，如实记下来）
        line = format_event_content(_fat_state_update(n_entities=200, n_blocks=64, n_players=8))
        self.assertIn('"entities":{"count":200', line)
        self.assertIn('"nearest"', line)

    def test_bulk_coordinates_never_reach_the_log(self):
        line = format_event_content(_fat_state_update())
        # 玩家/样本之外的坐标（远处 200 个实体、64 个方块）一律不进日志视图
        self.assertNotIn("9999", line)
        self.assertNotIn('"x":200', line)

    def test_fat_array_keeps_only_the_nearest_sample(self):
        summary = summarize_periodic_payload(_fat_state_update()["data"])
        self.assertEqual(summary["entities"]["count"], 200)
        sample = summary["entities"]["nearest"]
        self.assertEqual(sample["name"], "牛0")            # distance 30 最近
        self.assertNotIn("x", sample)                      # 身份 + 距离，不带坐标
        self.assertNotIn("z", sample)

    def test_an_irrelevant_far_away_change_does_not_change_the_line(self):
        before = format_event_content(_fat_state_update())
        after = format_event_content(_fat_state_update(move_far=True))
        self.assertEqual(before, after)

    def test_a_huge_single_string_is_also_bounded(self):
        line = format_event_content({"event": "chat", "data": {"message": "啊" * 50_000}})
        self.assertLessEqual(len(line), MAX_LOG_CONTENT_CHARS + 40)
        self.assertIn("(+", line)

    def test_exactly_at_the_limit_is_not_truncated(self):
        payload = {"data": {"m": "x" * 50}}
        full = format_event_content(payload, limit=10 ** 9)
        self.assertNotIn("字)", format_event_content(payload, limit=len(full)))

    def test_one_char_over_the_limit_gets_the_suffix(self):
        payload = {"data": {"m": "x" * 50}}
        full = format_event_content(payload, limit=10 ** 9)
        self.assertIn("(+1 字)", format_event_content(payload, limit=len(full) - 1))


if __name__ == "__main__":
    unittest.main()
