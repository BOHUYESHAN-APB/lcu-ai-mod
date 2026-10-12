"""
Modes Engine — Priority-based autonomous behavior system.
Replicates TouhouLittleMaid's Brain/Activity architecture.

Each mode is a behavioral pattern that activates based on conditions
and executes real actions via the Skills layer.

Priority levels (只剩**生存反射**；自主行为已改为注册表里的工具，由 LLM 决定用不用):
  0: SelfPreservation — fire, drowning, void
  1: SelfHeal — use healing items when health is low
  2: Cowardice — flee from hostiles when low health
  3: SelfDefense — 被打才还手（没挨打绝不先动手）
"""

import logging
import math
import time
from abc import ABC, abstractmethod
from typing import Any, Optional

from . import perception
from .control_lease import (HOLDER_REFLEX_EAT, REASON_SURVIVAL_EAT, ControlLease,
                            register_reflex_preemption)
from .resource_lease import MODE_RESOURCES, PRIORITY_DEFAULT, PRIORITY_SURVIVAL
from .territory import TIER_DANGEROUS, TIER_HOSTILE, tier_of

logger = logging.getLogger("modes_engine")


# ── 退避闸门 ────────────────────────────────────────────────────
# 模式过去直接调 `skills.*`，于是 action_manager 打出"本次拒绝"后 wire 帧照样发出
# （用户实机日志：`select_hotbar 正在退避，本次拒绝` → `[Skills] Sent: select_hotbar`）。
# 修法：模式看到的 `skills` 是下面这层薄代理——下发前先查 action_manager 的退避窗口，
# 被禁则**本次不下发**（只留一条 debug 日志），保证退避期内零 wire 帧。
_GATE_BYPASS = frozenset({
    "drive", "release_control", "command_context",
    "set_wire", "set_command_observer", "set_control_stream",
})


class _GatedSkills:
    """只读代理：把模式的下发卡在 action_manager 的退避窗口之外。

    只代理"命令"类方法（方法名 == wire cmd 名）；控制流（`drive`/`release_control`，
    P0 逃生用）与内部装配方法一律直通——命比节奏重要，永远不许被退避挡住。
    """

    def __init__(self, skills, action_manager=None):
        self._skills = skills
        self.action_manager = action_manager

    def __getattr__(self, name):
        attr = getattr(self._skills, name)
        if (name.startswith("_") or not callable(attr)
                or name in _GATE_BYPASS or self.action_manager is None):
            return attr
        action_manager = self.action_manager

        def gated(*args, **kwargs):
            if action_manager.is_backed_off(name):
                logger.debug("[Mode] %s 正在退避，本次不下发（无 wire 帧）", name)
                return {"success": False, "message": f"{name} backed off",
                        "backoff": True, "req_id": None}
            return attr(*args, **kwargs)

        return gated


class Mode(ABC):
    """Base class for a behavior mode."""

    def __init__(self, name: str, priority: int, interrupts: bool = False,
                 pauseable: bool = True, cooldown: float = 2.0):
        self.name = name
        self.priority = priority
        self.interrupts = interrupts
        self.pauseable = pauseable
        self.cooldown = cooldown
        self.active = False
        self._paused = False
        self._last_trigger: float = 0.0

        # 滞回：`state` 是按阈值增量上报的，列表会闪（同一条僵尸这一帧在、下一帧没了），
        # 于是模式跟着闪：激活→立刻失活→2 秒后又被激活，中间还反复重发同一个动作。
        #   - 激活：要连续 activate_streak_required 个 tick 都满足才认（默认 3 ≈150ms）；
        #   - 失活：要连续 deactivate_grace_ticks 个 tick 都不满足才放手（默认 20 ≈1s 宽限）。
        # 确定性威胁（岩浆/着火/卡住）各自覆盖成 1，不允许延迟响应。
        self.activate_streak_required = 3
        self.deactivate_grace_ticks = 20
        self._satisfied_streak = 0
        self._unsatisfied_streak = 0

        # 同一目标的动作重发间隔（秒）：0 = 每次都发（连续直驱类，如 P0 逃生的按住）。
        # 用户实机日志里 `attack` 每 2 秒重发一次 → action_manager 连报 "ran 3 times"。
        self.redispatch_interval = 1.0
        self._last_dispatch_target = ""
        self._last_dispatch_at = 0.0

    @abstractmethod
    def should_activate(self, state: dict) -> bool:
        """Check if this mode should activate."""

    @abstractmethod
    def tick(self, skills, state: dict) -> str | None:
        """Execute behavior. Return action description or None."""

    # ── 动作节奏 / 防重复 ──

    def should_dispatch(self, target: str, interval: Optional[float] = None) -> bool:
        """同一目标在 interval 秒内已经发过 → 返回 False（本次别再发）。

        "目标"是模式自己给的稳定标识（例如 `zombie:123` 或方块坐标）。模式仍然每 tick
        被 tick 一次（内部状态如两段式摆放要继续推进），只是**同一个目标不重复下发**。
        interval 为 0 表示不设限（连续按键类必须每 tick 重申）。
        """
        window = self.redispatch_interval if interval is None else interval
        if window <= 0:
            return True
        now = time.time()
        if target and target == self._last_dispatch_target and (now - self._last_dispatch_at) < window:
            return False
        self._last_dispatch_target = target
        self._last_dispatch_at = now
        return True

    def on_activate(self):
        self._satisfied_streak = 0
        self._unsatisfied_streak = 0
        logger.debug("[Mode] Activated: %s", self.name)

    def on_deactivate(self, skills=None):
        self._satisfied_streak = 0
        self._unsatisfied_streak = 0
        logger.debug("[Mode] Deactivated: %s", self.name)

    @property
    def is_on_cooldown(self) -> bool:
        return (time.time() - self._last_trigger) < self.cooldown if self.cooldown > 0 else False

    def _mark_triggered(self):
        self._last_trigger = time.time()


