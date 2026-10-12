"""分级摘要适配的验收测试（`agent/perception.py` + 各消费点 + 按需扫描出口）。

对应票据验收清单：

① 新摘要形状 ⇒ `partial=True`，行只来自带坐标的档位（威胁 / 玩家）+ 最近一条 / 最近掉落物，
   **不含编出来的坐标**；
② 旧列表形状 ⇒ `partial=False`，行数与列表一致（向后兼容）；
③ 摘要里"被动 9 个"**不会**变成 9 行带坐标的假数据；
④ `entities.find(...)` 在摘要形状下**如实标注**部分视图；
⑤ 造床安全判定在部分视图下**保守**（不因看不到怪就说安全）;
⑥ 方块：摘要下只有 `nearest` 一条（有坐标就照抄、没有就不编），`by_block` 只给计数；
⑦ L2 渲染在两种形状下都不报错且文本里有「按需」；
⑧ `scan_entities` 的 ToolSpec 参数名对（`radius,kind,limit`）且 handler 挂在 planner 上；
⑨ 回执 `truncated=true` ⇒ 必须出现「未列全」的说明；
⑩ 回执 `ok:false` 的 `code` 翻成人话，且**不**伪装成成功；
⑪ 部分视图的说明文字里出现「按需」。
"""

import unittest

from agent import perception
from agent.composites import CHECK_BED_CRAFTABLE, Check, evaluate
from agent.entities import find as find_entities
from agent.farm import feed_note, nearest as farm_nearest, note as farm_note
from agent.planner import Planner, build_tool_schemas
from agent.tool_registry import TOOL_BY_NAME
from agent.territory import bed_attempt

# 场景固定值：摘要里的坐标都是**模组报的**，测试只比对"有没有被篡改/编造"。
PLAYER = {"name": "BoHuYeShan", "x": 850, "y": 63, "z": -2190, "distance": 4.0, "health": 20.0}
ZOMBIE = {"name": "僵尸", "type": "hostile", "tier": "HOSTILE",
          "x": 851, "y": 63, "z": -2189, "distance": 6.2}
SKELETON = {"name": "骷髅", "type": "hostile", "tier": "HOSTILE",
            "x": 853, "y": 63, "z": -2190, "distance": 8.1}
ITEM = {"item_id": "minecraft:oak_log", "item_count": 1, "distance": 3.1,
        "owner": "", "age": 240, "despawn_in": 5760}
NEAREST = {"distance": 4.0, "name": "BoHuYeShan", "type": "player", "health": 20.0}


def entity_summary(**overrides) -> dict:
    summary = {
        "count": 13,
        "by_kind": {"dangerous": 0, "hostile": 2, "neutral": 1, "passive": 9,
                    "player": 1, "item": 0, "unknown": 0},
        "nearest": dict(NEAREST),
        "threats": [dict(ZOMBIE), dict(SKELETON)],
        "players": [dict(PLAYER)],
        "items": {"count": 4, "nearest": dict(ITEM)},
    }
    summary.update(overrides)
    return summary


def block_summary(*, nearest=None) -> dict:
    return {"count": 37,
            "by_block": {"minecraft:oak_log": 3, "minecraft:stone": 12},
            "nearest": nearest if nearest is not None else {"block_id": "minecraft:stone",
                                                            "distance": 1.4}}


def legacy_state() -> dict:
    return {
        "entities": [{"type": "animal", "name": "Cow", "id": 7,
                      "x": 3.0, "y": 64.0, "z": 2.0, "distance": 3.6},
                     {"type": "mob", "name": "Zombie", "id": 9,
                      "x": 1.0, "y": 64.0, "z": 1.0, "distance": 1.4}],
        "nearby_blocks": [{"block_id": "minecraft:stone", "x": 1, "y": 64, "z": 0,
                           "distance": 1.0},
                          {"block_id": "minecraft:farmland", "x": 2, "y": 63, "z": 0,
                           "distance": 2.0}],
    }


