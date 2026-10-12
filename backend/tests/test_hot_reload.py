"""后端进程内热载（`agent/hot_reload.py` + `POST /api/reload`）的测试。

分三层，全部**不碰仓库里的源文件**（临时模块写到 `tmp` 目录）：

1. **纯逻辑**：`watch_snapshot` / `changed_paths`（增 / 删 / 改；不存在的路径不炸）；
2. **白/黑名单自洽**：不重叠、白名单顺序＝依赖顺序、`needs_restart` 点名"改了要重启"的那些；
3. **真热载**：真的 `importlib.reload` 一个被改过的临时模块（新值生效）、一个坏模块失败不影响
   其余、黑名单模块永远不进 `reloaded`；以及 `rebuild_session_planner` 用会话**已有**构件重建；
4. **接线**：忙 ⇒ `/api/reload` 明确拒绝且**没有**调用热载（假实现断言没被调用）；空闲 ⇒ 出报告；
   看门狗忙时**跳过 + 打日志**、空闲才热载、`stop()` 后线程结束。
"""

import importlib
import inspect
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

import server  # noqa: E402
from agent import hot_reload  # noqa: E402

# 白名单内部的 import 边（`consumer → 它 import 的白名单模块`）：用来钉住"依赖顺序"。
# 这张表是**静态**的（照 `agent/*.py` 的 import 语句读出来的），改了依赖关系就该改这里。
IMPORT_EDGES: dict[str, tuple[str, ...]] = {
    "agent.entities": ("agent.borrow",),
    "agent.use_on": ("agent.borrow", "agent.craft"),
    "agent.haul": ("agent.borrow", "agent.craft"),
    "agent.container_view": ("agent.borrow",),
    "agent.farm": ("agent.craft", "agent.territory"),
    "agent.modes_engine": ("agent.territory",),
    "agent.where": ("agent.territory",),
    "agent.composites": ("agent.craft", "agent.farm", "agent.territory"),
    "agent.tool_registry": ("agent.composites",),
    "agent.planner": ("agent.borrow", "agent.composites", "agent.container_view", "agent.craft",
                      "agent.entities", "agent.farm", "agent.haul", "agent.modes_engine",
                      "agent.territory", "agent.tool_registry", "agent.use_on"),
}


# ── 快照 / 差异（纯逻辑）─────────────────────────────────────────────────────


class WatchSnapshotTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def test_missing_paths_do_not_explode(self):
        """不存在的路径 / 目录：不进快照，但**绝不抛**。"""
        snapshot = hot_reload.watch_snapshot(
            [self.root / "nope.py", self.root / "missing_dir" / "x.py", self.root])
        self.assertEqual(snapshot, {})

    def test_existing_file_is_tracked_with_mtime_and_size(self):
        probe = self.root / "probe.py"
        probe.write_text("VALUE = 1\n", encoding="utf-8")
        snapshot = hot_reload.watch_snapshot([probe])
        self.assertEqual(list(snapshot), [str(probe)])
        row = snapshot[str(probe)]
        self.assertEqual(row["size"], probe.stat().st_size)
        self.assertIsInstance(row["mtime"], float)

    def test_real_source_paths_are_scanned(self):
        """真源码清单：认得出运行期模块，且不含 tests/ / sdk/ / web/。"""
        paths = hot_reload.source_paths()
        names = {hot_reload.module_name_of(path) for path in paths}
        self.assertIn("agent.planner", names)
        self.assertIn("server", names)
        self.assertIn("protocol.wire_client", names)
        self.assertNotIn("", names)
        self.assertTrue(all("test" not in name for name in names))


