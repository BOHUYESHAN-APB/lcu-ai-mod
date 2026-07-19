"""Typed manifests for built-in and contributed companion skills."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from typing import Any


class SkillValidationError(ValueError):
    pass


@dataclass(frozen=True)
class SkillManifest:
    id: str
    version: str
    category: str
    command: str
    description: str
    input_schema: dict[str, Any]
    source: str = "builtin"
    safety_class: str = "standard"
    duration: str = "immediate"
    cancellable: bool = False
    completion: str = "response"
    schedulable: bool = True
    durable: bool = True
    executor: str = "deterministic"
    offline: bool = True
    requires: tuple[str, ...] = ()
    effects: tuple[str, ...] = ()

    def public_dict(self) -> dict[str, Any]:
        return asdict(self)


def _object_schema(properties: dict[str, dict[str, Any]], required: list[str] | None = None) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
        "additionalProperties": False,
    }


NUMBER = {"type": "number", "minimum": -30_000_000, "maximum": 30_000_000}
INTEGER = {"type": "integer"}
STRING = {"type": "string", "minLength": 1}
BLOCK_COORD = {"type": "integer", "minimum": -30_000_000, "maximum": 30_000_000}
FACE = {"type": "string", "enum": ["down", "up", "north", "south", "west", "east"]}


BUILTIN_SKILLS = [
    SkillManifest("core.move_to", "2.0.0", "core", "move_to", "Move to world coordinates.",
                  _object_schema({"x": NUMBER, "y": NUMBER, "z": NUMBER}, ["x", "y", "z"]),
                  duration="long_running", cancellable=True, completion="outcome", effects=("body.move",)),
    SkillManifest("core.look_at", "1.0.0", "core", "look_at", "Look at world coordinates.",
                  _object_schema({"x": NUMBER, "y": NUMBER, "z": NUMBER}, ["x", "y", "z"]), effects=("camera.move",)),
    SkillManifest("core.look_at_entity", "1.0.0", "core", "look_at_entity", "Look at a specific entity ID.",
                  _object_schema({"id": INTEGER}, ["id"]), schedulable=False, effects=("camera.move",)),
    SkillManifest("core.jump", "1.0.0", "core", "jump", "Jump once.", _object_schema({}), effects=("body.move",)),
    SkillManifest("core.attack", "1.0.0", "core", "attack", "Attack the targeted entity.", _object_schema({}), safety_class="combat", schedulable=False, effects=("entity.attack",)),
    SkillManifest("core.attack_entity", "1.0.0", "core", "attack_entity", "Attack a specific entity ID.",
                  _object_schema({"entity_id": INTEGER}, ["entity_id"]), safety_class="combat", schedulable=False, effects=("entity.attack",)),
    SkillManifest("core.mine_block", "2.0.0", "core", "mine_block", "Mine the targeted block.", _object_schema({}), duration="long_running", cancellable=True, completion="outcome", schedulable=False, durable=False),
    SkillManifest("core.mine_block_at", "1.0.0", "core", "mine_block_at", "Mine a specific block position.",
                  _object_schema({"x": BLOCK_COORD, "y": BLOCK_COORD, "z": BLOCK_COORD, "face": FACE}, ["x", "y", "z"]),
                  duration="long_running", cancellable=True, completion="outcome", schedulable=False, durable=False, effects=("world.break",)),
    SkillManifest("core.use_on", "1.0.0", "core", "use_on", "Use the held item on the current target.", _object_schema({}), schedulable=False, effects=("world.interact",)),
    SkillManifest("core.use_item", "1.0.0", "core", "use_item", "Use the selected main-hand item.", _object_schema({}), schedulable=False, effects=("inventory.use",)),
    SkillManifest("core.use_on_entity", "1.0.0", "core", "use_on_entity", "Interact with a specific entity ID.",
                  _object_schema({"id": INTEGER}, ["id"]), schedulable=False, effects=("entity.interact",)),
    SkillManifest("core.interact_block_at", "1.0.0", "core", "interact_block_at", "Interact with a specific block position.",
                  _object_schema({"x": BLOCK_COORD, "y": BLOCK_COORD, "z": BLOCK_COORD, "face": FACE}, ["x", "y", "z"]), schedulable=False, effects=("world.interact",)),
    SkillManifest("inventory.equip_item", "1.0.0", "inventory", "equip_item", "Select a matching hotbar item in the main hand.",
                  _object_schema({"item": STRING}, ["item"]), schedulable=False, effects=("inventory.equip",)),
    SkillManifest("core.send_chat", "1.0.0", "core", "send_chat", "Send a Minecraft chat message.",
                  _object_schema({"message": STRING}, ["message"]), safety_class="social", schedulable=False, effects=("chat.send",)),
    SkillManifest("core.stop", "1.0.0", "core", "stop_all", "Stop current movement and actions.", _object_schema({}), safety_class="safety", schedulable=False),
    SkillManifest("general.follow_player", "2.0.0", "general", "follow_player", "Follow a named player.",
                  _object_schema({"player": STRING}, ["player"]), duration="long_running", cancellable=True, completion="outcome", schedulable=False, durable=False),
    SkillManifest("general.collect_blocks", "2.0.0", "general", "collect_blocks", "Collect blocks by registry ID.",
                  _object_schema({"block_type": STRING, "count": {"type": "integer", "minimum": 1, "maximum": 2304}}, ["block_type", "count"]),
                  safety_class="resource_mutation", duration="long_running", cancellable=True, completion="outcome",
                  requires=("inventory.read", "world.collect",), effects=("inventory.produce", "world.break")),
    SkillManifest("general.craft_item", "2.0.0", "general", "craft_item", "Craft an item and resolve supported recipe dependencies locally.",
                  _object_schema({"item": STRING, "count": {"type": "integer", "minimum": 1, "maximum": 2304}}, ["item", "count"]),
                  safety_class="resource_mutation", duration="long_running", cancellable=True, completion="outcome",
                  requires=("inventory.read", "recipes.query", "recipe.execute"),
                  effects=("inventory.consume", "inventory.produce", "world.interact")),
    SkillManifest("general.explore", "1.0.0", "general", "explore", "Explore within a radius.",
                  _object_schema({"radius": {"type": "integer", "minimum": 1, "maximum": 256}}, ["radius"]),
                  duration="long_running", cancellable=True, schedulable=False, durable=False),
    SkillManifest("general.eat", "2.0.0", "general", "eat", "Eat suitable food from inventory.", _object_schema({}), duration="long_running", cancellable=True, completion="outcome", effects=("inventory.consume",)),
    SkillManifest("inventory.get_container", "2.0.0", "inventory", "get_container", "Read the currently open container and distinguish storage from player slots.",
                  _object_schema({}), schedulable=False, durable=False, effects=("inventory.read",)),
    SkillManifest("inventory.get_state", "1.0.0", "inventory", "get_state", "Read immediate player and body state.",
                  _object_schema({}), schedulable=False, durable=False, effects=("state.read",)),
    SkillManifest("inventory.get_recipes", "1.0.0", "inventory", "get_recipes", "List live recipe alternatives for an item or item tag.",
                  _object_schema({"item": STRING}, ["item"]), schedulable=False, durable=False, effects=("recipes.read",)),
    SkillManifest("inventory.select_hotbar", "1.0.0", "inventory", "select_hotbar", "Select a hotbar index.",
                  _object_schema({"index": {"type": "integer", "minimum": 0, "maximum": 8}}, ["index"]), schedulable=False, effects=("inventory.equip",)),
    SkillManifest("inventory.click", "2.0.0", "inventory", "inventory_click", "Send a validated vanilla inventory click.",
                  _object_schema({"container_id": INTEGER, "expected_state_id": INTEGER, "slot": INTEGER, "button": INTEGER, "click_type": STRING}, ["container_id", "expected_state_id", "slot", "click_type"]),
                  safety_class="resource_mutation", schedulable=False, durable=False, effects=("inventory.transfer",)),
    SkillManifest("inventory.container_button", "2.0.0", "inventory", "container_button", "Select a button or indexed option in the current menu.",
                  _object_schema({"container_id": INTEGER, "expected_state_id": INTEGER, "button_id": INTEGER}, ["container_id", "expected_state_id", "button_id"]),
                  safety_class="resource_mutation", schedulable=False, durable=False, effects=("inventory.ui",)),
    SkillManifest("inventory.place_recipe", "2.0.0", "inventory", "place_recipe", "Place a known recipe through the current recipe-book menu.",
                  _object_schema({"container_id": INTEGER, "expected_state_id": INTEGER, "recipe_id": STRING, "craft_all": {"type": "boolean"}}, ["container_id", "expected_state_id", "recipe_id"]),
                  safety_class="resource_mutation", schedulable=False, durable=False, effects=("inventory.ui", "inventory.transfer")),
    SkillManifest("inventory.take_item", "2.0.0", "inventory", "take_item", "Transfer one storage slot into player inventory.",
                  _object_schema({"container_id": INTEGER, "expected_state_id": INTEGER, "slot": INTEGER}, ["container_id", "expected_state_id", "slot"]),
                  safety_class="resource_mutation", schedulable=False, durable=False, effects=("inventory.transfer",)),
    SkillManifest("inventory.put_item", "2.0.0", "inventory", "put_item", "Transfer one player slot into the open container.",
                  _object_schema({"container_id": INTEGER, "expected_state_id": INTEGER, "slot": INTEGER}, ["container_id", "expected_state_id", "slot"]),
                  safety_class="resource_mutation", schedulable=False, durable=False, effects=("inventory.transfer",)),
    SkillManifest("inventory.close_container", "1.0.0", "inventory", "close_container", "Close the currently open container.",
                  _object_schema({}), schedulable=False, durable=False, effects=("inventory.ui",)),
    SkillManifest("inventory.drop_item", "1.0.0", "inventory", "drop_item", "Drop an item by registry ID.",
                  _object_schema({"item": STRING, "count": {"type": "integer", "minimum": 1, "maximum": 2304}}, ["item", "count"]),
                  safety_class="resource_mutation", schedulable=False, durable=False, effects=("inventory.drop",)),
]


class SkillRegistry:
    def __init__(self, manifests: list[SkillManifest] | None = None):
        items = manifests or BUILTIN_SKILLS
        self._skills = {item.id: item for item in items}
        self._body_tools: dict[str, dict[str, Any]] | None = None
        if len(self._skills) != len(items):
            raise ValueError("duplicate skill id")

    def list(self, category: str | None = None) -> list[dict[str, Any]]:
        items = self._skills.values()
        body_tools = self._body_tools
        if category:
            items = [item for item in items if item.category == category]
        result = []
        for item in sorted(items, key=lambda item: item.id):
            public = item.public_dict()
            if body_tools is not None:
                body_tool = body_tools.get(item.command)
                public["available"] = self._body_tool_compatible(item, body_tool)
                public["availability_reason"] = "" if public["available"] else "body capability is missing or incompatible"
                public["body_contract"] = body_tool
            result.append(public)
        return result

    def set_body_tools(self, tools: list[dict[str, Any]] | None) -> None:
        if tools is None:
            self._body_tools = None
            return
        self._body_tools = {
            str(tool.get("command")): dict(tool)
            for tool in tools
            if isinstance(tool, dict) and tool.get("command")
        }

    @property
    def revision(self) -> str:
        encoded = json.dumps(self.list(), ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("ascii")).hexdigest()[:16]

    def get(self, skill_id: str) -> SkillManifest:
        try:
            return self._skills[skill_id]
        except KeyError as exc:
            raise KeyError(f"unknown skill: {skill_id}") from exc

    def validate_input(self, skill_id: str, payload: dict[str, Any]) -> SkillManifest:
        manifest = self.get(skill_id)
        body_tools = self._body_tools
        if body_tools is not None:
            body_tool = body_tools.get(manifest.command)
            if not self._body_tool_compatible(manifest, body_tool):
                raise SkillValidationError(f"body capability unavailable: {manifest.command}")
        schema = manifest.input_schema
        properties = schema["properties"]
        unknown = sorted(set(payload) - set(properties))
        if unknown:
            raise SkillValidationError(f"unknown fields: {', '.join(unknown)}")
        missing = [name for name in schema.get("required", []) if name not in payload]
        if missing:
            raise SkillValidationError(f"missing fields: {', '.join(missing)}")
        for name, value in payload.items():
            self._validate_value(name, value, properties[name])
        return manifest

    @staticmethod
    def _body_tool_compatible(manifest: SkillManifest, body_tool: dict[str, Any] | None) -> bool:
        if not body_tool or body_tool.get("available") is False:
            return False
        body_version = body_tool.get("version")
        if body_version is not None and str(body_version) != manifest.version:
            return False
        completion = body_tool.get("completion")
        if completion is not None and str(completion) != manifest.completion:
            return False
        return True

    @staticmethod
    def _validate_value(name: str, value: Any, schema: dict[str, Any]) -> None:
        expected = schema["type"]
        valid = (
            (expected == "string" and isinstance(value, str))
            or (expected == "integer" and isinstance(value, int) and not isinstance(value, bool))
            or (expected == "number" and isinstance(value, (int, float)) and not isinstance(value, bool))
            or (expected == "boolean" and isinstance(value, bool))
        )
        if not valid:
            raise SkillValidationError(f"{name} must be {expected}")
        if expected == "string" and len(value) < schema.get("minLength", 0):
            raise SkillValidationError(f"{name} is too short")
        if "enum" in schema and value not in schema["enum"]:
            raise SkillValidationError(f"{name} must be one of: {', '.join(schema['enum'])}")
        if expected in {"integer", "number"}:
            if isinstance(value, float) and not math.isfinite(value):
                raise SkillValidationError(f"{name} must be finite")
            if "minimum" in schema and value < schema["minimum"]:
                raise SkillValidationError(f"{name} must be >= {schema['minimum']}")
            if "maximum" in schema and value > schema["maximum"]:
                raise SkillValidationError(f"{name} must be <= {schema['maximum']}")
