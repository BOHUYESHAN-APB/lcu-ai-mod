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
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

logger = logging.getLogger("planner")
TOOL_CALL_RE = re.compile(r"^\s*tool\(\s*([a-zA-Z_]+)\s*,\s*(\{.*\})\s*\)\s*$")
REPLY_RE = re.compile(r"^\s*reply\((.*)\)\s*$")
ACTION_SYNTAX_RE = re.compile(
    r"(?<![A-Za-z0-9_])(?:tool|reply|finish|move_to|follow|attack|mine(?:_block)?|place(?:_block)?|craft|collect|"
    r"equip|get_inventory|explore|trade|sleep|eat|drop|sort_inventory|build|stop)\s*\(",
    re.IGNORECASE,
)

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
    "铁镐": "iron_pickaxe",
    "钻石镐": "diamond_pickaxe",
    "下界合金镐": "netherite_pickaxe",
}

BLOCK_ALIASES = {
    "木头": "#lcu:wood",
    "原木": "#minecraft:logs",
    "木板": "#minecraft:planks",
    "圆石": "cobblestone",
    "煤": "coal_ore",
    "铁": "iron_ore",
}


def _clip(value: Any, limit: int) -> str:
    text = str(value).replace("\x00", "")
    return text if len(text) <= limit else text[:limit] + "..."


@dataclass(frozen=True)
class SkillProposal:
    skill_id: str
    input: dict[str, Any]
    source: str

    def public_dict(self) -> dict[str, Any]:
        return {"skill_id": self.skill_id, "input": dict(self.input), "source": self.source}


class Planner:
    """
    规划器：决定 AI 应该做什么。
    
    设计参考 MaiBot 的两阶段推理：
    1. Timing Gate: 是否应该回复？
    2. Planner: 回复什么？做什么？
    """
    
    def __init__(self, llm_service, memory, skills=None):
        self.llm = llm_service
        self.memory = memory
        self.skills = skills
        self._proposal_dispatcher: Callable[[SkillProposal], bool] | None = None
        self._seen_proposals: set[str] = set()
        self._proposal_emitted = False
        self._proposal_attempted = False
        self._generation = 0
        self._planning_lock = threading.RLock()
        self._is_planning = False
        self._last_plan_time = 0.0
        self._last_plan_executed_action = False
        self._last_plan_preview = ""
        self._last_execution_source = "none"
        self._last_protocol_error = ""

    def set_proposal_dispatcher(self, dispatcher: Callable[[SkillProposal], bool] | None) -> None:
        self._proposal_dispatcher = dispatcher

    def dispatch_proposal(self, proposal: SkillProposal) -> bool:
        if self._proposal_dispatcher is None:
            self._last_protocol_error = "planner proposal dispatcher is not configured"
            return False
        try:
            return bool(self._proposal_dispatcher(proposal))
        except Exception as exc:
            self._last_protocol_error = str(exc)[:500]
            return False
    
    def plan_and_execute(self, sender: str, message: str, 
                         context: dict, bot_name: str = "AI") -> Optional[str]:
        """
        规划并执行动作。
        
        返回: 回复文本，或 None（如果不回复）
        """
        if not self.llm or not self.llm.is_configured("planner"):
            logger.warning("[Planner] LLM 未配置，无法规划")
            return None
        
        with self._planning_lock:
            generation = self._generation
            self._is_planning = True
            self._last_plan_time = time.time()
        
        try:
            # 构建规划提示
            prompt = self._build_planner_prompt(sender, message, context, bot_name)
            
            # 调用 LLM 规划
            result = self.llm.chat([{"role": "user", "content": prompt}], agent="planner")
            plan_text = result.get("content", "")
            with self._planning_lock:
                if generation != self._generation:
                    return None
                self._last_plan_preview = str(plan_text)[:500]
                if not plan_text:
                    logger.warning("[Planner] LLM 返回空内容")
                    return None
                logger.info("[Planner] LLM 规划: %s", plan_text[:100])

                # Keep parsing and proposal emission atomic with respect to interrupt().
                response = self._execute_plan(plan_text, sender, message, context)
                if not self._last_plan_executed_action and not self._proposal_attempted \
                        and not self._last_protocol_error:
                    self._execute_direct_intent_fallback(sender, message, context)
            
            return response
            
        except Exception as e:
            with self._planning_lock:
                if generation == self._generation:
                    self._last_protocol_error = str(e)
            logger.error("[Planner] 规划失败: %s", e)
            return None
        finally:
            with self._planning_lock:
                if generation == self._generation:
                    self._is_planning = False
    
    def _build_planner_prompt(self, sender: str, message: str, 
                               context: dict, bot_name: str) -> str:
        """构建规划提示。"""
        # 获取记忆上下文
        memory_context = context
        persona = context.get("persona", {}) if isinstance(context, dict) else {}
        persona_name = persona.get("name", bot_name)
        personality = persona.get("personality", "友好、自然")
        speaking_style = persona.get("speaking_style", "口语化、简短")
        external_context = persona.get("external_context", {})
        external_context_text = _clip(
            json.dumps(external_context, ensure_ascii=False, default=str) if external_context else "无",
            1000,
        )

        recent_text = _clip(memory_context.get("interaction_summary", "暂无对话记录"), 1500)
        relationship_text = _clip(memory_context.get("relationship_summary", "无"), 1000)
        task_outcome_text = _clip(memory_context.get("task_outcomes", "无"), 1000)
        world_experience_text = _clip(memory_context.get("world_experience", "无"), 1000)
        task_state_text = _clip(context.get("task_state", {"kind": "idle", "status": "idle"}), 500)
        sender_text = _clip(sender, 128)
        message_text = _clip(message, 1000)
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
- 发送者：{sender_text}
- 消息：{message_text}

