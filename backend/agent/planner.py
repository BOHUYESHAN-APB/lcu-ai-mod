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

# 两阶段决策：planner 产出候选动作，decider 打分并选一个。
# 置信度低于该阈值时不下发动作，交给 P0/P1 反射层与下一轮兜底。
DECISION_CONFIDENCE_THRESHOLD = 0.6
# decider 必须快：超时即放弃，否则它自己会变成新的延迟瓶颈。
DECIDER_TIMEOUT_SECONDS = 8.0
DECIDER_MAX_TOKENS = 32
DECIDER_TEMPERATURE = 0.2

# /locate 要走一趟"发命令 → 游戏回话 → 解析"，给一个够用但不至于把回合卡死的上限。
# 超时就**如实说没拿到**（绝不编坐标 —— 编出来的坐标会让它一头扎进错误的方向）。
LOCATE_TIMEOUT_SECONDS = 6.0
# 驻守的默认水平半径（用户口径：如 32 格）。
GUARD_DEFAULT_RADIUS = 32

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

# ── 动作 schema（按阶段分组）──────────────────────────────────
# 只把与当前阶段相关的动作注入 prompt：16 条全量内联对 32k 上下文是隐患，
# 且大部分动作与当前阶段无关。（见 ROADMAP M3-3）
# 每项：(动作名, 参数示例, 说明)
ACTION_GROUPS: dict[str, tuple[tuple[str, str, str], ...]] = {
    "move": (
        ("move_to", "x,y,z", "移动到坐标"),
        ("follow", "player", "跟随玩家"),
        ("explore", "radius", "探索半径"),
        ("stop", "", "停止当前动作"),
    ),
    "locate": (
        ("find_block", "block,radius,limit", "扫描附近有没有该方块（只查不走）"),
        ("go_to_block", "block,radius,remember", "扫附近找该方块并自动寻路过去；到达后记住它"),
        ("locate_structure", "name,kind", "用原版 /locate 查**结构/群系**坐标"
                                          "（kind=structure|biome，名字用 minecraft:village 这类 id）"),
    ),
    "gather": (
        ("mine_block", "", "挖掘面前或脚下的方块"),
        ("collect_blocks", "block_type,count", "采集方块"),
        ("drop_item", "item,count", "丢弃物品"),
        ("sort_inventory", "", "整理背包"),
        ("get_inventory", "", "查看背包"),
    ),
    "craft": (
        ("craft_item", "item,count", "合成物品"),
        ("place_block", "", "放置手中的方块"),
        ("build", "x,y,z,structure", "建造结构"),
    ),
    "combat": (
        ("attack", "", "攻击附近的敌人"),
    ),
    "items": (
        ("equip_weapon", "", "换上热栏里最好的武器（动手/挨打前先换）"),
        ("select_hotbar", "index", "切到热栏第 index 格（0-8，放方块/吃东西用）"),
    ),
    "survive": (
        ("eat", "", "吃东西"),
        ("sleep", "", "睡觉"),
        ("trade", "villager_type", "与村民交易"),
    ),
    "night": (
        ("sleep_in_bed", "", "走到最近的床并睡到天亮（只有夜里能睡）"),
    ),
    "guard": (
        ("guard", "x,y,z,radius", "在给定坐标的**水平半径**内驻守（默认 32 格，越界自动走回）；"
                                  "省略坐标＝用最近发话者发言时站的位置"),
        ("guard_off", "", "取消驻守"),
    ),
    "hud": (
        ("set_hud_note", "text", "把当前在干嘛写进 HUD 状态词后面的描述词（空字符串＝清空）"),
    ),
    "memory": (
        ("remember_poi", "name,category,x,y,z", "把当前位置/坐标记成记录点（持久记忆）"),
    ),
}

# task_state.kind → 阶段
_KIND_TO_PHASE = {
    "craft": "craft",
    "collect": "gather",
    "follow": "travel",
    "eat": "survive",
}

# 阶段 → 注入哪些组（顺序即 prompt 中的展示顺序）
# night / guard / hud / memory 全局注入：睡觉、守点、HUD 描述词与记忆点在任何阶段
# 都可能有意义（用户说"在这儿守着"时他可能正在看着 AI 挖矿）。
PHASE_GROUPS: dict[str, tuple[str, ...]] = {
    "idle": ("move", "locate", "gather", "craft", "guard", "items", "survive", "combat", "hud", "night", "memory"),
    "travel": ("move", "locate", "combat", "guard", "items", "survive", "hud", "night", "memory"),
    "gather": ("move", "locate", "gather", "guard", "items", "craft", "hud", "night", "memory"),
    "craft": ("move", "locate", "craft", "guard", "items", "gather", "hud", "night", "memory"),
    "survive": ("survive", "hud", "night", "memory", "guard", "items", "move", "locate"),
}

_TERMINAL_STATUSES = {"", "idle", "done", "failed", "cancelled", "error"}


def _phase_from_task(task: Any) -> str:
    """从 task_state 推断规划阶段；终态或无任务 → idle。"""
    if not isinstance(task, dict):
        return "idle"
    status = str(task.get("status", "idle")).strip().lower()
    if status in _TERMINAL_STATUSES:
        return "idle"
    return _KIND_TO_PHASE.get(str(task.get("kind", "")).strip().lower(), "idle")


