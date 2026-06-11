"""
Multi-Agent Orchestrator — 多 Agent 协调器。
协调 Conversation Agent 和 Action Agent 的工作。

架构设计：
┌─────────────────────────────────────────────────────────────┐
│                    Orchestrator (协调器)                      │
│   接收所有事件，分发给不同 Agent 处理                          │
└─────────────────────────────────────────────────────────────┘
         │                    │                    │
         ▼                    ▼                    ▼
┌─────────────────┐  ┌─────────────────┐  ┌─────────────────┐
│  Action Agent   │  │ Conversation    │  │  Behavior Agent  │
│  (Java 端)      │  │ Agent (Python)  │  │  (Java 端)       │
│                 │  │                 │  │                  │
│ • 移动控制      │  │ • 聊天回复      │  │ • 自动漫游       │
│ • 战斗系统      │  │ • 记忆管理      │  │ • 生存行为       │
│ • 合成系统      │  │ • 用户画像      │  │ • 危险规避       │
│ • 采集系统      │  │ • 人设系统      │  │ • 头部追踪       │
└─────────────────┘  └─────────────────┘  └─────────────────┘
     持续运行              异步LLM              持续运行
"""

import asyncio
import logging
import time
from typing import Optional, Dict, Any

from .conversation_agent import ConversationAgent
from .action_agent import ActionAgent, ActionPriority
from .memory import Memory
from .message_db import MessageDB
from .llm_service import LLMService
from .skills import Skills

logger = logging.getLogger("multi_agent_orchestrator")


class MultiAgentOrchestrator:
    """
    多 Agent 协调器。
    
    核心职责：
    1. 接收游戏事件
    2. 分发给对应的 Agent 处理
    3. 协调 Agent 之间的通信
    4. 管理 Agent 生命周期
    
    事件路由：
    - player_chat → Conversation Agent
    - state_update → Action Agent (用于自主行为)
    - command_response → Action Agent
    """
    
    def __init__(self, wire):
        self.wire = wire
        
        # 初始化子系统
        self.memory = Memory()
        self.message_db = MessageDB()
        self.llm = LLMService()
        self.skills = Skills(wire)
        
        # 初始化 Agent
        self.conversation_agent = ConversationAgent(
            llm_service=self.llm,
            memory=self.memory,
            message_db=self.message_db
        )
        
        self.action_agent = ActionAgent(
            skills=self.skills
        )
        
        # 状态
        self._is_running = False
        self._start_time = None
        
        # 统计
        self.stats = {
            "messages_processed": 0,
            "actions_executed": 0,
            "llm_calls": 0,
        }
    
    async def start(self):
        """启动所有 Agent"""
        if self._is_running:
            return
        
        self._is_running = True
        self._start_time = time.time()
        
        # 启动 Agent
        await self.conversation_agent.start()
        await self.action_agent.start()
        
        logger.info("[MultiAgentOrch] Started with Conversation Agent and Action Agent")
    
    async def stop(self):
        """停止所有 Agent"""
        if not self._is_running:
            return
        
        self._is_running = False
        
        # 停止 Agent
        await self.conversation_agent.stop()
        await self.action_agent.stop()
        
        # 保存数据
        self.memory.save()
        self.message_db.close()
        
        logger.info("[MultiAgentOrch] Stopped")
    
    # ── 事件处理 ──
    
    async def handle_event(self, event_type: str, data: dict):
        """处理游戏事件"""
        if not self._is_running:
            return
        
        try:
            if event_type == "player_chat":
                await self._handle_chat(data)
            elif event_type == "state_update":
                await self._handle_state_update(data)
            elif event_type == "command_response":
                await self._handle_command_response(data)
            elif event_type == "player_death":
                await self._handle_death(data)
            else:
                logger.debug("[MultiAgentOrch] Unhandled event: %s", event_type)
                
        except Exception as e:
            logger.error("[MultiAgentOrch] Error handling %s: %s", event_type, e)
    
    async def _handle_chat(self, data: dict):
        """处理聊天消息 — 发送给 Conversation Agent"""
        sender = data.get("sender", "?")
        message = data.get("message", "")
        is_system = data.get("is_system", False)
        
        logger.info("[MultiAgentOrch] Chat from %s: %s", sender, message[:50])
        
        # 发送到 Conversation Agent 的队列
        await self.conversation_agent.action_queue.put({
            "sender": sender,
            "message": message,
            "is_system": is_system,
            "timestamp": time.time()
        })
        
        self.stats["messages_processed"] += 1
        
        # 等待回复（带超时）
        try:
            # Conversation Agent 会通过回调返回回复
            pass
        except Exception as e:
            logger.error("[MultiAgentOrch] Error waiting for reply: %s", e)
    
    async def _handle_state_update(self, data: dict):
        """处理状态更新 — 用于自主行为"""
        # 更新运行时状态
        # Action Agent 可以使用这些数据进行自主行为
        pass
    
    async def _handle_command_response(self, data: dict):
        """处理命令响应"""
        req_id = data.get("id", "?")
        success = data.get("success", False)
        
        logger.debug("[MultiAgentOrch] Command response: %s success=%s", req_id, success)
        
        self.stats["actions_executed"] += 1
    
    async def _handle_death(self, data: dict):
        """处理玩家死亡"""
        logger.info("[MultiAgentOrch] Player died")
        
        # 记录到记忆
        self.memory.record_event("death", "玩家死亡")
        self.message_db.add_event("death", "玩家死亡")
    
    # ── 动作提交 API ──
    
    async def submit_action(self, name: str, action, 
                           priority: ActionPriority = ActionPriority.MEDIUM):
        """提交动作给 Action Agent"""
        await self.action_agent.submit_action(
            name=name,
            action=action,
            priority=priority
        )
    
    async def move_to(self, x: float, y: float, z: float, speed: float = 1.0):
        """移动到指定坐标"""
        await self.action_agent.move_to(x, y, z, speed)
    
    async def follow_player(self, player_name: str):
        """跟随玩家"""
        await self.action_agent.follow_player(player_name)
    
    async def attack(self):
        """攻击"""
        await self.action_agent.attack()
    
    async def mine_block(self):
        """挖掘"""
        await self.action_agent.mine_block()
    
    async def craft_item(self, item_name: str):
        """合成物品"""
        await self.action_agent.craft_item(item_name)
    
    # ── 状态查询 ──
    
    def get_status(self) -> dict:
        """获取整体状态"""
        return {
            "is_running": self._is_running,
            "uptime": time.time() - self._start_time if self._start_time else 0,
            "conversation_agent": self.conversation_agent.get_status(),
            "action_agent": self.action_agent.get_status(),
            "stats": self.stats.copy(),
            "llm_config": {
                "configured": self.llm.api_key is not None,
                "model": self.llm.model,
            },
        }
    
    def get_memory_stats(self) -> dict:
        """获取记忆统计"""
        return self.message_db.get_stats()
