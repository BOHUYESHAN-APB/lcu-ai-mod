import unittest
from typing import Any, cast

from agent.llm_service import LLMService


class DummyResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class DummyClient:
    def __init__(self):
        self.last_get = {"url": "", "headers": {}}

    def get(self, url, headers=None):
        self.last_get = {"url": url, "headers": headers or {}}
        return DummyResponse({"data": [{"id": "model-a"}, {"id": "model-b"}]})


class LLMServiceTests(unittest.TestCase):
    def test_agent_config_overrides_default_model_and_base_url(self):
        service = LLMService()
        service.set_agent_config("default", {"base_url": "https://default.test/v1", "model": "default-model"})
        service.set_agent_config("planner", {"base_url": "https://planner.test/v1/", "model": "planner-model"})

        payload = service._build_payload([{"role": "user", "content": "hi"}], config=service._resolve_config("planner"))

        self.assertEqual(payload["model"], "planner-model")
        self.assertEqual(service._resolve_config("planner")["base_url"], "https://planner.test/v1")

    def test_fetch_models_uses_agent_base_url_and_bearer_key(self):
        service = LLMService()
        dummy_client = DummyClient()
        service._client = cast(Any, dummy_client)
        service.set_agent_config("default", {"base_url": "https://default.test/v1", "api_key": "sk-test"})

        models = service.fetch_models("default")

        self.assertEqual(models, ["model-a", "model-b"])
        self.assertEqual(dummy_client.last_get["url"], "https://default.test/v1/models")
        self.assertEqual(dummy_client.last_get["headers"]["Authorization"], "Bearer sk-test")

    def test_system_prompt_includes_persona_and_external_context(self):
        service = LLMService()

        prompt = service.build_system_prompt({
            "persona": {
                "name": "Maid",
                "personality": "calm",
                "speaking_style": "brief",
                "external_context": {"origin": "launcher"},
            }
        })

        self.assertIn("你是 Maid", prompt)
        self.assertIn("calm", prompt)
        self.assertIn("brief", prompt)
        self.assertIn("launcher", prompt)


if __name__ == "__main__":
    unittest.main()
