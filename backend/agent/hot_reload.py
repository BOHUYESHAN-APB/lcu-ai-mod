"""后端进程内热载（in-process hot reload）：改完 Python **不用重启、不掉 wire 连接**。

用户口径："如果只是改后端的话，我希望我们后面的服务器开的时候可以做一个高级的内容，
就是后端服务器改了可以热载。"

为什么不是 `uvicorn --reload`：那条路是**重启进程** ⇒ 掉 wire 连接、清空运行期状态
（记忆/任务/租约全没了）。这里走**进程内** `importlib.reload`：连接与内存态都还在。

三条纪律：

1. **白名单才热载**：`HOT_MODULES` 只放"常量表 / 纯函数 / 值对象（dataclass）"，以及
   重载后被 `rebuild_session_planner` 重建的那**一个**活实例（planner）。按**依赖顺序**
   重载（被依赖的在前），这样重载 `planner` 时它重新 `from .farm import …` 拿到的是新代码。
2. **拿不准就不能热载**：不在白名单里的一律按"改了要重启"处理，由 `restart_report`
   **如实点名**（黑名单带各自原因，白名单之外给通用原因）——不假装"什么都能热载"。
3. **忙就不动**：`HotReloadWatcher` 只在调用方注入的 `busy()` 说空闲时才真热载；忙则
   **跳过 + 打日志**，把这次变化**挂起**（只挂一份，不排队堆叠），等空闲再一次性热载。

两条诚实边界（别当成万能热载）：

* `importlib.reload` 是**原地**重载模块对象：`from x import f` 的老绑定、以及**已经创建**
  的实例对象（仍是旧类）不会跟着变。所以热载对"重载后重新 import 它们的人"（planner 及
  白名单内部）立即生效；黑名单宿主（`session` 等）里早先拿到的绑定与实例仍是旧的。
* 因此白名单里那几个**活实例握在会话手里**的模块（`skills` / `modes_engine` / `territory`
  / `borrow`）会同时出现在 `needs_restart` 里（见 `LIVE_INSTANCE_MODULES`）：模块换了新代码，
  但会话里那个旧实例要**重启**才换。
"""

from __future__ import annotations

import importlib
import logging
import sys
import threading
import time
from collections.abc import Mapping as _MappingABC
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional

logger = logging.getLogger("hot_reload")

# 后端根目录（`backend/`）：本模块在 `backend/agent/` 下，所以上两级。
BACKEND_DIR = Path(__file__).resolve().parent.parent

# ── 白名单：**能安全热载**的模块（顺序 = 依赖顺序，被依赖的在前）───────────────
# 判定口径：模块只放常量表 / 纯函数 / 值对象（dataclass），或重载后由
# `rebuild_session_planner` 重建的那一个活实例（planner）。
HOT_MODULES: tuple[str, ...] = (
    # —— 叶子（不 import 其它白名单模块）——
    "agent.perception",       # 分级摘要的唯一读入口（纯函数）
    "agent.anchor",           # 锚点解析（纯函数 + 值对象）
    "agent.context_budget",   # token 估算 / 压缩计划（纯函数 + 值对象）
    "agent.borrow",           # 借用规则表（活实例在 session，见 LIVE_INSTANCE_MODULES）
    "agent.craft",            # 合成缺口计算（纯函数 + 值对象）
    "agent.territory",        # 领地/基地/区域（活实例在 session，同上）
    "agent.skills",           # wire 命令出口（活实例在 session，同上）
    "agent.home_bed",         # 床位绑定判据（纯函数）
    # —— 一层依赖 ——
    "agent.entities",         # ← borrow
    "agent.use_on",           # ← borrow, craft
    "agent.haul",             # ← borrow, craft
    "agent.container_view",   # ← borrow
    "agent.farm",             # ← craft, territory
    "agent.modes_engine",     # ← territory（活实例在 session，见 LIVE_INSTANCE_MODULES）
    "agent.where",            # ← territory
    # —— 复合体 / 工具表（`tool_registry` 从 `composites` 生成 L3；反向是懒 import）——
    "agent.composites",       # ← craft, farm, territory
    "agent.tool_registry",    # ← composites
    # —— 主脑（最后：依赖上面全部）——
    "agent.planner",          # 重载后由 `rebuild_session_planner` 换掉活实例
)

