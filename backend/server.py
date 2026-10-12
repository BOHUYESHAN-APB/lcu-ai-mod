"""
FastAPI web server.
Serves dashboard, WebSocket, and REST API for the Session-based architecture.
"""

import json
import math
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Optional, cast

import asyncio

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from agent import LLMService
from agent import hot_reload
from agent.command_gate import HARD_BLACKLIST, HIGH_RISK, CommandsStore
from agent.config_store import get_config_store, DEFAULT_CONFIG_PATH
from agent.contacts import ContactStore
from agent.orchestrator import Orchestrator
from agent.permissions import PermissionsStore
from agent.persona_store import KNOWN_KEYS as PERSONA_KEYS, PersonaStore
from protocol import WireClient

app = FastAPI(title="LCUMod Backend")

CONFIG_PATH = DEFAULT_CONFIG_PATH
# 共享实例：Session/Planner 拿到的必须是同一个（否则 UI 改阈值不生效）
config_store = get_config_store(CONFIG_PATH)
app_config = config_store.raw(redact=True)

# 权限 / 人设：运行期一律用 session 里那一份（单一实例），
# 进程还没起来（模组未连接）时退回模块级实例，保证 REST 依然可用。
_permissions_fallback = PermissionsStore()
_persona_fallback = PersonaStore()


def _permissions_store() -> PermissionsStore:
    session = orchestrator.session if orchestrator else None
    store = getattr(session, "permissions", None)
    return store if isinstance(store, PermissionsStore) else _permissions_fallback


def _persona_store() -> PersonaStore:
    session = orchestrator.session if orchestrator else None
    store = getattr(session, "persona", None)
    return store if isinstance(store, PersonaStore) else _persona_fallback


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
        # 视觉按**能力层级**开关：L1（默认）纯文本，不抓屏、不传图片；
        # L2 才启动后台抓屏线程（用同一个多模态模型）。
        if config_store.get_capability_level() >= 2:
            orchestrator.session.vision.start()
        else:
            orchestrator.session.vision.stop()


def _apply_persona_to_session():
    if not orchestrator or not orchestrator.session:
        return
    # 合并视图：legacy（config.json 的 personality/speaking_style）打底，
    # `.local/persona.json` 的三段正文覆盖 —— 空值不覆盖（UI 清空某个可选字段时不至于把人设抹掉）。
    persona = dict(config_store.get_persona())
    for key, value in _persona_store().get().items():
        if value not in (None, "", [], {}):
            persona[key] = value
    orchestrator.session.runtime["persona"] = persona

# Static files
STATIC_DIR = Path(__file__).parent / "web" / "static"
STATIC_DIR.mkdir(parents=True, exist_ok=True)

# OBS 组件注册表：一个组件 = 一个 `/overlay/<name>` 页（透明、100% 自适应、不写死尺寸）。
# **加新属性只在这里加一条** —— 页面 `overlay.html` 按名字渲染，URL 固定可写进 OBS 浏览器源。
OVERLAY_COMPONENTS: dict[str, dict[str, str]] = {
    "status": {"title": "状态", "desc": "当前动作状态词 + HUD 描述词"},
    "task": {"title": "任务", "desc": "当前任务与进度（含队列条数）"},
    "players": {"title": "在线玩家", "desc": "在线玩家，最近发话者高亮"},
    "state": {"title": "基础状态", "desc": "位置 / 生命 / 饥饿 / 维度 / 时间"},
    "backend": {"title": "后端状态", "desc": "在场门 / 模态 / 联系人数 / Token 用量"},
}


def _overlay_manifest() -> list[dict[str, str]]:
    """组件清单（前端/OBS 可直接消费；与 `/overlay/<name>` 路由一一对应）。"""
    return [{"name": name, "title": meta["title"], "desc": meta["desc"],
             "url": f"/overlay/{name}"}
            for name, meta in OVERLAY_COMPONENTS.items()]


# ── OBS 外观主题（overlay_theme）─────────────────────────────────────────────
# 附加式：只新增读写端点，不改任何现有路由行为。主题存进**同一个** .local/config.json
# 的 `overlay_theme` 键（实测：ConfigStore.save() 虽然整份写回，但会原样保留未知键；
# 而且我们改的是同一个共享实例，所以不会和 patrol_radius 之类的现有键互相抹掉）。
_OV_THEME_KEY = "overlay_theme"
_OV_COLOR_RE = re.compile(
    r"^(#[0-9a-fA-F]{3}|#[0-9a-fA-F]{6}"
    r"|rgba?\(\s*[0-9.]+%?\s*,\s*[0-9.]+%?\s*,\s*[0-9.]+%?\s*(?:,\s*[0-9.]+\s*)?\))$"
)


def _ov_color(value: Any) -> str:
    """只接受 #rgb / #rrggbb / rgb()/rgba()；其余（含注入尝试）一律回默认空串。"""
    if not isinstance(value, str):
        return ""
    probe = value.strip()
    return probe if _OV_COLOR_RE.match(probe) else ""


def _ov_bg(value: Any) -> str:
    """背景：`transparent` 或合法颜色；非法 → 空串（不覆盖页面内置默认）。"""
    if not isinstance(value, str):
        return ""
    probe = value.strip()
    if probe == "transparent":
        return "transparent"
    return _ov_color(probe)


