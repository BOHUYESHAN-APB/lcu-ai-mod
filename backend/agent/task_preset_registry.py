"""Declarative task presets resolved through the existing durable Skill executor."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any

from .skill_registry import SkillRegistry, SkillValidationError


class TaskPresetValidationError(ValueError):
    pass


PLACEHOLDER_RE = re.compile(r"\$\{parameters\.([A-Za-z_][A-Za-z0-9_]*)\}")


@dataclass(frozen=True)
class TaskPresetStep:
    key: str
    title: str
    skill_id: str
    input_template: dict[str, Any]


@dataclass(frozen=True)
class TaskPreset:
    id: str
    version: str
    title: str
    description: str
    category: str
    skill_id: str
    parameter_schema: dict[str, Any]
    input_template: dict[str, Any]
    examples: tuple[dict[str, Any], ...] = ()
    tags: tuple[str, ...] = ()
    steps: tuple[TaskPresetStep, ...] = ()
    dynamic_handler: str = ""

    def public_dict(self) -> dict[str, Any]:
        return {**asdict(self), "kind": "workflow" if self.steps or self.dynamic_handler else "skill"}


def _schema(properties: dict[str, dict[str, Any]], required: list[str] | None = None) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
        "additionalProperties": False,
    }


STRING = {"type": "string", "minLength": 1}
COUNT = {"type": "integer", "minimum": 1, "maximum": 2304, "default": 1}
COORD = {"type": "number", "minimum": -30_000_000, "maximum": 30_000_000}


BUILTIN_TASK_PRESETS = [
    TaskPreset(
        "craft.iron_pickaxe", "1.0.0", "制作铁镐", "递归获取材料并制作一把铁镐。", "crafting",
        "general.craft_item", _schema({}), {"item": "minecraft:iron_pickaxe", "count": 1},
        examples=({"name": "一把铁镐", "parameters": {}},), tags=("vanilla", "tool"),
    ),
    TaskPreset(
        "craft.item", "1.0.0", "制作物品", "按 registry ID 制作指定数量的物品。", "crafting",
        "general.craft_item", _schema({"item": STRING, "count": COUNT}, ["item", "count"]),
        {"item": "${parameters.item}", "count": "${parameters.count}"},
        examples=({"name": "火把", "parameters": {"item": "minecraft:torch", "count": 16}},),
        tags=("vanilla", "generic"),
    ),
    TaskPreset(
        "collect.logs", "1.0.0", "收集原木", "收集任意符合原木标签的木材。", "resources",
        "general.collect_blocks", _schema({"count": {**COUNT, "default": 16}}, ["count"]),
        {"block_type": "#minecraft:logs", "count": "${parameters.count}"},
        examples=({"name": "收集 16 个原木", "parameters": {"count": 16}},), tags=("vanilla", "wood"),
    ),
    TaskPreset(
        "collect.resource", "1.0.0", "收集资源", "从已知仓储、掉落物或附近方块获取资源。", "resources",
        "general.collect_blocks", _schema({"item": STRING, "count": COUNT}, ["item", "count"]),
        {"block_type": "${parameters.item}", "count": "${parameters.count}"},
        examples=({"name": "煤炭", "parameters": {"item": "minecraft:coal", "count": 8}},),
        tags=("vanilla", "generic"),
    ),
    TaskPreset(
        "navigation.coordinates", "1.0.0", "移动到坐标", "导航到指定世界坐标。", "movement",
        "core.move_to", _schema({"x": COORD, "y": COORD, "z": COORD}, ["x", "y", "z"]),
        {"x": "${parameters.x}", "y": "${parameters.y}", "z": "${parameters.z}"},
        examples=({"name": "世界出生点附近", "parameters": {"x": 0, "y": 64, "z": 0}},),
        tags=("vanilla", "movement"),
    ),
    TaskPreset(
        "survival.eat", "1.0.0", "进食", "选择热键栏中的可用食物并完成一次进食。", "survival",
        "general.eat", _schema({}), {}, examples=({"name": "进食", "parameters": {}},),
        tags=("vanilla", "survival"),
    ),
    TaskPreset(
        "workflow.starter_chest", "1.0.0", "收集木材并制作箱子",
        "先收集八个原木，再用获得的材料制作一个箱子。", "workflow", "", _schema({}), {},
        examples=({"name": "基础储物箱", "parameters": {}},), tags=("vanilla", "multi-step"),
        steps=(
            TaskPresetStep("collect_logs", "收集原木", "general.collect_blocks", {
                "block_type": "#minecraft:logs", "count": 8,
            }),
            TaskPresetStep("craft_chest", "制作箱子", "general.craft_item", {
                "item": "minecraft:chest", "count": 1,
            }),
        ),
    ),
    TaskPreset(
        "farm.region", "1.0.0", "收获农田区域",
        "扫描附近成熟作物，逐个收获并补种。", "farming", "",
        _schema({"radius": {"type": "integer", "minimum": 1, "maximum": 16}}, ["radius"]), {},
        examples=({"name": "附近农田", "parameters": {"radius": 8}},),
        tags=("vanilla", "farming", "multi-step"), dynamic_handler="farm_region",
    ),
]


class TaskPresetRegistry:
    def __init__(self, skills: SkillRegistry, presets: list[TaskPreset] | None = None):
        self.skills = skills
        items = presets or BUILTIN_TASK_PRESETS
        self._presets = {preset.id: preset for preset in items}
        if len(self._presets) != len(items):
            raise ValueError("duplicate task preset id")
        for preset in items:
            self._validate_manifest(preset)

    def list(self, category: str | None = None) -> list[dict[str, Any]]:
        skill_status = {item["id"]: item for item in self.skills.list()}
        result = []
        for preset in sorted(self._presets.values(), key=lambda item: item.id):
            if category and preset.category != category:
                continue
            public = preset.public_dict()
            if preset.dynamic_handler == "farm_region":
                skill_ids = ["world.scan_crops", "world.harvest_crop_at"]
            else:
                skill_ids = [step.skill_id for step in preset.steps] if preset.steps else [preset.skill_id]
            skills = [skill_status.get(skill_id, {}) for skill_id in skill_ids]
            unavailable = [skill for skill in skills if skill.get("available", True) is False]
            public["available"] = not unavailable
            public["availability_reason"] = "; ".join(
                dict.fromkeys(skill.get("availability_reason", "") for skill in unavailable if skill.get("availability_reason"))
            )
            public["skill"] = skills[0] if not preset.steps and not preset.dynamic_handler else None
            public["skills"] = skills
            public["step_count"] = len(preset.steps) if preset.steps else 0 if preset.dynamic_handler else 1
            public["schedulable"] = not preset.steps and not preset.dynamic_handler \
                and bool(skills[0].get("schedulable", False))
            result.append(public)
        return result

    def get(self, preset_id: str) -> TaskPreset:
        try:
            return self._presets[preset_id]
        except KeyError as exc:
            raise KeyError(f"unknown task preset: {preset_id}") from exc

    def render(self, preset_id: str, parameters: dict[str, Any]) -> dict[str, Any]:
        preset = self.get(preset_id)
        self._validate_parameters(preset.parameter_schema, parameters)
        if preset.dynamic_handler:
            return {
                "id": preset.id,
                "version": preset.version,
                "kind": "workflow",
                "dynamic_handler": preset.dynamic_handler,
                "parameters": dict(parameters),
                "steps": [],
            }
        definitions = preset.steps or (
            TaskPresetStep("run", preset.title, preset.skill_id, preset.input_template),
        )
        steps = []
        for definition in definitions:
            resolved = self._resolve(definition.input_template, parameters)
            try:
                manifest = self.skills.validate_input(definition.skill_id, resolved)
            except (KeyError, SkillValidationError) as exc:
                raise TaskPresetValidationError(str(exc)) from exc
            if not manifest.durable:
                raise TaskPresetValidationError(f"preset skill is not durable: {manifest.id}")
            steps.append({
                "key": definition.key,
                "title": definition.title,
                "skill_id": manifest.id,
                "skill_version": manifest.version,
                "completion": manifest.completion,
                "input": resolved,
            })
        return {
            "id": preset.id,
            "version": preset.version,
            "kind": "workflow" if preset.steps else "skill",
            "parameters": dict(parameters),
            "steps": steps,
        }

    def _validate_manifest(self, preset: TaskPreset) -> None:
        definitions = preset.steps or (
            TaskPresetStep("run", preset.title, preset.skill_id, preset.input_template),
        )
        if sum(bool(value) for value in (preset.skill_id, preset.steps, preset.dynamic_handler)) != 1:
            raise ValueError(f"task preset must define one skill or workflow steps: {preset.id}")
        if preset.dynamic_handler:
            if preset.dynamic_handler != "farm_region":
                raise ValueError(f"unknown dynamic task handler: {preset.dynamic_handler}")
            self.skills.get("world.scan_crops")
            harvest = self.skills.get("world.harvest_crop_at")
            if not harvest.durable:
                raise ValueError("farm_region requires durable harvest skill")
            for example in preset.examples:
                self._validate_parameters(preset.parameter_schema, example.get("parameters", {}))
            return
        keys = [step.key for step in definitions]
        if len(keys) != len(set(keys)):
            raise ValueError(f"task preset step keys must be unique: {preset.id}")
        for definition in definitions:
            manifest = self.skills.get(definition.skill_id)
            if not manifest.durable:
                raise ValueError(f"task preset requires durable skill: {definition.skill_id}")
        for example in preset.examples:
            parameters = example.get("parameters", {})
            self._validate_parameters(preset.parameter_schema, parameters)
            for definition in definitions:
                resolved = self._resolve(definition.input_template, parameters)
                self.skills.validate_input(definition.skill_id, resolved)

    @staticmethod
    def _validate_parameters(schema: dict[str, Any], parameters: dict[str, Any]) -> None:
        if not isinstance(parameters, dict):
            raise TaskPresetValidationError("parameters must be an object")
        properties = schema.get("properties", {})
        unknown = sorted(set(parameters) - set(properties))
        if unknown:
            raise TaskPresetValidationError(f"unknown parameters: {', '.join(unknown)}")
        missing = [name for name in schema.get("required", []) if name not in parameters]
        if missing:
            raise TaskPresetValidationError(f"missing parameters: {', '.join(missing)}")
        for name, value in parameters.items():
            try:
                SkillRegistry._validate_value(name, value, properties[name])
            except SkillValidationError as exc:
                raise TaskPresetValidationError(str(exc)) from exc

    @classmethod
    def _resolve(cls, value: Any, parameters: dict[str, Any]) -> Any:
        if isinstance(value, dict):
            return {key: cls._resolve(item, parameters) for key, item in value.items()}
        if isinstance(value, list):
            return [cls._resolve(item, parameters) for item in value]
        if not isinstance(value, str):
            return value
        exact = PLACEHOLDER_RE.fullmatch(value)
        if exact:
            return parameters[exact.group(1)]
        return PLACEHOLDER_RE.sub(lambda match: str(parameters[match.group(1)]), value)
