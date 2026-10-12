"""HTTP 面回捞（附加式）：`master` 有、26.3 缺、且**本机真有数据源**的端点。

三条硬口径（与 `docs/dev/PORT_LIST.md` §4 的回捞报告一一对应）：

1. **只新增**：本模块只挂新路由；不改任何既有路由的行为，不碰启动/鉴权/静态文件逻辑。
2. **形状以 SDK 为准**：路径、方法、响应字段名照抄 `backend/sdk/**`（Apache-2.0 的第三方
   接入面，也是 `master:backend/server.py` 的实现）。SDK 与 master 冲突时以 SDK 为准
   （本批没有这种冲突）。
3. **拿不到真数据就不挂**：宁缺勿假。每个端点的数据来源都写在它的 docstring 里；
   没挂的端点与理由见 `docs/dev/PORT_LIST.md` §4 的结项清单。

数据来源速查：

======================  ==========================================================
端点                     真数据来源
======================  ==========================================================
`GET /api/sdk/info`      本进程**真实路由表**（自省，随实现自动漂移）+ 已实现接口分组
`POST /api/sdk/chat`     `Orchestrator.handle_chat` → `Session.handle_chat`（人格/记忆/Planner）
`POST /api/sdk/command`  `Session.skills` 的低层命令出口（真 wire 命令 + 会话指令审计）
`GET /api/v2/control`    `Planner.control_lease`（活对象，生存反射的抢占登记在这里）
`GET /api/v2/audit`     `Session` 的安全审计落盘（`data/audit/security.jsonl`）
`POST /api/reload`      `agent/hot_reload.py`：白名单模块真热载 + 用会话现有构件重建 planner
======================  ==========================================================
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query

from agent.audit_log import AuditLog

# SDK API 版本（`docs/sdk.md`：当前为 `1`）。
SDK_API_VERSION = "1"

# SDK 声明为**保留**的身体命令：控制面原语，第三方不许借 `/api/sdk/command` 直接驱动
# （与 master `RESERVED_BODY_COMMANDS` 逐字相同；其中 `toggle_ai`/`shutdown` 在 26.3 的
# `ActionExecutor` 里是真实命令，所以这条闸门是有意义的，不是摆设）。
RESERVED_BODY_COMMANDS = frozenset({"control_external", "control_builtin", "toggle_ai", "shutdown"})

# master 用来防"绕开围栏"的参数名前缀（`__lcu_fencing_token` / `__lcu_world_epoch` …）。
# 26.3 的 wire 协议里没有这些字段（`git grep __lcu_` 只命中死簇 `task_coordinator.py`），
# 所以这里只作为"别把 master 的控制面从 SDK 塞回来"的闸门。
RESERVED_ARG_PREFIX = "__lcu_"

# 安全审计的落盘路径：与 `Session._on_security_audit`（`agent/session.py`）逐字一致。
AUDIT_PATH = "data/audit/security.jsonl"

# SDK 客户端的接口分组（`docs/sdk.md` 的三组）。每组列的是**定义该组**的端点；
# 只有全部真的挂了，这个分组才算"可用"——避免 `/api/sdk/info` 说一套做一套。
SDK_INTERFACES: dict[str, tuple[tuple[str, str], ...]] = {
    "gateway": (("POST", "/api/sdk/chat"), ("GET", "/api/sdk/context"),
                ("POST", "/api/sdk/context")),
    "observer": (("GET", "/api/status"), ("GET", "/api/session"), ("GET", "/api/memory"),
                 ("GET", "/api/config")),
    "actuator": (("POST", "/api/sdk/command"),),
}

# `backend/sdk/python_client.py` / `browser_client.js` 实际会打的全部端点（逐条抄自 SDK）。
# `/api/sdk/info` 用它自省"本进程实现了哪些 SDK 面、缺哪些"，省得集成方去猜。
SDK_ENDPOINTS: tuple[tuple[str, str], ...] = (
    ("GET", "/api/status"),
    ("GET", "/api/sdk/info"),
    ("GET", "/api/v2/info"),
    ("GET", "/api/session"),
    ("GET", "/api/memory"),
    ("GET", "/api/sdk/identity"),
    ("POST", "/api/sdk/identity"),
    ("GET", "/api/llm/providers"),
    ("POST", "/api/llm/config"),
    ("POST", "/api/llm/models"),
    ("GET", "/api/persona"),
    ("POST", "/api/persona"),
    ("GET", "/api/sdk/context"),
    ("POST", "/api/sdk/context"),
    ("POST", "/api/sdk/chat"),
    ("POST", "/api/sdk/command"),
    ("GET", "/api/v2/body-requests/{request_id}"),
    ("GET", "/api/v2/skills"),
    ("GET", "/api/v2/skills/{skill_id}"),
    ("POST", "/api/v2/skills/{skill_id}/runs"),
    ("GET", "/api/v2/task-presets"),
    ("GET", "/api/v2/task-presets/{preset_id}"),
    ("POST", "/api/v2/task-presets/{preset_id}/runs"),
    ("GET", "/api/v2/control"),
    ("POST", "/api/v2/control/leases"),
    ("POST", "/api/v2/control/leases/{lease_id}/heartbeat"),
    ("POST", "/api/v2/control/leases/{lease_id}/release"),
    ("POST", "/api/v2/player-pairings"),
    ("GET", "/api/v2/runs"),
    ("GET", "/api/v2/runs/{run_id}"),
    ("POST", "/api/v2/runs/{run_id}/cancel"),
    ("POST", "/api/v2/runs/{run_id}/resume"),
    ("GET", "/api/v2/events"),
    ("GET", "/api/v2/schedules"),
    ("POST", "/api/v2/schedules"),
    ("PATCH", "/api/v2/schedules/{schedule_id}"),
    ("DELETE", "/api/v2/schedules/{schedule_id}"),
    ("GET", "/api/config"),
    ("POST", "/api/config"),
)

_PARAM_RE = re.compile(r"\{[^}]*\}")

router = APIRouter()


def install(app: Any) -> None:
    """把回捞的端点挂到既有 app 上（`server.py` 末尾调用一次）。"""
    app.include_router(router)


# ── 进程内真实状态（延迟导入 server：它在文件末尾 include 本模块，模块级导入会成环）──


def _server_module() -> Any:
    import server  # noqa: PLC0415 —— 见上面的原因

    return server


def _orchestrator() -> Any:
    return getattr(_server_module(), "orchestrator", None)


def _session() -> Any:
    """当前 `Session`；进程还没连模组（或还在启动）时为 `None`。"""
    return getattr(_orchestrator(), "session", None)


def _wire() -> Any:
    return getattr(_server_module(), "wire", None)


def _body_connected() -> bool:
    wire = _wire()
    return bool(wire is not None and getattr(wire, "connected", False))


def _leaf_routes(routes: Any) -> list[Any]:
    """展开 `include_router` 的懒包装。

    FastAPI 0.141（本仓运行的版本）里 `include_router` **不再把子路由平铺**进
    `app.routes`，而是放一个 `_IncludedRouter` 标记对象（真正的路由在
    `original_router.routes` 里）。路由匹配照样生效，但"自省路由表"必须下钻。
    """
    leaves: list[Any] = []
    for route in routes or ():
        inner = getattr(getattr(route, "original_router", None), "routes", None)
        if inner is not None:
            leaves.extend(_leaf_routes(inner))
        else:
            leaves.append(route)
    return leaves


def _registered_endpoints() -> set[tuple[str, str]]:
    """本进程**真实注册**的 (方法, 路径模板) 集合（路径里的参数名已归一化）。"""
    app = getattr(_server_module(), "app", None)
    endpoints: set[tuple[str, str]] = set()
    for route in _leaf_routes(getattr(app, "routes", None)):
        path = _PARAM_RE.sub("{}", str(getattr(route, "path", "")))
        for method in getattr(route, "methods", None) or ():
            endpoints.add((str(method).upper(), path))
    return endpoints


def _payload(data: Any) -> dict[str, Any]:
    return data if isinstance(data, dict) else {}


# ── GET /api/sdk/info ────────────────────────────────────────────────────────


@router.get("/api/sdk/info")
async def get_sdk_info():
    """SDK 能力自述。

    真数据来源：本进程的**路由表**（`server.app.routes`）。所以：

    * `interfaces` 只列**真的全部挂上**的分组（不是抄一份期望清单）；
    * `sdk_endpoints.implemented/missing` 是拿 SDK 实际会打的端点逐条比对得到的结果
      —— 缺的那批（V2 skills/task-presets/runs/schedules/leases/…）在 26.3 没有真实
      数据源或已被 §9.4 判为不搬，集成方看这里就知道边界，不用试错。

    `auth_required` 为 `False` 是事实：26.3 没有 SDK token / 鉴权中间件（master 有
    `SDK_API_TOKEN`）。`supported_versions` 只报 `1`：V2 面只回了 `control`/`audit`
    两件，报 `2` 会夸大。
    """
    registered = _registered_endpoints()
    interfaces = [
        name
        for name, group in SDK_INTERFACES.items()
        if all((method, _PARAM_RE.sub("{}", path)) in registered for method, path in group)
    ]
    implemented: list[str] = []
    missing: list[str] = []
    for method, path in SDK_ENDPOINTS:
        line = f"{method} {path}"
        if (method, _PARAM_RE.sub("{}", path)) in registered:
            implemented.append(line)
        else:
            missing.append(line)
    return {
        "api_version": SDK_API_VERSION,
        "auth_required": False,
        "interfaces": interfaces,
        "supported_versions": [SDK_API_VERSION],
        "sdk_endpoints": {"implemented": implemented, "missing": missing},
    }


# ── Gateway：POST /api/sdk/chat ──────────────────────────────────────────────


@router.post("/api/sdk/chat")
async def sdk_chat(data: dict):
    """外部 SDK 发一条聊天：**走同伴自己的人格/记忆/Planner**，不是一个旁路机器人。

    真数据来源：`Orchestrator.handle_chat` → `Session.handle_chat`（回复文本就是同伴
    真回的那句；它按 `ReplyGate` 决定不理人时返回 `None` ⇒ 这里如实回空串，不编话）。
    LLM 调用会阻塞，所以丢到线程里跑（与 master 一致）。
    """
    orchestrator = _orchestrator()
    if orchestrator is None or getattr(orchestrator, "session", None) is None:
        raise HTTPException(status_code=503, detail="Companion session is not running")
    payload = _payload(data)
    message = str(payload.get("message") or "")
    if not message:
        raise HTTPException(status_code=422, detail="message must be a non-empty string")
    sender = str(payload.get("sender") or "sdk")
    response = await asyncio.to_thread(orchestrator.handle_chat, sender, message)
    return {"status": "ok", "response": response or ""}


# ── Actuator：POST /api/sdk/command ──────────────────────────────────────────


@router.post("/api/sdk/command")
async def sdk_command(data: dict):
    """外部 SDK 把一条**低层身体命令**直接发给已连接的客户端模组。

    真数据来源：`Session.skills` 的低层发送出口（每个技能方法都从它出去）——真 wire
    命令帧 + 会话的指令审计观察者（`command_context("sdk")` 标出请求方）。

    诚实边界：
    * 命令认不认识由 **Java 侧**判定（`ActionExecutor` 的
      `default -> sendResponse(id, false, "Unknown: …")`）——这里只负责如实下发；
    * 只回 `request_id`，**不假装**能查终态：master 的 `/api/v2/body-requests/{id}`
      依赖进程内诊断存储，26.3 没有这条实现（`PORT_LIST.md` §9.4 明确不搬）。
    """
    session = _session()
    if session is None:
        raise HTTPException(status_code=503, detail="Companion session is not running")
    if not _body_connected():
        raise HTTPException(status_code=503, detail="Companion body is not connected")
    skills = getattr(session, "skills", None)
    if skills is None:
        raise HTTPException(status_code=503, detail="Body command channel is not available")
    payload = _payload(data)
    command = str(payload.get("command") or "").strip()
    if not command:
        raise HTTPException(status_code=422, detail="command must be a non-empty string")
    if command in RESERVED_BODY_COMMANDS:
        raise HTTPException(status_code=403, detail="Reserved control command")
    args = payload.get("args")
    if args is None:
        args = {}
    if not isinstance(args, dict):
        raise HTTPException(status_code=422, detail="args must be an object")
    if any(str(key).startswith(RESERVED_ARG_PREFIX) for key in args):
        raise HTTPException(status_code=403, detail="Reserved control argument")
    # `_send_cmd` 是 `Skills` 里唯一的低层发送出口（所有技能方法都走它）。
    # 不新增公开方法：本轮 `backend/agent/**` 由另一个改动占用（避免互相踩）。
    with skills.command_context("sdk"):
        result = skills._send_cmd(command, args)  # noqa: SLF001
    if not result.get("success"):
        raise HTTPException(status_code=503,
                            detail=str(result.get("message") or "Body command channel unavailable"))
    return {"status": "accepted", "command": command, "request_id": result.get("req_id")}


# ── Observer：GET /api/v2/control ────────────────────────────────────────────


@router.get("/api/v2/control")
async def get_v2_control():
    """当前控制模式 + 活动控制租约（只读）。

    真数据来源：`Planner.control_lease`（活对象：`modes_engine.EatYield` 的生存反射抢占
    真的登记在它上面），`snapshot()` 里含持租人/理由/时限/能力位。

    诚实边界：`mode` 恒为 `"builtin"` —— 26.3 没有"外部接管"这条模式（`CONTROL_HOLDERS`
    只认左脑五个身份，右脑/外部一律 `LeaseDenied`），所以本批**不发**
    `POST /api/v2/control/leases*`（承接/续租/释放）与 master 的 fencing/owns 语义。
    `coordinator` 恒为 `null`：26.3 没有 `TaskCoordinator` 在跑（master 在缺它时也是 null）。
    """
    session = _session()
    lease: Optional[dict[str, Any]] = None
    if session is not None:
        control_lease = getattr(getattr(session, "planner", None), "control_lease", None)
        if control_lease is not None:
            lease = control_lease.snapshot()
    return {"mode": "builtin", "lease": lease, "coordinator": None}


# ── Observer：GET /api/v2/audit ──────────────────────────────────────────────


def _audit_source() -> Optional[AuditLog]:
    """审计来源：优先**运行期那份**（`Session` 的落盘句柄，同一把锁/同一条路径），
    没有活会话时退回同路径的只读句柄；路径上什么都没有 ⇒ `None`（不建空文件）。"""
    session = _session()
    store = getattr(session, "_security_log", None)
    if isinstance(store, AuditLog):
        return store
    path = Path(AUDIT_PATH)
    return AuditLog(path) if path.exists() else None


@router.get("/api/v2/audit")
async def get_audit_events(
    category: str = Query(default="", max_length=64),
    limit: int = Query(default=100, ge=1, le=500),
):
    """安全审计事件（时间倒序，最新的在前）。

    真数据来源：`Session._on_security_audit` 的落盘（`data/audit/security.jsonl`）——
    出站清洗、指令闸门、TPA、私信、SDK 原始命令都进这一份。`category` 不传＝全部
    （26.3 目前只有 `security` 一类；别的名字如实回空，不当成"没有审计"）。
    """
    store = _audit_source()
    if store is None:
        return {"events": [], "count": 0, "source": str(AUDIT_PATH), "available": False}
    events = store.list(category=category, limit=limit)
    return {"events": events, "count": len(events), "source": str(store.path), "available": True}


# ── 后端热载：POST /api/reload ────────────────────────────────────────────────


@router.get("/api/reload/watch")
async def get_hot_reload_watch():
    """**只读**看门狗状态（`enabled` / `interval` / 累计轮询与热载次数），给界面显示用。

    数据来源：`server._hot_reload_watch_state()`（进程生命周期那一边的唯一事实源）。
    """
    return _server_module()._hot_reload_watch_state()


@router.post("/api/reload")
async def post_reload():
    """**进程内**热载后端 Python：不重启进程、不掉 wire 连接。

    真数据来源（实现都在 `server.py`，这里只挂口）：

    * `server.perform_hot_reload()` → `agent/hot_reload.py` 的 `reload_agent_modules()`
      （真 `importlib.reload` 白名单模块，按依赖顺序；单个失败不中断其余）
      + `rebuild_session_planner()`（用会话**已有**的构件重建 `session.planner`，不新造连接/记忆）。
    * `server.hot_reload_busy()`：忙的判据读**现有**状态字段（未终态 job / `task_state` 非
      idle 终态 / 挂起的复合体）。

    忙 ⇒ **明确拒绝**（`ok:false` + `reason:"busy"` + 当前在跑什么），**绝不热一半**；
    空闲 ⇒ `ok:true` + 报告（`reloaded` / `failed` / `needs_restart`：哪些改了要重启，逐条带原因）。

    看门狗开关在 `POST /api/reload/watch`（`server.py`：进程生命周期那一边），
    `LCU_HOT_RELOAD=1` 可开机即开。
    """
    server = _server_module()
    busy = server.hot_reload_busy()
    if busy:
        return {"ok": False, "reason": "busy", "busy": busy,
                "note": f"会话正忙（{busy}）：已拒绝热载（绝不热一半）。等空闲再来；"
                        f"或用 POST /api/reload/watch 打开看门狗，让它空闲时自动热载。"}
    report = await asyncio.to_thread(server.perform_hot_reload, None)
    restart = [str(row.get("module")) for row in report.get("needs_restart", [])
               if isinstance(row, dict)]
    note = "已进程内热载：连接与内存态都还在（没重启、没断 wire）。"
    note += (f"这些改了要重启：{'、'.join(restart)}。" if restart else "本次没有需要重启的改动。")
    return {"ok": True, "note": note, **report}