# ── Mode Implementations ────────────────────────────────────────

def _yaw_towards(px: float, pz: float, tx: float, tz: float) -> float:
    """从 (px,pz) 指向 (tx,tz) 的 Minecraft yaw（度）。"""
    return math.degrees(math.atan2(-(tx - px), (tz - pz)))


class SelfPreservationMode(Mode):
    """P0 — React to immediate threats: lava, fire, drowning, void.

    Runs with zero LLM involvement. Detection reads the survival fields emitted by
    StateCollector (in_lava / on_fire / fire_ticks / air_ticks) and falls back to the
    y-coordinate for the void. A missing field keeps its safe default, so an older
    client that does not report it never triggers a false alarm.
    """

    def __init__(self):
        # cooldown=0 是刻意的：P0 必须每 tick 重申控制流按键。若带冷却，
        # 引擎会在下一 tick 判定"激活中但冷却"而停用模式 → 立刻松手，
        # 逃生就变成每秒脉冲一次而不是持续按住。
        super().__init__("self_preservation", priority=0, interrupts=True, cooldown=0.0)
        # P0 不走滞回：威胁字段（in_lava/on_fire/air_ticks）是确定性的、不会像实体列表
        # 那样闪，延迟激活或延迟松手都是错的 —— 前者慢半秒，后者一直按着键不放。
        self.activate_streak_required = 1
        self.deactivate_grace_ticks = 1
        # 连续直驱（按住跳/前进）必须每 tick 重申，不能被"防重复"吃掉。
        self.redispatch_interval = 0.0

    @staticmethod
    def _threat(state: dict) -> str | None:
        """Return the most urgent survival threat, or None if safe."""
        state = state if isinstance(state, dict) else {}
        player = state.get("player", {})
        player = player if isinstance(player, dict) else {}
        if player.get("in_lava"):
            return "lava"
        if player.get("on_fire") or player.get("fire_ticks", 0) > 0:
            return "fire"
        if player.get("air_ticks", 300) < 50:
            # 本地上浮反射（毫秒级闭环、已在下潜）正在处理溺水 → 让路，别重复按键。
            if state.get("reflex_swim"):
                return None
            return "drowning"
        if player.get("y", 64) < -32:
            return "void"
        return None

    def should_activate(self, state: dict) -> bool:
        return self._threat(state) is not None

    def tick(self, skills, state: dict) -> str | None:
        player = state.get("player", {})
        x, z = player.get("x", 0), player.get("z", 0)
        threat = self._threat(state)
        if threat is None:
            # 引擎判定"还该活跃"和真正下手之间 state 可能已经变了（例如本地反射接手了
            # 溺水）—— 这时什么都不按，绝不能掉进下面的"虚空兜底"乱按跳。
            return None

        # 应急逃生全部走低层直驱（控制流）：无回执、latest-wins，
        # 在岩浆/水里 `move_to` 那套高层寻路既慢又基本必然失败。
        if threat == "lava":
            skills.drive(jump=True, look_dy=-20)
            return "self_preservation: in lava, swimming up (direct drive)"

        if threat == "fire":
            water = self._find_water(state)
            if water is not None:
                yaw = _yaw_towards(x, z, water.get("x", x), water.get("z", z))
                skills.drive(forward=True, sprint=True, look_yaw=yaw, look_pitch=0.0)
                return "self_preservation: on fire, running to water (direct drive)"
            # 够不到水时绝不停在原地烧：朝当前朝向冲刺跑开。
            skills.drive(forward=True, sprint=True)
            return "self_preservation: on fire, sprinting away (no water in reach)"

        if threat == "drowning":
            skills.drive(jump=True, look_dy=-30)
            return "self_preservation: drowning, swimming up (direct drive)"

        # 虚空：垫方块上浮需要脚下有可放置面，真·虚空里不存在，
        # 这里只做尽力而为的上浮（支柱宏留待后续）。
        skills.drive(jump=True, look_dy=-30)
        return "self_preservation: in void, best-effort upward"

    def on_deactivate(self, skills=None):
        # 控制流活跃时独占运动与视角；退出必须松手，
        # 否则 Pathfinder / move_to 会被永久压制。
        super().on_deactivate(skills)
        if skills is not None:
            skills.release_control()

    @staticmethod
    def _find_water(state: dict) -> dict | None:
        """Nearest water source within perception range, if any.

        行来自 `perception.blocks_rows`（**唯一读法**）：旧**逐条清单**形状里方块带坐标，
        照旧挑最近的水；**分级摘要**形状里方块没有逐条坐标（只有计数 + 最近一种的距离）
        ⇒ 这里**看不出**水在哪，如实返回 None（**不按 `by_block` 里的计数编一个位置**）。
        要真拿坐标得走按需扫描（`find_block`）—— 那是模型的活，不是反射层每 tick 的活。
        """
        view = perception.blocks_rows(state)
        water = [row for row in view["rows"]
                 if "water" in str(row.get("block_id") or row.get("name") or "")
                 and perception.point_of(row) is not None]
        if not water:
            return None
        return min(water, key=lambda row: _distance(row, default=999.0))


