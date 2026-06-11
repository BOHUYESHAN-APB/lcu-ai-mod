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

    @abstractmethod
    def should_activate(self, state: dict) -> bool:
        """Check if this mode should activate."""

    @abstractmethod
    def tick(self, skills, state: dict) -> str | None:
        """Execute behavior. Return action description or None."""

    def on_activate(self):
        logger.debug("[Mode] Activated: %s", self.name)

    def on_deactivate(self):
        logger.debug("[Mode] Deactivated: %s", self.name)

    @property
    def is_on_cooldown(self) -> bool:
        return (time.time() - self._last_trigger) < self.cooldown if self.cooldown > 0 else False

    def _mark_triggered(self):
        self._last_trigger = time.time()


# ── Mode Implementations ────────────────────────────────────────

class SelfPreservationMode(Mode):
    """P0 — React to immediate threats: fire, drowning, void."""

    def __init__(self):
        super().__init__("self_preservation", priority=0, interrupts=True, cooldown=1.0)

    def should_activate(self, state: dict) -> bool:
        player = state.get("player", {})
        fire = player.get("fire_ticks", 0)
        air = player.get("air_ticks", 300)
        y = player.get("y", 64)
        return (fire > 0) or (air < 50) or (y < -32)

    def tick(self, skills, state: dict) -> str | None:
        player = state.get("player", {})
        fire = player.get("fire_ticks", 0)
        air = player.get("air_ticks", 300)
        if fire > 0:
            skills.send_chat("I'm on fire!")
            return "self_preservation: on fire"
        if air < 50:
            # Move upward to surface
            skills.move_to(player.get("x", 0), player.get("y", 0) + 10, player.get("z", 0))
            return "self_preservation: drowning, swimming up"
        return "self_preservation: in void"


class SelfHealMode(Mode):
    """P1 — Use healing items when health is low."""

    def __init__(self):
        super().__init__("self_heal", priority=1, interrupts=True, cooldown=10.0)

    def should_activate(self, state: dict) -> bool:
        player = state.get("player", {})
        health = player.get("health", 20)
        max_health = player.get("max_health", 20)
        # Heal when below 30% HP
        return health < max_health * 0.3 if max_health > 0 else health < 6

    def tick(self, skills, state: dict) -> str | None:
        # Try eating food for healing
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
        # Run away: move in random direction
        skills.send_chat("Too dangerous! Retreating!")
        return "cowardice: fleeing"


class SelfDefenseMode(Mode):
    """P3 — Attack nearby hostiles."""

    def __init__(self):
        super().__init__("self_defense", priority=3, interrupts=True, cooldown=2.0)

    def should_activate(self, state: dict) -> bool:
        entities = state.get("entities", [])
        return any(e.get("type") == "hostile" and e.get("distance", 999) < 8
                   for e in entities)

    def tick(self, skills, state: dict) -> str | None:
        entities = state.get("entities", [])
        hostiles = [e for e in entities if e.get("type") == "hostile"
                    and e.get("distance", 999) < 8]
        if hostiles:
            nearest = hostiles[0]
            # Look at the hostile and attack
            skills.look_at(nearest.get("x", 0), nearest.get("y", 0), nearest.get("z", 0))
            skills.attack()
            return f"self_defense: attacking {nearest.get('name', 'hostile')}"
        return None


class AutoEatMode(Mode):
    """P4 — Auto-eat when hungry."""

    def __init__(self):
        super().__init__("auto_eat", priority=4, interrupts=True, cooldown=5.0)

    def should_activate(self, state: dict) -> bool:
        player = state.get("player", {})
        hunger = player.get("hunger", 20)
        return hunger < 14

    def tick(self, skills, state: dict) -> str | None:
        # Scan inventory for food items in hotbar
        inventory = state.get("inventory", [])
        food_slot = None
        food_names = {"minecraft:bread", "minecraft:beef", "minecraft:porkchop", "minecraft:chicken",
                      "minecraft:potato", "minecraft:baked_potato", "minecraft:apple", "minecraft:golden_apple",
                      "minecraft:carrot", "minecraft:cooked_beef", "minecraft:cooked_porkchop",
                      "minecraft:cooked_chicken", "minecraft:cooked_cod", "minecraft:cooked_salmon",
                      "minecraft:bread", "minecraft:cookie", "minecraft:melon_slice", "minecraft:pumpkin_pie",
                      "minecraft:mushroom_stew", "minecraft:beetroot_soup", "minecraft:suspicious_stew",
                      "minecraft:rabbit_stew", "minecraft:steak"}
        for item in inventory:
            slot = item.get("slot", -1)
            name = item.get("name", "")
            if 0 <= slot < 9 and name in food_names:
                food_slot = slot
                break

        if food_slot is not None:
            skills.select_hotbar(food_slot)
            skills.use_item()
            return f"auto_eat: ate {food_slot}"
        return "auto_eat: no food in hotbar"


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
            # Walk to the item
            skills.move_to(nearest.get("x", 0), nearest.get("y", 0), nearest.get("z", 0))
            return f"item_collect: picking up {nearest.get('name', 'item')}"
        return None


class TorchPlaceMode(Mode):
    """P6 — Place torches in dark areas."""

    def __init__(self):
        super().__init__("torch_place", priority=6, interrupts=False, cooldown=15.0)

    def should_activate(self, state: dict) -> bool:
        world = state.get("world", {})
        light = world.get("light_level", 15)
        return light < 7

    def tick(self, skills, state: dict) -> str | None:
        return "torch_place: area too dark (need torch in hotbar)"


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
            skills.move_to(mx, my, mz)
            return f"follow_master: walking to {self._master_name}"
        elif dist < 2:
            # Too close — patrol in a circle around master
            self._patrol_angle += 1.0
            tx = mx + 3 * math.cos(self._patrol_angle)
            tz = mz + 3 * math.sin(self._patrol_angle)
            player = state.get("player", {})
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
        # Check if current active mode should still run
        if self._active_mode:
            if self._active_mode.should_activate(state) and not self._active_mode.is_on_cooldown:
                action = self._active_mode.tick(self.skills, state)
                if action:
                    self._last_action = action
                    logger.info("[Mode] %s: %s", self._active_mode.name, action)
                return action
            else:
                logger.info("[Mode] Deactivated: %s", self._active_mode.name)
                self._active_mode.on_deactivate()
                self._active_mode = None

        # Scan modes in priority order
        for mode in self._sorted:
            if mode.is_on_cooldown:
                continue
            if action_busy and not mode.interrupts:
                continue

            if mode.should_activate(state):
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

    def reset(self):
        if self._active_mode:
            self._active_mode.on_deactivate()
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