def _actions_for_phase(phase: str) -> list[tuple[str, str, str]]:
    """当前阶段该注入的动作（去重、保持顺序）。"""
    groups = PHASE_GROUPS.get(phase, PHASE_GROUPS["idle"])
    seen: set[str] = set()
    out: list[tuple[str, str, str]] = []
    for group in groups:
        for action in ACTION_GROUPS.get(group, ()):
            if action[0] not in seen:
                seen.add(action[0])
                out.append(action)
    return out


def _render_actions(actions: list[tuple[str, str, str]]) -> str:
    """紧凑渲染：`- name(args): 说明`（无参数时省略括号）。"""
    return "\n".join(
        f"- {name}({args}): {desc}" if args else f"- {name}: {desc}"
        for name, args, desc in actions
    )


# 中文方块名 → Java 侧可匹配的方块 id / 通配串。
# 通配形式（`*_bed` 后缀、`*chest*` 包含、`oak_*` 前缀）由 Java 侧扫描实现。
# 注意：不要用裸 `bed`（会误匹配 bedrock 基岩），床一律用 `*_bed`。
BLOCK_ALIASES = {
    "木头": "oak_log",
    "原木": "oak_log",
    "木板": "oak_planks",
    "圆石": "cobblestone",
    "煤": "coal_ore",
    "铁": "iron_ore",
    "床": "*_bed",
    "工作台": "*crafting_table*",
    "熔炉": "*furnace",
    "箱子": "*chest*",
    "水": "minecraft:water",
    "矿": "*iron_ore*",
}


def _as_optional_float(value: Any) -> Optional[float]:
    """坐标等可选数值：缺失/空串/非法 → None（Java 侧回退到当前脚下）。"""
    if value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# ── /locate 的查询词与回复解析 ────────────────────────────────
# 目标：模型说"找村庄"时发的是 `locate structure minecraft:village`，而不是被写死成
# 某一个结构 —— **具体资源名由模型给**，这里只做语言翻译与命名空间规范化。

STRUCTURE_ALIASES = {
    "村庄": "minecraft:village",
    "村": "minecraft:village",
    "下界要塞": "minecraft:fortress",
    "地狱要塞": "minecraft:fortress",
    "要塞": "minecraft:stronghold",
    "废弃矿井": "minecraft:mineshaft",
    "矿井": "minecraft:mineshaft",
    "沙漠神殿": "minecraft:desert_pyramid",
    "丛林神庙": "minecraft:jungle_pyramid",
    "海底神殿": "minecraft:monument",
    "海底遗迹": "minecraft:monument",
    "女巫小屋": "minecraft:swamp_hut",
    "林地府邸": "minecraft:mansion",
    "掠夺者前哨站": "minecraft:pillager_outpost",
    "前哨站": "minecraft:pillager_outpost",
    "远古城市": "minecraft:ancient_city",
    "埋藏的宝藏": "minecraft:buried_treasure",
    "沉船": "minecraft:shipwreck",
    "海底废墟": "minecraft:ocean_ruin",
    "废弃传送门": "minecraft:ruined_portal",
    "末地城": "minecraft:end_city",
    "末地船": "minecraft:end_city",
    "试炼密室": "minecraft:trial_chambers",
}

BIOME_ALIASES = {
    "平原": "minecraft:plains",
    "沙漠": "minecraft:desert",
    "森林": "minecraft:forest",
    "丛林": "minecraft:jungle",
    "沼泽": "minecraft:swamp",
    "海洋": "minecraft:ocean",
    "雪原": "minecraft:snowy_plains",
    "蘑菇岛": "minecraft:mushroom_fields",
    "樱花树林": "minecraft:cherry_grove",
    "繁花森林": "minecraft:flower_forest",
}

# /locate 的回复形如：
#   EN: The nearest minecraft:village is at [123, 64, -456] (789 blocks away)
#   CN: 最近的 minecraft:village 位于 [123, 64, -456]（789 格外）
# 兼容圆括号/方括号/全角逗号与 y 上的 `~`（个别服务端会带相对值符号）。
LOCATE_COORD_RE = re.compile(
    r"[\[\(【]\s*(-?\d+(?:\.\d+)?)\s*[,，]\s*~?\s*(-?\d+(?:\.\d+)?)\s*[,，]\s*(-?\d+(?:\.\d+)?)\s*[\]\)】]"
)

# 游戏明说"附近没有"时的中英文话术（1.19+ 是 "Unable to locate any …"）。
LOCATE_FAILURE_MARKERS = (
    "unable to locate",
    "could not find",
    "couldn't find",
    "cannot find",
    "no structure",
    "not found",
    "无法找到",
    "无法定位",
    "找不到",
    "没有找到",
)

# 带坐标的**非系统**消息里，还得有这些线索才认（避免玩家顺手打一句 `[1,2,3]` 被当成查询结果）。
LOCATE_HINTS = ("locate", "structure", "biome", "结构", "群系", "位于", "is at", "found")


