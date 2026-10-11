import json
import unittest

from agent.planner import Planner


class DummyMemory:
    def build_context(self):
        return {}

    def get_recent_context(self, _count):
        return []


class DummySkills:
    def __init__(self):
        self.calls = []

    def craft_item(self, item_name, count=1):
        self.calls.append(("craft_item", item_name, count))

    def follow_player(self, player_name):
        self.calls.append(("follow_player", player_name))

    def collect_blocks(self, block_type, count=1):
        self.calls.append(("collect_blocks", block_type, count))

    def find_block(self, block, radius=32, limit=8):
        self.calls.append(("find_block", block, radius, limit))

    def go_to_block(self, block, radius=32, remember=True):
        self.calls.append(("go_to_block", block, radius, remember))

    def sleep_in_bed(self):
        self.calls.append(("sleep_in_bed",))

    def remember_poi(self, name="", category="waypoint", x=None, y=None, z=None):
        self.calls.append(("remember_poi", name, category, x, y, z))


class FakeLLM:
    """Returns a canned response per agent; an Exception value is raised instead."""

    def __init__(self, responses):
        self.api_key = "test-key"
        self._responses = responses
        self.calls = []

    def chat(self, messages, agent=None, **kwargs):
        self.calls.append({"agent": agent, "kwargs": kwargs})
        value = self._responses.get(agent)
        if isinstance(value, Exception):
            raise value
        return {"content": value}


