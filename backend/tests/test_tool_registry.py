"""工具注册表（`agent/tool_registry.py`）的一致性验收。

用户口径：「工具只好建立为标准」——注册表是**唯一事实来源**：
  - 注册表 ↔ L3 工具表一致（无孤儿、无未登记工具）；
  - 每条工具都有 handler 与权限字段，handler 真能在 Planner 上调用；
  - 阶段过滤与旧行为逐字一致（注入哪些组、组内顺序、按名去重）；
  - 派发由注册表驱动（未登记的 tool 一律拒绝；权限门统一执行）；
  - 工具目录 `docs/dev/TOOLS.md` 由脚本生成、与注册表不脱节（不许手写）。
"""

import importlib.util
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.planner import (  # noqa: E402
    ACTION_GROUPS, PHASE_GROUPS, Planner, _actions_for_phase, build_tool_schemas,
)
from agent.permissions import TIER_GUEST, PermissionsStore  # noqa: E402
from agent.tool_registry import TOOL_BY_NAME, TOOL_SPECS, ToolPermission  # noqa: E402

BACKEND = Path(__file__).resolve().parent.parent
REPO_ROOT = BACKEND.parent


class DummyMemory:
    def build_context(self):
        return {}

    def get_recent_context(self, _count):
        return []


class DummySkills:
    def __init__(self):
        self.calls = []

    def __getattr__(self, _name):
        def _record(*args, **kwargs):
            self.calls.append((_name, args, kwargs))
            return {"success": True}

        return _record


def _planner(skills=None, **kwargs):
    return Planner(llm_service=None, memory=DummyMemory(),
                   skills=skills if skills is not None else DummySkills(), **kwargs)


class RegistryConsistencyTests(unittest.TestCase):
    def test_registry_matches_the_built_tool_schemas(self):
        built = {item["function"]["name"] for item in build_tool_schemas()}
        registered = {spec.name for spec in TOOL_SPECS}

        self.assertEqual(built, registered)
        self.assertEqual(len(TOOL_SPECS), len(registered), "注册表里不能有重名工具")

    def test_no_orphan_actions_in_the_group_table(self):
        """动作组表由注册表生成：两边名字集合必须完全一致（无孤儿、无漏登记）。"""
        declared = {name for actions in ACTION_GROUPS.values() for name, _, _ in actions}

        self.assertEqual(declared, {spec.name for spec in TOOL_SPECS})

    def test_every_tool_has_a_handler_and_a_permission_field(self):
        for spec in TOOL_SPECS:
            self.assertTrue(spec.handler, f"{spec.name} 缺 handler")
            self.assertIsInstance(spec.permission, ToolPermission, spec.name)
            handler = getattr(Planner, spec.handler, None)
            self.assertTrue(callable(handler), f"{spec.name} 的 handler {spec.handler} 不存在")

    def test_every_tool_carries_a_description_and_parameters(self):
        for spec in TOOL_SPECS:
            self.assertTrue(spec.description.strip(), spec.name)
            schema = spec.schema()
            self.assertEqual(schema["type"], "function")
            self.assertEqual(schema["function"]["parameters"]["type"], "object")
            self.assertEqual(schema["function"]["parameters"]["required"], [])


class PhaseFilteringTests(unittest.TestCase):
    """阶段过滤必须与旧 `PHASE_GROUPS` + `ACTION_GROUPS` 行为逐字一致。"""

    def _legacy_expected(self, phase: str) -> list[str]:
        groups = PHASE_GROUPS.get(phase, PHASE_GROUPS["idle"])
        seen: set[str] = set()
        out: list[str] = []
        for group in groups:
            for name, _, _ in ACTION_GROUPS.get(group, ()):
                if name not in seen:
                    seen.add(name)
                    out.append(name)
        return out

    def test_every_phase_matches_the_legacy_ordering(self):
        for phase in PHASE_GROUPS:
            actual = [name for name, _, _ in _actions_for_phase(phase)]
            self.assertEqual(actual, self._legacy_expected(phase), phase)

    def test_schema_order_matches_the_actions_order(self):
        for phase in PHASE_GROUPS:
            built = [item["function"]["name"] for item in build_tool_schemas(phase)]
            self.assertEqual(built, self._legacy_expected(phase), phase)

    def test_idle_is_a_superset_of_every_phase(self):
        idle = {name for name, _, _ in _actions_for_phase("idle")}
        for phase in PHASE_GROUPS:
            names = {name for name, _, _ in _actions_for_phase(phase)}
            self.assertTrue(names <= idle, f"{phase} 不该有 idle 之外的独家工具")


class RegistryDrivenDispatchTests(unittest.TestCase):
    def test_unregistered_tool_is_rejected(self):
        skills = DummySkills()
        planner = _planner(skills=skills)

        self.assertFalse(planner._dispatch_tool_call("not_a_tool", {}, {}))
        self.assertEqual(skills.calls, [])

    def test_tier_gate_is_driven_by_the_registry(self):
        """`grant_territory` 的 tier=territory 门：guest 被拒（一帧不发），master 放行。"""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            store = PermissionsStore(Path(tmp) / "p.json")
            store.set({"territory": {"guest": False}})
            skills = DummySkills()
            planner = _planner(skills=skills, permissions=store)

            denied = planner._dispatch_tool_call(
                "grant_territory", {"name": "home", "radius": 16},
                {"actor_tier": TIER_GUEST})

            self.assertFalse(denied)
            self.assertEqual(skills.calls, [], "未授权时一帧都不发")
            self.assertIn("没资格", planner._territory_note)

    def test_container_gate_uses_the_shared_access_helper(self):
        """容器工具的权限在注册表里标 container=True；统一校验走同一个 helper。"""
        spec = TOOL_BY_NAME["container_move"]
        self.assertTrue(spec.permission.container)
        self.assertFalse(spec.permission.read_only)

        container_specs = {s.name for s in TOOL_SPECS if s.permission.container}
        self.assertEqual(container_specs,
                         {"container_move", "container_click", "borrow_item",
                          "view_container"})
        # 只读变体只有 `view_container` 一个（no_touch 拒、borrow_only 只读可以）。
        read_only_specs = {s.name for s in TOOL_SPECS if s.permission.read_only}
        self.assertEqual(read_only_specs, {"view_container"})


class ToolDocTests(unittest.TestCase):
    """`docs/dev/TOOLS.md` 必须与注册表一致（由脚本生成，不许手写）。"""

    def _load_module(self):
        path = BACKEND / "scripts" / "dump_tools.py"
        spec = importlib.util.spec_from_file_location("dump_tools", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_generated_doc_is_current(self):
        module = self._load_module()
        expected = module.render()
        target = REPO_ROOT / "docs" / "dev" / "TOOLS.md"

        if not target.exists():
            # docs/dev/ 是**刻意** gitignored 的（开发文档进仓库但不提交）：干净 checkout（CI）
            # 上这个文件必然不存在，所以"缺失"是环境事实，不是失败。本地先跑
            # backend/scripts/dump_tools.py 生成它，这条断言才会真正生效。
            self.skipTest("docs/dev/TOOLS.md 不在版本控制内（.gitignore），本地生成后才校验")
        self.assertEqual(target.read_text(encoding="utf-8").replace("\r\n", "\n"), expected,
                         "TOOLS.md 与注册表脱节：重跑 scripts/dump_tools.py")

    def test_render_is_deterministic(self):
        module = self._load_module()

        self.assertEqual(module.render(), module.render())


if __name__ == "__main__":
    unittest.main()
