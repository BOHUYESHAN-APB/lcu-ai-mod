"""单页 OBS 叠加层后端（附加式）：布局读写 + 叠加层数据 + 原版贴图缓存。

用户口径（他拍的板）：OBS 那边**只叠一个网页**（不是每个组件一个浏览器源），所有参数在
**一个统一页面**里调 —— 拖哪个组件、放哪、多大、多透明，都直接在编辑器里改。

所以后端只做三件事（全部**只新增**，既有 `/overlay/<component>` 老组件页与
`_overlay_manifest()` 一字不动：别人的 OBS 源还在用）：

======================  ====================================================================
接口                     说明
======================  ====================================================================
`GET /api/overlay/layout` 读布局；**没存过 / 存档坏了**都回默认布局（四件齐全、不重叠）
`PUT /api/overlay/layout` 校验后落盘（`config_store` 的 `overlay_layout` 键）；非法**如实拒**
`GET /api/overlay/data`   叠加层数据（单页与编辑器**共用这一个通道**）
`GET /api/icon/<item_id>` 模组推上来的原版贴图（base64 PNG → 内存缓存）
======================  ====================================================================

三条硬口径：

1. **不编数**：`/api/overlay/data` 的一切数值都来自模组最近一次 `state_update` /
   `task_state`；拿不到就 `connected:false` + 空对象。
2. **不哑吞**：布局越界/未知 kind/重复 kind 一律拒（HTTP 400 + 机器可读 `code`），
   **绝不悄悄夹取**；非法请求**绝不写入**。
3. **不冒充**：`/api/icon/<item_id>` 没有这个 id 就 404（`icon_unknown`），
   绝不回占位图假装是真贴图。

模组事件旁路：`item_icons` 不在 `Session.handle_event` 的已知事件里（那个文件不属于本批
改动范围），所以 `install_icon_capture()` 在启动时**包一层同一个 session 实例的方法**
—— 只旁听缓存贴图，再原样交给原实现，不改它的任何行为。
"""

from __future__ import annotations

import base64
import binascii
import copy
import math
import re
import threading
from typing import Any, Optional

from fastapi import APIRouter, Body, HTTPException
from fastapi.responses import Response

router = APIRouter()

# ── 布局 ─────────────────────────────────────────────────────────────────────

LAYOUT_KEY = "overlay_layout"
LAYOUT_VERSION = 1
# 只有这四种组件（未知 = 拒）。顺序 = 默认布局的堆叠顺序。
COMPONENT_KINDS: tuple[str, ...] = ("gear", "weapon", "vitals", "task")

# 位置单位是**视口百分比**（组件左上角），0–100；缩放 0.5–3.0；透明度 0–1。
X_RANGE = (0.0, 100.0)
Y_RANGE = (0.0, 100.0)
SCALE_RANGE = (0.5, 3.0)
OPACITY_RANGE = (0.0, 1.0)

# 默认布局：四件都在、都在左侧一列且**互不重叠**，拿去 OBS 就能用（不需要先存一次）。
DEFAULT_COMPONENTS: tuple[dict[str, Any], ...] = (
    {"kind": "gear", "x": 2, "y": 2, "scale": 1.0, "opacity": 1.0, "visible": True},
    {"kind": "weapon", "x": 2, "y": 20, "scale": 1.0, "opacity": 1.0, "visible": True},
    {"kind": "vitals", "x": 2, "y": 38, "scale": 1.0, "opacity": 1.0, "visible": True},
    {"kind": "task", "x": 2, "y": 56, "scale": 1.0, "opacity": 1.0, "visible": True},
)


def default_layout() -> dict[str, Any]:
    """默认布局（每次给**新的副本**：调用方改了它不该污染模块常量）。"""
    return {"version": LAYOUT_VERSION,
            "components": [dict(component) for component in DEFAULT_COMPONENTS]}


def _invalid(code: str, field: str, message: str) -> dict[str, str]:
    return {"code": code, "field": field, "message": message}


