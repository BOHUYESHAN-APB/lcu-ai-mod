"""带标签的 `remembered_pois` → 后端消费「家 / 床」事实的验收测试。

覆盖（对照票据的验收清单）：
  ① 有 `home` 没有 `bed` ⇒ 上下文如实报"床没了"，坐标是**家**的位置；
  ② 有 `home` 有 `bed` ⇒ 不报"床没了"（家完好）；
  ③ 只有 `bed` 没有 `home` ⇒ 按 bed 记着，**绝不**自动升级成 home；
  ④ `set_home` 两次 ⇒ 只有最新那张床是 home；`go_home` 走的是**当前** home，
    不吃上一轮的陈旧快照（store 没坐标时才退到模组报的家记忆）；
  ⑤ 上下文只追加：新状态不改写旧上下文、不改写模组上报的原始点。

坐标口径：家的坐标只报一次（上面基地行打过就不再重复），别让坐标刷屏。
"""

import copy
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from agent.memory import WorldPoiMemory
from agent.permissions import PermissionsStore
from agent.planner import Planner
from agent.session import Session
from agent.territory import (BED_TAG, HOME_TAG, TerritoryStore, home_memory_facts,
                             poi_tags)

SERVER = "play.example:25565"
WORLD = "world-a"
OVERWORLD = "minecraft:overworld"

HOME_X, HOME_Y, HOME_Z = 12.0, 64.0, -30.0


def home_poi(*, tags=("bed", "home"), category="bed", x=HOME_X, y=HOME_Y, z=HOME_Z,
             name="床"):
    """模组发来的一条记忆点（`category` = 主标签，`tags` = 全部标签）。"""
    return {"block_id": "minecraft:white_bed", "category": category, "tags": list(tags),
            "name": name, "x": x, "y": y, "z": z, "distance": 4.2}


class DummyMemory:
    def build_context(self):
        return {}

    def get_recent_context(self, _count=5):
        return []


class RecordingSkills:
    """记录下发的帧。"""

    def __init__(self):
        self.calls = []

    def move_to(self, x, y, z):
        self.calls.append(("move_to", (float(x), float(y), float(z))))

    def names(self):
        return [call[0] for call in self.calls]


def planner(*, store=None, skills=None, permissions=None) -> Planner:
    return Planner(llm_service=None, memory=DummyMemory(),
                   skills=skills if skills is not None else RecordingSkills(),
                   permissions=permissions, territory=store)


# ── ⓪ 标签与事实的纯函数（没有"数量"式断言，只看行为与边界）────────


class HomeMemoryFactsTests(unittest.TestCase):
    def test_tags_merge_the_new_array_with_the_legacy_category(self):
        self.assertEqual(poi_tags({"category": "Bed", "tags": ["Home", " bed "]}),
                         {"bed", "home"})
        self.assertEqual(poi_tags({"category": "waypoint"}), {"waypoint"})
        self.assertEqual(poi_tags({"tags": ["bed"]}), {"bed"})
        self.assertEqual(poi_tags(None), set())
        self.assertEqual(poi_tags({"name": "什么都没有"}), set())

    def test_no_home_tag_means_no_home_fact(self):
        self.assertEqual(home_memory_facts([]), {})
        self.assertEqual(home_memory_facts([{"category": "bed", "tags": ["bed"],
                                             "x": 1, "y": 2, "z": 3}]), {},
                         "只有 bed 没有 home：不是家，绝不自动升级")

    def test_home_with_a_bed_is_intact_and_bed_missing_is_a_fact(self):
        intact = home_memory_facts([home_poi()])
        self.assertEqual(intact["home_bed"], "present")
        self.assertNotIn("home_note", intact)

        gone = home_memory_facts([home_poi(tags=("home",), category="home")])
        self.assertEqual(gone["home_bed"], "missing")
        self.assertEqual(gone["home_point"], {"x": HOME_X, "y": HOME_Y, "z": HOME_Z})
        self.assertIn("床没了", gone["home_note"])
        self.assertIn("go_home", gone["home_note"])
        self.assertIn("make_bed", gone["home_note"])

    def test_the_home_point_that_still_has_a_bed_wins(self):
        old = home_poi(tags=("home",), category="home", x=1.0, y=64.0, z=2.0)
        new = home_poi(tags=("bed", "home"), category="bed", x=40.0, y=64.0, z=50.0)

        facts = home_memory_facts([old, new])

        self.assertEqual(facts["home_bed"], "present", "还带着 bed 的那个才是当前的家")
        self.assertEqual(facts["home_point"], {"x": 40.0, "y": 64.0, "z": 50.0})


