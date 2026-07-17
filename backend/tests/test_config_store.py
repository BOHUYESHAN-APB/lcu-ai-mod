import tempfile
import unittest
from pathlib import Path

from agent.config_store import ConfigStore


class ConfigStoreTests(unittest.TestCase):
    def test_provider_preset_fills_base_url_and_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ConfigStore(Path(tmp) / "config.json")

            config = store.set_agent_llm_config("planner", {"provider": "deepseek"})

            self.assertEqual(config["provider"], "deepseek")
            self.assertEqual(config["base_url"], "https://api.deepseek.com/v1")
            self.assertEqual(config["model"], "deepseek-chat")

    def test_api_key_is_persisted_but_redacted_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            store = ConfigStore(path)

            public_config = store.set_agent_llm_config("default", {"api_key": "sk-secret"})
            private_config = store.get_agent_llm_config("default", redact=False)

            self.assertEqual(public_config["api_key"], "***")
            self.assertEqual(private_config["api_key"], "sk-secret")

    def test_persona_and_external_context_are_persistent(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            store = ConfigStore(path)

            store.set_persona({"name": "Maid", "personality": "quiet"})
            store.set_integration_context({"external_context": {"origin": "upstream"}})

            reloaded = ConfigStore(path)
            persona = reloaded.get_persona()

            self.assertEqual(persona["name"], "Maid")
            self.assertEqual(persona["personality"], "quiet")
            self.assertEqual(persona["external_context"]["origin"], "upstream")

    def test_default_provider_is_mimo_with_wake_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ConfigStore(Path(tmp) / "config.json")

            config = store.get_agent_llm_config("default", redact=False)
            persona = store.get_persona()

            self.assertEqual(config["provider"], "mimo")
            self.assertEqual(config["base_url"], "https://token-plan-cn.xiaomimimo.com/v1")
            self.assertEqual(config["model"], "mimo-v2.5")
            self.assertIn("小A", persona["wake_names"])

    def test_invalid_generation_limits_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ConfigStore(Path(tmp) / "config.json")

            with self.assertRaisesRegex(ValueError, "temperature"):
                store.set_agent_llm_config("planner", {"temperature": 3})
            with self.assertRaisesRegex(ValueError, "max_tokens"):
                store.set_agent_llm_config("planner", {"max_tokens": 0})

    def test_companion_identity_is_generated_once_and_persisted(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"

            first = ConfigStore(path).get_companion_config()
            second = ConfigStore(path).get_companion_config()

            self.assertTrue(first["id"])
            self.assertEqual(first["id"], second["id"])
            self.assertEqual(first["persistence"]["scope"], "global")

    def test_companion_scope_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ConfigStore(Path(tmp) / "config.json")
            before = store.get_companion_config()

            with self.assertRaisesRegex(ValueError, "scope"):
                store.set_companion_config({"scope": "dimension"})

            self.assertEqual(store.get_companion_config(), before)

    def test_companion_config_accepts_public_identity_field_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ConfigStore(Path(tmp) / "config.json")

            result = store.set_companion_config({
                "companion_id": "streamer-one",
                "scope": "world",
                "server_id": "example.org",
                "world_id": "survival",
            })

            self.assertEqual(result["id"], "streamer-one")
            self.assertEqual(result["persistence"]["scope"], "world")
            self.assertEqual(result["persistence"]["server_id"], "example.org")


if __name__ == "__main__":
    unittest.main()
