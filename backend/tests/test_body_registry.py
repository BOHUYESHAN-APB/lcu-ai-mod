import unittest

from agent.body_registry import BodyRegistry


class BodyRegistryTests(unittest.TestCase):
    def test_registry_keeps_multiple_body_records_isolated(self):
        registry = BodyRegistry()
        registry.register("client-ai", runtime_role="body_client", server_id="survival")
        registry.register("server-miner", runtime_role="server_fake_player", server_id="survival")
        registry.update("client-ai", connected=True, armed=True, capabilities=["actions"])

        records = registry.list()

        self.assertEqual([record["id"] for record in records], ["client-ai", "server-miner"])
        self.assertTrue(registry.get("client-ai")["armed"])
        self.assertFalse(registry.get("server-miner")["armed"])

    def test_unknown_role_and_update_are_rejected(self):
        registry = BodyRegistry()
        with self.assertRaisesRegex(ValueError, "runtime_role"):
            registry.register("bad", runtime_role="player_client")
        with self.assertRaises(KeyError):
            registry.update("missing", connected=True)


if __name__ == "__main__":
    unittest.main()