def summary_state() -> dict:
    return {"entities": entity_summary(), "nearby_blocks": block_summary()}


class DummyMemory:
    def build_context(self):
        return {}

    def get_recent_context(self, _count=5):
        return []


class RecordingSkills:
    """记下发出去的每一帧（`scan_entities` 也走 `_send_cmd` 那条路）。"""

    def __init__(self, req_id="req-1"):
        self.calls = []
        self.req_id = req_id

    def __getattr__(self, item):
        def recorder(*args, **kwargs):
            self.calls.append((item, args, kwargs))
            return {"success": True, "req_id": self.req_id}
        return recorder

    def names(self):
        return [call[0] for call in self.calls]


def make_planner(skills=None) -> Planner:
    return Planner(llm_service=None, memory=DummyMemory(),
                   skills=skills if skills is not None else RecordingSkills())


# ── ①③⑥ 纯函数：两种形状 → 统一行 + partial 如实标注 ─────────────


class EntityRowsTests(unittest.TestCase):
    def test_summary_shape_is_marked_partial_and_rows_only_come_from_coordinate_buckets(self):
        view = perception.entities_rows(summary_state())

        self.assertTrue(view["partial"])
        self.assertEqual(view["shape"], perception.SHAPE_SUMMARY)
        names = [row["name"] for row in view["rows"]]
        # 只可能是威胁 / 玩家 / 最近掉落物 / 最近一条（且最近一条与玩家重复时去重）。
        self.assertIn("僵尸", names)
        self.assertIn("BoHuYeShan", names)
        self.assertIn("minecraft:oak_log", names)
        self.assertEqual(set(names), {"僵尸", "骷髅", "BoHuYeShan", "minecraft:oak_log"})

    def test_summary_coordinates_are_verbatim_and_never_invented(self):
        view = perception.entities_rows(summary_state())
        rows = {row["name"]: row for row in view["rows"]}

        self.assertEqual(perception.point_of(rows["僵尸"]), (851.0, 63.0, -2189.0))
        self.assertEqual(perception.point_of(rows["BoHuYeShan"]), (850.0, 63.0, -2190.0))
        # 最近一条 / 最近掉落物**本来就没有坐标**：不许按 kind 编一个出来。
        self.assertIsNone(perception.point_of(rows["minecraft:oak_log"]))
        self.assertIsNone(perception.point_of(dict(NEAREST)))

    def test_passive_counts_do_not_become_fake_coordinate_rows(self):
        """摘要里"被动 9 个"只有计数 —— 不许变成 9 行带坐标的假数据。"""
        view = perception.entities_rows(summary_state())

        self.assertEqual(view["by_kind"]["passive"], 9)
        self.assertFalse([row for row in view["rows"] if row.get("kind") == "passive"])
        self.assertEqual(sum(view["by_kind"].values()), view["count"],
                         "计数自洽：各档之和 == count（模组口径）")

    def test_legacy_list_shape_stays_backward_compatible(self):
        state = legacy_state()
        view = perception.entities_rows(state)

        self.assertFalse(view["partial"])
        self.assertEqual(view["shape"], perception.SHAPE_LIST)
        self.assertEqual(len(view["rows"]), len(state["entities"]))
        self.assertEqual(view["rows"], state["entities"], "旧形状原样透传（不添字段、不改字节）")

    def test_unknown_garbage_shape_is_treated_as_partial_not_as_empty(self):
        for value in ({"whatever": 1}, "x", 3):
            view = perception.entities_rows({"entities": value})
            self.assertTrue(view["partial"], value)
            self.assertNotEqual(view["shape"], perception.SHAPE_SUMMARY)

    def test_block_summary_gives_counts_plus_one_nearest_row_only(self):
        view = perception.blocks_rows(summary_state())

        self.assertTrue(view["partial"])
        self.assertEqual(len(view["rows"]), 1)
        self.assertEqual(view["rows"][0]["block_id"], "minecraft:stone")
        # by_block 只给计数，**不许**变成带坐标的行。
        self.assertEqual(view["by_block"]["minecraft:stone"], 12)
        self.assertFalse([row for row in view["rows"] if isinstance(row.get("x"), (int, float))])

    def test_block_nearest_coordinates_are_copied_when_the_mod_reports_them(self):
        state = {"nearby_blocks": block_summary(nearest={"block_id": "minecraft:stone",
                                                        "x": 851, "y": 63, "z": -2189,
                                                        "distance": 1.4})}
        row = perception.blocks_rows(state)["rows"][0]

        self.assertEqual(perception.point_of(row), (851.0, 63.0, -2189.0))

    def test_block_and_entity_summary_notes_say_scan_on_demand(self):
        state = summary_state()

        self.assertIn("按需", perception.summary_note(state))
        self.assertIn("按需", perception.block_note(state))
        self.assertIn("按需", perception.block_missing_note())
        self.assertIn("按需", perception.partial_note("实体"))


