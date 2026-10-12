"""HTTP 面回捞（`api/http_surface.py`）的接口测试。

走**真实 app**（`server.app` + `TestClient`，不进入 lifespan），断言三件事：

1. 形状/状态码（与 `backend/sdk/**` 对齐）；
2. **真数据**：注入一个已知状态（真 `Skills` + 记录型 wire、真 `ControlLease`、
   真 `AuditLog`），断言响应里真出现它；
3. **无数据时的诚实行为**（503/403/422/空列表 + `available=False`），而不是编造。

落盘一律指到临时目录，绝不碰仓库里的 `.local/` 与 `data/`。
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

import server  # noqa: E402
from agent.audit_log import AuditLog  # noqa: E402
from agent.control_lease import (HOLDER_LEFT_LOCAL, HOLDER_REFLEX_EAT,  # noqa: E402
                                 REASON_SURVIVAL_EAT, ControlLease)
from agent.skills import Skills  # noqa: E402
from api import http_surface  # noqa: E402


class _RecordingWire:
    """真 `WireClient` 的替身：接口面一致（`.connected` / `.send_command`），只记录命令。"""

    def __init__(self, connected=True):
        self.connected = connected
        self.commands = []
        self._n = 0

    def send_command(self, cmd, args=None):
        self._n += 1
        req_id = f"req_{self._n}"
        self.commands.append({"cmd": cmd, "args": dict(args or {}), "id": req_id})
        return req_id


class _ChatSession:
    """只提供聊天链路那一件事：把 (sender, message) 如实回显（**不跑 LLM**）。"""

    def __init__(self, reply="同伴的回话"):
        self.reply = reply
        self.calls = []

    def handle_chat(self, sender, message):
        self.calls.append((sender, message))
        if self.reply is None:
            return None  # 与真 `Session.handle_chat` 一样：不理人时返回 None
        return f"{self.reply}|{sender}|{message}"


class _SurfaceTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self._saved_orchestrator = server.orchestrator
        self._saved_wire = server.wire
        self._saved_audit_path = http_surface.AUDIT_PATH
        server.orchestrator = None
        server.wire = None
        http_surface.AUDIT_PATH = os.path.join(self.tmp, "security.jsonl")
        self.client = TestClient(server.app)

    def tearDown(self):
        server.orchestrator = self._saved_orchestrator
        server.wire = self._saved_wire
        http_surface.AUDIT_PATH = self._saved_audit_path
        self._tmp.cleanup()

    # ── 注入用的真对象 ──

    def install_session(self, **overrides):
        """挂一个"有真子对象"的会话（`skills` / `planner.control_lease` 都是真家伙）。"""
        session = SimpleNamespace(
            skills=Skills(None),
            planner=SimpleNamespace(control_lease=ControlLease()),
            runtime={},
        )
        for key, value in overrides.items():
            setattr(session, key, value)
        server.orchestrator = SimpleNamespace(session=session)
        return session

    def install_chat(self, reply="同伴的回话"):
        chat = _ChatSession(reply=reply)
        server.orchestrator = SimpleNamespace(
            session=chat,
            handle_chat=lambda sender, message: chat.handle_chat(sender, message),
        )
        return chat


# ── GET /api/sdk/info ────────────────────────────────────────────────────────


class SdkInfoTests(_SurfaceTestCase):
    def test_reports_version_and_auth_truthfully(self):
        resp = self.client.get("/api/sdk/info")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["api_version"], "1")
        self.assertIs(body["auth_required"], False, "26.3 没有 SDK token/鉴权中间件")
        self.assertEqual(body["supported_versions"], ["1"], "V2 面只回了两件，报 2 会夸大")

    def test_interfaces_are_derived_from_the_real_route_table(self):
        body = self.client.get("/api/sdk/info").json()
        self.assertEqual(sorted(body["interfaces"]), ["actuator", "gateway", "observer"])

    def test_endpoint_lists_match_the_real_route_table(self):
        body = self.client.get("/api/sdk/info").json()
        implemented = set(body["sdk_endpoints"]["implemented"])
        missing = set(body["sdk_endpoints"]["missing"])
        self.assertFalse(implemented & missing)
        self.assertEqual(len(implemented) + len(missing), len(http_surface.SDK_ENDPOINTS))
        for line in ("GET /api/sdk/info", "POST /api/sdk/chat", "POST /api/sdk/command",
                     "GET /api/v2/control", "GET /api/config"):
            self.assertIn(line, implemented)
        for line in ("GET /api/sdk/identity", "GET /api/v2/skills", "POST /api/v2/control/leases",
                     "GET /api/v2/runs", "GET /api/v2/schedules"):
            self.assertIn(line, missing)

    def test_claimed_get_endpoints_really_serve(self):
        """报"已实现"的 GET 不许是 404（防自省表与路由漂移）。"""
        body = self.client.get("/api/sdk/info").json()
        implemented = set(body["sdk_endpoints"]["implemented"])
        for line in ("GET /api/status", "GET /api/session", "GET /api/memory",
                     "GET /api/config", "GET /api/llm/providers", "GET /api/sdk/context",
                     "GET /api/v2/control"):
            self.assertIn(line, implemented)
            path = line.split(" ", 1)[1]
            self.assertNotEqual(self.client.get(path).status_code, 404, path)
        # `/api/v2/audit` 不在 SDK 会打的清单里（SDK 没有 audit 方法），所以它不参与
        # implemented/missing 的比对，但同样必须是真在服务的路由。
        self.assertNotEqual(self.client.get("/api/v2/audit").status_code, 404)


# ── POST /api/sdk/chat ───────────────────────────────────────────────────────


class SdkChatTests(_SurfaceTestCase):
    def test_passes_through_to_the_chat_pipeline(self):
        chat = self.install_chat(reply="在，一起去挖铁")
        resp = self.client.post("/api/sdk/chat", json={"message": "去挖铁", "sender": "launcher"})
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["status"], "ok")
        self.assertIn("在，一起去挖铁", body["response"])
        self.assertEqual(chat.calls, [("launcher", "去挖铁")])

    def test_silence_is_reported_as_an_empty_reply(self):
        self.install_chat(reply=None)
        body = self.client.post("/api/sdk/chat", json={"message": "在吗"}).json()
        self.assertEqual(body, {"status": "ok", "response": ""})

    def test_sender_defaults_to_sdk(self):
        chat = self.install_chat()
        self.client.post("/api/sdk/chat", json={"message": "hi"})
        self.assertEqual(chat.calls, [("sdk", "hi")])

    def test_empty_message_is_422(self):
        self.install_chat()
        self.assertEqual(self.client.post("/api/sdk/chat", json={"message": ""}).status_code, 422)

    def test_without_a_session_it_is_503(self):
        resp = self.client.post("/api/sdk/chat", json={"message": "hi"})
        self.assertEqual(resp.status_code, 503)
        self.assertIn("not running", resp.json()["detail"])


# ── POST /api/sdk/command ────────────────────────────────────────────────────


class SdkCommandTests(_SurfaceTestCase):
    def setUp(self):
        super().setUp()
        self.wire = _RecordingWire()
        self.skills = Skills(self.wire)
        self.install_session(skills=self.skills)
        server.wire = self.wire

    def test_command_reaches_the_wire_and_returns_the_real_request_id(self):
        resp = self.client.post("/api/sdk/command", json={"command": "get_state"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"status": "accepted", "command": "get_state",
                                       "request_id": "req_1"})
        self.assertEqual(self.wire.commands,
                         [{"cmd": "get_state", "args": {}, "id": "req_1"}])

    def test_args_are_forwarded_verbatim(self):
        resp = self.client.post("/api/sdk/command",
                                json={"command": "move_to", "args": {"x": 1.5, "z": -2}})
        self.assertEqual(resp.json()["request_id"], "req_1")
        self.assertEqual(self.wire.commands[0]["cmd"], "move_to")
        self.assertEqual(self.wire.commands[0]["args"], {"x": 1.5, "z": -2})

    def test_raw_command_is_tagged_as_sdk_in_the_session_audit(self):
        seen = []
        self.skills.set_command_observer(
            lambda cmd, req_id, context, args: seen.append((cmd, req_id, context, args)))
        self.client.post("/api/sdk/command", json={"command": "jump"})
        self.assertEqual(seen, [("jump", "req_1", "sdk", {})])

    def test_reserved_control_commands_are_refused_and_nothing_is_sent(self):
        for command in sorted(http_surface.RESERVED_BODY_COMMANDS):
            resp = self.client.post("/api/sdk/command", json={"command": command})
            self.assertEqual(resp.status_code, 403, command)
        self.assertEqual(self.wire.commands, [])

    def test_reserved_control_arguments_are_refused(self):
        resp = self.client.post("/api/sdk/command",
                                json={"command": "move_to", "args": {"__lcu_fencing_token": 7}})
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(self.wire.commands, [])

    def test_disconnected_body_is_503(self):
        self.wire.connected = False
        resp = self.client.post("/api/sdk/command", json={"command": "get_state"})
        self.assertEqual(resp.status_code, 503)
        self.assertIn("not connected", resp.json()["detail"])
        self.assertEqual(self.wire.commands, [])

    def test_missing_channel_is_503(self):
        session = self.install_session(skills=None)
        resp = self.client.post("/api/sdk/command", json={"command": "get_state"})
        self.assertEqual(resp.status_code, 503)
        self.assertIsNone(session.skills)
        self.assertEqual(self.wire.commands, [])

    def test_bad_payloads_are_422(self):
        for payload in ({"command": ""}, {"command": "   "}, {},
                        {"command": "move_to", "args": "x"}):
            self.assertEqual(self.client.post("/api/sdk/command", json=payload).status_code, 422,
                             payload)
        self.assertEqual(self.wire.commands, [])

    def test_without_a_session_it_is_503(self):
        server.orchestrator = None
        resp = self.client.post("/api/sdk/command", json={"command": "get_state"})
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(self.wire.commands, [])


# ── GET /api/v2/control ──────────────────────────────────────────────────────


class V2ControlTests(_SurfaceTestCase):
    def test_no_session_is_honest_about_having_no_lease(self):
        body = self.client.get("/api/v2/control").json()
        self.assertEqual(body, {"mode": "builtin", "lease": None, "coordinator": None})

    def test_reports_the_live_control_lease(self):
        lease = ControlLease()
        self.install_session(planner=SimpleNamespace(control_lease=lease))
        lease.acquire(HOLDER_LEFT_LOCAL, "本地闭环接管", ttl=30.0)
        lease.preempt(HOLDER_REFLEX_EAT, REASON_SURVIVAL_EAT, ttl=8.0)
        body = self.client.get("/api/v2/control").json()
        self.assertEqual(body["mode"], "builtin")
        self.assertEqual(body["coordinator"], None)
        self.assertEqual(body["lease"]["holder"], HOLDER_REFLEX_EAT)
        self.assertEqual(body["lease"]["reason"], REASON_SURVIVAL_EAT)
        self.assertEqual(body["lease"]["events"], 2, "获取 + 抢占都留了审计")
        self.assertIs(body["lease"]["capabilities"]["dual_brain_arbitration"], False)

    def test_release_is_visible_immediately(self):
        lease = ControlLease()
        self.install_session(planner=SimpleNamespace(control_lease=lease))
        lease.acquire(HOLDER_REFLEX_EAT, REASON_SURVIVAL_EAT, ttl=8.0)
        lease.release(HOLDER_REFLEX_EAT, "吃完了")
        body = self.client.get("/api/v2/control").json()
        self.assertIsNone(body["lease"]["holder"])
        self.assertEqual(body["lease"]["reason"], "")

    def test_reads_only_never_creates_a_lease(self):
        """只读端点：没有会话时也不许凭空造出租约对象。"""
        body = self.client.get("/api/v2/control").json()
        self.assertIsNone(body["lease"])
        self.assertEqual(self.client.get("/api/v2/control").status_code, 200)


# ── GET /api/v2/audit ────────────────────────────────────────────────────────


class V2AuditTests(_SurfaceTestCase):
    def _seed(self, path, rows):
        store = AuditLog(path)
        for category, action in rows:
            store.append(category=category, action=action, target="tp", actor="master",
                         outcome="succeeded", details={"note": action})
        return store

    def test_returns_real_events_newest_first(self):
        self._seed(http_surface.AUDIT_PATH, [("security", "command.allowed"),
                                             ("security", "command.blocked")])
        resp = self.client.get("/api/v2/audit")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertIs(body["available"], True)
        self.assertEqual(body["count"], 2)
        self.assertEqual(body["source"], http_surface.AUDIT_PATH)
        self.assertEqual([event["action"] for event in body["events"]],
                         ["command.blocked", "command.allowed"])
        self.assertEqual(set(body["events"][0]), {"id", "occurred_at", "category", "action",
                                                  "target", "actor", "outcome", "details"})

    def test_category_filter_is_applied_honestly(self):
        self._seed(http_surface.AUDIT_PATH, [("security", "command.allowed")])
        self.assertEqual(self.client.get("/api/v2/audit", params={"category": "security"})
                         .json()["count"], 1)
        other = self.client.get("/api/v2/audit", params={"category": "gui_skill"}).json()
        self.assertEqual(other["count"], 0)
        self.assertIs(other["available"], True, "审计在，只是没有这一类")

    def test_limit_is_respected_and_validated(self):
        self._seed(http_surface.AUDIT_PATH, [("security", f"a{i}") for i in range(3)])
        self.assertEqual(self.client.get("/api/v2/audit", params={"limit": 1}).json()["count"], 1)
        self.assertEqual(self.client.get("/api/v2/audit", params={"limit": 500}).status_code, 200)
        for bad in (0, 501):
            self.assertEqual(self.client.get("/api/v2/audit", params={"limit": bad}).status_code,
                             422, bad)

    def test_without_any_log_it_says_so_and_creates_nothing(self):
        self.assertFalse(Path(http_surface.AUDIT_PATH).exists())
        body = self.client.get("/api/v2/audit").json()
        self.assertEqual(body["events"], [])
        self.assertEqual(body["count"], 0)
        self.assertIs(body["available"], False)
        self.assertEqual(body["source"], http_surface.AUDIT_PATH)
        self.assertFalse(Path(http_surface.AUDIT_PATH).exists(), "只读端点不许建空文件")

    def test_prefers_the_live_session_handle(self):
        """运行期优先用 `Session` 那份句柄（同锁同路径），而不是另开一个。"""
        live_path = os.path.join(self.tmp, "live.jsonl")
        self._seed(live_path, [("security", "live.event")])
        self._seed(http_surface.AUDIT_PATH, [("security", "file.event")])
        self.install_session(_security_log=AuditLog(live_path))
        body = self.client.get("/api/v2/audit").json()
        self.assertEqual([event["action"] for event in body["events"]], ["live.event"])
        self.assertEqual(body["source"], live_path)


# ── 附加式回归护栏 ───────────────────────────────────────────────────────────


class ExistingRoutesUntouchedTests(_SurfaceTestCase):
    def test_status_and_sdk_context_still_behave_as_before(self):
        status = self.client.get("/api/status").json()
        # `background_threads` 是 2026-10-12 新加的**观察点**（关停自检口径）：会话没起来时
        # 应该是空表；其余两个键与以前逐字相同。
        self.assertEqual(status, {"running": False, "mod_connected": False,
                                  "background_threads": []})
        context = self.client.get("/api/sdk/context")
        self.assertEqual(context.status_code, 200)
        self.assertEqual(set(context.json()), {"persona", "integration"})

    def test_new_routes_do_not_shadow_the_pre_existing_sdk_prefix(self):
        registered = {f"{method} {path}" for method, path in http_surface._registered_endpoints()}
        for line in ("GET /api/sdk/context", "GET /api/sdk/info", "POST /api/sdk/chat",
                     "POST /api/sdk/command"):
            self.assertIn(line, registered)


if __name__ == "__main__":
    unittest.main()
