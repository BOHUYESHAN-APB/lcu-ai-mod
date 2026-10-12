"""「把床绑成家」这条链**必须有人推**，而且不能只靠模型轮次。

真机事故（2026-10-12 21:20，证据 `backend/logs/lcu.log`）：
    21:20:28 `set_home({'base': '家'}) -> ok`（床 (851,63,-2191)，人在 (849,63,-2194)）；
    之后**没有任何** `move_to` / 导航类下发，位置一直 (849,63,-2194) —— 原地罚站。
根因（`planner.py`）：链的推进被写在 `if self._composite_runs:` 里面，而 `set_home` 起链时
**不创建任何复合体运行** ⇒ 条件恒假 ⇒ `home_bed_step` 一次都没跑；session 心跳里也没有它。

本文件钉住四件事：
    ① 起链那一刻**一帧都不发**（没有隐藏的自主触发）；
    ② `set_home` 之后**没有新对话、只有一份新感知** ⇒ 链照样推进，出现 `move_to`（session tick 槽位）；
    ③ 一路走到"右键 → 拿原版重生点核对"：绑上了如实说绑上了，绑不上如实报失败，绝不静默停在原地；
    ④ `move_to` **只发一次**（每份感知重下会把寻路进度清零 —— "有路却一步不动"的老毛病）。
"""

import contextlib
import json
import tempfile
import unittest
from pathlib import Path

from agent.permissions import PermissionsStore
from agent.planner import Planner
from agent.session import Session
from agent.territory import TerritoryStore

SERVER = "play.example:25565"
WORLD = "world-a"
OVERWORLD = "minecraft:overworld"

BED_X, BED_Y, BED_Z = 851.0, 63.0, -2191.0
HERE = (849.0, 63.0, -2194.0)          # 真机那一帧的位置（离床约 3.6 格，还没到 REACH）


class DummyMemory:
    def build_context(self):
        return {}

    def get_recent_context(self, _count=5):
        return []


class FakeLLM:
    """按角色回固定内容（`plan_and_execute` 的第一阶段）。"""

    api_key = "test-key"

    def __init__(self, responses=None):
        self._responses = dict(responses or {})
        self.calls = []

    def chat(self, messages, agent=None, **kwargs):
        self.calls.append({"agent": agent, "messages": messages, "kwargs": kwargs})
        return {"content": self._responses.get(
            agent, json.dumps({"reply": "嗯。", "candidates": []}))}


class RecordingSkills:
    """记录下发的帧 —— 这份记录就是"到底往游戏里发了什么"的事实。"""

    def __init__(self):
        self.calls = []

    def command_context(self, _name):
        return contextlib.nullcontext()

    def __getattr__(self, item):
        def _record(*args, **kwargs):
            self.calls.append((item, args, kwargs))
            return {"success": True, "req_id": f"req-{len(self.calls)}"}
        return _record

    def names(self):
        return [call[0] for call in self.calls]


def bed_row(x=BED_X, y=BED_Y, z=BED_Z, distance=3.6):
    """模组上报的一行 `nearby_beds`。"""
    return {"id": "minecraft:red_bed", "name": "red_bed", "x": x, "y": y, "z": z,
            "distance": distance}


