import unittest

from agent.skills import Skills


class DummyWire:
    def __init__(self):
        self.sent = []

    def send_command(self, cmd, args):
        req_id = f"req_{len(self.sent) + 1}"
        self.sent.append((cmd, args, req_id))
        return req_id


class SkillsTests(unittest.TestCase):
    def test_command_context_is_reported_to_observer(self):
        wire = DummyWire()
        skills = Skills(wire)
        observed = []
        skills.set_command_observer(lambda cmd, req_id, context, args: observed.append((cmd, req_id, context, args)))

        with skills.command_context("manual_chat"):
            skills.follow_player("Alice")

        self.assertEqual(observed[0][0], "follow_player")
        self.assertEqual(observed[0][2], "manual_chat")
        self.assertEqual(observed[0][3], {"player": "Alice"})

    def test_craft_item_sends_count(self):
        wire = DummyWire()
        skills = Skills(wire)

        skills.craft_item("wooden_sword", 3)

        self.assertEqual(wire.sent[0][0], "craft_item")
        self.assertEqual(wire.sent[0][1], {"item": "wooden_sword", "count": 3})


if __name__ == "__main__":
    unittest.main()
