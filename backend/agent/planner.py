"""
Planner — 规划器。
参考 MaiBot 的 maisaka_chat.prompt 设计。

在 Timing Gate 判断应该回复后，Planner 决定具体做什么：
- reply(): 生成回复
- query_memory(): 查询记忆
- execute_action(): 执行游戏动作
- finish(): 结束本轮

支持可中断规划：新消息到达时可以中断当前规划。
"""

import logging
import time
from typing import Any, Callable, Optional

logger = logging.getLogger("planner")


class Planner:
    """
    规划器：决定 AI 应该做什么。
    
    设计参考 MaiBot 的两阶段推理：
    1. Timing Gate: 是否应该回复？
    2. Planner: 回复什么？做什么？
    """
    
    def __init__(self, llm_service, memory, skills):
        self.llm = llm_service
        self.memory = memory
        self.skills = skills
        self._is_planning = False
        self._last_plan_time = 0.0
    
    def plan_and_execute(self, sender: str, message: str, 
                         context: dict, bot_name: str = "AI") -> Optional[str]:
        """
        规划并执行动作。
        
        返回: 回复文本，或 None（如果不回复）
        """
        if not self.llm or not self.llm.api_key:
            logger.warning("[Planner] LLM 未配置，无法规划")
            return None
        
        self._is_planning = True
        self._last_plan_time = time.time()
        
        try:
            # 构建规划提示
            prompt = self._build_planner_prompt(sender, message, context, bot_name)
            
            # 调用 LLM 规划
            result = self.llm.chat([{"role": "user", "content": prompt}], agent="planner")
            plan_text = result.get("content", "")
            
            if not plan_text:
                logger.warning("[Planner] LLM 返回空内容")
                return None
            
            logger.info("[Planner] LLM 规划: %s", plan_text[:100])
            
            # 解析规划结果
            response = self._execute_plan(plan_text, sender, message, context)
            
            return response
            
        except Exception as e:
            logger.error("[Planner] 规划失败: %s", e)
            return None
        finally:
            self._is_planning = False
    
    def _build_planner_prompt(self, sender: str, message: str, 
                               context: dict, bot_name: str) -> str:
        """构建规划提示。"""
        # 获取记忆上下文
        memory_context = self.memory.build_context()
        persona = context.get("persona", {}) if isinstance(context, dict) else {}
        persona_name = persona.get("name", bot_name)
        personality = persona.get("personality", "友好、自然")
        speaking_style = persona.get("speaking_style", "口语化、简短")
        
        # 获取最近对话
        recent = self.memory.get_recent_context(5)
        recent_text = "\n".join([
            f"{msg.get('sender', '?')}: {msg.get('message', '')}" 
            for msg in recent
        ])
        
        prompt = f"""你是 {persona_name}，一个正在玩 Minecraft 的玩家。你和服务器里的其他玩家一起玩。

当前状态：
- 发送者：{sender}
- 消息：{message}

最近对话：
{recent_text}

记忆上下文：
{memory_context.get('interaction_summary', '无')}

当前人设：
- 性格：{personality}
- 说话风格：{speaking_style}

可用动作：
- reply(内容): 生成一条回复
- move_to(x, y, z): 移动到指定坐标
- follow(player_name): 跟随一个玩家
- attack(): 攻击附近的敌人
- mine_block(): 挖掘方块
- place_block(): 放置方块
- craft(item_name): 合成一个物品（如 wooden_pickaxe, stone_sword 等）
- collect(block_type, count): 收集指定数量的方块
- equip(slot): 装备物品（mainhand, offhand, head, chest, legs, feet）
- get_inventory(): 查看背包
- build(x, y, z, structure): 在指定位置建造结构
- explore(radius): 探索指定半径内的区域
- trade(villager_type): 与村民交易
- sleep(): 睡觉
- eat(): 吃东西
- drop(item, count): 丢弃物品
- sort_inventory(): 整理背包
- finish(): 结束本轮

重要规则：
1. 你是一个真实玩家，不是 AI。不要说"作为 AI"、"我是 AI"之类的话
2. 说话要自然、随意，像真人玩家一样。可以用"哈哈"、"好的"、"来了"、"等下"等口语
3. 不要过度解释自己在做什么，直接做就行
4. 如果玩家让你做某事，直接去做，不要问太多问题
5. 回复要简短，不要长篇大论
6. 如果需要多步操作，可以连续执行多个动作

请分析情况并决定下一步行动。如果需要回复，使用 reply()。如果需要执行动作，使用相应的动作函数。
如果不需要做任何事，使用 finish()。

回复格式：
reply(你的回复内容)
或
动作函数
或
finish()"""

        return prompt
    
    def _execute_plan(self, plan_text: str, sender: str, message: str, 
                      context: dict) -> Optional[str]:
        """执行规划结果。"""
        plan_lower = plan_text.lower()
        
        # 回复
        if "reply(" in plan_lower:
            content = self._extract_between(plan_text, "reply(", ")")
            if content:
                return content.strip()
        
        # 移动
        if "move_to(" in plan_lower:
            coords = self._extract_coords(plan_text, "move_to(")
            if coords and len(coords) >= 3:
                self.skills.move_to(coords[0], coords[1], coords[2])
                logger.info("[Planner] 移动到 (%.0f, %.0f, %.0f)", coords[0], coords[1], coords[2])
                return None
        
        # 跟随玩家
        if "follow(" in plan_lower:
            player_name = self._extract_between(plan_text, "follow(", ")")
            if player_name:
                self.skills.follow_player(player_name.strip())
                logger.info("[Planner] 跟随 %s", player_name.strip())
                return None
        
        # 攻击
        if "attack()" in plan_lower:
            self.skills.attack()
            logger.info("[Planner] 攻击")
            return None
        
        # 挖掘
        if "mine_block()" in plan_lower or "mine(" in plan_lower:
            self.skills.mine_block()
            logger.info("[Planner] 挖掘")
            return None
        
        # 放置方块
        if "place_block()" in plan_lower or "place(" in plan_lower:
            self.skills.place_block()
            logger.info("[Planner] 放置")
            return None
        
        # 合成
        if "craft(" in plan_lower:
            item_name = self._extract_between(plan_text, "craft(", ")")
            if item_name:
                self.skills.craft_item(item_name.strip())
                logger.info("[Planner] 合成 %s", item_name.strip())
                return None
        
        # 收集
        if "collect(" in plan_lower:
            args = self._extract_between(plan_text, "collect(", ")")
            if args:
                parts = args.split(",")
                block_type = parts[0].strip() if len(parts) > 0 else ""
                count = int(parts[1].strip()) if len(parts) > 1 else 1
                self.skills.collect_blocks(block_type, count)
                logger.info("[Planner] 收集 %s x%d", block_type, count)
                return None
        
        # 装备
        if "equip(" in plan_lower:
            slot = self._extract_between(plan_text, "equip(", ")")
            if slot:
                self.skills.equip(slot.strip())
                logger.info("[Planner] 装备 %s", slot.strip())
                return None
        
        # 查看背包
        if "get_inventory()" in plan_lower:
            self.skills.get_inventory()
            logger.info("[Planner] 查看背包")
            return None
        
        # 探索
        if "explore(" in plan_lower:
            radius_str = self._extract_between(plan_text, "explore(", ")")
            if radius_str:
                try:
                    radius = int(radius_str.strip())
                    self.skills.explore(radius)
                    logger.info("[Planner] 探索 %d 格", radius)
                except ValueError:
                    self.skills.explore(16)
                return None
        
        # 交易
        if "trade(" in plan_lower:
            villager_type = self._extract_between(plan_text, "trade(", ")")
            if villager_type:
                self.skills.trade(villager_type.strip())
                logger.info("[Planner] 交易 %s", villager_type.strip())
                return None
        
        # 睡觉
        if "sleep()" in plan_lower:
            self.skills.sleep()
            logger.info("[Planner] 睡觉")
            return None
        
        # 吃东西
        if "eat()" in plan_lower:
            self.skills.eat()
            logger.info("[Planner] 吃东西")
            return None
        
        # 丢弃物品
        if "drop(" in plan_lower:
            args = self._extract_between(plan_text, "drop(", ")")
            if args:
                parts = args.split(",")
                item = parts[0].strip() if len(parts) > 0 else ""
                count = int(parts[1].strip()) if len(parts) > 1 else 1
                self.skills.drop_item(item, count)
                logger.info("[Planner] 丢弃 %s x%d", item, count)
                return None
        
        # 整理背包
        if "sort_inventory()" in plan_lower:
            self.skills.sort_inventory()
            logger.info("[Planner] 整理背包")
            return None
        
        # 建造
        if "build(" in plan_lower:
            args = self._extract_between(plan_text, "build(", ")")
            if args:
                parts = args.split(",")
                if len(parts) >= 4:
                    try:
                        x, y, z = float(parts[0]), float(parts[1]), float(parts[2])
                        structure = parts[3].strip()
                        self.skills.build(x, y, z, structure)
                        logger.info("[Planner] 建造 %s 在 (%.0f, %.0f, %.0f)", structure, x, y, z)
                    except ValueError:
                        pass
                return None
        
        # 结束
        if "finish()" in plan_lower:
            logger.info("[Planner] 结束本轮")
            return None
        
        # 如果没有匹配到任何动作，将整个文本作为回复
        logger.info("[Planner] 未匹配到动作，作为回复: %s", plan_text[:50])
        return plan_text
    
    def _extract_between(self, text: str, start_marker: str, end_marker: str) -> Optional[str]:
        """提取两个标记之间的内容。"""
        start = text.find(start_marker)
        if start < 0:
            return None
        content_start = start + len(start_marker)
        end = text.find(end_marker, content_start)
        if end < 0:
            return None
        return text[content_start:end]
    
    def _extract_coords(self, text: str, marker: str) -> Optional[list]:
        """提取坐标参数。"""
        content = self._extract_between(text, marker, ")")
        if not content:
            return None
        try:
            coords = [float(c.strip()) for c in content.split(",")]
            return coords
        except ValueError:
            return None
    
    def interrupt(self):
        """中断当前规划。"""
        self._is_planning = False
        logger.info("[Planner] 规划被中断")
    
    def get_status(self) -> dict:
        return {
            "is_planning": self._is_planning,
            "last_plan_time": self._last_plan_time,
        }
