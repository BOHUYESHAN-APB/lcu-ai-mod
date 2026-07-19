import unittest
from typing import Any, cast
from unittest.mock import patch

from agent.llm_service import LLMRequestRejected, LLMService


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
        service.set_agent_config("planner", {
            "base_url": "https://planner.test/v1/",
            "model": "planner-model",
            "temperature": 1.1,
            "max_tokens": 4096,
        })

        payload = service._build_payload([{"role": "user", "content": "hi"}], config=service._resolve_config("planner"))

        self.assertEqual(payload["model"], "planner-model")
        self.assertEqual(payload["temperature"], 1.1)
        self.assertEqual(payload["max_tokens"], 4096)
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

    def test_named_agent_key_and_keyless_provider_are_independently_configured(self):
        service = LLMService()
        service.set_agent_config("planner", {"api_key": "planner-key"})
        service.set_agent_config("timing_gate", {"provider": "ollama", "api_key": ""})

        self.assertFalse(service.is_configured("default"))
        self.assertTrue(service.is_configured("planner"))
        self.assertTrue(service.is_configured("timing_gate"))

    def test_unconfigured_provider_is_rejected_before_transport(self):
        service = LLMService()

        with patch.object(service, "_post") as post:
            with self.assertRaises(LLMRequestRejected) as caught:
                service.chat([{"role": "user", "content": "hello"}])

        self.assertEqual(caught.exception.code, "not_configured")
        post.assert_not_called()

    def test_prepare_request_compacts_old_optional_messages_without_mutating_input(self):
        service = LLMService()
        service.set_agent_config("planner", {
            "context_window_tokens": 120,
            "max_input_tokens": 90,
            "max_output_tokens": 10,
            "reserved_output_tokens": 20,
            "compression_enabled": True,
            "compression_trigger_tokens": 65,
            "compression_target_tokens": 45,
            "recent_messages_to_keep": 1,
            "max_request_bytes": 4096,
        })
        messages = [
            {"role": "system", "content": "policy"},
            {"role": "user", "content": "old message one"},
            {"role": "assistant", "content": "old message two"},
            {"role": "user", "content": "old message three"},
            {"role": "user", "content": "latest request"},
        ]
        original = [dict(message) for message in messages]

        payload, meta = service._prepare_request(
            messages, "planner", service._resolve_config("planner"), stream=False,
        )

        self.assertEqual(messages, original)
        self.assertLess(len(payload["messages"]), len(messages))
        self.assertEqual(payload["messages"][0]["role"], "system")
        self.assertEqual(payload["messages"][-1]["content"], "latest request")
        self.assertGreater(meta["compression"]["removed_messages"], 0)

    def test_required_oversized_input_is_rejected_before_transport(self):
        service = LLMService()
        service.set_agent_config("planner", {
            "context_window_tokens": 100,
            "max_input_tokens": 70,
            "max_output_tokens": 10,
            "reserved_output_tokens": 20,
            "compression_enabled": True,
            "compression_trigger_tokens": 60,
            "compression_target_tokens": 40,
            "recent_messages_to_keep": 1,
            "max_request_bytes": 4096,
            "api_key": "planner-key",
        })
        messages = [{"role": "system", "content": "x" * 500, "required": True}]

        with patch.object(service, "_post") as post:
            with self.assertRaises(LLMRequestRejected) as caught:
                service.chat(messages, agent="planner")

        self.assertEqual(caught.exception.code, "input_limit_exceeded")
        post.assert_not_called()
        usage = service.get_usage()
        self.assertEqual(usage["rejected_requests"], 1)
        self.assertEqual(usage["latest_rejection"]["code"], "input_limit_exceeded")

    def test_streaming_uses_the_same_pre_transport_input_budget(self):
        service = LLMService()
        service.set_agent_config("conversation", {
            "context_window_tokens": 100,
            "max_input_tokens": 70,
            "max_output_tokens": 10,
            "reserved_output_tokens": 20,
            "compression_enabled": False,
            "compression_trigger_tokens": 60,
            "compression_target_tokens": 40,
            "recent_messages_to_keep": 1,
            "max_request_bytes": 4096,
            "api_key": "conversation-key",
        })

        with patch.object(service._client, "stream") as stream:
            with self.assertRaises(LLMRequestRejected) as caught:
                list(service.chat_stream([
                    {"role": "system", "content": "x" * 500, "required": True},
                ], agent="conversation"))

        self.assertEqual(caught.exception.code, "input_limit_exceeded")
        stream.assert_not_called()

    def test_requested_output_cannot_exceed_agent_limit(self):
        service = LLMService()
        service.set_agent_config("planner", {"max_output_tokens": 20, "api_key": "planner-key"})

        with self.assertRaises(LLMRequestRejected) as caught:
            service.chat([{"role": "user", "content": "hello"}], agent="planner", max_tokens=21)

        self.assertEqual(caught.exception.code, "output_limit_exceeded")

    def test_requested_output_reduces_available_input_context(self):
        service = LLMService()
        service.set_agent_config("planner", {
            "context_window_tokens": 100,
            "max_input_tokens": 90,
            "max_output_tokens": 80,
            "reserved_output_tokens": 10,
            "compression_enabled": False,
            "api_key": "planner-key",
        })

        with patch.object(service, "_post") as post:
            with self.assertRaises(LLMRequestRejected) as caught:
                service.chat([
                    {"role": "user", "content": "x" * 100},
                ], agent="planner", max_tokens=80)

        self.assertEqual(caught.exception.code, "input_limit_exceeded")
        post.assert_not_called()

    def test_success_usage_is_attributed_to_agent(self):
        service = LLMService()
        service.set_agent_config("planner", {"api_key": "planner-key"})
        response = {
            "choices": [{"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 11, "completion_tokens": 3},
        }

        with patch.object(service, "_post", return_value=response):
            result = service.chat([{"role": "user", "content": "hello"}], agent="planner")

        self.assertEqual(result["content"], "ok")
        usage = service.get_usage()
        self.assertEqual(usage["total_tokens"], 14)
        self.assertEqual(usage["by_agent"]["planner"]["request_count"], 1)


if __name__ == "__main__":
    unittest.main()
