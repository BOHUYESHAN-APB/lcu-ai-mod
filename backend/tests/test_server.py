import asyncio
import os
import tempfile
import threading
import unittest
from contextlib import contextmanager, nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import server
from agent.agent_state import AgentStateDB
from agent.config_store import ConfigStore
from agent.session import Session
from agent.task_coordinator import TaskCoordinator
from protocol import BodyEvent


class FakeBody:
    def __init__(self, connected=True):
        self.is_connected = connected
        self.commands = []
        self.connect_calls = 0
        self.disconnect_calls = 0
        self.event_callback = None

    def connect(self):
        self.connect_calls += 1
        self.is_connected = True
        return True

    def disconnect(self):
        self.disconnect_calls += 1
        self.is_connected = False

    def send_command(self, command, args=None, request_id=None):
        self.commands.append((command, args or {}))
        resolved_id = request_id or f"fake-{len(self.commands)}"
        if command in {"control_external", "control_builtin"} and self.event_callback:
            self.event_callback(BodyEvent("response", {
                "type": "response", "id": resolved_id, "success": True, "data": {},
            }))
        return resolved_id

    def set_event_callback(self, callback):
        self.event_callback = callback

    def drain(self):
        return []


class FakeSession:
    def __init__(self):
        self.commands = []
        self.stop_calls = 0
        self.control_mode = "builtin"
        self.control_fencing_token = 0
        self.identity = SimpleNamespace(server_id="default", world_id="default")
        self.llm = FakeLLM()

    def register_external_command(self, command, request_id, args, requester):
        self.commands.append((command, request_id, args, requester))

    def unregister_external_command(self, request_id):
        self.commands = [command for command in self.commands if command[1] != request_id]

    def stop(self):
        self.stop_calls += 1

    def set_control_mode(self, mode, fencing_token=0):
        self.control_mode = mode
        self.control_fencing_token = fencing_token if mode == "external" else 0

    def is_busy_for_external_task(self):
        return False


class FakeLLM:
    def __init__(self, usage=None):
        self.usage = usage or {"total_tokens": 123, "request_count": 2}
        self.configs = {}

    def get_usage(self):
        return dict(self.usage)

    def set_agent_config(self, agent, config):
        self.configs[agent] = dict(config)

    def get_agent_config(self, agent=None, redact=True):
        return {"model": "fake-summary-model", "api_key": "***" if redact else "fake"}

    def chat(self, messages, agent=None, **kwargs):
        self.usage = {
            "total_tokens": 10,
            "request_count": 1,
            "recent_requests": [{"agent": agent or "default", "prompt_tokens": 8, "completion_tokens": 2}],
        }
        return {"role": "assistant", "content": "Alice saved a diamond mining location.", "finish_reason": "stop"}

    def is_configured(self, agent=None):
        return True

    def build_system_prompt(self, context, commands_docs=""):
        return "You are a private conversation companion."


class FakeOrchestrator:
    def __init__(self, body=None, **_options):
        self.body = body
        self.session = FakeSession()
        self.started = threading.Event()
        self.ticked = threading.Event()
        self.start_calls = 0
        self.stop_calls = 0
        self.tick_calls = 0
        self.task_coordinator = None

    def set_task_coordinator(self, coordinator):
        self.task_coordinator = coordinator

    def start(self):
        self.start_calls += 1
        self.started.set()

    def stop(self):
        self.stop_calls += 1

    def tick(self):
        self.tick_calls += 1
        self.ticked.set()

    def on_body_disconnect(self):
        if self.task_coordinator:
            self.task_coordinator.on_disconnect()

    @contextmanager
    def session_context(self):
        yield self.session