# ── 生物对待策略的读法（与 Java `MobPolicy` / `territory` 同一张表）────
#
# 状态里每行实体都带模组算的 `tier`（DANGEROUS/HOSTILE/NEUTRAL/PASSIVE/UNKNOWN）；
# `tier_of` 优先信任它，缺字段（旧客户端）才按名字兜底。生存反射只认两张名单：
#   * 威胁 = 高危险 + 敌对 → 低血逃跑 / 还手；
#   * 还手名单**只有敌对**（中立与被动不是它的活，高危险更不许碰 —— 用户口径
#     "高危险的不主动打；中立的不惹；只有该处理的才处理"）。

def _distance(entry: dict, default: float = 999.0) -> float:
    """实体距离（认不出就给默认值 —— 状态里偶发缺字段不该让模式崩掉）。"""
    try:
        return float(entry.get("distance", default))
    except (TypeError, ValueError):
        return default


def _threats(entities: Any, within: float) -> list[dict]:
    """近处的**威胁**（高危险 + 敌对，按距离升序）；中立/被动/认不出都不进来。"""
    found = [entry for entry in entities or []
             if isinstance(entry, dict) and tier_of(entry) in (TIER_DANGEROUS, TIER_HOSTILE)
             and _distance(entry) < within]
    return sorted(found, key=_distance)


def _is_dangerous(entry: Any) -> bool:
    """**高危险**（监守者/凋灵/末影龙）：不贴脸、优先拉开距离，绝不还手。"""
    return isinstance(entry, dict) and tier_of(entry) == TIER_DANGEROUS


class SelfHealMode(Mode):
    """P1 — Use healing items when health is low."""

    def __init__(self):
        super().__init__("self_heal", priority=1, interrupts=True, cooldown=10.0)
        # 吃东西是"持续动作"（原版要按住 1.6 秒），不该每 tick 重发一次 use_item。
        self.redispatch_interval = 3.0

    def should_activate(self, state: dict) -> bool:
        player = state.get("player", {})
        health = player.get("health", 20)
        max_health = player.get("max_health", 20)
        # Heal when below 30% HP
        return health < max_health * 0.3 if max_health > 0 else health < 6

    def tick(self, skills, state: dict) -> str | None:
        # Try eating food for healing
        if not self.should_dispatch("heal"):
            return None
        skills.use_item()
        return "self_heal: using healing item"


