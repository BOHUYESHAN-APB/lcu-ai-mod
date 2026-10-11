import json
import tempfile
import unittest
from pathlib import Path

from agent.config_store import ConfigStore, get_config_store


class ConfigStoreTests(unittest.TestCase):
    def test_provider_preset_fills_base_url_and_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ConfigStore(Path(tmp) / "config.json")

            config = store.set_agent_llm_config("default", {"provider": "deepseek"})

            self.assertEqual(config["provider"], "deepseek")
            self.assertEqual(config["base_url"], "https://api.deepseek.com/v1")
            self.assertEqual(config["model"], "deepseek-v4.1-flash")

    def test_role_provider_only_applies_when_single_model_is_off(self):
        """单模型模式（默认）下，角色级 provider 一律归一化为继承 default。"""
        with tempfile.TemporaryDirectory() as tmp:
            store = ConfigStore(Path(tmp) / "config.json")
            self.assertNotIn("provider", store.set_agent_llm_config("planner", {"provider": "mimo"}))

            store.set_agent_llm_config("default", {"single_model": False})
            config = store.set_agent_llm_config("planner", {"provider": "mimo"})
            self.assertEqual(config["provider"], "mimo")

    def test_switching_default_provider_resets_other_roles_to_inherit(self):
        """换 default 的服务商后，别的角色不能再钉着旧家的地址/密钥/模型。

        否则会出现"配了新 key 和新 URL，后端却仍在打旧地址"（历史上 vision 角色就是这样
        静默 401 的）；角色自己的调参（温度/上限）必须保留。
        """
        with tempfile.TemporaryDirectory() as tmp:
            store = ConfigStore(Path(tmp) / "config.json")
            store.set_agent_llm_config("vision", {"provider": "mimo",
                                                  "base_url": "https://old.example/v1",
                                                  "api_key": "old-key", "model": "old-vision"})

            store.set_agent_llm_config("default", {"provider": "deepseek",
                                                   "base_url": "https://new.example/v1",
                                                   "api_key": "new-key", "model": "new-model"})

            vision = store.get_agent_llm_config("vision", redact=False)
            for key in ("provider", "base_url", "api_key", "model"):
                self.assertNotIn(key, vision, f"vision.{key} 应回到继承 default")

            decider = store.get_agent_llm_config("decider", redact=False)
            self.assertEqual(decider["temperature"], 0.2, "角色自己的调参要保留")

    def test_single_model_clears_stale_role_endpoints_on_load(self):
        """历史配置里每个角色都钉过旧家的地址/密钥/模型，且文件没有 single_model 键。

        这是用户实机遇到的"换了 key 却仍在打旧地址（vision 反复 401）"的根因。
        **加载时**就必须归一化，不能等用户重新保存一次。
        """
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps({
                "llm": {
                    "agents": {
                        "default": {
                            "provider": "deepseek",
                            "base_url": "https://api.deepseek.com/v1",
                            "api_key": "new-key",
                            "model": "deepseek-v4.1-flash",
                        },
                        "vision": {
                            "provider": "mimo",
                            "base_url": "https://token-plan-cn.xiaomimimo.com/v1",
                            "api_key": "tp-old",
                            "model": "mimo-v2-omni",
                            "temperature": 0.3,
                        },
                    }
                }
            }, ensure_ascii=False), encoding="utf-8")

            store = ConfigStore(path)
            vision = store.get_agent_llm_config("vision", redact=False)
            for key in ("provider", "base_url", "api_key", "model"):
                self.assertNotIn(key, vision, f"vision.{key} 必须回继承 default（否则又会打旧地址）")
            self.assertEqual(vision["temperature"], 0.3, "角色自己的调参要保留")

            # 关掉单模型模式后，角色可以各自指定服务商/模型
            store.set_agent_llm_config("default", {"single_model": False})
            store.set_agent_llm_config("vision", {"base_url": "https://my-own.example/v1"})
            self.assertEqual(
                store.get_agent_llm_config("vision", redact=False)["base_url"],
                "https://my-own.example/v1",
            )

    def test_capability_level_defaults_to_text_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ConfigStore(Path(tmp) / "config.json")
            self.assertEqual(store.get_capability_level(), 1, "默认 L1：纯文本、不抓屏、不传图片")

    def test_capability_level_roundtrip_and_clamps(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            store = ConfigStore(path)
            store.set_app_config({"capability_level": 2})
            self.assertEqual(store.get_capability_level(), 2)
            self.assertEqual(ConfigStore(path).get_capability_level(), 2, "要落盘")
            store.set_app_config({"capability_level": 99})
            self.assertEqual(store.get_capability_level(), 3)
            store.set_app_config({"capability_level": "abc"})
            self.assertEqual(store.get_capability_level(), 1, "非法值回退 L1")

    def test_night_auto_sleep_defaults_on(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ConfigStore(Path(tmp) / "config.json")
            self.assertTrue(store.get_night_auto_sleep(), "默认开：自主模式下夜里自己找床睡")

    def test_night_auto_sleep_roundtrip_and_persistence(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            store = ConfigStore(path)
            store.set_app_config({"night_auto_sleep": False})
            self.assertFalse(store.get_night_auto_sleep())
            self.assertFalse(ConfigStore(path).get_night_auto_sleep(), "要落盘")
            store.set_app_config({"night_auto_sleep": True})
            self.assertTrue(store.get_night_auto_sleep())

    def test_night_auto_sleep_accepts_ui_string_form(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ConfigStore(Path(tmp) / "config.json")
            store.set_app_config({"night_auto_sleep": "false"})
            self.assertFalse(store.get_night_auto_sleep())
            store.set_app_config({"night_auto_sleep": "是"})
            self.assertTrue(store.get_night_auto_sleep())

    def test_night_auto_sleep_ignores_garbage_and_keeps_previous(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ConfigStore(Path(tmp) / "config.json")
            store.set_app_config({"night_auto_sleep": False})
            store.set_app_config({"night_auto_sleep": "maybe"})
            self.assertFalse(store.get_night_auto_sleep(), "非法值维持原状，不误开")

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

    def test_default_provider_is_neutral_and_not_vendor_locked(self):
        """默认**不绑定任何一家**（用户要求）：默认走 DeepSeek 的 OpenAI 兼容端点，
        base_url/model/api_key 都是可在 UI 里改的一等字段；MiMo 只是可选预设。"""
        with tempfile.TemporaryDirectory() as tmp:
            store = ConfigStore(Path(tmp) / "config.json")

            config = store.get_agent_llm_config("default", redact=False)
            persona = store.get_persona()

            self.assertEqual(config["provider"], "deepseek")
            self.assertEqual(config["base_url"], "https://api.deepseek.com/v1")
            self.assertEqual(config["model"], "deepseek-v4.1-flash")
            self.assertIn("小A", persona["wake_names"])

    def test_decision_threshold_default_override_and_persistence(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            store = ConfigStore(path)

            self.assertEqual(store.get_decision_threshold(), 0.6)

            store.set_app_config({"decision": {"confidence_threshold": 0.75}})

            self.assertEqual(store.get_decision_threshold(), 0.75)
            # 落盘后新实例也能读到
            self.assertEqual(ConfigStore(path).get_decision_threshold(), 0.75)

    def test_decision_threshold_is_clamped(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ConfigStore(Path(tmp) / "config.json")

            store.set_app_config({"decision": {"confidence_threshold": 5}})
            self.assertEqual(store.get_decision_threshold(), 1.0)

            store.set_app_config({"decision": {"confidence_threshold": -2}})
            self.assertEqual(store.get_decision_threshold(), 0.0)

    def test_decision_threshold_ignores_non_numeric(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            store = ConfigStore(path)

            store.set_app_config({"decision": {"confidence_threshold": "abc"}})

            self.assertEqual(store.get_decision_threshold(), 0.6)

    def test_config_store_registry_shares_one_instance_per_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"

            self.assertIs(get_config_store(path), get_config_store(path))


if __name__ == "__main__":
    unittest.main()
