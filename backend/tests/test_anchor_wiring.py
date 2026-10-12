"""锚点接进 prompt 的行为回归：**「这里」= 用户说那句话时他站的位置**。

用户口径（原话）："用户说『来到这里』，那么 AI 就会探查用户说的话这一瞬间，
用户所在的位置，然后到这个位置。如果后面附加『在附近干什么』，那么就基于这个点，
然后再按照一定的半径去找对应的方块或者对应的东西。"

`agent/anchor.py` 只管"怎么解析圆心"；本文件管**它有没有真的进模型看到的那份 prompt**，
以及三种情况都不许自欺：
  ① 新鲜锚点 ⇒ 明说「这里」就是他那时的位置，且点明**不是**我自己的位置；
  ② 过期锚点 ⇒ 明说已经过期、别拿旧坐标当他现在的站位；
  ③ **没有时间戳** ⇒ 只报位置，**不许编"刚刚"**（宁可不说过期，也不假装新鲜）。
"""

import time
import unittest

from agent import anchor
from agent.planner import Planner


class DummyMemory:
    def build_context(self):
        return {}

    def get_recent_context(self, _count=5):
        return []


class DummySkills:
    def __getattr__(self, _item):
        def recorder(*_args, **_kwargs):
            return {"success": True}
        return recorder


def prompt_with(last_speaker) -> str:
    planner = Planner(None, DummyMemory(), DummySkills())
    context = {"last_speaker": dict(last_speaker)}
    return planner._build_planner_prompt("Alice", "来到这里", context, "AI")


def speaker(**overrides) -> dict:
    base = {"name": "BoHuYeShan", "message": "来到这里", "time": time.time(),
            "x": 851.0, "y": 63.0, "z": -2189.0, "distance": 4.2}
    base.update(overrides)
    return base


class AnchorPromptTests(unittest.TestCase):

    def test_a_fresh_anchor_names_the_spot_and_says_whose_it_is(self):
        text = prompt_with(speaker())
        self.assertIn("最近发话者：BoHuYeShan", text)
        self.assertIn("(851, 63, -2189)", text)          # 他说话那一刻的坐标
        self.assertIn("圆心", text)
        self.assertIn("不是", text)                      # 明确"不是我站的地方"
        self.assertIn("move_to", text)                   # 想去就先去那儿再扫

    def test_the_anchor_never_becomes_my_own_position(self):
        text = prompt_with(speaker())
        self.assertNotIn("圆心**就是我自己", text)
        self.assertIn("「这里」「这附近」的圆心**就是它**", text)

    def test_a_stale_anchor_is_called_out_instead_of_kept_silent(self):
        old = time.time() - (anchor.MAX_AGE_SECONDS + 120.0)
        text = prompt_with(speaker(time=old))
        self.assertIn("已经过期", text)
        self.assertIn("别把这个旧坐标当成他现在的位置", text)
        self.assertNotIn("圆心**就是它**", text)

    def test_without_a_timestamp_it_reports_the_spot_and_admits_it_cannot_age_it(self):
        text = prompt_with(speaker(time=None))
        self.assertIn("(851, 63, -2189)", text)
        self.assertIn("没带时间戳", text)
        self.assertNotIn("他刚说这句话时", text)          # 不许编"刚刚"
        self.assertNotIn("圆心**就是它**", text)          # 不知道新鲜度就不敢打包票

    def test_garbage_coordinates_do_not_become_a_fake_spot(self):
        text = prompt_with(speaker(x="远处", z=None))
        self.assertIn("最近发话者：BoHuYeShan", text)
        self.assertNotIn("(851", text)
        self.assertNotIn("(0, 0, 0)", text)              # 绝不退化成原点

    def test_the_center_rule_reaches_the_prompt(self):
        text = prompt_with(speaker())
        self.assertIn("扫的是**我自己周围**", text)
        self.assertIn("先 `move_to` 到他那个坐标、再扫", text)


if __name__ == "__main__":
    unittest.main()