class CowardiceMode(Mode):
    """P2 — 低血遇到威胁就跑；**高危险不管血量先拉开距离**（用户口径：不贴脸）。"""

    # 高危险进这个距离就退（监守者/凋灵/末影龙：跟它们近战是拿命换血，先拉开再说）
    DANGEROUS_STANDOFF = 12.0

    def __init__(self):
        super().__init__("cowardice", priority=2, interrupts=True, cooldown=5.0)

    def should_activate(self, state: dict) -> bool:
        # 实体只从 `perception.entities_rows` 读（**唯一读法**）：旧逐条清单 ⇒ 全量；
        # 分级摘要 ⇒ 带坐标的威胁 / 玩家 + 计数（`by_kind`）。
        view = perception.entities_rows(state)
        entities = view["rows"]
        # 高危险：不看血量，先进后退（"优先拉开距离"）。
        if any(_is_dangerous(e) and _distance(e) < self.DANGEROUS_STANDOFF for e in entities):
            return True
        # 摘要只列**最近的 3 条**威胁：高危险那只可能被截掉（`by_kind.dangerous` 还数得到它）。
        # 它确实在 16 格内 ⇒ 按"可能贴脸"保守处理（tick 朝最近那条威胁的**反方向**退，
        # 不会因为"没这条行"就当作没有高危险）。
        if (view["partial"] and (view["by_kind"].get("dangerous") or 0) > 0
                and not any(_is_dangerous(e) for e in entities)):
            return True
        player = state.get("player", {})
        health = player.get("health", 20)
        if health > 8:
            return False
        # 低血 + 附近有威胁（敌对/高危险）才逃；中立（末影人/狼/村民）不吓唬自己。
        return bool(_threats(entities, 16.0))

    def tick(self, skills, state: dict) -> str | None:
        player = state.get("player", {})
        px = player.get("x", 0)
        py = player.get("y", 0)
        pz = player.get("z", 0)
        threats = _threats(perception.entities_rows(state)["rows"], 16.0)
        if not threats:
            return None
        nearest = threats[0]
        # 逃跑目标每秒重算一次就够；每 tick 重发 move_to 只会把寻路刷屏。
        target = (f"flee:{nearest.get('name', '?')}:"
                  f"{nearest.get('entity_id') or nearest.get('id') or ''}")
        if not self.should_dispatch(target):
            return None
        dx = px - nearest.get("x", px)
        dz = pz - nearest.get("z", pz)
        mag = (dx * dx + dz * dz) ** 0.5 or 1.0
        # Flee 12 blocks directly away from the nearest threat
        skills.move_to(px + dx / mag * 12, py, pz + dz / mag * 12, 1.0)
        return f"cowardice: fleeing {nearest.get('name', 'hostile')} (dist {nearest.get('distance', '?')}m)"


