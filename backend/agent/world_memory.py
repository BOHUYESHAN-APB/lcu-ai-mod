"""站点 / 路径记忆的按 **world 层** 分区落盘 + 「回灌」计划（后端 = 这份记忆的真相来源）。

模组侧的 `StationMemory` / `RouteMemory` 只活在内存里：新世界、模组重启都会清空。
所以后端要把模组上报的站点/路径按「服务器 / 存档 / 维度」分区存盘，等模组侧这份记忆
空了再整份推回去：

    state_update.stations / .routes  ──absorb（原样存）──▶ data/stations|routes/<key>.json
    后端存盘 ──plan_replay（逐条比对）──▶ remember_station / remember_route

三条硬口径（来自用户/设计）：

* **不加工坐标**：存的就是模组上报的字段；推回去的也是同一份（缺字段不补、不抽稀）。
* **不无意义覆盖**：模组侧同名同内容 → 一帧都不发；内容不同才推（并记日志）。
* **只报事实**：路径没有完整点列（模组只报了点数）时**不推**，如实记进 `blocked`
  —— 现编坐标比不推更坏。

**记忆是分层的，不能串**（用户口径）：

    global                                          人设/规则/回复策略/权限（跨服通用）
    person:<uuid>                                   同一个人的稳定事实（可以跨服串一点点）
    server:<server_key>                             对话记忆、社交关系
    world:<server_key>/<world_name>/<dimension>     站点、路径、POI、容器内容（坐标只在这一层有效）

站点/路径属于 **world 层**：同一服务器里主世界与下界的 xyz 完全不是一回事，
少了维度这一段不同世界就会互相串。所以文件名把三段都编进去，缺的用 `unknown` 占位，
**绝不省略层级**（见 `world_scope`）。person 层见 `agent/person_memory.py`。

本模块只做数据与纯函数计划（`plan_replay`），下发由 Session 负责 ——
这样它能在没有 socket 的测试里被完整驱动。分段用 `conversation.server_key` + `sanitize_key`。
"""

from __future__ import annotations

import copy
import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Optional

from .conversation import sanitize_key, server_key

logger = logging.getLogger("world_memory")

# 落盘根目录：与 `data/conversations/`、`data/rules/`、`data/poi/` 同级，按 server 分区。
DEFAULT_STATIONS_ROOT = Path(__file__).parent.parent / "data" / "stations"
DEFAULT_ROUTES_ROOT = Path(__file__).parent.parent / "data" / "routes"

# 模组 `remember_route` 的硬约束（Java RouteMemory.MIN_POINTS / MAX_POINTS）：
# 点数不足/超限会被整条拒绝，所以本地先挡一道（省一次注定失败的往返）。
MIN_ROUTE_POINTS = 2
MAX_ROUTE_POINTS = 2000

# 站点坐标/朝向/半径是浮点：比"有没有变"时给一点容差（Java round3 之后其实相等）。
_TOLERANCE = 1e-6


# 缺层的固定占位：省掉一段会让"主世界/下界/不同存档"落进同一个文件（互相串）。
UNKNOWN = "unknown"


def world_scope(server: Any, world_name: Any, dimension: Any) -> str:
    """world 层分区键：`<server_key>__<world_name>__<dimension>`（三段都编，缺的占位）。

    `server_key` 本身拿不到服务器就退存档名、再退 unknown（见 `conversation.server_key`）。
    """
    parts = (server_key(server, world_name), world_name, dimension)
    return "__".join(sanitize_key(part) if str(part or "").strip() else UNKNOWN for part in parts)


def entry_name(row: Any) -> str:
    """条目名（站点/路径都用 `name` 做键），去空白。"""
    return str((row or {}).get("name") or "").strip() if isinstance(row, dict) else ""


