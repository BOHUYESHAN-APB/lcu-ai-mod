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
    
    def __init__(self, path: str | Path = "data/memory.json"):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        
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
    
    # ── 长期记忆 ──
    
    def record_event(self, event_type: str, description: str, 
                     location: Optional[dict] = None, players: Optional[list] = None):
        """记录一个重要事件。"""
        event = {
            "time": time.time(),
            "type": event_type,
            "description": description,
            "location": location,
            "players": players or [],
        }
        self.events.append(event)
        if len(self.events) > self.max_events:
            self.events.pop(0)
        
        logger.info("[Memory] 记录事件: %s - %s", event_type, description[:50])
    
    def record_action(self, action: str, success: bool):
        """记录一个动作。"""
        self.total_actions += 1
        self.events.append({
            "time": time.time(),
            "type": "action",
            "description": f"{action} ({'成功' if success else '失败'})",
        })
    
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
    
    def build_context(self) -> dict:
        """构建 LLM 上下文。"""
        context = {
            "interaction_summary": self._build_interaction_summary(),
            "recent_events": self._build_recent_events(),
            "player_profiles": self._build_player_profiles_summary(),
            "locations": self._build_locations_summary(),
            "action_insights": self._build_action_insights(),
        }
        return context
    
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
            self.recent_messages = data.get("recent_messages", [])
            self.events = data.get("events", [])
            self.player_profiles = data.get("player_profiles", {})
            self.locations = data.get("locations", {})
            self.interaction_count = data.get("interaction_count", 0)
            self.total_actions = data.get("total_actions", 0)
            logger.info("[Memory] 加载了 %d 条消息、%d 个事件、%d 个玩家画像",
                       len(self.recent_messages), len(self.events), len(self.player_profiles))
        except Exception as e:
            logger.warning("[Memory] 加载失败: %s", e)
    
    def save(self):
        """保存持久化数据。"""
        data = {
            "recent_messages": self.recent_messages,
            "events": self.events,
            "player_profiles": self.player_profiles,
            "locations": self.locations,
            "interaction_count": self.interaction_count,
            "total_actions": self.total_actions,
        }
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        temporary.replace(self.path)
        logger.debug("[Memory] 保存了 %d 条消息、%d 个事件", 
                    len(self.recent_messages), len(self.events))
