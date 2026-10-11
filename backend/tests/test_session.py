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


class _FakeSkills:
    def __init__(self):
        self.slept = 0

    def command_context(self, _name):
        import contextlib
        return contextlib.nullcontext()

    def sleep_in_bed(self):
        self.slept += 1
        return {"success": True, "message": "sleep sent"}


class NightAutoSleepTests(unittest.TestCase):
    """夜晚自动睡觉：状态门 + 限频（注入假时间/假状态，无网络无时钟依赖）。"""

    NIGHT_WITH_BED = {
        "is_night": True,
        "sky_darken": 8,
        "can_sleep": True,
        "nearby_beds": [{"id": "minecraft:red_bed", "x": 3, "y": 64, "z": 5, "distance": 2.5}],
    }

    def _session(self, runtime, enabled=True):
        session = Session.__new__(Session)
        session.runtime = runtime
        session.skills = _FakeSkills()
        session._last_auto_sleep = 0.0
        session._auto_sleep_sent = False
        session._night_auto_sleep_provider = lambda: enabled
        return session

    def test_sleeps_once_per_night(self):
        session = self._session(dict(self.NIGHT_WITH_BED))

        self.assertTrue(Session._maybe_auto_sleep(session, True, True, False, now=1000.0))
        # 同一夜不重复下发，即使已经过了冷却。
        self.assertFalse(Session._maybe_auto_sleep(session, True, True, False, now=5000.0))
        self.assertEqual(session.skills.slept, 1)

    def test_day_resets_so_next_night_sleeps_again(self):
        session = self._session(dict(self.NIGHT_WITH_BED))
        Session._maybe_auto_sleep(session, True, True, False, now=1000.0)

        session.runtime["is_night"] = False
        self.assertFalse(Session._maybe_auto_sleep(session, True, True, False, now=1100.0))

        session.runtime["is_night"] = True
        self.assertTrue(Session._maybe_auto_sleep(session, True, True, False, now=2000.0))
        self.assertEqual(session.skills.slept, 2)

    def test_cooldown_blocks_immediate_retry(self):
        session = self._session(dict(self.NIGHT_WITH_BED))
        Session._maybe_auto_sleep(session, True, True, False, now=1000.0)
        session._auto_sleep_sent = False  # 模拟状态被重新允许

        self.assertFalse(Session._maybe_auto_sleep(session, True, True, False, now=1030.0),
                         "冷却内不得重发")
        self.assertTrue(Session._maybe_auto_sleep(session, True, True, False, now=1070.0))
        self.assertEqual(session.skills.slept, 2)

    def test_no_sleep_when_disabled_or_busy_or_day(self):
        session = self._session(dict(self.NIGHT_WITH_BED), enabled=False)
        self.assertFalse(Session._maybe_auto_sleep(session, True, True, False, now=1000.0))

        session = self._session(dict(self.NIGHT_WITH_BED))
        self.assertFalse(Session._maybe_auto_sleep(session, False, True, False, now=1000.0),
                         "自主关闭时不动")
        self.assertFalse(Session._maybe_auto_sleep(session, True, False, False, now=1000.0),
                         "非 idle 不动")
        self.assertFalse(Session._maybe_auto_sleep(session, True, True, True, now=1000.0),
                         "有活跃任务不动")

        day = self._session({"is_night": False, "can_sleep": True,
                             "nearby_beds": self.NIGHT_WITH_BED["nearby_beds"]})
        self.assertFalse(Session._maybe_auto_sleep(day, True, True, False, now=1000.0))

        self.assertEqual(session.skills.slept, 0)
        self.assertEqual(day.skills.slept, 0)

    def test_no_sleep_when_cannot_sleep_yet(self):
        runtime = dict(self.NIGHT_WITH_BED)
        runtime["can_sleep"] = False
        session = self._session(runtime)

        self.assertFalse(Session._maybe_auto_sleep(session, True, True, False, now=1000.0))
        self.assertEqual(session.skills.slept, 0)

    def test_no_sleep_without_any_known_bed(self):
        runtime = dict(self.NIGHT_WITH_BED)
        runtime["nearby_beds"] = []
        session = self._session(runtime)

        self.assertFalse(Session._maybe_auto_sleep(session, True, True, False, now=1000.0),
                         "附近和记忆里都没床 → 不空转")

        runtime["remembered_pois"] = [{"name": "bed1", "category": "bed",
                                       "x": 100, "y": 64, "z": 100}]
        self.assertTrue(Session._maybe_auto_sleep(session, True, True, False, now=2000.0))
        self.assertEqual(session.skills.slept, 1)

    def test_bed_detection_by_id_does_not_match_bedrock(self):
        self.assertTrue(Session._is_bed_poi({"id": "minecraft:white_bed"}))
        self.assertTrue(Session._is_bed_poi({"category": "bed", "id": "whatever"}))
        self.assertFalse(Session._is_bed_poi({"id": "minecraft:bedrock"}))
        self.assertFalse(Session._is_bed_poi({"id": "minecraft:chest"}))

    def test_has_known_bed_reads_remembered_pois(self):
        self.assertTrue(Session._has_known_bed(
            {"remembered_pois": [{"id": "minecraft:black_bed"}]}))
        self.assertFalse(Session._has_known_bed({"remembered_pois": [{"id": "minecraft:chest"}]}))


if __name__ == "__main__":
    unittest.main()
