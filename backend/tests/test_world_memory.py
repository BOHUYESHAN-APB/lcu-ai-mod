"""站点 / 路径记忆（world 层）落盘 + 回灌。

覆盖：
  ① stations/routes 落盘 + 重启（新实例）读出；
  ② 模组侧空 → 推；模组已有同名同内容 → 不推；同 session 同 world 只推一次（幂等）；
  ③ 推回去的命令帧字段与 Java 契约**逐字一致**（`remember_station` / `remember_route`）；
  ④ **防串号**：同一 server 的两个 world（主世界 vs 下界）同名站点互不影响，
     切 world 后推的是该 world 的那条路径；
  ⑤ 只报事实：没有点列（模组只报点数）的路径**不推**，如实记进 blocked。
  ⑥ 模组只报点数、后端也只有点数 → `route_export` 按需取回几何 → 灌回；拿不到就维持
     `no_geometry`（不现编）；坏点/超 2000 点**既不推也不落盘**；点列不进每帧 state。

全部离线：临时目录落盘 + 假 skills/wire 记录下发帧，不碰真实 data/。
"""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from agent.action_manager import ActionManager
from agent.person_memory import PersonMemoryStore
from agent.session import Session
from agent.skills import Skills
from agent.world_memory import (
    MAX_ROUTE_POINTS,
    MIN_ROUTE_POINTS,
    WorldMemoryStore,
    plan_replay,
    world_scope,
)

SERVER = "play.example:25565"
WORLD = "world-a"
OVERWORLD = "minecraft:overworld"
NETHER = "minecraft:the_nether"


def station(name="home", *, kind="iron_farm", x=10.0, y=64.0, z=-3.5,
            yaw=90.0, pitch=1.5, radius=3.0, tick=120):
    return {"name": name, "kind": kind, "x": x, "y": y, "z": z,
            "yaw": yaw, "pitch": pitch, "radius": radius, "saved_at_tick": tick}


def route(name="tunnel", *, points=None, tick=200):
    return {"name": name,
            "points": points if points is not None else [
                {"x": 1.0, "y": 64.0, "z": 2.0, "yaw": 0.0},
                {"x": 9.0, "y": 66.0, "z": 2.0, "yaw": 90.0},
                {"x": 17.0, "y": 70.0, "z": 6.0},
            ],
            "saved_at_tick": tick}


def state(*, stations=None, routes=None, server=SERVER, world=WORLD,
          dimension=OVERWORLD):
    data = {"world": {"server": server, "world_name": world},
            "player": {"dimension": dimension}}
    if stations is not None:
        data["stations"] = stations
    if routes is not None:
        data["routes"] = routes
    return data


class RecordingSkills:
    """记录每一条下发的回灌帧（`calls` 就是"发了哪些帧"的代表）。"""

    def __init__(self):
        self.calls = []

    def remember_station(self, **args):
        self.calls.append(("remember_station", dict(args)))
        return {"success": True}

    def remember_route(self, name, points):
        self.calls.append(("remember_route", {"name": name, "points": list(points)}))
        return {"success": True}


class RecordingWire:
    def __init__(self):
        self.sent = []

    def send_command(self, cmd, args):
        self.sent.append((cmd, args))
        return f"req-{len(self.sent)}"


# ── ① 落盘 + 重启读出 ───────────────────────────────────────────


class PlacePersistenceTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def store(self) -> WorldMemoryStore:
        return WorldMemoryStore(stations_root=self.root / "stations",
                                routes_root=self.root / "routes")

    def test_stations_and_routes_survive_a_restart(self):
        store = self.store()
        store.use_namespace(SERVER, WORLD, OVERWORLD)
        store.absorb_state(state(stations=[station()], routes=[route()]))

        reloaded = self.store()
        reloaded.use_namespace(SERVER, WORLD, OVERWORLD)

        self.assertEqual(reloaded.stations(), [station()])
        self.assertEqual(reloaded.routes(), [route()])
        self.assertTrue(store.stations_path.exists())
        self.assertTrue(store.routes_path.exists())

    def test_filename_encodes_all_three_world_layer_segments(self):
        store = self.store()
        store.use_namespace(SERVER, WORLD, OVERWORLD)
        store.absorb_state(state(stations=[station()]))

        self.assertEqual(store.namespace,
                         "play.example_25565__world-a__minecraft_overworld")
        self.assertEqual(store.stations_path.name,
                         "play.example_25565__world-a__minecraft_overworld.json")
        # 三段都在（缺的层也不能省）：全是空 → 固定占位。
        self.assertEqual(world_scope("", "", ""), "unknown__unknown__unknown")

    def test_absorb_keeps_mod_reported_fields_verbatim(self):
        """**不加工**：存的就是模组上报的字段，一个键都不多、一个都不改。"""
        store = self.store()
        store.use_namespace(SERVER, WORLD, OVERWORLD)
        reported_station = station()
        # 模组当前对路径只上报**点数**（`points` 是整数）——原样存下来。
        reported_route = {"name": "tunnel", "points": 3, "saved_at_tick": 200}
        store.absorb_state(state(stations=[reported_station], routes=[reported_route]))

        self.assertEqual(store.stations(), [reported_station])
        self.assertEqual(store.routes(), [reported_route])

    def test_absorb_never_deletes_on_an_empty_report(self):
        """模组报空列表是"我这边是空的"，不是"删掉"（后端才是真相来源）。"""
        store = self.store()
        store.use_namespace(SERVER, WORLD, OVERWORLD)
        store.absorb_state(state(stations=[station()]))

        store.absorb_state(state(stations=[], routes=[]))

        self.assertEqual(len(store.stations()), 1)

    def test_same_name_is_overwritten_not_duplicated(self):
        store = self.store()
        store.use_namespace(SERVER, WORLD, OVERWORLD)
        store.absorb_state(state(stations=[station(x=10.0)]))
        store.absorb_state(state(stations=[station(x=99.0)]))

        rows = store.stations()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["x"], 99.0)

    def test_route_geometry_is_not_downgraded_to_a_point_count(self):
        """模组对路径只上报点数 → 不能把后端存的整条点列抹成一个整数。"""
        store = self.store()
        store.use_namespace(SERVER, WORLD, OVERWORLD)
        store.absorb_state(state(routes=[route()]))

        store.absorb_state(state(routes=[{"name": "tunnel", "points": 3, "saved_at_tick": 999}]))

        stored = store.routes()[0]
        self.assertEqual(len(stored["points"]), 3, "点列（坐标）要留着，否则下次回灌不出去")
        self.assertEqual(stored["saved_at_tick"], 999, "其余字段以来件为准")

    def test_route_geometry_is_dropped_when_the_mod_reports_a_different_count(self):
        """模组上重记过（点数对不上）→ 后端那份坐标已过期，宁可退回只有点数。"""
        store = self.store()
        store.use_namespace(SERVER, WORLD, OVERWORLD)
        store.absorb_state(state(routes=[route()]))

        store.absorb_state(state(routes=[{"name": "tunnel", "points": 5, "saved_at_tick": 900}]))

        stored = store.routes()[0]
        self.assertEqual(stored["points"], 5)
        plan = plan_replay([], [stored], [], [])
        self.assertEqual(plan["routes"], [], "旧坐标绝不推出去")
        self.assertEqual(plan["blocked"][0]["reason"], "no_geometry")


# ── ④ 防串号（同一 server 的两个 world）─────────────────────────


class WorldIsolationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def store(self) -> WorldMemoryStore:
        return WorldMemoryStore(stations_root=self.root / "stations",
                                routes_root=self.root / "routes")

    def test_same_station_name_in_two_worlds_does_not_bleed(self):
        store = self.store()
        store.use_namespace(SERVER, WORLD, OVERWORLD)
        store.absorb_state(state(stations=[station(x=10.0)],
                                 routes=[route(points=[{"x": 1, "y": 2, "z": 3}])]))
        store.use_namespace(SERVER, WORLD, NETHER)
        store.absorb_state(state(stations=[station(x=-300.0)],
                                 routes=[route(points=[{"x": 100, "y": 50, "z": 100}])],
                                 dimension=NETHER))

        self.assertEqual(store.namespace, "play.example_25565__world-a__minecraft_the_nether")
        overworld = self.store()
        nether = self.store()
        overworld.use_namespace(SERVER, WORLD, OVERWORLD)
        nether.use_namespace(SERVER, WORLD, NETHER)

        self.assertEqual(overworld.stations()[0]["x"], 10.0)
        self.assertEqual(nether.stations()[0]["x"], -300.0)
        self.assertNotEqual(overworld.stations_path, nether.stations_path)
        self.assertEqual(overworld.routes()[0]["points"], [{"x": 1, "y": 2, "z": 3}])

    def test_world_layer_is_isolated_from_server_and_person_layers(self):
        store = self.store()
        store.use_namespace(SERVER, WORLD, OVERWORLD)
        store.absorb_state(state(stations=[station()]))
        people = PersonMemoryStore(root=self.root / "people")
        people.identify("uuid-steve", "Steve")
        people.remember_fact("uuid-steve", "Steve", "is_master", True)

        # 换 world：站点分区变了；person 层照旧（按人取件，与 world 无关）。
        store.use_namespace(SERVER, WORLD, NETHER)
        self.assertEqual(store.stations(), [])
        self.assertEqual(people.facts("uuid-steve", "Steve"), {"is_master": True})
        self.assertTrue((self.root / "people" / "uuid_uuid-steve.json").exists())
        self.assertFalse((self.root / "stations" / f"{store.namespace}.json").exists())


# ── ② / ⑤ 回灌计划（纯函数）────────────────────────────────────


class ReplayPlanTests(unittest.TestCase):
    def test_push_when_the_mod_side_is_empty(self):
        plan = plan_replay([station()], [route()], [], [])

        self.assertEqual(len(plan["stations"]), 1)
        self.assertEqual(len(plan["routes"]), 1)
        self.assertEqual(plan["blocked"], [])
        self.assertEqual(plan["skipped"], [])

    def test_identical_entries_are_not_pushed(self):
        plan = plan_replay([station()], [route()], [station()], [route()])

        self.assertEqual(plan["stations"], [])
        self.assertEqual(plan["routes"], [])
        self.assertEqual([row["reason"] for row in plan["skipped"]], ["identical", "identical"])

    def test_changed_station_is_pushed(self):
        plan = plan_replay([station(x=10.0)], [], [station(x=99.0)], [])

        self.assertEqual(plan["stations"], [{"name": "home", "kind": "iron_farm",
                                             "x": 10.0, "y": 64.0, "z": -3.5,
                                             "yaw": 90.0, "pitch": 1.5, "radius": 3.0}])
        self.assertEqual(plan["skipped"], [])

    def test_route_without_geometry_is_blocked_never_fabricated(self):
        stored = {"name": "tunnel", "points": 3, "saved_at_tick": 200}
        plan = plan_replay([], [stored], [], [])

        self.assertEqual(plan["routes"], [])
        self.assertEqual(plan["blocked"],
                         [{"kind": "route", "name": "tunnel", "reason": "no_geometry"}])

    def test_two_count_only_routes_are_never_identical(self):
        """两边都只有点数、都没几何 → 不记 identical：这是几何缺口，要留给 route_export 去取。"""
        stored = {"name": "tunnel", "points": 3, "saved_at_tick": 200}
        reported = {"name": "tunnel", "points": 3, "saved_at_tick": 250}

        plan = plan_replay([], [stored], [], [reported])

        self.assertEqual(plan["routes"], [], "没有几何就绝不推")
        self.assertEqual(plan["skipped"], [], "几何缺失不能记成 identical（否则永远不去取）")
        self.assertEqual(plan["blocked"],
                         [{"kind": "route", "name": "tunnel", "reason": "no_geometry"}])

    def test_geometry_backed_route_still_matches_a_reported_count(self):
        """后端存着几何、模组只报点数：点数一致就算同一份（不每轮无脑重推）。"""
        plan = plan_replay([], [route()], [], [{"name": "tunnel", "points": 3}])

        self.assertEqual(plan["routes"], [])
        self.assertEqual([row["reason"] for row in plan["skipped"]], ["identical"])

    def test_station_without_coordinates_is_blocked(self):
        plan = plan_replay([{"name": "home", "kind": "custom"}], [], [], [])

        self.assertEqual(plan["stations"], [])
        self.assertEqual(plan["blocked"],
                         [{"kind": "station", "name": "home", "reason": "no_coords"}])

    def test_mod_not_reporting_means_no_push(self):
        plan = plan_replay([station()], [route()], None, None)

        self.assertEqual(plan["stations"], [])
        self.assertEqual(plan["routes"], [])
        self.assertEqual([row["reason"] for row in plan["blocked"]],
                         ["mod_not_reporting", "mod_not_reporting"])

    def test_too_short_route_is_not_pushed(self):
        short = {"name": "stub", "points": [{"x": 1, "y": 2, "z": 3}], "saved_at_tick": 1}
        plan = plan_replay([], [short], [], [])

        self.assertEqual(plan["routes"], [], f"点数 <{MIN_ROUTE_POINTS} 不该推")
        self.assertEqual(plan["blocked"][0]["reason"], "no_geometry")


