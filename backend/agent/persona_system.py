"""
Persona System — 人设系统。
管理 AI 的人格、说话风格、长期记忆。

参考 MaiBot 的 PersonInfo 和 A_memorix：
- 人格特征管理
- 说话风格学习
- 长期记忆存储
- 对话历史分析
"""

import json
import logging
import time
from pathlib import Path
from typing import Optional, Dict, List, Any
from dataclasses import dataclass, field

logger = logging.getLogger("persona_system")


@dataclass
class PersonalityTrait:
    """人格特征"""
    name: str
    value: float  # 0.0 - 1.0
    description: str


@dataclass
class SpeakingStyle:
    """说话风格"""
    formality: float = 0.3      # 正式程度 (0=口语化, 1=正式)
    verbosity: float = 0.3      # 详细程度 (0=简短, 1=详细)
    humor: float = 0.5          # 幽默程度
    emoji_usage: float = 0.2    # 表情使用频率
    slang_usage: float = 0.5    # 俚语使用频率


@dataclass
class MemoryFragment:
    """记忆片段"""
    content: str
    importance: float  # 0.0 - 1.0
    timestamp: float
    memory_type: str  # "event", "conversation", "fact", "emotion"
    related_players: List[str] = field(default_factory=list)
    tags: List[str] = field(default_factory=list)


