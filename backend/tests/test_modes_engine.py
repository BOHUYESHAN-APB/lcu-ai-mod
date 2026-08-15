import unittest

from agent.modes_engine import AutoEatMode, Mode, ModesEngine, SelfDefenseMode


class _PulseMode(Mode):
    def __init__(self):
        super().__init__("pulse", priority=0, cooldown=60.0)
        self.calls = 0

    def should_activate(self, state: dict) -> bool:
        return True

    def tick(self, skills, state: dict) -> str:
        self.calls += 1
        return "pulse"


class ModesEngineTests(unittest.TestCase):
    def test_defaults_only_register_bounded_operations(self):
        engine = ModesEngine(skills=object())
        engine.add_defaults()

        self.assertEqual(["self_heal", "auto_eat"], [mode.name for mode in engine.modes])

    def test_active_mode_action_refreshes_cooldown(self):
        mode = _PulseMode()
        engine = ModesEngine(skills=object())
        engine.add_mode(mode)

        self.assertEqual("pulse", engine.tick({}))
        mode._last_trigger = 0
        self.assertEqual("pulse", engine.tick({}))
        self.assertIsNone(engine.tick({}))
        self.assertEqual(2, mode.calls)

    def test_self_defense_requires_combat_policy(self):
        mode = SelfDefenseMode()
        state = {"entities": [{"type": "hostile", "distance": 2}]}

        self.assertFalse(mode.should_activate(state))
        state["automation_policy"] = {"allowAutomatedCombat": False}
        self.assertFalse(mode.should_activate(state))
        state["automation_policy"]["allowAutomatedCombat"] = True
        self.assertTrue(mode.should_activate(state))

    def test_auto_eat_requires_inventory_policy(self):
        mode = AutoEatMode()
        state = {"player": {"hunger": 5}}

        self.assertFalse(mode.should_activate(state))
        state["automation_policy"] = {"allowInventoryAutomation": True}
        self.assertTrue(mode.should_activate(state))


if __name__ == "__main__":
    unittest.main()
