"""一轮之内的闭环：未做步骤进 L2（**不盲发**）+ 首个动作失败后回灌一次让模型自己纠正。

用户口径 / 真机现象（2026-10-12）：说"在这附近找床" ⇒ 模型给的是 `[go_to_block, use_on]` 这类
**多步意图**，我们只发了第一步、后一步**静默消失** ⇒ "不找也不右键"；而且动作失败后模型
**完全不知道**，下一轮还选同一个错工具 ⇒ "时成时败"。

验收（本文件逐条覆盖）：
  ① 单候选 / 无候选两条既有路径**行为不变**；
  ② 候选 > 1 ⇒ 只下发第一个，其余**如实进 L2**（提醒，不是补发）；
  ③ 首个动作下发失败（含被权限门/语义门拦下）⇒ 同一轮内把**失败原话**当模型消息回灌**一次**；
  ④ 纠错轮再失败 / 抛异常 ⇒ **不再追问**，如实收尾（"没有执行"那句实话必须在）；
  ⑤ 新一轮的候选永远优先，**陈旧候选绝不被执行**（它只是一个提示）。
"""

import json
import unittest

from agent.planner import MAX_CORRECTION_ROUNDS, Planner


class DummyMemory:
    def build_context(self):
        return {}

    def get_recent_context(self, _count):
        return []


class DummySkills:
    """记录被调用的技能 —— 这份记录就是"到底往游戏里发了什么"的事实。"""

    def __init__(self):
        self.calls = []

    def go_to_block(self, block, radius=32, remember=True):
        self.calls.append(("go_to_block", block))

    def move_to(self, x, y, z):
        self.calls.append(("move_to", x, y, z))

    def remember_poi(self, name="", category="waypoint", x=None, y=None, z=None):
        self.calls.append(("remember_poi", name, category))


class FakeLLM:
    """按角色回固定内容；一个角色给 list 就**依次**取（用满后停在最后一个）。

    值可以是 Exception（那一轮调用直接抛）。`calls` 全量记录请求 —— 纠错轮的提示词、
    每轮的 L2 文本、以及"到底问了几次"都靠它来断言。
    """

    api_key = "test-key"

    def __init__(self, responses):
        self._responses = responses
        self.calls = []

    def chat(self, messages, agent=None, **kwargs):
        self.calls.append({"agent": agent, "messages": messages, "kwargs": kwargs})
        value = self._responses.get(agent)
        if isinstance(value, list):
            seen = sum(1 for call in self.calls if call["agent"] == agent) - 1
            value = value[min(seen, len(value) - 1)]
        if isinstance(value, Exception):
            raise value
        return {"content": value}

    def planner_calls(self):
        return [call for call in self.calls if call["agent"] == "planner"]

    @staticmethod
    def l2_of(call):
        """这次请求里的 L2（当轮动态 user 消息；纠错轮的回执是后面那条 user）。"""
        for message in call["messages"]:
            if message["role"] == "user":
                return message["content"]
        return ""


def _plan(reply, *candidates):
    return json.dumps({"reply": reply, "candidates": list(candidates)})


def _cand(tool, args=None, why=""):
    return {"tool": tool, "args": args or {}, "why": why}


