"""
FastAPI web server.
Serves dashboard, WebSocket, and REST API for the Session-based architecture.
"""

import json
import os
import secrets
import threading
import time
import ipaddress
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Optional, cast
from urllib.parse import urlparse

import asyncio

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field, model_validator

from agent import LLMService
from agent.agent_state import AgentStateDB, LeaseConflictError, LeaseNotFoundError
from agent.config_store import ConfigStore, DEFAULT_CONFIG_PATH
from agent.orchestrator import Orchestrator
from agent.skill_registry import SkillRegistry, SkillValidationError
from protocol import BodyAdapter, WireClient

SDK_API_VERSION = "1"
SDK_V2_API_VERSION = "2"
app = FastAPI(title="LCUMod Backend", version="0.1.0")

CONFIG_PATH = DEFAULT_CONFIG_PATH
config_store = ConfigStore(CONFIG_PATH)


def _sdk_api_token() -> str:
    return os.getenv("SDK_API_TOKEN", "").strip()


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
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)


@app.middleware("http")
async def sdk_authentication(request: Request, call_next):
    if request.method != "OPTIONS" and request.url.path.startswith("/api/"):
        client_host = request.client.host if request.client else None
        if not _valid_token(_request_token(request), client_host, request.headers.get("host")):
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


class SDKCommandRequest(BaseModel):
    command: str = Field(min_length=1)
    args: dict[str, Any] = Field(default_factory=dict)


class ControlLeaseRequest(BaseModel):
    owner: str = Field(min_length=1, max_length=128)
    mode: str = "external"
    owns: list[str] = Field(default_factory=lambda: ["persona", "memory", "planner", "autonomy", "actions"])
    ttl_seconds: int = Field(default=30, ge=5, le=300)


class ControlLeaseHeartbeat(BaseModel):
    fencing_token: int = Field(ge=1)
    ttl_seconds: int = Field(default=30, ge=5, le=300)


class ControlLeaseRelease(BaseModel):
    fencing_token: int = Field(ge=1)


class SkillRunRequest(BaseModel):
    input: dict[str, Any] = Field(default_factory=dict)
    lease_id: str | None = None
    fencing_token: int | None = Field(default=None, ge=1)

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
connected_browsers: list[WebSocket] = []
shutdown_event = threading.Event()
connection_thread: threading.Thread | None = None

CONTROL_DOMAINS = {"persona", "memory", "planner", "autonomy", "actions"}
RESERVED_BODY_COMMANDS = {"control_external", "control_builtin"}
FENCING_FIELD = "__lcu_fencing_token"


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
                try:
                    command = "control_external" if mode == "external" else "control_builtin"
                    active_body.send_command(command, {FENCING_FIELD: token})
                except ConnectionError:
                    return False
            session.set_control_mode(mode, fencing_token)
            return True
    if active_body and active_body.is_connected and changed:
        try:
            command = "control_external" if mode == "external" else "control_builtin"
            active_body.send_command(command, {FENCING_FIELD: fencing_token})
        except ConnectionError:
            return False
    return True


