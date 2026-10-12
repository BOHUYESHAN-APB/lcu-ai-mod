"""三件新工具的验收：`find_entity` / `use_on` / `view_container`。

覆盖票据的验收清单：
  ① `find_entity`：按 kind（英文 / 中文 / 类别）找**附近实体**，候选与 `where_is` 同形状
     （`kind/name/x/y/z/distance/source` + `truncated` + 诚实 note）；数据只来自 state 快照，
     没有就说没有（并说明只覆盖约 16 格），**绝不编坐标**、**绝不移动**；
  ② `use_on`：对目标使用手持物品，底层复用已有原语；声明占 `use` 资源；失败带 code
     （`no_target`/`bad_target`/`out_of_range`/`wrong_item`）且一帧不发；
  ③ `view_container`：只读列内容（`{items:[{name,count,slot}],empty,truncated,note}`）+ 关界面，
     **什么都不拿**；声明占 `container`（独占）；尊重容器策略（`no_touch` 直接拒、
     `borrow_only` 只读可以）；没开着 ⇒ 带 code 如实说不知道；
  ④ 三件都登记在注册表、有 handler、每阶段可用（按组）、描述只走 L3（不进 L0/L1 静态前缀）。
"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent import container_view, entities, use_on  # noqa: E402
from agent.borrow import normalize_item  # noqa: E402
from agent.planner import PHASE_GROUPS, Planner, _actions_for_phase  # noqa: E402
from agent.resource_lease import RESOURCE_CONTAINER, RESOURCE_USE, resources_for_tool  # noqa: E402
from agent.territory import TerritoryStore  # noqa: E402
from agent.tool_registry import TOOL_BY_NAME, TOOL_SPECS  # noqa: E402

PLAYER = {"x": 0.0, "y": 64.0, "z": 0.0}


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


def _context(entities_rows=None, **extra) -> dict:
    context = {"player": dict(PLAYER), "entities": list(entities_rows or [])}
    context.update(extra)
    return context


def _row(name, *, kind_type="animal", x=3.0, y=64.0, z=0.0, distance=None, id_=7, **extra):
    row = {"id": id_, "name": name, "type": kind_type, "x": x, "y": y, "z": z}
    row["distance"] = distance if distance is not None else round(
        ((x - PLAYER["x"]) ** 2 + (y - PLAYER["y"]) ** 2 + (z - PLAYER["z"]) ** 2) ** 0.5, 1)
    row.update(extra)
    return row


def _fresh_planner(skills=None, **kwargs):
    """带一份**登记过**的领地/容器存储的 planner（容器策略判定用）。"""
    tmp = tempfile.mkdtemp()
    store = TerritoryStore(root=tmp)
    return _planner(skills=skills, territory=store, **kwargs), store


def _open_container(title="仓库箱", items=None, *, slots=63, inv_start=27, kind="chest"):
    return {"id": 3, "title": title, "kind": kind, "slots": slots,
            "player_inv_start": inv_start, "items": items if items is not None else []}


def _stack(slot, item, count):
    return {"slot": slot, "item": item, "count": count, "name": item}


# ── ① 注册表接线 ─────────────────────────────────────────────


class RegistryTests(unittest.TestCase):
    def test_three_new_tools_are_registered_with_their_handlers(self):
        expected = {"find_entity": "kind,limit", "use_on": "target,item,x,y,z",
                    "view_container": "target,limit"}
        for name, args in expected.items():
            spec = TOOL_BY_NAME.get(name)
            self.assertIsNotNone(spec, f"{name} 必须登记在注册表（工具的唯一事实来源）")
            self.assertEqual(spec.args, args, name)
            self.assertTrue(callable(getattr(Planner, spec.handler, None)), name)
            self.assertEqual(set(spec.schema()["function"]["parameters"]["properties"]),
                             set(args.split(",")), name)

    def test_tool_names_are_unique(self):
        """名字唯一是**不变量** ✓；**具体条数不测** ✗ —— "又长了几条"这种附带性断言，
        每次加工具都会假红一次（这条以前写死 58，复合体一落地就红了 ✓）。"""
        self.assertEqual(len(TOOL_SPECS), len({spec.name for spec in TOOL_SPECS}))

    def test_declared_resources(self):
        """`use_on` 占 use（右键）、`view_container` 占 container（独占）、`find_entity` 不占。"""
        self.assertEqual(resources_for_tool("use_on"), frozenset({RESOURCE_USE}))
        self.assertEqual(resources_for_tool("view_container"), frozenset({RESOURCE_CONTAINER}))
        self.assertEqual(resources_for_tool("find_entity"), frozenset())

    def test_view_container_declares_the_read_only_container_gate(self):
        perm = TOOL_BY_NAME["view_container"].permission

        self.assertTrue(perm.container)
        self.assertTrue(perm.read_only, "只读变体必须在注册表里声明（不藏在 handler 里）")

    def test_registry_matches_the_built_tool_schemas(self):
        from agent.planner import build_tool_schemas

        built = {item["function"]["name"] for item in build_tool_schemas()}
        self.assertTrue({"find_entity", "use_on", "view_container"} <= built)

    def test_available_in_phases_and_out_of_the_static_prefix(self):
        for name in ("find_entity", "use_on"):
            for phase in PHASE_GROUPS:
                names = {item for item, _, _ in _actions_for_phase(phase)}
                self.assertIn(name, names, f"{name} 在 {phase} 阶段该可用")
        for phase in ("idle", "travel", "gather", "craft"):
            names = {item for item, _, _ in _actions_for_phase(phase)}
            self.assertIn("view_container", names, phase)

    def test_descriptions_only_go_to_l3_and_teach_the_closed_loop(self):
        layers = _planner().build_prompt_layers(
            "Steve", "hi", {"task_state": {"kind": "idle", "status": "idle"}}, "AI")
        tools = [item["function"]["name"] for item in layers.tools]

        for name in ("find_entity", "use_on", "view_container"):
            self.assertIn(name, tools)
            self.assertNotIn(name, layers.system, "工具描述只走 L3，不进 L0/L1")

        entity_text = TOOL_BY_NAME["find_entity"].description
        for follow_up in ("move_to", "go_to_block", "follow"):
            self.assertIn(follow_up, entity_text)
        self.assertIn("不编坐标", entity_text)

        use_text = TOOL_BY_NAME["use_on"].description
        self.assertIn("use 资源", use_text)
        self.assertIn("wrong_item", use_text)

        view_text = TOOL_BY_NAME["view_container"].description
        self.assertIn("haul", view_text)
        self.assertIn("no_touch", view_text)
        self.assertIn("什么都不拿", view_text)


# ── ② find_entity ────────────────────────────────────────────


class FindEntityTests(unittest.TestCase):
    def test_exact_kind_hit_carries_coords_distance_and_source(self):
        rows = [_row("Cow", x=3.0, y=64.0, z=4.0)]

        result = entities.find("cow", player_pos=(0.0, 64.0, 0.0), entities=rows)

        self.assertTrue(result["found"])
        candidate = result["candidates"][0]
        self.assertEqual(candidate["kind"], "cow")
        self.assertEqual(candidate["name"], "Cow")
        self.assertEqual((candidate["x"], candidate["y"], candidate["z"]), (3.0, 64.0, 4.0))
        self.assertEqual(candidate["distance"], 5.0)
        self.assertEqual(candidate["source"], entities.SOURCE_ENTITIES)
        self.assertEqual(result["note"], entities.describe(result))

    def test_chinese_kind_hits_english_display_name_and_vice_versa(self):
        rows = [_row("Cow"), _row("Sheep", x=5.0, id_=8)]

        by_chinese = entities.find("牛", entities=rows)
        by_english = entities.find("绵羊", entities=rows)

        self.assertEqual([row["name"] for row in by_chinese["candidates"]], ["Cow"])
        self.assertEqual([row["name"] for row in by_english["candidates"]], ["Sheep"])

    def test_type_category_and_containment(self):
        rows = [_row("Zombie", kind_type="hostile", x=2.0),
                _row("Zombie Villager", kind_type="hostile", x=9.0, id_=9),
                _row("Cow", x=1.0, id_=10)]

        hostile = entities.find("hostile", entities=rows)
        zombie = entities.find("zombie", entities=rows)

        self.assertEqual({row["name"] for row in hostile["candidates"]},
                         {"Zombie", "Zombie Villager"})
        self.assertEqual({row["name"] for row in zombie["candidates"]},
                         {"Zombie", "Zombie Villager"})

    def test_item_entities_are_findable_by_category(self):
        rows = [_row("Diamond", kind_type="item", x=4.0, item_id="minecraft:diamond",
                     item_count=2)]

        result = entities.find("掉落物", entities=rows)

        self.assertEqual(result["candidates"][0]["kind"], "item")

    def test_blank_kind_lists_everything_nearby(self):
        rows = [_row("Cow"), _row("Zombie", kind_type="hostile", x=2.0, id_=9)]

        result = entities.find("", entities=rows)

        self.assertEqual(len(result["candidates"]), 2)
        self.assertIn("候选", entities.describe(result))

    def test_nothing_matching_is_honest_and_never_invents_coordinates(self):
        rows = [_row("Cow", x=12.0, y=70.0, z=9.0)]

        result = entities.find("僵尸", player_pos=(0.0, 64.0, 0.0), entities=rows)

        self.assertFalse(result["found"])
        self.assertEqual(result["candidates"], [])
        note = entities.describe(result)
        self.assertIn("当前快照里没有", note)
        self.assertIn("16 格", note)
        self.assertNotIn("@(", note, "空结果绝不能带坐标")
        for token in ("12.0", "(12,", "70.0"):
            self.assertNotIn(token, note)

    def test_players_without_geometry_are_skipped_and_pointed_at_where_is(self):
        rows = [{"name": "Steve", "type": "player", "distance": 4.0, "uuid": "u-1"}]

        result = entities.find("player", entities=rows)

        self.assertEqual(result["candidates"], [])
        self.assertEqual(result["skipped"], 1)
        note = entities.describe(result)
        self.assertIn("没给坐标", note)
        self.assertIn("where_is kind=player", note)
        self.assertNotIn("@(", note)

    def test_truncation_keeps_the_nearest_and_flags_it(self):
        rows = [_row(f"Cow_{i}", x=float(i + 1), id_=100 + i) for i in range(12)]

        result = entities.find("cow", limit=3, player_pos=(0.0, 64.0, 0.0), entities=rows)

        self.assertTrue(result["truncated"])
        self.assertEqual(len(result["candidates"]), 3)
        self.assertEqual(result["candidates"][0]["name"], "Cow_0")
        self.assertIn("还有更多", entities.describe(result))

        # 没给玩家坐标又没报距离 → 排不出远近，就别谎称"最近的"。
        blind_rows = [{key: value for key, value in row.items() if key != "distance"}
                      for row in rows]
        blind = entities.find("cow", limit=3, entities=blind_rows)
        self.assertTrue(blind["truncated"])
        self.assertIn("只报了前", blind["note"])
        self.assertNotIn("最近的", blind["note"])

    def test_unknown_kind_is_refused_honestly(self):
        result = entities.find("钻石矿", entities=[_row("Cow")])

        self.assertEqual(result["candidates"], [])
        self.assertIn("find_block", entities.describe(result))

    def test_limit_is_clamped_and_never_raises_on_junk(self):
        rows = [_row(f"Cow_{i}", x=float(i), id_=i) for i in range(40)]

        self.assertEqual(len(entities.find("cow", limit=999, entities=rows)["candidates"]),
                         entities.MAX_LIMIT)
        self.assertEqual(len(entities.find("cow", limit=0, entities=rows)["candidates"]), 1)
        for kind in ("", None, 123, "  "):
            for source in (None, [], [None, {}, {"name": "Cow"}]):
                result = entities.find(kind, entities=source or [])
                self.assertIsInstance(entities.describe(result), str)

    def test_data_comes_from_the_state_snapshot_only(self):
        context = {"entities": [_row("Cow")]}

        self.assertEqual(entities.gather(context), context["entities"])
        self.assertEqual(entities.gather(None), [])
        self.assertEqual(entities.gather({"entities": "x"}), [])


# ── ③ use_on ─────────────────────────────────────────────────


class UseOnTests(unittest.TestCase):
    def _hand(self, name, display="", count=1):
        return {"player": {"equipment": {"main_hand": {"name": name, "count": count,
                                                       "display": display}}},
                "entities": [_row("Cow")]}

    def test_entity_target_resolves_to_the_nearest_with_its_id(self):
        context = self._hand("minecraft:bucket", "Bucket")

        result = use_on.plan({"target": "牛"}, context=context)

        self.assertTrue(result["ok"])
        self.assertEqual(result["mode"], use_on.MODE_ENTITY)
        self.assertEqual(result["target"]["name"], "Cow")
        self.assertEqual(result["target"]["id"], 7)
        self.assertEqual(result["held"]["name"], "bucket")

    def test_position_target_is_allowed(self):
        result = use_on.plan({"x": 2.0, "y": 64.0, "z": 2.0}, context={"player": dict(PLAYER)})

        self.assertTrue(result["ok"])
        self.assertEqual(result["mode"], use_on.MODE_POSITION)
        self.assertIn("(", result["target"]["name"])

    def test_no_target_is_reported_with_a_code(self):
        result = use_on.plan({"target": "cow"}, context={"player": dict(PLAYER), "entities": []})

        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], use_on.NO_TARGET)
        note = use_on.describe(result)
        self.assertIn("code=no_target", note)
        # 指到**唯一的坐标出口**：分级摘要里被动生物没有坐标，find_entity 也拿不到，
        # 只有按需扫描能取回来；而且要说明"摘要里没有 ≠ 附近没有"。
        self.assertIn("scan_entities", note)
        self.assertIn("不等于", note)

    def test_bad_target_without_entity_or_coordinates(self):
        result = use_on.plan({}, context={"player": dict(PLAYER)})

        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], use_on.BAD_TARGET)
        self.assertIn("code=bad_target", use_on.describe(result))

    def test_out_of_range_asks_the_model_to_walk_over(self):
        rows = [_row("Cow", x=9.0, y=64.0, z=0.0, distance=9.0, id_=11)]

        result = use_on.plan({"target": "cow"},
                             context={"player": dict(PLAYER), "entities": rows})

        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], use_on.OUT_OF_RANGE)
        note = use_on.describe(result)
        self.assertIn("code=out_of_range", note)
        self.assertIn("go_to_block", note)

    def test_wrong_item_when_the_declared_item_is_not_in_hand(self):
        context = self._hand("minecraft:stone", "Stone")

        result = use_on.plan({"target": "cow", "item": "bucket"}, context=context)

        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], use_on.WRONG_ITEM)
        note = use_on.describe(result)
        self.assertIn("code=wrong_item", note)
        self.assertIn("select_hotbar", note)
        self.assertIn("bucket", note)

    def test_wrong_item_when_the_declared_item_is_not_in_the_inventory(self):
        context = {"player": {"equipment": {}}, "entities": [_row("Cow")],
                   "inventory": [{"name": "minecraft:stone", "count": 3}]}

        result = use_on.plan({"target": "cow", "item": "shears"}, context=context)

        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], use_on.WRONG_ITEM)
        self.assertIn("没有", use_on.describe(result))

    def test_declared_item_that_matches_the_hand_passes(self):
        context = self._hand("minecraft:shears", "Shears")

        result = use_on.plan({"target": "cow", "item": "shears"}, context=context)

        self.assertTrue(result["ok"])
        self.assertIn("Shears", use_on.describe(result))

    def test_unknown_hand_is_said_out_loud_instead_of_guessed(self):
        result = use_on.plan({"target": "cow"},
                             context={"player": {}, "entities": [_row("Cow")]})

        self.assertTrue(result["ok"])
        self.assertEqual(result["held"]["name"], "")
        self.assertIn("没报手持物品", use_on.describe(result))

    def test_held_item_reads_the_state_equipment(self):
        self.assertEqual(use_on.held_item(self._hand("minecraft:shears", "Shears"))["display"],
                         "Shears")
        self.assertEqual(use_on.held_item(None)["name"], "")

    def test_malformed_input_never_raises(self):
        for payload in (None, {}, {"target": 5}, {"x": "a"}, {"target": "cow", "item": {}}):
            result = use_on.plan(payload, context={"player": dict(PLAYER)})
            self.assertIsInstance(use_on.describe(result), str)


# ── ④ view_container ────────────────────────────────────────


class ViewContainerTests(unittest.TestCase):
    def test_lists_only_the_container_side(self):
        snapshot = _open_container(items=[_stack(0, "minecraft:iron_ingot", 64),
                                          _stack(3, "minecraft:coal", 5),
                                          _stack(30, "minecraft:dirt", 4)])

        result = container_view.view(snapshot)

        self.assertTrue(result["known"])
        self.assertFalse(result["empty"])
        self.assertEqual(result["total"], 2, "背包侧（槽 ≥ player_inv_start）不算容器内容")
        self.assertEqual([row["name"] for row in result["items"]], ["iron_ingot", "coal"])
        self.assertEqual(result["items"][0], {"name": "iron_ingot", "count": 64, "slot": 0})
        self.assertIn("槽 0", result["note"])

    def test_empty_container_is_reported_as_empty(self):
        result = container_view.view(_open_container(items=[]))

        self.assertTrue(result["known"])
        self.assertTrue(result["empty"])
        self.assertEqual(result["items"], [])
        self.assertIn("空的", container_view.describe(result))

    def test_truncation_is_flagged(self):
        items = [_stack(slot, f"minecraft:item_{slot}", 1) for slot in range(10)]

        result = container_view.view(_open_container(items=items), limit=4)

        self.assertTrue(result["truncated"])
        self.assertEqual(len(result["items"]), 4)
        self.assertEqual(result["total"], 10)
        self.assertIn("只列了前 4 格", container_view.describe(result))

    def test_nothing_open_is_never_reported_as_empty(self):
        for snapshot in ({}, None, {"title": ""}):
            result = container_view.view(snapshot)

            self.assertFalse(result["known"])
            self.assertEqual(result["code"], container_view.CODE_NO_CONTAINER)
            self.assertFalse(result["empty"], "不知道 ≠ 空")
            self.assertEqual(result["items"], [])
            note = container_view.describe(result)
            self.assertIn(f"code={container_view.CODE_NO_CONTAINER}", note)
            self.assertIn("use_on", note)

    def test_snapshot_without_a_layout_is_refused_honestly(self):
        result = container_view.view(_open_container(items=[_stack(0, "minecraft:coal", 5)],
                                                     inv_start=0))

        self.assertFalse(result["known"])
        self.assertEqual(result["code"], container_view.CODE_NO_LAYOUT)
        self.assertIn(container_view.CODE_NO_LAYOUT, container_view.describe(result))

    def test_malformed_rows_are_skipped_not_invented(self):
        snapshot = _open_container(items=[_stack(0, "minecraft:coal", 5), {"item": "x"},
                                          "junk", {"slot": "?"}])

        result = container_view.view(snapshot)

        self.assertEqual(result["items"], [{"name": "coal", "count": 5, "slot": 0}])

    def test_target_matching_and_its_note(self):
        snapshot = _open_container(title="仓库箱")

        self.assertTrue(container_view.matches(snapshot, "仓库"))
        self.assertTrue(container_view.matches(snapshot, ""))
        self.assertFalse(container_view.matches(snapshot, "别的箱"))
        note = container_view.other_container_note("别的箱", snapshot)
        self.assertIn(container_view.CODE_OTHER_CONTAINER, note)
        self.assertIn("仓库箱", note)

    def test_denied_notes_carry_their_codes(self):
        self.assertIn(container_view.CODE_NO_TOUCH,
                      container_view.denied_note(container_view.CODE_NO_TOUCH,
                                                 container={"name": "禁区箱"}))
        self.assertIn(container_view.CODE_UNAUTHORIZED,
                      container_view.denied_note(container_view.CODE_UNAUTHORIZED,
                                                 tier="guest"))
        self.assertIn(container_view.CODE_BUSY,
                      container_view.denied_note(container_view.CODE_BUSY))


# ── ⑤ handler 级：真派发、真发帧（或真不发）──────────────────


class HandlerTests(unittest.TestCase):
    def test_find_entity_answers_with_candidates_and_never_moves(self):
        skills = DummySkills()
        planner = _planner(skills=skills)

        executed = planner._dispatch_tool_call("find_entity", {"kind": "cow"},
                                               _context([_row("Cow", x=3.0, z=4.0)]))

        self.assertTrue(executed)
        self.assertIn("(3,64,4)", planner._action_note)
        self.assertIn(entities.SOURCE_ENTITIES, planner._action_note)
        self.assertEqual(skills.calls, [], "find_entity 只答位置，一次都不许动")

    def test_find_entity_is_honest_when_the_snapshot_has_nothing(self):
        skills = DummySkills()
        planner = _planner(skills=skills)

        planner._dispatch_tool_call("find_entity", {"kind": "僵尸"}, _context([_row("Cow")]))

        self.assertIn("当前快照里没有", planner._action_note)
        self.assertNotIn("@(", planner._action_note)
        self.assertEqual(skills.calls, [])

    def test_use_on_entity_goes_through_the_existing_primitive(self):
        skills = DummySkills()
        planner = _planner(skills=skills)
        context = _context([_row("Cow", id_=12)])
        context["player"] = {"x": 0.0, "y": 64.0, "z": 0.0,
                             "equipment": {"main_hand": {"name": "minecraft:bucket",
                                                         "count": 1, "display": "Bucket"}}}

        executed = planner._dispatch_tool_call("use_on", {"target": "cow"}, context)

        self.assertTrue(executed)
        self.assertEqual(skills.calls, [("use_on_entity", (12,), {})])
        self.assertIn("Bucket", planner._action_note)

    def test_use_on_position_aims_first_then_right_clicks(self):
        skills = DummySkills()
        planner = _planner(skills=skills)

        executed = planner._dispatch_tool_call("use_on", {"x": 2.0, "y": 64.0, "z": 2.0},
                                               _context())

        self.assertTrue(executed)
        self.assertEqual([name for name, _, _ in skills.calls], ["look_at", "use_on"])

    def test_use_on_refusal_sends_nothing_and_carries_a_code(self):
        skills = DummySkills()
        planner = _planner(skills=skills)
        context = _context([_row("Cow", distance=9.0, x=9.0)])

        executed = planner._dispatch_tool_call("use_on", {"target": "cow"}, context)

        self.assertFalse(executed)
        self.assertEqual(skills.calls, [], "判定不过就一帧都不发")
        self.assertIn("code=out_of_range", planner._action_note)

    def test_view_container_lists_and_closes_without_taking_anything(self):
        skills = DummySkills()
        planner, store = _fresh_planner(skills=skills)
        store.remember_container("仓库箱", x=1.0, y=64.0, z=1.0, policy="borrow_only")
        context = _context(open_container=_open_container(
            items=[_stack(0, "minecraft:iron_ingot", 64)]))

        executed = planner._dispatch_tool_call("view_container", {}, context)

        self.assertTrue(executed)
        self.assertEqual(skills.calls, [("container_close", (), {})],
                         "只读：列完就关，绝不取用")
        self.assertIn("iron_ingot", planner._action_note)
        self.assertIn("什么都没拿", planner._action_note)

    def test_view_container_uses_the_state_snapshot_not_a_new_frame(self):
        skills = DummySkills()
        planner, store = _fresh_planner(skills=skills)
        store.remember_container("仓库箱", x=1.0, y=64.0, z=1.0, policy="free")
        context = _context(open_container=_open_container(items=[]))

        executed = planner._dispatch_tool_call("view_container", {"target": "仓库箱"}, context)

        self.assertTrue(executed)
        self.assertIn("空的", planner._action_note)
        self.assertEqual([name for name, _, _ in skills.calls], ["container_close"])

    def test_view_container_without_an_open_container_is_honest(self):
        skills = DummySkills()
        planner = _planner(skills=skills)

        executed = planner._dispatch_tool_call("view_container", {}, _context())

        self.assertFalse(executed)
        self.assertEqual(skills.calls, [])
        self.assertIn(f"code={container_view.CODE_NO_CONTAINER}", planner._action_note)
        self.assertIn("use_on", planner._action_note)

    def test_view_container_on_a_no_touch_container_is_refused(self):
        skills = DummySkills()
        planner, store = _fresh_planner(skills=skills)
        store.remember_container("禁区箱", x=1.0, y=64.0, z=1.0, policy="no_touch")
        context = _context(open_container=_open_container(title="禁区箱"))

        executed = planner._dispatch_tool_call("view_container", {}, context)

        self.assertFalse(executed)
        self.assertEqual(skills.calls, [], "no_touch 连看都不看")
        self.assertIn(f"code={container_view.CODE_NO_TOUCH}", planner._craft_note)
        self.assertIn("no_touch", planner._with_notes("好"))

    def test_view_container_on_an_unregistered_container_needs_authorization(self):
        from agent.permissions import PermissionsStore

        with tempfile.TemporaryDirectory() as tmp:
            permissions = PermissionsStore(Path(tmp) / "p.json")
            skills = DummySkills()
            planner = _planner(skills=skills, permissions=permissions)
            context = _context(open_container=_open_container(title="陌生箱"))

            executed = planner._dispatch_tool_call("view_container", {}, context)

            self.assertFalse(executed)
            self.assertEqual(skills.calls, [])
            self.assertIn(f"code={container_view.CODE_UNAUTHORIZED}", planner._craft_note)

    def test_view_container_on_an_unregistered_container_is_authorized_when_allowed(self):
        """有翻箱授权时，没登记的箱子**只读**也给看（这一层不额外加严）。"""
        from agent.permissions import PermissionsStore, TIER_MASTER

        with tempfile.TemporaryDirectory() as tmp:
            permissions = PermissionsStore(Path(tmp) / "p.json")
            permissions.set({"containers": {TIER_MASTER: True}})
            skills = DummySkills()
            planner = _planner(skills=skills, permissions=permissions)
            context = _context(open_container=_open_container(
                title="陌生箱", items=[_stack(0, "minecraft:coal", 3)]))
            context["actor_tier"] = TIER_MASTER

            executed = planner._dispatch_tool_call("view_container", {}, context)

            self.assertTrue(executed)
            self.assertEqual([name for name, _, _ in skills.calls], ["container_close"])
            self.assertIn("coal", planner._action_note)

    def test_view_container_borrow_only_is_readable_without_container_rights(self):
        """`borrow_only` ⇒ 只读可以（不必有翻箱授权），但仍不拿东西。"""
        skills = DummySkills()
        planner, store = _fresh_planner(skills=skills)   # 没有权限表 → 取用路径一律拒
        store.remember_container("传递箱", x=1.0, y=64.0, z=1.0, policy="borrow_only")
        context = _context(open_container=_open_container(
            title="传递箱", items=[_stack(1, "minecraft:wheat", 9)]))

        executed = planner._dispatch_tool_call("view_container", {}, context)

        self.assertTrue(executed)
        self.assertEqual([name for name, _, _ in skills.calls], ["container_close"])
        self.assertIn("wheat", planner._action_note)

    def test_view_container_while_hauling_is_busy(self):
        skills = DummySkills()
        planner, store = _fresh_planner(skills=skills)
        store.remember_container("仓库箱", x=1.0, y=64.0, z=1.0, policy="free")
        planner._haul_job = object()
        context = _context(open_container=_open_container())

        executed = planner._dispatch_tool_call("view_container", {}, context)

        self.assertFalse(executed)
        self.assertEqual(skills.calls, [])
        self.assertIn(f"code={container_view.CODE_BUSY}", planner._action_note)

    def test_view_container_on_some_other_open_container_says_so(self):
        skills = DummySkills()
        planner, store = _fresh_planner(skills=skills)
        store.remember_container("仓库箱", x=1.0, y=64.0, z=1.0, policy="free")
        context = _context(open_container=_open_container(title="仓库箱"))

        executed = planner._dispatch_tool_call("view_container", {"target": "别的箱"}, context)

        self.assertFalse(executed)
        self.assertEqual(skills.calls, [])
        self.assertIn(f"code={container_view.CODE_OTHER_CONTAINER}", planner._action_note)

    def test_notes_reach_the_reply_like_the_other_honest_notes(self):
        planner = _planner()
        planner._action_note = "（find_entity「cow」：1 个候选 …）"

        self.assertIn("find_entity", planner._with_notes("好"))

    def test_use_on_goes_through_the_resource_lease(self):
        """`use_on` 声明占 `use`：别处占着时**排队**，一帧都不发（走状态治理，不硬闯）。"""
        skills = DummySkills()
        planner = _planner(skills=skills)
        planner.resource_lease.request("task:look", {"look"}, priority=50)
        context = _context([_row("Cow", id_=12)])
        context["player"] = {"x": 0.0, "y": 64.0, "z": 0.0,
                             "equipment": {"main_hand": {"name": "minecraft:bucket",
                                                         "count": 1, "display": "Bucket"}}}

        executed = planner._dispatch_tool_call("use_on", {"target": "cow"}, context)

        self.assertFalse(executed)
        self.assertEqual(skills.calls, [])
        self.assertIn("资源", planner._action_note)

    def test_use_on_handler_never_fabricates_a_held_item(self):
        """读不到主手时**不许**假装核对过：如实说明"没法先核对"，不自封 wrong_item。"""
        skills = DummySkills()
        planner = _planner(skills=skills)
        context = _context([_row("Cow")])

        executed = planner._dispatch_tool_call("use_on", {"target": "cow", "item": "bucket"},
                                               context)

        self.assertTrue(executed)
        self.assertIn("没报手持物品", planner._action_note)
        self.assertNotIn("code=wrong_item", planner._action_note)
        self.assertEqual([name for name, _, _ in skills.calls], ["use_on_entity"])
        self.assertEqual(normalize_item("minecraft:bucket"), "bucket")


if __name__ == "__main__":
    unittest.main()
