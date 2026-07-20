import tempfile
import unittest
from pathlib import Path

from agent.access_policy import classify_skill, evaluate, load_policy
from agent.config_store import ConfigStore


class AccessPolicyTests(unittest.TestCase):
    def test_unknown_skills_fail_into_a_denied_class(self):
        self.assertEqual(classify_skill("core.move_to"), "task.movement")
        self.assertEqual(classify_skill("third-party.op"), "task.unknown")

    def test_public_chat_does_not_grant_body_skills(self):
        decision = evaluate(
            load_policy(Path("missing-access-policy.json")),
            {"name": "Bob"},
            channel="chat.public",
            skill="chat.reply",
            server_id="survival",
            body_id="server-ai-1",
        )
        self.assertTrue(decision["allowed"])

        decision = evaluate(
            load_policy(Path("missing-access-policy.json")),
            {"name": "Bob"},
            channel="chat.public",
            skill="task.resource",
            server_id="survival",
            body_id="server-ai-1",
        )
        self.assertFalse(decision["allowed"])

    def test_private_chat_gate_can_require_an_explicit_principal(self):
        policy = load_policy(Path("missing-access-policy.json"))
        policy["private_chat"] = False
        policy["principals"].append({"id": "alice", "uuid": "uuid-a", "role": "friend"})

        self.assertFalse(evaluate(policy, {"uuid": "uuid-b"}, channel="chat.private", skill="chat.reply")["allowed"])
        self.assertTrue(evaluate(policy, {"uuid": "uuid-a"}, channel="chat.private", skill="chat.reply")["allowed"])

    def test_principal_scope_and_skill_override(self):
        policy = load_policy(Path("missing-access-policy.json"))
        policy["principals"].append({
            "id": "alice",
            "uuid": "uuid-a",
            "role": "master",
            "server_ids": ["survival"],
            "body_ids": ["server-ai-1"],
            "skills": {"task.inventory": "allow"},
        })
        self.assertTrue(evaluate(policy, {"uuid": "uuid-a"}, channel="task", skill="task.inventory", server_id="survival", body_id="server-ai-1")["allowed"])
        self.assertFalse(evaluate(policy, {"uuid": "uuid-a"}, channel="task", skill="task.inventory", server_id="creative", body_id="server-ai-1")["allowed"])
        self.assertFalse(evaluate(policy, {"uuid": "uuid-a"}, channel="task", skill="task.inventory", server_id="survival", body_id="player-owned-ai")["allowed"])
        self.assertFalse(evaluate(policy, {"uuid": "uuid-b", "name": "alice"}, channel="task", skill="task.inventory", server_id="survival", body_id="server-ai-1")["allowed"])

    def test_config_store_principal_crud_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ConfigStore(Path(tmp) / "config.json")
            store.upsert_access_principal({"id": "alice", "uuid": "uuid-a", "role": "master"})
            self.assertEqual(store.get_access_policy()["principals"][0]["id"], "alice")
            store.upsert_access_principal({"id": "alice", "role": "friend"})
            self.assertEqual(store.get_access_policy()["principals"][0]["role"], "friend")
            self.assertTrue(store.delete_access_principal("alice"))
            self.assertFalse(store.delete_access_principal("alice"))


if __name__ == "__main__":
    unittest.main()
