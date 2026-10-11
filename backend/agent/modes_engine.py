"""
Modes Engine — Priority-based autonomous behavior system.
Replicates TouhouLittleMaid's Brain/Activity architecture.

Each mode is a behavioral pattern that activates based on conditions
and executes real actions via the Skills layer.

Priority levels:
  0: SelfPreservation — fire, drowning, void
  1: SelfHeal — use healing items when health is low
  2: Cowardice — flee from hostiles when low health
  3: SelfDefense — attack nearby hostiles
  4: AutoEat — eat food when hungry
  5: ItemCollect — pick up nearby items
  6: TorchPlace — place torches in dark areas
  7: Hunting — hunt animals for food
"""

import logging
import math
import time
from abc import ABC, abstractmethod
from typing import Any, Optional

logger = logging.getLogger("modes_engine")


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


# ── 食物（原版 FoodProperties：营养值 / 饱和度）──────────────────
# Java 侧能直接读物品组件 `FoodProperties.nutrition()/saturation()`；Python 侧拿不到组件，
# 所以照抄原版数据包的数值做成表 —— 这比"名字里带 meat 就算好"这类猜测可靠得多。
#
# 两个数管的是两件事（用户点名要搞清楚）：
#   nutrition  = 饱食度（food level）补多少，决定"饿不饿"
#   saturation = 饱和度（隐藏值），决定"能不能回血 / 扛多久"
# 自然回血的条件是「饱食度 ≥18 且饱和度 >0」，所以：
#   - 缺血（血量不满）→ 优先**高饱和度**（金胡萝卜/熟肉/面包），先让回血转起来；
#   - 单纯饿（血量满）→ 优先**高营养**，先把饥饿条补满。
FOOD_PROPERTIES: dict[str, tuple[int, float]] = {
    "minecraft:golden_apple": (4, 9.6),
    "minecraft:enchanted_golden_apple": (4, 9.6),
    "minecraft:golden_carrot": (6, 14.4),
    "minecraft:cooked_beef": (8, 12.8),
    "minecraft:cooked_porkchop": (8, 12.8),
    "minecraft:cooked_mutton": (6, 9.6),
    "minecraft:cooked_salmon": (6, 9.6),
    "minecraft:cooked_chicken": (6, 7.2),
    "minecraft:cooked_rabbit": (5, 6.0),
    "minecraft:cooked_cod": (5, 6.0),
    "minecraft:rabbit_stew": (10, 12.0),
    "minecraft:baked_potato": (5, 6.0),
    "minecraft:bread": (5, 6.0),
    "minecraft:pumpkin_pie": (8, 4.8),
    "minecraft:mushroom_stew": (6, 7.2),
    "minecraft:beetroot_soup": (6, 7.2),
    "minecraft:suspicious_stew": (6, 7.2),
    "minecraft:apple": (4, 2.4),
    "minecraft:carrot": (3, 3.6),
    "minecraft:melon_slice": (2, 1.2),
    "minecraft:glow_berries": (2, 0.4),
    "minecraft:sweet_berries": (2, 0.4),
    "minecraft:cookie": (2, 0.4),
    "minecraft:beetroot": (1, 1.2),
    "minecraft:dried_kelp": (1, 0.6),
    "minecraft:potato": (1, 0.6),
    "minecraft:beef": (3, 1.8),
    "minecraft:porkchop": (3, 1.8),
    "minecraft:rabbit": (3, 1.8),
    "minecraft:chicken": (2, 1.2),
    "minecraft:mutton": (2, 1.2),
    "minecraft:cod": (2, 0.4),
    "minecraft:salmon": (2, 0.4),
    "minecraft:tropical_fish": (1, 0.2),
}

# 有害/带负面效果的：**永不自吃**（腐肉是"能填饱但会饿回去"，吃了等于白吃）。
HARMFUL_FOOD = {
    "minecraft:rotten_flesh",
    "minecraft:spider_eye",
    "minecraft:poisonous_potato",
    "minecraft:pufferfish",
    "minecraft:chorus_fruit",
}

# 生食：能吃但收益差，只在没有更好的选择时兜底（与 Java 侧反射同一口径）。
# 注意用**精确 id**，别用 "chicken" 子串 —— 会误伤熟鸡肉。
RAW_FOOD = {
    "minecraft:chicken",
    "minecraft:beef",
    "minecraft:porkchop",
    "minecraft:mutton",
    "minecraft:rabbit",
    "minecraft:cod",
    "minecraft:salmon",
    "minecraft:tropical_fish",
}


