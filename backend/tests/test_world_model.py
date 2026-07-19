import json
import unittest

from agent.world_model import WorldModel


class WorldModelTests(unittest.TestCase):
    def test_authoritative_snapshot_clears_missing_collections_and_retains_overlays(self):
        model = WorldModel()
        model.set_connected(True)
        model.ingest_snapshot({
            "player": {"health": 20},
            "inventory": [{"slot": 0, "name": "minecraft:bread", "count": 2}],
            "entities": [{"id": 1, "type": "item", "distance": 2}],
            "fixture": "preserved",
        }, observed_at=10)
        model.ingest_overlay("task_state", {"kind": "collect", "status": "running"}, observed_at=11)

        model.ingest_snapshot({"player": {"health": 18}}, observed_at=12)
        projection = model.legacy_projection()

        self.assertEqual(projection["inventory"], [])
        self.assertEqual(projection["entities"], [])
        self.assertEqual(projection["task_state"]["kind"], "collect")
        self.assertEqual(projection["fixture"], "preserved")

    def test_malformed_field_preserves_last_valid_fact(self):
        model = WorldModel()
        model.ingest_snapshot({"player": {"health": 20}, "inventory": []}, observed_at=10)

        model.ingest_snapshot({"player": "invalid", "inventory": {}}, observed_at=11)

        projection = model.legacy_projection()
        self.assertEqual(projection["player"], {"health": 20})
        self.assertEqual(projection["inventory"], [])
        self.assertEqual(model.status(now=11)["invalid_updates"], 2)

    def test_malformed_only_snapshot_does_not_refresh_global_freshness(self):
        model = WorldModel()
        model.set_connected(True)
        model.ingest_snapshot({"player": {"health": 20}}, observed_at=10)

        model.ingest_snapshot({"player": "invalid", "world": []}, observed_at=20)

        status = model.status(now=20)
        self.assertEqual(status["observed_at"], 10)
        self.assertTrue(status["stale"])

    def test_equivalent_key_order_produces_identical_observation(self):
        first = WorldModel()
        second = WorldModel()
        first.ingest_snapshot({
            "player": {"health": 20, "x": 1},
            "entities": [{"id": 2, "distance": 3, "name": "zombie"}],
        }, observed_at=10)
        second.ingest_snapshot({
            "entities": [{"name": "zombie", "distance": 3, "id": 2}],
            "player": {"x": 1, "health": 20},
        }, observed_at=10)

        left = json.dumps(first.observation_slice(first.legacy_projection(), now=10), ensure_ascii=False)
        right = json.dumps(second.observation_slice(second.legacy_projection(), now=10), ensure_ascii=False)
        self.assertEqual(left, right)

    def test_observation_slice_is_bounded_sorted_and_deduplicated(self):
        model = WorldModel()
        model.set_connected(True)
        entities = [
            {"id": index, "type": "mob", "name": "zombie-" + "x" * 200, "distance": 100 - index}
            for index in range(100)
        ]
        entities.append(dict(entities[-1]))
        model.ingest_snapshot({
            "player": {"health": 4, "hunger": 2, "x": 1, "y": 64, "z": 2},
            "world": {"game_time": 100},
            "entities": entities,
            "inventory": [{"slot": index, "name": f"minecraft:item_{index}", "count": 64} for index in range(36)],
            "control_state": {"ai_controlled": True},
            "task_state": {"kind": "collect", "status": "running"},
        }, observed_at=20)

        sliced = model.observation_slice(model.legacy_projection(), max_chars=1400, now=21)
        encoded = json.dumps(sliced, ensure_ascii=False, separators=(",", ":"))

        self.assertLessEqual(len(encoded), 1400)
        self.assertEqual(sliced["player"]["health"], 4)
        self.assertEqual(sliced["control_state"]["ai_controlled"], True)
        distances = [entity["distance"] for entity in sliced.get("entities", [])]
        self.assertEqual(distances, sorted(distances))
        self.assertEqual(len({entity["id"] for entity in sliced.get("entities", [])}), len(distances))

    def test_disconnect_marks_last_known_facts_stale_without_erasing_them(self):
        model = WorldModel()
        model.set_connected(True)
        model.ingest_snapshot({"player": {"health": 20}}, observed_at=10)
        model.set_connected(False)

        status = model.status(now=11)

        self.assertTrue(status["stale"])
        self.assertTrue(status["facts"]["player"]["stale"])
        self.assertEqual(model.legacy_projection()["player"]["health"], 20)

    def test_health_and_task_boundaries_are_queued_and_acknowledged(self):
        model = WorldModel()
        model.set_connected(True)
        model.ingest_snapshot({"player": {"health": 20, "hunger": 20}}, observed_at=10)
        model.ingest_overlay("task_state", {"kind": "collect", "status": "running"}, observed_at=11)

        model.ingest_snapshot({"player": {"health": 3, "hunger": 20}}, observed_at=12)
        model.ingest_overlay("task_state", {
            "kind": "collect", "status": "failed", "target": "minecraft:iron_ore",
        }, observed_at=13)

        triggers = model.pending_decision_triggers()
        self.assertEqual([item["type"] for item in triggers], [
            "player.health_decreased", "task.state_changed",
        ])
        self.assertEqual(model.acknowledge_decision_triggers(triggers[0]["sequence"]), 1)
        self.assertEqual(len(model.pending_decision_triggers()), 1)

    def test_inventory_changes_are_journaled_without_decision_trigger(self):
        model = WorldModel()
        model.ingest_snapshot({
            "player": {"health": 20},
            "inventory": [{"slot": 0, "name": "minecraft:log", "count": 1}],
        }, observed_at=10)
        model.ingest_snapshot({
            "player": {"health": 20},
            "inventory": [{"slot": 0, "name": "minecraft:log", "count": 4}],
        }, observed_at=11)

        self.assertEqual(model.recent_journal()[-1]["type"], "inventory.counts_changed")
        self.assertEqual(model.pending_decision_triggers(), [])

    def test_semantic_journal_is_included_without_breaking_observation_budget(self):
        model = WorldModel()
        model.set_connected(True)
        model.ingest_snapshot({"player": {"health": 20, "hunger": 20}}, observed_at=10)
        for health in range(19, 3, -1):
            model.ingest_snapshot({"player": {"health": health, "hunger": 20}}, observed_at=30 - health)

        sliced = model.observation_slice(model.legacy_projection(), max_chars=1400, now=30)
        encoded = json.dumps(sliced, ensure_ascii=False, separators=(",", ":"))

        self.assertLessEqual(len(encoded), 1400)
        self.assertTrue(sliced.get("semantic_journal"))


if __name__ == "__main__":
    unittest.main()
