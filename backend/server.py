"""
FastAPI web server.
Serves dashboard, WebSocket, and REST API for the Session-based architecture.
"""

import json
import os
import secrets
import threading
import time
import uuid
import ipaddress
from contextlib import contextmanager, nullcontext
from pathlib import Path
from typing import Any, Literal, Optional, cast
from urllib.parse import urlparse

import asyncio

from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agent import LLMService
from agent.agent_state import AgentStateDB, LeaseConflictError, LeaseNotFoundError
from agent.config_store import ConfigStore, DEFAULT_CONFIG_PATH
from agent.memory_catalog import MEMORY_CATEGORIES, MemoryCatalog, MemoryQuery
from agent.memory_management import MemoryPreviewError, MemoryPreviewStore, evaluate_retention
from agent.memory_overlay import MEMORY_STATES, RetentionConflictError
from agent.llm_service import LLMRequestRejected
from agent.orchestrator import Orchestrator
from agent.skill_registry import SkillRegistry, SkillValidationError
from agent.storage_policy import enforce_storage_policy
from agent.task_coordinator import TaskCoordinator
from protocol import BodyAdapter, WireClient

SDK_API_VERSION = "1"
SDK_V2_API_VERSION = "2"
app = FastAPI(title="LCUMod Backend", version="0.1.0")

storage_policy = enforce_storage_policy()
CONFIG_PATH = DEFAULT_CONFIG_PATH
config_store = ConfigStore(CONFIG_PATH)


def _sdk_api_token() -> str:
    return os.getenv("SDK_API_TOKEN", "").strip()


def _player_api_token() -> str:
    return os.getenv("PLAYER_API_TOKEN", "").strip()


def _allowed_origins() -> list[str]:
    configured = os.getenv("SDK_ALLOWED_ORIGINS", "")
    if configured.strip():
        return [origin.strip() for origin in configured.split(",") if origin.strip()]
    integration = config_store.raw(redact=True).get("integration", {})
    return list(integration.get("allowed_origins", []))


def _is_loopback(host: str | None) -> bool:
    if not host:
        return False
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host.lower() == "localhost"


def _local_authority(authority: str | None) -> bool:
    if not authority:
        return False
    return _is_loopback(urlparse(f"//{authority}").hostname)


def _valid_token(candidate: str | None, client_host: str | None = None,
                 request_host: str | None = None) -> bool:
    expected = _sdk_api_token()
    if not expected:
        return _is_loopback(client_host) and _local_authority(request_host)
    return bool(candidate) and secrets.compare_digest(candidate, expected)


def _request_token(request: Request) -> str | None:
    authorization = request.headers.get("authorization", "")
    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return None


def _origin_allowed(origin: str | None, server_url: str | None = None) -> bool:
    if not origin:
        return True
    if origin in _allowed_origins():
        return True
    if not server_url:
        return False
    parsed_origin = urlparse(origin)
    parsed_server = urlparse(server_url)
    origin_port = parsed_origin.port or (443 if parsed_origin.scheme == "https" else 80)
    server_port = parsed_server.port or (443 if parsed_server.scheme == "wss" else 80)
    expected_origin_scheme = "https" if parsed_server.scheme == "wss" else "http"
    return (
        parsed_origin.scheme == expected_origin_scheme
        and parsed_origin.hostname == parsed_server.hostname
        and origin_port == server_port
    )


def _unauthorized_response(request: Request) -> JSONResponse:
    response = JSONResponse({"detail": "Invalid or missing SDK token"}, status_code=401)
    origin = request.headers.get("origin")
    if origin and origin in _allowed_origins():
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Vary"] = "Origin"
    return response


app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins(),
    allow_credentials=False,
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)


@app.middleware("http")
async def sdk_authentication(request: Request, call_next):
    if request.method != "OPTIONS" and request.url.path.startswith("/api/"):
        client_host = request.client.host if request.client else None
        if request.url.path.startswith("/api/player/"):
            expected = _player_api_token()
            candidate = _request_token(request)
            allowed = (
                bool(expected) and bool(candidate) and secrets.compare_digest(candidate, expected)
            ) or (
                not expected and _is_loopback(client_host) and _local_authority(request.headers.get("host"))
            )
        else:
            allowed = _valid_token(_request_token(request), client_host, request.headers.get("host"))
        if not allowed:
            return _unauthorized_response(request)
    return await call_next(request)


class SDKContextRequest(BaseModel):
    external_context: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def accept_legacy_raw_context(cls, data: Any) -> Any:
        if isinstance(data, dict) and "external_context" not in data:
            return {"external_context": data}
        return data


class SDKChatRequest(BaseModel):
    message: str = Field(min_length=1)
    sender: str = "sdk"


class PlayerConversationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    player_id: str = Field(min_length=1, max_length=128)
    player_name: str = Field(min_length=1, max_length=64)
    message: str = Field(min_length=1, max_length=2000)
    client_message_id: str = Field(min_length=1, max_length=128)
    server_id: str = Field(default="unknown", max_length=256)


class SDKCommandRequest(BaseModel):
    command: str = Field(min_length=1)
    args: dict[str, Any] = Field(default_factory=dict)


class ControlLeaseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    owner: str = Field(min_length=1, max_length=128)
    mode: Literal["external"] = "external"
    owns: list[str] = Field(default_factory=lambda: ["persona", "memory", "planner", "autonomy", "actions"])
    ttl_seconds: int = Field(default=30, ge=5, le=300)

    @field_validator("owner")
    @classmethod
    def validate_owner(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("owner must not be blank")
        return value


class ControlLeaseHeartbeat(BaseModel):
    fencing_token: int = Field(ge=1)
    ttl_seconds: int = Field(default=30, ge=5, le=300)


class ControlLeaseRelease(BaseModel):
    fencing_token: int = Field(ge=1)


class LeaseCredentials(BaseModel):
    model_config = ConfigDict(extra="forbid")
    lease_id: str | None = None
    fencing_token: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def require_complete_lease_credentials(self):
        if (self.lease_id is None) != (self.fencing_token is None):
            raise ValueError("lease_id and fencing_token must be supplied together")
        return self


class SkillRunRequest(LeaseCredentials):
    input: dict[str, Any] = Field(default_factory=dict)


class ScheduleRequest(LeaseCredentials):
    name: str = Field(min_length=1, max_length=128)
    skill_id: str = Field(min_length=1)
    input: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True
    clock: Literal["wall", "game"]
    trigger_type: Literal["once", "interval", "time_of_day"]
    misfire_policy: Literal["skip", "fire_once"] = "fire_once"
    wall_run_at: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    wall_interval_seconds: float | None = Field(default=None, ge=1, allow_inf_nan=False)
    game_interval_ticks: int | None = Field(default=None, ge=20)
    time_of_day_tick: int | None = Field(default=None, ge=0, lt=24000)


class ScheduleUpdateRequest(LeaseCredentials):
    enabled: bool


class RunCancelRequest(LeaseCredentials):
    pass


class MemoryExportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    q: str = Field(default="", max_length=500)
    categories: list[str] = Field(default_factory=list)
    player: str = Field(default="", max_length=128)
    format: Literal["json", "jsonl"] = "jsonl"
    include_provenance: bool = True
    states: list[Literal["active", "archived", "deleted"]] = Field(default_factory=lambda: ["active"])
    record_ids: list[str] = Field(default_factory=list, max_length=500)

    @field_validator("categories")
    @classmethod
    def validate_categories(cls, values: list[str]) -> list[str]:
        unknown = sorted(set(values) - MEMORY_CATEGORIES)
        if unknown:
            raise ValueError(f"unknown memory categories: {', '.join(unknown)}")
        return sorted(set(values))


class MemoryCompressionPreviewRequest(MemoryExportRequest):
    format: Literal["json", "jsonl"] = "jsonl"
    include_provenance: bool = True
    summary_model_agent: str = Field(default="default", min_length=1, max_length=128)
    target_tokens: int = Field(default=1024, ge=64, le=4096)


class MemoryCompressionRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    preview_id: str = Field(min_length=1)


class MemoryActionPreviewRequest(MemoryExportRequest):
    action: Literal["archive", "delete", "restore"]
    states: list[Literal["active", "archived", "deleted"]] = Field(default_factory=list)
    allow_active: bool = False
    reason: str = Field(default="", max_length=500)


class MemoryActionRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    preview_id: str = Field(min_length=1)
    confirmation_token: str = Field(min_length=1)
    confirmation_text: str = Field(min_length=1)


class RetentionRule(BaseModel):
    model_config = ConfigDict(extra="forbid")
    category: str
    enabled: bool = True
    archive_after_days: float | None = Field(default=None, ge=0)
    delete_after_days: float | None = Field(default=None, ge=0)
    min_keep: int = Field(default=0, ge=0)

    @field_validator("category")
    @classmethod
    def validate_category(cls, value: str) -> str:
        if value not in MEMORY_CATEGORIES:
            raise ValueError(f"unknown memory category: {value}")
        return value

    @model_validator(mode="after")
    def validate_order(self):
        if self.archive_after_days is None and self.delete_after_days is None:
            raise ValueError("retention rule must archive or delete records")
        if (
            self.archive_after_days is not None
            and self.delete_after_days is not None
            and self.delete_after_days <= self.archive_after_days
        ):
            raise ValueError("delete_after_days must be greater than archive_after_days")
        return self


class RetentionUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=0)
    rules: list[RetentionRule]

    @field_validator("rules")
    @classmethod
    def unique_categories(cls, values: list[RetentionRule]) -> list[RetentionRule]:
        categories = [value.category for value in values]
        if len(categories) != len(set(categories)):
            raise ValueError("retention categories must be unique")
        return values

def create_body(host: str, port: int) -> BodyAdapter:
    """Construct the configured companion body."""
    return WireClient(host, port)


# Global state
body: BodyAdapter | None = None
orchestrator: Orchestrator | None = None
llm_service = LLMService()
skill_registry = SkillRegistry()
agent_state = AgentStateDB()
agent_state.sync_skills(skill_registry.list())
task_coordinator: TaskCoordinator | None = None
memory_preview_store = MemoryPreviewStore()
connected_browsers: list[WebSocket] = []
shutdown_event = threading.Event()
connection_thread: threading.Thread | None = None
control_transition_lock = threading.RLock()
control_response_lock = threading.Lock()
control_response_waiters: dict[str, tuple[threading.Event, dict[str, Any]]] = {}

CONTROL_DOMAINS = {"persona", "memory", "planner", "autonomy", "actions"}
RESERVED_BODY_COMMANDS = {"control_external", "control_builtin"}
FENCING_FIELD = "__lcu_fencing_token"


def _capture_control_response(event) -> None:
    if event.type != "response":
        return
    request_id = str(event.data.get("id", ""))
    with control_response_lock:
        waiter = control_response_waiters.get(request_id)
        if waiter:
            waiter[1]["response"] = event.data
            waiter[0].set()


def _send_control_command(command: str, fencing_token: int) -> bool:
    active_body = body
    if not active_body or not active_body.is_connected:
        return False
    request_id = str(uuid.uuid4())
    ready = threading.Event()
    result: dict[str, Any] = {}
    with control_response_lock:
        control_response_waiters[request_id] = (ready, result)
    try:
        active_body.send_command(command, {FENCING_FIELD: fencing_token}, request_id=request_id)
        if not ready.wait(3.0):
            return False
        response = result.get("response", {})
        return bool(response.get("success", False))
    except ConnectionError:
        return False
    finally:
        with control_response_lock:
            control_response_waiters.pop(request_id, None)


def _apply_control_mode(mode: str, fencing_token: int = 0, force_body: bool = False) -> bool:
    active_body = body
    active_orchestrator = orchestrator
    changed = force_body
    if active_orchestrator:
        with active_orchestrator.session_context() as session:
            previous_token = session.control_fencing_token
            changed = changed or session.control_mode != mode \
                or (mode == "external" and previous_token != fencing_token)
            token = fencing_token if mode == "external" else previous_token or fencing_token
        if active_body and active_body.is_connected and changed:
            command = "control_external" if mode == "external" else "control_builtin"
            if not _send_control_command(command, token):
                return False
        with active_orchestrator.session_context() as session:
            session.set_control_mode(mode, fencing_token)
            return True
    if active_body and active_body.is_connected and changed:
        command = "control_external" if mode == "external" else "control_builtin"
        if not _send_control_command(command, fencing_token):
            return False
    return True


def _reconcile_control_mode(force_body: bool = False) -> dict[str, Any] | None:
    with control_transition_lock:
        session_guard = orchestrator.session_context() if orchestrator else nullcontext(None)
        with session_guard as session:
            gate = task_coordinator.coordination_guard() if task_coordinator else nullcontext()
            with gate:
                lease = agent_state.get_active_lease()
                token = lease["fencing_token"] if lease else agent_state.latest_fencing_token()
                desired_mode = lease["mode"] if lease else "builtin"
                current_mode = session.control_mode if session else "builtin"
                if desired_mode != current_mode and task_coordinator:
                    task_coordinator.on_control_transition()
                _apply_control_mode(desired_mode, token, force_body=force_body)
    return lease


def _fenced_args(args: dict[str, Any], lease: dict[str, Any] | None) -> dict[str, Any]:
    command_args = dict(args)
    command_args.pop(FENCING_FIELD, None)
    if lease:
        command_args[FENCING_FIELD] = lease["fencing_token"]
    return command_args


def _validate_public_command(command: str, args: dict[str, Any]) -> None:
    if command in RESERVED_BODY_COMMANDS or FENCING_FIELD in args:
        raise HTTPException(status_code=403, detail="Reserved control command or argument")


@contextmanager
def _configuration_guard():
    with control_transition_lock:
        if agent_state.get_active_lease() is not None:
            raise HTTPException(status_code=409, detail="Configuration is owned by an active control lease")
        yield