def _ov_font_scale(value: Any) -> Optional[float]:
    """字号倍率：有限正数，夹在 [0.25, 8]；非法 → None。"""
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number <= 0:
        return None
    return round(max(0.25, min(8.0, number)), 3)


def _normalize_overlay_theme(payload: Any) -> dict[str, Any]:
    """把任意输入规整成 {bg, fg, accent, font_scale}；非法字段回默认，绝不原样回传。"""
    source = payload if isinstance(payload, dict) else {}
    return {
        "bg": _ov_bg(source.get("bg")),
        "fg": _ov_color(source.get("fg")),
        "accent": _ov_color(source.get("accent")),
        "font_scale": _ov_font_scale(source.get("font_scale")),
    }


def _read_overlay_theme() -> dict[str, Any]:
    with config_store._lock:  # noqa: SLF001 —— 共享实例的既有存储，避免两套配置源
        stored = config_store._data.get(_OV_THEME_KEY)
    return _normalize_overlay_theme(stored)


def _save_overlay_theme(payload: Any) -> dict[str, Any]:
    theme = _normalize_overlay_theme(payload)
    with config_store._lock:  # noqa: SLF001
        config_store._data[_OV_THEME_KEY] = dict(theme)
        config_store.save()
    return theme


def _static_html(filename: str, fallback: str) -> HTMLResponse:
    """读 `web/static/<filename>`；文件缺失（打包/裁剪）时给最小兜底，端点不 500。"""
    path = STATIC_DIR / filename
    if path.exists():
        try:
            return HTMLResponse(path.read_text(encoding="utf-8"))
        except OSError as exc:
            print(f"[Backend] 读取 {path} 失败: {exc}")
    return HTMLResponse(fallback)


def _seed_local_stores() -> None:
    """首次启动时把默认的人设/权限落成文件，方便操作者直接手改。

    这两份配置本来就支持"文件不存在 → 用内置默认值"，这里只是让文件露出来、可被编辑。
    """
    for store, label in ((_persona_store(), "persona"), (_permissions_store(), "permissions")):
        path = getattr(store, "path", None)
        if path is None:
            continue
        try:
            if not Path(path).exists():
                store.set(store.get())
                print(f"[Backend] 已写入默认 {label} 配置: {path}")
        except Exception as e:
            print(f"[Backend] 初始化 {label} 配置失败({path}): {e}")


@app.on_event("startup")
async def startup():
    """Start wire client and orchestrator on startup."""
    global wire, orchestrator

    host = os.getenv("MOD_HOST", "127.0.0.1")
    port = int(os.getenv("MOD_PORT", "25568"))

    wire = WireClient(host, port)
    wire.configure_log_dedupe(
        enabled=config_store.get_log_changes_only(),
        heartbeat_seconds=config_store.get_log_heartbeat_seconds(),
        min_interval_seconds=config_store.get_log_min_interval_seconds(),
    )
    orchestrator = Orchestrator(wire)
    # 原版贴图缓存（`item_icons` 事件）：模组事件统一走 `Session.handle_event`，而
    # `agent/session.py` 不在本批改动范围 —— 所以只在这个实例上**包一层旁听**（见 api/overlay.py）。
    from api import overlay as overlay_api  # noqa: PLC0415

    overlay_api.install_icon_capture(orchestrator)
    _apply_config_to_llm_services()
    _apply_persona_to_session()
    _seed_local_stores()
    # `LCU_HOT_RELOAD=1` ⇒ 开机即开后端热载看门狗（默认关）。
    _hot_reload_autostart()

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
                while w.connected:
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
    return _static_html("index.html", "<h1>Dashboard</h1>")


@app.get("/contacts", response_class=HTMLResponse)
async def get_contacts_page():
    """私信页（通讯录两栏）：左联系人 / 右对话。**不改 master 的 /api/v2/inbox**。"""
    return _static_html("contacts.html", "<h1>Contacts</h1>")


@app.get("/contacts/{uuid}", response_class=HTMLResponse)
async def get_contacts_thread_page(uuid: str):
    """同一页，深链到某个人（前端从路径里取 uuid）。`uuid` 只是展示用，不落库。"""
    return _static_html("contacts.html", "<h1>Contacts</h1>")


# 单页叠加层 + 可视化编辑器（用户口径：OBS 里只叠**一个**网页，参数在一个统一页面里调）。
# 必须**注册在 `/overlay/{component}` 之前** —— Starlette 按注册顺序匹配，否则 `/overlay/editor`
# 会被那个组件页吞掉（当成未知组件 404）。老组件页 `/overlay/<component>` 行为一字不改。
@app.get("/overlay", response_class=HTMLResponse)
async def get_overlay_stage_page():
    """单页叠加层：OBS 里加**一个**浏览器源即可；透明走既有 `?bg=transparent` 约定。"""
    return _static_html("stage.html", "<h1>Overlay</h1>")


@app.get("/overlay/editor", response_class=HTMLResponse)
async def get_overlay_editor_page():
    """可视化布局编辑器（拖组件、调缩放/透明度，存 `/api/overlay/layout`）。"""
    return _static_html("overlay_editor.html", "<h1>Overlay editor</h1>")