def _number(value: Any, low: float, high: float, field: str) -> tuple[Any, Optional[dict]]:
    """有限数字且在 [low, high] 内；否则**如实拒**（越界 `out_of_range`，类型不对 `invalid_value`）。

    整数写法原样保留（JSON 里 `2` 与 `2.0` 数值相等，但回读时别把用户写的 `2` 变成 `2.0`）。
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None, _invalid("invalid_value", field, f"{field} 必须是数字")
    number = float(value)
    if not math.isfinite(number) or number < low or number > high:
        return None, _invalid("out_of_range", field,
                              f"{field} 必须在 {low:g}–{high:g} 之间（收到 {value!r}）")
    return (value if isinstance(value, int) else round(number, 3)), None


def validate_layout(payload: Any) -> tuple[Optional[dict[str, Any]], Optional[dict[str, Any]]]:
    """把任意输入校验成布局；不合法返回 `(None, {code, …, errors: [...]})`。

    绝不夹取、绝不忍略：越界/未知 kind/重复 kind/类型不对都算失败，由调用方拒并**不落盘**。
    """
    if not isinstance(payload, dict):
        return None, {"code": "invalid_layout", "field": "", "message": "布局必须是 JSON 对象",
                      "errors": [_invalid("invalid_layout", "", "布局必须是 JSON 对象")]}

    errors: list[dict[str, str]] = []

    version = payload.get("version", LAYOUT_VERSION)
    if isinstance(version, bool) or not isinstance(version, int) or version != LAYOUT_VERSION:
        errors.append(_invalid("unsupported_version", "version",
                               f"只支持 version={LAYOUT_VERSION}"))

    raw_components = payload.get("components")
    if not isinstance(raw_components, list):
        return None, {"code": "invalid_layout", "field": "components",
                      "message": "components 必须是数组",
                      "errors": errors + [_invalid("invalid_layout", "components",
                                                   "components 必须是数组")]}

    components: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, entry in enumerate(raw_components):
        where = f"components[{index}]"
        if not isinstance(entry, dict):
            errors.append(_invalid("invalid_layout", where, "组件必须是 JSON 对象"))
            continue

        kind = entry.get("kind")
        if kind not in COMPONENT_KINDS:
            errors.append(_invalid("unknown_kind", f"{where}.kind",
                                   f"未知组件 kind={kind!r}（只允许 {'/'.join(COMPONENT_KINDS)}）"))
            continue
        if kind in seen:
            errors.append(_invalid("duplicate_kind", f"{where}.kind",
                                   f"组件 {kind} 出现了不止一次"))
            continue
        seen.add(kind)

        component: dict[str, Any] = {"kind": kind}
        for name, (low, high) in (("x", X_RANGE), ("y", Y_RANGE),
                                  ("scale", SCALE_RANGE), ("opacity", OPACITY_RANGE)):
            value = entry.get(name)
            if value is None and name in ("scale", "opacity"):
                value = 1.0  # 只给位置也能用：缩放/透明度回默认，不算"越界"
            value, error = _number(value, low, high, f"{where}.{name}")
            if error is not None:
                errors.append(error)
                continue
            component[name] = value

        visible = entry.get("visible", True)
        if not isinstance(visible, bool):
            errors.append(_invalid("invalid_value", f"{where}.visible", "visible 必须是布尔值"))
        else:
            component["visible"] = visible

        components.append(component)

    if errors:
        first = errors[0]
        return None, {"code": first["code"], "field": first["field"], "message": first["message"],
                      "errors": errors}
    return {"version": LAYOUT_VERSION, "components": components}, None


# ── 进程内真实状态（延迟导入 server：它在文件末尾 include 本模块，模块级导入会成环）──


def _server_module() -> Any:
    import server  # noqa: PLC0415

    return server


def _config_store() -> Any:
    """共享的 `ConfigStore` 实例（测试会换成临时配置文件的那一份）。"""
    return getattr(_server_module(), "config_store")


def _session() -> Any:
    orchestrator = getattr(_server_module(), "orchestrator", None)
    return getattr(orchestrator, "session", None)


def _wire_connected() -> bool:
    wire = getattr(_server_module(), "wire", None)
    return bool(wire is not None and getattr(wire, "connected", False))


def _runtime() -> dict[str, Any]:
    runtime = getattr(_session(), "runtime", None)
    return runtime if isinstance(runtime, dict) else {}


def _stored_layout() -> Any:
    store = _config_store()
    with store._lock:  # noqa: SLF001 —— 与 overlay_theme 一样走同一个共享存储
        return store._data.get(LAYOUT_KEY)  # noqa: SLF001


# ── 原版贴图缓存（`item_icons` 事件 → 内存；按 id 覆盖、跨事件累积）─────────────

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
# 白名单字符：`minecraft:iron_sword` / `minecraft:block/stone` / `iron_sword`。
# 有它就不可能变成本地文件读取（何况这里根本不做文件访问，只查内存字典）。
_ITEM_ID_RE = re.compile(r"^[A-Za-z0-9_.\-]+(?::[A-Za-z0-9_.\-/]+)?$")
MAX_ICON_BYTES = 4 * 1024 * 1024
MAX_ICONS = 4096

_icons_lock = threading.Lock()
_icon_pngs: dict[str, bytes] = {}  # item_id → PNG 字节（插入顺序 = 收到顺序）


def reset_icon_cache() -> None:
    """清空贴图缓存（测试用；进程里只有模组推的贴图，不会自带任何东西）。"""
    with _icons_lock:
        _icon_pngs.clear()


def safe_item_id(item_id: Any) -> bool:
    """只接受白名单字符的 item id；`..`/绝对路径之类一律不算。"""
    if not isinstance(item_id, str) or not item_id or len(item_id) > 256:
        return False
    if ".." in item_id or item_id.startswith("/") or "\\" in item_id:
        return False
    return bool(_ITEM_ID_RE.match(item_id))


def _decode_icon(value: Any) -> Optional[bytes]:
    """base64（容错 `data:image/png;base64,` 前缀）→ PNG 字节；不是 PNG 就 None。"""
    if not isinstance(value, str) or not value:
        return None
    probe = value.strip()
    if probe.startswith("data:") and "," in probe:
        probe = probe.split(",", 1)[1]
    # base64 编码后约为原文的 4/3：超过上限的串直接丢，别先解码再判断（省一次大分配）。
    if not probe or len(probe) > MAX_ICON_BYTES * 4 // 3 + 8:
        return None
    try:
        raw = base64.b64decode(probe, validate=True)
    except (binascii.Error, ValueError):
        return None
    if not raw or len(raw) > MAX_ICON_BYTES or not raw.startswith(_PNG_MAGIC):
        return None
    return raw


def note_item_icons(payload: Any) -> int:
    """吸收一次 `item_icons` 事件，返回**真的收下**的条数（坏条目逐条丢弃，不冒充）。

    兼容两种形状：`{"icons": {...}}`（含带 `type` 的整包）与直接摊平的 `{id: b64}`。
    """
    if not isinstance(payload, dict):
        return 0
    icons = payload.get("icons")
    if not isinstance(icons, dict):
        icons = payload

    accepted = 0
    with _icons_lock:
        for item_id, value in icons.items():
            if not safe_item_id(item_id):
                continue
            if item_id not in _icon_pngs and len(_icon_pngs) >= MAX_ICONS:
                continue
            raw = _decode_icon(value)
            if raw is None:
                continue
            _icon_pngs[item_id] = raw  # 覆盖式更新，天然跨事件累积
            accepted += 1
    return accepted


def cached_icon_ids() -> list[str]:
    with _icons_lock:
        return list(_icon_pngs)


def install_icon_capture(orchestrator: Any) -> bool:
    """在**同一个 session 实例**上包一层 `handle_event`：旁听 `item_icons` 后原样转交。

    模组事件统一走 `Session.handle_event`（`api/` 侧没有别的旁路点），而 `agent/session.py`
    不在本批改动范围 —— 所以只包这一个实例的方法，不碰类本身、不改任何事件的行为。
    重复调用是幂等的。
    """
    session = getattr(orchestrator, "session", None)
    if session is None or getattr(session, "_lcu_icon_capture_installed", False):
        return False
    original = getattr(session, "handle_event", None)
    if not callable(original):
        return False

    def _handle_event(event_type: str, data: dict) -> Any:
        if event_type == "item_icons":
            try:
                note_item_icons(data)
            except Exception as exc:  # 贴图缓存坏掉绝不能拖垮事件分发
                print(f"[Overlay] item_icons 缓存失败: {exc}")
        return original(event_type, data)

    session.handle_event = _handle_event
    session._lcu_icon_capture_installed = True  # noqa: SLF001
    return True


# ── 端点：布局 ───────────────────────────────────────────────────────────────


@router.get("/api/overlay/layout")
async def get_overlay_layout():
    """当前布局；**没保存过 / 存档损坏**都当"没有保存过"处理，回默认布局（不 500）。"""
    layout, _ = validate_layout(_stored_layout())
    return layout if layout is not None else default_layout()


@router.put("/api/overlay/layout")
async def put_overlay_layout(data: Any = Body(default=None)):
    """校验后持久化（`overlay_layout` 键）并回保存后的布局；非法 ⇒ 400 + 机器可读 code。

    非法请求**不写入**（先校验再落盘），也不会悄悄夹取越界值。
    """
    layout, error = validate_layout(data)
    if layout is None:
        raise HTTPException(status_code=400, detail=error)
    store = _config_store()
    with store._lock:  # noqa: SLF001
        store._data[LAYOUT_KEY] = copy.deepcopy(layout)  # noqa: SLF001
        store.save()  # 整份写回，未知键（别人的配置）原样保留
    return layout


# ── 端点：叠加层数据 ─────────────────────────────────────────────────────────


def _number_or_none(value: Any) -> Optional[float]:
    """真数字才回；拿不到就 `None`（**不编造** 0/20 之类的假值）。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _vitals(state: dict[str, Any]) -> dict[str, Optional[float]]:
    """生命/饱食/饱和/氧气 —— 取模组 `state_update.player` 里**已有的**字段。

    键名如实对齐现有 state：饱食度在 state 里叫 `hunger`（顶层 `food` 是旧版兜底），
    饱和度优先 `player.saturation`、其次顶层 `saturation`（与 `Session._state_for_persona` 一致）。
    """
    player = state.get("player")
    player = player if isinstance(player, dict) else {}
    return {
        "health": _number_or_none(player.get("health")),
        "max_health": _number_or_none(player.get("max_health")),
        "food": _number_or_none(player.get("hunger", state.get("food"))),
        "saturation": _number_or_none(player.get("saturation", state.get("saturation"))),
        "air": _number_or_none(player.get("air", state.get("air"))),
        "max_air": _number_or_none(player.get("max_air", state.get("max_air"))),
    }


