"""
Memory System — 长期记忆 + 上下文管理。
参考 MaiBot 的 A_memorix 子系统。

功能：
1. 短期记忆：当前对话上下文
2. 长期记忆：重要事件、玩家互动、位置记忆
3. 人物画像：记住玩家的说话风格、偏好
4. 上下文注入：在 LLM 调用前注入相关记忆
"""

import json
import logging
import time
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("memory")


class Memory:
    """
    分层记忆系统。
    
    层级：
    1. 短期记忆（对话上下文）— 最近 N 条消息
    2. 长期记忆（事件记忆）— 重要事件持久化
    3. 人物画像（玩家档案）— 玩家的说话风格、偏好
    4. 位置记忆（命名位置）— 记住重要坐标
    """
    
    SCHEMA_VERSION = 3

    def __init__(self, path: str | Path = "data/memory.json", *,
                 server_id: str = "default", world_id: str = "default"):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.server_id = server_id
        self.world_id = world_id
        
        # 短期记忆：最近对话
        self.recent_messages: list[dict] = []
        self.max_recent = 50  # 保留最近 50 条消息
        
        # 长期记忆：重要事件
        self.events: list[dict] = []
        self.max_events = 500
        
        # 人物画像：玩家档案
        self.player_profiles: dict[str, dict] = {}
        
        # 位置记忆：命名位置
        self.locations: dict[str, dict] = {}

        # Structured durable memory
        self.player_relationships: dict[str, dict] = {}
        self.experiences: dict[str, dict] = {"servers": {}, "worlds": {}}
        self.task_outcomes: list[dict] = []
        self.max_task_outcomes = 100
        self._save_blocked_reason: str | None = None
        
        # 统计
        self.interaction_count = 0
        self.total_actions = 0
        
        # 加载持久化数据
        self._load()
    
    # ── 短期记忆 ──
    
    def add_interaction(self, sender: str, message: str, response: str = "",
                        action: str = "", success: Optional[bool] = None):
        """记录一次交互。"""
        entry = {
            "time": time.time(),
            "sender": sender,
            "message": message,
            "response": response,
            "action": action,
            "success": success,
        }
        self.recent_messages.append(entry)
        if len(self.recent_messages) > self.max_recent:
            self.recent_messages.pop(0)
        
        self.interaction_count += 1
        
        # 更新玩家画像
        if sender and sender != "system":
            self._update_player_profile(sender, message)
    
    def get_recent_context(self, n: int = 10) -> list[dict]:
        """获取最近 N 条消息。"""
        return self.recent_messages[-n:]

    def attach_response(self, sender: str, message: str, response: str) -> None:
        for entry in reversed(self.recent_messages):
            if entry.get("sender") == sender and entry.get("message") == message:
                entry["response"] = response
                return
    
    # ── 长期记忆 ──
    
    def record_event(self, event_type: str, description: str, 
                     location: Optional[dict] = None, players: Optional[list] = None,
                     metadata: Optional[dict] = None):
        """记录一个重要事件。"""
        event = {
            "time": time.time(),
            "type": event_type,
            "description": description,
            "location": location,
            "players": players or [],
            "metadata": metadata or {},
        }
        self._append_event(event)
        
        logger.info("[Memory] 记录事件: %s - %s", event_type, description[:50])
    
    def record_action(self, action: str, success: bool):
        """记录一个动作。"""
        self.total_actions += 1
        self._append_event({
            "time": time.time(),
            "type": "action",
            "description": f"{action} ({'成功' if success else '失败'})",
        })

    def _append_event(self, event: dict) -> None:
        self.events.append(event)
        if len(self.events) > self.max_events:
            del self.events[:-self.max_events]

    def observe_player(self, name: str, player_id: str = "", message: str = "") -> None:
        """Update durable relationship facts without guessing sentiment."""
        now = time.time()
        key = f"uuid:{player_id}" if player_id else f"name:{name.casefold()}"
        relationship = self.player_relationships.setdefault(key, {
            "names": [],
            "first_seen": now,
            "last_seen": now,
            "message_count": 0,
            "tasks_requested": 0,
            "task_outcomes": {"success": 0, "failed": 0, "cancelled": 0, "unknown": 0},
            "last_task": {},
        })
        if name and name not in relationship["names"]:
            relationship["names"].append(name)
        relationship["last_seen"] = now
        if message:
            relationship["message_count"] += 1

    def observe_world(self, state: dict) -> None:
        """Aggregate world experience without appending high-frequency events."""
        now = time.time()
        server = self.experiences.setdefault("servers", {}).setdefault(self.server_id, {
            "first_seen": now, "last_seen": now, "known_players": [],
        })
        server["last_seen"] = now
        known_players = set(server.get("known_players", []))
        for entity in state.get("entities", []):
            if entity.get("type") == "player" and entity.get("name"):
                known_players.add(str(entity["name"]))
        server["known_players"] = sorted(known_players)

        world_key = f"{self.server_id}\u0000{self.world_id}"
        world = self.experiences.setdefault("worlds", {}).setdefault(world_key, {
            "first_seen": now, "last_seen": now, "dimensions": {}, "last_position": {},
            "deaths": 0, "task_stats": {},
        })
        world["last_seen"] = now
        player = state.get("player", {})
        dimension = str(player.get("dimension", "unknown"))
        world.setdefault("dimensions", {})[dimension] = now
        if all(key in player for key in ("x", "y", "z")):
            world["last_position"] = {
                "x": player["x"], "y": player["y"], "z": player["z"], "dimension": dimension,
            }

    def record_task_outcome(self, command: str, outcome: str, *, target: str = "",
                            requester: str = "", requester_id: str = "", detail: str = "",
                            duration: float = 0.0) -> None:
        if outcome not in {"success", "failed", "cancelled", "unknown"}:
            raise ValueError("invalid task outcome")
        now = time.time()
        entry = {
            "time": now,
            "command": command,
            "target": target,
            "outcome": outcome,
            "requester": requester,
            "detail": detail,
            "duration": round(max(0.0, duration), 2),
            "server_id": self.server_id,
            "world_id": self.world_id,
        }
        self.task_outcomes.append(entry)
        if len(self.task_outcomes) > self.max_task_outcomes:
            del self.task_outcomes[:-self.max_task_outcomes]
        self.record_event(
            "task_outcome",
            f"{command} {target} -> {outcome}".strip(),
            players=[requester] if requester else [],
            metadata=entry,
        )
        world_key = f"{self.server_id}\u0000{self.world_id}"
        world = self.experiences.setdefault("worlds", {}).setdefault(world_key, {
            "first_seen": now, "last_seen": now, "dimensions": {}, "last_position": {},
            "deaths": 0, "task_stats": {},
        })
        stats = world.setdefault("task_stats", {}).setdefault(command, {
            "success": 0, "failed": 0, "cancelled": 0, "unknown": 0,
        })
        stats[outcome] += 1
        if requester:
            self.observe_player(requester, requester_id)
            key = f"uuid:{requester_id}" if requester_id else f"name:{requester.casefold()}"
            relationship = self.player_relationships[key]
            relationship["tasks_requested"] += 1
            relationship["task_outcomes"][outcome] += 1
            relationship["last_task"] = entry

    def record_death(self, description: str = "玩家死亡") -> None:
        world_key = f"{self.server_id}\u0000{self.world_id}"
        now = time.time()
        world = self.experiences.setdefault("worlds", {}).setdefault(world_key, {
            "first_seen": now, "last_seen": now, "dimensions": {}, "last_position": {},
            "deaths": 0, "task_stats": {},
        })
        world["deaths"] = int(world.get("deaths", 0)) + 1
        self.record_event("death", description)
    
    # ── 人物画像 ──
    
    def _update_player_profile(self, sender: str, message: str):
        """更新玩家画像。"""
        if sender not in self.player_profiles:
            self.player_profiles[sender] = {
                "first_seen": time.time(),
                "message_count": 0,
                "avg_message_length": 0,
                "common_words": {},
                "last_active": time.time(),
            }
        
        profile = self.player_profiles[sender]
        profile["message_count"] += 1
        profile["last_active"] = time.time()
        
        # 更新平均消息长度
        msg_len = len(message)
        profile["avg_message_length"] = (
            (profile["avg_message_length"] * (profile["message_count"] - 1) + msg_len) 
            / profile["message_count"]
        )
        
        # 更新常用词（简单实现）
        words = message.lower().split()
        for word in words:
            if len(word) > 1:
                profile["common_words"][word] = profile["common_words"].get(word, 0) + 1
    
    def get_player_profile(self, sender: str) -> Optional[dict]:
        """获取玩家画像。"""
        return self.player_profiles.get(sender)
    
    # ── 位置记忆 ──
    
    def save_location(self, name: str, x: float, y: float, z: float, 
                      dimension: str = "overworld", description: str = ""):
        """保存一个命名位置。"""
        self.locations[name] = {
            "x": x, "y": y, "z": z,
            "dimension": dimension,
            "description": description,
            "saved_at": time.time(),
        }
        logger.info("[Memory] 保存位置: %s (%.0f, %.0f, %.0f)", name, x, y, z)
    
    def get_location(self, name: str) -> Optional[dict]:
        """获取命名位置。"""
        return self.locations.get(name)
    
    # ── 上下文构建 ──
    
    def build_context(self, current_player: str | None = None, max_chars: int = 4000) -> dict:
        """Build deterministic, bounded context for LLM prompts."""
        sections = {
            "relationship_summary": self._build_relationship_summary(current_player),
            "task_outcomes": self._build_task_outcomes_summary(),
            "world_experience": self._build_world_experience_summary(),
            "interaction_summary": self._build_interaction_summary(),
            "recent_events": self._build_recent_events(),
            "player_profiles": self._build_player_profiles_summary(),
            "locations": self._build_locations_summary(),
            "action_insights": self._build_action_insights(),
        }
        context: dict[str, str] = {}
        remaining = max(0, max_chars)
        for key, value in sections.items():
            if remaining <= 0:
                context[key] = ""
                continue
            compact = "\n".join(line.strip() for line in str(value).splitlines() if line.strip())
            context[key] = compact[:remaining]
            remaining -= len(context[key])
        return context

    def _build_relationship_summary(self, current_player: str | None) -> str:
        relationships = list(self.player_relationships.values())
        relationships.sort(
            key=lambda item: (
                0 if current_player and current_player in item.get("names", []) else 1,
                -float(item.get("last_seen", 0)),
                ",".join(item.get("names", [])),
            )
        )
        lines = []
        for item in relationships[:8]:
            names = item.get("names") or ["unknown"]
            name = names[-1]
            outcomes = item.get("task_outcomes", {})
            lines.append(
                f"{name}: messages={item.get('message_count', 0)}, tasks={item.get('tasks_requested', 0)}, "
                f"success={outcomes.get('success', 0)}, failed={outcomes.get('failed', 0)}"
            )
        return "\n".join(lines) or "暂无玩家关系"

    def _build_task_outcomes_summary(self) -> str:
        lines = []
        for item in self.task_outcomes[-5:]:
            target = f" {item.get('target')}" if item.get("target") else ""
            detail = f": {item.get('detail')}" if item.get("detail") else ""
            lines.append(f"{item.get('command')}{target} -> {item.get('outcome')}{detail}")
        return "\n".join(lines) or "暂无任务结果"

    def _build_world_experience_summary(self) -> str:
        world_key = f"{self.server_id}\u0000{self.world_id}"
        world = self.experiences.get("worlds", {}).get(world_key, {})
        server = self.experiences.get("servers", {}).get(self.server_id, {})
        position = world.get("last_position", {})
        stats = world.get("task_stats", {})
        return (
            f"server={self.server_id}, world={self.world_id}, known_players={server.get('known_players', [])}, "
            f"last_position={position}, task_stats={stats}"
        )
    
    def _build_interaction_summary(self) -> str:
        """构建最近交互摘要。"""
        if not self.recent_messages:
            return "暂无对话记录"
        
        lines = []
        for msg in self.recent_messages[-5:]:
            sender = msg.get("sender", "?")
            message = msg.get("message", "")
            response = msg.get("response", "")
            if response:
                lines.append(f"{sender}: {message}")
                lines.append(f"AI: {response}")
            else:
                lines.append(f"{sender}: {message}")
        return "\n".join(lines)
    
    def _build_recent_events(self) -> str:
        """构建最近事件摘要。"""
        if not self.events:
            return "暂无重要事件"
        
        lines = []
        for event in self.events[-5:]:
            event_type = event.get("type", "?")
            desc = event.get("description", "")
            lines.append(f"[{event_type}] {desc}")
        return "\n".join(lines)
    
    def _build_player_profiles_summary(self) -> str:
        """构建玩家画像摘要。"""
        if not self.player_profiles:
            return "暂无玩家画像"
        
        lines = []
        for name, profile in self.player_profiles.items():
            msg_count = profile.get("message_count", 0)
            avg_len = profile.get("avg_message_length", 0)
            lines.append(f"{name}: 发送了 {msg_count} 条消息，平均长度 {avg_len:.0f} 字")
        return "\n".join(lines)
    
    def _build_locations_summary(self) -> str:
        """构建位置记忆摘要。"""
        if not self.locations:
            return "暂无保存的位置"
        
        lines = []
        for name, loc in self.locations.items():
            x, y, z = loc.get("x", 0), loc.get("y", 0), loc.get("z", 0)
            desc = loc.get("description", "")
            lines.append(f"{name}: ({x:.0f}, {y:.0f}, {z:.0f}) {desc}")
        return "\n".join(lines)
    
    def _build_action_insights(self) -> str:
        """构建动作洞察。"""
        if self.total_actions == 0:
            return "暂无动作记录"
        
        success_count = sum(1 for e in self.events 
                           if e.get("type") == "action" and "成功" in e.get("description", ""))
        success_rate = success_count / max(1, self.total_actions) * 100
        return f"共执行 {self.total_actions} 个动作，成功率 {success_rate:.0f}%"
    
    # ── 持久化 ──
    
    def _load(self):
        """加载持久化数据。"""
        if not self.path.exists():
            return
        
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("memory root must be an object")
            version = int(data.get("schema_version", 1))
            if version > self.SCHEMA_VERSION:
                self._save_blocked_reason = f"future schema {version}"
                logger.warning("[Memory] 跳过未来版本 schema=%d", version)
                return
            recent_messages = data.get("recent_messages", [])
            events = data.get("events", [])
            player_profiles = data.get("player_profiles", {})
            locations = data.get("locations", {})
            player_relationships = data.get("player_relationships", {})
            experiences = data.get("experiences", {"servers": {}, "worlds": {}})
            task_outcomes = data.get("task_outcomes", [])
            if not isinstance(recent_messages, list) or not isinstance(events, list) or not isinstance(task_outcomes, list):
                raise ValueError("memory list fields are invalid")
            if not all(isinstance(item, dict) for item in (player_profiles, locations, player_relationships, experiences)):
                raise ValueError("memory object fields are invalid")
            servers = experiences.get("servers", {})
            worlds = experiences.get("worlds", {})
            if not isinstance(servers, dict) or not isinstance(worlds, dict):
                raise ValueError("experience fields are invalid")
            experiences["servers"] = servers
            experiences["worlds"] = worlds
            self.recent_messages = recent_messages
            self.events = events
            self.player_profiles = player_profiles
            self.locations = locations
            self.player_relationships = player_relationships
            self.experiences = experiences
            self.task_outcomes = task_outcomes[-self.max_task_outcomes:]
            self.recent_messages = self.recent_messages[-self.max_recent:]
            self.events = self.events[-self.max_events:]
            self.interaction_count = data.get("interaction_count", 0)
            self.total_actions = data.get("total_actions", 0)
            logger.info("[Memory] 加载了 %d 条消息、%d 个事件、%d 个玩家画像",
                       len(self.recent_messages), len(self.events), len(self.player_profiles))
        except Exception as e:
            self._save_blocked_reason = str(e)
            logger.warning("[Memory] 加载失败: %s", e)
    
    def save(self):
        """保存持久化数据。"""
        if self._save_blocked_reason:
            logger.error("[Memory] 为保护原文件跳过保存: %s", self._save_blocked_reason)
            return
        data = {
            "schema_version": self.SCHEMA_VERSION,
            "recent_messages": self.recent_messages,
            "events": self.events,
            "player_profiles": self.player_profiles,
            "locations": self.locations,
            "player_relationships": self.player_relationships,
            "experiences": self.experiences,
            "task_outcomes": self.task_outcomes,
            "interaction_count": self.interaction_count,
            "total_actions": self.total_actions,
        }
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        temporary.replace(self.path)
        logger.debug("[Memory] 保存了 %d 条消息、%d 个事件", 
                    len(self.recent_messages), len(self.events))
