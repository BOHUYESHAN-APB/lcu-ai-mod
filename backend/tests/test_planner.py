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


if __name__ == "__main__":
    unittest.main()
