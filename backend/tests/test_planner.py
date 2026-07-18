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

    def stop_all(self):
        self.calls.append(("stop_all",))


class PlannerTests(unittest.TestCase):
    def test_external_context_is_included_in_main_planner_prompt(self):
        planner = Planner(llm_service=None, memory=DummyMemory(), skills=DummySkills())

        prompt = planner._build_planner_prompt(
            sender="owner",
            message="hello",
            context={
                "persona": {
                    "name": "Maid",
                    "external_context": {"stream": "live", "mood": "focused"},
                }
            },
            bot_name="AI",
        )

        self.assertIn("上游集成上下文", prompt)
        self.assertIn("stream", prompt)
        self.assertIn("focused", prompt)

    def test_structured_memory_context_is_included_in_prompt(self):
        planner = Planner(llm_service=None, memory=DummyMemory(), skills=DummySkills())

        prompt = planner._build_planner_prompt(
            sender="Alice",
            message="继续",
            context={
                "relationship_summary": "Alice: tasks=2, success=1",
                "task_outcomes": "craft_item stone_pickaxe -> success",
                "world_experience": "server=example.org, world=survival",
            },
            bot_name="AI",
        )

        self.assertIn("Alice: tasks=2", prompt)
        self.assertIn("craft_item stone_pickaxe -> success", prompt)
        self.assertIn("server=example.org", prompt)

    def test_prompt_clips_untrusted_message_and_memory_sections(self):
        planner = Planner(llm_service=None, memory=DummyMemory(), skills=DummySkills())

        prompt = planner._build_planner_prompt(
            sender="A" * 500,
            message="M" * 10000,
            context={
                "interaction_summary": "R" * 10000,
                "relationship_summary": "P" * 10000,
                "task_outcomes": "T" * 10000,
                "world_experience": "W" * 10000,
                "persona": {"external_context": {"payload": "X" * 10000}},
            },
            bot_name="AI",
        )

        self.assertLess(len(prompt), 20000)
        self.assertNotIn("M" * 1001, prompt)
        self.assertNotIn("R" * 1501, prompt)

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

    def test_prompt_requires_action_requests_to_include_executable_tool(self):
        planner = Planner(llm_service=None, memory=DummyMemory(), skills=DummySkills())

        prompt = planner._build_planner_prompt("Alice", "做一个铁镐", {}, "AI")

        self.assertIn('tool(craft_item, {"item":"minecraft:iron_pickaxe","count":1})', prompt)
        self.assertIn("不能只答应而不调用工具", prompt)

    def test_structured_craft_tool_normalizes_chinese_item(self):
        skills = DummySkills()
        planner = Planner(llm_service=None, memory=DummyMemory(), skills=skills)

        planner._execute_plan(
            'reply(马上)\ntool(craft_item, {"item":"铁镐","count":1})',
            sender="Alice",
            message="做一个铁镐",
            context={},
        )

        self.assertEqual(skills.calls, [("craft_item", "iron_pickaxe", 1)])

    def test_legacy_structured_craft_name_and_item_name_remain_executable(self):
        skills = DummySkills()
        planner = Planner(llm_service=None, memory=DummyMemory(), skills=skills)

        planner._execute_plan(
            'tool(craft, {"item_name":"iron_pickaxe","count":2})',
            sender="Alice",
            message="做两个铁镐",
            context={},
        )

        self.assertEqual(skills.calls, [("craft_item", "iron_pickaxe", 2)])

    def test_structured_tool_is_not_executed_again_by_legacy_parser(self):
        skills = DummySkills()
        planner = Planner(llm_service=None, memory=DummyMemory(), skills=skills)

        planner._execute_plan(
            'tool(follow, {"player":"Alice"})\nfollow(Alice)',
            sender="Alice", message="跟着我", context={},
        )

        self.assertEqual(skills.calls, [("follow_player", "Alice")])

    def test_direct_follow_fallback_uses_sender_for_follow_me(self):
        skills = DummySkills()
        planner = Planner(llm_service=None, memory=DummyMemory(), skills=skills)

        executed = planner._execute_direct_intent_fallback("MixedCase_Player", "跟着我", {})

        self.assertTrue(executed)
        self.assertEqual(skills.calls, [("follow_player", "MixedCase_Player")])

    def test_direct_follow_fallback_canonicalizes_online_player_case(self):
        skills = DummySkills()
        planner = Planner(llm_service=None, memory=DummyMemory(), skills=skills)

        executed = planner._execute_direct_intent_fallback(
            "owner", "跟随 mixedcase_player",
            {"online_players": [{"name": "MixedCase_Player"}]},
        )

        self.assertTrue(executed)
        self.assertEqual(skills.calls, [("follow_player", "MixedCase_Player")])

    def test_direct_craft_fallback_executes_when_model_only_replies(self):
        skills = DummySkills()
        planner = Planner(llm_service=None, memory=DummyMemory(), skills=skills)

        executed = planner._execute_direct_intent_fallback("owner", "帮我做一个铁镐", {})

        self.assertTrue(executed)
        self.assertEqual(skills.calls, [("craft_item", "iron_pickaxe", 1)])


if __name__ == "__main__":
    unittest.main()