class ServerSDKTests(unittest.TestCase):
    def test_player_conversation_round_trip_is_persistent_idempotent_and_action_isolated(self):
        with tempfile.TemporaryDirectory() as tmp:
            body = FakeBody(False)
            session = Session(body, companion_id="player-chat", storage_root=Path(tmp), legacy_root=None)
            session.llm.close()
            session.llm = FakeLLM()
            orchestrator = FakeOrchestrator()
            orchestrator.session = session
            payload = {
                "player_id": "uuid-alice",
                "player_name": "Alice",
                "message": "Can we talk privately?",
                "client_message_id": "client-message-1",
                "server_id": "example.org",
            }
            try:
                with (
                    patch.dict(os.environ, {"PLAYER_API_TOKEN": "player-secret", "SDK_API_TOKEN": "operator-secret"}),
                    patch.object(server, "orchestrator", orchestrator),
                ):
                    client = TestClient(server.app)
                    headers = {"Authorization": "Bearer player-secret"}
                    sent = client.post("/api/player/v1/messages", json=payload, headers=headers)
                    duplicate = client.post("/api/player/v1/messages", json=payload, headers=headers)
                    contacts = client.get("/api/player/v1/contacts", headers=headers)
                    conversation_id = sent.json()["conversation_id"]
                    history = client.get(
                        f"/api/player/v1/conversations/{conversation_id}/messages", headers=headers,
                    )
                    operator_contacts = client.get(
                        "/api/v2/inbox/contacts",
                        headers={"Authorization": "Bearer operator-secret"},
                    )
                    operator_history = client.get(
                        f"/api/v2/inbox/conversations/{conversation_id}/messages",
                        headers={"Authorization": "Bearer operator-secret"},
                    )
                    wrong_token = client.get(
                        "/api/player/v1/contacts",
                        headers={"Authorization": "Bearer operator-secret"},
                    )
            finally:
                session.stop()

        self.assertEqual(sent.status_code, 200)
        self.assertEqual(sent.json()["reply"], "Alice saved a diamond mining location.")
        self.assertEqual(duplicate.json(), sent.json())
        self.assertEqual(len(contacts.json()["contacts"]), 1)
        self.assertEqual(len(history.json()["messages"]), 2)
        self.assertEqual(operator_contacts.json()["contacts"], contacts.json()["contacts"])
        self.assertEqual(operator_history.json()["messages"], history.json()["messages"])
        self.assertEqual([item["is_ai"] for item in history.json()["messages"]], [0, 1])
        self.assertEqual(body.commands, [])
        self.assertEqual(wrong_token.status_code, 401)

    def test_memory_v2_browse_detail_and_export_use_active_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = Session(
                FakeBody(False), companion_id="memory-api", persistence_scope="world",
                server_id="example.org", world_id="survival", storage_root=Path(tmp), legacy_root=None,
            )
            session.message_db.add_message("Alice", "remember diamonds", metadata={"secret": "hidden"})
            session.memory.save_location("mine", 4, 12, 8, description="diamond level")
            orchestrator = FakeOrchestrator()
            orchestrator.session = session
            try:
                with (
                    patch.dict(os.environ, {"SDK_API_TOKEN": ""}),
                    patch.object(server, "orchestrator", orchestrator),
                ):
                    client = TestClient(
                        server.app, base_url="http://127.0.0.1:8080", client=("127.0.0.1", 50000),
                    )
                    status = client.get("/api/v2/memory/status")
                    listed = client.get("/api/v2/memory/records", params={"q": "diamonds", "category": "message"})
                    record_id = listed.json()["records"][0]["id"]
                    detail = client.get(f"/api/v2/memory/records/{record_id}")
                    exported = client.post("/api/v2/memory/exports", json={
                        "categories": ["location"], "format": "json", "include_provenance": False,
                    })
                    invalid = client.get("/api/v2/memory/records", params={"category": "unknown"})
            finally:
                session.stop()

        self.assertEqual(status.status_code, 200)
        self.assertEqual(status.json()["scope"]["world_id"], "survival")
        self.assertFalse(status.json()["repository"]["production_ready"])
        self.assertEqual(listed.json()["count"], 1)
        self.assertEqual(detail.json()["content"]["metadata"]["secret"], "***")
        self.assertIn("attachment", exported.headers["content-disposition"])
        self.assertNotIn("provenance", exported.json()["records"][0])
        self.assertEqual(invalid.status_code, 422)

    def test_memory_summary_preview_and_commit_preserve_source_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = Session(
                FakeBody(False), companion_id="memory-summary", storage_root=Path(tmp), legacy_root=None,
            )
            session.llm.close()
            session.llm = FakeLLM()
            session.memory.save_location("mine", 4, 12, 8, description="diamond level")
            orchestrator = FakeOrchestrator()
            orchestrator.session = session
            try:
                with (
                    patch.dict(os.environ, {"SDK_API_TOKEN": ""}),
                    patch.object(server, "orchestrator", orchestrator),
                    patch.object(server, "_configuration_guard", lambda: nullcontext()),
                    patch.object(server, "memory_preview_store", server.MemoryPreviewStore()),
                ):
                    client = TestClient(
                        server.app, base_url="http://127.0.0.1:8080", client=("127.0.0.1", 50000),
                    )
                    preview = client.post("/api/v2/memory/compression/previews", json={
                        "categories": ["location"], "summary_model_agent": "default", "target_tokens": 128,
                    })
                    committed = client.post("/api/v2/memory/compression/runs", json={
                        "preview_id": preview.json()["id"],
                    })
                    records = client.get("/api/v2/memory/records", params={"category": "location"})
            finally:
                session.stop()

        self.assertEqual(preview.status_code, 200)
        self.assertTrue(preview.json()["source_records_retained"])
        self.assertEqual(committed.status_code, 200)
        self.assertTrue(committed.json()["summary"]["source_records_retained"])
        self.assertEqual(records.json()["count"], 1)

    def test_memory_lifecycle_and_retention_complete_reversible_sqlite_loop(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = Session(FakeBody(False), companion_id="memory-loop", storage_root=Path(tmp), legacy_root=None)
            session.memory.save_location("old-mine", 1, 12, 3, description="test")
            session.memory.locations["old-mine"]["saved_at"] = 1.0
            orchestrator = FakeOrchestrator()
            orchestrator.session = session
            preview_store = server.MemoryPreviewStore()
            try:
                with (
                    patch.dict(os.environ, {"SDK_API_TOKEN": ""}),
                    patch.object(server, "orchestrator", orchestrator),
                    patch.object(server, "_configuration_guard", lambda: nullcontext()),
                    patch.object(server, "memory_preview_store", preview_store),
                ):
                    client = TestClient(
                        server.app, base_url="http://127.0.0.1:8080", client=("127.0.0.1", 50000),
                    )

                    def preview_and_run(action):
                        preview = client.post("/api/v2/memory/actions/previews", json={
                            "action": action, "categories": ["location"],
                        })
                        self.assertEqual(preview.status_code, 200, preview.text)
                        data = preview.json()
                        run = client.post("/api/v2/memory/actions", json={
                            "preview_id": data["id"],
                            "confirmation_token": data["confirmation_token"],
                            "confirmation_text": data["confirmation_text"],
                        })
                        self.assertEqual(run.status_code, 200, run.text)

                    preview_and_run("archive")
                    archived = client.get("/api/v2/memory/records", params={"category": "location", "state": "archived"})
                    preview_and_run("delete")
                    deleted = client.get("/api/v2/memory/records", params={"category": "location", "state": "deleted"})
                    preview_and_run("restore")
                    restored = client.get("/api/v2/memory/records", params={"category": "location", "state": "active"})
                    audit = client.get("/api/v2/memory/audit")

                    retention = client.patch("/api/v2/memory/retention", json={
                        "expected_version": 0,
                        "rules": [{
                            "category": "location", "archive_after_days": 0,
                            "delete_after_days": 1, "min_keep": 0,
                        }],
                    })
                    retention_preview = client.post("/api/v2/memory/retention/previews")
                    rp = retention_preview.json()
                    retention_run = client.post("/api/v2/memory/retention/runs", json={
                        "preview_id": rp["id"],
                        "confirmation_token": rp["confirmation_token"],
                        "confirmation_text": rp["confirmation_text"],
                    })
                    retained = client.get("/api/v2/memory/records", params={"category": "location", "state": "archived"})
            finally:
                session.stop()

        self.assertEqual(archived.json()["count"], 1)
        self.assertEqual(deleted.json()["count"], 1)
        self.assertEqual(restored.json()["count"], 1)
        self.assertEqual(len(audit.json()["items"]), 3)
        self.assertEqual(retention.status_code, 200)
        self.assertEqual(retention_preview.status_code, 200)
        self.assertEqual(retention_run.status_code, 200)
        self.assertEqual(retained.json()["count"], 1)

    def test_llm_config_reports_usage_from_active_session(self):
        orchestrator = FakeOrchestrator()
        orchestrator.session.llm = FakeLLM({"total_tokens": 987, "request_count": 4})
        with (
            patch.dict(os.environ, {"SDK_API_TOKEN": ""}),
            patch.object(server, "orchestrator", orchestrator),
            patch.object(server.llm_service, "get_usage", return_value={"total_tokens": 1}),
        ):
            response = TestClient(
                server.app, base_url="http://127.0.0.1:8080", client=("127.0.0.1", 50000),
            ).get("/api/llm/config")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["usage"]["total_tokens"], 987)

    def test_llm_config_round_trips_canonical_budget_and_rejects_invalid_limits(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ConfigStore(Path(tmp) / "config.json")
            orchestrator = FakeOrchestrator()
            payload = {
                "agent": "planner",
                "provider": "openai",
                "context_window_tokens": 8192,
                "max_input_tokens": 6144,
                "max_output_tokens": 1024,
                "reserved_output_tokens": 1024,
                "max_request_bytes": 262144,
                "compression_enabled": True,
                "compression_trigger_tokens": 5000,
                "compression_target_tokens": 3000,
                "recent_messages_to_keep": 8,
                "summary_model_agent": "default",
                "summary_max_output_tokens": 512,
                "temperature": 0.4,
            }
            with (
                patch.dict(os.environ, {"SDK_API_TOKEN": ""}),
                patch.object(server, "config_store", store),
                patch.object(server, "orchestrator", orchestrator),
                patch.object(server, "_configuration_guard", lambda: nullcontext()),
                patch.object(server, "_apply_config_to_llm_services"),
            ):
                client = TestClient(
                    server.app, base_url="http://127.0.0.1:8080", client=("127.0.0.1", 50000),
                )
                saved = client.post("/api/llm/config", json=payload)
                rejected = client.post("/api/llm/config", json={
                    "agent": "planner", "max_output_tokens": 2048, "reserved_output_tokens": 1024,
                })

            self.assertEqual(saved.status_code, 200)
            self.assertEqual(saved.json()["config"]["max_output_tokens"], 1024)
            self.assertNotIn("max_tokens", saved.json()["config"])
            self.assertEqual(rejected.status_code, 400)
            self.assertEqual(store.get_agent_llm_config("planner", redact=False)["max_output_tokens"], 1024)

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

    def test_cors_preflight_allows_schedule_patch(self):
        client = TestClient(server.app)
        response = client.options(
            "/api/v2/schedules/example",
            headers={
                "Origin": "http://localhost",
                "Access-Control-Request-Method": "PATCH",
                "Access-Control-Request-Headers": "Authorization,Content-Type",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("PATCH", response.headers["access-control-allow-methods"])

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
            body.set_event_callback(server._capture_control_response)
            orchestrator = FakeOrchestrator(body)
            coordinator = TaskCoordinator(state, server.skill_registry, body)
            coordinator.set_body_armed(True)
            with (
                patch.dict(os.environ, {"SDK_API_TOKEN": ""}),
                patch.object(server, "agent_state", state),
                patch.object(server, "body", body),
                patch.object(server, "orchestrator", orchestrator),
                patch.object(server, "task_coordinator", coordinator),
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
                schedule = client.post("/api/v2/schedules", json={
                    "name": "jump interval",
                    "skill_id": "core.jump",
                    "input": {},
                    "clock": "wall",
                    "trigger_type": "interval",
                    "wall_interval_seconds": 60,
                    "lease_id": lease["id"],
                    "fencing_token": lease["fencing_token"],
                })
                events = client.get("/api/v2/events?after=0")
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
        self.assertEqual(run.json()["status"], "dispatched")
        self.assertEqual(schedule.status_code, 200)
        self.assertEqual(events.status_code, 200)
        self.assertGreater(events.json()["next_cursor"], 0)
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

    def test_v2_task_presets_list_detail_and_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = AgentStateDB(Path(tmp) / "agent_state.db")
            body = FakeBody()
            orchestrator = FakeOrchestrator(body)
            coordinator = TaskCoordinator(state, server.skill_registry, body)
            coordinator.set_body_armed(True)
            with (
                patch.dict(os.environ, {"SDK_API_TOKEN": ""}),
                patch.object(server, "agent_state", state),
                patch.object(server, "body", body),
                patch.object(server, "orchestrator", orchestrator),
                patch.object(server, "task_coordinator", coordinator),
            ):
                client = TestClient(server.app, base_url="http://127.0.0.1:8080", client=("127.0.0.1", 50000))
                listed = client.get("/api/v2/task-presets?category=crafting")
                detail = client.get("/api/v2/task-presets/craft.iron_pickaxe")
                run = client.post("/api/v2/task-presets/craft.iron_pickaxe/runs", json={"parameters": {}})
            state.close()

        self.assertEqual(listed.status_code, 200)
        self.assertGreaterEqual(listed.json()["count"], 1)
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.json()["id"], "craft.iron_pickaxe")
        self.assertEqual(run.status_code, 200)
        self.assertEqual(run.json()["skill_id"], "general.craft_item")
        self.assertEqual(body.commands, [("craft_item", {"item": "minecraft:iron_pickaxe", "count": 1})])

    def test_v2_workflow_preset_returns_parent_with_step_details(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = AgentStateDB(Path(tmp) / "agent_state.db")
            body = FakeBody()
            orchestrator = FakeOrchestrator(body)
            coordinator = TaskCoordinator(state, server.skill_registry, body)
            coordinator.set_body_armed(True)
            with (
                patch.dict(os.environ, {"SDK_API_TOKEN": ""}),
                patch.object(server, "agent_state", state),
                patch.object(server, "body", body),
                patch.object(server, "orchestrator", orchestrator),
                patch.object(server, "task_coordinator", coordinator),
            ):
                client = TestClient(server.app, base_url="http://127.0.0.1:8080", client=("127.0.0.1", 50000))
                preset = client.get("/api/v2/task-presets/workflow.starter_chest")
                created = client.post(
                    "/api/v2/task-presets/workflow.starter_chest/runs", json={"parameters": {}},
                )
                detail = client.get(f"/api/v2/runs/{created.json()['id']}")
                listed = client.get("/api/v2/runs")
            state.close()

        self.assertEqual(preset.status_code, 200)
        self.assertEqual(preset.json()["kind"], "workflow")
        self.assertEqual(created.status_code, 200)
        self.assertEqual(created.json()["run_kind"], "workflow")
        self.assertEqual([step["status"] for step in detail.json()["steps"]], ["dispatched", "pending"])
        self.assertEqual(listed.json()["count"], 1)
        self.assertEqual(body.commands, [(
            "collect_blocks", {"block_type": "#minecraft:logs", "count": 8},
        )])

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
