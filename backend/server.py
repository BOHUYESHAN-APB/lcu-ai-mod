"""
FastAPI web server.
Serves dashboard, WebSocket, and REST API for the Session-based architecture.
"""

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Optional, cast

import asyncio

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from agent import LLMService
from agent.config_store import ConfigStore
from agent.orchestrator import Orchestrator
from protocol import WireClient

app = FastAPI(title="LCUMod Backend")

CONFIG_PATH = Path(__file__).parent / "config.json"
config_store = ConfigStore(CONFIG_PATH)
app_config = config_store.raw(redact=True)

# Global state
wire: WireClient | None = None
orchestrator: Orchestrator | None = None
llm_service = LLMService()
connected_browsers: list[WebSocket] = []


def _apply_config_to_llm_services():
    """Sync persisted LLM config into global and session LLM services."""
    default_config = config_store.get_agent_llm_config("default", redact=False)
    llm_service.set_agent_config("default", default_config)
    for agent_name, config in config_store.raw(redact=False).get("llm", {}).get("agents", {}).items():
        llm_service.set_agent_config(agent_name, config)
    if orchestrator and orchestrator.session:
        for agent_name, config in config_store.raw(redact=False).get("llm", {}).get("agents", {}).items():
            orchestrator.session.llm.set_agent_config(agent_name, config)


def _apply_persona_to_session():
    if not orchestrator or not orchestrator.session:
        return
    persona = config_store.get_persona()
    orchestrator.session.runtime["persona"] = persona

# Static files
STATIC_DIR = Path(__file__).parent / "web" / "static"
STATIC_DIR.mkdir(parents=True, exist_ok=True)


@app.on_event("startup")
async def startup():
    """Start wire client and orchestrator on startup."""
    global wire, orchestrator

    host = os.getenv("MOD_HOST", "127.0.0.1")
    port = int(os.getenv("MOD_PORT", "25568"))

    wire = WireClient(host, port)
    orchestrator = Orchestrator(wire)
    _apply_config_to_llm_services()
    _apply_persona_to_session()

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
        w = wire
        o = orchestrator
        if w is None or o is None:
            return
        while True:
            if w.connect():
                print(f"[Backend] Connected to mod at {host}:{port}")
                o.start()
                # Event loop: drain wire + tick orchestrator
                while w.sock:
                    o.tick()
                    time.sleep(0.05)  # 20Hz loop
                o.stop()
                print("[Backend] Disconnected. Reconnecting...")
            else:
                print(f"[Backend] Mod not available at {host}:{port}. Retry in 5s...")
                time.sleep(5)

    t = threading.Thread(target=_connect_loop, daemon=True)
    t.start()


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
    await ws.accept()
    connected_browsers.append(ws)
    try:
        while True:
            data = await ws.receive_text()
            msg = json.loads(data)
            event_type = msg.get("type", "")

            if event_type == "command":
                cmd = msg.get("cmd", "")
                args = msg.get("args", {})
                if wire:
                    wire.send_command(cmd, args)

            elif event_type == "chat":
                message = msg.get("message", "")
                sender = msg.get("sender", "web")
                if orchestrator:
                    response = orchestrator.handle_chat(sender, message)
                    await ws.send_text(json.dumps({
                        "type": "chat_response",
                        "data": {"response": response or ""},
                    }))

            elif event_type == "llm_config":
                data = msg.get("data", {})
                agent = data.get("agent", "default")
                config_store.set_agent_llm_config(agent, data)
                _apply_config_to_llm_services()

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
        return orchestrator.session.get_status()
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
        mem = orchestrator.session.memory
        return {
            "interactions": len(mem.recent_messages),
            "actions": mem.total_actions,
            "locations": list(mem.locations.keys()),
            "recent": mem.build_context(),
        }
    return {"interactions": 0}


@app.get("/api/tokens")
async def get_tokens():
    """REST endpoint for token usage (backward compat)."""
    global orchestrator
    if orchestrator and orchestrator.session:
        usage = orchestrator.session.llm.get_usage()
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
    return app_config


@app.post("/api/config")
async def set_config(data: dict):
    """Update backend configuration."""
    global app_config
    app_config = config_store.set_app_config(data)
    return {"status": "ok", "config": app_config}


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
        api_key = data.get("api_key") if "api_key" in data else config.get("api_key")
        models = llm_service.fetch_models(agent=agent, base_url=base_url, api_key=api_key)
        return {"models": models, "source": "remote"}
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Failed to fetch models: {e}") from e


@app.get("/api/persona")
async def get_persona():
    return config_store.get_persona()


@app.post("/api/persona")
async def set_persona(data: dict):
    persona = config_store.set_persona(data)
    _apply_persona_to_session()
    return {"status": "ok", "persona": persona}


@app.post("/api/sdk/context")
async def set_sdk_context(data: dict):
    """External SDK endpoint for upstream persona/context injection."""
    result = config_store.set_integration_context(data)
    _apply_persona_to_session()
    return {"status": "ok", **result}


@app.get("/api/sdk/context")
async def get_sdk_context():
    return {
        "persona": config_store.get_persona(),
        "integration": config_store.raw(redact=True).get("integration", {}),
    }


@app.get("/api/database")
async def get_database():
    """Get message database statistics and recent messages."""
    global orchestrator
    if orchestrator and orchestrator.session:
        db = orchestrator.session.message_db
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
        db = orchestrator.session.message_db
        messages = db.get_recent_messages(limit=limit, sender=sender)
        return {"messages": messages}
    return {"messages": []}


@app.get("/api/database/search")
async def search_database_messages(q: str, limit: int = 50):
    """Search messages in database."""
    global orchestrator
    if orchestrator and orchestrator.session:
        db = orchestrator.session.message_db
        messages = db.search_messages(query=q, limit=limit)
        return {"messages": messages}
    return {"messages": []}