# ── 黑名单：**不能热载**（持有活状态 / 连接 / 路由表）⇒ 改了要重启 ──────────────
# 只在这里写"有明确原因"的那些；**其它任何白名单之外的模块**一律按同一口径处理
# （`UNKNOWN_RESTART_REASON`）—— 拿不准就不能热载。
RESTART_MODULES: dict[str, str] = {
    "agent.session": "持有活会话状态（runtime / planner / skills / jobs…）——换类会让主循环与仪表盘看到两套状态",
    "agent.orchestrator": "持有 wire 连接与主循环（重载等于把正在跑的那条循环换掉）",
    "agent.llm_service": "持有 HTTP 客户端与用量累计（重建等于丢 token 账）",
    "agent.memory": "持有对话记忆与落盘句柄",
    "agent.message_db": "持有 sqlite 连接",
    "agent.config_store": "全局共享单例（UI 改阈值即时生效靠的就是“同一个实例”）",
    "agent.state_governor": "持有活退避 / 卡死治理状态",
    "agent.action_manager": "持有在飞动作与退避窗口",
    "agent.job_registry": "持有活 job 句柄台账",
    "agent.task_list": "持有任务列表活状态",
    "agent.resource_lease": "持有活资源租约",
    "agent.control_lease": "持有活控制租约",
    "agent.audit_log": "持有落盘句柄（同一个文件两把锁会互踩）",
    "agent.vision": "持有后台抓屏线程",
    "agent.self_prompter": "持有空闲计时状态",
    "agent.conversation": "持有对话分区活状态",
    "agent.world_memory": "持有 world / person 层记忆与落盘句柄",
    "agent.person_memory": "持有落盘句柄",
    "agent.command_gate": "持有白名单 / 二次确认的活实例",
    "agent.permissions": "持有权限门活实例",
    "agent.persona_store": "持有人设活实例",
    "agent.contacts": "持有通讯录 / 私信活实例",
    "agent.tpa": "持有 TPA 协调器活实例",
    "protocol": "wire 连接：重载等于断线（用户口径里明确不许掉线）",
    "server": "FastAPI app / 路由表 / 全局 orchestrator 与 wire",
    "main": "进程入口（uvicorn 启动参数）",
    "api.http_surface": "路由表已经挂到 app 上（重载不会重挂）",
    "api.overlay": "同上；且它持有图标缓存接线",
}

# 白名单里"模块本身可热载，但活实例握在黑名单宿主（session）手里"的那几个：
# 重载后**新**调用拿到新代码，**已经创建的那个实例**仍是旧类 ⇒ 如实进 needs_restart。
LIVE_INSTANCE_MODULES: dict[str, str] = {
    "agent.skills": "会话里的 Skills 活实例仍是旧类（模块已热载；那个实例要重启才换）",
    "agent.modes_engine": "会话里的 ModesEngine 活实例仍是旧类（planner 侧的新实例是新的）",
    "agent.territory": "会话里的 TerritoryStore 活实例仍是旧类",
    "agent.borrow": "会话里的 BorrowRulesStore 活实例仍是旧类",
}

# 白名单之外、又没在上面写原因的模块（拿不准 ⇒ 不能热载）。
UNKNOWN_RESTART_REASON = "不在热载白名单：按“拿不准就不能热载”处理，改了要重启"

# 参与快照的运行期顶层包（`tests/` / `web/` / `sdk/` 不是运行期模块，不参与）。
SOURCE_PACKAGES: tuple[str, ...] = ("agent", "api", "protocol")


# ── 路径 / 模块名 ────────────────────────────────────────────────────────────


def _path_candidates(path: Any) -> list[Path]:
    """三种写法都容忍：绝对路径、相对 `backend/`（`agent/farm.py`）、带仓库根前缀
    （`backend/agent/farm.py`）；带前缀的**先剥掉前缀**再试（否则会被误认成 `backend/…`）。"""
    raw = Path(str(path))
    candidates: list[Path] = []
    if raw.parts and raw.parts[0] == BACKEND_DIR.name:
        candidates.append(Path(*raw.parts[1:]))
    candidates.append(raw)
    return candidates