class PlannerTests(unittest.TestCase):
    def test_duplicate_active_craft_is_not_redispatched(self):
        skills = DummySkills()
        planner = Planner(llm_service=None, memory=DummyMemory(), skills=skills)

        planner._execute_plan(
            "craft(wooden_sword, 1)",
            sender="BoHuYeShan",
            message="做个木剑",
            context={"task_state": {"kind": "craft", "status": "collecting", "target": "wooden_sword"}},
        )

        self.assertEqual(skills.calls, [])

    def test_configured_threshold_gates_dispatch(self):
        """M3-4：阈值可注入并即时生效——0.95 拦下 0.8 的决策，0.5 放行。"""
        candidates = json.dumps({
            "reply": "好",
            "candidates": [
                {"tool": "collect_blocks", "args": {"block_type": "oak_log", "count": 4}, "why": "缺木头"},
            ],
        })
        decider = '{"choice": 0, "confidence": 0.8}'

        strict_skills = DummySkills()
        strict = Planner(
            llm_service=FakeLLM({"planner": candidates, "decider": decider}),
            memory=DummyMemory(), skills=strict_skills, threshold_provider=lambda: 0.95)
        strict.plan_and_execute("Steve", "砍点木头", {})

        self.assertEqual(strict_skills.calls, [])
        self.assertEqual(strict.get_status()["last_decision"]["threshold"], 0.95)

        loose_skills = DummySkills()
        loose = Planner(
            llm_service=FakeLLM({"planner": candidates, "decider": decider}),
            memory=DummyMemory(), skills=loose_skills, threshold_provider=lambda: 0.5)
        loose.plan_and_execute("Steve", "砍点木头", {})

        self.assertEqual(loose_skills.calls, [("collect_blocks", "oak_log", 4)])

    def test_threshold_provider_failure_falls_back_to_default(self):
        def boom():
            raise RuntimeError("config unavailable")

        planner = Planner(llm_service=None, memory=DummyMemory(), skills=DummySkills(),
                          threshold_provider=boom)

        self.assertEqual(planner._threshold(), 0.6)

    def _planner_with(self, skills, planner_output, decider_output):
        llm = FakeLLM({"planner": planner_output, "decider": decider_output})
        planner = Planner(llm_service=llm, memory=DummyMemory(), skills=skills)
        return planner, llm

    def test_decider_selects_candidate_and_dispatches(self):
        skills = DummySkills()
        planner, _ = self._planner_with(
            skills,
            json.dumps({
                "reply": "来了",
                "candidates": [
                    {"tool": "collect_blocks", "args": {"block_type": "oak_log", "count": 4}, "why": "缺木头"},
                    {"tool": "follow", "args": {"player": "Steve"}, "why": "跟随玩家"},
                ],
            }),
            '{"choice": 1, "confidence": 0.91}',
        )

        reply = planner.plan_and_execute("Steve", "跟着我", {})

        self.assertEqual(reply, "来了")
        self.assertEqual(skills.calls, [("follow_player", "Steve")])
        self.assertEqual(planner.get_status()["last_decision"]["choice"], 1)

    def test_low_confidence_does_not_dispatch(self):
        skills = DummySkills()
        planner, _ = self._planner_with(
            skills,
            json.dumps({
                "reply": "嗯",
                "candidates": [{"tool": "follow", "args": {"player": "Steve"}, "why": "跟随"}],
            }),
            '{"choice": 0, "confidence": 0.2}',
        )

        reply = planner.plan_and_execute("Steve", "跟着我", {})

        self.assertEqual(reply, "嗯")
        self.assertEqual(skills.calls, [])
        self.assertEqual(planner.get_status()["last_decision"]["confidence"], 0.2)

    def test_decider_failure_does_not_dispatch(self):
        skills = DummySkills()
        planner, _ = self._planner_with(
            skills,
            json.dumps({
                "reply": "好",
                "candidates": [{"tool": "follow", "args": {"player": "Steve"}, "why": "跟随"}],
            }),
            TimeoutError("decider timed out"),
        )

        reply = planner.plan_and_execute("Steve", "跟着我", {})

        self.assertEqual(reply, "好")
        self.assertEqual(skills.calls, [])
        self.assertIsNone(planner.get_status()["last_decision"]["choice"])

    def test_decider_is_called_with_short_timeout_and_no_reasoning_budget(self):
        skills = DummySkills()
        planner, llm = self._planner_with(
            skills,
            json.dumps({"reply": "", "candidates": [
                {"tool": "follow", "args": {"player": "Steve"}, "why": "跟随"}]}),
            '{"choice": 0, "confidence": 0.9}',
        )

        planner.plan_and_execute("Steve", "跟着我", {})

        decider_call = next(c for c in llm.calls if c["agent"] == "decider")
        self.assertEqual(decider_call["kwargs"]["max_tokens"], 32)
        self.assertIn("timeout", decider_call["kwargs"])
        # 零思考：decider 明确不发思考模式/深度，直接要一行 JSON。
        self.assertIs(decider_call["kwargs"]["thinking"], False)

    def test_non_json_output_falls_back_to_legacy_parser(self):
        skills = DummySkills()
        planner, _ = self._planner_with(skills, 'tool(follow, {"player": "Steve"})', None)

        planner.plan_and_execute("Steve", "跟着我", {})

        self.assertEqual(skills.calls, [("follow_player", "Steve")])

    def test_prompt_injects_vision_description(self):
        planner = Planner(llm_service=None, memory=DummyMemory(), skills=DummySkills())

        with_vision = planner._build_planner_prompt("Steve", "hi", {"vision": "前方有岩浆"}, "AI")
        without_vision = planner._build_planner_prompt("Steve", "hi", {}, "AI")

        self.assertIn("前方有岩浆", with_vision)
        self.assertIn("画面观察", with_vision)
        self.assertIn("无（视觉未开启）", without_vision)

    def test_prompt_injects_known_poi(self):
        planner = Planner(llm_service=None, memory=DummyMemory(), skills=DummySkills())

        prompt = planner._build_planner_prompt(
            "Steve", "hi", {"known_poi": "minecraft:chest@(10,64,20)"}, "AI")

        self.assertIn("已知箱子", prompt)
        self.assertIn("minecraft:chest@(10,64,20)", prompt)

    def test_prompt_injects_time_beds_and_remembered_pois(self):
        planner = Planner(llm_service=None, memory=DummyMemory(), skills=DummySkills())

        prompt = planner._build_planner_prompt("Steve", "hi", {
            "is_night": True,
            "day_time": 15000,
            "can_sleep": True,
            "nearby_beds": [
                {"id": "minecraft:red_bed", "x": 3, "y": 64, "z": 5, "distance": 2.5},
            ],
            "remembered_pois": [
                {"name": "home", "category": "waypoint", "x": 100, "y": 65, "z": -20},
            ],
        }, "AI")

        self.assertIn("是夜晚", prompt)
        self.assertIn("15000", prompt)
        self.assertIn("现在能睡觉", prompt)
        self.assertIn("minecraft:red_bed@(3,64,5)", prompt)
        self.assertIn("home[waypoint]@(100,65,-20)", prompt)

    def test_prompt_defaults_time_and_bed_blocks_to_placeholders(self):
        planner = Planner(llm_service=None, memory=DummyMemory(), skills=DummySkills())

        prompt = planner._build_planner_prompt("Steve", "hi", {}, "AI")

        self.assertIn("是白天", prompt)
        self.assertIn("现在不能睡觉", prompt)
        self.assertIn("附近的床：暂无", prompt)
        self.assertIn("已知记录点（你自己记住的持久记忆，可能在很远的地方）：\n暂无", prompt)

    def test_prompt_reads_night_fields_from_world_when_top_level_absent(self):
        planner = Planner(llm_service=None, memory=DummyMemory(), skills=DummySkills())

        prompt = planner._build_planner_prompt(
            "Steve", "hi", {"world": {"is_night": True, "day_time": 17000, "can_sleep": True}}, "AI")

        self.assertIn("是夜晚", prompt)
        self.assertIn("17000", prompt)

    def test_prompt_rules_cover_night_locator_and_memory(self):
        planner = Planner(llm_service=None, memory=DummyMemory(), skills=DummySkills())

        prompt = planner._build_planner_prompt("Steve", "hi", {}, "AI")

        self.assertIn("给 sleep_in_bed", prompt)
        self.assertIn("用 go_to_block", prompt)
        self.assertIn("不要用 explore", prompt)
        self.assertIn("用 remember_poi", prompt)
        self.assertIn("回复必须和实际动作一致", prompt)


