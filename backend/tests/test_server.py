import os
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import server


class ServerSDKTests(unittest.TestCase):
    def test_unauthenticated_sdk_is_limited_to_loopback_clients(self):
        with patch.dict(os.environ, {"SDK_API_TOKEN": ""}):
            local_client = TestClient(
                server.app,
                base_url="http://127.0.0.1:8080",
                client=("127.0.0.1", 50000),
            )
            remote_client = TestClient(
                server.app,
                base_url="http://127.0.0.1:8080",
                client=("192.0.2.10", 50000),
            )

            local_response = local_client.get("/api/sdk/info")
            remote_response = remote_client.get("/api/sdk/info")

        self.assertEqual(local_response.status_code, 200)
        self.assertEqual(remote_response.status_code, 401)

    def test_unauthenticated_sdk_rejects_dns_rebinding_host(self):
        with patch.dict(os.environ, {"SDK_API_TOKEN": ""}):
            client = TestClient(server.app, client=("127.0.0.1", 50000))
            response = client.get("/api/sdk/info", headers={"Host": "attacker.example"})

        self.assertEqual(response.status_code, 401)

    def test_api_requires_configured_bearer_token(self):
        with patch.dict(os.environ, {"SDK_API_TOKEN": "secret"}):
            client = TestClient(server.app)

            denied = client.get("/api/sdk/info")
            allowed = client.get("/api/sdk/info", headers={"Authorization": "Bearer secret"})

        self.assertEqual(denied.status_code, 401)
        self.assertEqual(allowed.status_code, 200)
        self.assertEqual(allowed.json()["api_version"], "1")

    def test_cors_preflight_allows_configured_local_origin(self):
        client = TestClient(server.app)

        response = client.options(
            "/api/sdk/info",
            headers={
                "Origin": "http://localhost",
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "Authorization",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["access-control-allow-origin"], "http://localhost")

    def test_auth_error_keeps_cors_headers(self):
        with patch.dict(os.environ, {"SDK_API_TOKEN": "secret"}):
            client = TestClient(server.app)
            response = client.get("/api/sdk/info", headers={"Origin": "http://localhost"})

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.headers["access-control-allow-origin"], "http://localhost")

    def test_websocket_rejects_invalid_token(self):
        with patch.dict(os.environ, {"SDK_API_TOKEN": "secret"}):
            client = TestClient(server.app)
            with self.assertRaises(WebSocketDisconnect) as raised:
                with client.websocket_connect("/ws", subprotocols=["lcu-token.wrong"]):
                    pass

        self.assertEqual(raised.exception.code, 1008)

    def test_websocket_accepts_valid_token_subprotocol(self):
        with patch.dict(os.environ, {"SDK_API_TOKEN": "secret"}):
            client = TestClient(server.app)
            with client.websocket_connect("/ws", subprotocols=["lcu-token.secret"]) as ws:
                self.assertEqual(ws.accepted_subprotocol, "lcu-token.secret")

    def test_websocket_accepts_same_origin_loopback_without_token(self):
        with patch.dict(os.environ, {"SDK_API_TOKEN": ""}):
            client = TestClient(server.app, base_url="http://127.0.0.1:8080", client=("127.0.0.1", 50000))
            with client.websocket_connect(
                "/ws",
                headers={"Origin": "http://127.0.0.1:8080", "Host": "127.0.0.1:8080"},
            ):
                pass

    def test_websocket_rejects_hostile_browser_origin(self):
        with patch.dict(os.environ, {"SDK_API_TOKEN": "secret"}):
            client = TestClient(server.app)
            with self.assertRaises(WebSocketDisconnect) as raised:
                with client.websocket_connect(
                    "/ws",
                    subprotocols=["lcu-token.secret"],
                    headers={"Origin": "https://evil.example"},
                ):
                    pass

        self.assertEqual(raised.exception.code, 1008)

    def test_context_model_accepts_legacy_raw_payload(self):
        data = server.SDKContextRequest.model_validate({"source": "legacy", "mood": "calm"})

        self.assertEqual(data.external_context, {"source": "legacy", "mood": "calm"})


if __name__ == "__main__":
    unittest.main()
