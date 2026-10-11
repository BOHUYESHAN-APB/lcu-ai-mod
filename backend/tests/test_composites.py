"""复合体（`agent/composites.py`，AGENT_ARCH §10.2 #4）的验收测试。

用户口径 / 工单验收：

  ① **成功路径**：声明的每一步都**真被调用**、且**顺序对**；
  ② 某步失败 ⇒ 报出**第几步 + 哪一步 + 它自己的 code**，且**已完成的前几步如实保留**；
  ③ 某步被**权限门 / 区域门 / 容器门**拦 ⇒ **原样转发**该门的原因（不重试、不换路绕过）；
  ④ 复合体**不能**调用未注册的东西（声明期 + 运行期都拒，一条测试各证明一次）；
  ⑤ 取消 ⇒ 当前步被停掉、**后面的步骤不再走**；
  ⑥ 关掉开关（`config.json → composites.enabled=false`）⇒ 行为回到现在（**一步都不发**）；
  ⑦ 进度**按步单调**（job 句柄四样门槛齐备，失败 code = 那一步的 code）。

外加接线：注册表里的工具条目（L3 可见、静态前缀 L0/L1 逐字节不变）、config.json 默认值。
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.composites import (  # noqa: E402
    CHECK_BED_IN_INVENTORY, CHECK_KINDS, CODE_AWAIT_TIMEOUT, CODE_CANCELLED, CODE_MISSING_ARG,
    CODE_PERMISSION, CODE_SKIPPED, CODE_UNREGISTERED, COMPOSITE_BY_NAME, COMPOSITES,
    DEFAULT_AWAIT_SECONDS, DEPOSIT_ALL, MAKE_BED, STATUS_CANCELLED, STATUS_DONE, STATUS_FAILED,
    WHEN_FAIL, Check, Composite, CompositeRejected, CompositeRun, bed_craftable,
    bed_in_inventory, composite_note, evaluate, expire_run, has_item, hotbar_slot, item_matches,
    missing_item, run_composite, slot, step, validate_composite)
from agent.config_store import ConfigStore, _default_config  # noqa: E402
from agent.job_registry import (JOB_CANCELLED, JOB_DONE, JOB_FAILED,  # noqa: E402
                               JOB_RUNNING, JobRegistry)
from agent.permissions import PermissionsStore  # noqa: E402
from agent.planner import PHASE_GROUPS, Planner, _actions_for_phase, build_tool_schemas  # noqa: E402
from agent.territory import TerritoryStore  # noqa: E402
from agent.tool_registry import TOOL_BY_NAME, TOOL_SPECS  # noqa: E402

SENTINEL = (918273.0, 64738.0, 51236.0)


class DummyMemory:
    def build_context(self):
        return {}

    def get_recent_context(self, _count):
        return []


class HookSkills:
    """把技能调用记下来（不碰 wire）；`on_call` 用来在某一刻插入副作用（如取消）。"""

    def __init__(self):
        self.calls: list[str] = []
        self.on_call = None

    def __getattr__(self, name):
        def _record(*_args, **_kwargs):
            self.calls.append(name)
            if callable(self.on_call):
                self.on_call(name)
            return {"success": True}

        return _record


class FakeActionManager:
    """只记"有没有被停"（复合体的取消路径要停当前步）。"""

    def __init__(self):
        self.stops = 0
        self.current_label = None

    def stop(self):
        self.stops += 1


class FakeClock:
    """假钟：把"等超时"测出来（**不是定时器** —— 时间只由测试喂，代码里没有后台触发）。"""

    def __init__(self, now: float = 1000.0):
        self.now = float(now)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += float(seconds)


class RecordingLlm:
    """只记"有没有被叫"（挂起/续跑**不许**引发任何模型调用）。"""

    def __init__(self):
        self.calls: list[dict] = []
        self.api_key = "test"

    def chat(self, *args, **kwargs):
        self.calls.append({"args": args, "kwargs": kwargs})
        return {"content": ""}


_UNSET = object()


def _planner(*, craft=True, job_registry=_UNSET, skills=None, action_manager=None,
             llm_service=None, **kwargs):
    tmp = tempfile.mkdtemp()
    permissions = PermissionsStore(Path(tmp) / "permissions.json")
    permissions.set({"craft": {"guest": craft}, "containers": {"guest": craft}})
    territory = TerritoryStore(root=tmp)
    # 一个 free（随取）与一个 no_touch（什么都不许动）的箱子，用来验证容器门被转发。
    territory.remember_container("仓库", x=1, y=2, z=3, policy="free")
    territory.remember_container("秘密", x=1, y=2, z=4, policy="no_touch")
    kwargs.setdefault("memory", DummyMemory())
    kwargs.setdefault("skills", skills if skills is not None else HookSkills())
    if job_registry is _UNSET:
        kwargs.setdefault("job_registry", JobRegistry())
    else:
        kwargs["job_registry"] = job_registry      # 显式 None ＝"就不注入台账"
    return Planner(llm_service=llm_service, permissions=permissions, territory=territory,
                   action_manager=action_manager, **kwargs)


def _ctx(**extra) -> dict:
    ctx = {"inventory": [], "actor_tier": "guest", "entities": [],
           "player": {"x": 0.0, "y": 64.0, "z": 0.0}}
    ctx.update(extra)
    return ctx


def _dispatch_recorder(*, fail_at: str = "", code: str = "tool_failed", detail: str = "",
                       on_call=None):
    """假派发口：记下 (工具, 参数) 顺序；可按工具名让某一步失败。"""
    calls: list[tuple] = []

    def _dispatch(tool, payload, _context):
        calls.append((tool, dict(payload)))
        if callable(on_call):
            on_call(tool, calls)
        if fail_at and tool == fail_at:
            return {"ok": False, "code": code, "detail": detail}
        return True

    return _dispatch, calls


# ── ① 声明式 + 注册表接线 ─────────────────────────────────────

class DeclarationTests(unittest.TestCase):
    def test_two_real_composites_are_declared_with_their_steps(self):
        """落地的复合体：名字 + 步骤（顺序即执行顺序）。"""
        self.assertEqual([comp.name for comp in COMPOSITES], ["make_bed", "deposit_all"])
        self.assertEqual([item.tool for item in MAKE_BED.steps],
                         ["get_inventory", "craft_via_recipe", "select_hotbar",
                          "place_block", "sleep_in_bed"])
        self.assertEqual([item.label for item in MAKE_BED.steps],
                         ["check_backpack", "craft_bed", "hold_bed", "place_bed", "sleep"])
        self.assertEqual([item.tool for item in DEPOSIT_ALL.steps],
                         ["view_container", "haul"])
        self.assertEqual([item.label for item in DEPOSIT_ALL.steps],
                         ["peek_target", "haul_to_container"])

    def test_every_step_declares_a_registered_tool_and_an_ok_declaration(self):
        for comp in COMPOSITES:
            validate_composite(comp, is_registered=lambda name: name in TOOL_BY_NAME)
            for item in comp.steps:
                self.assertIn(item.tool, TOOL_BY_NAME, comp.name)
                if item.when is not None:
                    self.assertIn(item.when.kind, CHECK_KINDS)

    def test_composites_are_registered_as_tools_with_job_flag(self):
        for comp in COMPOSITES:
            spec = TOOL_BY_NAME[comp.name]
            self.assertEqual(spec.args, ",".join(comp.args), comp.name)
            self.assertEqual(spec.groups, ("composite",), comp.name)
            self.assertTrue(spec.job, "复合体是长任务 ⇒ 登记 job 句柄")
            self.assertTrue(callable(getattr(Planner, spec.handler, None)),
                            f"{comp.name} 的入口 {spec.handler} 不存在")

    def test_composites_are_in_l3_for_every_phase_but_not_in_the_static_prefix(self):
        for phase in PHASE_GROUPS:
            names = [name for name, _, _ in _actions_for_phase(phase)]
            for comp in COMPOSITES:
                self.assertIn(comp.name, names, f"{comp.name} 在 {phase} 阶段该可用")
        layers = _planner().build_prompt_layers(
            "Steve", "hi", {"task_state": {"kind": "idle", "status": "idle"}}, "AI")
        tools = {item["function"]["name"] for item in build_tool_schemas("idle")}
        for comp in COMPOSITES:
            self.assertIn(comp.name, tools)
            self.assertNotIn(comp.name, layers.system, "复合体只走 L3，不进 L0/L1")

    def test_static_prefix_is_byte_for_byte_unchanged(self):
        """本批**零 prompt 改动**：L0+L1 静态前缀仍是 1648（只减不抬）。"""
        layers = _planner().build_prompt_layers(
            "Steve", "hi", {"task_state": {"kind": "idle", "status": "idle"}}, "AI")

        self.assertEqual(len(layers.system), 1648)

    def test_declaration_is_rejected_when_it_calls_something_unregistered(self):
        """④ 声明期就拒：复合体内部**只准**引用已注册的工具。"""
        bogus = Composite("bogus", "x", (step("not_a_tool", name="bad"),))

        with self.assertRaises(CompositeRejected) as caught:
            validate_composite(bogus, is_registered=lambda name: name in TOOL_BY_NAME)
        self.assertIn("未注册", str(caught.exception))

    def test_declaration_is_rejected_for_duplicate_labels_and_unknown_wiring(self):
        dup = Composite("dup", "x", (step("get_inventory", name="same"),
                                     step("place_block", name="same")))
        with self.assertRaises(CompositeRejected):
            validate_composite(dup, is_registered=lambda name: name in TOOL_BY_NAME)
        bad_bind = Composite("bad", "x", (step("get_inventory", {"a": {"from": "magic"}}),))
        with self.assertRaises(CompositeRejected):
            validate_composite(bad_bind, is_registered=lambda name: name in TOOL_BY_NAME)
        bad_check = Composite("bad2", "x", (step("get_inventory", when=Check("teleport")),))
        with self.assertRaises(CompositeRejected):
            validate_composite(bad_check, is_registered=lambda name: name in TOOL_BY_NAME)

    def test_status_words_match_the_job_registry(self):
        """复合体不 import job_registry（免得注册表在 import 期被拽进环里）——同值由这里钉死。"""
        self.assertEqual((STATUS_DONE, STATUS_FAILED, STATUS_CANCELLED),
                         (JOB_DONE, JOB_FAILED, JOB_CANCELLED))

    def test_item_family_and_hotbar_binding_helpers(self):
        self.assertTrue(item_matches("white_bed", "bed"))
        self.assertFalse(item_matches("bedrock", "bed"))
        inventory = [{"name": "white_bed", "count": 1, "slot": 4},
                     {"name": "oak_planks", "count": 8, "slot": 20}]
        self.assertEqual(hotbar_slot(inventory, "bed"), 4)
        self.assertIsNone(hotbar_slot(inventory, "oak_planks"), "主背包里的东西不算热栏")
        self.assertEqual(TOOL_SPECS[-2].name, "make_bed")


# ── ② / ④ / ⑤ / ⑦ 引擎（假派发口，不碰 planner）──────────────

class EngineTests(unittest.TestCase):
    def test_success_path_calls_every_declared_step_in_order(self):
        dispatch, calls = _dispatch_recorder()
        run = run_composite(CompositeRun(DEPOSIT_ALL, {"container": "仓库"}),
                            dispatch=dispatch, context=_ctx())

        self.assertEqual([tool for tool, _ in calls], ["view_container", "haul"])
        self.assertEqual(calls[0][1], {"target": "仓库"})
        self.assertEqual(calls[1][1], {"from": "self", "to": "仓库", "item": "", "count": ""})
        self.assertEqual(run.status, STATUS_DONE)
        self.assertEqual(run.result()["done"], ["peek_target", "haul_to_container"])
        self.assertEqual(run.progress(), 1.0)

    def test_failure_reports_step_index_and_its_own_code(self):
        dispatch, calls = _dispatch_recorder(fail_at="haul", code="haul:no_touch",
                                            detail="（「仓库」是 no_touch：什么都不许动）")
        run = run_composite(CompositeRun(DEPOSIT_ALL, {"container": "仓库"}),
                            dispatch=dispatch, context=_ctx())

        self.assertEqual(run.status, STATUS_FAILED)
        self.assertEqual((run.failed_index, run.failed_step), (2, "haul_to_container"))
        self.assertEqual(run.failure_code, "haul:no_touch")
        self.assertEqual(run.result()["done"], ["peek_target"], "前面那一步照实记着")
        self.assertIn("第 2 步 haul_to_container", run.note)
        self.assertIn("haul:no_touch", run.note)
        self.assertIn("什么都不许动", run.note)
        self.assertEqual(len(calls), 2, "失败就停在这一步，不再往下")

    def test_optional_step_failure_is_recorded_but_does_not_stop(self):
        dispatch, calls = _dispatch_recorder(
            fail_at="view_container", code="container:no_touch",
            detail="（「仓库」是 no_touch：什么都不许动）")
        run = run_composite(CompositeRun(DEPOSIT_ALL, {"container": "仓库"}),
                            dispatch=dispatch, context=_ctx())

        self.assertEqual(run.status, STATUS_DONE)
        self.assertEqual([tool for tool, _ in calls], ["view_container", "haul"])
        self.assertIn("peek_target 没成（container:no_touch", run.note)

    def test_skipped_step_is_not_dispatched(self):
        dispatch, calls = _dispatch_recorder()
        inventory = [{"name": "white_bed", "count": 1, "slot": 3}]
        run = run_composite(CompositeRun(MAKE_BED, {}), dispatch=dispatch,
                            context=_ctx(inventory=inventory))

        self.assertEqual([tool for tool, _ in calls],
                         ["get_inventory", "select_hotbar", "place_block", "sleep_in_bed"])
        self.assertEqual(run.result()["skipped"], ["craft_bed"])
        self.assertIn("跳过 craft_bed", run.note)

    def test_unregistered_step_is_refused_before_any_dispatch(self):
        """④ 运行期也拒：复合体绝不调未注册的东西（假派发口一次都不该被叫到）。"""
        dispatch, calls = _dispatch_recorder()
        bogus = Composite("bogus", "x", (step("not_a_tool", name="bad"),))
        run = run_composite(CompositeRun(bogus, {}), dispatch=dispatch, context=_ctx())

        self.assertEqual(calls, [])
        self.assertEqual(run.failure_code, CODE_UNREGISTERED)
        self.assertEqual((run.failed_index, run.failed_step), (1, "bad"))

    def test_missing_required_argument_stops_before_dispatch(self):
        dispatch, calls = _dispatch_recorder()
        run = run_composite(CompositeRun(DEPOSIT_ALL, {}), dispatch=dispatch, context=_ctx())

        self.assertEqual(calls, [], "取不到参数就一步都不发")
        self.assertEqual(run.failed_index, 2)
        self.assertEqual(run.failure_code, CODE_MISSING_ARG)

    def test_cancel_stops_the_current_step_and_the_rest(self):
        """⑤ 取消 ⇒ 停当前步 + 后面的步骤不再走。"""
        stops: list[str] = []

        def _cancel_on_second(tool, _calls):
            if tool == "select_hotbar":
                run.cancel("玩家让我停")

        dispatch, calls = _dispatch_recorder(on_call=_cancel_on_second)
        run = CompositeRun(MAKE_BED, {})
        run_composite(run, dispatch=dispatch, context=_ctx(
            inventory=[{"name": "white_bed", "count": 1, "slot": 2}]),
            cancel_current=lambda: stops.append("stop"))

        self.assertEqual([tool for tool, _ in calls],
                         ["get_inventory", "select_hotbar"], "place_block/sleep 不该再发")
        self.assertEqual(stops, ["stop"], "取消路径只跑一次（停当前步）")
        self.assertEqual(run.status, STATUS_CANCELLED)
        self.assertEqual(run.failure_code, CODE_CANCELLED)
        self.assertIn("被取消", run.note)

    def test_progress_is_monotonic_by_step(self):
        """⑦ 进度按步、单调不减。"""
        samples: list[float] = []

        def _sample(_tool, _calls):
            samples.append(run.progress())

        dispatch, _calls = _dispatch_recorder(on_call=_sample)
        run = CompositeRun(MAKE_BED, {})
        run_composite(run, dispatch=dispatch,
                      context=_ctx(inventory=[{"name": "white_bed", "count": 1, "slot": 1}]))

        self.assertEqual(samples, sorted(samples))
        self.assertEqual(samples, [0.0, 0.4, 0.6, 0.8])
        self.assertEqual(run.progress(), 1.0)

    def test_condition_helpers_are_declarative_data(self):
        self.assertEqual(has_item("bed").kind, "has_item")
        self.assertEqual(missing_item("wool", 3).count, 3)
        self.assertEqual(bed_craftable().kind, "bed_craftable")
        self.assertEqual(slot("bed"), {"from": "hotbar_slot", "item": "bed"})
        self.assertEqual(evaluate(None, {}), (True, "", "", ""))
        met, code, detail, how = evaluate(bed_craftable(), {"inventory": []})
        self.assertFalse(met)
        self.assertEqual((code, how), ("bed_missing", WHEN_FAIL))
        self.assertIn("材料不够", detail)

    def test_note_is_honest_without_change(self):
        run = CompositeRun(MAKE_BED, {})
        dispatch, _calls = _dispatch_recorder()
        run_composite(run, dispatch=dispatch, context=_ctx())
        self.assertEqual(run.note, composite_note(run))


# ── ② / ③ / ④ / ⑤ / ⑥ planner 接线 ────────────────────────────

class PlannerCompositeTests(unittest.TestCase):
    def test_make_bed_success_path_runs_the_steps_in_order(self):
        planner = _planner()
        ctx = _ctx(inventory=[{"name": "white_bed", "count": 1, "slot": 0}])

        self.assertTrue(planner._dispatch_tool_call("make_bed", {}, ctx))
        self.assertEqual(planner.skills.calls,
                         ["get_inventory", "select_hotbar", "place_block", "sleep_in_bed"])
        self.assertEqual(planner._composite_last["status"], STATUS_DONE)
        self.assertEqual(planner._composite_last["skipped"], ["craft_bed"])
        self.assertIn("复合体 make_bed", planner._composite_note)

    def test_make_bed_reports_missing_materials_at_the_craft_step(self):
        """② + 缺料如实报：卡在第 2 步，code=bed_missing，前面做完的步骤留在账上。"""
        planner = _planner()
        skills = planner.skills

        self.assertFalse(planner._dispatch_tool_call("make_bed", {}, _ctx()))
        last = planner._composite_last
        self.assertEqual((last["step_index"], last["step"]), (2, "craft_bed"))
        self.assertEqual(last["code"], "bed_missing")
        self.assertEqual(last["done"], ["check_backpack"])
        self.assertIn("材料不够", last["detail"])
        self.assertIn("已经做完 check_backpack", planner._composite_note)
        self.assertEqual(skills.calls, ["get_inventory"], "素材不够就别硬造")

    def test_craft_permission_gate_is_forwarded_not_bypassed(self):
        """③ 权限门：合成那一步被 tier 门拦下 ⇒ 原样转发门的原因（不重试、不换路）。"""
        planner = _planner(craft=False)
        ctx = _ctx(inventory=[{"name": "wool", "count": 3, "slot": 9},
                              {"name": "oak_planks", "count": 3, "slot": 10}])

        self.assertFalse(planner._dispatch_tool_call("make_bed", {}, ctx))
        last = planner._composite_last
        self.assertEqual((last["step_index"], last["step"], last["code"]),
                         (2, "craft_bed", CODE_PERMISSION))
        self.assertIn("没被允许让我造东西", last["detail"])
        self.assertEqual(planner.skills.calls, ["get_inventory"], "被门拦下一帧都不发")

    def test_container_policy_gate_is_forwarded(self):
        """③ 容器门：no_touch 的箱子 ⇒ haul 自己的拒绝原话被转发（复合体不绕）。"""
        planner = _planner()

        self.assertFalse(planner._dispatch_tool_call("deposit_all", {"container": "秘密"},
                                                     _ctx()))
        last = planner._composite_last
        self.assertEqual(last["step_index"], 2)
        self.assertEqual(last["code"], "haul:no_touch")
        self.assertIn("no_touch", last["detail"])
        self.assertIn("什么都不许动", planner._composite_note)

    def test_area_gate_reason_is_forwarded(self):
        """③ 区域门：没有授权地块 ⇒ 农活那一步如实转发"不在别人的地里干活"。"""
        planner = _planner()
        probe = Composite("probe_area", "x", (step("farm", {"action": "harvest"},
                                                   name="farm_here"),))

        run = run_composite(CompositeRun(probe, {}), dispatch=planner._composite_dispatch,
                            context=_ctx())
        self.assertEqual(run.failure_code, "tool_failed")
        self.assertIn("没有授权给我的农田", run.failure_detail)

    def test_deposit_all_without_container_sends_nothing(self):
        planner = _planner()

        self.assertFalse(planner._dispatch_tool_call("deposit_all", {}, _ctx()))
        self.assertEqual(planner._composite_last["code"], CODE_MISSING_ARG)
        self.assertEqual(planner.skills.calls, [])

    def test_composite_registers_a_job_with_all_four_gates(self):
        """⑦ 四样门槛齐备的 job 句柄：进度 / 终止条件 / 失败 code / 取消路径。"""
        registry = JobRegistry()
        planner = _planner(job_registry=registry)
        ctx = _ctx(inventory=[{"name": "white_bed", "count": 1, "slot": 0}])

        planner._dispatch_tool_call("make_bed", {}, ctx)
        jobs = registry.jobs()

        self.assertEqual(len(jobs), 1, "复合体只登记**一条** job（不重复账）")
        row = jobs[0].as_dict()
        self.assertEqual(row["tool"], "make_bed")
        self.assertEqual(row["status"], JOB_DONE)
        self.assertEqual(row["progress"], 1.0)
        self.assertTrue(row["progress_source"])
        self.assertTrue(row["terminal_condition"])
        self.assertTrue(row["has_cancel_path"])
        self.assertEqual(row["failure_code"], "composite_failed")
        self.assertEqual(row["result"]["steps"][-1]["step"], "sleep")

    def test_failed_composite_job_carries_the_step_code(self):
        registry = JobRegistry()
        planner = _planner(craft=False, job_registry=registry)
        ctx = _ctx(inventory=[{"name": "wool", "count": 3, "slot": 9},
                              {"name": "oak_planks", "count": 3, "slot": 10}])

        planner._dispatch_tool_call("make_bed", {}, ctx)
        job = registry.jobs()[0]

        self.assertEqual(job.status, JOB_FAILED)
        self.assertEqual(job.failure_code, CODE_PERMISSION, "失败 code = 那一步自己的 code")
        self.assertEqual(job.result["step"], "craft_bed")
        self.assertIn("卡在第 2 步 craft_bed", str(job.result.get("message")))

    def test_cancel_via_the_job_path_stops_the_remaining_steps(self):
        """⑤ 取消：job 的取消路径落到 `action_manager.stop`，后面的步骤不再走。"""
        registry = JobRegistry()
        action_manager = FakeActionManager()
        skills = HookSkills()
        planner = _planner(job_registry=registry, skills=skills, action_manager=action_manager)
        skills.on_call = (lambda name: registry.cancel_all("玩家让我停")
                          if name == "select_hotbar" else None)
        ctx = _ctx(inventory=[{"name": "white_bed", "count": 1, "slot": 0}])

        self.assertFalse(planner._dispatch_tool_call("make_bed", {}, ctx))
        self.assertEqual(skills.calls, ["get_inventory", "select_hotbar"])
        self.assertEqual(action_manager.stops, 1, "当前步要被停掉")
        self.assertEqual(registry.jobs()[0].status, JOB_CANCELLED)
        self.assertIn("被取消", planner._composite_note)

    def test_switch_off_refuses_the_call_and_sends_nothing(self):
        """⑥ 关掉 ⇒ 行为回到现在：一步都不发，且如实说明被关掉了。"""
        planner = _planner(composites_enabled=lambda: False)

        self.assertFalse(planner._dispatch_tool_call("make_bed", {}, _ctx()))
        self.assertEqual(planner.skills.calls, [])
        self.assertIn("关掉", planner._composite_note)
        self.assertEqual(planner.job_registry.jobs(), [], "关掉时连 job 都不该登记")

    def test_nested_call_of_the_same_composite_is_refused(self):
        """复合体不许套自己（防无限递归），如实拒绝。"""
        planner = _planner()
        planner._composite_stack.append("make_bed")

        self.assertFalse(planner._run_composite("make_bed", {}, _ctx()))
        self.assertIn("正在跑", planner._composite_note)

    def test_unknown_composite_name_is_refused(self):
        planner = _planner()

        self.assertFalse(planner._run_composite("nope", {}, _ctx()))
        self.assertIn("没有叫", planner._composite_note)
        self.assertEqual(COMPOSITE_BY_NAME.get("nope"), None)

    def test_planner_without_a_job_registry_still_runs_the_composite(self):
        """没注入 job 台账（单跑/测试）⇒ 照跑，只是不登记 job（行为不静默坏掉）。"""
        planner = _planner(job_registry=None)
        ctx = _ctx(inventory=[{"name": "white_bed", "count": 1, "slot": 0}])

        self.assertTrue(planner._dispatch_tool_call("make_bed", {}, ctx))
        self.assertIsNone(planner.job_registry)


# ── config.json：开关与默认值 ─────────────────────────────────

class CompositeConfigTests(unittest.TestCase):
    def test_default_is_on(self):
        self.assertEqual(_default_config()["composites"], {"enabled": True})

    def test_getter_defaults_to_on_and_follows_the_store(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ConfigStore(Path(tmp) / "config.json")

            self.assertTrue(store.get_composites_enabled(), "默认**开**")
            store.set_app_config({"composites": {"enabled": False}})
            self.assertFalse(store.get_composites_enabled())
            stored = json.loads((Path(tmp) / "config.json").read_text(encoding="utf-8"))
            self.assertIs(stored["composites"]["enabled"], False)
            store.set_app_config({"composites": {"enabled": "是"}})
            self.assertTrue(store.get_composites_enabled())


# ── 跨轮推进（挂起 → 续跑 → 走完 / 超时 / 取消 / 无僵尸）────────

def _craft_context(**extra) -> dict:
    """手里有羊毛 + 木板（够造床）、但**还没有床**：正是"合成后要等下一轮"的场景。"""
    ctx = _ctx(inventory=[{"name": "wool", "count": 3, "slot": 9},
                          {"name": "oak_planks", "count": 3, "slot": 10}])
    ctx.update(extra)
    return ctx


def _bed_context(**extra) -> dict:
    """下一轮的快照：床已经到手（合成结果可见）。"""
    ctx = _ctx(inventory=[{"name": "white_bed", "count": 1, "slot": 2}])
    ctx.update(extra)
    return ctx


class AwaitDeclarationTests(unittest.TestCase):
    def test_make_bed_waits_for_the_bed_before_holding_it(self):
        hold = MAKE_BED.steps[2]

        self.assertEqual(hold.label, "hold_bed")
        self.assertEqual(hold.await_check, CHECK_BED_IN_INVENTORY)
        self.assertEqual(hold.await_seconds, 10.0)

    def test_unknown_await_check_is_rejected_at_declaration_time(self):
        bad = Composite("bad", "x", (step("select_hotbar", name="s",
                                          await_check="not_a_check"),))

        with self.assertRaises(CompositeRejected) as caught:
            validate_composite(bad, is_registered=lambda name: name in TOOL_BY_NAME)
        self.assertIn("await_check", str(caught.exception))

    def test_await_seconds_must_be_positive(self):
        bad = Composite("bad", "x", (step("select_hotbar", name="s",
                                          await_check=CHECK_BED_IN_INVENTORY,
                                          await_seconds=0),))

        with self.assertRaises(CompositeRejected):
            validate_composite(bad, is_registered=lambda name: name in TOOL_BY_NAME)
        self.assertGreater(DEFAULT_AWAIT_SECONDS, 0, "默认上限必须是正数（不许无限等）")

    def test_bed_in_inventory_is_a_declarative_check(self):
        self.assertEqual(bed_in_inventory().kind, CHECK_BED_IN_INVENTORY)
        self.assertIn(CHECK_BED_IN_INVENTORY, CHECK_KINDS)
        met, _code, detail, _how = evaluate(bed_in_inventory(), {"inventory": []})
        self.assertFalse(met)
        self.assertIn("还没有床", detail)
        self.assertTrue(evaluate(bed_in_inventory(),
                                 {"inventory": [{"name": "red_bed", "count": 1}]})[0])

    def test_no_background_trigger_machinery_in_the_module(self):
        """挂起靠**既有轮次**推进：这个模块里不许出现线程/定时器/asyncio 定时 ✗。"""
        source = (Path(__file__).resolve().parent.parent / "agent" / "composites.py").read_text(
            encoding="utf-8")

        for forbidden in ("threading", "Timer", "asyncio", "schedule(", "sleep("):
            self.assertNotIn(forbidden, source, f"复合体不许有后台触发：{forbidden}")


class CrossTurnEngineTests(unittest.TestCase):
    def _waiting_composite(self) -> Composite:
        return Composite("probe_wait", "x", (
            step("get_inventory", name="look"),
            step("select_hotbar", {"index": 0}, name="hold",
                 await_check=CHECK_BED_IN_INVENTORY, await_seconds=5.0),
            step("place_block", name="place"),
        ))

    def test_unsatisfied_await_parks_and_sends_nothing(self):
        dispatch, calls = _dispatch_recorder()
        run = CompositeRun(self._waiting_composite(), {}, clock=FakeClock())
        run_composite(run, dispatch=dispatch, context=_ctx())

        self.assertTrue(run.parked)
        self.assertEqual([tool for tool, _ in calls], ["get_inventory"], "等不到就一步都不发")
        self.assertEqual((run.waiting_index, run.waiting_step, run.waiting_check), (1, "hold",
                                                                                    CHECK_BED_IN_INVENTORY))
        self.assertEqual(run.status, "", "挂起＝还在跑（不是失败、不是完成）")
        self.assertIn("等「bed_in_inventory」", run.note)
        self.assertIn("上限 5 秒", run.note)
        self.assertEqual(run.result()["waiting"]["check"], CHECK_BED_IN_INVENTORY)

    def test_resume_with_a_new_context_continues_from_the_waiting_step(self):
        dispatch, calls = _dispatch_recorder()
        run = CompositeRun(self._waiting_composite(), {}, clock=FakeClock())
        run_composite(run, dispatch=dispatch, context=_ctx())

        run_composite(run, dispatch=dispatch, context=_ctx(
            inventory=[{"name": "white_bed", "count": 1, "slot": 4}]))

        self.assertEqual([tool for tool, _ in calls], ["get_inventory", "select_hotbar",
                                                       "place_block"])
        self.assertEqual(run.status, STATUS_DONE)
        self.assertFalse(run.parked)
        self.assertEqual(run.progress(), 1.0)

    def test_await_satisfied_at_once_never_parks(self):
        dispatch, calls = _dispatch_recorder()
        run = CompositeRun(self._waiting_composite(), {}, clock=FakeClock())
        run_composite(run, dispatch=dispatch, context=_ctx(
            inventory=[{"name": "white_bed", "count": 1, "slot": 4}]))

        self.assertEqual([tool for tool, _ in calls], ["get_inventory", "select_hotbar",
                                                       "place_block"])
        self.assertEqual(run.status, STATUS_DONE)
        self.assertNotIn("等「", run.note)

    def test_expire_reports_step_check_and_how_long_it_waited(self):
        clock = FakeClock()
        run = CompositeRun(self._waiting_composite(), {}, clock=clock)
        dispatch, _calls = _dispatch_recorder()
        run_composite(run, dispatch=dispatch, context=_ctx())

        clock.advance(5.0)
        self.assertTrue(expire_run(run))
        self.assertEqual(run.status, STATUS_FAILED)
        self.assertEqual((run.failed_index, run.failed_step, run.failure_code),
                         (2, "hold", CODE_AWAIT_TIMEOUT))
        self.assertIn("等「bed_in_inventory」等了 5 秒", run.failure_detail)
        self.assertIn("超过上限 5 秒", run.failure_detail)
        self.assertIn("已经做完 look", run.note)
        self.assertFalse(run.parked)

    def test_expire_is_not_yet_due_before_the_limit(self):
        clock = FakeClock()
        run = CompositeRun(self._waiting_composite(), {}, clock=clock)
        dispatch, _calls = _dispatch_recorder()
        run_composite(run, dispatch=dispatch, context=_ctx())

        clock.advance(4.9)
        self.assertFalse(expire_run(run))
        self.assertIsNone(run.terminal(), "没到期 ⇒ job 还是 running")
        clock.advance(0.2)
        verdict = run.terminal()

        self.assertEqual(verdict["status"], STATUS_FAILED)
        self.assertEqual(verdict["code"], CODE_AWAIT_TIMEOUT, "有界：不靠下一轮也认到期")


class CrossTurnPlannerTests(unittest.TestCase):
    def test_make_bed_parks_then_finishes_on_the_next_turn(self):
        clock = FakeClock()
        registry = JobRegistry()
        planner = _planner(job_registry=registry, composite_clock=clock)

        # 第一轮：合成下发 ⇒ 床还没到手 ⇒ 挂在第 3 步（一步都不再发）
        self.assertTrue(planner._dispatch_tool_call("make_bed", {}, _craft_context()))
        self.assertEqual(planner.skills.calls, ["get_inventory", "craft_via_recipe"])
        parked = planner.parked_composites()
        self.assertEqual(len(parked), 1)
        self.assertEqual(parked[0]["waiting"]["step_index"], 3)
        self.assertEqual(registry.jobs()[0].status, JOB_RUNNING)
        registry.refresh()
        self.assertEqual(registry.jobs()[0].progress, 0.4, "进度停在第 3 步（2/5）")

        # 下一轮：喂新 context（床到了）⇒ 从等那一步接着往下，一路走完
        clock.advance(1.0)
        self.assertEqual(planner.resume_composites(_bed_context()), ["make_bed"])

        self.assertEqual(planner.skills.calls,
                         ["get_inventory", "craft_via_recipe", "select_hotbar", "place_block",
                          "sleep_in_bed"])
        self.assertEqual(planner._composite_last["status"], STATUS_DONE)
        self.assertEqual(planner._composite_last["done"],
                         ["check_backpack", "craft_bed", "hold_bed", "place_bed", "sleep"])
        self.assertEqual(planner.parked_composites(), [], "走完就不该再挂账")
        job = registry.jobs()[0]
        self.assertEqual((job.status, job.progress), (JOB_DONE, 1.0))

    def test_resume_with_a_stale_context_stays_parked_and_quiet(self):
        clock = FakeClock()
        llm = RecordingLlm()
        planner = _planner(llm_service=llm, composite_clock=clock)
        planner._dispatch_tool_call("make_bed", {}, _craft_context())
        before = list(planner.skills.calls)

        for _ in range(3):                      # 三个"安静轮次"：没有任何新东西
            clock.advance(0.5)
            self.assertEqual(planner.resume_composites(_craft_context()), [])

        self.assertEqual(planner.skills.calls, before, "条件没满足就一步都不发")
        self.assertEqual(len(planner.parked_composites()), 1, "仍然挂着等（有界 ✓）")
        self.assertEqual(llm.calls, [], "挂起/续跑本身**不引发任何模型调用**")

    def test_await_timeout_fails_the_job_with_the_step_code(self):
        clock = FakeClock()
        registry = JobRegistry()
        planner = _planner(job_registry=registry, composite_clock=clock)
        planner._dispatch_tool_call("make_bed", {}, _craft_context())

        clock.advance(11.0)
        self.assertEqual(planner.resume_composites(_craft_context()), ["make_bed"])

        last = planner._composite_last
        self.assertEqual((last["step_index"], last["step"], last["code"]),
                         (3, "hold_bed", CODE_AWAIT_TIMEOUT))
        self.assertEqual(last["done"], ["check_backpack", "craft_bed"], "前几步照实保留")
        self.assertIn("等了 11 秒", planner._composite_note)
        self.assertIn("上限 10 秒", planner._composite_note)
        job = registry.jobs()[0]
        self.assertEqual((job.status, job.failure_code), (JOB_FAILED, CODE_AWAIT_TIMEOUT))
        self.assertEqual(job.result["step"], "hold_bed")
        self.assertEqual(planner.parked_composites(), [], "超时就收摊，不留僵尸")

    def test_parked_job_is_bounded_even_without_a_turn(self):
        """没有"下一轮"也不会无限挂着：`jobs.refresh()` 判到期同样如实失败。"""
        clock = FakeClock()
        registry = JobRegistry()
        planner = _planner(job_registry=registry, composite_clock=clock)
        planner._dispatch_tool_call("make_bed", {}, _craft_context())

        clock.advance(10.5)
        changed = registry.refresh()

        self.assertEqual([row["kind"] for row in changed], ["progress", "terminal"])
        self.assertEqual(registry.jobs()[0].status, JOB_FAILED)
        self.assertEqual(registry.jobs()[0].failure_code, CODE_AWAIT_TIMEOUT)
        self.assertEqual(planner.resume_composites(_craft_context()), ["make_bed"],
                         "已经结算过的挂起账会被收掉（无僵尸）")
        self.assertEqual(planner.parked_composites(), [])

    def test_cancel_while_parked_stops_everything(self):
        clock = FakeClock()
        registry = JobRegistry()
        planner = _planner(job_registry=registry, composite_clock=clock)
        planner._dispatch_tool_call("make_bed", {}, _craft_context())
        craft_calls = list(planner.skills.calls)

        self.assertEqual(registry.cancel_all("玩家让我停"), 1)
        # 取消路径自己就"停当前步"（这一步之后就一步都不发了）。
        self.assertEqual(planner.skills.calls, craft_calls + ["stop_all"])
        stopped = list(planner.skills.calls)

        clock.advance(1.0)
        planner.resume_composites(_bed_context())

        self.assertEqual(planner.skills.calls, stopped, "即使条件满足也不许再往下")
        self.assertEqual(planner._composite_last["status"], STATUS_CANCELLED)
        self.assertEqual(registry.jobs()[0].status, JOB_CANCELLED)
        self.assertEqual(planner.parked_composites(), [])

    def test_new_instruction_aborts_the_parked_run(self):
        """一条别的指令（模型说停）⇒ 挂起的复合体被中止，不留僵尸。"""
        clock = FakeClock()
        registry = JobRegistry()
        action_manager = FakeActionManager()
        planner = _planner(job_registry=registry, action_manager=action_manager,
                           composite_clock=clock)
        planner._dispatch_tool_call("make_bed", {}, _craft_context())
        before = list(planner.skills.calls)

        self.assertTrue(planner._dispatch_tool_call("stop", {}, _craft_context()))

        self.assertEqual(planner.parked_composites(), [])
        self.assertEqual(registry.jobs()[0].status, JOB_CANCELLED)
        self.assertEqual(action_manager.stops, 1, "走已有的停路：停当前步")
        self.assertIn("被取消", planner._composite_note)
        clock.advance(1.0)
        planner.resume_composites(_bed_context())
        self.assertEqual(planner.skills.calls, before + ["stop_all"], "取消后不再往下发")

    def test_interrupt_aborts_the_parked_runs(self):
        planner = _planner(composite_clock=FakeClock())
        planner._dispatch_tool_call("make_bed", {}, _craft_context())

        planner.interrupt()

        self.assertEqual(planner.parked_composites(), [])
        self.assertEqual(planner._composite_last["status"], STATUS_CANCELLED)

    def test_multiple_parked_runs_are_all_resumed(self):
        clock = FakeClock()
        registry = JobRegistry()
        planner = _planner(job_registry=registry, composite_clock=clock)

        planner._dispatch_tool_call("make_bed", {}, _craft_context())
        planner._dispatch_tool_call("make_bed", {}, _craft_context())

        self.assertEqual(len(planner.parked_composites()), 2, "允许多个挂起并行 ✓")
        clock.advance(1.0)
        self.assertEqual(sorted(planner.resume_composites(_bed_context())),
                         ["make_bed", "make_bed"])
        self.assertEqual(planner.parked_composites(), [])
        self.assertEqual([job.status for job in registry.jobs()], [JOB_DONE, JOB_DONE])

    def test_switch_off_never_parks(self):
        planner = _planner(composites_enabled=lambda: False, composite_clock=FakeClock())

        self.assertFalse(planner._dispatch_tool_call("make_bed", {}, _craft_context()))
        self.assertEqual(planner.parked_composites(), [])
        self.assertEqual(planner.skills.calls, [])
        self.assertEqual(planner.resume_composites(_bed_context()), [])

    def test_a_normal_turn_drives_the_parked_run_forward(self):
        """挂起的续跑就长在**既有轮次**里（`plan_and_execute`）：这一轮本来就要发生 ✓。"""
        clock = FakeClock()
        registry = JobRegistry()
        llm = RecordingLlm()
        planner = _planner(job_registry=registry, composite_clock=clock, llm_service=llm)
        planner._dispatch_tool_call("make_bed", {}, _craft_context())

        clock.advance(1.0)
        planner.plan_and_execute("Steve", "hi", _bed_context(), "AI")

        self.assertEqual(planner.skills.calls,
                         ["get_inventory", "craft_via_recipe", "select_hotbar", "place_block",
                          "sleep_in_bed"])
        self.assertEqual(registry.jobs()[0].status, JOB_DONE)
        self.assertEqual(planner.parked_composites(), [])
        self.assertEqual(len(llm.calls), 1, "这一轮自己的模型调用；续跑自己不请模型 ✓")

    def test_progress_stays_monotonic_across_park_and_resume(self):
        clock = FakeClock()
        registry = JobRegistry()
        planner = _planner(job_registry=registry, composite_clock=clock)
        samples: list[float] = []

        planner._dispatch_tool_call("make_bed", {}, _craft_context())
        registry.refresh()
        samples.append(registry.jobs()[0].progress)
        for step in range(3):
            clock.advance(0.5)
            planner.resume_composites(_craft_context())
            registry.refresh()
            samples.append(registry.jobs()[0].progress)
        clock.advance(0.5)
        planner.resume_composites(_bed_context())

        self.assertEqual(samples, sorted(samples))
        self.assertEqual(registry.jobs()[0].progress, 1.0)
        self.assertEqual(len(planner.parked_composites()), 0)


if __name__ == "__main__":
    unittest.main()