# ── ① / ② / ③ 上下文里如实报「家还在、床没了」────────────────


class HomeContextTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def session(self, *, pois, player=None, base=None):
        """只搭 `_runtime_context` 这条链真正用到的东西（其余按生产缺省）。"""
        s = Session.__new__(Session)
        s.runtime = {
            "player": player if player is not None else {"x": 1000.0, "y": 64.0, "z": 1000.0},
            "remembered_pois": pois,
            "players": [],
        }
        s.memory = DummyMemory()
        s.vision = SimpleNamespace(latest=lambda: "")
        s.poi = SimpleNamespace(describe=lambda *a, **k: "", find=lambda *a, **k: [])
        s.territories = TerritoryStore(root=self.root / "territory", server=SERVER,
                                       world_name=WORLD, dimension=OVERWORLD)
        if base is not None:
            s.territories.set_base("home", anchor=base, radius=32)
        s.last_speaker = {}
        s.chat_inbox = None
        s._hud_note = lambda: ""
        s.persona_blocks = lambda *_a, **_k: {}
        s.conversation = None
        s.llm_active = lambda: True
        s._actor = {"name": "Owner", "tier": "master"}
        s._zone_blocks = []
        s._home_away_since = None
        return s

    def render(self, context):
        return planner()._render_territory(context)

    def test_home_without_bed_reports_the_missing_bed_at_the_home_point(self):
        s = self.session(pois=[home_poi(tags=("home",), category="home")],
                         base={"x": HOME_X, "y": HOME_Y, "z": HOME_Z})

        context = s._runtime_context()
        status = context["home_status"]

        self.assertEqual(status["home_bed"], "missing")
        self.assertEqual(status["home_point"], {"x": HOME_X, "y": HOME_Y, "z": HOME_Z},
                         "坐标必须是**家**的位置（床没了点还在）")
        self.assertIn("床没了", status["home_note"])
        self.assertIn("go_home", status["home_note"], "要给出可走的路子，且不自动调用")
        self.assertIn("make_bed", status["home_note"])

        text = self.render(context)
        self.assertIn("床没了", text)
        self.assertEqual(text.count("12,64,-30"), 1, "家的坐标只报一次（基地行已经打过）")
        self.assertIn("（坐标见上面的基地行）", text)

    def test_home_without_bed_and_without_any_base_still_reports_the_point(self):
        s = self.session(pois=[home_poi(tags=("home",), category="home")])

        context = s._runtime_context()

        self.assertIsNone(context["current_base"])
        self.assertEqual(context["home_status"]["home_bed"], "missing")
        self.assertEqual(context["home_status"]["home_point"],
                         {"x": HOME_X, "y": HOME_Y, "z": HOME_Z})

        text = self.render(context)
        self.assertIn("床没了", text)
        self.assertEqual(text.count("12,64,-30"), 1, "没有基地行时，家的坐标也得报一次")

    def test_home_with_a_bed_is_not_reported_as_missing(self):
        s = self.session(pois=[home_poi()], base={"x": HOME_X, "y": HOME_Y, "z": HOME_Z})

        context = s._runtime_context()

        self.assertEqual(context["home_status"]["home_bed"], "present")
        self.assertNotIn("home_note", context["home_status"], "家完好就没有「床没了」这回事")
        self.assertNotIn("床没了", self.render(context))

    def test_a_bed_without_the_home_tag_is_never_promoted_to_home(self):
        poi = WorldPoiMemory(root=str(self.root / "poi"))
        poi.use_namespace("s", "w")
        poi.observe_state({"remembered_pois": [home_poi(tags=("bed",), category="bed")]})

        s = self.session(pois=[home_poi(tags=("bed",), category="bed")])
        context = s._runtime_context()

        self.assertNotIn("home_bed", context["home_status"])
        self.assertNotIn("home_point", context["home_status"])
        self.assertEqual(self.render(context), "", "不是家就一条回家事实都不报")
        self.assertEqual(poi.find(HOME_TAG), [], "记忆里也不能挂到 home 上")
        self.assertEqual(len(poi.find(BED_TAG)), 1, "按 bed 照常记着")