def _apply_config_to_llm_services():
    """Sync persisted LLM config into global and session LLM services."""
    default_config = config_store.get_agent_llm_config("default", redact=False)
    llm_service.set_agent_config("default", default_config)
    for agent_name, config in config_store.raw(redact=False).get("llm", {}).get("agents", {}).items():
        llm_service.set_agent_config(agent_name, config)
    if orchestrator and orchestrator.session:
        with orchestrator.session_context() as session:
            for agent_name, config in config_store.raw(redact=False).get("llm", {}).get("agents", {}).items():
                session.llm.set_agent_config(agent_name, config)


def _apply_persona_to_session():
    if not orchestrator or not orchestrator.session:
        return
    persona = config_store.get_persona()
    with orchestrator.session_context() as session:
        session.runtime["persona"] = persona


def _configured_identity() -> dict[str, str]:
    companion = config_store.get_companion_config()
    persistence = companion.get("persistence", {})
    return {
        "companion_id": companion["id"],
        "scope": persistence.get("scope", "global"),
        "server_id": persistence.get("server_id", "default"),
        "world_id": persistence.get("world_id", "default"),
    }

# Static files
STATIC_DIR = Path(__file__).parent / "web" / "static"
STATIC_DIR.mkdir(parents=True, exist_ok=True)


@app.on_event("startup")
async def startup():
    """Start the companion body and orchestrator on startup."""
    global body, orchestrator, task_coordinator, shutdown_event, connection_thread

    host = os.getenv("MOD_HOST", "127.0.0.1")
    port = int(os.getenv("MOD_PORT", "25568"))
    companion = config_store.get_companion_config()
    persistence = companion.get("persistence", {})

    body = create_body(host, port)
    if hasattr(body, "set_event_callback"):
        body.set_event_callback(_capture_control_response)
    orchestrator = Orchestrator(
        body,
        companion_id=os.getenv("COMPANION_ID", companion["id"]),
        persistence_scope=os.getenv("MEMORY_SCOPE", persistence.get("scope", "global")),
        server_id=os.getenv("SERVER_ID", persistence.get("server_id", "default")),
        world_id=os.getenv("WORLD_ID", persistence.get("world_id", "default")),
    )
    task_coordinator = TaskCoordinator(agent_state, skill_registry, body)
    orchestrator.set_task_coordinator(task_coordinator)
    shutdown_event = threading.Event()
    stop_event = shutdown_event
    _apply_config_to_llm_services()
    _apply_persona_to_session()
    _reconcile_control_mode()

    # Store event loop for background thread scheduling
    loop = asyncio.get_event_loop()

    def _broadcast_payload(payload: dict[str, Any]):
        encoded = json.dumps(payload)
        async def _send():
            for ws in connected_browsers.copy():
                try:
                    await ws.send_text(encoded)
                except Exception:
                    if ws in connected_browsers:
                        connected_browsers.remove(ws)
        asyncio.run_coroutine_threadsafe(_send(), loop)

    # Forward canonical body events to browser clients.
    def _broadcast_event(event_type: str, data: dict, occurred_at: float):
        _broadcast_payload({
            "type": "event",
            "event": event_type,
            "companion_id": orchestrator.session.identity.companion_id if orchestrator else None,
            "occurred_at": occurred_at,
            "data": data,
        })

    def _broadcast_chat(sender, message, is_system):
        _broadcast_payload({
            "type": "chat", "sender": sender, "message": message, "is_system": is_system,
        })

    cast(Any, orchestrator).on_chat = _broadcast_chat
    cast(Any, orchestrator).on_event = _broadcast_event

    # Connect in background thread with auto-reconnect
    def _connect_loop():
        active_body = body
        o = orchestrator
        if active_body is None or o is None:
            return
        last_control_check = 0.0
        while not stop_event.is_set():
            _reconcile_control_mode()
            if active_body.connect():
                if stop_event.is_set():
                    active_body.disconnect()
                    break
                print(f"[Backend] Companion body connected at {host}:{port}")
                o.start()
                _reconcile_control_mode(force_body=True)
                # Event loop: drain body events and tick orchestrator
                while active_body.is_connected and not stop_event.is_set():
                    o.tick()
                    now = time.monotonic()
                    if now - last_control_check >= 1.0:
                        _reconcile_control_mode()
                        last_control_check = now
                    time.sleep(0.05)  # 20Hz loop
                o.tick()  # Drain terminal messages queued immediately before socket closure.
                o.stop()
                o.on_body_disconnect()
                if not stop_event.is_set():
                    print("[Backend] Disconnected. Reconnecting...")
            else:
                if task_coordinator:
                    with o.session_context() as session:
                        runtime = dict(session.runtime)
                        control_mode = session.control_mode
                        clock_scope = f"{session.identity.server_id}\0{session.identity.world_id}"
                    task_coordinator.tick(runtime, control_mode, clock_scope)
                if not stop_event.is_set():
                    print(f"[Backend] Mod not available at {host}:{port}. Retry in 5s...")
                    stop_event.wait(5)

    connection_thread = threading.Thread(target=_connect_loop, daemon=True)
    connection_thread.start()


@app.on_event("shutdown")
async def shutdown():
    """Persist companion state and stop the active body connection."""
    shutdown_event.set()
    if body:
        body.disconnect()
    if connection_thread and connection_thread.is_alive():
        await asyncio.to_thread(connection_thread.join, 6.0)
    if orchestrator:
        with orchestrator.session_context() as session:
            session.stop()


# ── Routes ──


