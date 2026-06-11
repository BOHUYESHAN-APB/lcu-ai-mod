"""
Conversation Agent — 对话处理 Agent。
专门处理聊天消息、记忆管理、人设系统。
与 Action Agent 分离，使用异步 LLM 调用。

参考 MaiBot 的 HeartFlow 架构：
- 异步消息处理
- 多轮对话管理
- 记忆注入
"""

import asyncio
import logging
import time
from typing import Optional, Dict, Any
from dataclasses import dataclass, field

logger = logging.getLogger("conversation_agent")


@dataclass
class ConversationContext:
    """对话上下文"""
    sender: str
    message: str
    timestamp: float
    is_private: bool = False
    mentioned: bool = False
    recent_messages: list = field(default_factory=list)
    player_profile: dict = field(default_factory=dict)
    memory_context: dict = field(default_factory=dict)


class ConversationAgent:
    """
    对话 Agent — 处理所有聊天相关逻辑。
    
    核心职责：
    1. 接收聊天消息
    2. 判断是否回复（Timing Gate）
    3. 生成回复内容（LLM）
    4. 管理记忆和人设
    5. 将动作指令发送给 Action Agent
    
    与 Action Agent 的关系：
    - Conversation Agent 生成动作指令
    - Action Agent 执行动作
    - 两者通过消息队列通信
    """
    
    def __init__(self, llm_service, memory, message_db):
        self.llm = llm_service
        self.memory = memory
        self.message_db = message_db
        
        # 消息队列（发送给 Action Agent）
        self.action_queue: asyncio.Queue = asyncio.Queue()
        
        # 对话历史
        self.conversation_history: Dict[str, list] = {}
        
        # 人设配置
        self.persona = {
            "name": "AI",
            "personality": "友好、自然、像真人玩家",
            "speaking_style": "口语化、简短",
        }
        
        # 异步处理任务
        self._processing_task: Optional[asyncio.Task] = None
        self._is_running = False
    
    async def start(self):
        """启动对话 Agent"""
        self._is_running = True
        self._processing_task = asyncio.create_task(self._processing_loop())
        logger.info("[ConvAgent] Started")
    
    async def stop(self):
        """停止对话 Agent"""
        self._is_running = False
        if self._processing_task:
            self._processing_task.cancel()
            try:
                await self._processing_task
            except asyncio.CancelledError:
                pass
        logger.info("[ConvAgent] Stopped")
    
    async def _processing_loop(self):
        """主处理循环"""
        while self._is_running:
            try:
                # 从队列获取消息（非阻塞）
                try:
                    message_data = await asyncio.wait_for(
                        self.action_queue.get(), 
                        timeout=0.1
                    )
                    await self._process_message(message_data)
                except asyncio.TimeoutError:
                    continue
                    
            except Exception as e:
                logger.error("[ConvAgent] Error in processing loop: %s", e)
                await asyncio.sleep(1)
    
    async def _process_message(self, message_data: dict):
        """处理单条消息"""
        sender = message_data.get("sender", "?")
        message = message_data.get("message", "")
        is_system = message_data.get("is_system", False)
        
        if is_system:
            return
        
        # 保存到数据库
        self.message_db.add_message(
            sender=sender,
            message=message,
            is_system=is_system
        )
        
        # 更新记忆
        self.memory.add_interaction(
            sender=sender,
            message=message
        )
        
        # 构建对话上下文
        context = self._build_context(sender, message)
        
        # 判断是否回复
        if not self._should_respond(context):
            logger.info("[ConvAgent] 不回复 %s 的消息", sender)
            return
        
        # 生成回复
        response = await self._generate_response(context)
        
        if response:
            # 保存回复
            self.message_db.add_message(
                sender=self.persona["name"],
                message=response,
                is_ai=True
            )
            self.memory.add_interaction(
                sender=self.persona["name"],
                message=response
            )
            
            # 返回回复（由 Orchestrator 发送到游戏）
            return response
        
        return None
    
    def _build_context(self, sender: str, message: str) -> ConversationContext:
        """构建对话上下文"""
        # 获取最近消息
        recent = self.message_db.get_recent_messages(limit=10)
        
        # 获取玩家画像
        player_profile = self.message_db.get_player_stats(sender) or {}
        
        # 获取记忆上下文
        memory_context = self.memory.build_context()
        
        return ConversationContext(
            sender=sender,
            message=message,
            timestamp=time.time(),
            recent_messages=recent,
            player_profile=player_profile,
            memory_context=memory_context
        )
    
    def _should_respond(self, context: ConversationContext) -> bool:
        """判断是否应该回复（简化版 Timing Gate）"""
        message = context.message
        sender = context.sender
        
        # 检查是否 @提及
        if self.persona["name"].lower() in message.lower():
            return True
        
        # 检查是否是私聊
        if " -> " in message and "]" in message:
            return True
        
        # 检查是否是直接对话（最近消息中只有一个人）
        recent = context.recent_messages
        if len(recent) <= 2:
            return True
        
        # 检查是否是用户之间的对话
        if len(recent) >= 2:
            last_two = recent[-2:]
            if (last_two[0].get("sender") != self.persona["name"] and 
                last_two[1].get("sender") != self.persona["name"] and
                last_two[0].get("sender") != last_two[1].get("sender")):
                return False
        
        return True
    
    async def _generate_response(self, context: ConversationContext) -> Optional[str]:
        """生成回复（异步 LLM 调用）"""
        if not self.llm or not self.llm.api_key:
            logger.warning("[ConvAgent] LLM 未配置")
            return None
        
        try:
            # 构建提示
            prompt = self._build_prompt(context)
            
            # 异步调用 LLM
            result = await asyncio.to_thread(
                self.llm.chat,
                [{"role": "user", "content": prompt}],
                agent="conversation"
            )
            
            response = result.get("content", "")
            
            # 解析回复和动作
            parsed = self._parse_response(response)
            
            # 如果有动作，发送给 Action Agent
            if parsed.get("actions"):
                for action in parsed["actions"]:
                    await self._send_action(action)
            
            return parsed.get("reply")
            
        except Exception as e:
            logger.error("[ConvAgent] LLM error: %s", e)
            return None
    
    def _build_prompt(self, context: ConversationContext) -> str:
        """构建 LLM 提示"""
        recent_text = "\n".join([
            f"{msg.get('sender', '?')}: {msg.get('message', '')}"
            for msg in context.recent_messages[-5:]
        ])
        
        player_info = ""
        if context.player_profile:
            player_info = f"玩家 {context.sender} 已发送 {context.player_profile.get('message_count', 0)} 条消息"
        
        prompt = f"""你是 {self.persona['name']}，一个正在玩 Minecraft 的玩家。

当前情况：
- {context.sender} 说: {context.message}
- {player_info}

最近对话：
{recent_text}

重要规则：
1. 你是真实玩家，不是 AI
2. 说话自然、简短，像真人一样
3. 如果需要执行动作，使用 reply() 或其他动作函数

可用动作：
- reply(内容): 回复消息
- move_to(x, y, z): 移动到坐标
- follow(玩家名): 跟随玩家
- attack(): 攻击敌人
- mine(): 挖掘方块
- craft(物品): 合成物品
- finish(): 结束

请回复："""
        
        return prompt
    
    def _parse_response(self, response: str) -> dict:
        """解析 LLM 回复，提取动作和回复内容"""
        result = {
            "reply": None,
            "actions": []
        }
        
        # 提取 reply()
        if "reply(" in response:
            start = response.find("reply(")
            end = response.find(")", start)
            if end > start:
                reply_content = response[start+6:end].strip()
                result["reply"] = reply_content
        
        # 提取其他动作
        action_patterns = [
            "move_to(", "follow(", "attack()", "mine()",
            "craft(", "finish()"
        ]
        
        for pattern in action_patterns:
            if pattern in response:
                start = response.find(pattern)
                end = response.find(")", start)
                if end > start:
                    action_str = response[start:end+1]
                    result["actions"].append(action_str)
        
        # 如果没有 reply()，将整个文本作为回复
        if not result["reply"] and not result["actions"]:
            result["reply"] = response
        
        return result
    
    async def _send_action(self, action_str: str):
        """发送动作给 Action Agent"""
        # 这里通过 Orchestrator 发送到 Java 端
        logger.info("[ConvAgent] 发送动作: %s", action_str)
    
    def get_status(self) -> dict:
        """获取状态"""
        return {
            "is_running": self._is_running,
            "persona": self.persona,
            "queue_size": self.action_queue.qsize(),
        }
