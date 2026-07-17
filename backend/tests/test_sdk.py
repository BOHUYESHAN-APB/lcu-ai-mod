import unittest
from typing import Any, cast

import httpx

from sdk import LCUClient


class SDKClientTests(unittest.TestCase):
    def test_client_sends_bearer_token_and_unwraps_gateway_response(self):
        requests = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, json={"status": "ok", "response": "来了"})

        client = LCUClient(api_token="secret")
        client._client.close()
        client._client = cast(Any, httpx.Client(
            base_url="http://127.0.0.1:8080",
            transport=httpx.MockTransport(handler),
            headers={"Authorization": "Bearer secret"},
        ))

        response = client.send_chat("跟我来", sender="launcher")
        client.close()

        self.assertEqual(response, "来了")
        self.assertEqual(requests[0].headers["Authorization"], "Bearer secret")
        self.assertEqual(requests[0].url.path, "/api/sdk/chat")

    def test_client_raises_for_http_errors(self):
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={"detail": "denied"})

        client = LCUClient()
        client._client.close()
        client._client = cast(Any, httpx.Client(
            base_url="http://127.0.0.1:8080",
            transport=httpx.MockTransport(handler),
        ))

        with self.assertRaises(httpx.HTTPStatusError):
            client.get_status()
        client.close()

    def test_identity_response_is_unwrapped(self):
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"identity": {"companion_id": "stable-id", "scope": "global"}})

        client = LCUClient()
        client._client.close()
        client._client = cast(Any, httpx.Client(
            base_url="http://127.0.0.1:8080",
            transport=httpx.MockTransport(handler),
        ))

        self.assertEqual(client.get_identity()["companion_id"], "stable-id")
        client.close()


if __name__ == "__main__":
    unittest.main()
