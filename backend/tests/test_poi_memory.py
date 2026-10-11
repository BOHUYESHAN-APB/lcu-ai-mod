import tempfile
import unittest
from pathlib import Path

from agent.memory import WorldPoiMemory


def storage(x=10, y=64, z=20, contents=None):
    poi = {"block_id": "minecraft:chest", "x": x, "y": y, "z": z, "distance": 3.0}
    if contents:
        poi["contents"] = contents
    return poi


class WorldPoiMemoryTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _poi(self):
        return WorldPoiMemory(root=str(self.root))

    def test_namespaces_are_isolated_and_reloadable(self):
        poi = self._poi()
        poi.use_namespace("127.0.0.1:25565", "world-a")
        poi.observe("storage", storage(contents=[{"item_id": "minecraft:iron_ingot", "count": 12}]))
        poi.save()

        # 换到另一个服务器/世界：不应看到上一个命名空间的数据
        poi.use_namespace("127.0.0.1:25566", "world-b")
        self.assertEqual(poi.find("storage"), [])

        # 换回来：数据还在
        poi.use_namespace("127.0.0.1:25565", "world-a")
        self.assertEqual(len(poi.find("storage")), 1)

    def test_namespace_survives_new_instance(self):
        poi = self._poi()
        poi.use_namespace("singleplayer", "MyWorld")
        poi.observe("storage", storage())
        poi.save()

        reloaded = self._poi()
        reloaded.use_namespace("singleplayer", "MyWorld")

        self.assertEqual(len(reloaded.find("storage")), 1)

    def test_unchanged_observation_is_not_rewritten(self):
        poi = self._poi()
        poi.use_namespace("s", "w")

        self.assertTrue(poi.observe("storage", storage()))
        self.assertFalse(poi.observe("storage", storage()))

    def test_changed_contents_are_updated(self):
        poi = self._poi()
        poi.use_namespace("s", "w")
        poi.observe("storage", storage(contents=[{"item_id": "minecraft:coal", "count": 3}]))

        changed = poi.observe("storage", storage(contents=[{"item_id": "minecraft:coal", "count": 8}]))

        self.assertTrue(changed)
        self.assertEqual(poi.find("storage")[0]["contents"][0]["count"], 8)

    def test_observe_state_absorbs_storage_and_workstations(self):
        poi = self._poi()
        poi.use_namespace("s", "w")

        changed = poi.observe_state({
            "player": {"dimension": "minecraft:overworld"},
            "nearby_storage": [storage()],
            "nearby_workstations": [{"block_id": "minecraft:furnace", "x": 1, "y": 2, "z": 3}],
        })

        self.assertEqual(changed, 2)
        self.assertEqual(len(poi.find("storage")), 1)
        self.assertEqual(len(poi.find("workstation")), 1)
        self.assertEqual(poi.find("storage")[0]["dimension"], "minecraft:overworld")

    def test_observe_ignores_entries_without_coordinates(self):
        poi = self._poi()
        poi.use_namespace("s", "w")

        self.assertFalse(poi.observe("storage", {"block_id": "minecraft:chest"}))
        self.assertEqual(poi.find(), [])

    def test_describe_renders_contents_and_handles_empty(self):
        poi = self._poi()
        poi.use_namespace("s", "w")

        self.assertIn("暂无", poi.describe())

        poi.observe("storage", storage(contents=[{"item_id": "minecraft:iron_ingot", "count": 12}]))

        described = poi.describe()
        self.assertIn("minecraft:chest", described)
        self.assertIn("minecraft:iron_ingotx12", described)

    def test_observe_state_absorbs_remembered_pois_with_their_category(self):
        poi = self._poi()
        poi.use_namespace("s", "w")

        changed = poi.observe_state({
            "remembered_pois": [
                {"id": "minecraft:white_bed", "name": "bed1", "category": "bed",
                 "x": 1, "y": 64, "z": 2},
                {"id": "minecraft:crafting_table", "x": 5, "y": 64, "z": 5},
            ],
        })

        self.assertEqual(changed, 2)
        self.assertEqual(len(poi.find("bed")), 1)
        self.assertEqual(poi.find("bed")[0]["name"], "bed1")
        # 缺省 category → waypoint
        waypoints = poi.find("waypoint")
        self.assertEqual(len(waypoints), 1)
        self.assertEqual(waypoints[0]["block_id"], "minecraft:crafting_table")

    def test_observe_state_keeps_remembered_pois_distinct_from_nearby_storage(self):
        poi = self._poi()
        poi.use_namespace("s", "w")

        poi.observe_state({
            "nearby_storage": [{"block_id": "minecraft:chest", "x": 3, "y": 64, "z": 9}],
            "remembered_pois": [{"id": "minecraft:chest", "name": "老家的箱子",
                                 "category": "storage", "x": 300, "y": 70, "z": -400}],
        })

        self.assertEqual(len(poi.find("storage")), 2)

    def test_describe_waypoint_is_friendly(self):
        poi = self._poi()
        poi.use_namespace("s", "w")
        poi.observe("waypoint", {"id": "minecraft:chest", "name": "家", "x": 10, "y": 70, "z": -4})

        described = poi.describe("waypoint")

        self.assertIn("家", described)
        self.assertIn("@(10,70,-4)", described)
        self.assertNotIn("内容未知", described)

    def test_describe_bed_category_label_when_empty(self):
        poi = self._poi()
        poi.use_namespace("s", "w")

        self.assertEqual(poi.describe("bed"), "暂无已知床")

    def test_remembered_bed_persists_under_its_category(self):
        poi = self._poi()
        poi.use_namespace("s", "w")
        poi.observe_state({"remembered_pois": [
            {"id": "minecraft:white_bed", "name": "bed1", "category": "bed", "x": 1, "y": 64, "z": 2}]})
        poi.save()

        reloaded = self._poi()
        reloaded.use_namespace("s", "w")

        self.assertEqual(len(reloaded.find("bed")), 1)
        self.assertIn("bed1", reloaded.describe("bed"))

    def test_unsafe_server_string_becomes_safe_filename(self):
        poi = self._poi()
        name = poi.use_namespace("fe80::1/weird path", "w")

        self.assertNotIn("/", name)
        self.assertNotIn(":", name)

        poi.observe("storage", storage())
        poi.save()
        self.assertTrue(poi.path.exists())


if __name__ == "__main__":
    unittest.main()