@app.get("/overlay/{component}", response_class=HTMLResponse)
async def get_overlay_page(component: str):
    """OBS 单属性子页面：一个页面只渲染一个组件，透明背景、100% 自适应。

    组件名不在注册表里 → 404（避免 OBS 源静默挂在空白页上）。
    """
    if component not in OVERLAY_COMPONENTS:
        raise HTTPException(status_code=404, detail=f"unknown overlay component: {component}")
    return _static_html("overlay.html", "<h1>Overlay</h1>")


@app.get("/api/overlay/components")
async def get_overlay_components():
    """组件注册表（OBS 里要写的 URL + 标题/说明）。加属性 = 加一条注册。"""
    return {"components": _overlay_manifest()}


@app.get("/api/overlay/theme")
async def get_overlay_theme():
    """OBS 页外观默认值（背景/文字色/强调色/字号）。

    空串或 null = 不覆盖页面内置默认；因此**从未保存过**时页面外观与以前完全一致。
    """
    return {"theme": _read_overlay_theme(), "defaults": _normalize_overlay_theme({})}


@app.post("/api/overlay/theme")
async def set_overlay_theme(data: dict):
    """保存 OBS 页外观默认值。非法字段回默认（先校验格式再落盘，绝不原样写进页面）。"""
    return {"status": "ok", "theme": _save_overlay_theme(data)}


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
                response = ""
                error = ""
                session = getattr(orchestrator, "session", None) if orchestrator else None
                if orchestrator:
                    if session is not None and not session.llm_active():
                        # 在场门：不在世界里一律不调 LLM（省 token），但如实告诉操作者为什么。
                        error = (f"未在游戏中（界面：{session.ui_screen or '?'}），已暂停回复；"
                                 "这条消息仍记进了对话记忆")
                    else:
                        try:
                            response = orchestrator.handle_chat(sender, message) or ""
                        except Exception as e:
                            error = f"{type(e).__name__}: {e}"
                        if not response and not error:
                            if session is not None:
                                error = session.llm.get_last_error("planner")
                                if not error and not session.llm.api_key:
                                    error = "LLM 未配置 API Key（见设置页「运行中实际生效」）"
                else:
                    error = "后端会话未启动（模组尚未连接）"

                await ws.send_text(json.dumps({
                    "type": "chat_response",
                    "data": {"response": response, "error": error},
                }))

            elif event_type == "status_subscribe":
                # overlay / 通讯录订阅状态推送：优先走 /ws，别让浏览器源一直轮询。
                if ws not in status_pushers:
                    status_pushers[ws] = asyncio.create_task(_push_status(ws))

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
        _stop_status_push(ws)
        if ws in connected_browsers:
            connected_browsers.remove(ws)


# 订阅了状态推送的浏览器源（overlay）：一个 ws 一条任务，断开即取消（**不写死端口/尺寸**）。
status_pushers: dict[WebSocket, "asyncio.Task[None]"] = {}
OVERLAY_PUSH_INTERVAL = 2.0


async def _push_status(ws: WebSocket) -> None:
    """周期性往订阅方推 `/api/status`；写失败/断开就静默结束，让前端退化为轮询。"""
    try:
        while True:
            await ws.send_text(json.dumps({"type": "status", "data": _status_payload()}))
            await asyncio.sleep(OVERLAY_PUSH_INTERVAL)
    except Exception:
        return


def _stop_status_push(ws: WebSocket) -> None:
    task = status_pushers.pop(ws, None)
    if task is not None and not task.done():
        task.cancel()


def _status_payload() -> dict:
    """`/api/status` 的响应体（REST 与 overlay 的 `/ws` 推送共用同一份）。

    额外给出**模组连接状态**：前端那个"已连接"只代表浏览器↔后端的 WebSocket，
    与游戏/模组无关（这正是"连上了却什么都没有"的误导来源）。
    """
    if orchestrator:
        status = orchestrator.get_status()
        status["mod_connected"] = bool(wire and wire.connected)
        status["wire"] = f"{os.getenv('MOD_HOST', '127.0.0.1')}:{os.getenv('MOD_PORT', '25568')}"
        return status
    return {"running": False, "mod_connected": False}


@app.get("/api/status")
async def get_status():
    """REST endpoint for current status."""
    return _status_payload()


@app.get("/api/llm/effective")
async def get_llm_effective():
    """**运行中实际生效**的每角色配置 + 上次错误 —— 用来对照"配了却没生效"。

    同时报告两处实例（REST 用的 llm_service 与真正跑 planner/decider/vision 的
    session.llm）的生效值，不一致就标出来（历史上正是因为角色级旧值把 default 覆盖掉，
    导致"换了 key/URL 却还在打旧地址"）。
    """
    agents = config_store.raw(redact=False).get("llm", {}).get("agents", {})
    session_llm = orchestrator.session.llm if (orchestrator and orchestrator.session) else None

    roles = []
    for role in config_store.list_agent_roles():
        rid = role["id"]
        own = agents.get(rid, {})
        runtime = session_llm.effective_config(rid) if session_llm else None
        api_side = llm_service.effective_config(rid)
        entry = {
            "id": rid,
            "name": role["name"],
            "purpose": role.get("purpose", ""),
            "provider": own.get("provider", ""),
            "inherited": [k for k in ("provider", "base_url", "api_key", "model") if not own.get(k)],
            "runtime": runtime or api_side,
            "last_error": (session_llm or llm_service).get_last_error(rid),
        }
        if runtime and (runtime["base_url"] != api_side["base_url"] or runtime["model"] != api_side["model"]):
            entry["mismatch"] = api_side
        roles.append(entry)

    return {
        "roles": roles,
        "mod_connected": bool(wire and wire.connected),
        "note": "runtime = 真正执行 planner/decider/vision 的实例；继承 = 该字段沿用 default",
    }


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