def normalize_locate_name(name: Any, kind: str = "structure") -> str:
    """把模型给的结构/群系名整理成 /locate 能吃的 id。

    - 中文口语名走别名表（`村庄` → `minecraft:village`）；
    - 裸 id 补命名空间（`village` → `minecraft:village`）；
    - `#xxx` 保持原版标签形式（`/locate structure #minecraft:village` 也合法）。
    """
    text = str(name or "").strip().strip('"').strip("'")
    while text.startswith("/"):
        text = text[1:].strip()
    if not text:
        return ""
    aliases = BIOME_ALIASES if str(kind).strip().lower() == "biome" else STRUCTURE_ALIASES
    tokens = text.split()
    for candidate in (text, tokens[-1] if tokens else text):
        mapped = aliases.get(candidate) or aliases.get(candidate.lower())
        if mapped:
            return mapped
    # 模型可能把整句抄进来（`/locate structure minecraft:village`）→ 取最后一段当 id。
    text = tokens[-1] if tokens else text
    if text.startswith("#"):
        body = text[1:]
        return "#" + (body if ":" in body else f"minecraft:{body}")
    if ":" in text:
        return text
    return f"minecraft:{text}"


def parse_locate_reply(text: Any) -> Optional[tuple[float, float, float]]:
    """从 /locate 的回复里抠出坐标；抠不到返回 None（**绝不猜**）。"""
    match = LOCATE_COORD_RE.search(str(text or ""))
    if not match:
        return None
    try:
        return (float(match.group(1)), float(match.group(2)), float(match.group(3)))
    except ValueError:
        return None


def is_locate_failure(text: Any) -> bool:
    """游戏明说"附近没有"（区别于"我们没看懂"或"没等到"）。"""
    lowered = str(text or "").lower()
    return any(marker in lowered for marker in LOCATE_FAILURE_MARKERS)


def looks_like_locate_reply(entry: dict) -> bool:
    """一条聊天事件像不像 /locate 的回复。

    系统消息（/locate 的回复本就是系统消息）带括号坐标即认；玩家消息则要求同时出现
    "结构/群系/位于"这类线索 —— 判定窗口只有发命令之后的那几秒，不追求万无一失。
    """
    if not isinstance(entry, dict):
        return False
    message = str(entry.get("message") or "")
    if is_locate_failure(message):
        return True
    if parse_locate_reply(message) is None:
        return False
    if entry.get("is_system"):
        return True
    lowered = message.lower()
    return any(hint in lowered for hint in LOCATE_HINTS)


