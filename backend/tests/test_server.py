import asyncio
import os
import threading
import unittest
from contextlib import contextmanager
from unittest.mock import patch

from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import server


class FakeBody:
    def __init__(self, connected=True):
        self.is_connected = connected
        self.commands = []
        self.connect_calls = 0
        self.disconnect_calls = 0

    def connect(self):
        self.connect_calls += 1
        self.is_connected = True
        return True

    def disconnect(self):
        self.disconnect_calls += 1
        self.is_connected = False

    def send_command(self, command, args=None):
        self.commands.append((command, args or {}))
        return f"fake-{len(self.commands)}"

    def drain(self):
        return []


class FakeSession:
    def __init__(self):
        self.commands = []
        self.stop_calls = 0

    def register_external_command(self, command, request_id, args, requester):
        self.commands.append((command, request_id, args, requester))

    def stop(self):
        self.stop_calls += 1


class FakeOrchestrator:
    def __init__(self, body=None, **_options):
        self.body = body
        self.session = FakeSession()
        self.started = threading.Event()
        self.ticked = threading.Event()
        self.start_calls = 0
        self.stop_calls = 0
        self.tick_calls = 0

    def start(self):
        self.start_calls += 1
        self.started.set()

    def stop(self):
        self.stop_calls += 1

    def tick(self):
        self.tick_calls += 1
        self.ticked.set()

    @contextmanager
    def session_context(self):
        yield self.session


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

    def test_sdk_command_uses_body_adapter_and_tracks_request(self):
        body = FakeBody()
        orchestrator = FakeOrchestrator()
        with (
            patch.dict(os.environ, {"SDK_API_TOKEN": ""}),
            patch.object(server, "body", body),
            patch.object(server, "orchestrator", orchestrator),
        ):
            client = TestClient(server.app, base_url="http://127.0.0.1:8080", client=("127.0.0.1", 50000))
            response = client.post("/api/sdk/command", json={"command": "jump", "args": {}})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["request_id"], "fake-1")
        self.assertEqual(body.commands, [("jump", {})])
        self.assertEqual(orchestrator.session.commands, [("jump", "fake-1", {}, "sdk")])

    def test_sdk_command_rejects_disconnected_body(self):
        with (
            patch.dict(os.environ, {"SDK_API_TOKEN": ""}),
            patch.object(server, "body", FakeBody(False)),
        ):
            client = TestClient(server.app, base_url="http://127.0.0.1:8080", client=("127.0.0.1", 50000))
            response = client.post("/api/sdk/command", json={"command": "jump"})

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["detail"], "Companion body is not connected")

    def test_server_lifecycle_uses_only_body_adapter_methods(self):
        body = FakeBody(False)
        orchestrators = []

        def create_orchestrator(active_body, **options):
            instance = FakeOrchestrator(active_body, **options)
            orchestrators.append(instance)
            return instance

        with (
            patch.object(server, "body", None),
            patch.object(server, "orchestrator", None),
            patch.object(server, "connection_thread", None),
            patch.object(server, "create_body", return_value=body) as factory,
            patch.object(server, "Orchestrator", side_effect=create_orchestrator),
            patch.object(server, "_apply_config_to_llm_services"),
            patch.object(server, "_apply_persona_to_session"),
        ):
            asyncio.run(server.startup())
            self.assertTrue(orchestrators[0].started.wait(1.0))
            self.assertTrue(orchestrators[0].ticked.wait(1.0))
            asyncio.run(server.shutdown())

        factory.assert_called_once_with("127.0.0.1", 25568)
        self.assertEqual(body.connect_calls, 1)
        self.assertEqual(body.disconnect_calls, 1)
        self.assertGreater(orchestrators[0].tick_calls, 0)
        self.assertEqual(orchestrators[0].stop_calls, 1)
        self.assertEqual(orchestrators[0].session.stop_calls, 1)


if __name__ == "__main__":
    unittest.main()