def select_food(items, hurt: bool = False):
    """从物品里挑最该吃的那一件，挑不出返回 None。

    hurt=True（血量不满）→ 按**饱和度**优先；否则按**营养值**优先。
    有害食物永不自吃；生食只在没有熟食/加工食物时兜底。
    """
    preferred: list[tuple[tuple[int, float], dict]] = []
    raw: list[tuple[tuple[int, float], dict]] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip().lower()
        if name in HARMFUL_FOOD:
            continue
        props = FOOD_PROPERTIES.get(name)
        if props is None:
            continue
        (raw if name in RAW_FOOD else preferred).append((props, item))
    pool = preferred or raw
    if not pool:
        return None
    if hurt:
        # 饱和度优先、营养值次之
        return max(pool, key=lambda pair: (pair[0][1], pair[0][0]))[1]
    return max(pool, key=lambda pair: (pair[0][0], pair[0][1]))[1]


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
        """Nearest water source within perception range, if any."""
        water = [b for b in state.get("nearby_blocks", [])
                 if "water" in str(b.get("block_id", ""))]
        if not water:
            return None
        return min(water, key=lambda b: b.get("distance", 999))


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
    """P2 — Flee from hostiles when low health."""

    def __init__(self):
        super().__init__("cowardice", priority=2, interrupts=True, cooldown=5.0)

    def should_activate(self, state: dict) -> bool:
        player = state.get("player", {})
        health = player.get("health", 20)
        if health > 8:
            return False
        entities = state.get("entities", [])
        return any(e.get("type") == "hostile" for e in entities)

    def tick(self, skills, state: dict) -> str | None:
        player = state.get("player", {})
        px = player.get("x", 0)
        py = player.get("y", 0)
        pz = player.get("z", 0)
        hostiles = [e for e in state.get("entities", [])
                    if e.get("type") == "hostile" and e.get("distance", 999) < 16]
        if not hostiles:
            return None
        nearest = min(hostiles, key=lambda e: e.get("distance", 999))
        # 逃跑目标每秒重算一次就够；每 tick 重发 move_to 只会把寻路刷屏。
        target = (f"flee:{nearest.get('name', '?')}:"
                  f"{nearest.get('entity_id') or nearest.get('id') or ''}")
        if not self.should_dispatch(target):
            return None
        dx = px - nearest.get("x", px)
        dz = pz - nearest.get("z", pz)
        mag = (dx * dx + dz * dz) ** 0.5 or 1.0
        # Flee 12 blocks directly away from the nearest hostile
        skills.move_to(px + dx / mag * 12, py, pz + dz / mag * 12, 1.0)
        return f"cowardice: fleeing {nearest.get('name', 'hostile')} (dist {nearest.get('distance', '?')}m)"


class SelfDefenseMode(Mode):
    """P3 — Attack nearby hostiles."""

    def __init__(self):
        super().__init__("self_defense", priority=3, interrupts=True, cooldown=2.0)

    def should_activate(self, state: dict) -> bool:
        # 本地战斗反射正在交战（毫秒级闭环、已对准才出手）→ 后端别同时出手：
        # 两边一起发 attack/look_at 就是用户实机日志里那个 2 秒一抖的循环。
        if state.get("reflex_combat"):
            return False
        entities = state.get("entities", [])
        return any(e.get("type") == "hostile" and e.get("distance", 999) < 8
                   for e in entities)

    def tick(self, skills, state: dict) -> str | None:
        if state.get("reflex_combat"):
            return None
        entities = state.get("entities", [])
        hostiles = [e for e in entities if e.get("type") == "hostile"
                    and e.get("distance", 999) < 8]
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
            return f"self_defense: attacking {nearest.get('name', 'hostile')}"
        return None


