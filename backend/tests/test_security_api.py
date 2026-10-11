"""安全相关的 REST 面：指令白名单（`.local/commands.json`）+ 通讯录（自建端点）。

用 `TestClient` 直接打接口（**不进入 lifespan**，不起后台线程、不连模组）；
落盘路径一律指到临时目录，绝不碰仓库里的 `.local/` 与 `data/`。
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

import server  # noqa: E402
from agent.command_gate import CommandsStore  # noqa: E402
from agent.contacts import ContactStore  # noqa: E402


class SecurityApiTests(unittest.TestCase):
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

    # ── 指令白名单 ──

    def test_commands_default_whitelist_is_empty(self):
        body = self.client.get("/api/commands").json()

        self.assertEqual(body["config"]["global"], [])
        self.assertEqual(body["effective"], [])
        self.assertTrue(body["config"]["enabled"])

    def test_hard_blacklist_is_exposed_but_not_selectable(self):
        body = self.client.get("/api/commands").json()

        self.assertIn("kill", body["hard_blacklist"])
        self.assertIn("kick", body["hard_blacklist"])
        self.assertIn("tp", body["high_risk"])

    def test_whitelist_round_trip_drops_blacklisted_entries(self):
        posted = self.client.post("/api/commands", json={
            "global": ["msg", "tpaccept", "kill"],
            "whisper_family": "tell",
        })

        self.assertEqual(posted.status_code, 200)
        again = self.client.get("/api/commands").json()
        self.assertEqual(again["config"]["global"], ["msg", "tpaccept"])
        self.assertEqual(again["config"]["whisper_family"], "tell")
        self.assertEqual(again["effective"], ["msg", "tpaccept"])

    def test_server_specific_whitelist_is_kept(self):
        self.client.post("/api/commands", json={"servers": {"srvA": ["home"]}})

        config = self.client.get("/api/commands").json()["config"]
        self.assertEqual([item["name"] for item in config["servers"]["srvA"]], ["home"])
        self.assertEqual(config["servers"]["srvA"][0]["tiers"], ["master", "friend"])

    # ── 通讯录 ──

    def test_contacts_are_listed_with_unread_and_last_message(self):
        store = server._contacts_store()
        store.record_in("unknown", "uuid-steve", "Steve", "在吗")
        store.record_out("unknown", "uuid-steve", "Steve", "在")

        body = self.client.get("/api/contacts").json()

        self.assertEqual(body["server"], "unknown")
        self.assertEqual(len(body["contacts"]), 1)
        contact = body["contacts"][0]
        self.assertEqual(contact["name"], "Steve")
        self.assertEqual(contact["unread"], 1)
        self.assertEqual(contact["unreplied"], 0)
        self.assertEqual(contact["last_text"], "在")

    def test_messages_endpoint(self):
        server._contacts_store().record_in("unknown", "uuid-steve", "Steve", "hi")

        body = self.client.get("/api/contacts/uuid-steve/messages").json()

        self.assertEqual([row["text"] for row in body["messages"]], ["hi"])

    def test_read_endpoint(self):
        store = server._contacts_store()
        store.record_in("unknown", "uuid-steve", "Steve", "hi")

        body = self.client.post("/api/contacts/uuid-steve/read").json()

        self.assertEqual(body["marked"], 1)
        self.assertEqual(store.contacts("unknown")[0]["unread"], 0)

    def test_send_requires_text(self):
        resp = self.client.post("/api/contacts/uuid-steve/send", json={"text": "  "})

        self.assertEqual(resp.status_code, 400)

    def test_send_is_unavailable_without_a_session(self):
        resp = self.client.post("/api/contacts/uuid-steve/send", json={"text": "hi"})

        self.assertEqual(resp.status_code, 503)

    def test_unknown_contact_returns_empty_thread(self):
        body = self.client.get("/api/contacts/nobody/messages").json()

        self.assertEqual(body["messages"], [])


if __name__ == "__main__":
    unittest.main()