class SelfDefenseMode(Mode):
    """P3 — 还手：**只有最近确实挨过打**才还手（打我的那只敌对在范围内就还手它）。

    用户口径：「后台不许有自主开关/自动触发」。旧实现是 `8 格内有敌对就自动打` —— 那是
    一眼**自主行为**（没挨打也会先动手），跟"能力＝注册表里的工具、用不用由 LLM 决定"
    直接矛盾，所以降级成**纯反射的还手**：没挨打 ⇒ 一个字节都不发（该打谁由模型调
    `attack` 决定，不由后台替它开战）。

    「挨打」判据（按优先级）：
      ① state 里**直接的受伤事实**（`hurt` / `hurt_time` / `last_hurt` / `hurt_by` /
         `damage_taken`，或 `player.hurt_time` 这类）—— 有就用它；
      ② **没有**这类字段就用**血量近期下降**当代理判据（记上一帧的血量，掉血即视为挨打）。
        这是**代理**，不是事实：掉血也可能来自岩浆/坠落/饥饿，而"谁打的"更无从得知；
        所以它只在 `HURT_WINDOW_SECONDS` 秒内有效，且**只用来对"附近已有的敌对"还手**，
        绝不先动手。等模组把 `hurt_time` 之类报出来，①就会自动接管（那时不再靠代理）。
    """

    # 还手的范围：范围内的**敌对档**才还手（中立不惹、被动不是它的活、高危险不许碰）。
    RETALIATE_RANGE = 8.0
    # 「近期」挨打的有效期：过这么久没再挨打就收手。
    HURT_WINDOW_SECONDS = 10.0

    def __init__(self):
        super().__init__("self_defense", priority=3, interrupts=True, cooldown=2.0)
        self._last_health: Optional[float] = None
        self._last_hurt_at: float = 0.0

    # ── "挨打"判据 ──

    @staticmethod
    def _explicit_hurt(state: dict) -> bool:
        """state 里有没有**直接的受伤事实**（有就优先用它，不去猜血量）。"""
        if not isinstance(state, dict):
            return False
        for key in ("hurt", "hurt_time", "last_hurt", "hurt_by", "damage_taken"):
            value = state.get(key)
            if isinstance(value, bool):
                if value:
                    return True
            elif isinstance(value, (int, float)):
                if float(value) > 0:
                    return True
            elif value:
                return True
        player = state.get("player") if isinstance(state.get("player"), dict) else {}
        for key in ("hurt", "hurt_time", "last_hurt"):
            value = player.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool) \
                    and float(value) > 0:
                return True
        return False

    def _observe_hurt(self, state: dict) -> bool:
        """记下"这一帧挨没挨打"并返回结论（显式事实优先，否则看血量是否下降）。"""
        hurt = self._explicit_hurt(state)
        if not hurt:
            player = state.get("player") if isinstance(state, dict) \
                and isinstance(state.get("player"), dict) else {}
            health = player.get("health")
            if isinstance(health, (int, float)) and not isinstance(health, bool):
                health = float(health)
                if self._last_health is not None and health < self._last_health - 1e-6:
                    hurt = True
                self._last_health = health
        if hurt:
            self._last_hurt_at = time.time()
        return hurt

    def _recently_hurt(self) -> bool:
        return (time.time() - self._last_hurt_at) <= self.HURT_WINDOW_SECONDS

    def should_activate(self, state: dict) -> bool:
        # 本地战斗反射正在交战（毫秒级闭环、已对准才出手）→ 后端别同时出手：
        # 两边一起发 attack/look_at 就是用户实机日志里那个 2 秒一抖的循环。
        if state.get("reflex_combat"):
            return False
        self._observe_hurt(state)
        if not self._recently_hurt():
            # 8 格内有敌对但**没挨打** ⇒ 不许自动出手（这正是本次降级要挡掉的那条）。
            return False
        return any(tier_of(e) == TIER_HOSTILE and _distance(e) < self.RETALIATE_RANGE
                   for e in perception.entities_rows(state)["rows"])

    def tick(self, skills, state: dict) -> str | None:
        if state.get("reflex_combat"):
            return None
        self._observe_hurt(state)
        if not self._recently_hurt():
            return None
        entities = perception.entities_rows(state)["rows"]
        hostiles = [e for e in entities if tier_of(e) == TIER_HOSTILE
                    and _distance(e) < self.RETALIATE_RANGE]
        if hostiles:
            nearest = hostiles[0]
            # 同一目标（名字 + 位置）在 redispatch_interval 内不重复下发：
            # 打架时每秒重发一次 attack 是正常节奏，每 tick 重发就会被 action_manager
            # 判成死循环（"attack ran 3 times"）。
            target = f"{nearest.get('name', 'hostile')}:{nearest.get('entity_id') or nearest.get('id') or ''}"
            if not self.should_dispatch(target):
                return None
            # Look at the hostile and attack
            skills.look_at(nearest.get("x", 0), nearest.get("y", 0), nearest.get("z", 0))
            skills.attack()
            return f"self_defense: retaliating against {nearest.get('name', 'hostile')}"
        return None