@app.get("/api/llm/roles")
async def get_llm_roles():
    """List AI roles (planner / decider / vision / embedding) for per-role model config."""
    return {"roles": config_store.list_agent_roles()}


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
    # 日志开关改动立刻生效（不必重启）：wire 还没连上时也无害。
    if wire is not None:
        wire.configure_log_dedupe(
            enabled=config_store.get_log_changes_only(),
            heartbeat_seconds=config_store.get_log_heartbeat_seconds(),
            min_interval_seconds=config_store.get_log_min_interval_seconds(),
        )
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
    """人设：`.local/persona.json`（三段自由文本 + 备用风格）与 legacy 字段的合并视图。"""
    persona = dict(config_store.get_persona())
    persona.update(_persona_store().get())
    persona["sections"] = _persona_store().status()
    return persona


@app.post("/api/persona")
async def set_persona(data: dict):
    """写人设。三段（identity/reply_style/scene_rules + 备用风格）落 `.local/persona.json`
    —— 它是人设的**唯一权威来源**；只有 `external_context`（上游注入）仍存 config.json。"""
    sections = {key: value for key, value in (data or {}).items() if key in PERSONA_KEYS}
    external = {key: value for key, value in (data or {}).items() if key == "external_context"}
    if sections:
        _persona_store().set(sections)
    if external:
        config_store.set_persona(external)
    _apply_persona_to_session()
    merged = dict(config_store.get_persona())
    merged.update(_persona_store().get())
    return {"status": "ok", "persona": merged}


@app.get("/api/permissions")
async def get_permissions():
    """权限配置（`.local/permissions.json`）：master/friends/server_mode/频率参数。"""
    return _permissions_store().get()


@app.post("/api/permissions")
async def set_permissions(data: dict):
    """改权限配置；改完立刻生效（ReplyGate 实时读盘）。"""
    saved = _permissions_store().set(data or {})
    session = orchestrator.session if orchestrator else None
    store = getattr(session, "permissions", None)
    if isinstance(store, PermissionsStore) and hasattr(store, "reload"):
        try:
            store.reload()
        except Exception:
            pass
    return {"status": "ok", "permissions": saved}


def _operator_actor(requested: str) -> tuple[str, Optional[str]]:
    """仪表盘上的任务操作算谁做的。

    传了 `actor` 就按那个人查权限（tier 由 permissions.json 决定）；
    没传就是本地控制台在操作 —— 用配置里的第一位主人；连主人名单都空，则明确按 master 席位执行
    （这台机器上的仪表盘本来就是操作台，和直接发 wire 指令等价）。
    """
    actor = str(requested or "").strip()
    if actor:
        return actor, None
    masters = _permissions_store().get().get("master") or []
    if masters:
        return str(masters[0]), None
    return "", "master"


@app.get("/api/tasks")
async def get_tasks():
    """任务列表（多任务 + 归属 + 优先级 + 状态 + 进度）。"""
    session = orchestrator.session if orchestrator else None
    tasks = getattr(session, "tasks", None)
    if tasks is None:
        return {"items": [], "active": None, "count": 0}
    return tasks.status()


@app.post("/api/tasks/cancel")
async def cancel_task(data: dict):
    """取消任务：master 可取消任意；friend 只管自己的（权限由 tier 规则判定）。"""
    session = orchestrator.session if orchestrator else None
    if session is None:
        raise HTTPException(status_code=409, detail="会话未启动（模组未连接）")
    actor, tier = _operator_actor((data or {}).get("actor"))
    ok, reason = session.cancel_task(str((data or {}).get("id") or ""), actor=actor, tier=tier)
    return {"status": "ok" if ok else "denied", "reason": reason,
            "task_list": session.tasks.status()}


@app.post("/api/tasks/activate")
async def activate_task(data: dict):
    """插队：把某个任务提到最前（master 任意 / friend 仅自己的）。"""
    session = orchestrator.session if orchestrator else None
    if session is None:
        raise HTTPException(status_code=409, detail="会话未启动（模组未连接）")
    actor, tier = _operator_actor((data or {}).get("actor"))
    ok, reason = session.activate_task(str((data or {}).get("id") or ""), actor=actor, tier=tier)
    return {"status": "ok" if ok else "denied", "reason": reason,
            "task_list": session.tasks.status()}


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


# ── 私信（通讯录）+ 指令白名单 ──────────────────────────────────────
# 端点全部自建（**不用** master 的 `/api/v2/inbox/*`）。运行期一律用 session 里那一份，
# 进程还没起来时退回模块级实例（懒建，避免 import 时就写盘）。

_fallback_commands: CommandsStore | None = None
_fallback_contacts: ContactStore | None = None


def _commands_store() -> CommandsStore:
    global _fallback_commands
    session = orchestrator.session if orchestrator else None
    store = getattr(session, "commands_store", None)
    if isinstance(store, CommandsStore):
        return store
    if _fallback_commands is None:
        _fallback_commands = CommandsStore()
    return _fallback_commands


