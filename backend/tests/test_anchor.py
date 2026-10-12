"""锚点（「这里 / 这附近」的圆心）的行为回归。

用户口径（原话）："用户说『来到这里』，那么 AI 就会探查用户说的话这一瞬间，用户所在的位置，
然后到这个位置。如果后面附加『在附近干什么』，那么就基于这个点，然后再按照一定的半径去找。"

本文件锁死四件事：
  ① **锚点 = 说话瞬间的位置**（快照），保质期内优先于 AI 自身位置；
  ② **显式坐标永远优先**（模型说了去哪就去哪，锚点不抢）；
  ③ **过期就明说**：不许静默改用自身位置，也不许拿过期锚点当"这里"；
  ④ **读不出坐标就宁可不锚** —— 绝不退化成 (0,0,0) 或世界出生点。
"""
import unittest

from agent import anchor as A


def _speaker(x=100.0, y=64.0, z=100.0, name="BoHuYeShan", dimension="minecraft:overworld"):
    return {"x": x, "y": y, "z": z, "name": name, "dimension": dimension}


class AnchorFromSpeakerTests(unittest.TestCase):

    def test_reads_the_position_at_the_moment_of_speech(self):
        a = A.Anchor.from_speaker(_speaker(x=-176.0, y=72.0, z=-80.0), ts=1000.0)
        self.assertIsNotNone(a)
        self.assertEqual(a.center(), (-176.0, 72.0, -80.0))
        self.assertEqual(a.player, "BoHuYeShan")
        self.assertEqual(a.dimension, "minecraft:overworld")
        self.assertEqual(a.age(1012.0), 12.0)

    def test_missing_coordinates_never_become_a_fake_origin(self):
        for bad in ({"y": 64.0, "z": 100.0}, {"x": 1.0, "y": None, "z": 2.0}, {}):
            self.assertIsNone(A.Anchor.from_speaker(bad, ts=1.0), msg=f"不该锚在 {bad}")

    def test_garbage_coordinates_are_refused(self):
        self.assertIsNone(A.Anchor.from_speaker({"x": "远处", "y": 64, "z": 100}, ts=1.0))
        self.assertIsNone(A.Anchor.from_speaker({"x": True, "y": 64, "z": 100}, ts=1.0))

    def test_without_a_timestamp_there_is_no_anchor(self):
        self.assertIsNone(A.Anchor.from_speaker(_speaker(), ts=None))

    def test_a_backwards_clock_does_not_produce_a_negative_age(self):
        a = A.Anchor.from_speaker(_speaker(), ts=100.0)
        self.assertEqual(a.age(90.0), 0.0)

    def test_the_line_says_whose_spot_and_how_long_ago(self):
        a = A.Anchor.from_speaker(_speaker(), ts=100.0)
        text = a.text(now=340.0)
        self.assertIn("BoHuYeShan", text)
        self.assertIn("4.0 分钟前", text)


class AnchorResolutionTests(unittest.TestCase):

    def _anchor(self, ts=1000.0, x=100.0):
        return A.Anchor.from_speaker(_speaker(x=x), ts=ts)

    def test_a_fresh_anchor_beats_my_own_position(self):
        got = A.resolve(anchor=self._anchor(), self_pos=(0.0, 70.0, 0.0), now=1010.0)
        self.assertEqual(got["source"], "anchor")
        self.assertEqual(got["center"], (100.0, 64.0, 100.0))
        self.assertIn("你刚才说话时的位置", got["note"])

    def test_explicit_coordinates_beat_the_anchor(self):
        got = A.resolve({"x": 5, "y": 6, "z": 7}, anchor=self._anchor(), self_pos=(0.0, 70.0, 0.0),
                        now=1010.0)
        self.assertEqual(got["source"], "explicit")
        self.assertEqual(got["center"], (5.0, 6.0, 7.0))

    def test_a_stale_anchor_falls_back_and_says_so(self):
        got = A.resolve(anchor=self._anchor(ts=1000.0, x=999.0), self_pos=(0.0, 70.0, 0.0),
                        now=1000.0 + A.MAX_AGE_SECONDS + 60.0)
        self.assertEqual(got["source"], "self")
        self.assertEqual(got["center"], (0.0, 70.0, 0.0))
        self.assertIn("过期", got["note"])          # 不许静默换圆心
        self.assertNotEqual(got["center"], (999.0, 64.0, 999.0))

    def test_the_age_boundary_is_inclusive_on_the_fresh_side(self):
        anchor = self._anchor(ts=1000.0)
        exactly = A.resolve(anchor=anchor, self_pos=(0.0, 70.0, 0.0),
                            now=1000.0 + A.MAX_AGE_SECONDS)
        over = A.resolve(anchor=anchor, self_pos=(0.0, 70.0, 0.0),
                         now=1000.0 + A.MAX_AGE_SECONDS + 0.001)
        self.assertEqual(exactly["source"], "anchor")
        self.assertEqual(over["source"], "self")

    def test_no_anchor_and_no_self_position_means_no_center(self):
        got = A.resolve(anchor=None, self_pos=None, now=10.0)
        self.assertEqual(got["source"], "none")
        self.assertIsNone(got["center"])
        self.assertIn("没有可用圆心", got["note"])

    def test_a_partial_explicit_coordinate_is_not_a_center(self):
        got = A.resolve({"x": 5, "y": 6}, self_pos=None, now=10.0)
        self.assertIsNone(got["center"])
        self.assertEqual(got["source"], "none")

    def test_falling_back_to_self_without_any_anchor_is_still_spelled_out(self):
        got = A.resolve(self_pos=(1.0, 2.0, 3.0), now=10.0)
        self.assertEqual(got["source"], "self")
        self.assertIn("我自己现在的位置", got["note"])

    def test_every_outcome_names_its_center(self):
        cases = [
            A.resolve({"x": 1, "y": 2, "z": 3}, now=10.0),
            A.resolve(anchor=self._anchor(), now=1010.0),
            A.resolve(anchor=self._anchor(ts=0.0), self_pos=(1.0, 2.0, 3.0), now=10_000.0),
            A.resolve(self_pos=(1.0, 2.0, 3.0), now=10.0),
            A.resolve(now=10.0),
        ]
        for got in cases:
            self.assertIn("圆心", got["note"], msg=f"{got['source']} 没说圆心来自哪")
            self.assertIn(got["source"], ("explicit", "anchor", "self", "none"))

    def test_a_new_statement_replaces_the_old_anchor(self):
        first = self._anchor(ts=1000.0, x=100.0)
        second = A.Anchor.from_speaker(_speaker(x=500.0, z=500.0), ts=1100.0)
        self.assertEqual(A.resolve(anchor=second, now=1105.0)["center"], (500.0, 64.0, 500.0))
        self.assertNotEqual(A.resolve(anchor=second, now=1105.0)["center"], first.center())


if __name__ == "__main__":
    unittest.main()