class AutoEatMode(Mode):
    """P4 — Auto-eat：饿/缺血时吃热栏里的食物。

    触发条件（用户口径："健康<满就叫到饱食度 18+，而不是只看到 14 才吃"）：
      - 单纯饿：饱食度 < 14；
      - 缺血：血量不满（< 上限）且饱食度 < 18 —— 自然回血要「饱食度 ≥18 且饱和度 >0」，
        所以血量不满就该主动补到 18+，而不是等饿到见底。
    选食物：缺血优先**高饱和度**（回血门槛靠饱和度），单纯饿优先**高营养**；
    有害食物永不自吃、生食兜底 —— 见 `select_food`。
    """

    # 单纯饿的阈值：到 14 就该补（与原版/参考实现一致的保守值）。
    HUNGRY_HUNGER = 14
    # 缺血时的目标饱食度：吃到 18+ 才可能自然回血。
    HURT_HUNGER = 18

    def __init__(self):
        super().__init__("auto_eat", priority=4, interrupts=True, cooldown=5.0)
        # 一次进食是一个持续动作，别在模式活跃期间每 tick 重发 use_item。
        self.redispatch_interval = 2.0

    @staticmethod
    def _num(value, default: float) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return float(default)

    def _hunger_and_hurt(self, state: dict) -> tuple[float, bool]:
        player = state.get("player", {}) if isinstance(state, dict) else {}
        if not isinstance(player, dict):
            player = {}
        hunger = self._num(player.get("hunger"), 20.0)
        health = self._num(player.get("health"), 20.0)
        max_health = self._num(player.get("max_health"), 20.0)
        return hunger, health < max_health

    def should_activate(self, state: dict) -> bool:
        hunger, hurt = self._hunger_and_hurt(state)
        if hunger < self.HUNGRY_HUNGER:
            return True
        return hurt and hunger < self.HURT_HUNGER

    def tick(self, skills, state: dict) -> str | None:
        hunger, hurt = self._hunger_and_hurt(state)
        # 只看热栏：wire 的 use_item 用的是"当前手持格"，背包里的食物再好吃也没法直接吃。
        hotbar = [
            item for item in (state.get("inventory") or [])
            if isinstance(item, dict) and 0 <= int(item.get("slot", -1)) < 9
        ]
        food = select_food(hotbar, hurt=hurt)
        if food is None:
            return "auto_eat: no food in hotbar"
        slot = int(food.get("slot", -1))
        if not self.should_dispatch(f"eat:{slot}:{food.get('name', '')}"):
            return None
        skills.select_hotbar(slot)
        skills.use_item()
        mode = "saturation-first" if hurt else "nutrition-first"
        return (f"auto_eat: ate {food.get('name')} (hunger={hunger:.0f}, "
                f"{'hurt ' if hurt else ''}{mode})")


class ItemCollectMode(Mode):
    """P5 — Pick up nearby items."""

    def __init__(self):
        super().__init__("item_collect", priority=5, interrupts=False, cooldown=3.0)

    def should_activate(self, state: dict) -> bool:
        entities = state.get("entities", [])
        return any(e.get("type") == "item" and e.get("distance", 999) < 4
                   for e in entities)

    def tick(self, skills, state: dict) -> str | None:
        entities = state.get("entities", [])
        items = [e for e in entities if e.get("type") == "item"
                 and e.get("distance", 999) < 4]
        if items:
            nearest = items[0]
            target = f"item:{nearest.get('x', 0):.1f},{nearest.get('y', 0):.1f},{nearest.get('z', 0):.1f}"
            if not self.should_dispatch(target):
                return None
            # Walk to the item
            skills.move_to(nearest.get("x", 0), nearest.get("y", 0), nearest.get("z", 0))
            return f"item_collect: picking up {nearest.get('name', 'item')}"
        return None


class TorchPlaceMode(Mode):
    """P6 — Place torches in dark areas."""

    def __init__(self):
        super().__init__("torch_place", priority=6, interrupts=False, cooldown=2.0)
        self._arming = False  # True after look_at; place_block fires next tick

    def should_activate(self, state: dict) -> bool:
        world = state.get("world", {})
        light = world.get("light_level", 15)
        if light >= 7:
            self._arming = False
            return False
        return True

    def tick(self, skills, state: dict) -> str | None:
        torch = next((it for it in state.get("inventory", [])
                      if str(it.get("name", "")).endswith("torch")), None)
        if torch is None:
            return "torch_place: dark area but no torch in inventory"
        if torch.get("slot", 9) > 8:
            return "torch_place: torch in backpack (hotbar slot needed)"

        if not self._arming:
            player = state.get("player", {})
            px, py, pz = player.get("x", 0), player.get("y", 0), player.get("z", 0)
            px_i, pz_i = int(px), int(pz)
            # Aim at the exposed top face of a nearby solid block that is not
            # in the player's own column (torch needs the space we occupy).
            candidates = [
                b for b in state.get("nearby_blocks", [])
                if b.get("y") in (py - 1, py, py + 1)
                and not (b.get("x") == px_i and b.get("z") == pz_i)
                and abs(b.get("x", px) + 0.5 - px) < 4
                and abs(b.get("z", pz) + 0.5 - pz) < 4
            ]
            if not candidates:
                return "torch_place: no adjacent solid block to place on"
            target = min(candidates, key=lambda b:
                         (b.get("x", px) + 0.5 - px) ** 2 + (b.get("z", pz) + 0.5 - pz) ** 2)
            skills.look_at(target["x"] + 0.5, target["y"] + 1.0, target["z"] + 0.5)
            self._arming = True
            return "torch_place: aiming at placement face"

        self._arming = False
        skills.place_block()
        return "torch_place: placed torch"