def _name_from_parts(parts: tuple[str, ...]) -> str:
    """相对 `backend/` 的部件 → 运行期模块名（不是运行期模块 ⇒ 空串）。"""
    parts = list(parts)
    if not parts or not (parts[-1] == "__init__.py" or parts[-1].endswith(".py")):
        return ""
    if parts[-1] == "__init__.py":
        parts = parts[:-1]
    else:
        parts[-1] = parts[-1][: -len(".py")]
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0]                       # server / main
    return ".".join(parts) if parts[0] in SOURCE_PACKAGES else ""


def module_name_of(path: Any) -> str:
    """文件路径 → 运行期模块名；**认不出来就返回空串**（不猜、不硬凑）。

    `backend/agent/farm.py` → `agent.farm`；`backend/server.py` → `server`；
    `backend/protocol/wire_client.py` → `protocol.wire_client`；
    `backend/tests/test_x.py` / `backend/sdk/…` / `backend/web/…` → `""`（不参与）。
    """
    for candidate in _path_candidates(path):
        for probe in (candidate, BACKEND_DIR / candidate):
            try:
                resolved = Path(probe).resolve()
                relative = resolved.relative_to(BACKEND_DIR)
            except (ValueError, OSError):
                continue
            name = _name_from_parts(relative.parts)
            if name:
                return name
    return ""


def source_paths(root: Optional[Any] = None) -> list[Path]:
    """后端**运行期** Python 源文件（`agent/`、`api/`、`protocol/` + 顶层 `*.py`）。"""
    base = Path(str(root)) if root is not None else BACKEND_DIR
    found: dict[str, Path] = {}
    patterns = ["*.py"] + [f"{package}/**/*.py" for package in SOURCE_PACKAGES]
    for pattern in patterns:
        try:
            matches = sorted(base.glob(pattern))
        except OSError:
            continue
        for path in matches:
            try:
                if not path.is_file():
                    continue
            except OSError:
                continue
            if module_name_of(path):
                found[str(path)] = path
    return [found[key] for key in sorted(found)]


def _as_module_name(text: str) -> str:
    """已经是模块名（`agent.farm`）就原样返回；路径 / 别的东西返回空串。"""
    probe = text.strip().replace("\\", "/")
    if not probe or probe.endswith(".py") or "/" in probe:
        return ""
    parts = probe.split(".")
    return probe if all(part.isidentifier() for part in parts) else ""


def changed_modules(changed: Optional[Iterable[Any]]) -> list[str]:
    """变化项（**文件路径或模块名**）→ 去重排序的运行期模块名（认不出的丢掉）。"""
    names: set[str] = set()
    for item in changed or ():
        text = str(item)
        name = module_name_of(text) or _as_module_name(text)
        if name:
            names.add(name)
    return sorted(names)


# ── 快照 / 差异（纯逻辑，便于自检注入假快照）─────────────────────────────────


def watch_snapshot(paths: Iterable[Any]) -> dict[str, dict[str, Any]]:
    """`{路径: {"mtime": 秒, "size": 字节}}`。

    不存在 / 读不到 / 不是文件的路径**不进快照**（绝不抛）——"新增 / 删除"由
    `changed_paths` 按"键的出现 / 消失"如实报出来。
    """
    snapshot: dict[str, dict[str, Any]] = {}
    for raw in paths or ():
        path = Path(str(raw))
        try:
            if not path.is_file():
                continue
            stat = path.stat()
        except OSError:
            continue
        snapshot[str(path)] = {"mtime": float(stat.st_mtime), "size": int(stat.st_size)}
    return snapshot


def changed_paths(before: Mapping[str, Any], after: Mapping[str, Any]) -> list[str]:
    """**纯函数**：两份快照的差异（新增 / 删除 / 改动）→ 排序后的路径表。"""
    old = {str(key): value for key, value in (before or {}).items()}
    new = {str(key): value for key, value in (after or {}).items()}
    return sorted(key for key in set(old) | set(new) if old.get(key) != new.get(key))


# ── 启动基线（用于"自进程启动以来改了什么"）──────────────────────────────────

_BASELINE: Optional[dict[str, dict[str, Any]]] = None
_BASELINE_LOCK = threading.Lock()


