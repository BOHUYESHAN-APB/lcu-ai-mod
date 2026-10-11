"""Web 面板这一批的验收：微信观感的私信页、补齐的面板、只读目录与治理审计、job 取消。

用 `TestClient` 直接打接口（**不进入 lifespan**，不起后台线程、不连模组）；落盘路径一律指到
临时目录，绝不碰仓库里的 `.local/` 与 `data/`。所有断言都对着**已有的事实来源**（注册表 /
内存环形缓冲 / job 自己的取消路径），不假设第二份清单。
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import server  # noqa: E402
from agent.composites import COMPOSITES  # noqa: E402
from agent.config_store import ConfigStore  # noqa: E402
from agent.control_lease import (AUDIT_MAX as CONTROL_LEASE_RING, HOLDER_LEFT_LOCAL,
                                 HOLDER_REFLEX_COMBAT, REASON_SURVIVAL_COMBAT,
                                 ControlLease)  # noqa: E402
from agent.job_registry import AUDIT_MAX as JOB_RING, JobRegistry  # noqa: E402
from agent.resource_lease import (AUDIT_MAX as RESOURCE_LEASE_RING, PRIORITY_DEFAULT,
                                  RESOURCE_MOVE, ResourceLease)  # noqa: E402
from agent.state_governor import GOV_AUDIT_MAX as GOVERNOR_RING, StateGovernor  # noqa: E402
from agent.tool_registry import TOOL_SPECS  # noqa: E402


class _FakePlanner:
    def __init__(self, control_lease):
        self.control_lease = control_lease


class _FakeSession:
    """只挂上被测端点需要的东西（够用即可，不模拟整个 Session）。"""

    def __init__(self, **parts):
        for key, value in parts.items():
            setattr(self, key, value)


class _FakeOrchestrator:
    def __init__(self, session):
        self.session = session


class _ServerCase(unittest.TestCase):
    """公共脚手架：不连模组、配置落临时目录、跑完还原全局。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._saved = (server.orchestrator, server.config_store, server.app_config)
        server.orchestrator = None
        server.config_store = ConfigStore(Path(self._tmp.name) / "config.json")
        server.app_config = server.config_store.raw(redact=True)
        self.client = TestClient(server.app)

    def tearDown(self):
        server.orchestrator, server.config_store, server.app_config = self._saved
        self._tmp.cleanup()


class ToolCatalogTests(_ServerCase):
    """⑤ 工具目录：单一事实来源＝注册表（不手写第二份清单）。"""

    def test_tools_come_straight_from_the_registry(self):
        body = self.client.get("/api/tools").json()

        self.assertEqual([t["name"] for t in body["tools"]], [s.name for s in TOOL_SPECS])
        self.assertEqual(body["count"], len(TOOL_SPECS))
        self.assertEqual(body["job_count"], sum(1 for s in TOOL_SPECS if s.job))
        self.assertIn("tool_registry", body["source"])

    def test_every_row_carries_name_description_args_permission_and_job(self):
        body = self.client.get("/api/tools").json()

        for row in body["tools"]:
            for key in ("name", "description", "args", "arg_names", "parameters", "groups",
                        "handler", "job", "permission"):
                self.assertIn(key, row, row.get("name"))
            self.assertEqual(row["parameters"]["type"], "object")
            self.assertIsInstance(row["job"], bool)
            self.assertEqual(set(row["permission"]["gates"]),
                             {"tier", "container", "read_only", "command", "borrow", "area"})

    def test_job_flag_matches_the_registry_declaration(self):
        by_name = {t["name"]: t for t in self.client.get("/api/tools").json()["tools"]}

        for spec in TOOL_SPECS:
            self.assertEqual(by_name[spec.name]["job"], bool(spec.job), spec.name)

    def test_composites_are_projected_from_the_declarations(self):
        body = self.client.get("/api/composites").json()

        self.assertTrue(body["available"])
        self.assertIn("composites", body["source"])
        self.assertEqual([c["name"] for c in body["composites"]], [c.name for c in COMPOSITES])
        registered = {spec.name for spec in TOOL_SPECS}
        for comp in body["composites"]:
            self.assertTrue(comp["steps"])
            for step in comp["steps"]:
                self.assertIn(step["tool"], registered)
                self.assertTrue(step["registered"])