def _contacts_store() -> ContactStore:
    global _fallback_contacts
    session = orchestrator.session if orchestrator else None
    store = getattr(session, "contacts", None)
    if isinstance(store, ContactStore):
        return store
    if _fallback_contacts is None:
        _fallback_contacts = ContactStore()
    return _fallback_contacts


def _contacts_namespace() -> str:
    """通讯录的 server 分区键（与对话记忆/私信落盘同一套）。"""
    session = orchestrator.session if orchestrator else None
    if session is not None:
        try:
            key = str(session.current_server_key() or "")
            if key:
                return key
        except Exception:
            pass
    return "unknown"


def _online_names() -> list[str]:
    """当前在世界的玩家名（模组按距离报的 `runtime["players"]`）；拿不到就空 —— **不猜**。"""
    session = orchestrator.session if orchestrator else None
    runtime = getattr(session, "runtime", None)
    players = runtime.get("players") if isinstance(runtime, dict) else None
    if not isinstance(players, list):
        return []
    names = []
    for player in players:
        if isinstance(player, dict) and player.get("name"):
            names.append(str(player["name"]))
    return names


@app.get("/api/contacts")
async def get_contacts():
    """通讯录：每个联系人的名字/uuid/层级/未读/未回/最后一条（按最后一条时间倒序）。"""
    store = _contacts_store()
    session = orchestrator.session if orchestrator else None
    permissions = getattr(session, "permissions", None)
    tier_of = permissions.tier_of if isinstance(permissions, PermissionsStore) else None
    key = _contacts_namespace()
    return {"server": key,
            "contacts": store.contacts(key, tier_of=tier_of, online_names=_online_names())}


@app.get("/api/contacts/{uuid}/messages")
async def get_contact_messages(uuid: str, limit: int = 50):
    """某个联系人在**本服**的往来消息（时间从早到晚）。"""
    store = _contacts_store()
    key = _contacts_namespace()
    return {"server": key, "target": uuid,
            "messages": store.messages(key, uuid, limit=max(1, min(int(limit), 500)))}


@app.post("/api/contacts/{uuid}/send")
async def send_contact_message(uuid: str, data: dict):
    """人**代发**一条私信：同样过出站清洗 + 白名单（不许绕开闸门）。

    `send_whisper` 的目标必须是**能解析成唯一在线玩家的名字**，所以这里先把线程里的
    显示名取出来（没有就用 uuid 兜底，让下游如实回 `not_found`）。
    """
    text = str((data or {}).get("text") or (data or {}).get("message") or "")
    if not text.strip():
        raise HTTPException(status_code=400, detail="text is required")
    session = orchestrator.session if orchestrator else None
    if session is None:
        raise HTTPException(status_code=503, detail="session not ready")
    store = _contacts_store()
    key = _contacts_namespace()
    thread = store.export_thread(key, uuid)
    target = str(thread.get("name") or "").strip() or uuid
    # 仪表盘上的人手动代发 → 按 master 层级（和任务操作台同一口径）。
    result = session.send_whisper(target, text, tier="master")
    if not result.get("ok"):
        return {"status": "blocked", "target": target, **result}
    return {"status": "ok", "target": target, **result}


@app.post("/api/contacts/{uuid}/read")
async def mark_contact_read(uuid: str):
    """把某人的来信标为已读；返回改动条数。"""
    store = _contacts_store()
    key = _contacts_namespace()
    return {"status": "ok", "server": key, "marked": store.mark_read(key, uuid)}


def _command_family(name: Any) -> str:
    """指令族名（`minecraft:kill` → `kill`）；与 `CommandGate` 的判定口径一致。"""
    text = str(name or "").strip()
    while text.startswith("/"):
        text = text[1:].strip()
    return text.casefold().rsplit(":", 1)[-1]


def _is_hard_rejected(name: Any) -> bool:
    """这条指令是不是硬黑名单（代码里硬拒 → 配置也放不开，UI 不可勾选）。"""
    return _command_family(name) in HARD_BLACKLIST


def _requested_command_names(payload: Any) -> list[str]:
    """从 UI 回包里收集"它请求登记的指令名"（global/commands/servers 都算）。"""
    data = payload if isinstance(payload, dict) else {}
    names: list[str] = []

    def _collect(value: Any) -> None:
        items = value if isinstance(value, (list, tuple, set)) else [value]
        for item in items:
            if isinstance(item, str):
                names.append(item)
            elif isinstance(item, dict):
                picked = item.get("name") or item.get("command")
                if picked:
                    names.append(str(picked))

    for key in ("global", "commands"):
        if key in data:
            _collect(data[key])
    servers = data.get("servers")
    if isinstance(servers, dict):
        for value in servers.values():
            _collect(value)
    return names


def _command_entries_view(store: "CommandsStore", server_key: str) -> list[dict]:
    """UI 表格用的条目视图：全局 ∪ 本服，逐条标出**作用域 / 硬拒 / 需二次确认**。

    形如 `[{name, usage, desc, tiers, enabled, cooldown, scope, hard_rejected, high_risk}]`。
    """
    current = store.get()
    key = str(server_key or "").strip()
    rows: list[dict] = []
    for item in current.get("commands") or []:
        rows.append({**item, "scope": "global"})
    for candidate, entries in (current.get("servers") or {}).items():
        if str(candidate).casefold() != key.casefold():
            continue
        for item in entries or []:
            rows.append({**item, "scope": str(candidate)})
    for row in rows:
        row["hard_rejected"] = _is_hard_rejected(row.get("name"))
        row["high_risk"] = _command_family(row.get("name")) in HIGH_RISK
    return rows