class PersonaSystem:
    """
    人设系统 — 管理 AI 的人格和记忆。
    
    核心功能：
    1. 人格特征管理
    2. 说话风格学习
    3. 长期记忆存储
    4. 对话历史分析
    5. 情感状态追踪
    """
    
    def __init__(self, persona_path: str = "data/persona.json"):
        self.persona_path = Path(persona_path)
        self.persona_path.parent.mkdir(parents=True, exist_ok=True)
        
        # 人格特征
        self.traits: Dict[str, PersonalityTrait] = {
            "friendliness": PersonalityTrait("friendliness", 0.8, "友好程度"),
            "patience": PersonalityTrait("patience", 0.7, "耐心程度"),
            "curiosity": PersonalityTrait("curiosity", 0.6, "好奇心"),
            "helpfulness": PersonalityTrait("helpfulness", 0.9, "乐于助人"),
            "humor": PersonalityTrait("humor", 0.5, "幽默感"),
        }
        
        # 说话风格
        self.speaking_style = SpeakingStyle()
        
        # 长期记忆
        self.long_term_memories: List[MemoryFragment] = []
        self.max_memories = 1000
        
        # 情感状态
        self.emotion_state = {
            "happiness": 0.7,
            "energy": 0.8,
            "friendliness": 0.8,
        }
        
        # 玩家关系
        self.player_relationships: Dict[str, Dict] = {}
        
        # 加载数据
        self._load()
    
    def _load(self):
        """加载人设数据"""
        if not self.persona_path.exists():
            return
        
        try:
            data = json.loads(self.persona_path.read_text(encoding="utf-8"))
            
            # 加载人格特征
            if "traits" in data:
                for name, trait_data in data["traits"].items():
                    if name in self.traits:
                        self.traits[name].value = trait_data.get("value", self.traits[name].value)
            
            # 加载说话风格
            if "speaking_style" in data:
                style_data = data["speaking_style"]
                self.speaking_style.formality = style_data.get("formality", 0.3)
                self.speaking_style.verbosity = style_data.get("verbosity", 0.3)
                self.speaking_style.humor = style_data.get("humor", 0.5)
                self.speaking_style.emoji_usage = style_data.get("emoji_usage", 0.2)
                self.speaking_style.slang_usage = style_data.get("slang_usage", 0.5)
            
            # 加载长期记忆
            if "memories" in data:
                for mem_data in data["memories"]:
                    memory = MemoryFragment(
                        content=mem_data["content"],
                        importance=mem_data.get("importance", 0.5),
                        timestamp=mem_data.get("timestamp", time.time()),
                        memory_type=mem_data.get("type", "event"),
                        related_players=mem_data.get("players", []),
                        tags=mem_data.get("tags", [])
                    )
                    self.long_term_memories.append(memory)
            
            # 加载玩家关系
            if "player_relationships" in data:
                self.player_relationships = data["player_relationships"]
            
            logger.info("[Persona] Loaded persona data with %d memories", 
                       len(self.long_term_memories))
            
        except Exception as e:
            logger.warning("[Persona] Failed to load: %s", e)
    
    def save(self):
        """保存人设数据"""
        data = {
            "traits": {name: {"value": t.value, "description": t.description} 
                      for name, t in self.traits.items()},
            "speaking_style": {
                "formality": self.speaking_style.formality,
                "verbosity": self.speaking_style.verbosity,
                "humor": self.speaking_style.humor,
                "emoji_usage": self.speaking_style.emoji_usage,
                "slang_usage": self.speaking_style.slang_usage,
            },
            "memories": [
                {
                    "content": m.content,
                    "importance": m.importance,
                    "timestamp": m.timestamp,
                    "type": m.memory_type,
                    "players": m.related_players,
                    "tags": m.tags,
                }
                for m in self.long_term_memories[-self.max_memories:]
            ],
            "player_relationships": self.player_relationships,
        }
        
        self.persona_path.write_text(
            json.dumps(data, indent=2, ensure_ascii=False),
            encoding="utf-8"
        )
        logger.debug("[Persona] Saved persona data")
    
    # ── 记忆管理 ──
    
    def add_memory(self, content: str, importance: float = 0.5,
                   memory_type: str = "event",
                   related_players: Optional[List[str]] = None,
                   tags: Optional[List[str]] = None):
        """添加长期记忆"""
        memory = MemoryFragment(
            content=content,
            importance=importance,
            timestamp=time.time(),
            memory_type=memory_type,
            related_players=related_players or [],
            tags=tags or []
        )
        
        self.long_term_memories.append(memory)
        
        # 如果超过最大数量，删除最不重要的
        if len(self.long_term_memories) > self.max_memories:
            self.long_term_memories.sort(key=lambda m: m.importance)
            self.long_term_memories.pop(0)
        
        logger.info("[Persona] Added memory: %s (importance=%.2f)", 
                   content[:50], importance)
    
    def get_relevant_memories(self, context: str, limit: int = 5) -> List[MemoryFragment]:
        """获取相关记忆"""
        # 简单的关键词匹配（后续可以改为向量检索）
        context_lower = context.lower()
        
        scored_memories = []
        for memory in self.long_term_memories:
            score = 0
            
            # 内容匹配
            if any(word in memory.content.lower() for word in context_lower.split()):
                score += 1
            
            # 重要性加权
            score *= memory.importance
            
            # 时间衰减（越近越重要）
            age_hours = (time.time() - memory.timestamp) / 3600
            time_factor = 1.0 / (1.0 + age_hours * 0.1)
            score *= time_factor
            
            if score > 0:
                scored_memories.append((score, memory))
        
        # 排序并返回 top N
        scored_memories.sort(key=lambda x: x[0], reverse=True)
        return [m for _, m in scored_memories[:limit]]
    
    def get_memories_about_player(self, player_name: str) -> List[MemoryFragment]:
        """获取关于特定玩家的记忆"""
        return [m for m in self.long_term_memories 
                if player_name in m.related_players]
    
    # ── 玩家关系 ──
    
    def update_player_relationship(self, player_name: str, 
                                   interaction_type: str,
                                   sentiment: float = 0.0):
        """更新玩家关系"""
        if player_name not in self.player_relationships:
            self.player_relationships[player_name] = {
                "first_seen": time.time(),
                "interaction_count": 0,
                "sentiment": 0.0,  # -1.0 到 1.0
                "last_interaction": time.time(),
                "topics": [],
            }
        
        rel = self.player_relationships[player_name]
        rel["interaction_count"] += 1
        rel["last_interaction"] = time.time()
        
        # 更新情感（指数移动平均）
        alpha = 0.3
        rel["sentiment"] = alpha * sentiment + (1 - alpha) * rel["sentiment"]
        
        logger.debug("[Persona] Updated relationship with %s: sentiment=%.2f", 
                    player_name, rel["sentiment"])
    
    def get_player_relationship(self, player_name: str) -> Optional[Dict]:
        """获取玩家关系"""
        return self.player_relationships.get(player_name)
    
    # ── 说话风格 ──
    
    def get_style_prompt(self) -> str:
        """获取说话风格提示"""
        style = self.speaking_style
        
        prompts = []
        
        if style.formality < 0.3:
            prompts.append("说话口语化，像朋友聊天")
        elif style.formality > 0.7:
            prompts.append("说话正式一些")
        
        if style.verbosity < 0.3:
            prompts.append("回复简短，不要废话")
        elif style.verbosity > 0.7:
            prompts.append("可以详细解释")
        
        if style.humor > 0.6:
            prompts.append("适当幽默")
        
        if style.slang_usage > 0.5:
            prompts.append("可以用网络用语和俚语")
        
        return "，".join(prompts) if prompts else "说话自然随意"
    
    def update_style_from_interaction(self, message: str, response: str):
        """从交互中学习说话风格"""
        # 分析消息长度
        if len(message) < 10:
            self.speaking_style.verbosity = max(0.1, self.speaking_style.verbosity - 0.01)
        elif len(message) > 50:
            self.speaking_style.verbosity = min(0.9, self.speaking_style.verbosity + 0.01)
        
        # 分析是否使用表情
        emoji_count = sum(1 for c in message if ord(c) > 0x1F600)
        if emoji_count > 0:
            self.speaking_style.emoji_usage = min(0.8, self.speaking_style.emoji_usage + 0.05)
    
    # ── 人格特征 ──
    
    def get_trait(self, trait_name: str) -> float:
        """获取人格特征值"""
        if trait_name in self.traits:
            return self.traits[trait_name].value
        return 0.5
    
    def update_trait(self, trait_name: str, delta: float):
        """更新人格特征"""
        if trait_name in self.traits:
            trait = self.traits[trait_name]
            trait.value = max(0.0, min(1.0, trait.value + delta))
            logger.debug("[Persona] Trait %s updated to %.2f", trait_name, trait.value)
    
    # ── 情感状态 ──
    
    def update_emotion(self, event_type: str, intensity: float = 0.1):
        """更新情感状态"""
        if event_type == "positive":
            self.emotion_state["happiness"] = min(1.0, self.emotion_state["happiness"] + intensity)
        elif event_type == "negative":
            self.emotion_state["happiness"] = max(0.0, self.emotion_state["happiness"] - intensity)
        elif event_type == "tired":
            self.emotion_state["energy"] = max(0.0, self.emotion_state["energy"] - intensity)
        elif event_type == "rested":
            self.emotion_state["energy"] = min(1.0, self.emotion_state["energy"] + intensity)
    
    def get_emotion_prompt(self) -> str:
        """获取情感状态提示"""
        happiness = self.emotion_state["happiness"]
        energy = self.emotion_state["energy"]
        
        if happiness > 0.7 and energy > 0.7:
            return "心情很好，精力充沛"
        elif happiness < 0.3:
            return "心情不太好"
        elif energy < 0.3:
            return "有点累"
        return "心情平静"
    
    # ── 状态查询 ──
    
    def get_status(self) -> dict:
        """获取系统状态"""
        return {
            "traits": {name: t.value for name, t in self.traits.items()},
            "speaking_style": {
                "formality": self.speaking_style.formality,
                "verbosity": self.speaking_style.verbosity,
                "humor": self.speaking_style.humor,
            },
            "memories_count": len(self.long_term_memories),
            "player_relationships_count": len(self.player_relationships),
            "emotion_state": self.emotion_state.copy(),
        }