class EatYield:
    """BackendEatYield —— 进食的"让路 + 退避 + 冷却"反射闸门。

    "吃"现在是注册表里的工具（`eat`，**由 LLM 决定用不用**），这里的**安全约束**一字不改地
    保留下来（全部来自用户实机日志）：
      1. 让路：本地饥饿反射（Java `reflex_eat`）正在按右键吃 → 后端**一个字都不发**；
      2. 退避：`action_manager` 的退避窗口内不发（零 wire 帧）；
      3. 冷却：上一次 `eat` 失败/超时 → 冷却 `FAIL_COOLDOWN` 秒，并把真实原因写进
         `task_state`，绝不每 2 秒重试到触发 action_manager 的循环保护。

    它**不是模式**：没有"饿了自动吃"的自主触发（那是被删掉的 AutoEatMode）。

    接线（docs/dev/AGENT_ARCH.md §8）：构造函数可注入 `ControlLease`；本地饥饿反射抢占
    控制权时**经租约登记一行审计**（带理由 `survival:eat` 与时限）。没注入租约＝不登记，
    行为与字节完全照旧。此处**不启用**双脑仲裁、不改任何下发逻辑。
    """

    # 一次 `eat` 的等待上限（Java 侧 task_state 的 running→done 一般 2~3s；超时＝没回执）。
    EAT_TIMEOUT = 8.0
    # 失败后的冷却：用户口径"不要每 2 秒重试到触发循环保护"。
    FAIL_COOLDOWN = 10.0

    def __init__(self, action_manager=None, control_lease: Optional[ControlLease] = None):
        self._action_manager = action_manager
        self._pending_at = 0.0          # 上一次 `eat` 的下发时刻
        self._pending = False           # 上一次 `eat` 还没收到终态
        self._fail_until = 0.0          # 失败冷却的截止时刻
        self._fail_reason = ""
        # 控制租约（docs/dev/AGENT_ARCH.md §8）：本地饥饿反射抢占时**只登记一行审计**；
        # 没接线（None）＝行为与字节完全照旧。
        self._control_lease = control_lease
        self._reflex_logged = False     # 一次连续反射只登记一次，避免每 tick 刷审计

    def _note_reflex_hold(self) -> None:
        """本地饥饿反射抢占控制权：经租约登记（带理由与时限），留一行审计。"""
        if self._reflex_logged:
            return
        self._reflex_logged = True
        register_reflex_preemption(self._control_lease, holder=HOLDER_REFLEX_EAT,
                                   reason=REASON_SURVIVAL_EAT, ttl=self.EAT_TIMEOUT,
                                   detail={"source": "eat_yield", "reflex": "reflex_eat"})

    def allow(self, state: dict) -> tuple[bool, str]:
        """能否下发 `eat`：返回 `(是否放行, 被挡时的一句如实说明)`。"""
        if isinstance(state, dict) and state.get("reflex_eat"):
            self._note_reflex_hold()
            return False, "（本地饥饿反射正在吃，我不抢）"
        self._reflex_logged = False
        if time.time() < self._fail_until:
            return False, f"（上次进食没成：{self._fail_reason}；冷却中，稍后再吃）"
        if self._pending:
            return False, "（上一次进食还没回执，先等）"
        manager = self._action_manager if self._action_manager is not None else \
            getattr(state, "action_manager", None)
        if manager is not None and manager.is_backed_off("eat"):
            return False, "（进食正在退避，本次不发）"
        return True, ""

    def mark_sent(self) -> None:
        """记下刚刚下发了一次 `eat`（等回执）。"""
        self._pending = True
        self._pending_at = time.time()

    def absorb(self, state: dict) -> Optional[str]:
        """回收上一次 `eat` 的终态：失败/超时 → 冷却 + 如实写 task_state。"""
        if not self._pending:
            return None
        manager = self._action_manager
        outcome = manager.outcome_since("eat", self._pending_at) if manager else None
        if outcome is None:
            if time.time() - self._pending_at < self.EAT_TIMEOUT:
                return None             # 还在吃，等回执
            outcome = {"success": False,
                       "error": f"eat timeout (> {self.EAT_TIMEOUT:.0f}s 无回执)"}
        self._pending = False
        if outcome.get("success"):
            self._fail_until = 0.0
            self._fail_reason = ""
            return None
        reason = str(outcome.get("error") or "eat failed")
        self._fail_until = time.time() + self.FAIL_COOLDOWN
        self._fail_reason = reason
        self._write_task_state(state, reason)
        logger.warning("[EatYield] eat 失败（%s）→ 冷却 %.0fs", reason, self.FAIL_COOLDOWN)
        return f"eat: failed ({reason}) → 冷却 {self.FAIL_COOLDOWN:.0f}s"

    @staticmethod
    def _write_task_state(state, reason: str) -> None:
        """把失败原因如实写进 task_state（上层 planner/仪表盘据此知道为什么没吃上）。"""
        if not isinstance(state, dict):
            return
        task_state = state.get("task_state")
        task_state = dict(task_state) if isinstance(task_state, dict) else {}
        task_state.update({
            "kind": "eat",
            "status": "failed",
            "detail": reason,
            "source": "eat_yield",
        })
        state["task_state"] = task_state


