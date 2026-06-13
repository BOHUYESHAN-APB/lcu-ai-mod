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

import json
import logging
import re
import time
from typing import Any, Callable, Optional

logger = logging.getLogger("planner")
TOOL_CALL_RE = re.compile(r"tool\(\s*([a-zA-Z_]+)\s*,\s*(\{.*?\})\s*\)", re.DOTALL)

ITEM_ALIASES = {
    "木剑": "wooden_sword",
    "木棍": "stick",
    "木板": "oak_planks",
    "原木": "oak_log",
    "木头": "oak_log",
    "工作台": "crafting_table",
    "石剑": "stone_sword",
    "石镐": "stone_pickaxe",
    "木镐": "wooden_pickaxe",
}

BLOCK_ALIASES = {
    "木头": "oak_log",
    "原木": "oak_log",
    "木板": "oak_planks",
    "圆石": "cobblestone",
    "煤": "coal_ore",
    "铁": "iron_ore",
}


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
        nearby_blocks = context.get("nearby_blocks", []) if isinstance(context, dict) else []
        nearby_entities = context.get("entities", []) if isinstance(context, dict) else []
        inventory = context.get("inventory", []) if isinstance(context, dict) else []
        nearby_workstations = context.get("nearby_workstations", []) if isinstance(context, dict) else []
        nearby_storage = context.get("nearby_storage", []) if isinstance(context, dict) else []
        inventory_text = ", ".join(
            f"{item.get('name', '?')}x{item.get('count', 1)}"
            for item in inventory[:12]
        ) or "无"
        nearby_block_text = ", ".join(
            f"{block.get('block_id', block.get('name', '?'))}@{block.get('distance', '?')}m"
            for block in nearby_blocks[:8]
        ) or "无"
        nearby_entity_text = ", ".join(
            f"{entity.get('type', '?')}:{entity.get('name', '?')}@{entity.get('distance', '?')}m"
            for entity in nearby_entities[:8]
        ) or "无"
        nearby_item_text = ", ".join(
            f"{entity.get('item_id', entity.get('name', '?'))}x{entity.get('item_count', 1)}@{entity.get('distance', '?')}m"
            for entity in nearby_entities
            if entity.get("type") == "item"
        ) or "无"
        nearby_station_text = ", ".join(
            f"{poi.get('block_id', '?')}@{poi.get('distance', '?')}m"
            for poi in nearby_workstations[:8]
        ) or "无"
        nearby_storage_text = ", ".join(
            f"{poi.get('block_id', '?')}@{poi.get('distance', '?')}m"
            for poi in nearby_storage[:8]
        ) or "无"
        storage_contents_text = "\n".join(
            f"  - {poi.get('block_id', '?')}@{poi.get('distance', '?')}m: "
            + ", ".join(f"{c.get('item_id', '?')}x{c.get('count', 1)}" for c in (poi.get('contents') or [])[:6])
            for poi in nearby_storage[:6]
            if poi.get('contents')
        ) or "无"
        
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

当前任务状态：
{context.get('task_state', {'kind': 'idle', 'status': 'idle'})}

当前库存摘要：
{inventory_text}

附近资源：
- 方块：{nearby_block_text}
- 掉落物：{nearby_item_text}

附近工作站：
{nearby_station_text}

附近仓储：
{nearby_storage_text}

仓库已知内容：
{storage_contents_text}

附近实体：
{nearby_entity_text}

可用动作：
- reply(内容): 生成一条回复
- move_to(x, y, z): 移动到指定坐标
- follow(player_name): 跟随一个玩家
- attack(): 攻击附近的敌人
- mine_block(): 挖掘方块
- place_block(): 放置方块
- craft(item_name[, count]): 合成一个物品（如 wooden_pickaxe, stone_sword 等），并允许数量参数
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
- stop(): 停止当前动作（例如停止跟随/停止自动移动）
- finish(): 结束本轮

重要规则：
1. 你是一个真实玩家，不是 AI。不要说"作为 AI"、"我是 AI"之类的话
2. 说话要自然、随意，像真人玩家一样。可以用"哈哈"、"好的"、"来了"、"等下"等口语
3. 不要过度解释自己在做什么，直接做就行
4. 如果玩家让你做某事，直接去做，不要问太多问题
5. 回复要简短，不要长篇大论
6. 如果需要多步操作，可以连续执行多个动作
7. tool()/动作参数必须是可执行的真实参数，不能写“随机位置”“附近”“合适的地方”这种占位词
8. 如果用户要求停止跟随或停止当前动作，优先使用 stop()
9. 中文常见物品名要转换成 Minecraft 英文物品 ID，例如 木剑=wooden_sword，木头=oak_log
10. 如果当前任务状态不是 idle，且用户没有明确改变目标，不要重复下发同一个任务，优先继续或补全当前任务链