class HuntingMode(Mode):
    """P7 — Hunt animals for food."""

    def __init__(self):
        super().__init__("hunting", priority=7, interrupts=False, cooldown=10.0)

    def should_activate(self, state: dict) -> bool:
        player = state.get("player", {})
        hunger = player.get("hunger", 20)
        if hunger > 12:
            return False
        entities = state.get("entities", [])
        return any(e.get("type") == "animal" and e.get("distance", 999) < 12
                   for e in entities)

    def tick(self, skills, state: dict) -> str | None:
        entities = state.get("entities", [])
        animals = [e for e in entities if e.get("type") == "animal"
                   and e.get("distance", 999) < 12]
        if animals:
            nearest = animals[0]
            target = (f"animal:{nearest.get('name', '?')}:"
                      f"{nearest.get('entity_id') or nearest.get('id') or ''}")
            if not self.should_dispatch(target):
                return None
            skills.look_at(nearest.get("x", 0), nearest.get("y", 0), nearest.get("z", 0))
            skills.attack()
            return f"hunting: hunting {nearest.get('name', 'animal')}"
        return None


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


class FollowMasterMode(Mode):
    """P8 — Follow / patrol around a designated master player (TouhouLittleMaid-style)."""

    def __init__(self):
        super().__init__("follow_master", priority=8, interrupts=False, cooldown=4.0)
        self._master_name = ""
        self._patrol_angle = 0.0

    def set_master(self, name: str):
        self._master_name = name
        logger.info("[Mode] Master set to: %s", name)

    def should_activate(self, state: dict) -> bool:
        if not self._master_name:
            return False
        entities = state.get("entities", [])
        master = next((e for e in entities if e.get("type") == "player"
                       and e.get("name", "") == self._master_name), None)
        if master is None:
            return False
        dist = master.get("distance", 999)
        return dist > 3  # only activate when >3 blocks away

    def tick(self, skills, state: dict) -> str | None:
        entities = state.get("entities", [])
        master = next((e for e in entities if e.get("type") == "player"
                       and e.get("name", "") == self._master_name), None)
        if not master:
            return None

        import math
        dist = master.get("distance", 999)
        mx, my, mz = master.get("x", 0), master.get("y", 0), master.get("z", 0)

        if dist > 6:
            # Too far — walk toward master
            if not self.should_dispatch(f"to_master:{dist:.0f}"):
                return None
            skills.move_to(mx, my, mz)
            return f"follow_master: walking to {self._master_name}"
        elif dist < 2:
            # Too close — patrol in a circle around master
            self._patrol_angle += 1.0
            tx = mx + 3 * math.cos(self._patrol_angle)
            tz = mz + 3 * math.sin(self._patrol_angle)
            player = state.get("player", {})
            target = f"patrol:{tx:.1f},{tz:.1f}"
            if not self.should_dispatch(target):
                return None
            skills.move_to(tx, player.get("y", my), tz)
            return f"follow_master: patrolling around {self._master_name}"
        return None


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

    def __init__(self, skills, memory=None):
        self.skills = skills
        self.memory = memory
        self.modes: list[Mode] = []
        self._sorted: list[Mode] = []
        self._active_mode: Optional[Mode] = None
        self._last_action: str = ""

    def add_mode(self, mode: Mode):
        self.modes.append(mode)
        self._sorted = sorted(self.modes, key=lambda m: m.priority)

    def add_defaults(self):
        """Add all built-in modes (matches TouhouLittleMaid activity groups).
        Note: StuckRecoveryMode is NOT auto-triggered — only via user command."""
        self.add_mode(SelfPreservationMode())    # CORE: survival
        self.add_mode(SelfHealMode())            # CORE: health management
        self.add_mode(CowardiceMode())           # PANIC: flee
        self.add_mode(SelfDefenseMode())         # WORK: combat
        self.add_mode(AutoEatMode())             # IDLE: eat
        self.add_mode(ItemCollectMode())         # IDLE: collect
        self.add_mode(TorchPlaceMode())          # WORK: utility
        self.add_mode(HuntingMode())             # WORK: food
        self.add_mode(FollowMasterMode())       # IDLE: follow/patrol

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
                action = self._active_mode.tick(self.skills, state)
                if action:
                    self._last_action = action
                    logger.info("[Mode] %s: %s", self._active_mode.name, action)
                return action
            logger.info("[Mode] Deactivated: %s", self._active_mode.name)
            self._active_mode.on_deactivate(self.skills)
            self._active_mode = None
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
                logger.info("[Mode] Activated: %s (priority=%d)", mode.name, mode.priority)
                action = mode.tick(self.skills, state)
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
            self._active_mode.on_deactivate(self.skills)
        self._active_mode = None
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
