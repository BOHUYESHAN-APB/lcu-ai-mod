"""M3-2：P0 反射层接到控制流（低层直驱）。

验收（见 docs/dev/ROADMAP.md M3-2）：
- 逃生时确实发出 forward/jump 控制流消息
- 模式退出时发出全 false（松手），否则高层寻路被永久压制
"""

import unittest

from agent.modes_engine import ModesEngine, SelfPreservationMode, _yaw_towards
from agent.skills import Skills
from protocol.wire_client import ControlStream


class DummyWire:
    def __init__(self, connected=True):
        self.connected = connected
        self.sent = []

    def send_control(self, state):
        self.sent.append(dict(state))


def _skills(wire):
    skills = Skills(wire)
    skills.set_control_stream(ControlStream(wire, interval=0.0))
    return skills


SAFE_PLAYER = {
    "x": 0.0, "y": 64.0, "z": 0.0,
    "in_lava": False, "on_fire": False, "fire_ticks": 0, "air_ticks": 300,
}


def _state(player=None, blocks=None):
    return {"player": dict(SAFE_PLAYER, **(player or {})), "nearby_blocks": blocks or []}


def _held(wire):
    """最后一帧里被按下的键。"""
    last = wire.sent[-1]
    return {k for k in ControlStream.KEYS if last.get(k)}


class YawHelperTests(unittest.TestCase):
    def test_orthogonal_and_aligned(self):
        self.assertAlmostEqual(_yaw_towards(0, 0, 0, 10), 0.0)
        self.assertAlmostEqual(abs(_yaw_towards(0, 0, 10, 0)), 90.0)


class SelfPreservationDriveTests(unittest.TestCase):
    def setUp(self):
        self.wire = DummyWire()
        self.skills = _skills(self.wire)
        self.mode = SelfPreservationMode()

    def test_lava_holds_jump(self):
        self.mode.tick(self.skills, _state({"in_lava": True}))
        self.assertTrue(self.wire.sent, "逃生必须下发控制流")
        self.assertIn("jump", _held(self.wire))
        self.assertLess(self.wire.sent[-1]["look_dy"], 0, "岩浆里要看上(负 pitch)")

    def test_drowning_holds_jump(self):
        self.mode.tick(self.skills, _state({"air_ticks": 10}))
        self.assertIn("jump", _held(self.wire))

    def test_fire_runs_toward_water(self):
        blocks = [{"x": 5, "y": 64, "z": 0, "block_id": "minecraft:water", "distance": 5.0}]
        self.mode.tick(self.skills, _state({"on_fire": True}, blocks))
        held = _held(self.wire)
        self.assertIn("forward", held)
        self.assertIn("sprint", held)
        self.assertIn("yaw", self.wire.sent[-1], "朝水跑需要绝对朝向")

    def test_fire_without_water_still_sprints(self):
        self.mode.tick(self.skills, _state({"fire_ticks": 20}))
        self.assertIn("forward", _held(self.wire))

    def test_no_threat_emits_nothing(self):
        self.assertFalse(self.mode.should_activate(_state()))

    def test_deactivate_releases_everything(self):
        self.mode.tick(self.skills, _state({"in_lava": True}))
        self.assertTrue(self.skills._control.holding)

        self.mode.on_deactivate(self.skills)

        self.assertFalse(self.skills._control.holding)
        self.assertEqual(_held(self.wire), set(), "退出必须发全 false")


    def test_drive_transmits_attack_and_use(self):
        """M3-5：离散意图（attack/use）必须能经控制流下发，且松手后归零。"""
        wire = DummyWire()
        skills = _skills(wire)

        skills.drive(attack=True, use=True)

        self.assertTrue(wire.sent[-1]["attack"])
        self.assertTrue(wire.sent[-1]["use"])
        self.assertTrue(skills._control.holding, "按住离散键也算 holding（要心跳）")

        skills.release_control()

        self.assertFalse(wire.sent[-1]["attack"])
        self.assertFalse(wire.sent[-1]["use"])


class EngineWiringTests(unittest.TestCase):
    """引擎必须把 skills 传给 on_deactivate，否则永远不松手。"""

    def setUp(self):
        self.wire = DummyWire()
        self.skills = _skills(self.wire)
        self.engine = ModesEngine(self.skills)
        self.engine.add_defaults()

    def test_holding_persists_across_ticks(self):
        self.engine.tick(_state({"in_lava": True}))
        self.engine.tick(_state({"in_lava": True}))

        self.assertTrue(self.skills._control.holding, "P0 必须持续按住而不是脉冲")
        self.assertEqual(self.engine.get_status()["active"], "self_preservation")

    def test_engine_releases_when_threat_clears(self):
        self.engine.tick(_state({"in_lava": True}))
        self.assertTrue(self.skills._control.holding)

        self.engine.tick(_state())

        self.assertFalse(self.skills._control.holding)
        self.assertEqual(_held(self.wire), set())


if __name__ == "__main__":
    unittest.main()