class ChangedPathsTests(unittest.TestCase):
    def test_added_removed_and_modified(self):
        before = {"a.py": {"mtime": 1.0, "size": 10}, "b.py": {"mtime": 2.0, "size": 20},
                  "c.py": {"mtime": 3.0, "size": 30}}
        after = {"b.py": {"mtime": 2.5, "size": 20},        # 改：mtime 变了
                 "c.py": {"mtime": 3.0, "size": 31},        # 改：size 变了
                 "d.py": {"mtime": 4.0, "size": 40}}        # 增
        # a.py 缺席 after ⇒ 删
        self.assertEqual(hot_reload.changed_paths(before, after),
                         ["a.py", "b.py", "c.py", "d.py"])

    def test_no_change_is_empty(self):
        same = {"a.py": {"mtime": 1.0, "size": 10}}
        self.assertEqual(hot_reload.changed_paths(same, dict(same)), [])
        self.assertEqual(hot_reload.changed_paths({}, {}), [])

    def test_accepts_path_objects_and_tuples(self):
        """纯函数：键会归一成字符串，值按内容比较（元组/字典都行）。"""
        self.assertEqual(hot_reload.changed_paths({Path("a.py"): (1.0, 10)}, {"a.py": (1.0, 10)}), [])
        self.assertEqual(hot_reload.changed_paths({Path("a.py"): (1.0, 10)}, {"a.py": (1.0, 11)}),
                         ["a.py"])

    def test_changed_modules_maps_paths_and_names(self):
        self.assertEqual(
            hot_reload.changed_modules(["backend/agent/session.py", "agent.farm", "not a module",
                                        "../src/main/java/X.java", "agent.farm.py"]),
            ["agent.farm", "agent.session"])


# ── 白 / 黑名单自洽 ──────────────────────────────────────────────────────────


class WhitelistTests(unittest.TestCase):
    def test_whitelist_and_blacklist_do_not_overlap(self):
        self.assertFalse(set(hot_reload.HOT_MODULES) & set(hot_reload.RESTART_MODULES))

    def test_hot_modules_are_unique(self):
        self.assertEqual(len(set(hot_reload.HOT_MODULES)), len(hot_reload.HOT_MODULES))

    def test_hot_modules_are_listed_leaf_first(self):
        """依赖顺序：被依赖的必须排在依赖者前面（否则热载拿到的是旧绑定）。"""
        order = {name: index for index, name in enumerate(hot_reload.HOT_MODULES)}
        for consumer, deps in IMPORT_EDGES.items():
            self.assertIn(consumer, order, f"{consumer} 不在白名单里")
            for dep in deps:
                self.assertIn(dep, order, f"{dep}（{consumer} 的依赖）不在白名单里")
                self.assertLess(order[dep], order[consumer], f"{dep} 必须排在 {consumer} 前面")
        self.assertEqual(hot_reload.HOT_MODULES[-1], "agent.planner", "planner 必须最后")
        leaves = {"agent.perception", "agent.anchor", "agent.context_budget", "agent.borrow",
                  "agent.craft", "agent.territory", "agent.skills", "agent.home_bed"}
        self.assertEqual(set(IMPORT_EDGES) | leaves, set(hot_reload.HOT_MODULES))

    def test_live_instance_modules_are_whitelisted(self):
        """带活实例的那些必须是白名单的子集（模块换了，但实例在会话里 ⇒ 单列出来点名）。"""
        self.assertTrue(set(hot_reload.LIVE_INSTANCE_MODULES) <= set(hot_reload.HOT_MODULES))

    def test_needs_restart_names_blacklisted_changes(self):
        """注入假数据：黑名单里的已改模块必须点名；纯函数模块不该被误报。"""
        rows = hot_reload.restart_report(["backend/agent/session.py", "agent.memory",
                                          "agent.farm", "agent.planner", "agent/composites.py"])
        names = [row["module"] for row in rows]
        self.assertIn("agent.session", names)
        self.assertIn("agent.memory", names)
        self.assertNotIn("agent.farm", names)
        self.assertNotIn("agent.planner", names)
        self.assertNotIn("agent.composites", names)
        self.assertTrue(all(row["reason"] for row in rows))

    def test_needs_restart_names_live_instance_modules(self):
        rows = hot_reload.restart_report(["agent/skills.py", "agent/modes_engine.py"])
        self.assertEqual([row["module"] for row in rows],
                         ["agent.modes_engine", "agent.skills"])
        for row in rows:
            self.assertIn("活实例", row["reason"])

    def test_unknown_modules_go_to_needs_restart(self):
        """拿不准就不能热载：白名单之外的模块（含 api / 新增模块）一律点名要重启。"""
        rows = hot_reload.restart_report(["agent/brand_new.py", "api/whatever.py", "backend/server.py"])
        self.assertEqual([row["module"] for row in rows],
                         ["agent.brand_new", "api.whatever", "server"])
        self.assertEqual(rows[0]["reason"], hot_reload.UNKNOWN_RESTART_REASON)
        self.assertEqual(rows[1]["reason"], hot_reload.UNKNOWN_RESTART_REASON)
        self.assertEqual(rows[2]["reason"], hot_reload.RESTART_MODULES["server"])