请分析情况并决定下一步行动。如果需要回复，使用 reply()。如果需要执行动作，优先使用 tool(动作名, JSON参数)。
如果不需要做任何事，使用 finish()。

回复格式：
reply(你的回复内容)
或
tool(动作名, {"key":"value"})
或
finish()

特别规则：
- 如果用户说“停下”“别跟了”“先别做了”，优先调用 stop()
- 不要写 move_to(随机位置) 这种不可执行占位词，参数必须是真实值
- 需要原版材料时，先 collect(...) 再 craft(...)
"""

        return prompt
    
    def _execute_plan(self, plan_text: str, sender: str, message: str, 
                      context: dict) -> Optional[str]:
        """执行规划结果。"""
        plan_lower = plan_text.lower()
        reply_text: Optional[str] = None
        executed_any_action = self._execute_tool_calls(plan_text, context)
        
        # 回复
        if "reply(" in plan_lower:
            content = self._extract_between(plan_text, "reply(", ")")
            if content:
                reply_text = content.strip()
        
        # 移动
        if "move_to(" in plan_lower:
            coords = self._extract_coords(plan_text, "move_to(")
            if coords and len(coords) >= 3:
                self.skills.move_to(coords[0], coords[1], coords[2])
                logger.info("[Planner] 移动到 (%.0f, %.0f, %.0f)", coords[0], coords[1], coords[2])
                executed_any_action = True
        
        # 跟随玩家
        if "follow(" in plan_lower:
            player_name = self._extract_between(plan_text, "follow(", ")")
            if player_name:
                normalized_target = player_name.strip()
                if not self._is_duplicate_task(context, {"follow"}, normalized_target):
                    self.skills.follow_player(normalized_target)
                    logger.info("[Planner] 跟随 %s", normalized_target)
                    executed_any_action = True
        
        # 攻击
        if "attack()" in plan_lower:
            self.skills.attack()
            logger.info("[Planner] 攻击")
            executed_any_action = True
        
        # 挖掘
        if "mine_block()" in plan_lower or "mine(" in plan_lower:
            self.skills.mine_block()
            logger.info("[Planner] 挖掘")
            executed_any_action = True
        
        # 放置方块
        if "place_block()" in plan_lower or "place(" in plan_lower:
            self.skills.place_block()
            logger.info("[Planner] 放置")
            executed_any_action = True
        
        # 合成
        if "craft(" in plan_lower:
            item_name = self._extract_between(plan_text, "craft(", ")")
            if item_name:
                parts = [part.strip() for part in item_name.split(",") if part.strip()]
                normalized_item = self._normalize_item_name(parts[0])
                craft_count = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 1
                if not self._is_duplicate_task(context, {"craft"}, normalized_item):
                    self.skills.craft_item(normalized_item, craft_count)
                    logger.info("[Planner] 合成 %s x%d", normalized_item, craft_count)
                    executed_any_action = True
        
        # 收集
        if "collect(" in plan_lower:
            args = self._extract_between(plan_text, "collect(", ")")
            if args:
                parts = args.split(",")
                block_type = parts[0].strip() if len(parts) > 0 else ""
                count = int(parts[1].strip()) if len(parts) > 1 else 1
                normalized_block = self._normalize_block_name(block_type)
                if not self._is_duplicate_task(context, {"collect"}, normalized_block):
                    self.skills.collect_blocks(normalized_block, count)
                    logger.info("[Planner] 收集 %s x%d", normalized_block, count)
                    executed_any_action = True
        
        # 装备
        if "equip(" in plan_lower:
            slot = self._extract_between(plan_text, "equip(", ")")
            if slot:
                self.skills.equip(slot.strip())
                logger.info("[Planner] 装备 %s", slot.strip())
                executed_any_action = True
        
        # 查看背包
        if "get_inventory()" in plan_lower:
            self.skills.get_inventory()
            logger.info("[Planner] 查看背包")
            executed_any_action = True
        
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
                executed_any_action = True
        
        # 交易
        if "trade(" in plan_lower:
            villager_type = self._extract_between(plan_text, "trade(", ")")
            if villager_type:
                self.skills.trade(villager_type.strip())
                logger.info("[Planner] 交易 %s", villager_type.strip())
                executed_any_action = True
        
        # 睡觉
        if "sleep()" in plan_lower:
            self.skills.sleep()
            logger.info("[Planner] 睡觉")
            executed_any_action = True
        
        # 吃东西
        if "eat()" in plan_lower:
            self.skills.eat()
            logger.info("[Planner] 吃东西")
            executed_any_action = True
        
        # 丢弃物品
        if "drop(" in plan_lower:
            args = self._extract_between(plan_text, "drop(", ")")
            if args:
                parts = args.split(",")
                item = parts[0].strip() if len(parts) > 0 else ""
                count = int(parts[1].strip()) if len(parts) > 1 else 1
                self.skills.drop_item(item, count)
                logger.info("[Planner] 丢弃 %s x%d", item, count)
                executed_any_action = True
        
        # 整理背包
        if "sort_inventory()" in plan_lower:
            self.skills.sort_inventory()
            logger.info("[Planner] 整理背包")
            executed_any_action = True

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
                        executed_any_action = True
                    except ValueError:
                        pass

        if "stop()" in plan_lower:
            self.skills.stop_all()
            logger.info("[Planner] 停止所有动作")
            executed_any_action = True
        
        # 结束
        if "finish()" in plan_lower:
            logger.info("[Planner] 结束本轮")
            return reply_text

        if executed_any_action:
            return reply_text

        if reply_text:
            return reply_text
        
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

    def _execute_tool_calls(self, text: str, context: dict) -> bool:
        executed = False
        for match in TOOL_CALL_RE.finditer(text):
            tool_name = match.group(1).strip().lower()
            payload_text = match.group(2).strip()
            try:
                payload = json.loads(payload_text)
            except json.JSONDecodeError:
                logger.warning("[Planner] 无法解析 tool 调用参数: %s", payload_text[:120])
                continue

            if self._dispatch_tool_call(tool_name, payload, context):
                executed = True
        return executed

    def _dispatch_tool_call(self, tool_name: str, payload: dict, context: dict) -> bool:
        if tool_name == "move_to":
            if {"x", "y", "z"}.issubset(payload):
                self.skills.move_to(float(payload["x"]), float(payload["y"]), float(payload["z"]))
                return True
        elif tool_name == "follow":
            player = payload.get("player") or payload.get("player_name")
            if player and not self._is_duplicate_task(context, {"follow"}, str(player)):
                self.skills.follow_player(str(player))
                return True
        elif tool_name == "attack":
            self.skills.attack()
            return True
        elif tool_name == "mine_block":
            self.skills.mine_block()
            return True
        elif tool_name == "place_block":
            self.skills.place_block()
            return True
        elif tool_name == "craft_item":
            item = payload.get("item") or payload.get("item_name")
            normalized_item = self._normalize_item_name(str(item)) if item else ""
            if item and not self._is_duplicate_task(context, {"craft"}, normalized_item):
                self.skills.craft_item(normalized_item, int(payload.get("count", 1)))
                return True
        elif tool_name == "collect_blocks":
            block_type = payload.get("block_type") or payload.get("block")
            normalized_block = self._normalize_block_name(str(block_type)) if block_type else ""
            if block_type and not self._is_duplicate_task(context, {"collect"}, normalized_block):
                self.skills.collect_blocks(normalized_block, int(payload.get("count", 1)))
                return True
        elif tool_name == "get_inventory":
            self.skills.get_inventory()
            return True
        elif tool_name == "explore":
            self.skills.explore(int(payload.get("radius", 16)))
            return True
        elif tool_name == "trade":
            villager = payload.get("villager_type") or payload.get("type")
            if villager:
                self.skills.trade(str(villager))
                return True
        elif tool_name == "sleep":
            self.skills.sleep()
            return True
        elif tool_name == "eat":
            self.skills.eat()
            return True
        elif tool_name == "drop_item":
            item = payload.get("item")
            if item:
                self.skills.drop_item(str(item), int(payload.get("count", 1)))
                return True
        elif tool_name == "sort_inventory":
            self.skills.sort_inventory()
            return True
        elif tool_name == "build":
            if {"x", "y", "z", "structure"}.issubset(payload):
                self.skills.build(float(payload["x"]), float(payload["y"]), float(payload["z"]), str(payload["structure"]))
                return True
        elif tool_name == "stop":
            self.skills.stop_all()
            return True
        return False

    def _normalize_item_name(self, item_name: str) -> str:
        stripped = item_name.strip().replace(" ", "")
        return ITEM_ALIASES.get(stripped, item_name.strip())

    def _normalize_block_name(self, block_name: str) -> str:
        stripped = block_name.strip().replace(" ", "")
        return BLOCK_ALIASES.get(stripped, block_name.strip())

    def _is_duplicate_task(self, context: dict, kinds: set[str], target: str) -> bool:
        task_state = context.get("task_state", {}) if isinstance(context, dict) else {}
        if not isinstance(task_state, dict):
            return False
        kind = str(task_state.get("kind", "")).strip().lower()
        status = str(task_state.get("status", "")).strip().lower()
        active_target = self._normalize_task_target(str(task_state.get("target", "")))
        requested_target = self._normalize_task_target(target)
        return kind in kinds and status not in {"", "idle", "done", "failed", "cancelled"} and active_target == requested_target

    def _normalize_task_target(self, target: str) -> str:
        return target.strip().replace(" ", "").lower()
    
    def interrupt(self):
        """中断当前规划。"""
        self._is_planning = False
        logger.info("[Planner] 规划被中断")
    
    def get_status(self) -> dict:
        return {
            "is_planning": self._is_planning,
            "last_plan_time": self._last_plan_time,
        }