def _task(state: dict[str, Any]) -> dict[str, Any]:
    """当前任务（既有 `task_state` 的投影）；没有在跑的任务就空对象。"""
    task = state.get("task_state")
    if not isinstance(task, dict) or not task:
        return {}
    kind = task.get("kind")
    status = task.get("status")
    if str(kind or "").strip().lower() in {"", "idle"} and \
            str(status or "").strip().lower() in {"", "idle"}:
        return {}
    return {
        "kind": str(kind or ""),
        "status": str(status or ""),
        "detail": str(task.get("detail") or task.get("reason") or ""),
        "progress": _number_or_none(task.get("progress")),
    }


@router.get("/api/overlay/data")
async def get_overlay_data():
    """单页叠加层与编辑器**共用**的数据通道。

    `connected` 的含义是**真拿得到数据**（模组连着 **且** 收到过 `state_update`）；
    拿不到就 `connected:false` + 空对象/空值，绝不用默认值编出一份好看的假数据。
    `icons` 是后端已缓存到真贴图的 item id 列表（页面据此决定用图还是用字）。
    """
    state = _runtime()
    connected = bool(_wire_connected() and state)
    if not connected:
        return {"connected": False, "gear": {}, "vitals": {}, "task": {},
                "icons": cached_icon_ids()}

    gear = state.get("gear")
    return {
        "connected": True,
        "gear": gear if isinstance(gear, dict) else {},
        "vitals": _vitals(state),
        "task": _task(state),
        "icons": cached_icon_ids(),
    }


# ── 端点：原版贴图 ───────────────────────────────────────────────────────────


@router.get("/api/icon/{item_id:path}")
async def get_item_icon(item_id: str):
    """返回模组推上来的 PNG；没这个 id ⇒ 404 + `icon_unknown`（**绝不回占位图**）。

    `{item_id:path}` 接住路径里的 `:` 与 `/`（`minecraft:iron_helmet`、`minecraft:block/stone`）；
    先过白名单字符校验，再查内存缓存 —— 不存在任何本地文件访问。
    """
    if not safe_item_id(item_id):
        raise HTTPException(status_code=404, detail={"code": "icon_unknown", "item_id": item_id})
    with _icons_lock:
        png = _icon_pngs.get(item_id)
    if png is None:
        raise HTTPException(status_code=404, detail={"code": "icon_unknown", "item_id": item_id})
    return Response(content=png, media_type="image/png")


def install(app: Any) -> None:
    """把本模块的路由挂到既有 app 上（`server.py` 末尾调用一次）。"""
    app.include_router(router)