class GovernanceAuditTests(_ServerCase):
    """④ 治理 / 租约审计：只读内存环形缓冲，不落盘、不假装有历史。"""

    def test_without_a_session_it_says_so_instead_of_inventing_history(self):
        body = self.client.get("/api/governance").json()

        self.assertFalse(body["running"])
        self.assertFalse(body["persisted"])
        self.assertIn("未启动", body["note"])
        for key in ("governor", "resource_lease", "control_lease", "jobs"):
            self.assertEqual(body[key], {})

    def _populated_session(self):
        governor = StateGovernor()
        governor.register("move_to", resources=(), kind="move", target="move_to",
                          reason="去挖矿")
        resource_lease = ResourceLease()
        resource_lease.request("llm:move_to", [RESOURCE_MOVE], priority=PRIORITY_DEFAULT,
                               reason="去挖矿")
        resource_lease.request("left:reflex-combat", [RESOURCE_MOVE], survival=True,
                               reason="被打了")
        control_lease = ControlLease()
        control_lease.acquire(HOLDER_LEFT_LOCAL, "本地闭环", actor="system")
        control_lease.preempt(HOLDER_REFLEX_COMBAT, REASON_SURVIVAL_COMBAT, ttl=6.0,
                              actor="modes_engine")
        jobs = JobRegistry()
        jobs.register("collect_blocks", progress="背包计数", terminal="背包满", failure="no_tool",
                      cancel=lambda reason="": None)
        return _FakeSession(state_governor=governor, resource_lease=resource_lease,
                            jobs=jobs, planner=_FakePlanner(control_lease))

    def test_it_reads_the_in_memory_rings_and_reports_the_capacity(self):
        server.orchestrator = _FakeOrchestrator(self._populated_session())

        body = self.client.get("/api/governance").json()

        self.assertTrue(body["running"])
        self.assertFalse(body["persisted"])
        self.assertIn("不落盘", body["note"])
        governor = body["governor"]
        self.assertEqual(governor["ring"], GOVERNOR_RING)
        self.assertIn("register", [e["kind"] for e in governor["audit"]])
        first = governor["audit"][0]
        for key in ("at", "key", "kind", "reason"):
            self.assertIn(key, first, "谁 / 何时 / 为何 都要有")
        self.assertEqual(body["resource_lease"]["ring"], RESOURCE_LEASE_RING)
        self.assertEqual(body["control_lease"]["ring"], CONTROL_LEASE_RING)
        self.assertEqual(body["jobs"]["ring"], JOB_RING)

    def test_resource_lease_audit_says_who_preempted_whose_what(self):
        server.orchestrator = _FakeOrchestrator(self._populated_session())

        audit = self.client.get("/api/governance").json()["resource_lease"]["audit"]

        preempts = [row for row in audit if row["kind"] == "preempt"]
        self.assertTrue(preempts, "生存反射抢资源要留审计")
        self.assertEqual(preempts[0]["owner"], "left:reflex-combat")
        self.assertEqual(preempts[0]["detail"]["victim"], "llm:move_to")
        self.assertIn(RESOURCE_MOVE, preempts[0]["resources"])

    def test_control_lease_audit_keeps_the_preempted_holder_and_reason(self):
        server.orchestrator = _FakeOrchestrator(self._populated_session())

        control = self.client.get("/api/governance").json()["control_lease"]

        self.assertEqual(control["holder"], HOLDER_REFLEX_COMBAT)
        self.assertIn("capabilities", control)
        preempt = [row for row in control["audit"] if row["kind"] == "preempt"][0]
        self.assertEqual(preempt["previous"], HOLDER_LEFT_LOCAL)
        self.assertEqual(preempt["reason"], REASON_SURVIVAL_COMBAT)
        self.assertEqual(preempt["ttl"], 6.0)

    def test_a_broken_handle_does_not_take_the_panel_down(self):
        class _Exploding:
            def snapshot(self):
                raise RuntimeError("boom")

            def events(self, n=0):
                raise RuntimeError("boom")

        server.orchestrator = _FakeOrchestrator(_FakeSession(state_governor=_Exploding()))

        resp = self.client.get("/api/governance")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["governor"], {"ring": GOVERNOR_RING, "audit": []})


