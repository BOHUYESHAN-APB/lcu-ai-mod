"""死亡点记忆：模组只报事实（死在哪、什么时候），模型自己决定回不回去。

用户口径（原话）：
  - **"死了不会回死亡点捡装备"** ⇒ 要能回：把事实摆到模型眼前 + 给一个**模型可调用**的
    「回上次死亡点」工具（掉落物走过去自动捡，靠已有的 `move_to` 那条路）；
  - **"能力=注册的工具；后台不许有自主开关/自动触发"** ⇒ 只记事实、绝不自动往回走；
  - **旧客户端没有 `death_points`** ⇒ 不许凭空编一个死亡点（这一行整段不出现、工具如实拒绝）。

覆盖：
  ① 收到 `death_points` ⇒ 落进世界记忆（跨重启还在）且 L2 出现那一行（含坐标、维度、多久以前）；
  ② 没有该字段（或字段是坏的）⇒ L2**不出现**那一行、工具**如实拒绝**（`no_death_point`，不是假成功）；
  ③ 新的死亡点覆盖"上次"，旧的只留最近 3 次；
  ④ `go_to_death_point` 真把坐标交给 `move_to` 那条路，且**一个字都不写**任何既有记忆；
  ⑤ 上下文只追加：旧 context / 模组上报的那份数据都不被就地改写。

全部离线：临时目录落盘 + 假 skills 记录下发帧，不碰真实 `data/`。
"""

import copy
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from agent.memory import WorldPoiMemory
from agent.permissions import PermissionsStore
from agent.planner import Planner, _death_ago, death_point_line
from agent.session import Session
from agent.tool_registry import TOOL_BY_NAME

SERVER = "play.example:25565"
WORLD = "world-a"
OVERWORLD = "minecraft:overworld"

# 一次死亡：坐标、维度、墙上时间（毫秒）。
DEATH = {"x": 12.5, "y": 63.0, "z": -30.5, "dimension": OVERWORLD}


def death_row(*, at, **over) -> dict:
    """模组上报的一条死亡点（`at` = 死亡那一刻的 `System.currentTimeMillis()`）。"""
    row = dict(DEATH)
    row.update(over)
    row["at"] = int(at)
    return row


def state(*, death_points=(), server=SERVER, world=WORLD, dimension=OVERWORLD) -> dict:
    data = {"world": {"server": server, "world_name": world},
            "player": {"dimension": dimension}}
    if death_points is not None:
        data["death_points"] = list(death_points)
    return data


class DummyMemory:
    def build_context(self):
        return {}

    def get_recent_context(self, _count=5):
        return []


class RecordingSkills:
    """记录下发的帧（只记名字与参数，用来断言"到底走了哪条路"）。"""

    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def _record(*args, **kwargs):
            self.calls.append((name, args))
            return {"success": True}

        return _record

    def names(self) -> list[str]:
        return [call[0] for call in self.calls]


def planner(*, poi=None, skills=None, permissions=None) -> Planner:
    return Planner(llm_service=None, memory=DummyMemory(),
                   skills=skills if skills is not None else RecordingSkills(),
                   permissions=permissions, poi=poi)


def make_session(root: Path, *, poi=None) -> Session:
    """只搭 `state_update` 这条链真正用到的东西（其余按生产缺省 / 缺就是没有）。"""
    session = Session.__new__(Session)
    session.runtime = {}
    session.in_world = True
    session.memory = DummyMemory()
    session.vision = SimpleNamespace(latest=lambda: "")
    session.poi = poi if poi is not None else WorldPoiMemory(root=str(root / "poi"))
    session.people = None
    session.conversation = None
    session.borrow_rules = None
    session.territories = None
    session.last_speaker = {}
    session.chat_inbox = None
    session._present_person_facts = {}
    session._actor = {"name": "Owner", "tier": "master"}
    session._home_away_since = None
    session._hud_note = lambda: ""
    session.persona_blocks = lambda *_a, **_k: {}
    session.llm_active = lambda: True
    return session


class DeathPointTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def session(self) -> Session:
        return make_session(self.root)

    def permissions(self) -> PermissionsStore:
        store = PermissionsStore(path=self.root / "permissions.json")
        store.set({"master": ["Owner"]})
        return store

    def l2(self, session: Session, *, poi=None) -> str:
        """这一轮模型真正看到的那份 L2（与生产同一条链：session 上下文 → planner 渲染）。"""
        context = session._runtime_context()
        return planner(poi=poi if poi is not None else session.poi)._build_l2(
            "Owner", "你好", context, "LCU")


# ── ① 吸收 + 落盘 + 进 L2 ──────────────────────────────────────


class DeathPointContextTests(DeathPointTestCase):
    def test_the_state_update_puts_the_death_point_into_the_context(self):
        session = self.session()
        session.handle_event("state_update", state(death_points=[death_row(at=1760000000000)]))

        context = session._runtime_context()

        self.assertEqual(context["death_points"],
                         [{"x": 12.5, "y": 63.0, "z": -30.5, "dimension": OVERWORLD,
                           "at": 1760000000000.0}])

    def test_l2_carries_the_coordinates_the_dimension_and_how_long_ago(self):
        session = self.session()
        session.handle_event("state_update", state(
            death_points=[death_row(at=(time.time() - 5 * 60) * 1000)]))

        text = self.l2(session)

        self.assertIn("上次死亡点", text)
        self.assertIn("(12.5,63,-30.5)", text, "坐标要如实（整数不拖小数点，非整数留一位）")
        self.assertIn(OVERWORLD, text, "维度是必需的那一层")
        self.assertIn("分钟前", text, "`at` 要换算成「多久以前」")
        self.assertIn("那里的东西还掉着", text)
        self.assertIn("go_to_death_point", text, "要给出可走的路子（且它就是模型可调的那个工具）")
        self.assertIn("go_home", text, "回家是另一件事，别混")
        self.assertIn("自己往回走", text, "只摆事实，绝不自动回去")

    def test_the_coordinates_are_reported_once_in_l2(self):
        session = self.session()
        session.handle_event("state_update", state(
            death_points=[death_row(at=time.time() * 1000)],
            dimension=OVERWORLD))

        text = self.l2(session)

        self.assertEqual(text.count("12.5,63,-30.5"), 1, "坐标只报一次，别刷屏")
        # "已知记录点"那一行只说我们自己记住的点：死亡点不该混进去冒充记录点。
        remembered = text.split("已知记录点")[1].split("\n\n")[0]
        self.assertNotIn("12.5,63,-30.5", remembered)

    def test_an_older_death_point_mentions_the_index_without_listing_coordinates(self):
        session = self.session()
        session.handle_event("state_update", state(death_points=[
            death_row(at=2000, x=1.0, y=64.0, z=1.0),
            death_row(at=1000, x=9.0, y=64.0, z=9.0),
        ]))

        text = self.l2(session)

        self.assertIn("(1,64,1)", text, "这一行说的是**最新**那一次")
        self.assertIn("index=1/2", text, "更早的还记着，用 index 回")
        self.assertNotIn("(9,64,9)", text, "更早的坐标不在这一行刷屏")

    def test_the_death_point_survives_a_restart(self):
        session = self.session()
        session.handle_event("state_update", state(death_points=[death_row(at=1760000000000)]))

        # 换一个实例（= 重启后端）：同一「服务器 / 存档 / 维度」分区照样读得回来。
        reloaded = self.session()
        reloaded.poi.use_namespace(SERVER, WORLD, OVERWORLD)

        self.assertEqual(reloaded.death_points(),
                         [{"x": 12.5, "y": 63.0, "z": -30.5, "dimension": OVERWORLD,
                           "at": 1760000000000.0}])

    def test_a_row_without_a_dimension_uses_the_dimension_the_player_is_in(self):
        session = self.session()
        row = death_row(at=1760000000000)
        row.pop("dimension")

        session.handle_event("state_update", state(death_points=[row]))

        self.assertEqual(session.death_points()[0]["dimension"], OVERWORLD)
        self.assertIn(OVERWORLD, self.l2(session))

    def test_a_row_without_a_timestamp_reports_no_invented_time(self):
        session = self.session()
        row = death_row(at=1760000000000)
        row["at"] = None

        session.handle_event("state_update", state(death_points=[row]))
        text = self.l2(session)

        self.assertNotIn("at", session.death_points()[0], "不知道什么时候就别填一个假时间")
        line = text.split("上次死亡点")[1].splitlines()[0]
        self.assertIn(OVERWORLD, line)
        for invented in ("分钟前", "小时前", "天前", "刚刚"):
            self.assertNotIn(invented, line, "没有 `at` 就不许编一个时间")