def _num(value: Any) -> Optional[float]:
    """可空数值（bool 不算数；转不动就 None —— 不猜）。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_int(value: Any) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# ── 内容比对（"模组已经有这个了" 的判据）─────────────────────────


def stations_match(a: Any, b: Any) -> bool:
    """两个站点是不是同一份：同名 + 坐标/朝向/半径/类型都对得上。"""
    if not isinstance(a, dict) or not isinstance(b, dict) or not entry_name(a):
        return False
    if entry_name(a) != entry_name(b):
        return False
    if str(a.get("kind") or "") != str(b.get("kind") or ""):
        return False
    for field in ("x", "y", "z", "yaw", "pitch", "radius"):
        left, right = _num(a.get(field)), _num(b.get(field))
        if left is None or right is None:
            if left != right:              # 两边都缺才算一样
                return False
            continue
        if abs(left - right) > _TOLERANCE:
            return False
    return True


def route_points(row: Any) -> Optional[list[dict]]:
    """路径的完整点列（回灌用）；模组只报了点数（没有坐标）时返回 None。

    点列里每个点都必须是带数值 x/y/z 的对象；有一个坏点就整条作废（与 Java 同口径）。
    """
    if not isinstance(row, dict):
        return None
    points = row.get("points")
    if not isinstance(points, (list, tuple)) or len(points) < MIN_ROUTE_POINTS:
        return None
    out: list[dict] = []
    for point in points:
        if not isinstance(point, dict):
            return None
        x, y, z = _num(point.get("x")), _num(point.get("y")), _num(point.get("z"))
        if x is None or y is None or z is None:
            return None
        keep: dict = {"x": x, "y": y, "z": z}
        yaw = _num(point.get("yaw"))
        if yaw is not None:
            keep["yaw"] = yaw
        out.append(keep)
    return out


def route_point_count(row: Any) -> Optional[int]:
    """这条路径有几个点：点列就数长度，模组只报点数时就用那个数。"""
    if not isinstance(row, dict):
        return None
    points = row.get("points")
    if isinstance(points, (list, tuple)):
        return len(points)
    return _as_int(points)


def routes_match(a: Any, b: Any) -> bool:
    """两条路径是不是同一份：两边都有点列就逐点比，否则退化成比点数。

    模组当前只上报点数（`points` 是整数）。

    * **只有一边有几何**（典型：后端存着点列、模组只报点数）→ "同名 + 点数相同"
      就算同一份：这是能拿到的最强信号，好过每次都无脑重推。
    * **两边都只有点数、都没几何** → 不算同一份：无从确认是不是同一条，而且这正是
      **几何缺失**的缺口 —— 记成 identical 会让回灌永远不去取真几何（真几何就再也补不回来）。
      所以走 `blocked(no_geometry)`，由 `_request_route_exports` 按需取一次。
    """
    if not isinstance(a, dict) or not isinstance(b, dict) or not entry_name(a):
        return False
    if entry_name(a) != entry_name(b):
        return False
    left, right = route_points(a), route_points(b)
    if left is not None and right is not None:
        if len(left) != len(right):
            return False
        return all(_points_match(x, y) for x, y in zip(left, right))
    if left is None and right is None:
        # 都没有几何：不能据此断定"同一份"，它是待回灌的几何缺口。
        return False
    left_count, right_count = route_point_count(a), route_point_count(b)
    if left_count is None or right_count is None:
        return False
    return left_count == right_count


def _points_match(a: dict, b: dict) -> bool:
    for field in ("x", "y", "z", "yaw"):
        left, right = _num(a.get(field)), _num(b.get(field))
        left = 0.0 if left is None else left      # Java 侧缺 yaw 记 0
        right = 0.0 if right is None else right
        if abs(left - right) > _TOLERANCE:
            return False
    return True


# ── `remember_station` / `remember_route` 的参数（键名逐字对齐 Java）──


def station_args(row: Any) -> Optional[dict]:
    """站点的 `remember_station` 参数：`name/kind?/x?/y?/z?/yaw?/pitch?/radius?`。

    缺坐标（x/y/z 不全）就返回 None —— 少了坐标 Java 会回退到当前位姿，
    那是"现编一个位置"，不做。
    """
    if not isinstance(row, dict):
        return None
    name = entry_name(row)
    if not name:
        return None
    x, y, z = _num(row.get("x")), _num(row.get("y")), _num(row.get("z"))
    if x is None or y is None or z is None:
        return None
    args: dict = {"name": name}
    kind = row.get("kind")
    if kind not in (None, ""):
        args["kind"] = str(kind)
    args.update({"x": x, "y": y, "z": z})
    for field in ("yaw", "pitch", "radius"):
        value = _num(row.get(field))
        if value is not None:
            args[field] = value
    return args


def route_args(row: Any) -> Optional[dict]:
    """路径的 `remember_route` 参数：`name/points[{x,y,z,yaw?}]`。

    没有完整点列、或点数落在 Java 的 [2, 2000] 之外 → None（本地先挡，别浪费往返）。
    """
    if not isinstance(row, dict):
        return None
    name = entry_name(row)
    points = route_points(row)
    if not name or points is None:
        return None
    if not (MIN_ROUTE_POINTS <= len(points) <= MAX_ROUTE_POINTS):
        return None
    return {"name": name, "points": points}


# ── 回灌计划（纯函数）───────────────────────────────────────────


def plan_replay(saved_stations: Any, saved_routes: Any,
                mod_stations: Any, mod_routes: Any) -> dict:
    """决定这一轮要推哪些条目回去（不看盘、不发帧）。

    返回 `{stations, routes, skipped, blocked}`：

    * `stations` / `routes`：要下发的参数列表（可直接喂 skills）；
    * `skipped`：模组已有同名且内容一致 → 不推（幂等的关键）；
    * `blocked`：想推却推不了的（内容不同但缺坐标缺点列 / 模组压根没上报这一类）。

    `mod_stations` / `mod_routes` 传 None ＝ 这次 state 里没有这一类（旧模组）：
    不能确认"模组侧是空的"，就不推。
    """
    report: dict = {"stations": [], "routes": [], "skipped": [], "blocked": []}

    if mod_stations is None:
        if saved_stations:
            report["blocked"].append({"kind": "station", "name": "", "reason": "mod_not_reporting"})
    else:
        known = {entry_name(row): row for row in mod_stations if isinstance(row, dict)}
        for row in saved_stations or []:
            if not isinstance(row, dict):
                continue
            name = entry_name(row)
            if not name:
                continue
            existing = known.get(name)
            if existing is not None and stations_match(existing, row):
                report["skipped"].append({"kind": "station", "name": name, "reason": "identical"})
                continue
            args = station_args(row)
            if args is None:
                report["blocked"].append({"kind": "station", "name": name, "reason": "no_coords"})
                continue
            report["stations"].append(args)

    if mod_routes is None:
        if saved_routes:
            report["blocked"].append({"kind": "route", "name": "", "reason": "mod_not_reporting"})
    else:
        known = {entry_name(row): row for row in mod_routes if isinstance(row, dict)}
        for row in saved_routes or []:
            if not isinstance(row, dict):
                continue
            name = entry_name(row)
            if not name:
                continue
            existing = known.get(name)
            if existing is not None and routes_match(existing, row):
                report["skipped"].append({"kind": "route", "name": name, "reason": "identical"})
                continue
            args = route_args(row)
            if args is None:
                report["blocked"].append({"kind": "route", "name": name, "reason": "no_geometry"})
                continue
            report["routes"].append(args)

    return report


# ── 落盘 ────────────────────────────────────────────────────────


class WorldMemoryStore:
    """一个 **world 层** 分区下的站点/路径记忆（两个 `.json`，读写线程安全）。

    分区键 `world_scope(server, world_name, dimension)`：同一服务器里主世界与下界
    是两个分区（坐标只在同一维度里有效），不同存档同样分开。
    `data/stations/<key>.json` 存站点表，`data/routes/<key>.json` 存路径表。
    """

    def __init__(self, stations_root: str | Path = DEFAULT_STATIONS_ROOT,
                 routes_root: str | Path = DEFAULT_ROUTES_ROOT,
                 server: Any = "", world_name: Any = "", dimension: Any = "",
                 clock=time.time):
        self.stations_root = Path(stations_root)
        self.routes_root = Path(routes_root)
        self.stations_root.mkdir(parents=True, exist_ok=True)
        self.routes_root.mkdir(parents=True, exist_ok=True)
        self._clock = clock
        self._lock = threading.RLock()
        self._namespace = world_scope(server, world_name, dimension)
        self._stations: dict[str, dict] = {}
        self._routes: dict[str, dict] = {}
        self._dirty = False
        self._load()

    # ── 分区 ──

    def use_namespace(self, server: Any, world_name: Any, dimension: Any = "") -> str:
        """切换到指定的 world 层分区并按盘重建。

        同一分区重复调用直接返回（不重读，免得把内存里刚吸收的覆盖掉）；
        换维度/换世界/换服都换文件。
        """
        with self._lock:
            key = world_scope(server, world_name, dimension)
            if key == self._namespace:
                return key
            self._save()
            self._namespace = key
            self._load()
            return key

    @property
    def namespace(self) -> str:
        return self._namespace

    @property
    def stations_path(self) -> Path:
        return self.stations_root / f"{self._namespace or UNKNOWN}.json"

    @property
    def routes_path(self) -> Path:
        return self.routes_root / f"{self._namespace or UNKNOWN}.json"

    # ── 读 ──

    def stations(self) -> list[dict]:
        with self._lock:
            return [copy.deepcopy(row) for row in self._stations.values()]

    def routes(self) -> list[dict]:
        with self._lock:
            return [copy.deepcopy(row) for row in self._routes.values()]

    def station(self, name: Any) -> Optional[dict]:
        with self._lock:
            row = self._stations.get(str(name or "").strip())
            return copy.deepcopy(row) if row is not None else None

    def route(self, name: Any) -> Optional[dict]:
        with self._lock:
            row = self._routes.get(str(name or "").strip())
            return copy.deepcopy(row) if row is not None else None

    # ── 吸收（模组上报 → 盘）──

    def absorb_state(self, state: Any) -> dict:
        """把一次 `state_update` 里的 stations/routes 吸收进当前分区。

        逐条按名字 upsert（同名覆盖），**永不删除**：模组报空列表是在说"我这边是空的"，
        不是"把这些删掉"（后端才是真相来源）。返回 `{"stations": n, "routes": n}`。

        来件字段原样存；唯一的例外是**几何/坐标只许升级不许降级**（见 `_merge`）：
        模组当前对路径只上报点数，照抄会把后端存的整条点列抹成一个整数。
        """
        if not isinstance(state, dict):
            return {"stations": 0, "routes": 0}
        with self._lock:
            stations = self._absorb(self._stations, state.get("stations"), "station")
            routes = self._absorb(self._routes, state.get("routes"), "route")
            if stations or routes:
                self._dirty = True
                self._save()
        return {"stations": stations, "routes": routes}

    @classmethod
    def _absorb(cls, bucket: dict[str, dict], rows: Any, kind: str) -> int:
        if not isinstance(rows, (list, tuple)):
            return 0
        changed = 0
        for row in rows:
            if not isinstance(row, dict):
                continue
            name = entry_name(row)
            if not name:
                continue
            stored = cls._merge(kind, bucket.get(name), row)
            if bucket.get(name) == stored:
                continue
            bucket[name] = stored
            changed += 1
        return changed

    @staticmethod
    def _merge(kind: str, old: Optional[dict], new: dict) -> dict:
        """同一条目的新旧两份 → 该存哪份：**坐标/点列只许升级不许降级**。

        模组当前对路径只上报**点数**（`points` 是整数、没有坐标）。若照抄来件，
        后端存的整条点列会被抹掉，下次就再也回灌不出去。所以：

        * 来件缺几何而旧件有、且**点数对得上** → 保留旧点列，其余字段以新件为准；
        * 点数对不上 → 来件是在模组上被重记过的，后端那份坐标已经**过期**，
          宁可退回"只有点数"（回灌时如实记 no_geometry），也绝不推一条旧路径出去。
        * 站点同理：来件缺坐标而旧件有 → 保留旧坐标（模组正常都会带 x/y/z）。
        """
        if old is None:
            return copy.deepcopy(new)
        if kind == "route":
            old_points, new_points = route_points(old), route_points(new)
            if new_points is None and old_points is not None:
                count = route_point_count(new)
                if count in (None, len(old_points)):
                    merged = copy.deepcopy(new)
                    merged["points"] = copy.deepcopy(old["points"])
                    return merged
                # 模组上重记过（点数对不上）：后端那份坐标已过期，退回"只有点数"
                # —— 如实记下这次差异，别静默；随后由 `route_export` 去取回真几何。
                logger.info("[WorldMemory] 路径 '%s' 点数与存盘不一致（存 %d → 报 %s）："
                            "旧坐标已过期、退回只有点数，待 route_export 取回真几何",
                            entry_name(new), len(old_points), count)
            return copy.deepcopy(new)
        if _num(new.get("x")) is None and _num(old.get("x")) is not None:
            merged = copy.deepcopy(new)
            for field in ("x", "y", "z", "yaw", "pitch", "radius"):
                if field in old and _num(new.get(field)) is None:
                    merged[field] = copy.deepcopy(old[field])
            return merged
        return copy.deepcopy(new)

    # ── 盘 ──

    def _load(self) -> None:
        self._stations = self._read(self.stations_path, "stations")
        self._routes = self._read(self.routes_path, "routes")
        self._dirty = False

    def save(self) -> None:
        """强制落盘（一般不用调：吸收有变化时已经写过了）。"""
        with self._lock:
            self._write(self.stations_path, "stations", self._stations)
            self._write(self.routes_path, "routes", self._routes)
            self._dirty = False

    def _save(self) -> None:
        if not self._dirty:
            return
        self._write(self.stations_path, "stations", self._stations)
        self._write(self.routes_path, "routes", self._routes)
        self._dirty = False

    def _read(self, path: Path, field: str) -> dict[str, dict]:
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("[WorldMemory] 读取失败，已回退空表 %s: %s", path, exc)
            return {}
        rows = data.get(field) if isinstance(data, dict) else None
        if not isinstance(rows, list):
            return {}
        out: dict[str, dict] = {}
        for row in rows:
            if isinstance(row, dict) and entry_name(row):
                out[entry_name(row)] = copy.deepcopy(row)
        return out

    def _write(self, path: Path, field: str, bucket: dict[str, dict]) -> None:
        data = {
            "version": 1,
            "namespace": self._namespace,
            "saved_at": float(self._clock()),
            field: list(bucket.values()),
        }
        temporary = path.with_suffix(path.suffix + ".tmp")
        try:
            temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(path)
        except Exception as exc:
            logger.warning("[WorldMemory] 写盘失败 %s: %s", path, exc)
