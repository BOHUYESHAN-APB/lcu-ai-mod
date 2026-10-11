"""`where_is` 的验收：一次问、给候选、让模型挑（**只答位置，绝不移动、绝不编坐标**）。

覆盖票据的验收清单：
  ① 命中（在线玩家）/ 模糊命中 / 歧义（多候选）；
  ② 完全问不到 → 空清单 + 如实 note（**不是** exception），且**没有任何编造的坐标**；
  ③ `kind` 过滤；④ 截断（候选太多）；⑤ 方块/物品 → 指引用 `find_block`（不在这里扫）；
  ⑥ 聚合的是**现成存储**：state 在线玩家 + world 层记忆（记忆点 POI / 站点 / 容器 / 区域 / 基地）；
  ⑦ 工具登记正确（参数 `target,kind`、handler 可调、每阶段都能用）；
  ⑧ 描述只走 L3（工具表），**不进** L0/L1 静态前缀；
  ⑨ 调用它**不会自己移动**（skills 一次都没被调）。
"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent import where  # noqa: E402
from agent.memory import WorldPoiMemory  # noqa: E402
from agent.planner import PHASE_GROUPS, Planner, _actions_for_phase  # noqa: E402
from agent.territory import TerritoryStore  # noqa: E402
from agent.tool_registry import TOOL_BY_NAME  # noqa: E402
from agent.world_memory import WorldMemoryStore  # noqa: E402

PLAYER_POS = (0.0, 64.0, 0.0)


class DummyMemory:
    def build_context(self):
        return {}

    def get_recent_context(self, _count):
        return []


class DummySkills:
    def __init__(self):
        self.calls: list[tuple] = []

    def __getattr__(self, name):
        def _record(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            return {"success": True}

        return _record


def _planner(skills=None, **kwargs) -> Planner:
    return Planner(llm_service=None, memory=DummyMemory(),
                   skills=skills if skills is not None else DummySkills(), **kwargs)


def _src(kind, source, rows):
    return {"kind": kind, "source": source, "rows": rows}


class RegistryTests(unittest.TestCase):
    def test_registered_with_target_and_kind(self):
        spec = TOOL_BY_NAME.get("where_is")

        self.assertIsNotNone(spec, "where_is 必须登记在注册表（工具的唯一事实来源）")
        self.assertEqual(spec.args, "target,kind")
        self.assertTrue(callable(getattr(Planner, spec.handler, None)))
        self.assertEqual(spec.schema()["function"]["parameters"]["properties"].keys(),
                         {"target", "kind"})

    def test_description_teaches_the_closed_loop(self):
        """描述必须把闭环写清：先 where_is 拿候选 → 挑一个 → move_to/go_to_block/follow 过去。"""
        text = TOOL_BY_NAME["where_is"].description

        self.assertIn("where_is", text.lower())
        for follow_up in ("move_to", "go_to_block", "follow"):
            self.assertIn(follow_up, text)
        self.assertIn("不猜坐标", text)

    def test_available_in_every_phase(self):
        for phase in PHASE_GROUPS:
            names = {name for name, _, _ in _actions_for_phase(phase)}
            self.assertIn("where_is", names, phase)

    def test_stays_out_of_the_static_prefix(self):
        """描述只走 L3（tools 参数），绝不进 L0/L1 静态前缀。"""
        layers = _planner().build_prompt_layers(
            "Steve", "hi", {"task_state": {"kind": "idle", "status": "idle"}}, "AI")

        self.assertNotIn("where_is", layers.system)
        self.assertIn("where_is", [item["function"]["name"] for item in layers.tools])


class FindTests(unittest.TestCase):
    def test_exact_hit_carries_coords_distance_and_source(self):
        rows = [{"name": "Steve", "x": 3.0, "y": 64.0, "z": 4.0}]

        result = where.find("Steve", player_pos=PLAYER_POS,
                            sources=[_src(where.KIND_PLAYER, where.SOURCE_PLAYERS, rows)])

        self.assertTrue(result["found"])
        self.assertEqual(len(result["candidates"]), 1)
        candidate = result["candidates"][0]
        self.assertEqual(candidate["name"], "Steve")
        self.assertEqual((candidate["x"], candidate["y"], candidate["z"]), (3.0, 64.0, 4.0))
        self.assertEqual(candidate["distance"], 5.0)      # 3-4-0 直角三角形
        self.assertEqual(candidate["source"], where.SOURCE_PLAYERS)
        self.assertIn("(3,64,4)", result["note"])
        self.assertEqual(result["note"], where.describe(result))

    def test_fuzzy_name_hit(self):
        rows = [{"name": "Steve_123", "x": 1.0, "y": 2.0, "z": 3.0}]

        result = where.find("steve", sources=[
            _src(where.KIND_PLAYER, where.SOURCE_PLAYERS, rows)])

        self.assertEqual([c["name"] for c in result["candidates"]], ["Steve_123"])

    def test_ambiguous_target_returns_every_candidate_sorted_by_distance(self):
        rows = [
            {"name": "chest_far", "x": 100.0, "y": 64.0, "z": 0.0},
            {"name": "chest_near", "x": 5.0, "y": 64.0, "z": 0.0},
        ]

        result = where.find("chest", player_pos=PLAYER_POS, sources=[
            _src(where.KIND_CONTAINER, where.SOURCE_TERRITORY, rows)])

        self.assertEqual([c["name"] for c in result["candidates"]],
                         ["chest_near", "chest_far"])
        self.assertTrue(all(c["kind"] == where.KIND_CONTAINER
                            for c in result["candidates"]))

    def test_every_candidate_coordinate_comes_from_a_source_row(self):
        """候选坐标只许逐字来自来源行——这就是"绝不编坐标"的结构性保证。"""
        rows = [{"name": "Steve", "x": 3.0, "y": 64.0, "z": 4.0},
                {"name": "Steve_b", "x": -7.5, "y": 12.0, "z": 900.0}]
        allowed = {(r["x"], r["y"], r["z"]) for r in rows}

        result = where.find("steve", sources=[
            _src(where.KIND_PLAYER, where.SOURCE_PLAYERS, rows)])

        self.assertEqual(len(result["candidates"]), 2)
        for candidate in result["candidates"]:
            self.assertIn((candidate["x"], candidate["y"], candidate["z"]), allowed)

    def test_kind_filter_limits_the_sources(self):
        sources = [
            _src(where.KIND_PLAYER, where.SOURCE_PLAYERS,
                 [{"name": "home", "x": 1.0, "y": 2.0, "z": 3.0}]),
            _src(where.KIND_CONTAINER, where.SOURCE_TERRITORY,
                 [{"name": "home-chest", "x": 9.0, "y": 2.0, "z": 3.0}]),
        ]

        only_players = where.find("home", kind=where.KIND_PLAYER, sources=sources)
        only_containers = where.find("home", kind=where.KIND_CONTAINER, sources=sources)

        self.assertEqual([c["kind"] for c in only_players["candidates"]],
                         [where.KIND_PLAYER])
        self.assertEqual([c["kind"] for c in only_containers["candidates"]],
                         [where.KIND_CONTAINER])
        auto = where.find("home", player_pos=PLAYER_POS, sources=sources)
        self.assertEqual({c["kind"] for c in auto["candidates"]},
                         {where.KIND_PLAYER, where.KIND_CONTAINER})

    def test_truncation_keeps_the_nearest_and_flags_it(self):
        rows = [{"name": f"chest_{i}", "x": float(i), "y": 0.0, "z": 0.0}
                for i in range(12)]

        result = where.find("chest", player_pos=PLAYER_POS, sources=[
            _src(where.KIND_CONTAINER, where.SOURCE_TERRITORY, rows)])

        self.assertTrue(result["truncated"])
        self.assertEqual(len(result["candidates"]), where.DEFAULT_LIMIT)
        self.assertEqual(result["candidates"][0]["name"], "chest_0")
        self.assertIn("还有更多", where.describe(result))
        self.assertIn("最近的", where.describe(result))

        # 没给玩家坐标 → 排不出远近，就别谎称"最近的"。
        blind = where.find("chest", sources=[
            _src(where.KIND_CONTAINER, where.SOURCE_TERRITORY, rows)])
        self.assertTrue(blind["truncated"])
        self.assertIn("只报了前", blind["note"])
        self.assertNotIn("最近的", blind["note"])

    def test_unknown_target_returns_nothing_and_an_honest_note(self):
        result = where.find("totally_unknown_thing", player_pos=PLAYER_POS, sources=[
            _src(where.KIND_PLAYER, where.SOURCE_PLAYERS,
                 [{"name": "Steve", "x": 1.0, "y": 2.0, "z": 3.0}])])

        self.assertFalse(result["found"])
        self.assertEqual(result["candidates"], [])
        note = where.describe(result)
        self.assertIn("不知道", note)
        self.assertIn("我不猜坐标", note)
        self.assertEqual(result["note"], note)

    def test_unknown_target_never_invents_coordinates(self):
        """**关键**：问不到时返回里不许出现任何编造的坐标。"""
        result = where.find("钻石矿", sources=[
            _src(where.KIND_POI, where.SOURCE_REMEMBERED,
                 [{"name": "home", "x": 10.0, "y": 64.0, "z": 10.0}])])

        self.assertEqual(result["candidates"], [])
        note = where.describe(result)
        self.assertNotIn("@(", note, "空结果绝不能带坐标")
        for token in ("10.0", "(10,", "64.0"):
            self.assertNotIn(token, note)

    def test_block_or_item_target_is_pointed_at_find_block(self):
        result = where.find("diamond_ore", player_pos=PLAYER_POS)

        self.assertEqual(result["candidates"], [])
        self.assertIn("find_block", result["note"])
        self.assertIn("看着像方块/物品", result["note"])
        self.assertIn("我不在这里自己扫", result["note"])

    def test_player_or_chinese_target_is_not_mistaken_for_a_block(self):
        result = where.find("Steve", sources=[
            _src(where.KIND_PLAYER, where.SOURCE_PLAYERS, [])])

        self.assertFalse(where.looks_like_block_or_item("Steve"))
        self.assertFalse(where.looks_like_block_or_item("下界要塞"))
        self.assertTrue(where.looks_like_block_or_item("minecraft:chest"))
        self.assertNotIn("看着像", result["note"])
        self.assertIn("find_block", result["note"])

    def test_row_without_geometry_is_skipped_not_invented(self):
        """名字对上了但没坐标 → 只计数（skipped），**绝不补一个坐标**。"""
        rows = [{"name": "cave-base"}]      # 记过名、没记坐标

        result = where.find("cave-base", sources=[
            _src(where.KIND_STATION, where.SOURCE_STATIONS, rows)])

        self.assertEqual(result["candidates"], [])
        self.assertEqual(result["skipped"], 1)
        note = where.describe(result)
        self.assertIn("没有坐标", note)
        self.assertNotIn("@(", note)

    def test_unknown_kind_is_refused_honestly(self):
        result = where.find("Steve", kind="teleporter", sources=[
            _src(where.KIND_PLAYER, where.SOURCE_PLAYERS,
                 [{"name": "Steve", "x": 1.0, "y": 2.0, "z": 3.0}])])

        self.assertEqual(result["candidates"], [])
        self.assertIn("不认得", where.describe(result))

    def test_ambiguous_input_never_raises(self):
        """畸形输入也只是"问不到"，绝不抛异常（工具是聊天路径上的一环）。"""
        for target in ("", None, 123, "   "):
            for sources in (None, [], [None, {}], [{"kind": "player", "rows": "x"}]):
                result = where.find(target, sources=sources or [])
                self.assertEqual(result["candidates"], [])
                self.assertIsInstance(where.describe(result), str)


class SourceAggregationTests(unittest.TestCase):
    """聚合的是**现成存储**：结算成候选的过程不再新建第二套。"""

    def test_players_come_from_the_turn_context(self):
        context = {"players": [{"name": "Alex", "x": 7.0, "y": 64.0, "z": 0.0}]}

        sources = where.gather_sources(context)
        result = where.find("alex", player_pos=PLAYER_POS, sources=sources)

        self.assertEqual(result["candidates"][0]["source"], where.SOURCE_PLAYERS)
        self.assertEqual(result["candidates"][0]["distance"], 7.0)

    def test_remembered_pois_and_known_containers_come_from_the_context(self):
        context = {
            "remembered_pois": [{"name": "spawn", "category": "waypoint",
                                 "x": 0.0, "y": 70.0, "z": 0.0}],
            "known_containers": [{"name": "仓库", "block_id": "minecraft:chest",
                                  "x": 20.0, "y": 64.0, "z": 5.0}],
            "nearby_storage": [{"block_id": "minecraft:barrel",
                                "x": 2.0, "y": 64.0, "z": 2.0}],
        }

        sources = where.gather_sources(context)

        self.assertEqual(where.find("spawn", sources=sources)["candidates"][0]["kind"],
                         where.KIND_POI)
        self.assertEqual(where.find("仓库", sources=sources)["candidates"][0]["kind"],
                         where.KIND_CONTAINER)
        barrel = where.find("barrel", sources=sources)["candidates"][0]
        self.assertEqual(barrel["source"], where.SOURCE_NEARBY_STORAGE)

    def test_station_memory_comes_from_the_world_memory_store(self):
        with tempfile.TemporaryDirectory() as tmp:
            places = WorldMemoryStore(stations_root=Path(tmp) / "s",
                                      routes_root=Path(tmp) / "r")
            places.absorb_state({"stations": [
                {"name": "挂机点", "kind": "afk", "x": 31.0, "y": 65.0, "z": -7.0}]})

            sources = where.gather_sources({}, places=places)
            result = where.find("挂机", player_pos=PLAYER_POS, sources=sources)

            self.assertEqual(len(result["candidates"]), 1)
            station = result["candidates"][0]
            self.assertEqual(station["kind"], where.KIND_STATION)
            self.assertEqual(station["source"], where.SOURCE_STATIONS)
            self.assertEqual((station["x"], station["y"], station["z"]), (31.0, 65.0, -7.0))

    def test_poi_memory_store_is_aggregated(self):
        with tempfile.TemporaryDirectory() as tmp:
            poi = WorldPoiMemory(root=tmp)
            poi.use_namespace("srv", "world", "minecraft:overworld")
            poi.observe("waypoint", {"name": "营地", "block_id": "waypoint",
                                     "x": 1.0, "y": 64.0, "z": 1.0})
            poi.observe("storage", {"name": "箱子A", "block_id": "minecraft:chest",
                                    "x": 8.0, "y": 64.0, "z": 8.0, "contents": []})

            sources = where.gather_sources({}, poi=poi)

            waypoint = where.find("营地", sources=sources)["candidates"][0]
            self.assertEqual(waypoint["kind"], where.KIND_POI)
            self.assertEqual(waypoint["source"], where.SOURCE_POI_MEMORY)
            chest = where.find("箱子A", sources=sources)["candidates"][0]
            self.assertEqual(chest["kind"], where.KIND_POI)

    def test_territory_store_supplies_containers_zones_and_bases(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TerritoryStore(root=tmp)
            store.remember_container("仓库箱", x=4.0, y=64.0, z=4.0)
            store.set_base("老家", anchor={"x": -20.0, "y": 63.0, "z": 30.0}, radius=24.0)
            store.set_zone({"name": "麦田", "kind": "farm", "protect": ["crops"],
                            "shape": "radius", "center": {"x": 50.0, "y": 64.0, "z": 50.0},
                            "radius": 10.0, "box": None})

            sources = where.gather_sources({}, territory=store)

            self.assertEqual(where.find("仓库箱", sources=sources)["candidates"][0]["kind"],
                             where.KIND_CONTAINER)
            self.assertEqual(where.find("老家", sources=sources)["candidates"][0]["kind"],
                             where.KIND_BASE)
            self.assertEqual(where.find("麦田", sources=sources)["candidates"][0]["kind"],
                             where.KIND_ZONE)


class HandlerTests(unittest.TestCase):
    def test_handler_answers_with_candidates_and_never_moves(self):
        skills = DummySkills()
        planner = _planner(skills=skills)
        context = {"players": [{"name": "Steve", "x": 12.0, "y": 64.0, "z": 0.0}],
                   "player": {"x": 0.0, "y": 64.0, "z": 0.0}}

        executed = planner._dispatch_tool_call("where_is", {"target": "Steve"}, context)

        self.assertTrue(executed)
        self.assertIn("(12,64,0)", planner._action_note)
        self.assertIn("state.players", planner._action_note)
        self.assertEqual(skills.calls, [], "where_is 只答位置，一次都不许动")

    def test_handler_is_honest_when_it_does_not_know(self):
        skills = DummySkills()
        planner = _planner(skills=skills)
        context = {"players": [{"name": "Steve", "x": 1.0, "y": 64.0, "z": 0.0}]}

        planner._dispatch_tool_call("where_is", {"target": "下界要塞"}, context)

        self.assertIn("不知道", planner._action_note)
        self.assertNotIn("@(", planner._action_note)
        self.assertEqual(skills.calls, [])

    def test_handler_uses_injected_world_layer_stores(self):
        with tempfile.TemporaryDirectory() as tmp:
            places = WorldMemoryStore(stations_root=Path(tmp) / "s",
                                      routes_root=Path(tmp) / "r")
            places.absorb_state({"stations": [
                {"name": "铁矿挂机点", "x": 100.0, "y": 12.0, "z": -100.0}]})
            skills = DummySkills()
            planner = _planner(skills=skills, places=places)

            planner._dispatch_tool_call("where_is", {"target": "铁矿"}, {})

            self.assertIn("(100,12,-100)", planner._action_note)
            self.assertIn("memory.stations", planner._action_note)
            self.assertEqual(skills.calls, [])

    def test_notes_are_appended_to_the_reply_like_other_honest_notes(self):
        planner = _planner()
        planner._action_note = "（where_is「x」：…）"

        self.assertIn("where_is", planner._with_notes("好"))


if __name__ == "__main__":
    unittest.main()