# ── ② 没有该字段（或字段是坏的）⇒ 不出现那一行 + 工具如实拒绝 ──────


class NoDeathPointTests(DeathPointTestCase):
    def test_an_old_client_without_the_field_gets_no_line_and_no_move(self):
        session = self.session()
        session.handle_event("state_update", state(death_points=None))
        context = session._runtime_context()
        skills = RecordingSkills()
        p = planner(poi=session.poi, skills=skills, permissions=self.permissions())

        text = p._build_l2("Owner", "你好", context, "LCU")

        self.assertEqual(context["death_points"], [], "旧客户端没有这个字段 ⇒ 一条都不记")
        self.assertNotIn("死亡点", text, "没有记录就不许出现这一行")
        self.assertFalse(p._dispatch_tool_call("go_to_death_point", {}, context))
        self.assertEqual(skills.names(), [], "不知道死在哪就一帧都不发（绝不假装走了一趟）")
        self.assertIn("no_death_point", p._territory_note, "拒绝要带机器可读的 code")
        self.assertIn("不知道你上次死在哪", p._territory_note)

    def test_the_empty_report_and_broken_rows_never_become_a_death_point(self):
        session = self.session()
        session.handle_event("state_update", state(death_points=[]))
        session.handle_event("state_update", state(death_points=[
            {"dimension": OVERWORLD, "at": 1760000000000},          # 没有坐标
            {"x": "十二", "y": 63, "z": -30, "at": 1760000000001},   # 坐标不是数
            "不是一行数据",
            {"x": 1.0, "y": 64.0, "z": 1.0},                        # 有坐标、没有 at
        ]))

        context = session._runtime_context()

        self.assertEqual([row["x"] for row in context["death_points"]], [1.0],
                         "只记拿得准的那一条（坏行丢掉，绝不补一个假的）")
        self.assertIn("(1,64,1)", self.l2(session))

    def test_a_context_without_the_key_at_all_renders_nothing(self):
        session = self.session()
        text = planner(poi=session.poi)._build_l2("Owner", "你好", {}, "LCU")

        self.assertNotIn("死亡点", text)


# ── ③ 最新的覆盖"上次"，旧的只留最近 3 次 ──────────────────────


