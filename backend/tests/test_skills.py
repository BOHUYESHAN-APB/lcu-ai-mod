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

    def test_find_block_sends_block_radius_limit(self):
        wire = DummyWire()
        skills = Skills(wire)

        skills.find_block("minecraft:white_bed", 48, 4)

        self.assertEqual(
            wire.sent[0],
            ("find_block", {"block": "minecraft:white_bed", "radius": 48, "limit": 4}, "req_1"),
        )

    def test_find_block_defaults(self):
        wire = DummyWire()
        skills = Skills(wire)

        skills.find_block("*_bed")

        self.assertEqual(wire.sent[0][1], {"block": "*_bed", "radius": 32, "limit": 8})

    def test_go_to_block_defaults_remember_true(self):
        wire = DummyWire()
        skills = Skills(wire)

        skills.go_to_block("*chest*")

        self.assertEqual(wire.sent[0][0], "go_to_block")
        self.assertEqual(wire.sent[0][1], {"block": "*chest*", "radius": 32, "remember": True})

    def test_go_to_block_can_disable_remember(self):
        wire = DummyWire()
        skills = Skills(wire)

        skills.go_to_block("minecraft:furnace", radius=16, remember=False)

        self.assertEqual(
            wire.sent[0][1], {"block": "minecraft:furnace", "radius": 16, "remember": False})

    def test_sleep_in_bed_reuses_existing_sleep_command(self):
        """技能叫 sleep_in_bed，但 wire 命令是已有的 `sleep`（Java 侧会寻路 + 点床）。"""
        wire = DummyWire()
        skills = Skills(wire)

        skills.sleep_in_bed()

        self.assertEqual(wire.sent[0], ("sleep", {}, "req_1"))

    def test_remember_poi_omits_missing_coordinates(self):
        wire = DummyWire()
        skills = Skills(wire)

        skills.remember_poi("home", "waypoint")

        self.assertEqual(wire.sent[0][0], "remember_poi")
        self.assertEqual(wire.sent[0][1], {"name": "home", "category": "waypoint"})
        self.assertNotIn("x", wire.sent[0][1])
        self.assertNotIn("y", wire.sent[0][1])
        self.assertNotIn("z", wire.sent[0][1])

    def test_remember_poi_includes_only_provided_coordinates(self):
        wire = DummyWire()
        skills = Skills(wire)

        skills.remember_poi("mine", "resource", 10.0, None, -3)

        self.assertEqual(
            wire.sent[0][1],
            {"name": "mine", "category": "resource", "x": 10.0, "z": -3},
        )

    def test_remember_poi_defaults(self):
        wire = DummyWire()
        skills = Skills(wire)

        skills.remember_poi()

        self.assertEqual(wire.sent[0][1], {"name": "", "category": "waypoint"})


if __name__ == "__main__":
    unittest.main()
