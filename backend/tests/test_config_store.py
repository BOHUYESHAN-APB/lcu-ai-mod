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


if __name__ == "__main__":
    unittest.main()