最近对话：
{recent_text}

玩家关系：
{relationship_text}

最近任务结果：
{task_outcome_text}

服务器与世界经验：
{world_experience_text}

当前人设：
- 性格：{personality}
- 说话风格：{speaking_style}
- 上游集成上下文：{external_context_text}

当前任务状态：
{task_state_text}

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
- tool(move_to, {{"x":0,"y":64,"z":0}}): 移动到明确坐标
- tool(craft_item, {{"item":"minecraft:iron_pickaxe","count":1}}): 合成物品；缺少材料时继续执行确定性的取料和制作链
- tool(collect_blocks, {{"block_type":"minecraft:iron_ore","count":3}}): 收集指定数量的方块
- tool(eat, {{}}): 吃东西
- tool(stop, {{}}): 确定性停止当前动作
- finish(): 结束本轮

重要规则：
1. 你是一个真实玩家，不是 AI。不要说"作为 AI"、"我是 AI"之类的话
2. 说话要自然、随意，像真人玩家一样。可以用"哈哈"、"好的"、"来了"、"等下"等口语
3. 不要过度解释自己在做什么，直接做就行
4. 如果玩家让你做某事，必须输出对应的 tool()；可以同时 reply() 简短确认，但不能只答应而不调用工具
5. 回复要简短，不要长篇大论
6. 每轮最多输出一个动作；多步骤目标交给 durable workflow，不要连续输出多个动作
7. tool()/动作参数必须是可执行的真实参数，不能写“随机位置”“附近”“合适的地方”这种占位词
8. 如果用户要求停止跟随或停止当前动作，优先使用 stop()
9. 中文具体物品名转换为 registry ID；类别词使用标签，例如 铁镐=minecraft:iron_pickaxe，原木=#minecraft:logs，木板=#minecraft:planks，木头=#lcu:wood
10. 如果当前任务状态不是 idle，且用户没有明确改变目标，不要重复下发同一个任务，优先继续或补全当前任务链

请分析情况并决定下一步行动。如果需要回复，使用 reply()。如果需要执行动作，优先使用 tool(动作名, JSON参数)。
如果不需要做任何事，使用 finish()。

