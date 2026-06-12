"""
Commands — LLM output → action command parser.
Replicates mindcraft's commands/index.js + actions.js + queries.js.

Two types of commands:
  - Queries: read-only, return info (!stats, !inventory, !nearby)
  - Actions: modify world, executed via skills (!goTo, !attack, !collect)

Command format in LLM output:
  !commandName [arg1] [arg2] ...
"""

import logging
import re
from typing import Any, Callable, Optional

logger = logging.getLogger("commands")

# Regex to find !command patterns, stopping before the next !command.
CMD_RE = re.compile(r'!(\w+)((?:\s+(?!\!)\S+)*)')


class CommandResult:
    """Result of executing a command."""

    def __init__(self, success: bool, message: str = "", data: Optional[dict] = None):
        self.success = success
        self.message = message
        self.data = data or {}


class Commands:
    """
    Command registry and parser.
    LLM output is scanned for !command patterns, which are dispatched to handlers.

    Usage:
        cmds = Commands(skills)
        results = cmds.parse_and_execute(llm_output, state)
    """

    def __init__(self, skills):
        self.skills = skills
        self._queries: dict[str, Callable] = {}
        self._actions: dict[str, Callable] = {}
        self._register_defaults()

    # ── Registration ──

    def register_query(self, name: str, handler: Callable):
        """Register a read-only query command."""
        self._queries[name.lower()] = handler

    def register_action(self, name: str, handler: Callable):
        """Register a write/modify action command."""
        self._actions[name.lower()] = handler

    def get_docs(self) -> str:
        """Return command documentation string for LLM system prompt."""
        lines = ["Available commands:"]
        for name in sorted(self._queries):
            lines.append(f"  !{name} - query")
        for name in sorted(self._actions):
            lines.append(f"  !{name} - action")
        return "\n".join(lines)

    # ── Parse & Execute ──

    def parse_and_execute(self, text: str, state: dict) -> list[CommandResult]:
        """
        Parse !commands from LLM output text and execute them.

        Returns list of CommandResult (one per command found).
        """
        results = []
        for match in CMD_RE.finditer(text):
            cmd_name = match.group(1).lower()
            args_str = match.group(2).strip()
            args = args_str.split() if args_str else []

            result = self._execute_one(cmd_name, args, state)
            results.append(result)
            logger.info("[Cmd] !%s %s → success=%s msg=%s", cmd_name, args, result.success, result.message[:60])

        return results

    def _execute_one(self, name: str, args: list[str], state: dict) -> CommandResult:
        """Execute a single command by name."""
        if name in self._queries:
            try:
                return self._queries[name](args, state)
            except Exception as e:
                return CommandResult(False, f"Error in !{name}: {e}")

        if name in self._actions:
            try:
                self._actions[name](args, state)
                return CommandResult(True, f"!{name} executed")
            except Exception as e:
                return CommandResult(False, f"Error executing !{name}: {e}")

        return CommandResult(False, f"Unknown command: !{name}")

    def has_command(self, text: str) -> bool:
        """Check if text contains any !command."""
        return bool(CMD_RE.search(text))

    # ── Default Commands (replicating mindcraft's queries + actions) ──

    def _register_defaults(self):
        # ── Queries ──
        self.register_query("stats", self._query_stats)
        self.register_query("inventory", self._query_inventory)
        self.register_query("nearby", self._query_nearby)
        self.register_query("position", self._query_position)
        self.register_query("health", self._query_health)
        self.register_query("time", self._query_time)
        self.register_query("biome", self._query_biome)
        self.register_query("entities", self._query_entities)

        # ── Actions ──
        self.register_action("goTo", self._act_goto)
        self.register_action("goToPosition", self._act_goto)
        self.register_action("moveTo", self._act_goto)
        self.register_action("attack", self._act_attack)
        self.register_action("follow", self._act_follow)
        self.register_action("followPlayer", self._act_follow)
        self.register_action("collect", self._act_collect)
        self.register_action("collectBlocks", self._act_collect)
        self.register_action("mine", self._act_collect)
        self.register_action("lookAt", self._act_look_at)
        self.register_action("equip", self._act_equip)
        self.register_action("craft", self._act_craft)
        self.register_action("place", self._act_place)
        self.register_action("placeBlock", self._act_place)
        self.register_action("drop", self._act_drop)
        self.register_action("chat", self._act_chat)
        self.register_action("say", self._act_chat)
        self.register_action("sleep", self._act_sleep)
        self.register_action("stop", self._act_stop)
        self.register_action("resume", self._act_resume)

    # ── Query Handlers ──

    def _query_stats(self, args: list[str], state: dict) -> CommandResult:
        """!stats — Return player status."""
        player = state.get("player", {})
        world = state.get("world", {})
        msg = (
            f"Health: {player.get('health', '?')}/{player.get('max_health', 20)} | "
            f"Hunger: {player.get('hunger', '?')}/20 | "
            f"Position: ({player.get('x', '?'):.1f}, {player.get('y', '?'):.1f}, {player.get('z', '?'):.1f}) | "
            f"Dimension: {world.get('dimension', '?')} | "
            f"Time: {world.get('time', '?')} | "
            f"Weather: {world.get('weather', 'clear')}"
        )
        return CommandResult(True, msg)

    def _query_inventory(self, args: list[str], state: dict) -> CommandResult:
        """!inventory — List inventory contents."""
        inv = state.get("inventory", [])
        if not inv:
            return CommandResult(True, "Inventory is empty")
        items = [f"{i.get('count', 1)}x {i.get('name', '?')}" for i in inv[:36]]
        msg = "Inventory: " + ", ".join(items)
        return CommandResult(True, msg)

    def _query_nearby(self, args: list[str], state: dict) -> CommandResult:
        """!nearby [radius] — List nearby blocks."""
        radius = int(args[0]) if args else 5
        blocks = state.get("nearby_blocks", [])
        matched = [b for b in blocks if b.get("distance", 999) <= radius][:20]
        if not matched:
            return CommandResult(True, f"No blocks within {radius}m")
        msg = "Nearby: " + ", ".join(f"{b['name']} ({b['distance']}m)" for b in matched)
        return CommandResult(True, msg)

    def _query_position(self, args: list[str], state: dict) -> CommandResult:
        """!position — Current coordinates."""
        p = state.get("player", {})
        return CommandResult(True, f"Position: {p.get('x', 0):.1f} {p.get('y', 0):.1f} {p.get('z', 0):.1f}")

    def _query_health(self, args: list[str], state: dict) -> CommandResult:
        """!health — Health and hunger."""
        p = state.get("player", {})
        return CommandResult(True, f"Health: {p.get('health', 20)}/{p.get('max_health', 20)}  Hunger: {p.get('hunger', 20)}/20")

    def _query_time(self, args: list[str], state: dict) -> CommandResult:
        """!time — Current game time."""
        w = state.get("world", {})
        return CommandResult(True, f"Time: {w.get('time', '?')}  Weather: {w.get('weather', 'clear')}")

    def _query_biome(self, args: list[str], state: dict) -> CommandResult:
        """!biome — Current biome."""
        w = state.get("world", {})
        return CommandResult(True, f"Biome: {w.get('biome', 'unknown')}")

    def _query_entities(self, args: list[str], state: dict) -> CommandResult:
        """!entities [radius] — List nearby entities."""
        radius = int(args[0]) if args else 10
        entities = state.get("entities", [])
        nearby = [e for e in entities if e.get("distance", 999) <= radius]
        if not nearby:
            return CommandResult(True, f"No entities within {radius}m")
        msg = "Entities: " + ", ".join(
            f"{e.get('name', '?')} ({e.get('distance', 0):.0f}m)" for e in nearby[:10]
        )
        return CommandResult(True, msg)

    # ── Action Handlers ──

    def _act_goto(self, args: list[str], state: dict):
        """!goTo x y z  or  !goToPosition x y z"""
        if len(args) >= 3:
            try:
                x, y, z = float(args[0]), float(args[1]), float(args[2])
                self.skills.move_to(x, y, z)
            except ValueError:
                logger.warning("[Cmd] Invalid coordinates: %s", args)

    def _act_attack(self, args: list[str], state: dict):
        """!attack [entity_type]"""
        self.skills.attack()

    def _act_follow(self, args: list[str], state: dict):
        """!follow [player_name]"""
        if args:
            self.skills.follow_player(args[0])

    def _act_collect(self, args: list[str], state: dict):
        """!collect [block_type] [count]"""
        if args:
            block_type = args[0]
            count = int(args[1]) if len(args) > 1 else 1
            self.skills.collect_blocks(block_type, count)

    def _act_look_at(self, args: list[str], state: dict):
        """!lookAt x y z"""
        if len(args) >= 3:
            try:
                x, y, z = float(args[0]), float(args[1]), float(args[2])
                self.skills.look_at(x, y, z)
            except ValueError:
                pass

    def _act_equip(self, args: list[str], state: dict):
        """!equip [slot]"""
        slot = args[0] if args else "mainhand"
        self.skills.equip(slot)

    def _act_craft(self, args: list[str], state: dict):
        """!craft [recipe_name] [count]"""
        if args:
            self.skills.craft_item(args[0])

    def _act_place(self, args: list[str], state: dict):
        """!place x y z [block_type]"""
        self.skills.place_block()

    def _act_drop(self, args: list[str], state: dict):
        """!drop [count]"""
        count = int(args[0]) if args else 1
        self.skills.drop(-1, count)

    def _act_chat(self, args: list[str], state: dict):
        """!chat [message]  or  !say [message]"""
        if args:
            self.skills.send_chat(" ".join(args))

    def _act_sleep(self, args: list[str], state: dict):
        """!sleep — sleep in bed."""
        self.skills.sleep()

    def _act_stop(self, args: list[str], state: dict):
        """!stop — stop current action."""
        self.skills.stop_all()

    def _act_resume(self, args: list[str], state: dict):
        """!resume — resume last action."""
        pass  # Handled by action manager
