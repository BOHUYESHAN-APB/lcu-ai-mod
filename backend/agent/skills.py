"""
Skills — Python skill library matching mineflayer's API surface.
Sends action commands to Java mod via wire protocol.

Reference projects:
  - mineflayer: lib/plugins/ (physics, digging, inventory, place_block, etc.)
  - mindcraft: commands/actions.js (25+ actions)
  - TouhouLittleMaid: entity/task/ (23 tasks)
"""

import json
import logging
import time
from contextlib import contextmanager
from typing import Any, Optional

from protocol import BodyAdapter

logger = logging.getLogger("skills")


class Skills:
    """
    Skill library — Python-side functions that send commands to Java mod.
    Mirrors mineflayer's bot API surface.

    Each method returns dict: {"success": bool, "message": str, "req_id": str}
    """

    def __init__(self, body: BodyAdapter | None = None):
        self.body = body
        self._command_dispatcher = None
        self._command_observer = None
        self._command_context = "default"

    def set_body(self, body: BodyAdapter):
        self.body = body

    def set_command_observer(self, observer):
        self._command_observer = observer

    def set_command_dispatcher(self, dispatcher):
        self._command_dispatcher = dispatcher

    @contextmanager
    def command_context(self, context: str):
        previous = self._command_context
        self._command_context = context
        try:
            yield
        finally:
            self._command_context = previous

    # ── Movement (mineflayer physics.js) ───────────────────────

    def set_control_state(self, control: str, state: bool) -> dict:
        """Set individual movement key state (mineflayer-style).
        Controls: forward, back, left, right, jump, sneak, sprint"""
        return self._send_cmd("set_control_state", {"control": control, "state": state})

    def move_to(self, x: float, y: float, z: float, speed: float = 1.0) -> dict:
        """Move to absolute coordinates (continuous W key)."""
        return self._send_cmd("move_to", {"x": x, "y": y, "z": z, "speed": speed})

    def look_at(self, x: float, y: float, z: float) -> dict:
        """Look at a coordinate."""
        return self._send_cmd("look_at", {"x": x, "y": y, "z": z})

    def look_at_entity(self, entity_id: int) -> dict:
        """Look at a specific entity by ID."""
        return self._send_cmd("look_at_entity", {"id": entity_id})

    def jump(self) -> dict:
        return self._send_cmd("jump", {})

    def sneak(self, activate: bool = True) -> dict:
        return self._send_cmd("sneak", {"sneak": activate})

    def sprint(self, activate: bool = True) -> dict:
        return self._send_cmd("sprint", {"sprint": activate})

    def stop_all(self) -> dict:
        """Stop all movement and actions."""
        return self._send_cmd("stop_all", {})

    def disarm(self) -> dict:
        """Stop all work and return control to the local player."""
        return self._send_cmd("disarm", {})

    # ── Combat (mineflayer entities.js, mindcraft commands) ────

    def attack(self) -> dict:
        """Attack the entity currently being looked at."""
        return self._send_cmd("attack", {})

    def attack_entity(self, entity_id: int) -> dict:
        """Attack a specific entity by ID."""
        return self._send_cmd("attack_entity", {"entity_id": entity_id})

    # ── Block Interaction (mineflayer digging.js) ──────────────

    def mine_block(self) -> dict:
        """Start mining the block being looked at (auto-equips best tool)."""
        return self._send_cmd("mine_block", {})

    def mine_block_at(self, x: int, y: int, z: int, face: str | None = None) -> dict:
        payload = {"x": x, "y": y, "z": z}
        if face:
            payload["face"] = face
        return self._send_cmd("mine_block_at", payload)

    def stop_digging(self) -> dict:
        """Stop current digging."""
        return self._send_cmd("stop_digging", {})

    def auto_equip(self) -> dict:
        """Auto-equip best tool for targeted block."""
        return self._send_cmd("auto_equip", {})

    def place_block(self) -> dict:
        """Place held block at targeted position."""
        return self._send_cmd("place_block", {})

    def interact_block(self) -> dict:
        """Right-click the targeted block."""
        return self._send_cmd("interact_block", {})

    def interact_block_at(self, x: int, y: int, z: int, face: str | None = None) -> dict:
        payload = {"x": x, "y": y, "z": z}
        if face:
            payload["face"] = face
        return self._send_cmd("interact_block_at", payload)

    def equip_item(self, item: str) -> dict:
        return self._send_cmd("equip_item", {"item": item})

    def use_on(self) -> dict:
        """Right-click whatever is targeted (block or entity)."""
        return self._send_cmd("use_on", {})

    def use_item(self) -> dict:
        """Use held item (eat, drink, activate)."""
        return self._send_cmd("use_item", {})

    # ── Entity Interaction (mineflayer) ─────────────────────────

    def use_on_entity(self, entity_id: int) -> dict:
        """Right-click a specific entity."""
        return self._send_cmd("use_on_entity", {"id": entity_id})

    # ── Inventory (mineflayer inventory.js) ─────────────────────

    def get_inventory(self) -> dict:
        """Get full player inventory."""
        return self._send_cmd("get_inventory", {})

    def inspect_block(self, x: int, y: int, z: int) -> dict:
        """Inspect one loaded block and its structured state properties."""
        return self._send_cmd("inspect_block", {"x": x, "y": y, "z": z})

    def scan_crops(self, radius: int = 8) -> dict:
        """Scan nearby loaded blocks for supported vanilla crops."""
        return self._send_cmd("scan_crops", {"radius": radius})

    def harvest_crop_at(self, x: int, y: int, z: int, block_id: str, age: int,
                        target_token: str) -> dict:
        """Harvest and replant one observed mature vanilla crop."""
        return self._send_cmd("harvest_crop_at", {
            "x": x, "y": y, "z": z, "block_id": block_id, "age": age,
            "target_token": target_token,
        })

    def break_block_at(self, x: int, y: int, z: int, target_token: str,
                       face: str | None = None) -> dict:
        payload = {"x": x, "y": y, "z": z, "target_token": target_token}
        if face:
            payload["face"] = face
        return self._send_cmd("break_block_at", payload)

    def use_block_at(self, x: int, y: int, z: int, target_token: str,
                     face: str | None = None) -> dict:
        payload = {"x": x, "y": y, "z": z, "target_token": target_token}
        if face:
            payload["face"] = face
        return self._send_cmd("use_block_at", payload)

    def place_block_at(self, x: int, y: int, z: int, target_token: str,
                       place_x: int, place_y: int, place_z: int, item_id: str,
                       face: str | None = None) -> dict:
        payload = {
            "x": x, "y": y, "z": z, "target_token": target_token,
            "place_x": place_x, "place_y": place_y, "place_z": place_z,
            "item_id": item_id,
        }
        if face:
            payload["face"] = face
        return self._send_cmd("place_block_at", payload)

    def get_recipes(self, item: str) -> dict:
        return self._send_cmd("get_recipes", {"item": item})

    def inventory_click(self, container_id: int, expected_state_id: int, slot: int, click_type: str, button: int = 0) -> dict:
        return self._send_cmd("inventory_click", {
            "container_id": container_id,
            "expected_state_id": expected_state_id,
            "slot": slot,
            "click_type": click_type,
            "button": button,
        })

    def container_button(self, container_id: int, expected_state_id: int, button_id: int) -> dict:
        return self._send_cmd("container_button", {
            "container_id": container_id,
            "expected_state_id": expected_state_id,
            "button_id": button_id,
        })

    def place_recipe(self, container_id: int, expected_state_id: int, recipe_id: str, craft_all: bool = False) -> dict:
        return self._send_cmd("place_recipe", {
            "container_id": container_id,
            "expected_state_id": expected_state_id,
            "recipe_id": recipe_id,
            "craft_all": craft_all,
        })

    def select_hotbar(self, index: int) -> dict:
        """Select hotbar slot 0-8."""
        return self._send_cmd("select_hotbar", {"index": index})

    def equip(self, slot: str = "mainhand") -> dict:
        """Equip item from hotbar."""
        return self._send_cmd("equip", {"slot": slot})

    def drop(self, slot: int = -1, count: int = 1) -> dict:
        return self._send_cmd("drop", {"slot": slot, "count": count})

    # ── Container (mineflayer chest.js) ─────────────────────────

    def open_container(self) -> dict:
        """Open container by right-clicking targeted block."""
        return self._send_cmd("use_on", {})

    def get_container(self) -> dict:
        """Read contents of currently open container."""
        return self._send_cmd("get_container", {})

    def take_item(self, container_id: int, expected_state_id: int, slot: int) -> dict:
        """Take item from container slot into player inventory."""
        return self._send_cmd("take_item", {"container_id": container_id, "expected_state_id": expected_state_id, "slot": slot})

    def put_item(self, container_id: int, expected_state_id: int, slot: int) -> dict:
        """Put item from player inventory into container slot."""
        return self._send_cmd("put_item", {"container_id": container_id, "expected_state_id": expected_state_id, "slot": slot})

    def close_container(self) -> dict:
        """Close current container GUI."""
        return self._send_cmd("close_container", {})

    # ── State Queries (mineflayer, mindcraft) ──────────────────

    def get_state(self) -> dict:
        """Get full player/world state."""
        return self._send_cmd("get_state", {})

    def observe_gui(self, include_image: bool = False) -> dict:
        return self._send_cmd("observe_gui", {"include_image": include_image})

    def get_keybindings(self) -> dict:
        return self._send_cmd("get_keybindings", {})

    def ui_click(self, screen_revision: int, x: float, y: float, button: int = 0) -> dict:
        return self._send_cmd("ui_click", {"screen_revision": screen_revision, "x": x, "y": y, "button": button})

    def key_press(self, screen_revision: int, mapping_id: str) -> dict:
        return self._send_cmd("key_press", {"screen_revision": screen_revision, "mapping_id": mapping_id})

    # ── Chat ────────────────────────────────────────────────────

    def send_chat(self, message: str) -> dict:
        """Send a chat message (rate-limited at 1.5s)."""
        return self._send_cmd("send_chat", {"message": message})

    def server_command(self, family: str, command: str) -> dict:
        """Send a backend-evaluated, narrowly allowlisted server command."""
        return self._send_cmd("server_command", {"family": family, "command": command})

    # ── Behavior Control (mindcraft modes.js) ───────────────────

    def behavior_enable(self) -> dict:
        """Enable autonomous behavior modes."""
        return self._send_cmd("behavior_enable", {})

    def behavior_disable(self) -> dict:
        """Disable autonomous behavior modes."""
        return self._send_cmd("behavior_disable", {})

    # ── Advanced Actions ────────────────────────────────────────

    def follow_player(self, player_name: str) -> dict:
        """Follow a specific player."""
        return self._send_cmd("follow_player", {"player": player_name})

    def craft_item(self, item_name: str, count: int = 1) -> dict:
        """Craft an item by name."""
        return self._send_cmd("craft_item", {"item": item_name, "count": count})

    def collect_blocks(self, block_type: str, count: int = 1) -> dict:
        """Collect specified number of blocks."""
        return self._send_cmd("collect_blocks", {"block_type": block_type, "count": count})

    def explore(self, radius: int = 16) -> dict:
        """Explore the area within radius."""
        return self._send_cmd("explore", {"radius": radius})

    def trade(self, villager_type: str) -> dict:
        """Trade with a villager of specified type."""
        return self._send_cmd("trade", {"villager_type": villager_type})

    def sleep(self) -> dict:
        """Sleep in a bed."""
        return self._send_cmd("sleep", {})

    def eat(self) -> dict:
        """Eat food from inventory."""
        return self._send_cmd("eat", {})

    def drop_item(self, item: str, count: int = 1) -> dict:
        """Drop specified item."""
        return self._send_cmd("drop_item", {"item": item, "count": count})

    def sort_inventory(self) -> dict:
        """Sort inventory items."""
        return self._send_cmd("sort_inventory", {})

    def build(self, x: float, y: float, z: float, structure: str) -> dict:
        """Build a structure at position."""
        return self._send_cmd("build", {"x": x, "y": y, "z": z, "structure": structure})

    # ── Internal ────────────────────────────────────────────────

    def _send_cmd(self, cmd: str, args: dict) -> dict:
        """Send a command to the active body and return immediately."""
        if self._command_dispatcher:
            try:
                req_id = self._command_dispatcher(cmd, args, self._command_context)
            except (ConnectionError, RuntimeError, ValueError) as exc:
                logger.info("[Skills] Rejected %s: %s", cmd, exc)
                return {"success": False, "message": str(exc)}
        elif self.body:
            req_id = self.body.send_command(cmd, args)
        else:
            logger.warning("[Skills] No body, skipping %s", cmd)
            return {"success": False, "message": "No body connection"}
        if self._command_observer:
            try:
                self._command_observer(cmd, req_id, self._command_context, args)
            except Exception as exc:
                logger.debug("[Skills] Command observer failed for %s: %s", cmd, exc)
        logger.debug("[Skills] Sent: %s (req=%s)", cmd, req_id)
        return {"success": True, "message": f"{cmd} sent", "req_id": req_id}