# ── ④ find_entity：部分视图如实标注 ───────────────────────────────


class FindEntityPartialTests(unittest.TestCase):
    def test_find_in_summary_shape_is_marked_partial_and_says_so(self):
        view = perception.entities_rows(summary_state())

        result = find_entities("牛", entities=view["rows"], partial=view["partial"])

        self.assertFalse(result["found"])
        self.assertTrue(result["partial"])
        self.assertIn("摘要", result["note"])
        self.assertIn("按需", result["note"])
        self.assertNotIn("@(", result["note"], "没找到就不许带坐标")

    def test_find_reads_partial_from_the_row_marker_when_not_told(self):
        view = perception.entities_rows(summary_state())

        result = find_entities("僵尸", entities=view["rows"])

        self.assertTrue(result["found"])
        self.assertTrue(result["partial"], "行上有标记 ⇒ 不必额外传参也知道是部分视图")
        self.assertIn("扫", result["note"])

    def test_legacy_rows_are_not_marked_partial(self):
        state = legacy_state()
        result = find_entities("zombie", entities=state["entities"])

        self.assertFalse(result["partial"])
        self.assertEqual(result["candidates"][0]["name"], "Zombie")

    def test_coordinate_less_rows_are_skipped_not_invented(self):
        view = perception.entities_rows(summary_state())

        result = find_entities("掉落物", entities=view["rows"])

        self.assertEqual(result["candidates"], [])
        self.assertTrue(result["skipped"], "只有距离、没坐标 ⇒ 只计数，不进候选")
        self.assertNotIn("@(", result["note"])


# ── ⑤ 造床安全判定：部分视图下保守 ───────────────────────────────


