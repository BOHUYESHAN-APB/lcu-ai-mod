"""
User Profile System — 用户画像系统。
记录和分析玩家的行为模式、偏好、习惯。

参考 MaiBot 的 PersonInfo 和 TouhouLittleMaid 的好感度系统：
- 玩家行为分析
- 偏好学习
- 交互历史
- 好感度系统
"""

import json
import logging
import time
from pathlib import Path
from typing import Optional, Dict, List, Any
from dataclasses import dataclass, field
from collections import Counter

logger = logging.getLogger("user_profile")


@dataclass
class InteractionRecord:
    """交互记录"""
    timestamp: float
    interaction_type: str  # "chat", "trade", "help", "combat", etc.
    content: str
    sentiment: float  # -1.0 到 1.0


@dataclass
class PlayerProfile:
    """玩家画像"""
    player_id: str
    player_name: str
    first_seen: float
    last_seen: float
    
    # 基本信息
    message_count: int = 0
    avg_message_length: float = 0.0
    
    # 行为模式
    active_hours: List[int] = field(default_factory=list)  # 活跃时段
    common_words: Dict[str, int] = field(default_factory=dict)
    topics: Dict[str, int] = field(default_factory=dict)
    
    # 偏好
    preferred_activities: List[str] = field(default_factory=list)
    play_style: str = "casual"  # casual, builder, explorer, fighter, etc.
    
    # 关系
    relationship_score: float = 0.0  # -1.0 到 1.0
    interaction_history: List[InteractionRecord] = field(default_factory=list)
    
    # 好感度系统（参考 TouhouLittleMaid）
    favorability: float = 0.0  # 0-100
    favorability_level: int = 0  # 0-4 级