@app.get("/api/commands")
async def get_commands_config():
    """指令白名单（唯一来源 `.local/commands.json`）+ 硬黑名单/高风险清单。

    硬黑名单**代码里硬拒、配置放不开**，列出来只是让 UI 不显示勾选框。
    另给 `entries`（UI 表格直接消费的条目视图，默认空）。
    """
    store = _commands_store()
    key = _contacts_namespace()
    return {
        "config": store.get(),
        "entries": _command_entries_view(store, key),
        "hard_blacklist": sorted(HARD_BLACKLIST),
        "high_risk": sorted(HIGH_RISK),
        "server": key,
        "effective": store.whitelist_for(key),
    }


@app.post("/api/commands")
async def set_commands_config(data: dict):
    """改指令白名单/私信指令族/TPA 话术；改完即时生效（同一个 store 实例）。

    **硬黑名单在服务端拦截**：请求里写进来的 kill/op/… 会被剔掉并在 `rejected` 里如实回报
    （前端只是展示，不能靠它绕过）。
    """
    store = _commands_store()
    payload = data if isinstance(data, dict) else {}
    requested = _requested_command_names(payload)
    saved = store.set(payload)
    key = _contacts_namespace()
    rejected = [name for name in requested if _is_hard_rejected(name)]
    return {"status": "ok", "config": saved,
            "entries": _command_entries_view(store, key),
            "effective": store.whitelist_for(key),
            "rejected": rejected}


# ── 只读目录 · 治理审计 · job 取消（附加式）─────────────────────────────────────
# 三条纪律（全部是**只读暴露**，不改任何既有路由/启动/鉴权行为）：
#   ① 工具/复合体目录的**单一事实来源就是注册表**（`agent/tool_registry.py` 的 `TOOL_SPECS`
#      与 `agent/composites.py` 的 `COMPOSITES`）—— 不手写第二份清单 ✗；
#   ② 治理/租约审计读的是**进程内环形缓冲**（各模块自带容量上限），**不落盘** ✗，
#      拿不到历史就如实说"仅进程内最近 N 条"，不假装有台账；
#   ③ job 取消走 job **自己的取消路径**（`JobRegistry.cancel` → 回调），这里只补一个 HTTP 口。
from agent.composites import COMPOSITES  # noqa: E402
from agent.control_lease import AUDIT_MAX as CONTROL_LEASE_RING  # noqa: E402
from agent.job_registry import AUDIT_MAX as JOB_RING  # noqa: E402
from agent.resource_lease import AUDIT_MAX as RESOURCE_LEASE_RING  # noqa: E402
from agent.state_governor import GOV_AUDIT_MAX as GOVERNOR_RING  # noqa: E402
from agent.tool_registry import TOOL_BY_NAME, TOOL_SPECS  # noqa: E402

# 审计缓冲**不落盘**这一点的诚实口径（口径统一，前端直接显示）。
AUDIT_NOTE = ("仅展示**进程内**最近的环形缓冲：这些审计不落盘、进程重启即清空；"
              "每段都标了容量（ring），拿不到就是真的没有，不假装有历史。")


def _permission_view(permission: Any) -> dict:
    """`ToolPermission` → 一读就懂的门清单（空描述符＝无额外门）。"""
    fields = ("tier", "container", "read_only", "command", "borrow", "area")
    gates: dict[str, Any] = {name: getattr(permission, name, "") or False for name in fields}
    gates["tier"] = str(getattr(permission, "tier", "") or "")
    return {
        "gates": gates,
        "active": [name for name in fields if gates[name]],
        "deny_label": str(getattr(permission, "deny_label", "") or ""),
        "note": str(getattr(permission, "note", "") or ""),
    }


def _check_view(check: Any) -> dict:
    """复合体一步的**具名**前置条件（`Check`）→ 人读；没有就是"总是跑"。"""
    if check is None:
        return {}
    kind = str(getattr(check, "kind", "") or "")
    item = str(getattr(check, "item", "") or "")
    return {"kind": kind, "item": item, "count": int(getattr(check, "count", 1) or 1)}


@app.get("/api/tools")
async def get_tools_catalog():
    """工具目录（只读）。**单一事实来源 = `agent/tool_registry.TOOL_SPECS`**。

    `job=true` 的条目在派发时登记 job 句柄（在「操作」页能看到句柄并走它自己的取消路径）。
    """
    rows = [{
        "name": spec.name,
        "description": spec.description,
        "args": spec.args,
        "arg_names": list(spec.parameters()["properties"]),
        "parameters": spec.parameters(),
        "groups": list(spec.groups),
        "handler": spec.handler,
        "job": bool(spec.job),
        "permission": _permission_view(spec.permission),
    } for spec in TOOL_SPECS]
    return {"tools": rows, "count": len(rows),
            "job_count": sum(1 for row in rows if row["job"]),
            "source": "agent/tool_registry.TOOL_SPECS"}