class BedSafetyTests(unittest.TestCase):
    MATERIALS = [{"name": "white_wool", "count": 3}, {"name": "oak_planks", "count": 3}]

    def test_partial_view_is_treated_as_maybe_unsafe(self):
        view = perception.entities_rows(summary_state())
        # 模组这一帧只报了"靠近的威胁"（本例里全在 16 格外时 threats 会是空的）。
        calm = perception.entities_rows({"entities": entity_summary(threats=[], players=[])})

        safe_looking = bed_attempt(self.MATERIALS, calm["rows"])
        decision = perception.conservative_bed_decision(safe_looking, calm)

        self.assertEqual(safe_looking["action"], "craft", "territory 的原判定还是 craft")
        self.assertEqual(decision["action"], "unsafe", "看不到 ≠ 安全 ⇒ 保守抬成 unsafe")
        self.assertTrue(decision["partial"])
        note = perception.bed_caution_note(hostiles=decision["hostiles"])
        self.assertIn("按需", note)
        self.assertIn("不能确认", note)

    def test_full_view_keeps_the_old_verdict(self):
        calm = {"entities": [{"type": "animal", "name": "Cow", "id": 7,
                              "x": 3.0, "y": 64.0, "z": 2.0, "distance": 3.6}]}
        legacy = perception.entities_rows(calm)

        decision = perception.conservative_bed_decision(
            bed_attempt(self.MATERIALS, legacy["rows"]), legacy)

        self.assertFalse(decision.get("partial"))
        self.assertEqual(decision["action"], "craft")

    def test_full_view_with_a_hostile_still_refuses(self):
        legacy = perception.entities_rows(legacy_state())

        decision = perception.conservative_bed_decision(
            bed_attempt(self.MATERIALS, legacy["rows"]), legacy)

        self.assertEqual(decision["action"], "unsafe")
        self.assertFalse(decision.get("partial"), "全量清单里的威胁是**看到**的，不是保守假设")

    def test_hostiles_still_win_and_are_named(self):
        view = perception.entities_rows(summary_state())

        decision = perception.conservative_bed_decision(
            bed_attempt(self.MATERIALS, view["rows"]), view)

        self.assertEqual(decision["action"], "unsafe")
        self.assertIn("僵尸", perception.bed_caution_note(hostiles=decision["hostiles"]))

    def test_composite_bed_check_is_conservative_in_a_partial_view(self):
        ctx = {"inventory": self.MATERIALS,
               "entities": entity_summary(threats=[], players=[])}

        ok, code, note, how = evaluate(Check(CHECK_BED_CRAFTABLE), ctx)

        self.assertFalse(ok)
        self.assertEqual(code, "bed_unsafe")
        self.assertIn("不能确认", note)
        self.assertIn("按需", note)

    def test_farm_notes_say_scan_on_demand_in_a_partial_view(self):
        self.assertIn("按需", farm_note("harvest", "yard", None, reason="no_crop",
                                       partial=True))
        self.assertIn("按需", feed_note({"feedable": [], "blocked": [],
                                        "unauthorized": []}, partial=True))
        self.assertNotIn("按需", farm_note("harvest", "yard", None, reason="no_crop"))

    def test_farm_nearest_returns_nothing_when_summary_has_no_coordinates(self):
        view = perception.blocks_rows(summary_state())

        self.assertIsNone(farm_nearest(view["rows"],
                                      lambda row: row.get("block_id") == "minecraft:stone"))
        self.assertIsNotNone(farm_nearest(legacy_state()["nearby_blocks"],
                                         lambda row: row.get("block_id") == "minecraft:stone"))


# ── ⑦ L2 渲染：两种形状都不报错，且说清「按需」 ────────────────────


class L2RenderTests(unittest.TestCase):
    def test_summary_shape_renders_counts_threats_and_the_scan_caveat(self):
        planner = make_planner()

        text = planner.build_prompt_layers("Steve", "hi", summary_state(), "AI").user

        self.assertIn("按需", text)
        self.assertIn("僵尸", text)
        self.assertIn("BoHuYeShan", text)

    def test_legacy_shape_renders_as_before_and_still_mentions_on_demand(self):
        planner = make_planner()

        text = planner.build_prompt_layers("Steve", "hi", legacy_state(), "AI").user

        self.assertIn("按需", text)
        self.assertIn("Cow", text)

    def test_empty_and_garbage_contexts_do_not_raise(self):
        planner = make_planner()

        for context in ({}, {"entities": {}}, {"nearby_blocks": "x"}, {"entities": None}):
            text = planner.build_prompt_layers("Steve", "hi", context, "AI").user
            self.assertIsInstance(text, str)


# ── ⑧⑨⑩⑪ scan_entities：注册表 / 派发 / 回执的如实呈现 ─────────────