# ── 真热载：临时模块 ─────────────────────────────────────────────────────────


class _TempModuleCase(unittest.TestCase):
    NAMES = ("hr_probe", "hr_probe2", "hr_bad", "hr_never_imported")

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self._stamp = time.time() + 1000.0
        sys.path.insert(0, str(self.root))
        for name in self.NAMES:
            sys.modules.pop(name, None)

    def tearDown(self):
        for name in self.NAMES:
            sys.modules.pop(name, None)
        try:
            sys.path.remove(str(self.root))
        except ValueError:
            pass

    def write(self, name, body):
        """写临时模块，并把 mtime **每次严格推后**（否则同一秒内的两次写会让 CPython 复用旧 `.pyc`）。"""
        path = self.root / f"{name}.py"
        path.write_text(body, encoding="utf-8")
        self._stamp += 10.0
        os.utime(path, (self._stamp, self._stamp))
        return path


class ReloadAgentModulesTests(_TempModuleCase):
    def test_real_reload_picks_up_the_new_code(self):
        """真的能 `importlib.reload` 一个被改过的模块：新值立即生效。"""
        self.write("hr_probe", "VALUE = 1\n")
        importlib.import_module("hr_probe")
        self.write("hr_probe", "VALUE = 2\n")
        report = hot_reload.reload_agent_modules(changed=["hr_probe"], modules=["hr_probe"])
        self.assertEqual(report["reloaded"], ["hr_probe"])
        self.assertEqual(report["failed"], [])
        self.assertEqual(importlib.import_module("hr_probe").VALUE, 2)

    def test_one_broken_module_does_not_stop_the_rest(self):
        """坏模块（import 期就抛）记进 failed 并继续；其余照样重载。"""
        self.write("hr_bad", "VALUE = 0\n")
        self.write("hr_probe", "VALUE = 1\n")
        self.write("hr_probe2", "VALUE = 10\n")
        for name in ("hr_bad", "hr_probe", "hr_probe2"):
            importlib.import_module(name)
        self.write("hr_bad", 'raise RuntimeError("boom")\n')
        self.write("hr_probe", "VALUE = 7\n")
        with self.assertLogs("hot_reload", level="WARNING"):
            report = hot_reload.reload_agent_modules(modules=["hr_bad", "hr_probe", "hr_probe2"])
        self.assertEqual(report["reloaded"], ["hr_probe", "hr_probe2"])
        self.assertEqual([row["module"] for row in report["failed"]], ["hr_bad"])
        self.assertIn("boom", report["failed"][0]["error"])
        self.assertEqual(importlib.import_module("hr_probe").VALUE, 7)
        self.assertEqual(importlib.import_module("hr_probe2").VALUE, 10)

    def test_not_imported_modules_are_reported_not_reloaded(self):
        report = hot_reload.reload_agent_modules(modules=["hr_never_imported"])
        self.assertEqual(report["reloaded"], [])
        self.assertEqual(report["not_imported"], ["hr_never_imported"])

    def test_blacklisted_modules_never_end_up_in_reloaded(self):
        """真跑一次默认白名单：`reloaded` 只可能是白名单模块，黑名单只出现在 needs_restart。"""
        report = hot_reload.reload_agent_modules(changed=["agent/session.py", "agent/planner.py"])
        self.assertTrue(set(report["reloaded"]) <= set(hot_reload.HOT_MODULES))
        self.assertFalse(set(report["reloaded"]) & set(hot_reload.RESTART_MODULES))
        self.assertNotIn("agent.session", report["reloaded"])
        self.assertIn("agent.session", [row["module"] for row in report["needs_restart"]])

    def test_auto_changed_detection_uses_the_process_baseline(self):
        """`changed=None` ⇒ 与**进程启动基线**比对（注入假基线，别依赖真实文件此刻的 mtime）。"""
        farm = str(hot_reload.BACKEND_DIR / "agent" / "farm.py")
        fake = hot_reload.watch_snapshot(hot_reload.source_paths())
        self.assertIn(farm, fake)
        fake[farm] = {"mtime": 0.0, "size": 0}       # 与真实 mtime 必然对不上 ⇒ 算"改过"
        with mock.patch.object(hot_reload, "baseline_snapshot", lambda: dict(fake)):
            report = hot_reload.reload_agent_modules(modules=["agent.farm"])
        self.assertIn("agent.farm", report["changed"])
        self.assertEqual(report["needs_restart"], hot_reload.restart_report(report["changed"]))
        self.assertEqual(report["reloaded"], ["agent.farm"])