class JobCancelTests(_ServerCase):
    """③ job 句柄的取消口：走 job **自己的**取消路径（不是前端假装已停）。"""

    def _registry_with_job(self, seen):
        registry = JobRegistry()
        job = registry.register("move_to", progress="位置", terminal="到点", failure="no_path",
                               cancel=lambda reason="": seen.append(reason))
        return registry, job

    def test_cancel_runs_the_jobs_own_cancel_path(self):
        seen = []
        registry, job = self._registry_with_job(seen)
        server.orchestrator = _FakeOrchestrator(_FakeSession(jobs=registry))

        body = self.client.post("/api/jobs/cancel",
                                json={"id": job.id, "reason": "操作台取消"}).json()

        self.assertEqual(body["status"], "ok")
        self.assertEqual(seen, ["操作台取消"])
        self.assertEqual(body["jobs"]["jobs"][0]["status"], "cancelled")

    def test_unknown_job_is_denied_not_silently_ok(self):
        seen = []
        registry, _job = self._registry_with_job(seen)
        server.orchestrator = _FakeOrchestrator(_FakeSession(jobs=registry))

        body = self.client.post("/api/jobs/cancel", json={"id": "nope"}).json()

        self.assertEqual(body["status"], "denied")
        self.assertEqual(body["reason"], "not_found")
        self.assertEqual(seen, [])

    def test_missing_id_is_a_400_and_no_session_is_a_409(self):
        seen = []
        registry, _job = self._registry_with_job(seen)
        server.orchestrator = _FakeOrchestrator(_FakeSession(jobs=registry))
        self.assertEqual(self.client.post("/api/jobs/cancel", json={}).status_code, 400)

        server.orchestrator = None
        self.assertEqual(self.client.post("/api/jobs/cancel", json={"id": "j1"}).status_code, 409)

    def test_the_jobs_list_itself_stays_read_only_in_status(self):
        seen = []
        registry, job = self._registry_with_job(seen)
        session = _FakeSession(jobs=registry)

        listed = session.jobs.snapshot()["jobs"][0]

        self.assertEqual(listed["id"], job.id)
        self.assertEqual(listed["tool"], "move_to")
        self.assertEqual(listed["status"], "running")
        self.assertTrue(listed["has_cancel_path"])