class ScanEntitiesToolTests(unittest.TestCase):
    def test_spec_is_registered_with_the_frozen_parameter_names(self):
        spec = TOOL_BY_NAME["scan_entities"]

        self.assertEqual(spec.args, "radius,kind,limit")
        self.assertEqual(spec.handler, "_tool_scan_entities")
        self.assertIn("locate", spec.groups)
        self.assertTrue(callable(getattr(Planner, spec.handler)))
        names = [schema["function"]["name"] for schema in build_tool_schemas("idle")]
        self.assertIn("scan_entities", names)

    def test_dispatch_sends_the_frozen_arguments(self):
        skills = RecordingSkills()
        planner = make_planner(skills)

        self.assertTrue(planner._dispatch_tool_call(
            "scan_entities", {"radius": 16, "kind": "passive", "limit": 24}, {}))

        self.assertEqual(skills.calls, [("scan_entities",
                                         (), {"radius": 16, "kind": "passive", "limit": 24})])

    def test_defaults_and_clamp_match_the_mod(self):
        skills = RecordingSkills()
        planner = make_planner(skills)

        planner._dispatch_tool_call("scan_entities", {"limit": 999}, {})

        self.assertEqual(skills.calls[0][2], {"radius": 16, "kind": "all", "limit": 48})

    def test_illegal_parameters_are_refused_honestly_and_nothing_is_sent(self):
        for payload, needle in (({"radius": 64}, "上限"),
                                ({"radius": 0}, "整数"),
                                ({"kind": "cow"}, "kind"),
                                ({"limit": 0}, "整数")):
            skills = RecordingSkills()
            planner = make_planner(skills)

            self.assertFalse(planner._dispatch_tool_call("scan_entities", payload, {}))
            self.assertEqual(skills.calls, [], payload)
            self.assertIn("没发", planner._action_note)
            self.assertIn(needle, planner._action_note)

    def test_truncated_receipt_never_claims_to_have_everything(self):
        skills = RecordingSkills(req_id="req-42")
        planner = make_planner(skills)
        planner._dispatch_tool_call("scan_entities", {"kind": "passive"}, {})
        receipt = {"id": "req-42", "success": True,
                   "data": {"ok": True, "radius": 16, "kind": "passive", "total": 37,
                            "returned": 2, "truncated": True,
                            "entities": [{"id": 33227, "name": "牛", "type": "passive",
                                          "tier": "PASSIVE", "distance": 4.2,
                                          "x": 851, "y": 63, "z": -2189, "health": 10.0},
                                         {"id": 33228, "name": "羊", "type": "passive",
                                          "tier": "PASSIVE", "distance": 6.0,
                                          "x": 853, "y": 63, "z": -2190, "health": 8.0}]}}

        self.assertTrue(planner.absorb_command_response(receipt))

        note = planner._scan_note
        self.assertIn("未列全", note)
        self.assertIn("truncated=true", note)
        self.assertIn("37", note)
        self.assertIn("牛", note)
        self.assertIn("(851,63,-2189)", note)

    def test_complete_receipt_says_this_is_all(self):
        skills = RecordingSkills(req_id="req-7")
        planner = make_planner(skills)
        planner._dispatch_tool_call("scan_entities", {}, {})
        receipt = {"id": "req-7", "success": True,
                   "data": {"ok": True, "total": 1, "returned": 1, "truncated": False,
                            "entities": [{"id": 5, "name": "牛", "type": "passive",
                                          "distance": 2.0, "x": 1, "y": 64, "z": 1}]}}

        planner.absorb_command_response(receipt)

        self.assertIn("truncated=false", planner._scan_note)
        self.assertNotIn("未列全", planner._scan_note)

    def test_failed_receipt_turns_the_code_into_plain_words_and_never_fakes_success(self):
        for code, needle in (("radius_too_large", "最大 32"),
                             ("not_in_world", "不在世界"),
                             ("bad_kind", "kind")):
            skills = RecordingSkills(req_id="req-9")
            planner = make_planner(skills)
            planner._dispatch_tool_call("scan_entities", {}, {})

            planner.absorb_command_response({"id": "req-9", "success": False,
                                             "data": {"ok": False, "code": code}})

            self.assertIn("没扫成", planner._scan_note)
            self.assertIn(needle, planner._scan_note)
            self.assertFalse(planner._scan_note.startswith("（按需扫描（radius=16 kind=all"
                                                           " limit=24）：共"))

    def test_receipt_without_details_is_not_reported_as_empty_neighbourhood(self):
        skills = RecordingSkills(req_id="req-11")
        planner = make_planner(skills)
        planner._dispatch_tool_call("scan_entities", {}, {})

        planner.absorb_command_response({"id": "req-11", "success": True,
                                         "data": {"ok": True, "total": 5, "entities": []}})

        self.assertIn("当没扫成", planner._scan_note)

    def test_unrelated_receipts_are_left_alone(self):
        skills = RecordingSkills(req_id="req-1")
        planner = make_planner(skills)
        planner._dispatch_tool_call("scan_entities", {}, {})

        self.assertFalse(planner.absorb_command_response({"id": "other", "success": True}))
        self.assertEqual(planner._scan_note, "")

    def test_receipt_reaches_the_next_prompt(self):
        skills = RecordingSkills(req_id="req-3")
        planner = make_planner(skills)
        planner._dispatch_tool_call("scan_entities", {"kind": "passive"}, {})
        planner.absorb_command_response(
            {"id": "req-3", "success": True,
             "data": {"ok": True, "total": 1, "returned": 1, "truncated": False,
                      "entities": [{"id": 33227, "name": "牛", "type": "passive",
                                    "distance": 4.2, "x": 851, "y": 63, "z": -2189}]}})

        text = planner.build_prompt_layers("Steve", "hi", summary_state(), "AI").user

        self.assertIn("牛", text)
        self.assertIn("scan_entities", text)

    def test_scan_is_sent_when_a_partial_view_hides_the_animals(self):
        """摘要里看不到动物坐标时，`farm_cycle` 自己按需扫一趟（只读、有界）——
        不许把「摘要里没有」写成「附近没有动物」。"""
        skills = RecordingSkills()
        planner = make_planner(skills)
        ctx = {"actor_tier": "master", "actor_name": "Steve",
               "player": {"x": 850.0, "y": 63.0, "z": -2190.0},
               "inventory": [{"slot": 1, "name": "wheat", "count": 4}],
               "entities": entity_summary(nearest={"distance": 4.0, "name": "BoHuYeShan",
                                                   "type": "player"},
                                          items={"count": 0})}

        self.assertFalse(planner._dispatch_tool_call("farm_cycle", {"feed": True}, ctx))

        self.assertIn("scan_entities", skills.names())
        steps = {row["step"]: row for row in planner.cycle_status()["steps"]}
        self.assertIn("按需扫描", steps["feed"]["detail"])
        self.assertIn("按需", steps["feed"]["detail"])


