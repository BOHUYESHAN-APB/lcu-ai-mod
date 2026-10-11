"""M3-19 的 REST 面：权限 / 人设 / 任务列表 / 在场状态。

用 `TestClient` 直接打接口（**不进入 lifespan**，所以不会起后台重连线程、不连模组）。
人设/权限的落盘路径在 setUp 里被指到临时目录，绝不碰仓库里的 `.local/`。
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
from agent.permissions import PermissionsStore  # noqa: E402
from agent.persona_store import PersonaStore  # noqa: E402


class ConversationApiTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        # 进程级兜底实例指到临时目录：没有 session 时接口也能用，且不污染仓库
        self._saved_persona = server._persona_fallback
        self._saved_permissions = server._permissions_fallback
        self._saved_orchestrator = server.orchestrator
        server._persona_fallback = PersonaStore(path=os.path.join(self.tmp, "persona.json"))
        server._permissions_fallback = PermissionsStore(
            path=os.path.join(self.tmp, "permissions.json"))
        server.orchestrator = None
        self.seen = []
        self.client = TestClient(server.app)

    def tearDown(self):
        server._persona_fallback = self._saved_persona
        server._permissions_fallback = self._saved_permissions
        server.orchestrator = self._saved_orchestrator
        self._tmp.cleanup()

    # ── 权限 ──

    def test_permissions_round_trip(self):
        resp = self.client.get("/api/permissions")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["server_mode"], "private")

        posted = self.client.post("/api/permissions", json={
            "server_mode": "public",
            "master": ["Owner"],
            "friends": ["Buddy", "Carol"],
            "public_reply_guests": False,
            "session_adjust": 0.5,
        })
        self.assertEqual(posted.status_code, 200)

        again = self.client.get("/api/permissions").json()
        self.assertEqual(again["server_mode"], "public")
        self.assertEqual(again["master"], ["Owner"])
        self.assertEqual(again["friends"], ["Buddy", "Carol"])
        self.assertEqual(again["session_adjust"], 0.5)
        # 频率参数是可见可调的（公式的输入）
        for key in ("base_rates", "cooldown_seconds", "debounce_seconds", "window_seconds"):
            self.assertIn(key, again)

    # ── 人设 ──

    def test_persona_sections_round_trip(self):
        initial = self.client.get("/api/persona").json()
        self.assertTrue(initial["identity"])
        self.assertTrue(initial["scene_rules"])
        self.assertIn("sections", initial)

        posted = self.client.post("/api/persona", json={
            "name": "小A",
            "wake_names": ["小A", "A"],
            "identity": "你是一个安静但靠谱的搭档。",
            "reply_style": "句子短，口语化。",
            "multiple_reply_style": ["偶尔损一点"],
            "multiple_probability": 0.3,
            "scene_rules": "- 私聊：随便聊\n- 公开服：少说话\n- 被点名：先应一声",
        })
        self.assertEqual(posted.status_code, 200)

        persona = self.client.get("/api/persona").json()
        self.assertEqual(persona["name"], "小A")
        self.assertEqual(persona["identity"], "你是一个安静但靠谱的搭档。")
        self.assertEqual(persona["multiple_reply_style"], ["偶尔损一点"])
        self.assertAlmostEqual(persona["multiple_probability"], 0.3)
        self.assertIn("被点名", persona["scene_rules"])

    def test_persona_defaults_have_no_vendor_or_character_ip(self):
        persona = self.client.get("/api/persona").json()
        blob = persona["identity"] + persona["reply_style"] + persona["scene_rules"]

        for banned in ("OpenAI", "DeepSeek", "小米", "MiMo", "MaiBot", "女仆"):
            self.assertNotIn(banned, blob)
        self.assertTrue(persona["identity"].startswith("你"))

    # ── 任务列表 ──

    def test_task_endpoints_without_a_session_report_clearly(self):
        self.assertEqual(self.client.get("/api/tasks").json()["items"], [])
        for endpoint in ("/api/tasks/cancel", "/api/tasks/activate"):
            resp = self.client.post(endpoint, json={"id": "task_1"})
            self.assertEqual(resp.status_code, 409)

    def test_task_endpoints_drive_the_session_task_list(self):
        from agent.task_list import TaskList

        tasks = TaskList()
        item = tasks.add("collect", owner="Owner", owner_tier="master", status="active")
        server.orchestrator = SimpleNamespace(
            session=SimpleNamespace(tasks=tasks, cancel_task=self._cancel, activate_task=self._activate))

        listed = self.client.get("/api/tasks").json()
        self.assertEqual(listed["count"], 1)
        self.assertEqual(listed["items"][0]["owner"], "Owner")

        resp = self.client.post("/api/tasks/cancel", json={"id": item.id})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "ok")
        self.assertEqual(self.seen, [("cancel", item.id)])

        resp = self.client.post("/api/tasks/activate", json={"id": item.id})
        self.assertEqual(resp.json()["status"], "ok")
        self.assertEqual(self.seen[-1], ("activate", item.id))

    def _cancel(self, task_id, *, actor="", tier=None):
        self.seen.append(("cancel", task_id))
        return True, "cancelled"

    def _activate(self, task_id, *, actor="", tier=None):
        self.seen.append(("activate", task_id))
        return True, "activated"

    def test_status_without_a_session_still_reports_mod_connection(self):
        data = self.client.get("/api/status").json()

        self.assertFalse(data["running"])
        self.assertFalse(data["mod_connected"])


if __name__ == "__main__":
    unittest.main()