def _reconcile_control_mode(force_body: bool = False) -> dict[str, Any] | None:
    with agent_state.transition_guard():
        lease = agent_state.get_active_lease()
        token = lease["fencing_token"] if lease else agent_state.latest_fencing_token()
        _apply_control_mode(lease["mode"] if lease else "builtin", token, force_body=force_body)
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
    try:
        with agent_state.control_guard(None, None):
            yield
    except LeaseConflictError as exc:
        raise HTTPException(status_code=409, detail="Configuration is owned by an active control lease") from exc


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
    global body, orchestrator, shutdown_event, connection_thread

    host = os.getenv("MOD_HOST", "127.0.0.1")
    port = int(os.getenv("MOD_PORT", "25568"))
    companion = config_store.get_companion_config()
    persistence = companion.get("persistence", {})

    body = create_body(host, port)
    orchestrator = Orchestrator(
        body,
        companion_id=os.getenv("COMPANION_ID", companion["id"]),
        persistence_scope=os.getenv("MEMORY_SCOPE", persistence.get("scope", "global")),
        server_id=os.getenv("SERVER_ID", persistence.get("server_id", "default")),
        world_id=os.getenv("WORLD_ID", persistence.get("world_id", "default")),
    )
    shutdown_event = threading.Event()
    stop_event = shutdown_event
    _apply_config_to_llm_services()
    _apply_persona_to_session()
    _reconcile_control_mode()

    # Store event loop for background thread scheduling
    loop = asyncio.get_event_loop()

    # Forward chat events to browser WebSocket clients
    def _broadcast_chat(sender, message, is_system):
        """Called from background thread — schedule async broadcast."""
        payload = json.dumps({
            "type": "chat",
            "sender": sender,
            "message": message,
            "is_system": is_system,
        })
        async def _send():
            for ws in connected_browsers.copy():
                try:
                    await ws.send_text(payload)
                except Exception:
                    if ws in connected_browsers:
                        connected_browsers.remove(ws)
        asyncio.run_coroutine_threadsafe(_send(), loop)

    cast(Any, orchestrator).on_chat = _broadcast_chat

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
                o.stop()
                if not stop_event.is_set():
                    print("[Backend] Disconnected. Reconnecting...")
            else:
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
                        with agent_state.control_guard(msg.get("lease_id"), msg.get("fencing_token")) as lease:
                            request_id = body.send_command(cmd, _fenced_args(args, lease))
                        if orchestrator:
                            with orchestrator.session_context() as session:
                                session.register_external_command(cmd, request_id, args, requester="web")
                        await ws.send_text(json.dumps({"type": "command_accepted", "id": request_id}))
                    except (ConnectionError, LeaseConflictError, HTTPException) as exc:
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
                except HTTPException as exc:
                    await ws.send_text(json.dumps({"type": "config_rejected", "error": exc.detail}))

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


@app.get("/api/llm/config")
async def get_llm_config():
    """Get LLM config (without API key)."""
    config = config_store.raw(redact=True).get("llm", {})
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
    lease = agent_state.get_active_lease()
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
        with agent_state.transition_guard():
            lease = agent_state.acquire_lease(data.owner, data.mode, data.owns, data.ttl_seconds)
            if not _apply_control_mode(data.mode, lease["fencing_token"]):
                agent_state.release_lease(lease["id"], lease["fencing_token"])
                raise HTTPException(status_code=503, detail="Failed to transfer control to the companion body")
    except LeaseConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    lease_response = {**lease, "runtime_status": "queued" if body and body.is_connected else "pending_connection"}
    return {"status": "acquired", "lease": lease_response}


@app.post("/api/v2/control/leases/{lease_id}/heartbeat")
async def heartbeat_v2_control(lease_id: str, data: ControlLeaseHeartbeat):
    try:
        lease = agent_state.renew_lease(lease_id, data.fencing_token, data.ttl_seconds)
    except LeaseNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"status": "renewed", "lease": lease}


@app.post("/api/v2/control/leases/{lease_id}/release")
async def release_v2_control(lease_id: str, data: ControlLeaseRelease):
    try:
        with agent_state.transition_guard():
            lease = agent_state.release_lease(lease_id, data.fencing_token)
            runtime_applied = _apply_control_mode("builtin", lease["fencing_token"])
    except LeaseNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    runtime_status = "queued" if runtime_applied and body and body.is_connected else "pending_connection"
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
    if not body or not body.is_connected:
        raise HTTPException(status_code=503, detail="Companion body is not connected")
    try:
        manifest = skill_registry.validate_input(skill_id, data.input)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except SkillValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    try:
        with agent_state.control_guard(data.lease_id, data.fencing_token) as lease:
            request_id = body.send_command(manifest.command, _fenced_args(data.input, lease))
        if orchestrator:
            requester = f"sdk-v2:{lease['owner']}" if lease else "sdk-v2"
            with orchestrator.session_context() as session:
                session.register_external_command(manifest.command, request_id, data.input, requester=requester)
    except ConnectionError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except LeaseConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {
        "run_id": request_id,
        "request_id": request_id,
        "skill_id": manifest.id,
        "status": "accepted",
    }


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
        with agent_state.control_guard(None, None):
            request_id = body.send_command(data.command, data.args)
        if orchestrator:
            with orchestrator.session_context() as session:
                session.register_external_command(data.command, request_id, data.args, requester="sdk")
    except ConnectionError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except LeaseConflictError as exc:
        raise HTTPException(status_code=409, detail="Use the V2 skill API while a control lease is active") from exc
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