class LatestDeathPointTests(DeathPointTestCase):
    def test_the_newest_death_point_overrides_the_previous_one(self):
        session = self.session()
        session.handle_event("state_update", state(
            death_points=[death_row(at=1000, x=1.0, y=64.0, z=1.0)]))
        session.handle_event("state_update", state(
            death_points=[death_row(at=9000, x=7.0, y=70.0, z=7.0)]))

        context = session._runtime_context()
        text = self.l2(session)

        self.assertEqual(context["death_points"][0]["at"], 9000.0)
        self.assertIn("(7,70,7)", text)
        self.assertNotIn("(1,64,1)", text, "上一次已经退到后面去了，不占这一行")

        self.assertTrue(planner(poi=session.poi)._dispatch_tool_call(
            "go_to_death_point", {}, context), "默认就走最新那一次")

    def test_the_same_death_reported_again_stays_one_point(self):
        session = self.session()
        row = death_row(at=5000)
        session.handle_event("state_update", state(death_points=[row]))
        session.handle_event("state_update", state(death_points=[dict(row)]))

        self.assertEqual([point["at"] for point in session.death_points()], [5000.0])

    def test_only_the_three_most_recent_deaths_are_kept(self):
        session = self.session()
        # 模组按契约"最新在前"报；这里老的点故意也报一次（旧的在后面）。
        session.handle_event("state_update", state(death_points=[
            death_row(at=4000, x=4.0, y=64.0, z=4.0),
            death_row(at=3000, x=3.0, y=64.0, z=3.0),
            death_row(at=2000, x=2.0, y=64.0, z=2.0),
            death_row(at=1000, x=1.0, y=64.0, z=1.0),
        ]))
        session.handle_event("state_update", state(death_points=[
            death_row(at=5000, x=5.0, y=64.0, z=5.0)]))

        at = [point["at"] for point in session.death_points()]

        self.assertEqual(at, [5000.0, 4000.0, 3000.0],
                         "最新在前，旧的最多留到第 3 次（不无限长）")

    def test_an_index_beyond_what_is_remembered_is_refused_honestly(self):
        session = self.session()
        session.handle_event("state_update", state(death_points=[death_row(at=1000)]))
        skills = RecordingSkills()
        p = planner(poi=session.poi, skills=skills, permissions=self.permissions())

        self.assertFalse(p._dispatch_tool_call(
            "go_to_death_point", {"index": 2}, session._runtime_context()))

        self.assertEqual(skills.names(), [], "没有那一次就一帧都不发")
        self.assertIn("code=no_such_death_point", p._territory_note)


# ── ④ 走已有的 `move_to` 那条路，且不写任何记忆 ──────────────────


class GoToDeathPointToolTests(DeathPointTestCase):
    def setUp(self):
        super().setUp()
        self.session_ = self.session()
        self.session_.handle_event("state_update", state(death_points=[
            death_row(at=2000, x=1.0, y=64.0, z=1.0),
            death_row(at=1000, x=9.0, y=64.0, z=9.0),
        ]))
        self.context = self.session_._runtime_context()
        self.skills = RecordingSkills()
        self.p = planner(poi=self.session_.poi, skills=self.skills,
                         permissions=self.permissions())

    def test_the_default_index_walks_to_the_latest_death_point(self):
        self.assertTrue(self.p._dispatch_tool_call("go_to_death_point", {}, self.context))

        self.assertEqual(self.skills.calls[-1], ("move_to", (1.0, 64.0, 1.0)),
                         "复用已有的 move_to 那条路（不掉落物上没有第二条移动路径）")
        self.assertIn("(1,64,1)", self.p._territory_note)
        self.assertIn("自动捡", self.p._territory_note)

    def test_the_index_walks_to_an_older_death_point(self):
        self.assertTrue(self.p._dispatch_tool_call(
            "go_to_death_point", {"index": 1}, self.context))

        self.assertEqual(self.skills.calls[-1], ("move_to", (9.0, 64.0, 9.0)))
        self.assertIn("上上次死亡点", self.p._territory_note)

    def test_walking_there_writes_no_memory_at_all(self):
        memory = self.session_.poi
        before = copy.deepcopy(memory.death_points())
        before_file = memory.path.read_text(encoding="utf-8")

        self.assertFalse(self.skills.names())
        self.assertTrue(self.p._dispatch_tool_call("go_to_death_point", {}, self.context))

        self.assertEqual(memory.death_points(), before, "走一趟不该改写死亡点记忆")
        self.assertEqual(memory.path.read_text(encoding="utf-8"), before_file,
                         "更不该顺手刷盘（一个字都不写）")
        self.assertEqual([row["name"] for row in memory.find("waypoint")], [],
                         "别的记忆（记录点/箱子）也不许被碰")

    def test_a_bogus_index_never_moves(self):
        for bogus in ("上上次", 1.5, True, -1):
            self.skills.calls.clear()
            self.assertFalse(self.p._dispatch_tool_call(
                "go_to_death_point", {"index": bogus}, self.context), bogus)
            self.assertEqual(self.skills.names(), [], f"index={bogus!r} 不该乱走")
            self.assertIn("no_such_death_point", self.p._territory_note)

    def test_the_registry_description_says_it_is_not_going_home(self):
        description = TOOL_BY_NAME["go_to_death_point"].description

        self.assertIn("不是回家", description)
        self.assertIn("go_home", description)
        self.assertIn("no_death_point", description)


