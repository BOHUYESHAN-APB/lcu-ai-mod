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

    def test_v2_control_and_skill_run_are_typed(self):
        requests = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if request.url.path.endswith("/leases"):
                return httpx.Response(200, json={"lease": {
                    "id": "lease-1", "fencing_token": 7, "mode": "external",
                }})
            return httpx.Response(200, json={
                "run_id": "req-1", "skill_id": "general.craft_item", "status": "accepted",
            })

        client = LCUClient()
        client._client.close()
        client._client = cast(Any, httpx.Client(
            base_url="http://127.0.0.1:8080",
            transport=httpx.MockTransport(handler),
        ))

        lease = client.acquire_control("upstream")
        run = client.run_skill(
            "general.craft_item",
            {"item": "minecraft:torch", "count": 4},
            lease_id=lease["id"],
            fencing_token=lease["fencing_token"],
        )
        client.close()

        self.assertEqual(run["run_id"], "req-1")
        self.assertEqual(requests[0].url.path, "/api/v2/control/leases")
        self.assertEqual(requests[1].url.path, "/api/v2/skills/general.craft_item/runs")
        self.assertIn(b'"fencing_token":7', requests[1].content)


if __name__ == "__main__":
    unittest.main()