# ── ③ 命令帧逐字对齐 Java 契约 ──────────────────────────────────


class WireContractTests(unittest.TestCase):
    """`remember_station` / `remember_route` 的帧字段名与 Java 逐字一致。"""

    def setUp(self):
        self.wire = RecordingWire()
        self.skills = Skills(wire=self.wire)

    def test_remember_station_sends_exactly_the_java_arg_names(self):
        self.skills.remember_station(name="home", kind="iron_farm", x=1.5, y=64.0,
                                     z=-3.25, yaw=90.0, pitch=1.5, radius=3.0)

        self.assertEqual(self.wire.sent, [(
            "remember_station",
            {"name": "home", "kind": "iron_farm", "x": 1.5, "y": 64.0, "z": -3.25,
             "yaw": 90.0, "pitch": 1.5, "radius": 3.0},
        )])

    def test_remember_station_omits_missing_optionals(self):
        """缺的字段就不发（Java 侧 optDouble 返回 null → 走它自己的缺省）。"""
        self.skills.remember_station(name="home", x=1.0, y=2.0, z=3.0)

        self.assertEqual(self.wire.sent, [("remember_station",
                                           {"name": "home", "x": 1.0, "y": 2.0, "z": 3.0})])

    def test_remember_route_sends_name_and_points_array(self):
        points = [{"x": 1.0, "y": 2.0, "z": 3.0, "yaw": 0.0}, {"x": 4.0, "y": 5.0, "z": 6.0}]
        self.skills.remember_route("tunnel", points)

        self.assertEqual(self.wire.sent, [("remember_route",
                                           {"name": "tunnel", "points": points})])


# ── ② 会话接线：吸收 → 回灌 → 幂等 ─────────────────────────────


class SessionReplayTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def places(self) -> WorldMemoryStore:
        return WorldMemoryStore(stations_root=self.root / "stations",
                                routes_root=self.root / "routes")

    def session(self, store=None) -> Session:
        session = Session.__new__(Session)
        session.runtime = {}
        session.places = store or self.places()
        session.people = PersonMemoryStore(root=self.root / "people")
        session.poi = SimpleNamespace(use_namespace=lambda *a: "", observe_state=lambda *a: 0)
        session.conversation = None
        session.skills = RecordingSkills()
        session._replay_layers = set()
        session._people_seen = {}
        session._present_person_facts = {}
        return session

    def seed(self, store, *, stations, routes, world=WORLD, dimension=OVERWORLD):
        store.use_namespace(SERVER, world, dimension)
        store.absorb_state(state(stations=stations, routes=routes,
                                 world=world, dimension=dimension))
        store.save()

    def test_mod_empty_pushes_back_the_saved_memory(self):
        store = self.places()
        self.seed(store, stations=[station()], routes=[route()])
        session = self.session(store)

        session.handle_event("state_update", state(stations=[], routes=[]))

        self.assertEqual(session.skills.calls, [
            ("remember_station", {"name": "home", "kind": "iron_farm", "x": 10.0,
                                  "y": 64.0, "z": -3.5, "yaw": 90.0, "pitch": 1.5,
                                  "radius": 3.0}),
            ("remember_route", {"name": "tunnel", "points": route()["points"]}),
        ])
        self.assertIn(f"world:{store.namespace}", session._replay_layers)

    def test_replay_is_idempotent_within_the_same_world(self):
        store = self.places()
        self.seed(store, stations=[station()], routes=[route()])
        session = self.session(store)

        session.handle_event("state_update", state(stations=[], routes=[]))
        session.handle_event("state_update", state(stations=[], routes=[]))
        session.handle_event("state_update", state(stations=[], routes=[]))

        self.assertEqual(len(session.skills.calls), 2, "同一个 world 只推一次")

    def test_mod_side_already_has_it_no_frame_is_sent(self):
        store = self.places()
        self.seed(store, stations=[station()], routes=[route()])
        session = self.session(store)

        session.handle_event("state_update", state(stations=[station()], routes=[route()]))

        self.assertEqual(session.skills.calls, [], "同名同内容就不推（避免无意义覆盖）")

    def test_no_push_when_the_backend_has_nothing_saved(self):
        session = self.session()

        session.handle_event("state_update", state(stations=[], routes=[]))

        self.assertEqual(session.skills.calls, [])

    def test_switching_world_pushes_that_worlds_route(self):
        store = self.places()
        self.seed(store, stations=[], routes=[route(points=[{"x": 1, "y": 2, "z": 3},
                                                            {"x": 2, "y": 3, "z": 4}])])
        self.seed(store, stations=[], routes=[route(points=[{"x": 100, "y": 50, "z": 100},
                                                            {"x": 120, "y": 48, "z": 88}])],
                  world=WORLD, dimension=NETHER)
        session = self.session(store)

        session.handle_event("state_update", state(stations=[], routes=[], dimension=OVERWORLD))
        session.handle_event("state_update", state(stations=[], routes=[], dimension=NETHER))

        self.assertEqual([call[1]["points"] for call in session.skills.calls], [
            [{"x": 1, "y": 2, "z": 3}, {"x": 2, "y": 3, "z": 4}],
            [{"x": 100, "y": 50, "z": 100}, {"x": 120, "y": 48, "z": 88}],
        ], "切到哪个 world 就推那个 world 的那条，不推上一个")

    def test_layer_markers_are_per_layer_and_idempotent(self):
        store = self.places()
        self.seed(store, stations=[station()], routes=[route()])
        session = self.session(store)
        session.conversation = SimpleNamespace(use_namespace=lambda *a: "")

        session.handle_event("state_update", state(stations=[], routes=[]))
        session.handle_event("state_update", state(stations=[], routes=[]))

        layers = set(session._replay_layers)
        self.assertIn("global", layers, "global 层要留痕")
        self.assertTrue(any(key.startswith("world:") for key in layers), "world 层按世界分开记")
        self.assertEqual(len(session.skills.calls), 2, "同一个 world 只推一次")
        status = session._memory_layers_status()
        self.assertEqual(status["world"]["stations"], 1)
        self.assertEqual(status["world"]["routes"], 1)
        self.assertEqual(sorted(status["replayed_layers"]), sorted(layers))

    def test_station_not_reported_by_the_mod_is_passed_through_verbatim(self):
        """模组报的字段原样落盘、原样推回（缺 yaw/pitch/radius 就不推那几项）。"""
        store = self.places()
        lean = {"name": "spawn", "kind": "custom", "x": 0.5, "y": 70.0, "z": 0.5,
                "saved_at_tick": 5}
        self.seed(store, stations=[lean], routes=[])
        session = self.session(store)

        session.handle_event("state_update", state(stations=[], routes=[]))

        self.assertEqual(session.skills.calls, [
            ("remember_station", {"name": "spawn", "kind": "custom",
                                  "x": 0.5, "y": 70.0, "z": 0.5}),
        ])


