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
    def test_configured_dispatcher_owns_command_emission(self):
        body = DummyWire()
        skills = Skills(body)
        calls = []
        skills.set_command_dispatcher(lambda command, args, context: calls.append((command, args, context)) or "intent-1")

        result = skills.jump()

        self.assertTrue(result["success"])
        self.assertEqual(result["req_id"], "intent-1")
        self.assertEqual(calls, [("jump", {}, "default")])
        self.assertEqual(body.sent, [])

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

    def test_inspect_block_sends_integer_position(self):
        wire = DummyWire()
        skills = Skills(wire)

        skills.inspect_block(10, 64, -3)

        self.assertEqual(wire.sent[0][0], "inspect_block")
        self.assertEqual(wire.sent[0][1], {"x": 10, "y": 64, "z": -3})

    def test_scan_crops_sends_bounded_radius(self):
        wire = DummyWire()
        skills = Skills(wire)

        skills.scan_crops(12)

        self.assertEqual(wire.sent[0][0], "scan_crops")
        self.assertEqual(wire.sent[0][1], {"radius": 12})

    def test_harvest_crop_sends_observed_identity(self):
        wire = DummyWire()
        skills = Skills(wire)

        skills.harvest_crop_at(10, 64, -3, "minecraft:wheat", 7, "token-1")

        self.assertEqual(wire.sent[0][0], "harvest_crop_at")
        self.assertEqual(wire.sent[0][1], {
            "x": 10, "y": 64, "z": -3, "block_id": "minecraft:wheat", "age": 7,
            "target_token": "token-1",
        })

    def test_verified_block_actions_carry_target_token(self):
        wire = DummyWire()
        skills = Skills(wire)

        skills.break_block_at(1, 2, 3, "token", "up")
        skills.use_block_at(4, 5, 6, "token-2")
        skills.place_block_at(7, 8, 9, "token-3", 7, 9, 9, "minecraft:stone", "up")

        self.assertEqual(wire.sent[0][0], "break_block_at")
        self.assertEqual(wire.sent[0][1]["target_token"], "token")
        self.assertEqual(wire.sent[1][0], "use_block_at")
        self.assertEqual(wire.sent[2][0], "place_block_at")
        self.assertEqual(wire.sent[2][1]["item_id"], "minecraft:stone")


if __name__ == "__main__":
    unittest.main()