class UserProfileSystem:
    """
    用户画像系统 — 管理所有玩家的画像。
    
    核心功能：
    1. 玩家行为分析
    2. 偏好学习
    3. 交互历史记录
    4. 好感度管理
    5. 个性化响应
    """
    
    def __init__(self, profiles_path: str = "data/user_profiles.json"):
        self.profiles_path = Path(profiles_path)
        self.profiles_path.parent.mkdir(parents=True, exist_ok=True)
        
        # 玩家画像
        self.profiles: Dict[str, PlayerProfile] = {}
        
        # 加载数据
        self._load()
    
    def _load(self):
        """加载玩家画像"""
        if not self.profiles_path.exists():
            return
        
        try:
            data = json.loads(self.profiles_path.read_text(encoding="utf-8"))
            
            for player_id, profile_data in data.items():
                profile = PlayerProfile(
                    player_id=player_id,
                    player_name=profile_data.get("player_name", ""),
                    first_seen=profile_data.get("first_seen", time.time()),
                    last_seen=profile_data.get("last_seen", time.time()),
                    message_count=profile_data.get("message_count", 0),
                    avg_message_length=profile_data.get("avg_message_length", 0.0),
                    active_hours=profile_data.get("active_hours", []),
                    common_words=profile_data.get("common_words", {}),
                    topics=profile_data.get("topics", {}),
                    preferred_activities=profile_data.get("preferred_activities", []),
                    play_style=profile_data.get("play_style", "casual"),
                    relationship_score=profile_data.get("relationship_score", 0.0),
                    favorability=profile_data.get("favorability", 0.0),
                    favorability_level=profile_data.get("favorability_level", 0),
                )
                self.profiles[player_id] = profile
            
            logger.info("[UserProfile] Loaded %d player profiles", len(self.profiles))
            
        except Exception as e:
            logger.warning("[UserProfile] Failed to load: %s", e)
    
    def save(self):
        """保存玩家画像"""
        data = {}
        
        for player_id, profile in self.profiles.items():
            # 获取常用词 top 50
            common_words_sorted = sorted(
                profile.common_words.items(), 
                key=lambda x: x[1], 
                reverse=True
            )[:50]
            
            data[player_id] = {
                "player_name": profile.player_name,
                "first_seen": profile.first_seen,
                "last_seen": profile.last_seen,
                "message_count": profile.message_count,
                "avg_message_length": profile.avg_message_length,
                "active_hours": profile.active_hours,
                "common_words": dict(common_words_sorted),
                "topics": profile.topics,
                "preferred_activities": profile.preferred_activities,
                "play_style": profile.play_style,
                "relationship_score": profile.relationship_score,
                "favorability": profile.favorability,
                "favorability_level": profile.favorability_level,
            }
        
        self.profiles_path.write_text(
            json.dumps(data, indent=2, ensure_ascii=False),
            encoding="utf-8"
        )
        logger.debug("[UserProfile] Saved %d player profiles", len(self.profiles))
    
    # ── 玩家管理 ──
    
    def get_or_create_profile(self, player_name: str) -> PlayerProfile:
        """获取或创建玩家画像"""
        player_id = player_name.lower()
        
        if player_id not in self.profiles:
            profile = PlayerProfile(
                player_id=player_id,
                player_name=player_name,
                first_seen=time.time(),
                last_seen=time.time()
            )
            self.profiles[player_id] = profile
            logger.info("[UserProfile] Created profile for %s", player_name)
        
        return self.profiles[player_id]
    
    def get_profile(self, player_name: str) -> Optional[PlayerProfile]:
        """获取玩家画像"""
        player_id = player_name.lower()
        return self.profiles.get(player_id)
    
    # ── 交互记录 ──
    
    def record_interaction(self, player_name: str, 
                          interaction_type: str,
                          content: str,
                          sentiment: float = 0.0):
        """记录交互"""
        profile = self.get_or_create_profile(player_name)
        
        # 更新基本统计
        profile.last_seen = time.time()
        profile.message_count += 1
        
        # 更新平均消息长度
        msg_len = len(content)
        profile.avg_message_length = (
            (profile.avg_message_length * (profile.message_count - 1) + msg_len) 
            / profile.message_count
        )
        
        # 更新活跃时段
        current_hour = time.localtime().tm_hour
        if current_hour not in profile.active_hours:
            profile.active_hours.append(current_hour)
            # 只保留最近 24 个时段
            if len(profile.active_hours) > 24:
                profile.active_hours.pop(0)
        
        # 更新常用词
        words = content.lower().split()
        for word in words:
            if len(word) > 1:
                profile.common_words[word] = profile.common_words.get(word, 0) + 1
        
        # 添加交互记录
        record = InteractionRecord(
            timestamp=time.time(),
            interaction_type=interaction_type,
            content=content,
            sentiment=sentiment
        )
        profile.interaction_history.append(record)
        
        # 只保留最近 100 条记录
        if len(profile.interaction_history) > 100:
            profile.interaction_history.pop(0)
        
        # 更新关系分数
        alpha = 0.1
        profile.relationship_score = (
            alpha * sentiment + (1 - alpha) * profile.relationship_score
        )
        
        logger.debug("[UserProfile] Recorded interaction with %s: %s", 
                    player_name, interaction_type)
    
    # ── 好感度系统 ──
    
    def update_favorability(self, player_name: str, delta: float, reason: str = ""):
        """更新好感度（参考 TouhouLittleMaid）"""
        profile = self.get_or_create_profile(player_name)
        
        # 更新好感度
        profile.favorability = max(0, min(100, profile.favorability + delta))
        
        # 计算好感度等级
        # 0-20: 陌生, 20-40: 认识, 40-60: 熟悉, 60-80: 友好, 80-100: 亲密
        if profile.favorability < 20:
            profile.favorability_level = 0
        elif profile.favorability < 40:
            profile.favorability_level = 1
        elif profile.favorability < 60:
            profile.favorability_level = 2
        elif profile.favorability < 80:
            profile.favorability_level = 3
        else:
            profile.favorability_level = 4
        
        logger.info("[UserProfile] %s favorability: %.1f (+%.1f) Level %d [%s]",
                   player_name, profile.favorability, delta, 
                   profile.favorability_level, reason)
    
    def get_favorability_level_name(self, level: int) -> str:
        """获取好感度等级名称"""
        names = ["陌生", "认识", "熟悉", "友好", "亲密"]
        return names[min(level, len(names) - 1)]
    
    # ── 行为分析 ──
    
    def analyze_play_style(self, player_name: str) -> str:
        """分析玩家游戏风格"""
        profile = self.get_profile(player_name)
        if not profile:
            return "unknown"
        
        # 基于常用词分析
        words = profile.common_words
        
        # 建造相关词汇
        build_words = {"build", "place", "block", "house", "wall", "floor", "建造", "放置", "方块", "房子"}
        build_score = sum(words.get(w, 0) for w in build_words)
        
        # 探索相关词汇
        explore_words = {"explore", "find", "search", "look", "探索", "寻找", "看看"}
        explore_score = sum(words.get(w, 0) for w in explore_words)
        
        # 战斗相关词汇
        combat_words = {"attack", "fight", "kill", "monster", "攻击", "打", "杀", "怪物"}
        combat_score = sum(words.get(w, 0) for w in combat_words)
        
        # 采集相关词汇
        collect_words = {"mine", "collect", "gather", "挖掘", "采集", "收集"}
        collect_score = sum(words.get(w, 0) for w in collect_words)
        
        # 确定主要风格
        scores = {
            "builder": build_score,
            "explorer": explore_score,
            "fighter": combat_score,
            "collector": collect_score,
        }
        
        max_score = max(scores.values())
        if max_score == 0:
            return "casual"
        
        # 找到分数最高的风格
        for style, score in scores.items():
            if score == max_score:
                return style
        
        return "casual"
    
    def get_player_summary(self, player_name: str) -> str:
        """获取玩家摘要"""
        profile = self.get_profile(player_name)
        if not profile:
            return f"未知玩家: {player_name}"
        
        fav_level = self.get_favorability_level_name(profile.favorability_level)
        play_style = self.analyze_play_style(player_name)
        
        summary = f"{player_name} ({fav_level})"
        summary += f"\n消息数: {profile.message_count}"
        summary += f"\n游戏风格: {play_style}"
        summary += f"\n好感度: {profile.favorability:.0f}/100"
        
        if profile.active_hours:
            # 找出最活跃的时段
            hour_counts: Dict[int, int] = {}
            for h in profile.active_hours:
                hour_counts[h] = hour_counts.get(h, 0) + 1
            sorted_hours = sorted(hour_counts.items(), key=lambda x: x[1], reverse=True)
            most_active = sorted_hours[:3]
            hours_str = ", ".join([f"{h}:00" for h, _ in most_active])
            summary += f"\n活跃时段: {hours_str}"
        
        return summary
    
    # ── 偏好学习 ──
    
    def learn_preference(self, player_name: str, topic: str):
        """学习玩家偏好"""
        profile = self.get_or_create_profile(player_name)
        
        # 更新话题统计
        profile.topics[topic] = profile.topics.get(topic, 0) + 1
        
        # 更新偏好活动
        if topic not in profile.preferred_activities:
            profile.preferred_activities.append(topic)
            # 只保留 top 5 偏好
            if len(profile.preferred_activities) > 5:
                # 移除最少提到的
                min_topic = min(profile.preferred_activities, 
                              key=lambda t: profile.topics.get(t, 0))
                profile.preferred_activities.remove(min_topic)
    
    def get_personalized_response_hint(self, player_name: str) -> str:
        """获取个性化响应提示"""
        profile = self.get_profile(player_name)
        if not profile:
            return ""
        
        hints = []
        
        # 基于好感度
        if profile.favorability_level >= 3:
            hints.append("这是你的好朋友，可以更亲近")
        elif profile.favorability_level <= 1:
            hints.append("这是不太熟的人，保持礼貌")
        
        # 基于游戏风格
        play_style = self.analyze_play_style(player_name)
        if play_style == "builder":
            hints.append("他喜欢建造，可以聊聊建筑")
        elif play_style == "explorer":
            hints.append("他喜欢探索，可以分享发现")
        elif play_style == "fighter":
            hints.append("他喜欢战斗，可以聊战斗技巧")
        
        return "，".join(hints) if hints else ""
    
    # ── 状态查询 ──
    
    def get_status(self) -> dict:
        """获取系统状态"""
        return {
            "total_profiles": len(self.profiles),
            "profiles": {
                name: {
                    "message_count": p.message_count,
                    "favorability": p.favorability,
                    "favorability_level": p.favorability_level,
                    "play_style": self.analyze_play_style(name),
                }
                for name, p in self.profiles.items()
            }
        }