class PlannerLocatorToolTests(unittest.TestCase):
    """find_block / go_to_block / sleep_in_bed / remember_poi 的分发与校验。"""

    def _planner(self):
        skills = DummySkills()
        return skills, Planner(llm_service=None, memory=DummyMemory(), skills=skills)

    def test_find_block_normalizes_chinese_alias(self):
        skills, planner = self._planner()

        self.assertTrue(planner._dispatch_tool_call(
            "find_block", {"block": "箱子", "radius": 24, "limit": 5}, {}))

        self.assertEqual(skills.calls, [("find_block", "*chest*", 24, 5)])

    def test_find_block_rejects_empty_block(self):
        skills, planner = self._planner()

        self.assertFalse(planner._dispatch_tool_call("find_block", {"block": "   "}, {}))
        self.assertFalse(planner._dispatch_tool_call("find_block", {}, {}))
        self.assertEqual(skills.calls, [])

    def test_go_to_block_defaults_remember_true_and_passes_radius(self):
        skills, planner = self._planner()

        self.assertTrue(planner._dispatch_tool_call("go_to_block", {"block": "床"}, {}))

        self.assertEqual(skills.calls, [("go_to_block", "*_bed", 32, True)])

    def test_go_to_block_accepts_explicit_radius_and_remember(self):
        skills, planner = self._planner()

        planner._dispatch_tool_call(
            "go_to_block", {"block": "minecraft:furnace", "radius": 12, "remember": False}, {})

        self.assertEqual(skills.calls, [("go_to_block", "minecraft:furnace", 12, False)])

    def test_go_to_block_rejects_empty_block(self):
        skills, planner = self._planner()

        self.assertFalse(planner._dispatch_tool_call("go_to_block", {"block": ""}, {}))
        self.assertEqual(skills.calls, [])

    def test_go_to_block_skips_duplicate_active_task(self):
        skills, planner = self._planner()

        dispatched = planner._dispatch_tool_call(
            "go_to_block", {"block": "箱子"},
            {"task_state": {"kind": "go_to_block", "status": "moving", "target": "*chest*"}})

        self.assertFalse(dispatched)
        self.assertEqual(skills.calls, [])

    def test_sleep_in_bed_dispatches(self):
        skills, planner = self._planner()

        self.assertTrue(planner._dispatch_tool_call("sleep_in_bed", {}, {}))

        self.assertEqual(skills.calls, [("sleep_in_bed",)])

    def test_remember_poi_defaults_to_current_feet(self):
        skills, planner = self._planner()

        self.assertTrue(planner._dispatch_tool_call("remember_poi", {"name": "home"}, {}))

        self.assertEqual(skills.calls, [("remember_poi", "home", "waypoint", None, None, None)])

    def test_remember_poi_passes_coordinates_and_category(self):
        skills, planner = self._planner()

        planner._dispatch_tool_call(
            "remember_poi", {"name": "矿洞", "category": "resource", "x": 1, "y": 62, "z": -3}, {})

        self.assertEqual(
            skills.calls, [("remember_poi", "矿洞", "resource", 1.0, 62.0, -3.0)])

    def test_block_aliases_use_wildcards_for_beds_and_containers(self):
        from agent.planner import BLOCK_ALIASES

        self.assertEqual(BLOCK_ALIASES["床"], "*_bed", "床必须用通配，避免匹配到 bedrock")
        self.assertEqual(BLOCK_ALIASES["箱子"], "*chest*")
        self.assertEqual(BLOCK_ALIASES["熔炉"], "*furnace")
        self.assertEqual(BLOCK_ALIASES["工作台"], "*crafting_table*")
        self.assertEqual(BLOCK_ALIASES["水"], "minecraft:water")
        self.assertEqual(BLOCK_ALIASES["木头"], "oak_log", "木头保留原值")
        self.assertNotIn("#minecraft:beds", BLOCK_ALIASES.values())