class RuntimeSwitchTests(_ServerCase):
    """② 基础设置页补的四个开关：键**早就存在**，这里只验证能读能写、且不互相抹掉。"""

    def test_the_four_keys_are_already_in_the_config_payload(self):
        cfg = self.client.get("/api/config").json()

        self.assertTrue(cfg["state_governor"]["enabled"])
        self.assertTrue(cfg["job_orchestration"])
        self.assertTrue(cfg["log_changes_only"])
        self.assertEqual(cfg["log_heartbeat_seconds"], 0)

    def test_saving_the_switches_round_trips_through_the_same_endpoint(self):
        cfg = self.client.get("/api/config").json()
        governor = dict(cfg["state_governor"])
        governor["enabled"] = False

        body = self.client.post("/api/config", json={
            "state_governor": governor, "job_orchestration": False,
            "log_changes_only": False, "log_heartbeat_seconds": 7,
        }).json()

        self.assertEqual(body["status"], "ok")
        self.assertFalse(body["config"]["state_governor"]["enabled"])
        self.assertFalse(body["config"]["job_orchestration"])
        self.assertFalse(body["config"]["log_changes_only"])
        self.assertEqual(body["config"]["log_heartbeat_seconds"], 7)
        again = self.client.get("/api/config").json()
        self.assertFalse(again["state_governor"]["enabled"])
        self.assertEqual(again["log_heartbeat_seconds"], 7)

    def test_the_uis_read_modify_write_keeps_the_other_thresholds(self):
        """前端口径：读整段 → 只改 enabled → 整段写回；自定义阈值必须原样还在。"""
        self.client.post("/api/config", json={"state_governor": {"enabled": True,
                                                                 "no_progress_seconds": 42}})
        cfg = self.client.get("/api/config").json()
        self.assertEqual(cfg["state_governor"]["no_progress_seconds"], 42)
        governor = dict(cfg["state_governor"])
        governor["enabled"] = False

        self.client.post("/api/config", json={"state_governor": governor})

        after = self.client.get("/api/config").json()["state_governor"]
        self.assertFalse(after["enabled"])
        self.assertEqual(after["no_progress_seconds"], 42)

    def test_a_negative_heartbeat_is_clamped_by_the_backend(self):
        body = self.client.post("/api/config", json={"log_heartbeat_seconds": -5}).json()

        self.assertEqual(body["config"]["log_heartbeat_seconds"], 0)


class WebPanelHtmlTests(_ServerCase):
    """① 微信观感的私信页 + 新面板确实挂上了真实端点（静态检查页面）。"""

    def setUp(self):
        super().setUp()
        self.index = self.client.get("/").text
        self.contacts = self.client.get("/contacts").text

    def test_new_endpoints_are_registered(self):
        paths = {getattr(route, "path", "") for route in server.app.routes}

        for path in ("/api/tools", "/api/composites", "/api/governance", "/api/jobs/cancel"):
            self.assertIn(path, paths)

    def test_index_has_the_new_panels(self):
        for marker in ('id="page-tools"', "showPage('tools'", "loadToolsPage", "loadCompositesList",
                       'id="govGovernor"', 'id="govResourceLease"', 'id="govControlLease"',
                       "loadGovernancePage", 'id="jobsList"', "loadJobsPanel", "cancelJob",
                       'id="cfgGovernorEnabled"', 'id="cfgJobOrchestration"',
                       'id="cfgLogChangesOnly"', 'id="cfgLogHeartbeat"', "saveRuntimeSwitches"):
            self.assertIn(marker, self.index, marker)

    def test_panels_name_their_data_source(self):
        for marker in ("/api/tools", "/api/composites", "/api/governance", "/api/jobs/cancel",
                       "/api/status", "/api/config"):
            self.assertIn(marker, self.index, marker)

    def test_index_never_reaches_for_the_master_inbox_or_placeholders(self):
        self.assertNotIn("/api/v2/", self.index)
        self.assertNotIn("待移植", self.index)

    def test_chat_tab_is_a_wechat_like_thread_without_fake_data(self):
        for marker in ("dmSameRun", "dmAvatarColor", "dmAvatarHtml", "dm-timesep", "dm-unread",
                       "dm-stack", "dm-bubble ${out"):
            self.assertIn(marker, self.index, marker)
        self.assertIn("还没有私下聊过的人", self.index, "空态要诚实")
        self.assertIn("代发", self.index, "代发必须标注清楚")
        self.assertIn("/api/contacts/", self.index)

    def test_contacts_page_is_the_same_wechat_like_thread(self):
        for marker in ("通讯录", 'id="contacts"', 'id="messages"', "selectContact",
                       "/api/contacts/", "initialTarget", "avatarHtml", "timesep",
                       ".bubble.tail", "代发", "还没有私下聊过的人"):
            self.assertIn(marker, self.contacts, marker)
        self.assertNotIn("/api/v2/", self.contacts)


if __name__ == "__main__":
    unittest.main()
