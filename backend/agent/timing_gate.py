"""
Timing Gate — 节奏门控系统。
参考 MaiBot 的 maisaka_timing_gate.prompt 设计。

在决定是否回复之前，先判断聊天节奏：
- continue: 立刻回复
- no_reply: 不回复，等待新消息
- wait: 等待更多消息（用户可能还在输入）

核心原则：
1. 不要每条消息都回复
2. 不要打断用户之间的对话
3. 评估用户是否在和 AI 说话
4. 评估用户是否还有后续消息
"""

import logging
import time
from typing import Optional

logger = logging.getLogger("timing_gate")


class TimingGate:
    """
    节奏门控：决定 AI 是否应该回复。
    
    设计参考 MaiBot 的两阶段推理：
    1. 先判断节奏（Timing Gate）
    2. 再规划行动（Planner）
    """
    
    def __init__(self, llm_service):
        self.llm = llm_service
        self._last_reply_time = 0.0
        self._reply_count_in_window = 0
        self._window_start = 0.0
        self._window_size = 60.0  # 60 秒窗口
        self._max_replies_per_window = 5  # 每窗口最多回复 5 次
        self._min_reply_interval = 3.0  # 最少间隔 3 秒
        self._last_message_time = 0.0
        self._pending_messages = []
        self._debounce_seconds = 2.0  # 等待 2 秒看用户是否还有后续消息
    
    def should_respond(self, sender: str, message: str, 
                       recent_messages: Optional[list] = None,
                       bot_name: str = "AI",
                       wake_names: Optional[list[str]] = None) -> tuple[bool, str]:
        """
        判断是否应该回复。
        
        返回: (should_respond: bool, reason: str)
        
        判断逻辑（参考 MaiBot Timing Gate）：
        1. @提及 AI → 立刻回复
        2. 私聊（MC /msg 命令） → 立刻回复
        3. 频率限制 → 不回复
        4. 用户可能还在输入 → 等待
        5. 用户之间在对话 → 不打断
        """
        now = time.time()
        
        # 1. 检查是否 @提及 AI
        wake_names = wake_names or [bot_name]
        lowered_message = message.lower()
        for wake_name in wake_names:
            candidate = str(wake_name).strip()
            if candidate and candidate.lower() in lowered_message:
                logger.info("[TimingGate] 提及唤醒名 %s，立刻回复", candidate)
                return True, f"mentioned:{candidate}"
        
        # 2. 检查是否是私聊（MC /msg 命令格式）
        # MC 私聊格式: "[Player -> You] message" 或 "[You -> Player] message"
        if self._is_private_message(message, bot_name):
            logger.info("[TimingGate] 私聊，立刻回复")
            return True, "private_chat"
        
        # 3. 频率限制
        if now - self._window_start > self._window_size:
            self._reply_count_in_window = 0
            self._window_start = now
        
        if self._reply_count_in_window >= self._max_replies_per_window:
            logger.info("[TimingGate] 频率限制，不回复（%d/%d）", 
                       self._reply_count_in_window, self._max_replies_per_window)
            return False, "rate_limited"
        
        # 4. 最小间隔
        if now - self._last_reply_time < self._min_reply_interval:
            logger.info("[TimingGate] 间隔太短，不回复")
            return False, "too_soon"
        
        # 5. 检查用户是否还在输入（短时间内多条消息）
        if now - self._last_message_time < self._debounce_seconds:
            logger.info("[TimingGate] 用户可能还在输入，等待")
            return False, "debouncing"
        self._last_message_time = now
        
        # 6. 检查是否是用户之间的对话（不打断）
        if recent_messages and len(recent_messages) >= 2:
            # 如果最近两条消息来自不同用户，且都不是 AI
            last_two = recent_messages[-2:]
            if (last_two[0].get("sender") != bot_name and 
                last_two[1].get("sender") != bot_name and
                last_two[0].get("sender") != last_two[1].get("sender")):
                logger.info("[TimingGate] 用户之间在对话，不打断")
                return False, "user_conversation"
        
        # 7. 使用 LLM 判断是否应该回复（可选，需要 LLM 可用）
        if self.llm and self.llm.api_key:
            try:
                llm_decision = self._ask_llm_should_respond(sender, message, recent_messages, bot_name)
                if llm_decision:
                    logger.info("[TimingGate] LLM 判断：回复（%s）", llm_decision)
                    return True, f"llm:{llm_decision}"
                else:
                    logger.info("[TimingGate] LLM 判断：不回复")
                    return False, "llm:no_reply"
            except Exception as e:
                logger.warning("[TimingGate] LLM 判断失败: %s", e)
                # 降级到简单规则
                pass
        
        # 8. 默认：回复（如果前面的检查都通过）
        logger.info("[TimingGate] 默认回复")
        return True, "default"
    
    def _is_private_message(self, message: str, bot_name: str) -> bool:
        """
        检测是否是 MC 私聊消息。
        MC 私聊格式: "[Player -> You] message" 或 "[You -> Player] message"
        """
        # 检查是否包含 "->" 和 "]"
        if " -> " in message and "]" in message:
            # 检查是否是发送给 AI 的私聊
            if f"-> {bot_name}]" in message or f"-> You]" in message:
                return True
        return False
    
    def _ask_llm_should_respond(self, sender: str, message: str,
                                 recent_messages: Optional[list], bot_name: str) -> bool:
        """使用 LLM 判断是否应该回复。"""
        context = ""
        if recent_messages:
            for msg in recent_messages[-5:]:  # 最近 5 条消息
                context += f"{msg.get('sender', '?')}: {msg.get('message', '')}\n"
        
        prompt = f"""你是 {bot_name} 的节奏门控系统。分析当前聊天节奏，决定是否应该回复。

最近聊天记录：
{context}

新消息：{sender}: {message}

规则：
1. 如果用户 @提及 {bot_name}，必须回复
2. 如果用户在和 {bot_name} 说话，应该回复
3. 如果用户之间在对话，不要打断
4. 如果用户可能还在输入（短时间多条消息），等待
5. 如果 {bot_name} 已经回复了很多次，适当减少回复

只回复 "reply" 或 "no_reply"，不要解释。"""

        try:
            result = self.llm.chat([{"role": "user", "content": prompt}], agent="timing_gate")
            content = result.get("content", "").strip().lower()
            return "reply" in content and "no_reply" not in content
        except Exception as e:
            logger.warning("[TimingGate] LLM 调用失败: %s", e)
            return True  # 降级：默认回复
    
    def record_reply(self):
        """记录一次回复。"""
        self._last_reply_time = time.time()
        self._reply_count_in_window += 1
    
    def get_status(self) -> dict:
        return {
            "last_reply_time": self._last_reply_time,
            "reply_count_in_window": self._reply_count_in_window,
            "window_remaining": max(0, self._window_size - (time.time() - self._window_start)),
        }