回复格式：
reply(你的回复内容)
或
tool(动作名, {{"key":"value"}})
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
        self._last_plan_executed_action = False
        self._last_execution_source = "none"
        self._last_protocol_error = ""
        self._seen_proposals = set()
        self._proposal_emitted = False
        self._proposal_attempted = False
        lines = [line.strip() for line in plan_text.splitlines() if line.strip()]
        invalid_line = next((
            line for line in lines
            if line.startswith("```")
            or ACTION_SYNTAX_RE.search(line) is not None
            and REPLY_RE.fullmatch(line) is None
            and TOOL_CALL_RE.fullmatch(line) is None
            and line.lower() != "finish()"
        ), None)
        if invalid_line is not None:
            self._last_protocol_error = "planner response does not match the top-level action grammar"
            return None
        action_lines = [line for line in lines if TOOL_CALL_RE.fullmatch(line) or line.lower() == "finish()"]
        if action_lines and any(
            REPLY_RE.fullmatch(line) is None
            and TOOL_CALL_RE.fullmatch(line) is None
            and line.lower() != "finish()"
            for line in lines
        ):
            self._last_protocol_error = "planner response contains text outside the top-level grammar"
            return None
        if len(action_lines) > 1:
            self._last_protocol_error = "planner response contains more than one top-level action"
            return None
        action_text = "\n".join(action_lines)
        reply_text: Optional[str] = None
        executed_any_action = self._execute_tool_calls(action_text, context)
        
        # 回复
        for line in lines:
            match = REPLY_RE.fullmatch(line)
            if match:
                reply_text = match.group(1).strip() or None
                break

        if executed_any_action:
            self._last_plan_executed_action = True
            self._last_execution_source = "model_tool"
            return reply_text
        if self._proposal_attempted:
            self._last_execution_source = "proposal_rejected"
            return reply_text

        if reply_text:
            return reply_text
        if action_text.lower() == "finish()":
            return None
        if action_text:
            self._last_protocol_error = "planner response contains unsupported top-level syntax"
            return None
        logger.info("[Planner] 未匹配到动作，作为回复: %s", plan_text[:50])
        return plan_text

    def _execute_direct_intent_fallback(self, sender: str, message: str, context: dict) -> bool:
        compact = re.sub(r"\s+", "", message)
        lowered = compact.casefold()
        negated_stop = any(phrase in compact for phrase in ("不要停", "别停", "不用停", "无需停止")) \
            or re.search(r"\b(?:do\s+not|don't|dont)\s+stop\b", message, re.IGNORECASE)
        if not negated_stop and (
            any(phrase in compact for phrase in ("停下", "停止", "别跟了", "先别做了"))
            or re.search(r"\bstop\b", message, re.IGNORECASE)
        ):
            accepted = self._emit_skill("core.stop", {}, "direct_intent_fallback")
            self._last_plan_executed_action = accepted
            self._last_execution_source = "direct_intent_fallback" if accepted else "none"
            logger.info("[Planner] 直接意图兜底: stop")
            return accepted

        if "跟着" in compact or "跟随" in compact:
            target = self._resolve_follow_target(sender, compact, lowered, context)
            if target:
                if self._is_duplicate_task(context, {"follow"}, target):
                    return False
                accepted = self._emit_skill(
                    "general.follow_player", {"player": target}, "direct_intent_fallback",
                )
                self._last_plan_executed_action = accepted
                self._last_execution_source = "direct_intent_fallback" if accepted else "none"
                logger.info("[Planner] 直接意图兜底: follow %s", target)
                return accepted

        if any(verb in compact for verb in ("制作", "合成", "做一个", "做个", "做一把", "做把")):
            for alias in sorted(ITEM_ALIASES, key=len, reverse=True):
                if alias in compact:
                    item = self._normalize_item_name(alias)
                    if self._is_duplicate_task(context, {"craft"}, item):
                        return False
                    accepted = self._emit_skill(
                        "general.craft_item", {"item": item, "count": 1}, "direct_intent_fallback",
                    )
                    self._last_plan_executed_action = accepted
                    self._last_execution_source = "direct_intent_fallback" if accepted else "none"
                    logger.info("[Planner] 直接意图兜底: craft %s", alias)
                    return accepted
        return False

    def _resolve_follow_target(self, sender: str, compact: str, lowered: str, context: dict) -> Optional[str]:
        if "跟着我" in compact or "跟随我" in compact:
            return sender
        roster: list[str] = []
        for player in context.get("online_players", []):
            if isinstance(player, dict) and player.get("name"):
                roster.append(str(player["name"]))
        for entity in context.get("entities", []):
            if isinstance(entity, dict) and entity.get("type") == "player" and entity.get("name"):
                roster.append(str(entity["name"]))
        for name in dict.fromkeys(roster):
            if name.casefold() in lowered:
                return name
        match = re.search(r"(?:跟着|跟随)([A-Za-z0-9_]{1,32})", compact, re.IGNORECASE)
        if not match:
            return None
        requested = match.group(1)
        return next((name for name in roster if name.casefold() == requested.casefold()), requested)
    
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
        seen_calls: set[str] = set()
        for line in text.splitlines():
            match = TOOL_CALL_RE.fullmatch(line)
            if not match:
                continue
            tool_name = match.group(1).strip().lower()
            payload_text = match.group(2).strip()
            try:
                payload = json.loads(payload_text)
            except json.JSONDecodeError:
                logger.warning("[Planner] 无法解析 tool 调用参数: %s", payload_text[:120])
                continue

            call_key = json.dumps(
                {"tool": tool_name, "payload": payload},
                ensure_ascii=True, sort_keys=True, separators=(",", ":"),
            )
            if call_key in seen_calls:
                logger.info("[Planner] 忽略同一规划中的重复工具调用: %s", tool_name)
                continue
            seen_calls.add(call_key)
            if self._dispatch_tool_call(tool_name, payload, context):
                return True
            if self._proposal_attempted:
                return False
        return executed

    def _dispatch_tool_call(self, tool_name: str, payload: dict, context: dict) -> bool:
        if tool_name == "move_to":
            if {"x", "y", "z"}.issubset(payload):
                return self._emit_skill("core.move_to", {
                    "x": float(payload["x"]), "y": float(payload["y"]), "z": float(payload["z"]),
                }, "model_tool")
        elif tool_name == "follow":
            player = payload.get("player") or payload.get("player_name")
            if player and not self._is_duplicate_task(context, {"follow"}, str(player)):
                return self._emit_skill("general.follow_player", {"player": str(player)}, "model_tool")
        elif tool_name == "attack":
            return self._emit_skill("core.attack", {}, "model_tool")
        elif tool_name == "mine_block":
            return self._emit_skill("core.mine_block", {}, "model_tool")
        elif tool_name == "place_block":
            return self._emit_skill("legacy.place_block", {}, "model_tool")
        elif tool_name in {"craft", "craft_item"}:
            item = payload.get("item") or payload.get("item_name")
            normalized_item = self._normalize_item_name(str(item)) if item else ""
            if item and not self._is_duplicate_task(context, {"craft"}, normalized_item):
                return self._emit_skill("general.craft_item", {
                    "item": normalized_item, "count": int(payload.get("count", 1)),
                }, "model_tool")
        elif tool_name in {"collect", "collect_blocks"}:
            block_type = payload.get("block_type") or payload.get("block")
            normalized_block = self._normalize_block_name(str(block_type)) if block_type else ""
            if block_type and not self._is_duplicate_task(context, {"collect"}, normalized_block):
                return self._emit_skill("general.collect_blocks", {
                    "block_type": normalized_block, "count": int(payload.get("count", 1)),
                }, "model_tool")
        elif tool_name == "get_inventory":
            return self._emit_skill("legacy.get_inventory", {}, "model_tool")
        elif tool_name == "explore":
            return self._emit_skill("general.explore", {"radius": int(payload.get("radius", 16))}, "model_tool")
        elif tool_name == "trade":
            villager = payload.get("villager_type") or payload.get("type")
            if villager:
                return self._emit_skill("legacy.trade", {"villager_type": str(villager)}, "model_tool")
        elif tool_name == "sleep":
            return self._emit_skill("legacy.sleep", {}, "model_tool")
        elif tool_name == "eat":
            return self._emit_skill("general.eat", {}, "model_tool")
        elif tool_name == "drop_item":
            item = payload.get("item")
            if item:
                return self._emit_skill("inventory.drop_item", {
                    "item": str(item), "count": int(payload.get("count", 1)),
                }, "model_tool")
        elif tool_name == "sort_inventory":
            return self._emit_skill("legacy.sort_inventory", {}, "model_tool")
        elif tool_name == "build":
            if {"x", "y", "z", "structure"}.issubset(payload):
                return self._emit_skill("legacy.build", {
                    "x": float(payload["x"]), "y": float(payload["y"]), "z": float(payload["z"]),
                    "structure": str(payload["structure"]),
                }, "model_tool")
        elif tool_name == "stop":
            return self._emit_skill("core.stop", {}, "model_tool")
        self._proposal_attempted = True
        self._last_protocol_error = f"unsupported planner tool: {tool_name}"
        return False

    def _emit_skill(self, skill_id: str, input_data: dict[str, Any], source: str) -> bool:
        if self._proposal_emitted:
            return False
        proposal = SkillProposal(skill_id, dict(input_data), source)
        key = json.dumps(proposal.public_dict(), ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        if key in self._seen_proposals:
            return False
        self._seen_proposals.add(key)
        self._proposal_attempted = True
        if self._proposal_dispatcher is None:
            self._last_protocol_error = "planner proposal dispatcher is not configured"
            return False
        try:
            accepted = bool(self._proposal_dispatcher(proposal))
        except Exception as exc:
            self._last_protocol_error = str(exc)[:500]
            return False
        if not accepted:
            self._last_protocol_error = "planner proposal was rejected by admission"
            return False
        self._proposal_emitted = True
        return True

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
        with self._planning_lock:
            self._generation += 1
            self._is_planning = False
            self._last_protocol_error = "planner result invalidated by newer control intent"
        logger.info("[Planner] 规划被中断")
    
    def get_status(self) -> dict:
        return {
            "is_planning": self._is_planning,
            "last_plan_time": self._last_plan_time,
            "last_plan_preview": self._last_plan_preview,
            "last_plan_executed_action": self._last_plan_executed_action,
            "last_execution_source": self._last_execution_source,
            "last_protocol_error": self._last_protocol_error,
            "last_proposal_attempted": self._proposal_attempted,
        }