# ── ④ set_home / go_home 走"当前"的家 ───────────────────────────


class HomeToolTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.store = TerritoryStore(root=self.root / "territory", server=SERVER,
                                    world_name=WORLD, dimension=OVERWORLD)
        self.store.set_base("home", anchor={"x": 0, "y": 64, "z": 0}, radius=32)
        self.skills = RecordingSkills()
        self.permissions = PermissionsStore(path=self.root / "permissions.json")
        self.permissions.set({"master": ["Owner"]})

    def _planner(self):
        return planner(store=self.store, skills=self.skills, permissions=self.permissions)

    def test_set_home_twice_keeps_only_the_latest_bed(self):
        p = self._planner()
        near = {"x": 1, "y": 64, "z": 1}

        self.assertTrue(p._dispatch_tool_call(
            "set_home", {}, dict(actor_tier="master", actor_name="Owner", player=near,
                                 nearby_beds=[{"id": "minecraft:red_bed", "x": 5, "y": 64,
                                               "z": 5, "distance": 6.0}])))
        self.assertTrue(p._dispatch_tool_call(
            "set_home", {}, dict(actor_tier="master", actor_name="Owner", player=near,
                                 nearby_beds=[{"id": "minecraft:blue_bed", "x": 9, "y": 64,
                                               "z": 9, "distance": 6.0}])))

        self.assertEqual(self.store.base("home")["home_bed"],
                         {"x": 9.0, "y": 64.0, "z": 9.0}, "新床替换旧床，只能有一个 home")
        self.assertEqual([row["name"] for row in self.store.bases()], ["home"],
                         "换床不许多出一条基地")

    def test_go_home_walks_to_the_current_bed_not_a_stale_snapshot(self):
        p = self._planner()
        # 上一轮的 L2 快照（床位 (1,64,1) 是陈旧的）：go_home 必须现读 store。
        stale_context = {
            "player": {"x": 0, "y": 64, "z": 0},
            "territory": {"territories": [], "containers": [], "zones": [],
                          "bases": [{"name": "home", "kind": "base", "radius": 32,
                                     "anchor": {"x": 0, "y": 64, "z": 0},
                                     "home_bed": {"x": 1, "y": 64, "z": 1}}]},
            "current_base": None,
        }
        self.store.set_home_bed("home", {"x": 7, "y": 64, "z": 7})

        self.assertTrue(p._dispatch_tool_call("go_home", {}, dict(stale_context)))
        self.assertEqual(self.skills.calls[-1], ("move_to", (7.0, 64.0, 7.0)))

        self.store.set_home_bed("home", {"x": 9, "y": 64, "z": 9})
        p._dispatch_tool_call("go_home", {}, dict(stale_context))
        self.assertEqual(self.skills.calls[-1], ("move_to", (9.0, 64.0, 9.0)),
                         "再换一次也走当前那张床")

    def test_go_home_falls_back_to_the_remembered_home_point(self):
        empty = TerritoryStore(root=self.root / "empty", server=SERVER,
                               world_name=WORLD, dimension=OVERWORLD)
        p = planner(store=empty, skills=self.skills, permissions=self.permissions)
        context = {"player": {"x": 0, "y": 64, "z": 0},
                   "home_status": {"home_bed": "missing",
                                   "home_point": {"x": HOME_X, "y": HOME_Y, "z": HOME_Z},
                                   "home_note": "床没了"}}

        self.assertTrue(p._dispatch_tool_call("go_home", {}, context))
        self.assertEqual(self.skills.calls[-1],
                         ("move_to", (HOME_X, HOME_Y, HOME_Z)),
                         "store 没基地时走模组记着的家位置（不猜坐标、也不自动去）")

    def test_go_home_without_any_home_does_not_move(self):
        empty = TerritoryStore(root=self.root / "empty2", server=SERVER,
                               world_name=WORLD, dimension=OVERWORLD)
        p = planner(store=empty, skills=self.skills, permissions=self.permissions)

        self.assertFalse(p._dispatch_tool_call("go_home", {}, {"player": {"x": 0, "y": 64, "z": 0}}))
        self.assertEqual(self.skills.names(), [], "不知道家在哪就如实说，绝不乱走")