class Planner:
    """
    规划器：决定 AI 应该做什么。
    
    设计参考 MaiBot 的两阶段推理：
    1. Timing Gate: 是否应该回复？
    2. Planner: 回复什么？做什么？
    """
    
    def __init__(self, llm_service, memory, skills, threshold_provider=None, chat_inbox=None):
        self.llm = llm_service
        self.memory = memory
        self.skills = skills
        self._is_planning = False
        self._last_plan_time = 0.0
        self._last_decision: dict = {}
        # 置信度阈值来源（可注入）。默认用模块常量；注入后可在 UI 改并即时生效。
        self._threshold_provider = threshold_provider
        # /locate 要等聊天回复：注入 Session 的 ChatInbox（读 wire 读取线程喂的那份）。
        self.chat_inbox = chat_inbox
        # 本轮的 /locate 结果（`_locate_note()` 会把它如实附到回复后面）。
        self._last_locate_result: Optional[dict] = None

    def _threshold(self) -> float:
        """当前 decider 置信度阈值：读注入的 provider，失败回退默认常量。"""
        if self._threshold_provider is not None:
            try:
                return min(max(float(self._threshold_provider()), 0.0), 1.0)
            except Exception as e:
                logger.warning("[Planner] 阈值读取失败(%s)，回退默认 %.2f",
                               e, DECISION_CONFIDENCE_THRESHOLD)
        return DECISION_CONFIDENCE_THRESHOLD
    
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
        # 本轮 /locate 的结果从零开始记（避免上一轮的坐标串到这一轮的回复里）。
        self._last_locate_result = None

        try:
            # 第一阶段：planner 结合状态产出候选动作（JSON）
            prompt = self._build_planner_prompt(sender, message, context, bot_name)
            result = self.llm.chat([{"role": "user", "content": prompt}], agent="planner")
            plan_text = result.get("content", "")
            
            if not plan_text:
                logger.warning("[Planner] LLM 返回空内容")
                return None
            
            logger.info("[Planner] LLM 规划: %s", plan_text[:100])
            
            plan = self._parse_plan_json(plan_text)
            if plan is None:
                # 模型没按候选 JSON 契约输出 → 回退旧的自由文本解析
                logger.info("[Planner] 输出非候选 JSON，回退自由文本解析")
                return self._with_locate_note(
                    self._execute_plan(plan_text, sender, message, context))
            
            # 第二阶段：decider 打分并选出一个候选
            return self._with_locate_note(self._execute_decision(plan, context))
            
        except Exception as e:
            logger.error("[Planner] 规划失败: %s", e)
            return None
        finally:
            self._is_planning = False

    def _with_locate_note(self, reply: Optional[str]) -> Optional[str]:
        """把本轮 /locate 的**真实结果**附到回复后面。

        坐标只有等游戏回话才知道，模型不可能提前写进 reply；不附的话它就只能含糊其辞，
        甚至会为了"像个玩家"编一个坐标。查不到/超时也照实说 —— 不许猜位置。
        """
        result = self._last_locate_result
        if not result:
            return reply
        command = str(result.get("command") or "locate …")
        if result.get("ok"):
            note = (f"（我查了 /{command} → 最近的 {result.get('query')} 在 "
                    f"[{result.get('x'):.0f}, {result.get('y'):.0f}, {result.get('z'):.0f}]）")
        else:
            reason = str(result.get("reason") or "unknown")
            honest = {
                "timeout": "没等到游戏的回复（或回复格式不认得），坐标我不敢乱报",
                "not_found": "游戏说附近没有",
                "empty_name": "没告诉我要查什么",
                "no_chat_inbox": "没有聊天通道，等不到回复",
            }.get(reason, reason)
            note = f"（我查了 /{command}：{honest}）"
        if not reply:
            return note.strip("（）")
        return f"{reply}\n{note}"
    
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

        vision_text = (context.get("vision") or "").strip() if isinstance(context, dict) else ""
        if not vision_text:
            vision_text = "无（视觉未开启）"

        known_poi_text = (context.get("known_poi") or "").strip() if isinstance(context, dict) else ""
        if not known_poi_text:
            known_poi_text = "暂无"

        # 时间 / 夜晚 / 能否睡觉：state_update 顶层字段（也兼容塞在 world 里）。
        world_ctx = context.get("world", {}) if isinstance(context, dict) else {}
        if not isinstance(world_ctx, dict):
            world_ctx = {}

        def _ctx_value(key):
            if isinstance(context, dict) and context.get(key) is not None:
                return context.get(key)
            return world_ctx.get(key)

        is_night = bool(_ctx_value("is_night"))
        day_time = _ctx_value("day_time")
        sky_darken = _ctx_value("sky_darken")
        can_sleep = bool(_ctx_value("can_sleep"))
        night_text = "是夜晚" if is_night else "是白天"
        day_time_text = str(day_time) if day_time is not None else "未知"
        sky_darken_text = str(sky_darken) if sky_darken is not None else "未知"
        sleepable_text = "现在能睡觉" if can_sleep else "现在不能睡觉"

        nearby_beds = context.get("nearby_beds", []) if isinstance(context, dict) else []
        nearby_bed_text = ", ".join(
            f"{bed.get('id', bed.get('block_id', '?'))}"
            f"@({bed.get('x')},{bed.get('y')},{bed.get('z')}) {bed.get('distance', '?')}m"
            for bed in nearby_beds[:4] if isinstance(bed, dict)
        ) or "暂无"

        remembered_pois = context.get("remembered_pois", []) if isinstance(context, dict) else []
        remembered_text = ", ".join(
            f"{poi.get('name') or poi.get('id', '?')}"
            f"[{poi.get('category') or 'waypoint'}]"
            f"@({poi.get('x')},{poi.get('y')},{poi.get('z')})"
            for poi in remembered_pois[:16] if isinstance(poi, dict)
        ) or "暂无"

        # 在线玩家 + 最近聊天：判断"他在跟谁说话"靠的是这两块，而不是每次都要喊名字。
        last_speaker = context.get("last_speaker") if isinstance(context, dict) else None
        last_speaker = last_speaker if isinstance(last_speaker, dict) else {}
        players = context.get("players") if isinstance(context, dict) else None
        players = players if isinstance(players, list) else []
        players_text = "\n".join(
            "- {name}{where}{who}".format(
                name=player.get("name", "?"),
                where=f"（距你 {float(player.get('distance')):.1f}m）"
                if isinstance(player.get("distance"), (int, float)) else "",
                who=" ← 最近跟你说话的就是他" if player.get("speaking") else "",
            )
            for player in players[:8] if isinstance(player, dict)
        ) or "（视野里没有别的玩家）"

        recent_chat = context.get("recent_chat") if isinstance(context, dict) else None
        recent_chat = recent_chat if isinstance(recent_chat, list) else []
        now = time.time()

        def _ago(entry: dict) -> str:
            stamp = entry.get("time")
            if not isinstance(stamp, (int, float)):
                return "?"
            delta = max(0.0, now - float(stamp))
            if delta < 3:
                return "刚刚"
            if delta < 60:
                return f"{delta:.0f}秒前"
            return f"{delta / 60:.0f}分钟前"

        recent_chat_text = "\n".join(
            "({when}) {sender}{dist}: {message}".format(
                when=_ago(entry),
                sender="[系统]" if entry.get("is_system") else (entry.get("sender") or "?"),
                dist=f"（距你 {float(entry.get('sender_distance')):.1f}m）"
                if isinstance(entry.get("sender_distance"), (int, float)) else "",
                message=entry.get("message", ""),
            )
            for entry in recent_chat[-8:] if isinstance(entry, dict)
        ) or "（还没人说过话）"

        speaker_line = ""
        if last_speaker.get("name"):
            where = ""
            if isinstance(last_speaker.get("x"), (int, float)) and isinstance(last_speaker.get("z"), (int, float)):
                where = f"，说话时站在 ({float(last_speaker['x']):.0f}, {float(last_speaker.get('y') or 0):.0f}, {float(last_speaker['z']):.0f})"
            speaker_line = f"- 最近发话者：{last_speaker['name']}{where}\n"

        # 人设三段 + 与 identity 分离的 state_block：**四个独立块**，禁止混写
        # （设计文档 §3：人设不得被临时状态污染）。没配置三段时退回 legacy 两行。
        persona_blocks = context.get("persona_blocks") if isinstance(context, dict) else None
        persona_blocks = persona_blocks if isinstance(persona_blocks, dict) else {}
        identity_block = str(persona_blocks.get("identity") or "").strip()
        reply_style_block = str(persona_blocks.get("reply_style") or "").strip()
        scene_rules_block = str(persona_blocks.get("scene_rules") or "").strip()
        state_block = str(persona_blocks.get("state_block") or "").strip()
        if identity_block or reply_style_block or scene_rules_block:
            persona_section = (
                "你的人设（下面三块各管一件事，别把状态写进身份、也别在三处重复同一句话）：\n"
                f"【身份】{identity_block or '（未配置）'}\n"
                f"【表达习惯】{reply_style_block or '（未配置）'}\n"
                f"【场景规则】{scene_rules_block or '（未配置）'}\n"
                f"【此刻状态（会变，不是身份）】{state_block or '（无）'}\n"
            )
        else:
            persona_section = (f"当前人设：\n- 性格：{personality}\n"
                               f"- 说话风格：{speaking_style}\n")

        # 对话记忆（按服务器分区的 L1/L2/L3）：死亡、进出也在同一份记忆里。
        conversation = context.get("conversation") if isinstance(context, dict) else None
        conversation = conversation if isinstance(conversation, dict) else {}

        def _ago_ts(entry: dict) -> str:
            stamp = entry.get("ts") if isinstance(entry, dict) else None
            if not isinstance(stamp, (int, float)):
                return "?"
            delta = max(0.0, now - float(stamp))
            if delta < 3:
                return "刚刚"
            if delta < 60:
                return f"{delta:.0f}秒前"
            return f"{delta / 60:.0f}分钟前"

        memory_lines = []
        for entry in (conversation.get("recent") or [])[-12:]:
            if not isinstance(entry, dict):
                continue
            kind = str(entry.get("type") or "chat")
            who = str(entry.get("sender") or "?")
            text = str(entry.get("message") or "")
            if kind == "chat":
                memory_lines.append(f"({_ago_ts(entry)}) {who}: {text}")
            else:
                memory_lines.append(f"({_ago_ts(entry)}) [{'我' if entry.get('is_self') else kind}] {who} {text}".strip())
        memory_text = "\n".join(memory_lines) or "（本服务器还没有对话记录）"
        keywords = conversation.get("keywords") or []
        keywords_text = ", ".join(str(k) for k in keywords[:12]) or "无"
        summary_text = str(conversation.get("summary") or "").strip() or "（还没到需要摘要的量）"
        namespace = str(conversation.get("namespace") or "unknown")

        # 外部事件注入：不是"必发消息"，模型完全可以 no_action。
        event = context.get("event") if isinstance(context, dict) else None
        event = event if isinstance(event, dict) else {}
        event_section = ""
        event_rule = ""
        if event:
            event_section = (
                "\n外部事件（**不是**玩家直接对你说的话，只是世界里发生了一件事）：\n"
                f"{json.dumps(event, ensure_ascii=False)}\n"
                "要不要说点什么完全由你决定：不想说就把 reply 留空、candidates 给空数组——"
                "硬找话聊比不说话更糟。\n"
            )
            event_rule = ("12. 上面有外部事件时：先判断值不值得开口（有人死了/掉到好东西/第一次到新地方"
                          "才值得调侃一句），不值得就不说话；别把事件当指令去干活\n")

        # 饥饿/饱和度：自然回血要"饱食度 ≥18 且饱和度 >0"，所以缺血时不能等饿到见底才吃。
        player_ctx = context.get("player", {}) if isinstance(context, dict) else {}
        if not isinstance(player_ctx, dict):
            player_ctx = {}
        hunger_value = _ctx_value("food") if _ctx_value("food") is not None else player_ctx.get("hunger")
        saturation_value = _ctx_value("saturation")
        if saturation_value is None:
            saturation_value = player_ctx.get("saturation")
        health_value = player_ctx.get("health")
        max_health_value = player_ctx.get("max_health", 20)
        hunger_text = str(hunger_value) if hunger_value is not None else "未知"
        saturation_text = str(saturation_value) if saturation_value is not None else "未知"
        health_text = (f"{health_value}/{max_health_value}"
                       if health_value is not None else "未知")

        # 只注入与当前阶段相关的动作，避免 16 条全量内联挤爆 32k 上下文。
        phase = _phase_from_task(context.get("task_state") if isinstance(context, dict) else None)
        action_text = _render_actions(_actions_for_phase(phase))

        prompt = f"""你是 {persona_name}，一个正在玩 Minecraft 的玩家。你和服务器里的其他玩家一起玩。

当前状态：
- 发送者：{sender}
- 消息：{message}
{event_section}
在线玩家（按距离，标记出最近跟你说话的那个人）：
{players_text}
{speaker_line}
最近聊天上下文（按时间从早到晚；"刚刚/N秒前"是距现在多久。判断"这话是不是对你说的/在跟谁说"看这里，
不一定要有人喊你的名字才算对你说）：
{recent_chat_text}

最近对话：
{recent_text}

记忆上下文：
{memory_context.get('interaction_summary', '无')}

对话记忆（服务器 {namespace}；L2 窗口 + L3 中期摘要 + 关键词。
判断"他之前说过什么/这个服务器上发生过什么"看这里）：
{memory_text}
中期摘要：{summary_text}
关键词：{keywords_text}

{persona_section}
当前任务状态：
{context.get('task_state', {'kind': 'idle', 'status': 'idle'})}

饥饿与回血（饥饱两条独立计量，先看这两行再决定要不要吃）：
- 血量 {health_text}｜饱食度 {hunger_text}/20｜饱和度 {saturation_text}（饱和度是隐藏值）
- 自然回血条件：饱食度 ≥18 **且** 饱和度 >0 —— 这时血量才会慢慢回升；饱食度 <18 或饱和度为 0 都不回血
- 所以：血量不满就该主动补（吃到饱食度 18+），别等饿到见底；缺血时优先**高饱和度**食物（金苹果/熟肉/面包），
  单纯饿（血是满的）优先**高营养**食物；生食是最后手段，腐肉/河豚/毒马铃薯/蜘蛛眼这类有害食物永远别自己吃
- 热栏里没有吃的就先找/做，别对着空气 use_item

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

已知箱子（历史记忆，可能不在当前视野内）：
{known_poi_text}

时间与睡眠：
- 现在是{night_text}（sky_darken={sky_darken_text}，{sleepable_text}）
- day_time={day_time_text} 只是 gameTime 取模的近似值，不准，判断白天黑夜一律看"是否夜晚"
- 附近的床：{nearby_bed_text}

已知记录点（你自己记住的持久记忆，可能在很远的地方）：
{remembered_text}

附近实体：
{nearby_entity_text}

画面观察（来自截图，可能略有延迟）：
{vision_text}

可用动作（"tool" 必须精确用下列名称；括号内是参数名）：
{action_text}

规则：
1. 你是真实玩家，不是 AI；说话自然简短，直接做事
2. 参数用真实值，禁止"附近""随机位置"这类占位词
3. 用户要求停止时优先给 stop；当前任务非 idle 且目标未变时不要重复下发同一任务
4. 到了晚上又没事可做，且附近或记忆里有床 → 给 sleep_in_bed（它会自己走过去并睡下，别干等到天亮）
5. 用户说"找床/找箱子/找某某方块"时，用 go_to_block（自己会寻路过去），不要用 explore
6. 用户说"记住这里/保存一个记录点"时，用 remember_poi
7. 判断在跟谁说话：看最近聊天上下文 + 在线玩家的 ← 标记；对你说话不一定喊名字，
   谁刚在跟你讲、谁离你最近，就用谁的话当指令
8. "守住这里/在这儿等着/别走开" → guard（别自己编坐标，用最近发话者发言时站的位置 + 水平半径 32 格）；
   "不用守了" → guard_off
9. 要找结构/群系 → locate_structure（走原版 /locate，名字用 minecraft:village 这类 id，别写死成某一个）；
   坐标要等游戏回话才知道，所以 reply 里**别写坐标**，只说"去查一下"，真实坐标后端会补在你话后面
10. 有人问"在干嘛/要去哪" → 顺手用 set_hud_note 把当前打算写进 HUD 描述词
11. 回复必须和实际动作一致：写进 reply 的事必须真的作为候选下发；没下发的动作不要写进 reply
{event_rule}
给 1-4 个候选动作（按偏好排序，没有合适的就给空数组），只输出合法 JSON（无 markdown、无多余文字）：
"过来" → {{"reply":"来了","candidates":[{{"tool":"move_to","args":{{"x":100,"y":64,"z":-200}},"why":"靠近"}}]}}
"别跟着" → {{"reply":"行","candidates":[{{"tool":"stop","args":{{}},"why":"用户要求停止"}}]}}
"""

        return prompt

    # ── 两阶段决策：第二阶段（decider 打分选择） ──────────────────

    def _execute_decision(self, plan: dict, context: dict) -> Optional[str]:
        """用 decider 从 planner 给出的候选里选一个执行。

        只看置信度：低于 DECISION_CONFIDENCE_THRESHOLD 就不下发动作，把这一轮
        让给 P0/P1 反射层与下一轮规划，避免在没把握时乱动。
        """
        reply_text = plan.get("reply") or None
        candidates = [c for c in plan.get("candidates", [])
                      if isinstance(c, dict) and c.get("tool")]

        if not candidates:
            self._last_decision = {"source": "planner_only", "reason": "no candidates"}
            logger.info("[Planner] 无候选动作，只回复")
            return reply_text

        choice, confidence = self._decide(candidates, context)
        threshold = self._threshold()
        self._last_decision = {
            "source": "decider",
            "choice": choice,
            "confidence": round(confidence, 3),
            "candidate_count": len(candidates),
            "threshold": threshold,
        }

        if choice is None or confidence < threshold:
            logger.info(
                "[Planner] 决策置信度不足 (%.2f < %.2f)，不下发动作，交给反射层",
                confidence, threshold,
            )
            return reply_text

        candidate = candidates[choice]
        tool = str(candidate.get("tool", "")).strip().lower()
        args = candidate.get("args") if isinstance(candidate.get("args"), dict) else {}
        executed = self._dispatch_tool_call(tool, args, context)
        # 记进 last_decision，供 /api/status 与前端展示
        self._last_decision["tool"] = tool
        self._last_decision["executed"] = bool(executed)
        logger.info("[Planner] 决策执行 %s(%s) conf=%.2f -> %s",
                    tool, args, confidence, executed)
        return reply_text

    def _decide(self, candidates: list[dict], context: dict) -> tuple[Optional[int], float]:
        """调用 decider 角色选一个候选，返回 (编号, 置信度)。

        超时、报错或输出无法解析都视为"没有结论"，返回 (None, 0.0) 由调用方回退。
        """
        if not self.llm or not self.llm.api_key:
            return None, 0.0

        prompt = self._build_decider_prompt(candidates, context)
        try:
            result = self.llm.chat(
                [{"role": "user", "content": prompt}],
                agent="decider",
                temperature=DECIDER_TEMPERATURE,
                max_tokens=DECIDER_MAX_TOKENS,
                timeout=DECIDER_TIMEOUT_SECONDS,
                # 零思考：不发任何思考模式/深度字段，直接要一行 JSON。这是标准做法，
                # 不依赖专有决策模型（System One 是可选升级，见 docs/dev/ROADMAP.md）。
                thinking=False,
            )
        except Exception as e:
            logger.warning("[Planner] Decider 调用失败或超时: %s", e)
            return None, 0.0

        raw = (result.get("content") or "").strip()
        verdict = self._parse_decision(raw, len(candidates))
        if verdict is None:
            logger.warning("[Planner] Decider 输出无法解析: %s", raw[:80])
            return None, 0.0
        return verdict

    def _build_decider_prompt(self, candidates: list[dict], context: dict) -> str:
        """构建决策模型的极短提示：状态摘要 + 编号候选 + 单行 JSON 契约。"""
        player = context.get("player", {}) if isinstance(context, dict) else {}
        task_state = context.get("task_state", {}) if isinstance(context, dict) else {}
        speaker = context.get("last_speaker") if isinstance(context, dict) else None
        speaker_name = speaker.get("name") if isinstance(speaker, dict) else ""
        speaker_text = f"，最近发话者 {speaker_name}" if speaker_name else ""
        lines = []
        for i, candidate in enumerate(candidates):
            args = json.dumps(candidate.get("args", {}), ensure_ascii=False)
            lines.append(f"{i}. {candidate.get('tool')}({args}) — {candidate.get('why', '')}")
        options_text = "\n".join(lines)
        return f"""你是行动决策模型。请从下列候选动作中选出当前最优的一个，并给出置信度。

当前状态：血量 {player.get('health', '?')}/{player.get('max_health', 20)}，饥饿 {player.get('hunger', '?')}/20，饱和度 {player.get('saturation', '?')}，任务 {task_state.get('kind', 'idle')}/{task_state.get('status', 'idle')}{speaker_text}

候选动作：
{options_text}

只输出一行 JSON，不要解释，不要 markdown：
{{"choice": <候选编号>, "confidence": <0 到 1 之间的小数>}}
confidence 表示你对这个选择的确信程度；如果没有一个候选明显合适，就给低分。"""

    @staticmethod
    def _parse_plan_json(text: str) -> Optional[dict]:
        """从模型输出中抽取候选 JSON；不符合契约返回 None。"""
        cleaned = text.strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```[a-zA-Z]*\s*", "", cleaned)
            cleaned = re.sub(r"\s*```$", "", cleaned).strip()
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            data = json.loads(cleaned[start:end + 1])
        except json.JSONDecodeError:
            return None
        if not isinstance(data, dict) or not isinstance(data.get("candidates"), list):
            return None
        return data

    @staticmethod
    def _parse_decision(text: str, candidate_count: int) -> Optional[tuple[int, float]]:
        """解析 decider 的单行 JSON，返回 (编号, 置信度)；越界或非法返回 None。"""
        if not text:
            return None
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            data = json.loads(text[start:end + 1])
            choice = int(data["choice"])
            confidence = float(data.get("confidence", 0.0))
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            return None
        if choice < 0 or choice >= candidate_count:
            return None
        return choice, max(0.0, min(1.0, confidence))

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
        elif tool_name == "find_block":
            block = payload.get("block") or payload.get("block_type")
            if block and str(block).strip():
                self.skills.find_block(
                    self._normalize_block_name(str(block)),
                    int(payload.get("radius", 32)),
                    int(payload.get("limit", 8)),
                )
                return True
        elif tool_name == "go_to_block":
            block = payload.get("block") or payload.get("block_type")
            if block and str(block).strip():
                normalized_block = self._normalize_block_name(str(block))
                if not self._is_duplicate_task(context, {"go_to_block"}, normalized_block):
                    self.skills.go_to_block(
                        normalized_block,
                        int(payload.get("radius", 32)),
                        bool(payload.get("remember", True)),
                    )
                    return True
        elif tool_name == "sleep_in_bed":
            self.skills.sleep_in_bed()
            return True
        elif tool_name == "guard":
            return self._dispatch_guard(payload, context)
        elif tool_name == "guard_off":
            self.skills.guard_off()
            return True
        elif tool_name == "locate_structure":
            name = (payload.get("name") or payload.get("structure")
                    or payload.get("biome") or payload.get("target"))
            kind = str(payload.get("kind") or ("biome" if payload.get("biome") else "structure"))
            result = self.locate_structure(str(name or ""), kind=kind)
            return bool(result.get("command"))
        elif tool_name == "set_hud_note":
            if "text" not in payload:
                return False
            seconds = payload.get("seconds")
            try:
                seconds = float(seconds) if seconds is not None else 30.0
            except (TypeError, ValueError):
                seconds = 30.0
            self.skills.set_hud_note(str(payload.get("text") or ""), seconds)
            return True
        elif tool_name == "remember_poi":
            name = str(payload.get("name") or "").strip()
            category = str(payload.get("category") or "").strip() or "waypoint"
            self.skills.remember_poi(
                name,
                category,
                _as_optional_float(payload.get("x")),
                _as_optional_float(payload.get("y")),
                _as_optional_float(payload.get("z")),
            )
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
        elif tool_name == "equip_weapon":
            self.skills.equip_weapon()
            return True
        elif tool_name == "select_hotbar":
            index = payload.get("index")
            if index is not None:
                self.skills.select_hotbar(int(index))
                return True
        elif tool_name == "build":
            if {"x", "y", "z", "structure"}.issubset(payload):
                self.skills.build(float(payload["x"]), float(payload["y"]), float(payload["z"]), str(payload["structure"]))
                return True
        elif tool_name == "stop":
            self.skills.stop_all()
            return True
        return False

    def _dispatch_guard(self, payload: dict, context: dict) -> bool:
        """驻守：坐标缺省用**最近发话者发言时站的位置**（用户口径），否则不下发。

        编坐标＝让它守一个不存在的地方，所以宁可什么都不做（返回 False，日志直说）。
        """
        speaker = context.get("last_speaker") if isinstance(context, dict) else None
        speaker = speaker if isinstance(speaker, dict) else {}
        x = _as_optional_float(payload.get("x"))
        y = _as_optional_float(payload.get("y"))
        z = _as_optional_float(payload.get("z"))
        if x is None:
            x = _as_optional_float(speaker.get("x"))
        if y is None:
            y = _as_optional_float(speaker.get("y"))
        if z is None:
            z = _as_optional_float(speaker.get("z"))
        if x is None or z is None:
            logger.warning("[Planner] guard 没有可用坐标（也没人刚说过话），不下发")
            return False
        radius = int(payload.get("radius") or GUARD_DEFAULT_RADIUS)
        self.skills.guard(x, y, z, radius)
        logger.info("[Planner] 驻守 (%.0f, %.0f, %.0f) r=%d", x, y if y is not None else 0.0, z, radius)
        return True

    def locate_structure(self, name: str, kind: str = "structure",
                         timeout: Optional[float] = None) -> dict:
        """用原版 `/locate` 查结构/群系坐标：发命令 → 等游戏回话 → 解析坐标。

        返回 {"ok", "reason", "command", "query", "text", "x"/"y"/"z"}。
        **查不到/超时一律 ok=False**，如实把原因交给 `_with_locate_note()` 说出去 ——
        编一个坐标比说"没查到"坏得多（它会照着一个假坐标走丢）。

        `kind` 为 "biome" 时走 `/locate biome`（结构/群系是同一个能力的两种查询）。
        """
        query = normalize_locate_name(name, kind)
        which = "biome" if str(kind).strip().lower() == "biome" else "structure"
        result: dict = {"ok": False, "reason": "empty_name", "command": "", "query": query, "text": ""}
        if not query:
            self._last_locate_result = result
            return result

        command = f"locate {which} {query}"
        result["command"] = command
        inbox = self.chat_inbox
        # 游标要在发命令**之前**取：只认这条命令之后到达的回复。
        cursor = inbox.cursor if inbox is not None else 0
        self.skills.run_command(command)
        if inbox is None:
            result["reason"] = "no_chat_inbox"
            self._last_locate_result = result
            logger.warning("[Planner] locate 没有聊天通道，等不到回复: /%s", command)
            return result

        wait = LOCATE_TIMEOUT_SECONDS if timeout is None else float(timeout)
        entry = inbox.wait_after(cursor, looks_like_locate_reply, wait)
        if entry is None:
            result["reason"] = "timeout"
            self._last_locate_result = result
            logger.warning("[Planner] locate 等回复超时(%.1fs): /%s", wait, command)
            return result

        text = str(entry.get("message") or "")
        result["text"] = text
        coords = parse_locate_reply(text)
        if coords is None:
            # 谓词只认"能抠出坐标"或"游戏的失败话术"，所以到这儿基本只剩后者。
            result["reason"] = "not_found"
            self._last_locate_result = result
            logger.info("[Planner] locate 没给出坐标(not_found): %s", text[:80])
            return result

        result.update({
            "ok": True,
            "reason": "ok",
            "x": coords[0],
            "y": coords[1],
            "z": coords[2],
        })
        self._last_locate_result = result
        logger.info("[Planner] locate %s → (%.0f, %.0f, %.0f)",
                    query, coords[0], coords[1], coords[2])
        return result

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
            "last_decision": self._last_decision,
            # 最近一次 /locate 的结果（查不到也如实记下来，便于排查"它怎么走错方向了"）。
            "last_locate": self._last_locate_result,
        }
