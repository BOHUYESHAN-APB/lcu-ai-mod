"""前端/站点层：私信页 `/contacts`、OBS 组件子页 `/overlay/<component>`、指令白名单 UI 的端点。

用 `TestClient` 直接打接口（**不进入 lifespan**，不起后台线程、不连模组）；
落盘路径一律指到临时目录，绝不碰仓库里的 `.local/` 与 `data/`。
"""

import os
import re
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

import server  # noqa: E402
from agent.command_gate import HARD_BLACKLIST, CommandsStore  # noqa: E402
from agent.contacts import ContactStore  # noqa: E402


class _FakeSession:
    """只提供前端页面需要的两件事：命名空间 + 在线玩家快照。"""

    def __init__(self, server_key="srv1", players=()):
        self.runtime = {"players": list(players)}
        self._key = server_key

    def current_server_key(self):
        return self._key


class _FakeOrchestrator:
    def __init__(self, session):
        self.session = session


class FrontendPagesTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self._saved_orchestrator = server.orchestrator
        self._saved_commands = server._fallback_commands
        self._saved_contacts = server._fallback_contacts
        server.orchestrator = None
        server._fallback_commands = CommandsStore(path=os.path.join(self.tmp, "commands.json"))
        server._fallback_contacts = ContactStore(root=os.path.join(self.tmp, "contacts"))
        self.client = TestClient(server.app)

    def tearDown(self):
        server.orchestrator = self._saved_orchestrator
        server._fallback_commands = self._saved_commands
        server._fallback_contacts = self._saved_contacts
        self._tmp.cleanup()

    # ── 私信页 ──

    def test_contacts_page_serves_the_two_pane_ui(self):
        resp = self.client.get("/contacts")

        self.assertEqual(resp.status_code, 200)
        html = resp.text
        self.assertIn("通讯录", html)
        self.assertIn('id="contacts"', html)
        self.assertIn('id="messages"', html)
        self.assertIn("selectContact", html)
        self.assertIn("/api/contacts/", html, "代发/已读都走自建端点")

    def test_contacts_deep_link_serves_the_same_page(self):
        resp = self.client.get("/contacts/uuid-steve")

        self.assertEqual(resp.status_code, 200)
        self.assertIn("initialTarget", resp.text)

    def test_contacts_online_flag_comes_from_the_world_snapshot(self):
        session = _FakeSession(players=[{"name": "Steve"}])
        server.orchestrator = _FakeOrchestrator(session)
        store = server._contacts_store()
        store.record_in("srv1", "uuid-steve", "Steve", "hi")
        store.record_in("srv1", "uuid-alex", "Alex", "yo")

        body = self.client.get("/api/contacts").json()

        by_name = {c["name"]: c for c in body["contacts"]}
        self.assertTrue(by_name["Steve"]["online"])
        self.assertFalse(by_name["Alex"]["online"])
        self.assertEqual(body["server"], "srv1")

    def test_offline_without_a_session_does_not_guess_online(self):
        server._contacts_store().record_in("unknown", "uuid-steve", "Steve", "hi")

        body = self.client.get("/api/contacts").json()

        self.assertFalse(body["contacts"][0]["online"])

    def test_send_resolves_the_uuid_to_the_contact_name(self):
        session = _FakeSession(server_key="srv1")
        sent = []
        session.send_whisper = lambda player, text, *, tier="": (
            sent.append((player, text, tier)) or {"ok": True})
        server.orchestrator = _FakeOrchestrator(session)
        server._contacts_store().record_in("srv1", "uuid-steve", "Steve", "hi")

        resp = self.client.post("/api/contacts/uuid-steve/send", json={"text": "在的，马上"})

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "ok")
        self.assertEqual(resp.json()["target"], "Steve")
        self.assertEqual(sent, [("Steve", "在的，马上", "master")])

    def test_send_surfaces_a_server_side_block(self):
        session = _FakeSession(server_key="srv1")
        session.send_whisper = lambda *a, **k: {"ok": False, "blocked": "not_found"}
        server.orchestrator = _FakeOrchestrator(session)
        server._contacts_store().record_in("srv1", "uuid-steve", "Steve", "hi")

        body = self.client.post("/api/contacts/uuid-steve/send", json={"text": "hi"}).json()

        self.assertEqual(body["status"], "blocked")
        self.assertEqual(body["blocked"], "not_found")

    # ── OBS 组件 ──

    def test_overlay_manifest_names_are_the_registered_components(self):
        body = self.client.get("/api/overlay/components").json()

        names = [item["name"] for item in body["components"]]
        self.assertEqual(names, list(server.OVERLAY_COMPONENTS))
        for name in ("status", "task", "players", "state", "backend"):
            self.assertIn(name, names)
        for item in body["components"]:
            self.assertEqual(item["url"], f"/overlay/{item['name']}")
            self.assertTrue(item["title"] and item["desc"])

    def test_every_registered_component_has_a_page(self):
        for name in server.OVERLAY_COMPONENTS:
            resp = self.client.get(f"/overlay/{name}")
            self.assertEqual(resp.status_code, 200, name)
            self.assertIn("OVERLAY_RENDERERS", resp.text)

    def test_unknown_component_is_404(self):
        self.assertEqual(self.client.get("/overlay/nope").status_code, 404)

    def test_overlay_page_renders_every_registry_component(self):
        html = self.client.get("/overlay/status").text

        for name in server.OVERLAY_COMPONENTS:
            self.assertRegex(html, rf"(?m)^\s*{name}:\s*render",
                             f"注册表里的 {name} 必须在页面渲染表里有实现")

    def test_overlay_page_is_transparent_and_not_size_fixed(self):
        html = self.client.get("/overlay/state").text

        self.assertIn("background:transparent", html)
        self.assertIn("body{margin:0", html.replace(" ", ""))
        self.assertIn("100%", html)
        self.assertIn("theme", html)
        self.assertIn("scale", html)
        self.assertIn("max", html)

    # ── 指令白名单表格 ──

    def test_entries_view_is_empty_by_default(self):
        body = self.client.get("/api/commands").json()

        self.assertEqual(body["entries"], [])

    def test_entries_view_flags_scope_and_high_risk(self):
        self.client.post("/api/commands", json={
            "commands": [{"name": "tp", "desc": "传送", "tiers": ["master"]}],
            "servers": {"srvA": [{"name": "home", "desc": "回主城"}]},
        })
        server.orchestrator = _FakeOrchestrator(_FakeSession(server_key="srvA"))

        entries = self.client.get("/api/commands").json()["entries"]

        by_name = {e["name"]: e for e in entries}
        self.assertEqual(by_name["tp"]["scope"], "global")
        self.assertTrue(by_name["tp"]["high_risk"], "tp 是高风险 → UI 标『需二次确认』")
        self.assertFalse(by_name["tp"]["hard_rejected"])
        self.assertEqual(by_name["home"]["scope"], "srvA")
        self.assertFalse(by_name["home"]["high_risk"])

    def test_hard_blacklist_entry_cannot_be_enabled_from_the_ui(self):
        posted = self.client.post("/api/commands", json={
            "commands": [{"name": "kill", "enabled": True},
                         {"name": "op", "enabled": True},
                         {"name": "minecraft:stop", "enabled": True},
                         {"name": "msg", "enabled": True}],
        }).json()

        self.assertEqual(posted["config"]["global"], ["msg"])
        self.assertEqual(set(posted["rejected"]), {"kill", "op", "minecraft:stop"})
        self.assertNotIn("kill", posted["effective"])
        for name in ("kill", "op"):
            self.assertIn(name, HARD_BLACKLIST)
        again = self.client.get("/api/commands").json()
        self.assertNotIn("kill", again["effective"])
        self.assertEqual([e["name"] for e in again["entries"]], ["msg"])

    def test_ui_toggle_round_trips_enabled_flag(self):
        self.client.post("/api/commands", json={"global": ["msg"]})
        self.client.post("/api/commands", json={
            "commands": [{"name": "msg", "enabled": False}],
        })

        body = self.client.get("/api/commands").json()

        self.assertEqual(body["entries"][0]["name"], "msg")
        self.assertFalse(body["entries"][0]["enabled"])
        self.assertEqual(body["effective"], [], "关掉的条目不再生效")

    def test_dashboard_page_contains_the_whitelist_and_overlay_panels(self):
        html = self.client.get("/").text

        self.assertIn('id="page-commands"', html)
        self.assertIn('id="page-overlay"', html)
        self.assertIn("loadCommandsConfig", html)
        self.assertIn("loadOverlayComponents", html)
        self.assertIn("硬拒", html)
        self.assertIn("需二次确认", html)


if __name__ == "__main__":
    unittest.main()