class ScanReceiptWiringTests(unittest.TestCase):
    """session 的 `command_response` 要把扫描回执转给 planner（否则按需扫描等于白扫）。"""

    def _session(self, planner):
        from types import SimpleNamespace

        from agent.session import Session

        session = Session.__new__(Session)
        session.planner = planner
        session.action_manager = SimpleNamespace(handle_response=lambda *a, **k: None)
        session._manual_action_reqs = set()
        session._manual_task_kind = None
        return session

    def test_command_response_reaches_the_planner_absorber(self):
        skills = RecordingSkills(req_id="req-55")
        planner = make_planner(skills)
        planner._dispatch_tool_call("scan_entities", {"kind": "passive"}, {})
        session = self._session(planner)

        session.handle_event("command_response",
                             {"id": "req-55", "success": True,
                              "data": {"ok": True, "total": 3, "returned": 1,
                                       "truncated": True,
                                       "entities": [{"id": 1, "name": "牛", "type": "passive",
                                                     "distance": 2.0, "x": 5, "y": 64, "z": 5}]}})

        self.assertIn("牛", planner._scan_note)
        self.assertIn("未列全", planner._scan_note)

    def test_unrelated_command_responses_leave_the_scan_note_alone(self):
        planner = make_planner(RecordingSkills())
        session = self._session(planner)

        session.handle_event("command_response", {"id": "nope", "success": True, "data": {}})

        self.assertEqual(planner._scan_note, "")


if __name__ == "__main__":
    unittest.main()