class RebuildSessionPlannerTests(unittest.TestCase):
    def _planner(self):
        planner_class = importlib.import_module("agent.planner").Planner
        return planner_class(None, None, None)

    def test_rebuild_uses_the_existing_pieces_and_keeps_state(self):
        old = self._planner()
        old._craft_need = {"iron_ingot": 3}          # 运行期状态：热载不该把它丢了
        session = SimpleNamespace(planner=old)
        report = hot_reload.rebuild_session_planner(session)
        self.assertTrue(report["ok"], report)
        self.assertIsNot(session.planner, old)
        self.assertIs(session.planner.llm, old.llm)
        self.assertIs(session.planner.skills, old.skills)
        self.assertIs(session.planner.borrow_ledger, old.borrow_ledger)
        self.assertIs(session.planner.action_manager, old.action_manager)
        self.assertEqual(session.planner._craft_need, {"iron_ingot": 3})
        self.assertIn("llm_service", report["wiring"])

    def test_missing_planner_is_reported_not_raised(self):
        report = hot_reload.rebuild_session_planner(SimpleNamespace())
        self.assertFalse(report["ok"])
        self.assertTrue(report["reason"])

    def test_wiring_names_still_exist_on_the_real_planner(self):
        """重建靠"旧实例上的那几个属性名"：planner 一旦改构造 / 改字段名，这里先炸。"""
        planner = self._planner()
        missing = [attr for _, attr in hot_reload._PLANNER_WIRING if not hasattr(planner, attr)]
        self.assertEqual(missing, [], f"`Planner` 少了热载重建要读的属性：{missing}")
        args = [arg for arg, _ in hot_reload._PLANNER_WIRING]
        self.assertEqual(
            [name for name in args
             if name not in inspect.signature(type(planner).__init__).parameters], [],
            "`Planner.__init__` 少了热载重建要传的形参")


# ── 接线：忙 ⇒ 拒绝；空闲 ⇒ 出报告；看门狗 ──────────────────────────────────


