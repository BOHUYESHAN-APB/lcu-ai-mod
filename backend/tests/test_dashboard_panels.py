"""仪表盘各页签的落地面貌：真页面 / 没有 `(待移植)` 占位 / 数据源确实接上。

判定：静态解析 `web/static/index.html`（单页应用，用 showPage 切页签）——
1) 整份页面不许再出现 `待移植`（页签与占位卡片都不许留）；
2) 每个页签对应的 `page-*` 容器存在，且其内容引用了**已存在**的后端端点；
3) Bodies 页签已删除（背后没有真东西）；
4) 聊天页签接的是自建 `/api/contacts` 系列，而不是 master 的 `/api/v2/inbox/*`。
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import server  # noqa: E402
from agent.config_store import ConfigStore  # noqa: E402


class DashboardPanelTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._saved_store = server.config_store
        server.config_store = ConfigStore(Path(self._tmp.name) / "config.json")
        self.client = TestClient(server.app)
        self.html = self.client.get("/").text

    def tearDown(self):
        server.config_store = self._saved_store
        self._tmp.cleanup()

    def _function_body(self, name: str) -> str:
        start = self.html.index(f"function {name}(")
        end = self.html.index("\nfunction ", start + 1)
        return self.html[start:end]

    def test_no_migration_placeholder_survives(self):
        self.assertNotIn("待移植", self.html)
        self.assertNotIn("/api/v2/", self.html, "不该再引用 master 的 /api/v2 端点")

    def test_bodies_tab_and_page_are_gone(self):
        self.assertNotIn("page-bodies", self.html)
        self.assertNotIn("showPage('bodies'", self.html)

    def test_real_pages_exist_for_every_tab(self):
        for page in ("dashboard", "operations", "chat", "memory", "providers",
                     "access", "settings", "commands", "overlay", "diagnostics", "debug"):
            self.assertIn(f'id="page-{page}"', self.html, page)
            self.assertIn(f"showPage('{page}'", self.html, page)

    def test_chat_tab_is_a_real_two_pane_inbox_on_our_own_endpoints(self):
        self.assertIn('id="dmContacts"', self.html)
        self.assertIn('id="dmMessages"', self.html)
        self.assertIn('id="dmInput"', self.html)
        self.assertIn("function selectDmContact", self.html)
        self.assertIn("function sendDm", self.html)
        self.assertIn("/api/contacts/", self.html)
        # 靠左/靠右：in / out 两个方向都渲染。
        self.assertIn("dm-bubble ${out", self.html)
        # 空态诚实。
        self.assertIn("还没有私下聊过的人", self.html)

    def test_tab_loaders_wire_the_documented_endpoints(self):
        expectations = {
            "loadMemoryPanel": "/api/memory",
            "loadProvidersPage": "/api/llm/effective",
            "loadAccessPage": "/api/permissions",
            "loadOperationsPage": "/api/tasks",
            "loadDiagnosticsPage": "/api/status",
        }
        for fn, endpoint in expectations.items():
            self.assertIn(f"function {fn}", self.html)
            self.assertIn(endpoint, self.html)
        for name in expectations:
            self.assertIn(f"{name}()", self.html, f"showPage 必须挂上 {name}")

    def test_providers_page_never_echoes_api_keys(self):
        body = self._function_body("loadProvidersPage")
        # 只用 key_present 判断「已设置/未设置」，不渲染 key 值。
        self.assertIn("key_present", body)
        self.assertIn("已设置", body)
        self.assertNotIn(".api_key", body.replace(".api_key_required", ""))

    def test_access_page_does_not_judge_permissions_client_side(self):
        # 展示 tier/领地，写操作指回后端硬门（不在前端放行）。
        self.assertIn("前端不自行判定", self.html)
        self.assertIn("默认拒绝", self.html)
        self.assertIn("function loadAccessPage", self.html)

    def test_operations_actions_go_through_backend_gate(self):
        self.assertIn("/api/tasks/${action}", self.html)
        self.assertIn("被拒", self.html)


class DashboardEndpointSmokeTests(unittest.TestCase):
    """页签依赖的端点确实存在（用安全空值兜底，不 500）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._saved_store = server.config_store
        server.config_store = ConfigStore(Path(self._tmp.name) / "config.json")
        self.client = TestClient(server.app)

    def tearDown(self):
        server.config_store = self._saved_store
        self._tmp.cleanup()

    def test_panel_endpoints_answer_without_a_session(self):
        for path, key in (("/api/tasks", "items"), ("/api/memory", "interactions"),
                          ("/api/permissions", "server_mode"), ("/api/llm/providers", "providers"),
                          ("/api/llm/effective", "roles"), ("/api/status", "running"),
                          ("/api/contacts", "contacts")):
            resp = self.client.get(path)
            self.assertEqual(resp.status_code, 200, path)
            self.assertIn(key, resp.json(), path)


if __name__ == "__main__":
    unittest.main()