def _prime_baseline() -> None:
    """import 时抓一份基线快照（**进程启动那一刻**的源文件状态）。失败绝不让 import 炸。"""
    global _BASELINE
    try:
        _BASELINE = watch_snapshot(source_paths())
    except Exception as exc:  # noqa: BLE001 —— 基线拿不到只是"报不出自启动以来的改动"
        logger.warning("[HotReload] 启动快照失败（不影响热载）：%s", exc)
        _BASELINE = None


def baseline_snapshot() -> dict[str, dict[str, Any]]:
    """进程启动时的基线快照（可能在 import 时抓取失败 ⇒ 空）。"""
    with _BASELINE_LOCK:
        return dict(_BASELINE or {})


def changed_since_start() -> list[str]:
    """自**进程启动**以来变过的运行期源文件（基线固定不动：重启后才重抓）。"""
    baseline = baseline_snapshot()
    if not baseline:
        return []
    return changed_paths(baseline, watch_snapshot(source_paths()))


_prime_baseline()


# ── 报告：改了但**不能**热载的那些 ──────────────────────────────────────────


def restart_report(changed: Optional[Iterable[Any]]) -> list[dict[str, str]]:
    """"改了要重启"的清单：黑名单逐条带原因；白名单里的**活实例**模块同样点名。"""
    rows: list[dict[str, str]] = []
    for name in changed_modules(changed):
        reason = RESTART_MODULES.get(name) or LIVE_INSTANCE_MODULES.get(name)
        if reason is None and name in HOT_MODULES:
            continue                            # 白名单且没有活实例绑定 ⇒ 真的能热载
        rows.append({"module": name, "reason": reason or UNKNOWN_RESTART_REASON})
    return rows


# ── 热载 ────────────────────────────────────────────────────────────────────

_RELOAD_LOCK = threading.RLock()


def reload_agent_modules(changed: Optional[Iterable[Any]] = None,
                         modules: Optional[Iterable[str]] = None) -> dict[str, Any]:
    """按依赖顺序 `importlib.reload` 白名单里**已导入**的模块。

    * `changed`：本次变化过的**文件路径或模块名**（`None` ⇒ 与进程启动基线比对，自动算）。
      它只影响 `needs_restart` / `changed` 两份报告，**不决定**重载哪些（重载顺序是固定的）。
    * `modules`：要重载的模块名（默认 `HOT_MODULES`；自检可注入临时模块）。
    * 单个模块失败**不中断**其余：记进 `failed` 继续，逐个打 WARNING。

    返回 `{"reloaded": [...], "failed": [{"module","error"}], "needs_restart": [{"module","reason"}],
    "changed": [...], "not_imported": [...]}`（后两个是如实补充：白名单里还没被导入的模块、
    以及本次认出来的变化模块）。
    """
    names = tuple(modules) if modules is not None else HOT_MODULES
    changed_names = changed_modules(changed_since_start() if changed is None else changed)
    reloaded: list[str] = []
    failed: list[dict[str, str]] = []
    not_imported: list[str] = []
    with _RELOAD_LOCK:
        for name in names:
            module = sys.modules.get(name)
            if module is None:
                not_imported.append(name)
                continue
            try:
                importlib.reload(module)
            except Exception as exc:  # noqa: BLE001 —— 一个坏模块不许拖垮其余
                failed.append({"module": name, "error": f"{type(exc).__name__}: {exc}"})
                logger.warning("[HotReload] 重载 %s 失败（继续其余）：%s", name, exc)
                continue
            reloaded.append(name)
    return {
        "reloaded": reloaded,
        "failed": failed,
        "needs_restart": restart_report(changed_names),
        "changed": changed_names,
        "not_imported": not_imported,
    }


# `Planner.__init__` 的形参 ← 旧 planner 实例上的属性名（**同一个构件对象**，不新造）。
_PLANNER_WIRING: tuple[tuple[str, str], ...] = (
    ("llm_service", "llm"),
    ("memory", "memory"),
    ("skills", "skills"),
    ("threshold_provider", "_threshold_provider"),
    ("chat_inbox", "chat_inbox"),
    ("permissions", "permissions"),
    ("borrow_rules", "borrow_rules"),
    ("territory", "territory"),
    ("poi", "poi"),
    ("places", "places"),
    ("server_commands", "server_commands"),
    ("whisper_sender", "whisper_sender"),
    ("tpa_responder", "tpa_responder"),
    ("tpa_requester", "tpa_requester"),
    ("player_resolver", "player_resolver"),
    ("action_manager", "action_manager"),
    ("resource_lease", "resource_lease"),
    ("state_governor", "state_governor"),
    ("job_registry", "job_registry"),
    ("task_list", "task_list"),
    ("composites_enabled", "_composites_enabled_provider"),
    ("composite_clock", "_composite_clock"),
)