@app.get("/api/composites")
async def get_composites_catalog():
    """复合体清单（只读）。**单一事实来源 = `agent/composites.COMPOSITES`**。

    每一步只声明"调哪个已注册工具 + 参数从哪来（+ 具名前置条件）"；`registered` 逐条
    标出它引用的工具在注册表里是否真的存在（不猜）。
    """
    rows = [{
        "name": comp.name,
        "description": comp.description,
        "args": list(comp.args),
        "steps": [{
            "tool": step.tool,
            "label": step.label,
            "args": dict(step.args),
            "when": _check_view(step.when),
            "when_missing": str(getattr(step, "when_missing", "") or ""),
            "optional": bool(step.optional),
            "note": str(step.note or ""),
            "registered": step.tool in TOOL_BY_NAME,
        } for step in comp.steps],
    } for comp in COMPOSITES]
    return {"composites": rows, "count": len(rows), "available": True,
            "source": "agent/composites.COMPOSITES"}


def _audit_of(owner: Any, n: int = 0) -> list[dict]:
    """防御式取某个审计句柄的环形缓冲（拿不到就是空列表，绝不把异常甩给前端）。"""
    getter = getattr(owner, "events", None)
    if not callable(getter):
        return []
    try:
        rows = getter(n)
    except Exception:  # noqa: BLE001 —— 审计读失败不拖垮面板
        return []
    return [dict(row) for row in rows] if isinstance(rows, list) else []


def _snapshot_of(owner: Any) -> dict:
    getter = getattr(owner, "snapshot", None)
    if not callable(getter):
        return {}
    try:
        data = getter()
    except Exception:  # noqa: BLE001
        return {}
    return dict(data) if isinstance(data, dict) else {}


@app.get("/api/governance")
async def get_governance():
    """治理 / 资源租约 / 控制租约 / job 的**进程内**审计（只读）。

    分开三块，因为它们是三件事：
      * `governor`：状态治理（卡死即摘掉）——谁卡住、何时、为什么、被谁摘掉；
      * `resource_lease`：按**资源**分租——谁持有、排队、**抢了谁的什么**；
      * `control_lease`：底层**控制权**租约（左脑一方；右脑不可持）= 持租人/理由/时限/能力位。
    """
    session = orchestrator.session if orchestrator else None
    if session is None:
        return {"running": False, "persisted": False, "note":
                "会话未启动（模组未连接）：没有运行中的治理/租约实例可读；"
                "这些审计本来就只在进程内，没有历史可翻。",
                "governor": {}, "resource_lease": {}, "control_lease": {}, "jobs": {}}
    planner = getattr(session, "planner", None)
    governor = getattr(session, "state_governor", None)
    resource_lease = getattr(session, "resource_lease", None)
    control_lease = getattr(planner, "control_lease", None)
    jobs = getattr(session, "jobs", None)
    return {
        "running": True, "persisted": False, "note": AUDIT_NOTE,
        "governor": {**_snapshot_of(governor), "ring": GOVERNOR_RING,
                     "audit": _audit_of(governor)},
        "resource_lease": {**_snapshot_of(resource_lease), "ring": RESOURCE_LEASE_RING,
                           "audit": _audit_of(resource_lease)},
        "control_lease": {**_snapshot_of(control_lease), "ring": CONTROL_LEASE_RING,
                          "audit": _audit_of(control_lease)},
        "jobs": {**_snapshot_of(jobs), "ring": JOB_RING},
    }


@app.post("/api/jobs/cancel")
async def cancel_job(data: dict):
    """取消一个 job 句柄：走**它自己的取消路径**（`JobRegistry.cancel` → 回调），再标 cancelled。

    句柄列表是只读的 `/api/status` → `jobs`；这里只补一个取消口（附加式）。
    四样门槛里的「取消路径」在登记时就必须可执行，所以运行中的 job 一定有路径可走。
    """
    session = orchestrator.session if orchestrator else None
    if session is None:
        raise HTTPException(status_code=409, detail="会话未启动（模组未连接）")
    registry = getattr(session, "jobs", None)
    if registry is None:
        raise HTTPException(status_code=409, detail="本会话没有 job 台账")
    job_id = str((data or {}).get("id") or "").strip()
    if not job_id:
        raise HTTPException(status_code=400, detail="id is required")
    reason = str((data or {}).get("reason") or "operator cancelled")
    ok, why = registry.cancel(job_id, reason=reason)
    return {"status": "ok" if ok else "denied", "reason": why, "job": job_id,
            "jobs": registry.snapshot()}


# ── 后端热载（进程内，附加式）───────────────────────────────────────────────────
# 用户口径："后端服务器改了可以热载" —— **不重启进程、不掉 wire 连接**（`uvicorn --reload`
# 是重启进程，会掉线并清空运行期状态，本仓不用它）。白/黑名单与机制见
# `agent/hot_reload.py` 的模块 docstring；这里只做**接线**三件：
#   ① 忙的判据（读现有状态字段，不另造一套）；
#   ② 真正干活的那一步（重载 + 重建会话里的 planner）；
#   ③ 看门狗开关（进程生命周期的东西，放这儿）。
# 一次性热载口挂在 `api/http_surface.py` 的 `POST /api/reload`（调的就是下面这两个函数）。
HOT_RELOAD_INTERVAL = float(os.getenv("LCU_HOT_RELOAD_INTERVAL", "2") or 2)
_hot_reload_watcher: Optional[hot_reload.HotReloadWatcher] = None
_hot_reload_lock = threading.Lock()


