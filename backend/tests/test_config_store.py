import json
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
            with self.assertRaisesRegex(ValueError, "max_output_tokens"):
                store.set_agent_llm_config("planner", {"max_output_tokens": 0})

    def test_default_model_budget_is_complete_and_consistent(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ConfigStore(Path(tmp) / "config.json")

            config = store.get_agent_llm_config("default", redact=False)

            self.assertEqual(config["context_window_tokens"], 200000)
            self.assertEqual(config["max_output_tokens"], 16384)
            self.assertLessEqual(
                config["max_input_tokens"] + config["reserved_output_tokens"],
                config["context_window_tokens"],
            )
            self.assertLess(config["compression_target_tokens"], config["compression_trigger_tokens"])
            self.assertNotIn("max_tokens", config)

    def test_legacy_max_tokens_is_migrated_and_rewritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps({
                "version": 1,
                "llm": {"agents": {"default": {"max_tokens": 777}}},
            }), encoding="utf-8")

            store = ConfigStore(path)
            config = store.get_agent_llm_config("default", redact=False)
            persisted = json.loads(path.read_text(encoding="utf-8"))

            self.assertEqual(config["max_output_tokens"], 777)
            self.assertNotIn("max_tokens", persisted["llm"]["agents"]["default"])
            self.assertEqual(persisted["version"], 3)

    def test_version_two_inline_provider_migrates_without_changing_effective_connection(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps({
                "version": 2,
                "llm": {"agents": {"default": {
                    "provider": "openai",
                    "base_url": "https://example.test/v1",
                    "model": "existing-model",
                    "api_key": "existing-secret",
                    "temperature": 0.2,
                }}},
            }), encoding="utf-8")

            store = ConfigStore(path)
            effective = store.get_agent_llm_config("default", redact=False)
            persisted = store.raw(redact=False)

            self.assertEqual(effective["provider"], "openai")
            self.assertEqual(effective["base_url"], "https://example.test/v1")
            self.assertEqual(effective["model"], "existing-model")
            self.assertEqual(effective["api_key"], "existing-secret")
            self.assertEqual(persisted["version"], 3)
            profile_id = persisted["llm"]["agents"]["default"]["provider_profile"]
            self.assertEqual(persisted["llm"]["provider_profiles"][profile_id]["provider"], "openai")
            self.assertNotIn("api_key", persisted["llm"]["agents"]["default"])

    def test_model_budget_cross_field_validation_is_atomic(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ConfigStore(Path(tmp) / "config.json")
            before = store.get_agent_llm_config("planner", redact=False)

            with self.assertRaisesRegex(ValueError, "max_input_tokens plus reserved_output_tokens"):
                store.set_agent_llm_config("planner", {
                    "context_window_tokens": 100,
                    "max_input_tokens": 90,
                    "max_output_tokens": 10,
                    "reserved_output_tokens": 20,
                })
            with self.assertRaisesRegex(ValueError, "compression_target_tokens"):
                store.set_agent_llm_config("planner", {
                    "compression_target_tokens": 170000,
                })
            with self.assertRaisesRegex(ValueError, "compression_enabled"):
                store.set_agent_llm_config("planner", {"compression_enabled": "true"})

            self.assertEqual(store.get_agent_llm_config("planner", redact=False), before)

    def test_model_budget_round_trips_canonical_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ConfigStore(Path(tmp) / "config.json")

            config = store.set_agent_llm_config("planner", {
                "context_window_tokens": "8192",
                "max_input_tokens": "6144",
                "max_output_tokens": "1024",
                "reserved_output_tokens": "1024",
                "max_request_bytes": "262144",
                "compression_enabled": False,
                "compression_trigger_tokens": "5000",
                "compression_target_tokens": "3000",
                "recent_messages_to_keep": "8",
                "summary_model_agent": "default",
                "summary_max_output_tokens": "512",
            })

            self.assertEqual(config["context_window_tokens"], 8192)
            self.assertEqual(config["max_output_tokens"], 1024)
            self.assertIs(config["compression_enabled"], False)
            self.assertNotIn("max_tokens", config)

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

    def test_provider_profiles_supply_effective_agent_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ConfigStore(Path(tmp) / "config.json")
            store.upsert_provider_profile({
                "id": "deepseek-backup",
                "name": "DeepSeek backup",
                "provider": "deepseek",
                "api_key": "profile-secret",
            })
            store.set_agent_llm_config("planner", {
                "provider_profile": "deepseek-backup",
                "routing_mode": "priority",
                "fallback_profiles": ["mimo-default"],
            })

            effective = store.get_agent_llm_config("planner", redact=False)
            public_profiles = store.list_provider_profiles()

            self.assertEqual(effective["provider"], "deepseek")
            self.assertEqual(effective["api_key"], "profile-secret")
            self.assertEqual(effective["fallback_profiles"], ["mimo-default"])
            self.assertEqual(next(item for item in public_profiles if item["id"] == "deepseek-backup")["api_key"], "***")

    def test_assigned_provider_profile_cannot_be_deleted(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ConfigStore(Path(tmp) / "config.json")

            with self.assertRaisesRegex(ValueError, "assigned"):
                store.delete_provider_profile("mimo-default")

    def test_fallback_provider_profile_cannot_be_deleted(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ConfigStore(Path(tmp) / "config.json")
            store.upsert_provider_profile({"id": "fallback", "provider": "openai"})
            store.set_agent_llm_config("planner", {"fallback_profiles": ["fallback"]})

            with self.assertRaisesRegex(ValueError, "assigned"):
                store.delete_provider_profile("fallback")

    def test_profile_patch_preserves_secret_and_allows_missing_provider(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ConfigStore(Path(tmp) / "config.json")
            store.upsert_provider_profile({"id": "remote", "provider": "openai", "api_key": "secret"})
            updated = store.upsert_provider_profile({"id": "remote", "enabled": False, "api_key": "***"})

            self.assertFalse(updated["enabled"])
            self.assertEqual(store.get_provider_profile("remote", redact=False)["api_key"], "secret")

    def test_reading_unknown_agent_does_not_mutate_store(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ConfigStore(Path(tmp) / "config.json")
            before = store.raw(redact=False)

            store.get_agent_llm_config("not-configured", redact=False)

            self.assertEqual(store.raw(redact=False), before)

    def test_provider_profile_can_be_loaded_with_or_without_redaction(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ConfigStore(Path(tmp) / "config.json")
            store.upsert_provider_profile({
                "id": "remote", "provider": "openai", "api_key": "profile-secret",
            })

            self.assertEqual(store.get_provider_profile("remote")["api_key"], "***")
            self.assertEqual(store.get_provider_profile("remote", redact=False)["api_key"], "profile-secret")

    def test_legacy_whitelist_migrates_to_scoped_access_policy(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps({
                "version": 2,
                "whitelist": ["Alice"],
                "listen_public": False,
            }), encoding="utf-8")

            policy = ConfigStore(path).get_access_policy()

            self.assertFalse(policy["public_chat"])
            self.assertEqual(policy["principals"][0]["name"], "Alice")
            self.assertEqual(policy["principals"][0]["role"], "friend")


if __name__ == "__main__":
    unittest.main()
