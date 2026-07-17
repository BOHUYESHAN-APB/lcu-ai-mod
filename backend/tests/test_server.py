import asyncio
import os
import tempfile
import threading
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import server
from agent.agent_state import AgentStateDB


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
        self.control_mode = "builtin"
        self.control_fencing_token = 0

    def register_external_command(self, command, request_id, args, requester):
        self.commands.append((command, request_id, args, requester))

    def stop(self):
        self.stop_calls += 1

    def set_control_mode(self, mode, fencing_token=0):
        self.control_mode = mode
        self.control_fencing_token = fencing_token if mode == "external" else 0


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

    def test_v2_external_lease_fences_actions_and_runs_typed_skill(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = AgentStateDB(Path(tmp) / "agent_state.db")
            body = FakeBody()
            orchestrator = FakeOrchestrator(body)
            with (
                patch.dict(os.environ, {"SDK_API_TOKEN": ""}),
                patch.object(server, "agent_state", state),
                patch.object(server, "body", body),
                patch.object(server, "orchestrator", orchestrator),
            ):
                client = TestClient(
                    server.app,
                    base_url="http://127.0.0.1:8080",
                    client=("127.0.0.1", 50000),
                )
                skills = client.get("/api/v2/skills?category=general")
                acquired = client.post("/api/v2/control/leases", json={
                    "owner": "roleplay-agent",
                    "mode": "external",
                    "ttl_seconds": 30,
                })
                lease = acquired.json()["lease"]
                mode_during_lease = orchestrator.session.control_mode
                legacy = client.post("/api/sdk/command", json={"command": "jump"})
                reserved = client.post("/api/sdk/command", json={
                    "command": "control_external",
                    "args": {"__lcu_fencing_token": 999999},
                })
                persona = client.post("/api/persona", json={"name": "intruder"})
                unfenced = client.post("/api/v2/skills/core.jump/runs", json={"input": {}})
                run = client.post("/api/v2/skills/general.craft_item/runs", json={
                    "input": {"item": "minecraft:torch", "count": 8},
                    "lease_id": lease["id"],
                    "fencing_token": lease["fencing_token"],
                })
                released = client.post(
                    f"/api/v2/control/leases/{lease['id']}/release",
                    json={"fencing_token": lease["fencing_token"]},
                )
            state.close()

        self.assertEqual(skills.status_code, 200)
        self.assertGreater(skills.json()["count"], 0)
        self.assertEqual(acquired.status_code, 200)
        self.assertEqual(mode_during_lease, "external")
        self.assertEqual(orchestrator.session.control_mode, "builtin")
        self.assertEqual(legacy.status_code, 409)
        self.assertEqual(reserved.status_code, 403)
        self.assertEqual(persona.status_code, 409)
        self.assertEqual(unfenced.status_code, 409)
        self.assertEqual(run.status_code, 200)
        self.assertEqual(run.json()["skill_id"], "general.craft_item")
        self.assertEqual(released.status_code, 200)
        self.assertEqual(body.commands, [
            ("control_external", {"__lcu_fencing_token": lease["fencing_token"]}),
            ("craft_item", {
                "item": "minecraft:torch",
                "count": 8,
                "__lcu_fencing_token": lease["fencing_token"],
            }),
            ("control_builtin", {"__lcu_fencing_token": lease["fencing_token"]}),
        ])

    def test_expired_lease_restores_session_while_body_is_disconnected(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = AgentStateDB(Path(tmp) / "agent_state.db")
            lease = state.acquire_lease(
                "roleplay-agent",
                "external",
                ["persona", "memory", "planner", "autonomy", "actions"],
                30,
            )
            body = FakeBody(False)
            orchestrator = FakeOrchestrator(body)
            with (
                patch.object(server, "agent_state", state),
                patch.object(server, "body", body),
                patch.object(server, "orchestrator", orchestrator),
            ):
                server._reconcile_control_mode()
                self.assertEqual(orchestrator.session.control_mode, "external")
                with patch("agent.agent_state.time.time", return_value=lease["expires_at"] + 1):
                    server._reconcile_control_mode()
            state.close()

        self.assertEqual(orchestrator.session.control_mode, "builtin")


if __name__ == "__main__":
    unittest.main()
