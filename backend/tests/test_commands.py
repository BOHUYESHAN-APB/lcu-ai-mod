import unittest

from agent.commands import Commands


class DummySkills:
    def __init__(self):
        self.calls = []

    def collect_blocks(self, block_type, count=1):
        self.calls.append(("collect_blocks", block_type, count))

    def craft_item(self, item_name, count=1):
        self.calls.append(("craft_item", item_name, count))

    def place_block(self):
        self.calls.append(("place_block",))

    def stop_all(self):
        self.calls.append(("stop_all",))

    def follow_player(self, player_name):
        self.calls.append(("follow_player", player_name))


class CommandsTests(unittest.TestCase):
    def test_action_aliases_are_case_insensitive(self):
        skills = DummySkills()
        commands = Commands(skills)

        commands.parse_and_execute("!followPlayer Alice", {})

        self.assertIn(("follow_player", "Alice"), skills.calls)

    def test_collect_craft_place_and_stop_route_to_skills(self):
        skills = DummySkills()
        commands = Commands(skills)

        commands.parse_and_execute("!collect oak_log 3 !craft wooden_sword !place 1 2 3 stone !stop", {})

        self.assertIn(("collect_blocks", "oak_log", 3), skills.calls)
        self.assertIn(("craft_item", "wooden_sword", 1), skills.calls)
        self.assertIn(("place_block",), skills.calls)
        self.assertIn(("stop_all",), skills.calls)


if __name__ == "__main__":
    unittest.main()