class _ReloadGatewayTests(unittest.TestCase):
    """走**真实 app**（`server.app` + `TestClient`，不进入 lifespan）。"""

    def setUp(self):
        self._saved_orchestrator = server.orchestrator
        self._saved_perform = server.perform_hot_reload
        server.orchestrator = None
        self.client = TestClient(server.app)
        self.calls = []

    def tearDown(self):
        server.orchestrator = self._saved_orchestrator
        server.perform_hot_reload = self._saved_perform
        watcher = server._hot_reload_watcher
        if watcher is not None:
            watcher.stop()

    def install(self, *, jobs=(), task_state=None, composite_runs=()):
        session = SimpleNamespace(
            jobs=SimpleNamespace(active=lambda: list(jobs)),
            runtime={"task_state": task_state} if task_state is not None else {},
            planner=SimpleNamespace(_composite_runs=dict.fromkeys(composite_runs, object())),
        )
        server.orchestrator = SimpleNamespace(session=session)
        return session

    def spy_reload(self, report=None):
        def _spy(changed=None):
            self.calls.append(changed)
            return report if report is not None else {
                "reloaded": ["agent.farm"], "failed": [], "needs_restart": [],
                "changed": ["agent.farm"], "not_imported": [],
            }
        server.perform_hot_reload = _spy
        return _spy

    # ── 忙 ──

    def test_busy_job_rejects_and_never_reloads(self):
        self.install(jobs=[SimpleNamespace(id="job_7", tool="collect")])
        self.spy_reload()
        resp = self.client.post("/api/reload")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertIs(body["ok"], False)
        self.assertEqual(body["reason"], "busy")
        self.assertIn("job_7", body["busy"])
        self.assertIn("collect", body["busy"])
        self.assertEqual(self.calls, [], "忙时**一次**热载都不许发生（绝不热一半）")

    def test_busy_task_state_rejects(self):
        self.install(task_state={"kind": "collect", "status": "running"})
        self.spy_reload()
        body = self.client.post("/api/reload").json()
        self.assertIs(body["ok"], False)
        self.assertEqual(body["reason"], "busy")
        self.assertIn("running", body["busy"])
        self.assertEqual(self.calls, [])

    def test_busy_pending_composite_rejects(self):
        self.install(composite_runs=["job_a", "job_b"])
        self.spy_reload()
        body = self.client.post("/api/reload").json()
        self.assertIs(body["ok"], False)
        self.assertEqual(body["reason"], "busy")
        self.assertIn("2", body["busy"])
        self.assertEqual(self.calls, [])

    def test_terminal_task_state_is_idle(self):
        self.install(task_state={"kind": "collect", "status": "done"})
        self.spy_reload()
        body = self.client.post("/api/reload").json()
        self.assertIs(body["ok"], True)

    # ── 空闲 ──

    def test_idle_reloads_and_reports(self):
        self.install()
        self.spy_reload(report={
            "reloaded": ["agent.planner"],
            "failed": [{"module": "agent.farm", "error": "SyntaxError: boom"}],
            "needs_restart": [{"module": "agent.session", "reason": "持有活状态"}],
            "changed": ["agent/session.py"], "not_imported": [],
        })
        body = self.client.post("/api/reload").json()
        self.assertIs(body["ok"], True)
        self.assertEqual(self.calls, [None])
        self.assertEqual(body["reloaded"], ["agent.planner"])
        self.assertEqual(body["failed"][0]["module"], "agent.farm")
        self.assertEqual(body["needs_restart"][0]["module"], "agent.session")
        self.assertIn("agent.session", body["note"])

    def test_idle_without_restart_requirements_says_so(self):
        self.install()
        self.spy_reload()
        body = self.client.post("/api/reload").json()
        self.assertIs(body["ok"], True)
        self.assertIn("没有需要重启", body["note"])
        self.assertIn("没重启", body["note"])

    # ── 忙判据本身 ──

    def test_busy_judgement_reads_existing_state_fields(self):
        self.install()
        self.assertIsNone(server.hot_reload_busy())
        self.install(jobs=[SimpleNamespace(id="j", tool="mine")])
        self.assertEqual(server.hot_reload_busy(), "job j(mine)")
        self.install(task_state={"kind": "haul", "status": "moving"})
        self.assertEqual(server.hot_reload_busy(), "task_state haul=moving")
        self.install(task_state={"status": "running"})
        self.assertIsNone(server.hot_reload_busy(), "kind=idle 不算在跑")

    def test_no_session_is_idle(self):
        server.orchestrator = None
        self.assertIsNone(server.hot_reload_busy())

    # ── 看门狗开关 ──

    def test_watch_toggle_requires_a_boolean(self):
        resp = self.client.post("/api/reload/watch", json={})
        self.assertEqual(resp.status_code, 400)
        resp = self.client.post("/api/reload/watch", json={"enabled": "yes"})
        self.assertEqual(resp.status_code, 400)

    def test_watch_toggle_starts_and_stops_the_watcher(self):
        self.install()
        body = self.client.post("/api/reload/watch", json={"enabled": True}).json()
        self.assertIs(body["ok"], True)
        self.assertIs(body["enabled"], True)
        self.assertTrue(server._hot_reload_watcher.running)
        body = self.client.post("/api/reload/watch", json={"enabled": False}).json()
        self.assertIs(body["ok"], True)
        self.assertIs(body["enabled"], False)
        self.assertFalse(server._hot_reload_watcher.running)