class StepFeedbackTests(unittest.TestCase):
    def _planner_with(self, responses):
        llm = FakeLLM(responses)
        skills = DummySkills()
        planner = Planner(llm_service=llm, memory=DummyMemory(), skills=skills)
        return planner, llm, skills

    # ── ① 既有路径不变 ────────────────────────────────────────────

    def test_single_candidate_path_is_unchanged(self):
        """单候选：直接下发、**不叫第二个模型**、也不产生纠错轮（额度一次都没花）。"""
        planner, llm, skills = self._planner_with({
            "planner": _plan("我去床边看看。", _cand("go_to_block", {"block": "*_bed*"})),
        })

        reply = planner.plan_and_execute("Steve", "在这附近找床", {})

        self.assertEqual(skills.calls, [("go_to_block", "*_bed*")])
        self.assertEqual(len(llm.planner_calls()), 1, "单候选成功路径不该有第二次调用")
        self.assertNotIn("decider", [call["agent"] for call in llm.calls])
        decision = planner.get_status()["last_decision"]
        self.assertEqual(decision["source"], "planner")
        self.assertEqual(decision["tool"], "go_to_block")
        self.assertTrue(decision["executed"])
        self.assertEqual(decision["candidate_count"], 1)
        self.assertEqual(decision["pending_candidates"], 0)
        self.assertEqual(decision["correction_rounds"], 0)
        self.assertEqual(reply, "我去床边看看。")

    def test_no_candidate_path_is_unchanged(self):
        """无候选：只回话（`planner_only`）—— 不派发、不纠错、一个字都不许编。"""
        planner, llm, skills = self._planner_with({
            "planner": _plan("这附近我没看见床，你带路？"),
        })

        reply = planner.plan_and_execute("Steve", "在这附近找床", {})

        self.assertEqual(skills.calls, [])
        self.assertEqual(len(llm.planner_calls()), 1)
        decision = planner.get_status()["last_decision"]
        self.assertEqual(decision["source"], "planner_only")
        self.assertEqual(decision["reason"], "no candidates")
        self.assertEqual(decision["candidate_count"], 0)
        self.assertEqual(decision["pending_candidates"], 0)
        self.assertEqual(decision["correction_rounds"], 0)
        self.assertEqual(reply, "这附近我没看见床，你带路？")

    # ── ② 未做的候选进 L2（不盲发、不静默丢弃）─────────────────────

    def test_extra_candidates_go_into_l2_not_into_the_wire(self):
        """候选 3 个（`go_to_block` → `use_on` → `sleep_in_bed`）：只发第一个；
        其余**如实写进下一轮的 L2**，本轮**一条都别发**（够不着就 `use_on` 必然失败）。"""
        planner, llm, skills = self._planner_with({
            "planner": [
                _plan("我去床边。",
                      _cand("go_to_block", {"block": "*_bed*"}, why="先走到床边"),
                      _cand("use_on", {"target": "minecraft:white_bed"}, why="右键绑重生点"),
                      _cand("sleep_in_bed", {}, why="顺便睡一觉")),
                _plan("好了。"),
            ],
        })

        first = planner.plan_and_execute("Steve", "在这附近找床", {})

        self.assertEqual(skills.calls, [("go_to_block", "*_bed*")], "本轮只许发第一个")
        decision = planner.get_status()["last_decision"]
        self.assertEqual(decision["candidate_count"], 3)
        self.assertEqual(decision["pending_candidates"], 2)
        self.assertEqual(decision["executed"], True)
        self.assertEqual(decision["correction_rounds"], 0)
        self.assertEqual(first, "我去床边。")

        # 下一轮的 L2 里必须如实出现"还没做"的那两步（工具名 + 参数 + 当时的理由）。
        l2 = FakeLLM.l2_of(llm.planner_calls()[0])   # 第一轮的 L2（这时还没有待办）
        self.assertNotIn("还没做", l2)

        planner.plan_and_execute("Steve", "然后呢", {})
        l2_next = FakeLLM.l2_of(llm.planner_calls()[1])
        self.assertIn("上一轮你给的动作", l2_next)
        self.assertIn("go_to_block", l2_next)
        self.assertIn("已下发", l2_next)
        self.assertIn("use_on", l2_next)
        self.assertIn("minecraft:white_bed", l2_next)
        self.assertIn("sleep_in_bed", l2_next)
        self.assertIn("还没做", l2_next)
        self.assertIn("右键绑重生点", l2_next)        # 它当时的理由也如实带上
        self.assertIn("我**没有**替你补发", l2_next)   # 语义：只有提醒，没有补发
        self.assertEqual(skills.calls, [("go_to_block", "*_bed*")], "旧候选绝不许被执行")

    def test_new_round_candidates_win_over_stale_pending_steps(self):
        """新一轮给了别的候选 ⇒ 执行**新的**，陈旧候选只当提示（**永不**被补发执行）。"""
        planner, llm, skills = self._planner_with({
            "planner": [
                _plan("我先过去。",
                      _cand("go_to_block", {"block": "*_bed*"}),
                      _cand("use_on", {"target": "minecraft:white_bed"})),
                _plan("那我先记个点。", _cand("remember_poi", {"name": "床边", "category": "waypoint"})),
            ],
        })

        planner.plan_and_execute("Steve", "在这附近找床", {})
        planner.plan_and_execute("Steve", "先别动，记一下", {})

        self.assertEqual(skills.calls, [("go_to_block", "*_bed*"), ("remember_poi", "床边", "waypoint")])
        self.assertNotIn("use_on", [call[0] for call in skills.calls], "陈旧候选永不被补发")
        decision = planner.get_status()["last_decision"]
        self.assertEqual(decision["tool"], "remember_poi")
        self.assertEqual(decision["pending_candidates"], 0)
        # 陈旧候选确实**作为提示**出现在这一轮的 L2 里（不是被丢掉）。
        self.assertIn("use_on", FakeLLM.l2_of(llm.planner_calls()[1]))

    # ── ③ 失败回灌 + 有界纠错 ──────────────────────────────────────

    def test_first_failure_gets_exactly_one_correction_round_with_the_real_reason(self):
        """首个动作失败（缺参数 ⇒ 工具自己如实拒）⇒ 同一轮内**一次**纠错；
        回执里必须有工具名、参数、**失败原话**，且要求"换做法或说清不能做"；
        纠错轮给出别的工具 ⇒ 立刻执行它。"""
        planner, llm, skills = self._planner_with({
            "planner": [
                _plan("我给你盖个房子。",
                      _cand("build", {"x": 1, "y": 64, "z": 1, "structure": "house"}, why="用户要房子")),
                _plan("那我先把位置记下来，材料我再点清楚。",
                      _cand("remember_poi", {"name": "工地", "category": "waypoint"}, why="先记下位置")),
            ],
        })

        reply = planner.plan_and_execute("Steve", "帮我盖个房子", {})

        self.assertEqual(skills.calls, [("remember_poi", "工地", "waypoint")])
        planner_calls = llm.planner_calls()
        self.assertEqual(len(planner_calls), 2, "失败 ⇒ 恰好一次纠错轮")
        receipt = planner_calls[1]["messages"][-1]
        self.assertEqual(receipt["role"], "user")
        self.assertIn("没有下发成功", receipt["content"])
        self.assertIn("build", receipt["content"])
        self.assertIn('"structure": "house"', receipt["content"])      # 参数原样
        self.assertIn("得点名 material", receipt["content"])           # 失败**原话**
        self.assertIn("要么换一个做法", receipt["content"])
        self.assertIn("说清为什么不能做", receipt["content"])
        # 纠错轮续的是**同一套**提示词（L0/L1 一个字节都不改），不是新造一条通道。
        self.assertEqual(planner_calls[1]["messages"][0], planner_calls[0]["messages"][0])
        self.assertEqual(planner_calls[1]["messages"][1], planner_calls[0]["messages"][1])
        self.assertTrue(planner_calls[1]["kwargs"].get("tools"))
        self.assertIn("那我先把位置记下来", reply)
        self.assertNotIn("没有执行", reply, "纠错轮真做成了，就不该补那句实话")
        decision = planner.get_status()["last_decision"]
        self.assertEqual(decision["tool"], "remember_poi")
        self.assertTrue(decision["executed"])
        self.assertEqual(decision["correction_rounds"], 1)
        self.assertEqual(decision["candidate_count"], 1)
        self.assertEqual(decision["pending_candidates"], 0)

    def test_permission_gate_refusal_is_fed_back_too(self):
        """被**权限门**拦下同样算"派发失败"（真机最常走到的路径）⇒ 必须把原话回灌。"""
        planner, llm, skills = self._planner_with({
            "planner": [
                _plan("我造把木剑。", _cand("craft_item", {"item": "wooden_sword", "count": 1})),
                _plan("那我先不动手。"),
            ],
        })

        reply = planner.plan_and_execute("Steve", "做个木剑", {})

        self.assertEqual(skills.calls, [])
        self.assertEqual(len(llm.planner_calls()), 2)
        receipt = llm.planner_calls()[1]["messages"][-1]["content"]
        self.assertIn("craft_item", receipt)
        self.assertIn("没被允许让我造东西", receipt)      # 权限门自己的原话
        self.assertIn("没有下发成功", receipt)
        # 纠错轮改口"不动手" ⇒ 用它的话收尾，并如实说明没执行。
        self.assertIn("那我先不动手。", reply)
        self.assertIn("没有执行", reply)
        self.assertFalse(planner.get_status()["last_decision"]["executed"])

    def test_correction_round_failure_ends_the_round_honestly(self):
        """纠错轮又失败 ⇒ **不再有第三次调用**（上限就是 `MAX_CORRECTION_ROUNDS`），如实收尾。"""
        planner, llm, skills = self._planner_with({
            "planner": [
                _plan("我记一下。", _cand("go_to_block", {})),          # 缺 block ⇒ 工具自己拒
                _plan("我再记一下。", _cand("go_to_block", {})),        # 还是同一条 ⇒ 再失败
                _plan("第三次。", _cand("remember_poi", {"name": "x"})),  # 不该被问到
            ],
        })

        reply = planner.plan_and_execute("Steve", "记一下", {})

        self.assertEqual(len(llm.planner_calls()), 1 + MAX_CORRECTION_ROUNDS,
                         "每轮最多 1 次纠错，不许无限重试")
        self.assertEqual(skills.calls, [])
        self.assertIn("没有执行", reply)
        self.assertIn("我再记一下。", reply)     # 纠错轮的话照样说出去（只是被补了实话）
        decision = planner.get_status()["last_decision"]
        self.assertFalse(decision["executed"])
        self.assertEqual(decision["correction_rounds"], 1)

    def test_correction_round_exception_ends_the_round_honestly(self):
        """纠错轮自己超时/报错 ⇒ 记一条日志、如实收尾，**绝不**假装成功。"""
        planner, llm, skills = self._planner_with({
            "planner": [
                _plan("我把面包拿出来。", _cand("totally_unknown_tool", {"item": "bread"})),
                TimeoutError("llm timeout"),
            ],
        })

        reply = planner.plan_and_execute("Steve", "把面包拿出来", {})

        self.assertEqual(len(llm.planner_calls()), 2)
        self.assertEqual(skills.calls, [])
        self.assertIn("没有执行", reply)
        self.assertIn("工具表里没有", reply)     # 未登记的工具 ⇒ 如实说清，不编更细的理由
        self.assertFalse(planner.get_status()["last_decision"]["executed"])


if __name__ == "__main__":
    unittest.main()