class StuckRecoveryMode(Mode):
    """P0.5 — Detect stuck state and attempt recovery. Only triggers after prolonged immobility."""

    def __init__(self):
        super().__init__("stuck_recovery", priority=0, interrupts=True, cooldown=10.0)
        self._last_pos = None
        self._stuck_ticks = 0
        self._recovery_step = 0  # 0=jump, 1=pillar, 2=mine, 3=give_up
        self._max_stuck = 100  # 5 seconds (much more lenient)

    def should_activate(self, state: dict) -> bool:
        player = state.get("player", {})
        x, y, z = player.get("x", 0), player.get("y", 0), player.get("z", 0)
        pos = (round(x, 2), round(y, 2), round(z, 2))

        if self._last_pos is None:
            self._last_pos = pos
            self._stuck_ticks = 0
            return False

        # Check if player moved (very small threshold)
        dx = abs(pos[0] - self._last_pos[0])
        dy = abs(pos[1] - self._last_pos[1])
        dz = abs(pos[2] - self._last_pos[2])
        moved = dx > 0.05 or dy > 0.05 or dz > 0.05

        if not moved:
            self._stuck_ticks += 1
        else:
            self._stuck_ticks = 0
            self._recovery_step = 0

        self._last_pos = pos

        # Only trigger after 100 ticks (5 seconds) without ANY movement
        return self._stuck_ticks >= self._max_stuck

    def tick(self, skills, state: dict) -> str | None:
        player = state.get("player", {})
        x, y, z = player.get("x", 0), player.get("y", 0), player.get("z", 0)

        if self._recovery_step == 0:
            # Step 1: Try jumping ONCE
            skills.jump()
            self._recovery_step = 1
            self._stuck_ticks = 0  # Reset counter after jump attempt
            return "stuck_recovery: jumping"

        elif self._recovery_step == 1:
            # Step 2: If still stuck after jump, look down + place block
            skills.look_at(x, y - 2, z)
            skills.select_hotbar(0)
            skills.place_block()
            self._recovery_step = 2
            self._stuck_ticks = 0
            return "stuck_recovery: pillaring up"

        elif self._recovery_step == 2:
            # Step 3: Try breaking block in front
            skills.auto_equip()
            skills.mine_block()
            self._recovery_step = 3
            self._stuck_ticks = 0
            return "stuck_recovery: breaking block ahead"

        else:
            # Step 4: Give up, reset — don't try again for a while
            self._recovery_step = 0
            self._stuck_ticks = 0
            self._max_stuck = 200  # Wait 10 seconds before next attempt
            return "stuck_recovery: recovery failed, waiting for user"


# ── Modes Engine ─────────────────────────────────────────────────