# ── ⑥ route_export 回灌（模组只报点数 → 按需取回几何 → 灌回）──────

# 后端只存到"点数"的那种路径（模组当时只报了点数，没几何）。
COUNT_ONLY = {"name": "tunnel", "points": 3, "saved_at_tick": 200}

# `route_export` 回执里的点列（Java 侧原样导出：`{x,y,z,yaw}`）。
EXPORT_POINTS = [{"x": 1.0, "y": 64.0, "z": 2.0, "yaw": 0.0},
                 {"x": 9.0, "y": 66.0, "z": 2.0, "yaw": 90.0},
                 {"x": 17.0, "y": 70.0, "z": 6.0, "yaw": 180.0}]


class ExportSkills(RecordingSkills):
    """回灌 skills + `route_export`：`calls` 里同时记下按需取几何的那一帧。"""

    def route_export(self, name):
        self.calls.append(("route_export", {"name": name}))
        return {"success": True, "req_id": f"export-{len(self.calls)}"}


class RouteExportReplayTests(unittest.TestCase):
    """拿得到几何就取回并灌回；拿不到就维持诚实的 `no_geometry`（绝不现编坐标）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def places(self) -> WorldMemoryStore:
        return WorldMemoryStore(stations_root=self.root / "stations",
                                routes_root=self.root / "routes")

    def session(self, store=None) -> Session:
        session = Session.__new__(Session)
        session.runtime = {}
        session.places = store or self.places()
        session.people = PersonMemoryStore(root=self.root / "people")
        session.poi = SimpleNamespace(use_namespace=lambda *a: "", observe_state=lambda *a: 0)
        session.conversation = None
        session.skills = ExportSkills()
        session.action_manager = ActionManager()
        session._replay_layers = set()
        session._route_exports = {}
        session._route_exported = set()
        session._manual_action_reqs = set()
        session._manual_task_kind = None
        session._people_seen = {}
        session._present_person_facts = {}
        return session

    def seed_count_only(self, store) -> None:
        """后端只有"点数"的那条路径（模组侧没上报它 → 回灌时缺几何）。"""
        store.use_namespace(SERVER, WORLD, OVERWORLD)
        store.absorb_state(state(routes=[dict(COUNT_ONLY)]))
        store.save()

    def blocked(self, name="tunnel"):
        return [{"kind": "route", "name": name, "reason": "no_geometry"}]

    # ── ① 有几何 → 取回 → 下发 ──

    def test_count_only_route_is_exported_then_pushed_back_verbatim(self):
        store = self.places()
        self.seed_count_only(store)
        session = self.session(store)

        session.handle_event("state_update", state(routes=[]))
        self.assertEqual(session.skills.calls,
                         [("route_export", {"name": "tunnel"})],
                         "只有这一条按需出口，帧里**只有名字**（点列不进 state）")

        session.handle_event("command_response",
                             {"id": "export-1", "success": True,
                              "data": {"ok": True, "name": "tunnel",
                                       "points": EXPORT_POINTS,
                                       "count": len(EXPORT_POINTS),
                                       "saved_at_tick": 200}})

        self.assertEqual(session.skills.calls[-1],
                         ("remember_route", {"name": "tunnel", "points": EXPORT_POINTS}),
                         "取回的点列**原样**灌回（不抽稀、不加工）")
        self.assertEqual(store.route("tunnel")["points"], EXPORT_POINTS,
                         "取回的几何要落盘，下次才灌得出去")
        self.assertEqual(session._route_exports, {}, "回执到了就把待办清掉（不重发）")

    # ── ② 取不到：如实 blocked，不编坐标 ──

    def test_unknown_route_reply_is_reported_without_raising(self):
        store = self.places()
        self.seed_count_only(store)
        session = self.session(store)
        session.handle_event("state_update", state(routes=[]))

        session.handle_event("command_response",
                             {"id": "export-1", "success": False, "error": "unknown_route",
                              "data": {"ok": False, "name": "tunnel",
                                       "reason": "unknown_route"}})

        self.assertEqual([call[0] for call in session.skills.calls], ["route_export"],
                         "unknown_route 不报错、也不灌任何东西")
        self.assertEqual(store.route("tunnel"), COUNT_ONLY, "维持只有点数，绝不现编坐标")

    def test_no_geometry_reply_keeps_the_honest_blocked_state(self):
        store = self.places()
        self.seed_count_only(store)
        session = self.session(store)
        session.handle_event("state_update", state(routes=[]))

        session.handle_event("command_response",
                             {"id": "export-1", "success": False, "error": "no_geometry",
                              "data": {"ok": False, "name": "tunnel",
                                       "reason": "no_geometry"}})

        self.assertEqual([call[0] for call in session.skills.calls], ["route_export"])
        self.assertEqual(store.route("tunnel"), COUNT_ONLY)

    # ── ③ 坏点 / 超限：不推，也不落盘 ──

    def test_malformed_reply_is_neither_stored_nor_pushed(self):
        store = self.places()
        self.seed_count_only(store)
        session = self.session(store)
        session.handle_event("state_update", state(routes=[]))

        session._absorb_route_export(
            {"id": "export-1", "success": True,
             "data": {"ok": True, "name": "tunnel",
                      "points": [{"x": 1.0, "y": 64.0, "z": 2.0},
                                 {"x": None, "y": 66.0, "z": 2.0}],
                      "count": 2}})

        self.assertEqual([call[0] for call in session.skills.calls], ["route_export"],
                         "有一个坏点就整条作废，绝不推")
        self.assertEqual(store.route("tunnel"), COUNT_ONLY,
                         "半条废点列**不落盘**（存下来就会被当成'模组侧已经有了'）")

    def test_oversized_reply_is_neither_stored_nor_pushed(self):
        store = self.places()
        self.seed_count_only(store)
        session = self.session(store)
        session.handle_event("state_update", state(routes=[]))

        too_many = [{"x": float(i), "y": 64.0, "z": 0.0} for i in range(MAX_ROUTE_POINTS + 1)]
        session._absorb_route_export({"id": "export-1", "success": True,
                                      "data": {"ok": True, "name": "tunnel",
                                               "points": too_many, "count": len(too_many)}})

        self.assertEqual([call[0] for call in session.skills.calls], ["route_export"],
                         f"点数 >{MAX_ROUTE_POINTS} 会被 Java 整条拒，本地先挡（不推）")
        self.assertEqual(store.route("tunnel"), COUNT_ONLY, "超限的点列也不落盘")

    # ── ④ 不重试风暴 / 不串号 ──

    def test_one_export_attempt_per_route_per_session(self):
        store = self.places()
        session = self.session(store)

        first = session._request_route_exports(self.blocked(), store)
        second = session._request_route_exports(self.blocked(), store)

        self.assertEqual(first, ["tunnel"])
        self.assertEqual(second, [], "同一条路径一次会话只试一次（失败等下次进世界）")
        self.assertEqual([call[0] for call in session.skills.calls], ["route_export"])

    def test_a_late_reply_for_another_world_is_dropped(self):
        store = self.places()
        self.seed_count_only(store)
        session = self.session(store)
        session._route_exports["export-1"] = {"name": "tunnel",
                                             "namespace": "other__world__dim"}

        absorbed = session._absorb_route_export(
            {"id": "export-1", "success": True,
             "data": {"ok": True, "name": "tunnel", "points": EXPORT_POINTS,
                      "count": len(EXPORT_POINTS)}})

        self.assertFalse(absorbed, "分区早就换了：迟到回执灌错 world 就是串号，宁可丢掉")
        self.assertEqual(session.skills.calls, [])
        self.assertEqual(store.route("tunnel"), COUNT_ONLY)

    # ── ⑤ 点列绝不进每帧 state ──

    def test_no_state_frame_carries_a_point_array(self):
        """每帧走的是"有没有点数/哪条路径"，点列只从**按需动作**出口走。"""
        store = self.places()
        self.seed_count_only(store)
        session = self.session(store)

        # 模组报这条路径时**只报点数**：存下来就是那三个字段，后端不凭空补点列。
        session.handle_event("state_update",
                             state(routes=[{"name": "tunnel", "points": 3, "saved_at_tick": 250}]))
        self.assertEqual(store.route("tunnel")["points"], 3, "点数照存，几何不臆造")

        with_points = [name for name, _ in session.skills.calls if name != "route_export"]
        self.assertEqual(with_points, [], "这一轮只有 route_export：点列不进 state")

    # ── ⑥ 点数不一致：不记 identical，去核对一次 ──

    def seed_geometry(self, store, points=None) -> None:
        """后端存着**带几何**的路径（模组早先报过点列）。"""
        store.use_namespace(SERVER, WORLD, OVERWORLD)
        store.absorb_state(state(routes=[route(points=points or EXPORT_POINTS)]))
        store.save()

    def test_count_mismatch_triggers_one_export_and_never_pushes_stale_geometry(self):
        store = self.places()
        self.seed_geometry(store)
        session = self.session(store)

        session.handle_event("state_update",
                             state(routes=[{"name": "tunnel", "points": 5, "saved_at_tick": 900}]))

        self.assertEqual(store.route("tunnel")["points"], 5,
                         "点数对不上 → 旧坐标已过期，退回只有点数")
        self.assertEqual([call[0] for call in session.skills.calls], ["route_export"],
                         "缺几何不能记 identical-by-count 了事：要核对一次")
        self.assertEqual(len(session._route_exports), 1, "记下待办回执（回执到了才灌）")

    def test_count_mismatch_is_not_requested_twice_in_one_session(self):
        store = self.places()
        self.seed_geometry(store)
        session = self.session(store)

        session.handle_event("state_update",
                             state(routes=[{"name": "tunnel", "points": 5, "saved_at_tick": 900}]))
        session.handle_event("state_update",
                             state(routes=[{"name": "tunnel", "points": 5, "saved_at_tick": 901}]))

        self.assertEqual([call[0] for call in session.skills.calls], ["route_export"],
                         "同一条路径一次会话只试一次（不重试风暴）")

    def test_count_mismatch_export_failure_keeps_the_honest_count_only_row(self):
        store = self.places()
        self.seed_geometry(store)
        session = self.session(store)

        session.handle_event("state_update",
                             state(routes=[{"name": "tunnel", "points": 5, "saved_at_tick": 900}]))
        session.handle_event("command_response",
                             {"id": "export-1", "success": False, "error": "no_geometry",
                              "data": {"ok": False, "name": "tunnel", "reason": "no_geometry"}})

        self.assertEqual(store.route("tunnel"),
                         {"name": "tunnel", "points": 5, "saved_at_tick": 900},
                         "取不到就维持只有点数，绝不落盘废点列")
        self.assertEqual([call[0] for call in session.skills.calls], ["route_export"],
                         "失败不推任何东西")

    def test_count_mismatch_recovered_geometry_is_stored_verbatim_and_pushed(self):
        store = self.places()
        self.seed_geometry(store)
        session = self.session(store)

        session.handle_event("state_update",
                             state(routes=[{"name": "tunnel", "points": 5, "saved_at_tick": 900}]))
        recovered = [{"x": 1.0, "y": 64.0, "z": 2.0, "yaw": 0.0},
                     {"x": 5.0, "y": 65.0, "z": 2.0, "yaw": 45.0},
                     {"x": 9.0, "y": 66.0, "z": 2.0, "yaw": 90.0},
                     {"x": 13.0, "y": 68.0, "z": 4.0, "yaw": 135.0},
                     {"x": 17.0, "y": 70.0, "z": 6.0, "yaw": 180.0}]
        session.handle_event("command_response",
                             {"id": "export-1", "success": True,
                              "data": {"ok": True, "name": "tunnel",
                                       "points": recovered, "count": len(recovered)}})

        self.assertEqual(store.route("tunnel")["points"], recovered,
                         "取回的真几何**逐字**落盘（模组侧是真相）")
        self.assertEqual(session.skills.calls[-1],
                         ("remember_route", {"name": "tunnel", "points": recovered}),
                         "取回的点列原样灌回模组（不抽稀、不加工）")


if __name__ == "__main__":
    unittest.main()