def rebuild_session_planner(session: Any) -> dict[str, Any]:
    """用会话**已有**的构件把 `session.planner` 换成重载后的新 `Planner`。

    * 构件（llm / memory / skills / 各类 store 与回调）从**旧 planner 实例**上取 —— 与
      `Session.__init__` 注进去的是同一批对象，**不新造 wire / 记忆 / 连接** ✗；
    * 旧实例的运行期状态（借用欠账 / 搬运进度 / 扫描回执 / 复合体挂起…）原样带过去，
      免得"热载一下，它把借来的东西忘了"；
    * 失败如实返回 `{"ok": False, "reason": …}`，绝不抛给调用方。
    """
    old = getattr(session, "planner", None)
    if old is None:
        return {"ok": False, "reason": "会话还没有 planner（模组未连接或会话未起来）"}
    try:
        planner_class = importlib.import_module("agent.planner").Planner
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"导入 Planner 失败：{type(exc).__name__}: {exc}"}
    kwargs = {arg: getattr(old, attr, None) for arg, attr in _PLANNER_WIRING}
    try:
        fresh = planner_class(**kwargs)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"重建 Planner 失败：{type(exc).__name__}: {exc}"}
    state = dict(vars(old))
    fresh.__dict__.update(state)
    session.planner = fresh
    return {"ok": True, "wiring": sorted(kwargs), "preserved": sorted(state)}


# ── 看门狗 ──────────────────────────────────────────────────────────────────


def busy_text(value: Any) -> str:
    """把注入的 `busy()` 返回值压成一句人读的"在跑什么"；`None`/`False`/空 ⇒ 空串（空闲）。"""
    if value is None or value is False:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, _MappingABC):
        return "、".join(f"{key}={val}" for key, val in value.items())
    if isinstance(value, (list, tuple, set, frozenset)):
        return "、".join(str(item) for item in value)
    return str(value)


def _short(paths: Iterable[Any], cap: int = 6) -> str:
    """日志里的人读短名单（只留文件名，超了就写"等 N 个"）。"""
    names = [Path(str(path)).name for path in paths or ()]
    head = "、".join(names[:cap])
    return head + (f" 等 {len(names)} 个" if len(names) > cap else "")