# ── ⑤ 只追加：旧上下文 / 原始上报 / 旧记忆条目都不被改写 ──────────


class AppendOnlyTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def test_new_state_never_rewrites_the_old_context_or_the_reported_poi(self):
        reported = home_poi()
        reported_snapshot = copy.deepcopy(reported)
        s = Session.__new__(Session)
        s.runtime = {"player": {"x": 1000.0, "y": 64.0, "z": 1000.0},
                     "remembered_pois": [reported], "players": []}
        s.memory = DummyMemory()
        s.vision = SimpleNamespace(latest=lambda: "")
        s.poi = SimpleNamespace(describe=lambda *a, **k: "", find=lambda *a, **k: [])
        s.territories = TerritoryStore(root=self.root / "territory", server=SERVER,
                                       world_name=WORLD, dimension=OVERWORLD)
        s.last_speaker = {}
        s.chat_inbox = None
        s._hud_note = lambda: ""
        s.persona_blocks = lambda *_a, **_k: {}
        s.conversation = None
        s.llm_active = lambda: True
        s._actor = {"name": "Owner", "tier": "master"}
        s._zone_blocks = []
        s._home_away_since = None

        first = s._runtime_context()
        first_snapshot = copy.deepcopy(first)
        self.assertEqual(first["home_status"]["home_bed"], "present")

        # 下一帧：床被拆了（模组现场核对后摘掉 `bed` 标签，点还在）。
        s.runtime["remembered_pois"] = [home_poi(tags=("home",), category="home")]
        second = s._runtime_context()

        self.assertEqual(first, first_snapshot, "旧上下文一个字段都不许被改写（只追加）")
        self.assertEqual(second["home_status"]["home_bed"], "missing")
        self.assertEqual(reported, reported_snapshot, "模组上报的原始点只读，绝不就地改写")

    def test_poi_memory_keeps_old_points_and_refreshes_the_bed_tag(self):
        poi = WorldPoiMemory(root=str(self.root / "poi"))
        poi.use_namespace("s", "w")
        poi.observe_state({"remembered_pois": [
            home_poi(x=1.0, y=64.0, z=2.0),
            {"block_id": "minecraft:chest", "name": "箱子", "x": 9.0, "y": 64.0, "z": 9.0},
        ]})

        # 床被拆：同一个点主标签退成 home（点还在，别的点一个不动）。
        poi.observe_state({"remembered_pois": [home_poi(tags=("home",), category="home",
                                                        x=1.0, y=64.0, z=2.0)]})

        self.assertEqual(poi.find(BED_TAG), [], "床没了就不能再挂 bed（陈旧读取）")
        homes = poi.find(HOME_TAG)
        self.assertEqual(len(homes), 1)
        self.assertEqual(homes[0]["tags"], ["home"])
        self.assertEqual(poi.find("waypoint")[0]["name"], "箱子", "别的点原样留着（只追加）")

    def test_the_same_point_with_reordered_tags_is_not_rewritten(self):
        poi = WorldPoiMemory(root=str(self.root / "poi2"))
        poi.use_namespace("s", "w")
        poi.observe_state({"remembered_pois": [home_poi(tags=("bed", "home"),
                                                        x=1.0, y=64.0, z=2.0)]})

        changed = poi.observe_state({"remembered_pois": [home_poi(tags=("home", "bed"),
                                                                 x=1.0, y=64.0, z=2.0)]})

        self.assertEqual(changed, 0, "标签只是顺序不同 → 一个字段都不重写（不刷盘）")


if __name__ == "__main__":
    unittest.main()
