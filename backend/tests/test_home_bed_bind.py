"""把床绑成家：走过去 → 右键一次 → 用原版重生点核对。

用户纠正的语义（这是他亲口指出的原版行为）：
    "最少要在这张床上右键点击一次，无论是什么时候 —— 哪怕不是晚上/不是雷暴，右键这张床它都会
     **优先设置重生点**，只有到正确的时间段右键才会顺便睡觉。"

所以这里测的是：**只有原版说"重生点就是这张床"才算成功**，其余一律如实失败，
并且**不会**每轮重下 move_to（那会把寻路进度清零 —— "有路却一步不动"的老毛病）。
"""

import unittest

from agent import home_bed


def _ctx(x=0.0, y=64.0, z=0.0, *, bound=None, home=None):
    context = {"player": {"x": x, "y": y, "z": z}}
    if bound is not None:
        context["home_bed_bound"] = bound
    if home is not None:
        context["home_bed"] = home
    return context


class HomeBedBindTests(unittest.TestCase):
    def test_start_normalizes_the_point(self):
        state = home_bed.start((851.4, 63.0, -2191.6), base="main")
        self.assertEqual((state["x"], state["y"], state["z"]), (851.4, 63.0, -2191.6))
        self.assertEqual(state["stage"], home_bed.STAGE_WALK)
        self.assertEqual(state["base"], "main")
        self.assertEqual(state["clicks"], 0)

    def test_walks_there_and_asks_for_move_to_only_once(self):
        state = home_bed.start((100.0, 64.0, 100.0))
        first = home_bed.step(state, _ctx(0.0, 64.0, 0.0))
        self.assertEqual(first["action"], "move_to")
        self.assertFalse(first["done"])
        second = home_bed.step(state, _ctx(10.0, 64.0, 10.0))
        self.assertEqual(second["action"], "", "路上不许每轮重下 move_to（会把寻路进度清零）")
        self.assertFalse(second["done"])

    def test_clicks_the_bed_once_it_is_close(self):
        state = home_bed.start((100.0, 64.0, 100.0))
        first = home_bed.step(state, _ctx(99.0, 64.0, 99.0))     # 就在床边
        self.assertEqual(first["action"], "use_on")
        self.assertEqual(state["clicks"], 1)
        self.assertIn("右键", first["note"])

    def test_success_only_when_the_vanilla_respawn_point_is_this_bed(self):
        state = home_bed.start((100.0, 64.0, 100.0))
        home_bed.step(state, _ctx(1.0, 64.0, 1.0))            # 走到 → 右键
        done = home_bed.step(state, _ctx(1.0, 64.0, 1.0, bound=True,
                                        home={"x": 100.0, "y": 64.0, "z": 100.0}))
        self.assertTrue(done["done"])
        self.assertTrue(done["ok"])
        self.assertIn("绑好了", done["note"])

    def test_another_bed_binding_does_not_count_as_ours(self):
        state = home_bed.start((100.0, 64.0, 100.0))
        home_bed.step(state, _ctx(1.0, 64.0, 1.0))
        step = home_bed.step(state, _ctx(1.0, 64.0, 1.0, bound=True,
                                         home={"x": 900.0, "y": 70.0, "z": 900.0}))
        self.assertFalse(step["done"], "绑的是别的床，不许当成自己这张绑好了")

    def test_reports_failure_honestly_instead_of_pretending(self):
        state = home_bed.start((100.0, 64.0, 100.0))
        context = _ctx(99.0, 64.0, 99.0, bound=False)     # 站在床边，但重生点始终不是这张床
        outcome = None
        for _ in range(home_bed.MAX_ROUNDS):
            outcome = home_bed.step(state, context)
            if outcome["done"]:
                break
        self.assertIsNotNone(outcome)
        self.assertTrue(outcome["done"])
        self.assertFalse(outcome["ok"])
        self.assertIn("右键", outcome["note"])
        self.assertNotIn("绑好了", outcome["note"])

    def test_missing_bound_field_is_never_success(self):
        state = home_bed.start((100.0, 64.0, 100.0))
        outcome = home_bed.step(state, _ctx(1.0, 64.0, 1.0))
        self.assertFalse(outcome["done"], "拿不到 home_bed_bound 时不许下结论")

    def test_gives_up_after_the_round_budget(self):
        state = home_bed.start((100.0, 64.0, 100.0))
        state["rounds"] = home_bed.MAX_ROUNDS
        outcome = home_bed.step(state, _ctx(500.0, 64.0, 500.0))
        self.assertTrue(outcome["done"])
        self.assertFalse(outcome["ok"])
        self.assertIn("超时", outcome["note"])

    def test_no_state_means_no_work(self):
        outcome = home_bed.step(None, _ctx())
        self.assertTrue(outcome["done"])
        self.assertFalse(outcome["ok"])

    def test_describe_is_honest(self):
        state = home_bed.start((100.0, 64.0, 100.0))
        home_bed.step(state, _ctx(1.0, 64.0, 1.0))
        self.assertTrue(home_bed.describe(state))


if __name__ == "__main__":
    unittest.main()