class PlannerPromptSlimmingTests(unittest.TestCase):
    """M3-3 验收：只注入当前阶段相关动作，且动作清单显著变短。"""

    def _prompt(self, task_state):
        planner = Planner(llm_service=None, memory=DummyMemory(), skills=DummySkills())
        return planner._build_planner_prompt("Steve", "hi", {"task_state": task_state}, "AI")

    def test_craft_phase_excludes_combat_actions(self):
        prompt = self._prompt({"kind": "craft", "status": "crafting"})

        self.assertIn("- craft_item(", prompt)
        self.assertNotIn("- attack", prompt)

    def test_idle_phase_includes_combat_actions(self):
        prompt = self._prompt({"kind": "idle", "status": "idle"})

        self.assertIn("- attack", prompt)

    def test_static_block_stays_within_prompt_budget(self):
        """静态指令块预算：防止无节制内联把 32k 上下文挤爆。

        历史基线（旧口径：user 提示里"可用动作（"到结尾的那一段 = 动作表 + 规则 + JSON 骨架）：
        改动前 1205 字符（16 条动作 + 10 条规则 + JSON 骨架），当时 idle 实测 762；新增 4 个
        定位/记忆/睡觉工具后 1157；再新增守卫/HUD/`/locate` 四个工具与 4 条语境规则后 1861；
        最后新增借用台账两个工具 + 2 条借用规则后 idle 实测 **2392**，预算一度抬到 2750。

        **现在换成新口径**：静态块 = 真正发出去、要求逐字节不变的 **L0+L1 系统前缀**
        （契约 + 人设 + 规则 + 机制说明 + 借用授权）。工具表已改走 API `tools` 参数、
        JSON 骨架已并入 L0，静态块本身**只降不抬**：
          旧值 IDLE_STATIC_BUDGET = 2750（旧口径实测 2392）
          新值 IDLE_STATIC_BUDGET = 1700（新口径实测：上线时 1535，领地/区域/造床三条规则后 1678）
        """
        IDLE_STATIC_BUDGET = 1700

        # 最坏情况是 idle（`_build_l1` 与阶段无关，这里取的正是发出去的 system 前缀）。
        planner = Planner(llm_service=None, memory=DummyMemory(), skills=DummySkills())
        layers = planner.build_prompt_layers(
            "Steve", "hi", {"task_state": {"kind": "idle", "status": "idle"}}, "AI")

        self.assertLess(len(layers.system), IDLE_STATIC_BUDGET)
        # 工具表不再内联进提示词（改走 tools 参数）。
        self.assertNotIn("可用动作（", layers.system + layers.user)
        self.assertTrue(layers.tools)

    def test_phase_filtering_removes_unrelated_actions(self):
        from agent.planner import _actions_for_phase

        idle = [name for name, _, _ in _actions_for_phase("idle")]
        craft = [name for name, _, _ in _actions_for_phase("craft")]

        self.assertLess(len(craft), len(idle))
        self.assertIn("craft_item", craft)
        self.assertNotIn("attack", craft)

    def test_phase_mapping(self):
        from agent.planner import _phase_from_task

        self.assertEqual(_phase_from_task(None), "idle")
        self.assertEqual(_phase_from_task({"kind": "craft", "status": "idle"}), "idle")
        self.assertEqual(_phase_from_task({"kind": "collect", "status": "collecting"}), "gather")
        self.assertEqual(_phase_from_task({"kind": "follow", "status": "running"}), "travel")
        self.assertEqual(_phase_from_task({"kind": "eat", "status": "running"}), "survive")
        self.assertEqual(_phase_from_task({"kind": "bogus", "status": "running"}), "idle")

    def test_stop_action_available_in_every_phase(self):
        from agent.planner import PHASE_GROUPS, _actions_for_phase

        for phase in PHASE_GROUPS:
            names = [name for name, _, _ in _actions_for_phase(phase)]
            self.assertIn("stop", names, f"{phase} 阶段缺少 stop")

    def test_locator_tools_available_in_idle_gather_travel(self):
        from agent.planner import _actions_for_phase

        for phase in ("idle", "gather", "travel"):
            names = [name for name, _, _ in _actions_for_phase(phase)]
            self.assertIn("go_to_block", names, f"{phase} 阶段缺少 go_to_block")
            self.assertIn("find_block", names, f"{phase} 阶段缺少 find_block")

    def test_night_and_memory_tools_available_in_every_phase(self):
        from agent.planner import PHASE_GROUPS, _actions_for_phase

        for phase in PHASE_GROUPS:
            names = [name for name, _, _ in _actions_for_phase(phase)]
            self.assertIn("sleep_in_bed", names, f"{phase} 阶段缺少 sleep_in_bed")
            self.assertIn("remember_poi", names, f"{phase} 阶段缺少 remember_poi")


if __name__ == "__main__":
    unittest.main()