class HotReloadWatcher:
    """后台看门狗线程：按间隔轮询源文件快照，**只在空闲时**才真热载。

    两个注入点（都从 `server.py` 接线，便于自检换成假实现）：

    * `busy()`：返回"现在在跑什么"（`None`/`False`/`""`/空容器 ⇒ **空闲**）。
      忙 ⇒ **跳过 + 打一行日志**，把这次变化**挂起**（只有一份，不排队堆叠），空闲后再一次性热载。
    * `on_reload(paths)`：真正干活的那一步（重载 + 重建 planner），返回报告字典。

    `poll_once()` 由看门狗线程调用；自检可以直接同步调用它（不必起线程）。
    """

    def __init__(self, paths: Iterable[Any], *, busy: Callable[[], Any],
                 on_reload: Optional[Callable[[list[str]], Any]] = None,
                 interval: float = 2.0,
                 snapshot: Callable[[Iterable[Any]], dict[str, Any]] = watch_snapshot,
                 clock: Callable[[], float] = time.monotonic,
                 logger_: Optional[logging.Logger] = None) -> None:
        self.paths = [str(path) for path in paths]
        self.busy = busy
        self.on_reload = on_reload
        self.interval = max(0.05, float(interval))
        self._snapshot = snapshot
        self._clock = clock
        self._log = logger_ or logger
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        # 基线在**构造时**抓：`poll_once()` 不必先 `start()` 也能用（自检同步调用），
        # 且停掉再开不重置基线 —— "变了就要热载"这条纪律不因为开关而漏掉。
        self._before: dict[str, Any] = self._snapshot(self.paths)
        self._pending: list[str] = []
        self._polls = 0
        self._skips = 0
        self._reloads = 0
        self._last: dict[str, Any] = {}

    # ── 生命周期 ──────────────────────────────────────────────

    @property
    def running(self) -> bool:
        with self._lock:
            thread = self._thread
        return thread is not None and thread.is_alive()

    def start(self) -> bool:
        """启动看门狗（已在跑 ⇒ `False`）。基线是**构造时**那一份：停掉再开不漏改动。"""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="hot-reload-watch", daemon=True)
            self._thread.start()
        self._log.info("[HotReload] 看门狗开启：盯 %d 个文件，间隔 %.1fs（只在空闲时热载）",
                       len(self.paths), self.interval)
        return True

    def stop(self, timeout: float = 2.0) -> bool:
        """停掉看门狗线程（不在跑 ⇒ `False`；线程没在 `timeout` 内退出也如实返回 `False`）。"""
        with self._lock:
            thread = self._thread
            self._thread = None
        was_running = thread is not None and thread.is_alive()
        self._stop.set()
        if thread is not None:
            thread.join(timeout)
            if thread.is_alive():
                self._log.warning("[HotReload] 看门狗线程未在 %.1fs 内退出（已请求停止）", timeout)
                return False
        self._log.info("[HotReload] 看门狗停止（原本在跑=%s）", was_running)
        return was_running

    def status(self) -> dict[str, Any]:
        """看门狗自述（轮询/跳过/热载次数 + 最近一次结果 + 挂起了什么）。"""
        with self._lock:
            return {
                "running": self.running,
                "interval": self.interval,
                "watched": len(self.paths),
                "polls": self._polls,
                "skips": self._skips,
                "reloads": self._reloads,
                "pending": list(self._pending),
                "last": dict(self._last),
            }

    # ── 轮询 ──────────────────────────────────────────────────

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self.poll_once()
            except Exception as exc:  # noqa: BLE001 —— 看门狗绝不因一次异常退出
                self._log.warning("[HotReload] 轮询异常（继续）：%s: %s", type(exc).__name__, exc)

    def poll_once(self) -> Optional[dict[str, Any]]:
        """轮询一次：无变化 ⇒ `None`；有变化且空闲 ⇒ 热载；有变化但忙 ⇒ 跳过（日志 + 挂起）。"""
        now = self._snapshot(self.paths)
        changed = changed_paths(self._before, now)
        self._before = now
        pending = sorted(set(self._pending) | set(changed))
        self._polls += 1
        if not pending:
            return None
        busy = busy_text(self.busy())
        if busy:
            self._pending = pending
            self._skips += 1
            self._log.info("[HotReload] 跳过（会话正忙）：改的是 %s；忙在 %s —— 变化已挂起，空闲后再热载",
                           _short(pending), busy)
            return {"action": "skipped", "paths": pending, "busy": busy,
                    "note": "会话正忙 ⇒ 本次跳过（绝不热一半）；变化已挂起，空闲后自动热载"}
        self._pending = []
        report: dict[str, Any] = {}
        if self.on_reload is not None:
            try:
                raw = self.on_reload(pending)
            except Exception as exc:  # noqa: BLE001 —— 热载动作失败只记录，线程继续
                self._log.warning("[HotReload] 热载动作抛错（已跳过本次）：%s: %s",
                                  type(exc).__name__, exc)
                raw = {"failed": [{"module": "(on_reload)",
                                   "error": f"{type(exc).__name__}: {exc}"}]}
            if isinstance(raw, _MappingABC):
                report = dict(raw)
        self._reloads += 1
        restart = [row.get("module") for row in report.get("needs_restart", [])
                   if isinstance(row, _MappingABC)]
        self._last = {"at": self._clock(), "paths": pending, **report}
        failed = [row.get("module") for row in report.get("failed", [])
                  if isinstance(row, _MappingABC)]
        self._log.info("[HotReload] 热载：改了 %s ⇒ 重载 %s；要重启的是 %s；失败 %s",
                       _short(pending), report.get("reloaded", []), restart or "（无）",
                       failed or "（无）")
        return {"action": "reloaded", "paths": pending, **report}