class DriveHarness(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.permissions = PermissionsStore(path=self.root / "permissions.json")
        self.permissions.set({"master": ["BoHuYeShan"]})
        self.store = TerritoryStore(root=self.root / "territory", server=SERVER,
                                    world_name=WORLD, dimension=OVERWORLD)
        self.skills = RecordingSkills()

    def planner(self, *, with_llm=False):
        return Planner(llm_service=FakeLLM() if with_llm else None,
                       memory=DummyMemory(), skills=self.skills,
                       permissions=self.permissions, territory=self.store)

    def start_chain(self, p, *, x=BED_X, y=BED_Y, z=BED_Z):
        """按真机那一次的口径起链：`set_home` 在附近找到一张床。"""
        context = {"actor_tier": "master", "player": {"x": HERE[0], "y": HERE[1], "z": HERE[2]},
                   "nearby_beds": [bed_row(x, y, z)]}
        return p._dispatch_tool_call("set_home", {}, context)

    @staticmethod
    def session(p, skills):
        """只搭心跳槽位要的那几样（`_home_bed_step` 不碰别的东西）。"""
        s = Session.__new__(Session)
        s.skills = skills
        s.planner = p
        s.runtime = {"player": {"x": HERE[0], "y": HERE[1], "z": HERE[2]},
                     "home_bed": {"x": BED_X, "y": BED_Y, "z": BED_Z},
                     "home_bed_bound": False}
        s._state_seq = 0
        s._home_bed_seq = -1
        return s

    @staticmethod
    def perceive(s, **state):
        """模组送来**一份新的 state_update**：runtime 合并 + 感知序号 +1（同 `handle_event`）。"""
        s.runtime.update(state)
        s._state_seq = int(getattr(s, "_state_seq", 0)) + 1

    def bed(self, s, x=BED_X, y=BED_Y, z=BED_Z, *, bound=False, home=True):
        """一份"我在床边、重生点绑没绑上"的感知。"""
        return {"player": {"x": x, "y": y, "z": z},
                "home_bed": ({"x": BED_X, "y": BED_Y, "z": BED_Z} if home else None),
                "home_bed_bound": bound,
                "nearby_beds": [bed_row()]}


# ── ① / ② 起链不发帧；没有新对话也要靠新感知推进 ─────────────────────


class HeartbeatDriveTests(DriveHarness):
    def test_starting_the_chain_sends_nothing_by_itself(self):
        p = self.planner()
        self.assertTrue(self.start_chain(p))

        self.assertEqual(self.skills.names(), [], "起链那一刻一帧都不许发（没有隐藏的自主触发）")
        self.assertEqual(p._home_bed["stage"], "walk")

    def test_a_fresh_perception_without_any_new_chat_moves_it(self):
        """真机事故的回归：`set_home -> ok` 之后**没人再说话**，链也必须自己走起来。"""
        p = self.planner()
        self.start_chain(p)
        s = self.session(p, self.skills)

        self.perceive(s)                                   # 只是新来了一份感知
        s._home_bed_step()

        self.assertEqual(self.skills.names(), ["move_to"], "没有任何新对话，也必须下发 move_to")
        self.assertEqual(self.skills.calls[0][1], (BED_X, BED_Y, BED_Z))

    def test_the_same_perception_is_never_stepped_twice(self):
        """20Hz 的心跳不能把同一份感知当很多轮用（否则轮预算被空转烧光）。"""
        p = self.planner()
        self.start_chain(p)
        s = self.session(p, self.skills)

        self.perceive(s)
        s._home_bed_step()
        s._home_bed_step()
        s._home_bed_step()
        self.assertEqual(p._home_bed["rounds"], 1, "同一份感知只算一轮")
        self.assertEqual(self.skills.names(), ["move_to"], "move_to 只发一次，绝不重复下发")

        self.perceive(s)                                   # 新的一份感知 ⇒ 才允许再推
        s._home_bed_step()
        self.assertEqual(p._home_bed["rounds"], 2)
        self.assertEqual(self.skills.names(), ["move_to"], "还没走到床边：什么都不发（不重下寻路）")

    def test_walking_then_right_clicking_then_confirming_the_respawn_point(self):
        p = self.planner()
        self.start_chain(p)
        s = self.session(p, self.skills)

        self.perceive(s)
        s._home_bed_step()                                 # 出发
        self.perceive(s, **self.bed(s, BED_X - 1.0, BED_Y, BED_Z - 1.0))
        s._home_bed_step()                                 # 到床边 ⇒ 右键
        self.assertEqual(self.skills.names(), ["move_to", "look_at", "use_on"])

        self.perceive(s, **self.bed(s, bound=True, home=True))
        self.assertTrue(s._home_bed_step())                # 原版认了这张床 ⇒ 结论

        self.assertIsNone(p._home_bed, "结论出来就该清账（一次性）")
        self.assertIn("绑好了", p._home_bed_report)
        self.assertIn("绑好了", p._with_notes(None), "结论必须能说给玩家（绝不静默吞掉）")
        self.assertIsNone(p._home_bed_report, "结论只说一次（说完就清）")

    def test_it_reports_failure_honestly_instead_of_standing_there(self):
        p = self.planner()
        self.start_chain(p)
        s = self.session(p, self.skills)

        self.perceive(s)                                   # 出发：我在 (849,63,-2194)
        s._home_bed_step()
        for _ in range(40):
            self.perceive(s, **self.bed(s, BED_X - 1.0, BED_Y, BED_Z - 1.0, bound=False))
            s._home_bed_step()
            if p._home_bed is None:                        # 结论已出 ⇒ 账清了，槽位不再推进
                break
        self.assertIsNone(p._home_bed)
        self.assertTrue(p._home_bed_report, "必须如实收尾，不许静默停在原地")
        self.assertNotIn("绑好了", p._home_bed_report)
        self.assertEqual(self.skills.names().count("move_to"), 1, "整条链只下过一次 move_to")
        self.assertEqual(self.skills.names().count("use_on"), 2, "最多右键 MAX_CLICKS 次")


# ── ③ 既有轮次槽位也会推它（不依赖挂起复合体）────────────────────────


class TurnSlotTests(DriveHarness):
    def test_the_turn_slot_drives_it_with_no_composite_runs(self):
        """旧写法把它写在 `if self._composite_runs:` 里 ⇒ 恒真不了 ⇒ 一次都不跑（本测试钉住）。"""
        p = self.planner(with_llm=True)
        self.start_chain(p)
        self.assertEqual(p._composite_runs, {}, "set_home 不创建复合体运行（这正是旧 bug 的土壤）")

        context = {"player": {"x": HERE[0], "y": HERE[1], "z": HERE[2]},
                   "nearby_beds": [bed_row()]}
        p.plan_and_execute("BoHuYeShan", "然后呢", context)

        self.assertIn("move_to", self.skills.names(), "轮次槽位必须也推它（不被复合体条件挡住）")


# ── ④ 心跳只认"新感知"（接线：state_update 让序号 +1）─────────────────


class PerceptionCounterTests(unittest.TestCase):
    def test_state_update_bumps_the_perception_counter(self):
        session = Session.__new__(Session)
        session.runtime = {"player": {}}
        session._state_seq = 0
        session.in_world = True
        session.ui_screen = ""
        # 这里只验"新感知计数"这一件事：吸收钩子全部置空。
        for name in ("_apply_presence", "_absorb_poi", "_absorb_death_points",
                     "_absorb_conversation_namespace", "_absorb_places",
                     "_absorb_territory", "_absorb_people"):
            setattr(session, name, lambda *a, **k: None)

        session.handle_event("state_update", {"player": {"x": 1.0}})

        self.assertEqual(session._state_seq, 1)


if __name__ == "__main__":
    unittest.main()