class ModesEngine:
    """
    Central modes engine — runs all modes in priority order.
    Inspired by TouhouLittleMaid's Brain → Activity → Task architecture.

    Rules:
    - Only ONE mode active at a time
    - Higher priority (lower number) preempts lower
    - interrupts=False modes only run when action manager is idle
    """

    def __init__(self, skills, memory=None, action_manager=None, resource_lease=None,
                 enabled_provider=None):
        self.skills = skills
        self.memory = memory
        self.action_manager = action_manager
        # 模式只看见这层退避代理：action_manager 说"本次拒绝"时，退避期内零 wire 帧。
        self._mode_skills = _GatedSkills(skills, action_manager)
        # 资源租约（按资源分租）：模式**声明**自己占哪些资源；P0 走生存通道（永远可抢）。
        # 模式是安全层，**只登记不拦**（拿不到也不影响执行，P0 更是绝不被挡）。
        self._lease = resource_lease
        self._enabled_provider = enabled_provider
        self._lease_owner: Optional[str] = None
        self.modes: list[Mode] = []
        self._sorted: list[Mode] = []
        self._active_mode: Optional[Mode] = None
        self._last_action: str = ""

    def _sync_mode_lease(self, mode: Optional[Mode]) -> None:
        """把"当前模式占哪些资源"同步进资源租约（只登记不拦；P0 生存通道）。"""
        lease = self._lease
        if lease is None:
            self._lease_owner = None
            return
        provider = self._enabled_provider
        if provider is not None:
            try:
                if not provider():
                    self._release_mode_lease()
                    return
            except Exception as exc:  # noqa: BLE001 — 开关读失败按"开"处理
                logger.debug("[Mode] 资源租约开关读取失败: %s", exc)
        owner = f"mode:{mode.name}" if mode is not None else None
        if owner == self._lease_owner:
            return
        self._release_mode_lease()
        if mode is None:
            return
        resources = MODE_RESOURCES.get(mode.name, frozenset())
        if not resources:
            return
        survival = mode.priority <= 0
        try:
            result = lease.request(
                owner, resources, survival=survival,
                priority=PRIORITY_SURVIVAL if survival else PRIORITY_DEFAULT,
                kind="mode", target=mode.name, reason=f"模式 {mode.name}", actor="modes_engine")
        except Exception as exc:  # noqa: BLE001 — 登记失败绝不影响模式执行
            logger.warning("[Mode] 资源登记失败（不影响执行）: %s", exc)
            return
        if result.get("granted"):
            self._lease_owner = owner

    def _release_mode_lease(self) -> None:
        if self._lease is None or self._lease_owner is None:
            self._lease_owner = None
            return
        try:
            self._lease.release(self._lease_owner, "模式结束")
        except Exception as exc:  # noqa: BLE001
            logger.debug("[Mode] 资源释放失败: %s", exc)
        self._lease_owner = None

    def add_mode(self, mode: Mode):
        self.modes.append(mode)
        self._sorted = sorted(self.modes, key=lambda m: m.priority)

    def add_defaults(self):
        """只装**生存反射**（P0/P1/P2/P3）：岩浆/着火/溺水/虚空、回血、低血逃跑、被打还手。

        用户口径：「后台不许有'自主开关/自动触发'」——原来的 P4-P8（auto_eat / item_collect /
        torch_place / hunting / follow_master）全部删除：它们对应的**能力**已经是注册表里的
        工具（`eat` / `collect_blocks`+`move_to` / `place_block` / `attack` / `follow`），
        用不用由 LLM 决定，不再由后台条件自动触发。
        Note: StuckRecoveryMode is NOT auto-triggered — only via user command."""
        self.add_mode(SelfPreservationMode())    # P0 生存：岩浆/着火/溺水/虚空
        self.add_mode(SelfHealMode())            # P1 生存：低血用治疗物品
        self.add_mode(CowardiceMode())           # P2 生存：低血逃跑
        self.add_mode(SelfDefenseMode())         # P3 生存：被打还手

    def tick(self, state: dict, action_busy: bool = False) -> str | None:
        """
        Run one cycle of the modes engine.
        """
        # 当前模式还该不该继续？
        #
        # 历史 bug：这里写的是 `should_activate(...) and not is_on_cooldown`，而模式
        # **刚触发就把自己置入 cooldown**（`_mark_triggered`）→ 条件必然为假 →
        # 每次触发后立刻 Deactivated，2 秒后又 Activated，一直抖（用户实机日志里
        # Activated/Deactivated 每 2 秒一轮就是这么来的）。cooldown 的语义只能是
        # **重新激活**的门（在下面的候选扫描里），绝不能用来掐死正在运行的模式。
        if self._active_mode:
            if self._should_stay_active(self._active_mode, state):
                action = self._active_mode.tick(self._mode_skills, state)
                if action:
                    self._last_action = action
                    logger.info("[Mode] %s: %s", self._active_mode.name, action)
                return action
            logger.info("[Mode] Deactivated: %s", self._active_mode.name)
            self._active_mode.on_deactivate(self._mode_skills)
            self._active_mode = None
            self._sync_mode_lease(None)
            self._reset_streak_credit()

        # Scan modes in priority order
        for mode in self._sorted:
            if mode.is_on_cooldown:
                continue
            if action_busy and not mode.interrupts:
                continue

            if self._should_activate(mode, state):
                mode.active = True
                self._active_mode = mode
                mode.on_activate()
                self._sync_mode_lease(mode)
                logger.info("[Mode] Activated: %s (priority=%d)", mode.name, mode.priority)
                action = mode.tick(self._mode_skills, state)
                if action:
                    self._last_action = action
                    mode._mark_triggered()
                    logger.info("[Mode] %s: %s", mode.name, action)
                if self.memory:
                    self.memory.record_action(f"mode:{mode.name}", True)
                return action

        return None

    @staticmethod
    def _should_activate(mode: Mode, state: dict) -> bool:
        """激活判定（带滞回）：要连续 N 个 tick 都满足才认，挡掉 state 抖动。"""
        if mode.should_activate(state):
            mode._satisfied_streak += 1
            mode._unsatisfied_streak = 0
        else:
            mode._satisfied_streak = 0
        return mode._satisfied_streak >= max(1, mode.activate_streak_required)

    @staticmethod
    def _should_stay_active(mode: Mode, state: dict) -> bool:
        """继续运行判定：连续 M 个 tick 都不满足才放手（宽限 1s），**不看 cooldown**。"""
        if mode.should_activate(state):
            mode._satisfied_streak = 0
            mode._unsatisfied_streak = 0
            return True
        mode._unsatisfied_streak += 1
        return mode._unsatisfied_streak < max(1, mode.deactivate_grace_ticks)

    def _reset_streak_credit(self):
        """清掉"陈旧信用"：别的模式占着引擎时，低优先级模式攒的满足计数不算数。

        否则高优先级模式跑了 10 分钟后一放手，低优先级模式会凭"很久以前的 3 个 tick"
        立刻激活 —— 那不是滞回，是幽灵。
        """
        for mode in self._sorted:
            mode._satisfied_streak = 0

    def reset(self):
        if self._active_mode:
            self._active_mode.on_deactivate(self._mode_skills)
        self._active_mode = None
        self._release_mode_lease()
        for mode in self.modes:
            mode.active = False

    def get_status(self) -> dict:
        return {
            "active": self._active_mode.name if self._active_mode else None,
            "last_action": self._last_action,
            "modes": {m.name: {
                "priority": m.priority,
                "active": m.active,
                "interrupts": m.interrupts,
            } for m in self._sorted},
        }
