import tempfile
import unittest

from agent.memory import WorldPoiMemory
from agent.session import Session


class SessionTests(unittest.TestCase):
    def test_state_update_updates_runtime_and_absorbs_poi(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = Session.__new__(Session)
            session.runtime = {"player": {"x": 0}}
            session.poi = WorldPoiMemory(root=tmp)

            session.handle_event("state_update", {
                "player": {"x": 12, "dimension": "minecraft:overworld"},
                "world": {"time": 6000},
                "nearby_storage": [{"block_id": "minecraft:chest", "x": 3, "y": 64, "z": 9}],
            })

            self.assertEqual(session.runtime["player"]["x"], 12)
            self.assertEqual(session.runtime["world"]["time"], 6000)
            self.assertEqual(len(session.poi.find("storage")), 1)

    def test_manual_behavior_active_detects_follow_task(self):
        session = Session.__new__(Session)
        session.runtime = {"behavior_state": {"follow_target": "BoHuYeShan"}}
        session._manual_task_kind = "follow_player"

        self.assertTrue(Session._manual_behavior_active(session))

    def test_task_state_event_is_stored_in_runtime(self):
        session = Session.__new__(Session)
        session.runtime = {}
        session._manual_task_kind = None
        session._manual_action_reqs = set()

        session.handle_event("task_state", {"kind": "craft", "status": "collecting", "target": "wooden_sword"})

        self.assertEqual(session.runtime["task_state"]["kind"], "craft")
        self.assertEqual(session.runtime["task_state"]["status"], "collecting")

    def test_status_exposes_planner_decision(self):
        """M3-4 可观测：/api/status 应能看到最近一次 {choice, confidence}。"""
        from types import SimpleNamespace

        session = Session.__new__(Session)
        session.id = "t"
        session.action_manager = SimpleNamespace(get_status=lambda: {"label": None})
        session.planner = SimpleNamespace(
            get_status=lambda: {"last_decision": {"choice": 1, "confidence": 0.8}})
        session.modes_engine = SimpleNamespace(get_status=lambda: {})
        session.self_prompter = SimpleNamespace(get_status=lambda: {})
        session.control = SimpleNamespace(holding=False)
        session.memory = SimpleNamespace(interaction_count=0)
        session.message_db = SimpleNamespace(get_stats=lambda: {})
        session.runtime = {}
        session.vision = SimpleNamespace(get_status=lambda: {})
        session.poi = SimpleNamespace(namespace="srv__world", find=lambda _kind: [])
        session.llm = SimpleNamespace(get_usage=lambda: {})

        status = session.get_status()

        self.assertEqual(status["planner"]["last_decision"]["confidence"], 0.8)


class NightAutoTriggerRemovedTests(unittest.TestCase):
    """自主开关已删除：夜晚自动睡觉这条**自动触发**不再存在（用户口径：后台不许有自动触发）。"""

    def test_auto_sleep_hook_is_gone(self):
        self.assertFalse(hasattr(Session, "_maybe_auto_sleep"))
        self.assertFalse(hasattr(Session, "AUTO_SLEEP_COOLDOWN"))
        self.assertFalse(hasattr(Session, "_has_known_bed"))

    def test_session_init_has_no_auto_sleep_bookkeeping(self):
        self.assertFalse(hasattr(Session, "_auto_sleep_sent"))
        self.assertFalse(hasattr(Session, "_night_auto_sleep_provider"))


if __name__ == "__main__":
    unittest.main()