@app.get("/", response_class=HTMLResponse)
async def get_dashboard():
    """Serve the dashboard."""
    html_path = STATIC_DIR / "index.html"
    if html_path.exists():
        return HTMLResponse(html_path.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>Dashboard</h1>")


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    """Browser WebSocket: send commands, receive status."""
    authorization = ws.headers.get("authorization", "")
    header_token = authorization[7:].strip() if authorization.lower().startswith("bearer ") else None
    protocols = [item.strip() for item in ws.headers.get("sec-websocket-protocol", "").split(",") if item.strip()]
    token_protocol = next((item for item in protocols if item.startswith("lcu-token.")), None)
    protocol_token = token_protocol.removeprefix("lcu-token.") if token_protocol else None
    client_host = ws.client.host if ws.client else None
    if not _valid_token(header_token or protocol_token, client_host, ws.headers.get("host")):
        await ws.close(code=1008, reason="Invalid or missing SDK token")
        return
    if not _origin_allowed(ws.headers.get("origin"), str(ws.url)):
        await ws.close(code=1008, reason="WebSocket origin is not allowed")
        return
    await ws.accept(subprotocol=token_protocol)
    connected_browsers.append(ws)
    if orchestrator:
        await ws.send_text(json.dumps({
            "type": "event",
            "event": "session.snapshot",
            "companion_id": orchestrator.session.identity.companion_id,
            "occurred_at": time.time(),
            "data": orchestrator.get_status()["session"],
        }))
    try:
        while True:
            data = await ws.receive_text()
            msg = json.loads(data)
            event_type = msg.get("type", "")

            if event_type == "command":
                cmd = msg.get("cmd", "")
                args = msg.get("args", {})
                if body:
                    try:
                        _validate_public_command(cmd, args)
                        guard = task_coordinator.raw_command_guard() if task_coordinator else nullcontext()
                        session_guard = orchestrator.session_context() if orchestrator else nullcontext(None)
                        with session_guard as session:
                            with guard:
                                with agent_state.control_guard(msg.get("lease_id"), msg.get("fencing_token")) as lease:
                                    command_args = _fenced_args(args, lease)
                                    if task_coordinator and session:
                                        def register_raw(req_id: str) -> None:
                                            session.register_external_command(cmd, req_id, args, requester="web")
                                        def unregister_raw(req_id: str) -> None:
                                            session.unregister_external_command(req_id)
                                        request_id = task_coordinator.dispatch_raw_command(
                                            cmd, command_args, on_reserved=register_raw, on_failed=unregister_raw,
                                        )
                                    elif task_coordinator:
                                        request_id = task_coordinator.dispatch_raw_command(cmd, command_args)
                                    else:
                                        request_id = body.send_command(cmd, command_args)
                                if session and not task_coordinator:
                                    session.register_external_command(cmd, request_id, args, requester="web")
                        await ws.send_text(json.dumps({"type": "command_accepted", "id": request_id}))
                    except (ConnectionError, LeaseConflictError, HTTPException, ValueError) as exc:
                        error = exc.detail if isinstance(exc, HTTPException) else str(exc)
                        await ws.send_text(json.dumps({"type": "command_rejected", "error": error}))

            elif event_type == "chat":
                message = msg.get("message", "")
                sender = msg.get("sender", "web")
                if orchestrator:
                    response = await asyncio.to_thread(orchestrator.handle_chat, sender, message)
                    await ws.send_text(json.dumps({
                        "type": "chat_response",
                        "data": {"response": response or ""},
                    }))

            elif event_type == "llm_config":
                data = msg.get("data", {})
                agent = data.get("agent", "default")
                try:
                    with _configuration_guard():
                        config_store.set_agent_llm_config(agent, data)
                        _apply_config_to_llm_services()
                except (HTTPException, ValueError) as exc:
                    error = exc.detail if isinstance(exc, HTTPException) else str(exc)
                    await ws.send_text(json.dumps({"type": "config_rejected", "error": error}))

    except WebSocketDisconnect:
        pass
    except Exception as e:
        print(f"[WS] Error: {e}")
    finally:
        if ws in connected_browsers:
            connected_browsers.remove(ws)


@app.get("/api/status")
async def get_status():
    """REST endpoint for current status."""
    global orchestrator
    if orchestrator:
        return orchestrator.get_status()
    return {"running": False}


@app.get("/api/session")
async def get_session():
    """REST endpoint for session details."""
    global orchestrator
    if orchestrator and orchestrator.session:
        return orchestrator.get_status()["session"]
    return {"session": None}


@app.get("/api/player/v1/contacts")
async def get_player_contacts():
    if not orchestrator or not orchestrator.session:
        raise HTTPException(status_code=503, detail="Companion session is not available")
    with orchestrator.session_context() as session:
        conversations = session.message_db.list_conversations(limit=200)
    contacts = _conversation_contacts(conversations)
    return {"contacts": contacts}


def _conversation_contacts(conversations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    online_by_uuid: set[str] = set()
    if orchestrator and orchestrator.session:
        with orchestrator.session_context() as session:
            online_by_uuid = {
                str(player.get("uuid", ""))
                for player in session.runtime.get("online_players", [])
                if player.get("uuid")
            }
    contacts = []
    for conversation in conversations:
        participants = conversation.get("participants", [])
        contact_id = next((item for item in participants if item != "companion"), "unknown")
        minecraft_uuid = contact_id.removeprefix("minecraft:")
        contacts.append({
            "id": contact_id,
            "display_name": conversation.get("topic") or contact_id,
            "conversation_id": conversation["id"],
            "last_activity": conversation["last_activity"],
            "message_count": conversation["message_count"],
            "presence": "online" if minecraft_uuid in online_by_uuid else "unknown",
        })
    return contacts


@app.get("/api/player/v1/conversations/{conversation_id}/messages")
async def get_player_conversation_messages(
    conversation_id: str, limit: int = Query(default=100, ge=1, le=200),
):
    if not orchestrator or not orchestrator.session:
        raise HTTPException(status_code=503, detail="Companion session is not available")
    with orchestrator.session_context() as session:
        conversation = session.message_db.get_conversation(conversation_id)
        if conversation is None:
            raise HTTPException(status_code=404, detail="Conversation not found")
        messages = session.message_db.get_conversation_messages(conversation_id, limit)
    return {"conversation": conversation, "messages": messages}


@app.get("/api/v2/inbox/contacts")
async def get_operator_inbox_contacts():
    if not orchestrator or not orchestrator.session:
        raise HTTPException(status_code=503, detail="Companion session is not available")
    with orchestrator.session_context() as session:
        conversations = session.message_db.list_conversations(limit=200)
    return {"contacts": _conversation_contacts(conversations)}


@app.get("/api/v2/inbox/conversations/{conversation_id}/messages")
async def get_operator_inbox_messages(
    conversation_id: str, limit: int = Query(default=100, ge=1, le=200),
):
    if not orchestrator or not orchestrator.session:
        raise HTTPException(status_code=503, detail="Companion session is not available")
    with orchestrator.session_context() as session:
        conversation = session.message_db.get_conversation(conversation_id)
        if conversation is None:
            raise HTTPException(status_code=404, detail="Conversation not found")
        messages = session.message_db.get_conversation_messages(conversation_id, limit)
    return {"conversation": conversation, "messages": messages}


@app.post("/api/player/v1/messages")
async def send_player_conversation_message(data: PlayerConversationRequest):
    if not orchestrator or not orchestrator.session:
        raise HTTPException(status_code=503, detail="Companion session is not available")
    contact_id = f"minecraft:{data.player_id}"
    with orchestrator.session_context() as session:
        conversation_id = session.message_db.get_or_create_direct_conversation(contact_id, data.player_name)
        claimed, receipt = session.message_db.claim_player_message(data.client_message_id, conversation_id)
        if not claimed:
            if receipt["status"] == "completed":
                return {
                    "status": "completed", "conversation_id": conversation_id,
                    "client_message_id": data.client_message_id, "reply": receipt["response_text"],
                }
            raise HTTPException(status_code=409, detail=f"Message is already {receipt['status']}")
        session.message_db.add_message(
            data.player_name,
            data.message,
            conversation_id=conversation_id,
            metadata={
                "transport": "player_client",
                "player_id": data.player_id,
                "server_id": data.server_id,
                "client_message_id": data.client_message_id,
            },
        )
        session.memory.add_interaction(data.player_name, data.message)
        session.memory.observe_player(data.player_name, data.player_id, data.message)
        context = session.memory.build_context(current_player=data.player_name)
        llm = session.llm
        persona = session.runtime.get("persona", {})
    if not llm.is_configured("conversation"):
        with orchestrator.session_context() as session:
            session.message_db.fail_player_message(data.client_message_id, "conversation model is not configured")
        raise HTTPException(status_code=503, detail="Conversation model is not configured")
    system_prompt = llm.build_system_prompt({"persona": persona, **context})
    system_prompt += (
        "\nThis is a private text conversation. Reply conversationally only. "
        "Do not emit commands, tool syntax, or body-control instructions."
    )
    try:
        result = await asyncio.to_thread(
            llm.chat,
            [
                {"role": "system", "content": system_prompt, "required": True},
                {"role": "user", "content": f"{data.player_name}: {data.message}", "required": True},
            ],
            agent="conversation",
        )
        reply = str(result.get("content", "")).strip()
        if not reply:
            raise ValueError("Conversation model returned an empty reply")
    except Exception as exc:
        with orchestrator.session_context() as session:
            session.message_db.fail_player_message(data.client_message_id, str(exc))
        raise HTTPException(status_code=502, detail="Conversation model failed") from exc
    with orchestrator.session_context() as session:
        session.message_db.add_message(
            session.runtime.get("persona", {}).get("name", "AI"),
            reply,
            is_ai=True,
            conversation_id=conversation_id,
            metadata={"transport": "player_client", "reply_to": data.client_message_id},
        )
        session.memory.attach_response(data.player_name, data.message, reply)
        session.message_db.complete_player_message(data.client_message_id, reply)
    return {
        "status": "completed",
        "conversation_id": conversation_id,
        "client_message_id": data.client_message_id,
        "reply": reply,
    }


@app.get("/api/llm/config")
async def get_llm_config():
    """Get LLM config (without API key)."""
    config = config_store.raw(redact=True).get("llm", {})
    if orchestrator and orchestrator.session:
        with orchestrator.session_context() as session:
            config["usage"] = session.llm.get_usage()
    else:
        config["usage"] = llm_service.get_usage()
    return config


@app.post("/api/llm/config")
async def set_llm_config(data: dict):
    """Set LLM configuration."""
    try:
        with _configuration_guard():
            agent = data.get("agent", "default")
            saved = config_store.set_agent_llm_config(agent, data)
            _apply_config_to_llm_services()
        return {"status": "ok", "agent": agent, "config": saved}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@app.get("/api/llm/providers")
async def get_llm_providers():
    """List known OpenAI-compatible provider presets."""
    return {"providers": config_store.list_provider_presets()}


@app.get("/api/memory")
async def get_memory():
    """REST endpoint for memory summary."""
    global orchestrator
    if orchestrator and orchestrator.session:
        with orchestrator.session_context() as session:
            mem = session.memory
            return {
                "interactions": len(mem.recent_messages),
                "actions": mem.total_actions,
                "locations": list(mem.locations.keys()),
                "relationships": mem.player_relationships,
                "experiences": mem.experiences,
                "task_outcomes": mem.task_outcomes[-20:],
                "recent": mem.build_context(),
            }
    return {"interactions": 0}


def _memory_catalog(session) -> MemoryCatalog:
    return MemoryCatalog(session.memory, session.message_db, session.identity, session.memory_overlay)


@app.get("/api/v2/memory/status")
async def get_memory_status():
    if not orchestrator or not orchestrator.session:
        raise HTTPException(status_code=503, detail="Companion session is not available")
    with orchestrator.session_context() as session:
        return _memory_catalog(session).status()


@app.get("/api/v2/memory/records")
async def get_memory_records(
    q: str = Query(default="", max_length=500),
    category: list[str] = Query(default=[]),
    state: list[str] = Query(default=["active"]),
    player: str = Query(default="", max_length=128),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
):
    unknown = sorted(set(category) - MEMORY_CATEGORIES)
    if unknown:
        raise HTTPException(status_code=422, detail=f"Unknown memory categories: {', '.join(unknown)}")
    unknown_states = sorted(set(state) - MEMORY_STATES)
    if unknown_states:
        raise HTTPException(status_code=422, detail=f"Unknown memory states: {', '.join(unknown_states)}")
    if not orchestrator or not orchestrator.session:
        raise HTTPException(status_code=503, detail="Companion session is not available")
    query = MemoryQuery(q, frozenset(category), player, offset, limit, frozenset(state))
    with orchestrator.session_context() as session:
        return _memory_catalog(session).list_records(query)


@app.get("/api/v2/memory/records/{record_id}")
async def get_memory_record(record_id: str):
    if not orchestrator or not orchestrator.session:
        raise HTTPException(status_code=503, detail="Companion session is not available")
    with orchestrator.session_context() as session:
        record = _memory_catalog(session).get_record(record_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Memory record not found")
    return record


@app.post("/api/v2/memory/exports")
async def export_memory(data: MemoryExportRequest):
    if not orchestrator or not orchestrator.session:
        raise HTTPException(status_code=503, detail="Companion session is not available")
    query = MemoryQuery(
        data.q, frozenset(data.categories), data.player, 0, 1_000_000,
        frozenset(data.states), frozenset(data.record_ids),
    )
    with orchestrator.session_context() as session:
        payload, media_type = _memory_catalog(session).export(
            query, data.format, data.include_provenance,
        )
    extension = "json" if data.format == "json" else "jsonl"
    return Response(
        content=payload,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="lcu-memory-export.{extension}"'},
    )


@app.post("/api/v2/memory/compression/previews")
async def preview_memory_compression(data: MemoryCompressionPreviewRequest):
    if not orchestrator or not orchestrator.session:
        raise HTTPException(status_code=503, detail="Companion session is not available")
    categories = set(data.categories) if data.categories else MEMORY_CATEGORIES - {"summary"}
    query = MemoryQuery(
        data.q, frozenset(categories), data.player, 0, 1_000_000,
        frozenset(data.states), frozenset(data.record_ids),
    )
    with orchestrator.session_context() as session:
        records, revision = _memory_catalog(session).select_records(query)
        llm = session.llm
        config = llm.get_agent_config(data.summary_model_agent, redact=False)
    if not records:
        raise HTTPException(status_code=422, detail="No memory records matched the selection")
    source = [
        {
            "id": record["id"],
            "category": record["category"],
            "occurred_at": record["occurred_at"],
            "title": record["title"],
            "content": record["content"],
        }
        for record in records
    ]
    messages = [
        {
            "role": "system",
            "content": (
                "Create a faithful durable memory summary. Preserve named players, goals, task outcomes, "
                "locations, chronology, uncertainty, and unresolved constraints. Do not invent facts. "
                "Do not include credentials or authentication material. Return only the summary text."
            ),
            "required": True,
        },
        {
            "role": "user",
            "content": json.dumps(source, ensure_ascii=False, separators=(",", ":")),
            "required": True,
        },
    ]
    try:
        result = await asyncio.to_thread(
            llm.chat, messages, agent=data.summary_model_agent, max_output_tokens=data.target_tokens,
        )
        usage = llm.get_usage()
        recent_usage = usage.get("recent_requests", [])[-1] if usage.get("recent_requests") else {}
        preview = memory_preview_store.create_summary_preview(
            query=query,
            records=records,
            source_revision=revision,
            summary=str(result.get("content", "")),
            agent=data.summary_model_agent,
            model=str(config.get("model", "")),
            target_tokens=data.target_tokens,
            usage=recent_usage,
        )
        return preview
    except LLMRequestRejected as exc:
        raise HTTPException(status_code=422, detail={"code": exc.code, "message": str(exc), **exc.details}) from exc
    except MemoryPreviewError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/api/v2/memory/compression/runs")
async def run_memory_compression(data: MemoryCompressionRunRequest):
    if not orchestrator or not orchestrator.session:
        raise HTTPException(status_code=503, detail="Companion session is not available")
    try:
        preview = memory_preview_store.get(data.preview_id)
        with _configuration_guard():
            with orchestrator.session_context() as session:
                catalog = _memory_catalog(session)
                records, revision = catalog.select_records(preview.query)
                summary = memory_preview_store.commit(
                    data.preview_id,
                    current_revision=revision,
                    current_records=records,
                    memory=session.memory,
                )
        return {"status": "succeeded", "summary": summary, "source_records_retained": True}
    except MemoryPreviewError as exc:
        status_code = 409 if "changed" in str(exc).lower() else 404
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc


@app.post("/api/v2/memory/actions/previews")
async def preview_memory_action(data: MemoryActionPreviewRequest):
    if not orchestrator or not orchestrator.session:
        raise HTTPException(status_code=503, detail="Companion session is not available")
    default_states = {
        "archive": frozenset({"active"}),
        "delete": frozenset({"archived"}),
        "restore": frozenset({"archived", "deleted"}),
    }[data.action]
    selected_states = frozenset(data.states) if data.states else default_states
    query = MemoryQuery(
        data.q, frozenset(data.categories), data.player, 0, 1_000_000,
        selected_states, frozenset(data.record_ids),
    )
    with orchestrator.session_context() as session:
        catalog = _memory_catalog(session)
        records, revision = catalog.select_records(query)
    if data.action == "delete" and not data.allow_active and any(record["state"] == "active" for record in records):
        raise HTTPException(status_code=422, detail="Active records must be archived before deletion")
    target_state = {"archive": "archived", "delete": "deleted", "restore": "active"}[data.action]
    try:
        return memory_preview_store.create_state_preview(
            action=data.action,
            records=records,
            source_revision=revision,
            changes={target_state: [record["id"] for record in records]},
            reason=data.reason,
        )
    except MemoryPreviewError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/api/v2/memory/actions")
async def run_memory_action(data: MemoryActionRunRequest):
    if not orchestrator or not orchestrator.session:
        raise HTTPException(status_code=503, detail="Companion session is not available")
    try:
        with _configuration_guard():
            with orchestrator.session_context() as session:
                catalog = _memory_catalog(session)
                records, revision = catalog.select_records(MemoryQuery(states=frozenset(MEMORY_STATES), limit=1_000_000))
                result = memory_preview_store.commit_state_preview(
                    data.preview_id,
                    confirmation_token=data.confirmation_token,
                    confirmation_text=data.confirmation_text,
                    current_revision=revision,
                    current_records=records,
                    overlay_store=session.memory_overlay,
                    scope_id=session.identity.scope_id,
                )
        return {"status": "succeeded", "result": result, "source_records_retained": True}
    except MemoryPreviewError as exc:
        status_code = 409 if "changed" in str(exc).lower() else 422
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc


@app.get("/api/v2/memory/audit")
async def get_memory_audit(limit: int = Query(default=50, ge=1, le=200)):
    if not orchestrator or not orchestrator.session:
        raise HTTPException(status_code=503, detail="Companion session is not available")
    with orchestrator.session_context() as session:
        return {"items": session.memory_overlay.list_audit(session.identity.scope_id, limit)}


@app.get("/api/v2/memory/retention")
async def get_memory_retention():
    if not orchestrator or not orchestrator.session:
        raise HTTPException(status_code=503, detail="Companion session is not available")
    with orchestrator.session_context() as session:
        return session.memory_overlay.get_retention(session.identity.scope_id)


@app.patch("/api/v2/memory/retention")
async def set_memory_retention(data: RetentionUpdateRequest):
    if not orchestrator or not orchestrator.session:
        raise HTTPException(status_code=503, detail="Companion session is not available")
    try:
        with _configuration_guard():
            with orchestrator.session_context() as session:
                return session.memory_overlay.set_retention(
                    session.identity.scope_id,
                    data.expected_version,
                    [rule.model_dump() for rule in data.rules],
                )
    except RetentionConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/v2/memory/retention/previews")
async def preview_memory_retention():
    if not orchestrator or not orchestrator.session:
        raise HTTPException(status_code=503, detail="Companion session is not available")
    with orchestrator.session_context() as session:
        retention = session.memory_overlay.get_retention(session.identity.scope_id)
        records, revision = _memory_catalog(session).select_records(
            MemoryQuery(states=frozenset({"active", "archived"}), limit=1_000_000),
        )
    changes = evaluate_retention(records, retention["rules"])
    try:
        return memory_preview_store.create_state_preview(
            action="retention",
            records=records,
            source_revision=revision,
            changes=changes,
            reason=f"retention version {retention['version']}",
        )
    except MemoryPreviewError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/api/v2/memory/retention/runs")
async def run_memory_retention(data: MemoryActionRunRequest):
    return await run_memory_action(data)


@app.get("/api/tokens")
async def get_tokens():
    """REST endpoint for token usage (backward compat)."""
    global orchestrator
    if orchestrator and orchestrator.session:
        with orchestrator.session_context() as session:
            usage = session.llm.get_usage()
        return {
            "total_tokens": usage.get("total_tokens", 0),
            "total_cost": 0,
            "players": [],
            "flagged_count": 0,
        }
    return {"total_tokens": 0, "total_cost": 0, "players": [], "flagged_count": 0}


@app.get("/api/config")
async def get_config():
    """Get current backend configuration."""
    return config_store.raw(redact=True)


@app.post("/api/config")
async def set_config(data: dict):
    """Update backend configuration."""
    with _configuration_guard():
        config = config_store.set_app_config(data)
    return {"status": "ok", "config": config}


@app.get("/api/llm/models")
async def get_llm_models():
    """Get available LLM models."""
    try:
        models = llm_service.fetch_models("default")
        return {"models": models, "source": "remote"}
    except Exception as e:
        config = config_store.get_agent_llm_config("default")
        fallback = [config.get("model") or "gpt-4o-mini"]
        return {"models": fallback, "source": "fallback", "error": str(e)}


@app.post("/api/llm/models")
async def fetch_llm_models(data: dict):
    """Fetch available models for a selected agent or explicit provider config."""
    try:
        agent = data.get("agent", "default")
        config = config_store.get_agent_llm_config(agent, redact=False)
        base_url = data.get("base_url") or config.get("base_url")
        explicit_base_url = data.get("base_url")
        api_key = data.get("api_key") if "api_key" in data else None if explicit_base_url else config.get("api_key")
        models = llm_service.fetch_models(agent=agent, base_url=base_url, api_key=api_key)
        return {"models": models, "source": "remote"}
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Failed to fetch models: {e}") from e


@app.get("/api/persona")
async def get_persona():
    return config_store.get_persona()


@app.post("/api/persona")
async def set_persona(data: dict):
    with _configuration_guard():
        persona = config_store.set_persona(data)
        _apply_persona_to_session()
    return {"status": "ok", "persona": persona}


@app.post("/api/sdk/context")
async def set_sdk_context(data: SDKContextRequest):
    """External SDK endpoint for upstream persona/context injection."""
    with _configuration_guard():
        result = config_store.set_integration_context(data.model_dump())
        _apply_persona_to_session()
    return {"status": "ok", **result}


@app.get("/api/sdk/context")
async def get_sdk_context():
    return {
        "persona": config_store.get_persona(),
        "integration": config_store.raw(redact=True).get("integration", {}),
    }


@app.get("/api/sdk/info")
async def get_sdk_info():
    return {
        "api_version": SDK_API_VERSION,
        "auth_required": bool(_sdk_api_token()),
        "interfaces": ["gateway", "actuator", "observer"],
        "supported_versions": [SDK_API_VERSION, SDK_V2_API_VERSION],
    }


@app.get("/api/v2/info")
async def get_v2_info():
    return {
        "api_version": SDK_V2_API_VERSION,
        "auth_required": bool(_sdk_api_token()),
        "interfaces": ["observer", "control", "skills"],
        "control_modes": ["builtin", "external"],
        "skill_registry_revision": skill_registry.revision,
    }


@app.get("/api/v2/control")
async def get_v2_control():
    lease = _reconcile_control_mode()
    public_lease = None if lease is None else {
        key: value for key, value in lease.items() if key != "fencing_token"
    }
    return {"mode": lease["mode"] if lease else "builtin", "lease": public_lease}


@app.post("/api/v2/control/leases")
async def acquire_v2_control(data: ControlLeaseRequest):
    if data.mode != "external":
        raise HTTPException(status_code=400, detail="only external leases are currently supported")
    invalid_domains = sorted(set(data.owns) - CONTROL_DOMAINS)
    if invalid_domains:
        raise HTTPException(status_code=400, detail=f"unknown control domains: {', '.join(invalid_domains)}")
    if not data.owns:
        raise HTTPException(status_code=400, detail="owns must not be empty")
    if set(data.owns) != CONTROL_DOMAINS:
        raise HTTPException(status_code=400, detail="external mode must own all control domains")
    try:
        with control_transition_lock:
            session_guard = orchestrator.session_context() if orchestrator else nullcontext(None)
            with session_guard:
                gate = task_coordinator.coordination_guard() if task_coordinator else nullcontext()
                with gate:
                    lease = agent_state.acquire_lease(data.owner, data.mode, data.owns, data.ttl_seconds)
                    if task_coordinator:
                        task_coordinator.on_control_transition()
                    if not _apply_control_mode(data.mode, lease["fencing_token"]):
                        rollback_applied = _apply_control_mode("builtin", lease["fencing_token"], force_body=True)
                        if rollback_applied or not body or not body.is_connected:
                            agent_state.release_lease(lease["id"], lease["fencing_token"])
                        raise HTTPException(status_code=503, detail="Companion body rejected or did not confirm control")
    except LeaseConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    lease_response = {**lease, "runtime_status": "applied" if body and body.is_connected else "pending_connection"}
    return {"status": "acquired", "lease": lease_response}


@app.post("/api/v2/control/leases/{lease_id}/heartbeat")
async def heartbeat_v2_control(lease_id: str, data: ControlLeaseHeartbeat):
    try:
        lease = agent_state.renew_lease(lease_id, data.fencing_token, data.ttl_seconds)
    except LeaseNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except LeaseConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"status": "renewed", "lease": lease}


@app.post("/api/v2/control/leases/{lease_id}/release")
async def release_v2_control(lease_id: str, data: ControlLeaseRelease):
    try:
        with control_transition_lock:
            session_guard = orchestrator.session_context() if orchestrator else nullcontext(None)
            with session_guard:
                gate = task_coordinator.coordination_guard() if task_coordinator else nullcontext()
                with gate:
                    with agent_state.control_guard(lease_id, data.fencing_token) as lease:
                        if task_coordinator:
                            task_coordinator.on_control_transition()
                        runtime_applied = _apply_control_mode("builtin", lease["fencing_token"])
                        if body and body.is_connected and not runtime_applied:
                            raise HTTPException(status_code=503, detail="Companion body did not confirm built-in control")
                        lease = agent_state.release_lease(lease_id, data.fencing_token)
    except LeaseNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except LeaseConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    runtime_status = "applied" if runtime_applied and body and body.is_connected else "pending_connection"
    return {"status": "released", "lease": {**lease, "runtime_status": runtime_status}}


@app.get("/api/v2/skills")
async def list_v2_skills(category: str | None = None):
    skills = skill_registry.list(category)
    return {"skills": skills, "count": len(skills)}


@app.get("/api/v2/skills/{skill_id}")
async def get_v2_skill(skill_id: str):
    try:
        return skill_registry.get(skill_id).public_dict()
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/v2/skills/{skill_id}/runs")
async def run_v2_skill(skill_id: str, data: SkillRunRequest):
    if not task_coordinator:
        raise HTTPException(status_code=503, detail="Task coordinator is not running")
    try:
        manifest = skill_registry.validate_input(skill_id, data.input)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except SkillValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    try:
        if data.lease_id is not None and (not body or not body.is_connected):
            raise HTTPException(status_code=503, detail="Companion body is not connected")
        if data.lease_id is None and data.fencing_token is None and orchestrator:
            with orchestrator.session_context() as session:
                task_coordinator.set_session_busy(session.is_busy_for_external_task())
                run = task_coordinator.create_run(manifest.id, data.input)
        else:
            run = task_coordinator.create_run(
                manifest.id, data.input, lease_id=data.lease_id, fencing_token=data.fencing_token,
            )
    except LeaseConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ConnectionError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return run


@app.get("/api/v2/runs")
async def list_v2_runs(
    limit: int = Query(default=50, ge=1, le=200),
    status: Literal["queued", "dispatched", "running", "succeeded", "failed", "cancelled", "unknown"] | None = None,
):
    runs = agent_state.list_runs(limit=limit, status=status)
    return {"runs": runs, "count": len(runs)}


@app.get("/api/v2/runs/{run_id}")
async def get_v2_run(run_id: str):
    try:
        return agent_state.get_run(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/v2/runs/{run_id}/cancel")
async def cancel_v2_run(run_id: str, data: RunCancelRequest):
    if not task_coordinator:
        raise HTTPException(status_code=503, detail="Task coordinator is not running")
    try:
        return task_coordinator.cancel(
            run_id, lease_id=data.lease_id, fencing_token=data.fencing_token,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except LeaseConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ConnectionError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/v2/runs/{run_id}/resume")
async def resume_v2_run(run_id: str, data: RunCancelRequest):
    if not task_coordinator:
        raise HTTPException(status_code=503, detail="Task coordinator is not running")
    try:
        if orchestrator:
            with orchestrator.session_context() as session:
                task_coordinator.set_session_busy(session.is_busy_for_external_task())
                return task_coordinator.resume(
                    run_id, lease_id=data.lease_id, fencing_token=data.fencing_token,
                )
        return task_coordinator.resume(
            run_id, lease_id=data.lease_id, fencing_token=data.fencing_token,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except LeaseConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ConnectionError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/api/v2/events")
async def list_v2_events(
    after: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
    latest: bool = False,
):
    events = agent_state.list_events(after=after, limit=limit, latest=latest)
    return {"events": events, "count": len(events), "next_cursor": events[-1]["cursor"] if events else after}


@app.get("/api/v2/schedules")
async def list_v2_schedules():
    schedules = agent_state.list_schedules()
    return {"schedules": schedules, "count": len(schedules)}


@app.post("/api/v2/schedules")
async def create_v2_schedule(data: ScheduleRequest):
    try:
        manifest = skill_registry.validate_input(data.skill_id, data.input)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except SkillValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if not manifest.schedulable:
        raise HTTPException(status_code=422, detail="skill does not have a finite scheduling contract")
    if data.misfire_policy not in {"skip", "fire_once"}:
        raise HTTPException(status_code=422, detail="misfire_policy must be skip or fire_once")

    now = time.time()
    payload = data.model_dump(exclude={"lease_id", "fencing_token"})
    payload["skill_version"] = manifest.version
    if orchestrator:
        with orchestrator.session_context() as session:
            payload["scope_id"] = f"{session.identity.server_id}\0{session.identity.world_id}"
    else:
        payload["scope_id"] = "default"
    payload["next_wall_at"] = None
    payload["next_game_tick"] = None
    if data.clock == "wall" and data.trigger_type == "once" and data.wall_run_at is not None:
        payload["next_wall_at"] = data.wall_run_at
    elif data.clock == "wall" and data.trigger_type == "interval" \
            and data.wall_interval_seconds is not None and data.wall_interval_seconds >= 1:
        payload["next_wall_at"] = data.wall_run_at \
            if data.wall_run_at is not None else now + data.wall_interval_seconds
    elif data.clock == "game" and data.trigger_type == "interval" \
            and data.game_interval_ticks is not None and data.game_interval_ticks >= 20:
        pass
    elif data.clock == "game" and data.trigger_type == "time_of_day" \
            and data.time_of_day_tick is not None and 0 <= data.time_of_day_tick < 24000:
        pass
    else:
        raise HTTPException(status_code=422, detail="invalid clock and trigger fields")

    try:
        with agent_state.control_guard(data.lease_id, data.fencing_token):
            return agent_state.create_schedule(payload)
    except LeaseConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.patch("/api/v2/schedules/{schedule_id}")
async def update_v2_schedule(schedule_id: str, data: ScheduleUpdateRequest):
    try:
        with agent_state.control_guard(data.lease_id, data.fencing_token):
            return agent_state.set_schedule_enabled(schedule_id, data.enabled)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except LeaseConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.delete("/api/v2/schedules/{schedule_id}")
async def delete_v2_schedule(schedule_id: str, data: RunCancelRequest):
    try:
        with agent_state.control_guard(data.lease_id, data.fencing_token):
            agent_state.delete_schedule(schedule_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except LeaseConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"status": "deleted", "id": schedule_id}


@app.get("/api/sdk/identity")
async def get_sdk_identity():
    if orchestrator:
        return {"identity": orchestrator.get_status()["session"]["identity"]}
    return {"identity": _configured_identity()}


@app.post("/api/sdk/identity")
async def set_sdk_identity(data: dict):
    """Update stable identity settings; changes apply after backend restart."""
    overrides = [name for name in ("COMPANION_ID", "MEMORY_SCOPE", "SERVER_ID", "WORLD_ID") if os.getenv(name)]
    if overrides:
        raise HTTPException(
            status_code=409,
            detail=f"Identity is controlled by environment variables: {', '.join(overrides)}",
        )
    try:
        with _configuration_guard():
            companion = config_store.set_companion_config(data)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    persistence = companion.get("persistence", {})
    identity = {
        "companion_id": companion["id"],
        "scope": persistence.get("scope", "global"),
        "server_id": persistence.get("server_id", "default"),
        "world_id": persistence.get("world_id", "default"),
    }
    return {"status": "ok", "restart_required": True, "identity": identity}


@app.post("/api/sdk/chat")
async def sdk_chat(data: SDKChatRequest):
    """Send a message through the companion persona, memory, and planner."""
    if not orchestrator:
        raise HTTPException(status_code=503, detail="Companion session is not running")
    with orchestrator.session_context() as session:
        if session.control_mode == "external":
            raise HTTPException(status_code=409, detail="Built-in planner is disabled by external control")
    response = await asyncio.to_thread(orchestrator.handle_chat, data.sender, data.message)
    return {"status": "ok", "response": response or ""}


@app.post("/api/sdk/command")
async def sdk_command(data: SDKCommandRequest):
    """Send an authorized low-level command to the active client body."""
    if not body or not body.is_connected:
        raise HTTPException(status_code=503, detail="Companion body is not connected")
    try:
        _validate_public_command(data.command, data.args)
        guard = task_coordinator.raw_command_guard() if task_coordinator else nullcontext()
        session_guard = orchestrator.session_context() if orchestrator else nullcontext(None)
        with session_guard as session:
            with guard:
                with agent_state.control_guard(None, None):
                    if task_coordinator and session:
                        def register_raw(req_id: str) -> None:
                            session.register_external_command(data.command, req_id, data.args, requester="sdk")
                        def unregister_raw(req_id: str) -> None:
                            session.unregister_external_command(req_id)
                        request_id = task_coordinator.dispatch_raw_command(
                            data.command, data.args, on_reserved=register_raw, on_failed=unregister_raw,
                        )
                    elif task_coordinator:
                        request_id = task_coordinator.dispatch_raw_command(data.command, data.args)
                    else:
                        request_id = body.send_command(data.command, data.args)
                if session and not task_coordinator:
                    session.register_external_command(data.command, request_id, data.args, requester="sdk")
    except ConnectionError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except LeaseConflictError as exc:
        raise HTTPException(status_code=409, detail="Use the V2 skill API while a control lease is active") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"status": "accepted", "request_id": request_id}


@app.get("/api/database")
async def get_database():
    """Get message database statistics and recent messages."""
    global orchestrator
    if orchestrator and orchestrator.session:
        with orchestrator.session_context() as session:
            db = session.message_db
            stats = db.get_stats()
            recent = db.get_recent_messages(limit=20)
            players = db.get_all_players()
            return {
                "stats": stats,
                "recent_messages": recent,
                "players": players,
            }
    return {"stats": {}, "recent_messages": [], "players": []}


@app.get("/api/database/messages")
async def get_database_messages(limit: int = 50, sender: Optional[str] = None):
    """Get messages from database with optional filtering."""
    global orchestrator
    if orchestrator and orchestrator.session:
        with orchestrator.session_context() as session:
            messages = session.message_db.get_recent_messages(limit=limit, sender=sender)
            return {"messages": messages}
    return {"messages": []}


@app.get("/api/database/search")
async def search_database_messages(q: str, limit: int = 50):
    """Search messages in database."""
    global orchestrator
    if orchestrator and orchestrator.session:
        with orchestrator.session_context() as session:
            messages = session.message_db.search_messages(query=q, limit=limit)
            return {"messages": messages}
    return {"messages": []}
