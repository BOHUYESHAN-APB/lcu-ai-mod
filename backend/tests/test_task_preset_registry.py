import unittest

from agent.skill_registry import SkillRegistry
from agent.task_preset_registry import TaskPresetRegistry, TaskPresetValidationError


class TaskPresetRegistryTests(unittest.TestCase):
    def setUp(self):
        self.skills = SkillRegistry()
        self.registry = TaskPresetRegistry(self.skills)

    def test_lists_builtin_presets_with_skill_metadata(self):
        presets = self.registry.list("crafting")

        self.assertTrue(any(item["id"] == "craft.iron_pickaxe" for item in presets))
        self.assertTrue(all(item["skill"]["durable"] for item in presets))

        workflow = self.registry.list("workflow")[0]
        self.assertEqual(workflow["kind"], "workflow")
        self.assertEqual(workflow["step_count"], 2)
        self.assertTrue(all(item["durable"] for item in workflow["skills"]))

    def test_exact_placeholders_preserve_parameter_types(self):
        rendered = self.registry.render("navigation.coordinates", {
            "x": 1.5, "y": 64, "z": -2.25,
        })

        self.assertEqual(rendered["steps"][0]["skill_id"], "core.move_to")
        self.assertEqual(rendered["steps"][0]["input"], {"x": 1.5, "y": 64, "z": -2.25})

    def test_fixed_and_parameterized_presets_render_valid_skill_input(self):
        fixed = self.registry.render("craft.iron_pickaxe", {})["steps"][0]
        logs = self.registry.render("collect.logs", {"count": 12})["steps"][0]

        self.assertEqual((fixed["skill_id"], fixed["input"]), (
            "general.craft_item", {"item": "minecraft:iron_pickaxe", "count": 1},
        ))
        self.assertEqual((logs["skill_id"], logs["input"]), (
            "general.collect_blocks", {"block_type": "#minecraft:logs", "count": 12},
        ))

    def test_workflow_renders_ordered_durable_steps(self):
        rendered = self.registry.render("workflow.starter_chest", {})

        self.assertEqual(rendered["kind"], "workflow")
        self.assertEqual([step["key"] for step in rendered["steps"]], ["collect_logs", "craft_chest"])
        self.assertEqual(rendered["steps"][1]["input"], {"item": "minecraft:chest", "count": 1})

    def test_farm_region_renders_dynamic_workflow_with_bounded_radius(self):
        listed = next(item for item in self.registry.list("farming") if item["id"] == "farm.region")
        rendered = self.registry.render("farm.region", {"radius": 8})

        self.assertEqual(listed["kind"], "workflow")
        self.assertEqual([skill["id"] for skill in listed["skills"]], [
            "world.scan_crops", "world.harvest_crop_at",
        ])
        self.assertEqual(rendered["dynamic_handler"], "farm_region")
        self.assertEqual(rendered["parameters"], {"radius": 8})
        self.assertEqual(rendered["steps"], [])
        with self.assertRaisesRegex(TaskPresetValidationError, "must be <="):
            self.registry.render("farm.region", {"radius": 17})

    def test_rejects_missing_unknown_and_invalid_parameters(self):
        with self.assertRaisesRegex(TaskPresetValidationError, "missing parameters"):
            self.registry.render("collect.logs", {})
        with self.assertRaisesRegex(TaskPresetValidationError, "unknown parameters"):
            self.registry.render("survival.eat", {"food": "bread"})
        with self.assertRaisesRegex(TaskPresetValidationError, "must be >="):
            self.registry.render("collect.logs", {"count": 0})


if __name__ == "__main__":
    unittest.main()