class WatcherTests(unittest.TestCase):
    """看门狗：注入假快照 + 假 busy ⇒ 忙时跳过（打日志）、空闲才热载、`stop()` 后线程结束。"""

    def setUp(self):
        self.files = {"/src/a.py": {"mtime": 1.0, "size": 10}}
        self.busy = None
        self.reports = []

    def snapshot(self, paths):
        return {path: dict(self.files.get(path, {})) for path in paths if path in self.files}

    def on_reload(self, paths):
        self.reports.append(list(paths))
        return {"reloaded": ["agent.farm"], "failed": [], "needs_restart": []}

    def watcher(self, interval=0.05):
        return hot_reload.HotReloadWatcher(["/src/a.py"], busy=lambda: self.busy,
                                          on_reload=self.on_reload, interval=interval,
                                          snapshot=self.snapshot)

    def test_no_change_returns_none(self):
        watcher = self.watcher()
        self.assertIsNone(watcher.poll_once())
        self.assertEqual(self.reports, [])

    def test_busy_skips_logs_and_keeps_the_change_pending(self):
        watcher = self.watcher()
        self.files["/src/a.py"] = {"mtime": 2.0, "size": 11}     # 有变化
        self.busy = "job job_7(collect)"
        with self.assertLogs("hot_reload", level="INFO") as logs:
            result = watcher.poll_once()
        self.assertEqual(result["action"], "skipped")
        self.assertIn("job_7", result["busy"])
        self.assertEqual(self.reports, [], "忙 ⇒ 一次都不许热载")
        self.assertTrue(any("跳过" in line and "job_7" in line for line in logs.output),
                        logs.output)
        self.assertEqual(watcher.status()["pending"], ["/src/a.py"], "变化要挂起，不许排队堆叠")

    def test_idle_after_busy_reloads_the_pending_change_once(self):
        watcher = self.watcher()
        self.files["/src/a.py"] = {"mtime": 2.0, "size": 11}
        self.busy = "task_state collect=running"
        watcher.poll_once()
        self.assertEqual(self.reports, [])
        self.busy = None                                          # 闲了
        with self.assertLogs("hot_reload", level="INFO") as logs:
            result = watcher.poll_once()
        self.assertEqual(result["action"], "reloaded")
        self.assertEqual(result["paths"], ["/src/a.py"])
        self.assertEqual(self.reports, [["/src/a.py"]])
        self.assertEqual(result["reloaded"], ["agent.farm"])
        self.assertTrue(any("热载" in line for line in logs.output), logs.output)
        self.assertEqual(watcher.status()["pending"], [])
        self.assertIsNone(watcher.poll_once(), "已热载过 ⇒ 同样的快照不再重复热载")
        self.assertEqual(len(self.reports), 1)

    def test_reload_failure_does_not_kill_the_watcher(self):
        def boom(paths):
            raise RuntimeError("on_reload 炸了")
        watcher = hot_reload.HotReloadWatcher(["/src/a.py"], busy=lambda: None,
                                             on_reload=boom, interval=0.05,
                                             snapshot=self.snapshot)
        self.files["/src/a.py"] = {"mtime": 9.0, "size": 1}
        with self.assertLogs("hot_reload", level="WARNING"):
            result = watcher.poll_once()
        self.assertEqual(result["failed"][0]["module"], "(on_reload)")

    def test_start_and_stop_end_the_thread(self):
        watcher = self.watcher()
        self.assertFalse(watcher.running)
        self.assertTrue(watcher.start())
        self.assertTrue(watcher.running)
        self.assertFalse(watcher.start(), "已在跑 ⇒ 不重复起线程")
        self.assertTrue(watcher.stop())
        self.assertFalse(watcher.running)
        self.assertIsNone(watcher._thread)
        self.assertFalse(watcher.stop(), "已经停了 ⇒ 不算“停掉了一个在跑的”")


class BusyTextTests(unittest.TestCase):
    def test_idle_values_are_empty(self):
        for value in (None, False, "", [], {}, ()):
            self.assertEqual(hot_reload.busy_text(value), "")

    def test_descriptions_are_readable(self):
        self.assertEqual(hot_reload.busy_text("  job j1  "), "job j1")
        self.assertEqual(hot_reload.busy_text(["job j1", "task_state collect=running"]),
                         "job j1、task_state collect=running")
        self.assertEqual(hot_reload.busy_text({"jobs": 1}), "jobs=1")


if __name__ == "__main__":
    unittest.main()