def hot_reload_busy() -> Optional[str]:
    """热载"忙"的判据（**全部读现有状态字段**，不另造一套）：

    * 会话有**未终态**的 job（`session.jobs.active()`）；
    * `runtime["task_state"]` 既不是 idle 也不是终态（与 `Session.tick` 的 `task_active` 同口径）；
    * planner 上有**挂起**的复合体（`planner._composite_runs`：在等 await_check 续跑的那些）。

    返回 `None` = 空闲；否则是"现在在跑什么"的人读描述（直接回给前端）。
    """
    session = orchestrator.session if orchestrator else None
    if session is None:
        return None                     # 还没连模组 / 会话没起来 ⇒ 没有"在跑的东西"
    running: list[str] = []
    jobs = getattr(session, "jobs", None)
    if jobs is not None:
        try:
            running += [f"job {getattr(job, 'id', '?')}({getattr(job, 'tool', '?')})"
                        for job in jobs.active()]
        except Exception as e:  # noqa: BLE001 —— 判据读不到就当没有，绝不把异常甩出去
            print(f"[Backend] 热载忙判据：读 jobs 失败 {e}")
    runtime = getattr(session, "runtime", None)
    task_state = runtime.get("task_state") if isinstance(runtime, dict) else None
    if isinstance(task_state, dict):
        kind = str(task_state.get("kind", "idle")).strip().lower()
        status = str(task_state.get("status", "idle")).strip().lower()
        if kind != "idle" and status not in {"", "idle", "done", "failed", "cancelled"}:
            running.append(f"task_state {kind}={status}")
    composite_runs = getattr(getattr(session, "planner", None), "_composite_runs", None)
    if composite_runs:
        running.append(f"挂起的复合体 x{len(composite_runs)}")
    return "、".join(running) or None


def perform_hot_reload(changed: Optional[list[str]] = None) -> dict:
    """真正干活的那一步：重载白名单模块 + 用会话**已有**的构件重建 `session.planner`。

    `changed` 只用于"哪些改了要重启"的报告（`None` ⇒ 与进程启动基线比对，自动算）。
    """
    report = hot_reload.reload_agent_modules(changed)
    session = orchestrator.session if orchestrator else None
    if session is not None and report.get("reloaded") and hasattr(session, "planner"):
        rebuild = hot_reload.rebuild_session_planner(session)
        report["planner"] = rebuild
        if not rebuild.get("ok"):
            print(f"[Backend] 热载：planner 重建未成功（{rebuild.get('reason')}）")
    return report


def hot_reload_watch(enabled: bool) -> dict:
    """开/关热载看门狗。开着时**只在空闲时**自动热载（忙则跳过并打一行日志）。"""
    global _hot_reload_watcher
    if enabled:
        with _hot_reload_lock:
            if _hot_reload_watcher is None:
                _hot_reload_watcher = hot_reload.HotReloadWatcher(
                    hot_reload.source_paths(),
                    busy=hot_reload_busy,
                    on_reload=perform_hot_reload,
                    interval=HOT_RELOAD_INTERVAL,
                )
            started = _hot_reload_watcher.start()
        state = _hot_reload_watch_state()
        state["started"] = started
        return state
    watcher = _hot_reload_watcher
    state: dict = {"stopped": bool(watcher.stop()) if watcher is not None else False}
    state.update(_hot_reload_watch_state())
    return state


def _hot_reload_watch_state() -> dict:
    watcher = _hot_reload_watcher
    return {"enabled": bool(watcher is not None and watcher.running),
            "interval": HOT_RELOAD_INTERVAL,
            "status": watcher.status() if watcher is not None else {}}


def _hot_reload_autostart() -> None:
    """`LCU_HOT_RELOAD=1` ⇒ 开机即开看门狗（默认**关**：不替操作者做主）。"""
    if os.getenv("LCU_HOT_RELOAD", "").strip().lower() in {"1", "true", "yes", "on"}:
        state = hot_reload_watch(True)
        print(f"[Backend] 后端热载看门狗已开启（LCU_HOT_RELOAD=1，间隔 "
              f"{state['interval']:.1f}s）：只在空闲时热载")


@app.post("/api/reload/watch")
async def post_hot_reload_watch(data: dict):
    """开关热载看门狗：`{"enabled": true|false}`。开着时**只在空闲时**自动热载。"""
    payload = data if isinstance(data, dict) else {}
    enabled = payload.get("enabled")
    if not isinstance(enabled, bool):
        raise HTTPException(status_code=400, detail="enabled must be a boolean")
    state = hot_reload_watch(enabled)
    note = ("看门狗已开启：只在空闲时自动热载（忙则跳过并打日志）" if enabled
            else "看门狗已关闭：不再自动热载（POST /api/reload 仍可手动热载）")
    return {"ok": True, "note": note, **state}


# ── HTTP 面回捞（附加式）────────────────────────────────────────────────────────
# master 有、26.3 缺、且**本机真有数据源**的端点全在 `api/http_surface.py` 里：
# 这里只多做一件"挂上去"的事，上面任何既有路由/启动/鉴权/静态文件逻辑都不动。
# 缺口与逐条理由见 `docs/dev/PORT_LIST.md` §4 的结项清单。
from api import http_surface  # noqa: E402
from api import overlay as overlay_api  # noqa: E402

http_surface.install(app)
overlay_api.install(app)