# ── ⑤ 只追加：旧上下文 / 模组上报的那份数据都不被就地改写 ──────────


class AppendOnlyTests(DeathPointTestCase):
    def test_a_new_report_never_rewrites_the_old_context(self):
        session = self.session()
        session.handle_event("state_update", state(death_points=[death_row(at=1000, x=1.0)]))
        old_context = session._runtime_context()
        old_text = self.l2(session)

        session.handle_event("state_update", state(death_points=[death_row(at=2000, x=2.0)]))

        self.assertEqual(old_context["death_points"][0]["x"], 1.0,
                         "上一轮那份上下文没被就地改写（只追加）")
        self.assertEqual(old_context["death_points"][0]["at"], 1000.0)
        self.assertIn("(1,63,-30.5)", old_text)
        self.assertEqual(session._runtime_context()["death_points"][0]["x"], 2.0,
                         "新的那份才带最新一次")

    def test_the_mod_reported_rows_are_never_rewritten(self):
        session = self.session()
        rows = [death_row(at=2000, x=2.0), death_row(at=1000, x=1.0)]
        snapshot = copy.deepcopy(rows)

        session.handle_event("state_update", state(death_points=rows))
        # 模组下一轮又报同一份（幂等）：上报的那份数据一个字段都不许被我们改。
        session.handle_event("state_update", state(death_points=rows))

        self.assertEqual(rows, snapshot, "上报的数据是模组的，我们只读不改")

    def test_reading_the_context_twice_gives_two_independent_copies(self):
        session = self.session()
        session.handle_event("state_update", state(death_points=[death_row(at=1000)]))

        first = session._runtime_context()
        first["death_points"][0]["x"] = 999.0

        self.assertEqual(session._runtime_context()["death_points"][0]["x"], 12.5,
                         "改上下文改不到记忆里")


# ── 纯函数：时间换算 / 那一行的边界 ─────────────────────────────


class DeathPointLineTests(unittest.TestCase):
    NOW = 1_760_000_000.0

    def test_how_long_ago_is_computed_from_the_wall_clock(self):
        self.assertEqual(_death_ago((self.NOW - 5) * 1000, self.NOW), "刚刚")
        self.assertEqual(_death_ago((self.NOW - 180) * 1000, self.NOW), "3分钟前")
        self.assertEqual(_death_ago((self.NOW - 3 * 3600) * 1000, self.NOW), "3小时前")
        self.assertEqual(_death_ago((self.NOW - 2 * 86400) * 1000, self.NOW), "2天前")

    def test_a_missing_or_bogus_timestamp_invents_nothing(self):
        for bogus in (None, "", "昨天", True):
            self.assertEqual(_death_ago(bogus, self.NOW), "")
        self.assertEqual(_death_ago((self.NOW + 3600) * 1000, self.NOW), "",
                         "模组表跑到前面去了就说不知道，不编一个负数时间")

    def test_the_line_needs_real_coordinates(self):
        self.assertEqual(death_point_line(None), "")
        self.assertEqual(death_point_line({"dimension": OVERWORLD, "at": 1}), "")
        self.assertEqual(death_point_line({"x": "a", "y": 1, "z": 1}), "")


if __name__ == "__main__":
    unittest.main()
