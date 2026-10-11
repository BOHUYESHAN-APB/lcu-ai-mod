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
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .borrow import BorrowLedger
from .craft import (ContainerPick, armor_in_containers, clamp_take, describe_missing,
                    evaluate_craft, is_armor, normalize_item, route_containers)
from .llm_service import prompt_version, render_persona_triple
from .permissions import CraftPolicy, TIER_GUEST

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
        ("stop", "", "停止动作"),
    ),
    "locate": (
        ("find_block", "block,radius,limit", "扫附近有无该方块（只查不走）"),
        ("go_to_block", "block,radius,remember", "寻路过去，到达后记住"),
        ("locate_structure", "name,kind", "/locate 查结构/群系（kind=structure|biome）"),
    ),
    "gather": (
        ("mine_block", "", "挖面前方块"),
        ("collect_blocks", "block_type,count", "采集方块"),
        ("drop_item", "item,count", "丢弃物品"),
        ("sort_inventory", "", "整理背包"),
        ("get_inventory", "", "查看背包"),
    ),
    "craft": (
        ("craft_item", "item,count", "合成物品"),
        ("craft_via_recipe", "item,count", "原版配方合成（先看背包材料）"),
        ("place_block", "", "放置手中方块"),
        ("build", "x,y,z,structure", "建造结构"),
    ),
    "container": (
        ("container_move", "from_slot,to_slot,count", "容器内移物（省 to_slot＝背包↔快捷栏）"),
        ("container_click", "slot,click_type", "点容器一格（pickup|quick_move|swap|throw）"),
        ("container_close", "", "关掉容器界面"),
        ("borrow_item", "item,count,container", "从仓库借（须授权；不给 count＝整箱搬，拒绝）"),
        ("return_item", "item,count,container", "放回原容器（省 count＝全部剩余）"),
    ),
    "combat": (
        ("attack", "", "攻击附近敌人"),
    ),
    "items": (
        ("equip_armor", "", "穿背包里的盔甲（没有就去容器找）"),
        ("equip_weapon", "", "换上热栏里最好的武器"),
        ("select_hotbar", "index", "切到热栏第 index 格（0-8）"),
    ),
    "survive": (
        ("eat", "", "吃东西"),
        ("sleep", "", "睡觉"),
        ("trade", "villager_type", "与村民交易"),
    ),
    "night": (
        ("sleep_in_bed", "", "走到最近的床睡到天亮（只能夜里）"),
    ),
    "guard": (
        ("guard", "x,y,z,radius", "水平半径内驻守（默认 32；省坐标＝最近发话者位置）"),
        ("guard_off", "", "取消驻守"),
    ),
    "hud": (
        ("set_hud_note", "text", "把当前打算写进 HUD 描述词"),
    ),
    "server": (
        ("run_server_command", "command,args,confirm",
         "下一条原版指令（须已在白名单勾选；未勾选/黑名单一律不发）"),
        ("send_whisper", "player,text", "给某人发私信；用了它 reply 请留空"),
        ("tpa_respond", "action,player", "回应传送请求：action=accept|deny"),
        ("tpa_request", "player", "主动请求传送到某人（默认只有主人可以）"),
    ),
    "memory": (
        ("remember_poi", "name,category,x,y,z", "记住当前位置"),
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
    "idle": ("move", "locate", "gather", "craft", "container", "guard", "items", "survive", "combat", "hud", "night", "server", "memory"),
    "travel": ("move", "locate", "combat", "guard", "items", "container", "survive", "hud", "night", "server", "memory"),
    "gather": ("move", "locate", "gather", "guard", "items", "craft", "container", "hud", "night", "server", "memory"),
    "craft": ("move", "locate", "craft", "container", "guard", "items", "gather", "hud", "night", "server", "memory"),
    "survive": ("survive", "hud", "night", "server", "memory", "guard", "items", "move", "locate"),
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


# ── 分层提示词模型（L0/L1/L2/L3）────────────────────────────────────
# 设计文档：docs/dev/PROMPT_MODEL.md。四层的**注入时机**各不相同：
#   L0 system 契约 —— 每轮都在，但**逐字节不变**（缓存前缀第一段）；
#   L1 system 人设/规则/授权 —— 首轮 + **内容变更时**才重组（内容 hash 作版本键）；
#   L2 动态上下文 —— 每轮都换，**只进当轮 user 消息**；
#   L3 工具定义 —— 每轮都带，但走 API `tools` 参数（**不内联进提示词**）。
# 铁律：L0/L1 不得含任何动态值（坐标 / 饥饿 / 时间 / 玩家名 / 欠账）——测试用哨兵值打住。

# L0：官方契约（基础身份由 L1 的人设段给出，这里只写"角色与输出"的公约）。
L0_PLANNER_CONTRACT = """你是一个正在玩 Minecraft 的玩家角色，和服务器里的其他玩家一起玩。你不是 AI，不要暴露自己是 AI。

工具调用契约：
- 可用工具在本次请求的 `tools` 参数里（不在本提示词里）；tool 必须精确取自该清单，args 按其中的参数名给。
- 参数用真实值，禁止"附近""随机位置"这类占位词；拿不到的真实值宁可不下发该动作。

输出格式：只输出合法 JSON（无 markdown、无多余文字），给 1-4 个候选动作并按偏好排序：
{"reply": "对玩家说的话", "candidates": [{"tool": "工具名", "args": {...}, "why": "简短理由"}]}
没有合适的动作就给空数组：{"reply": "", "candidates": []}"""

# L0：decider（第二阶段打分选择）的官方契约 —— 同样逐字节不变。
L0_DECIDER_CONTRACT = """你是行动决策模型：从给定候选动作里选出当前最优的一个，并给出置信度。

输出格式：只输出一行合法 JSON，不要解释、不要 markdown：
{"choice": <候选编号>, "confidence": <0 到 1 之间的小数>}
confidence 是你对这个选择的确信程度；没有一个候选明显合适就给低分。"""

# L1：规则（**静态**，不随轮次变化；与人设、授权一起构成系统提示词）。
PLANNER_RULES: tuple[str, ...] = (
    "你是真实玩家，不是 AI；说话自然简短，直接做事。",
    "用户要求停止时优先给 stop；当前任务非 idle 且目标未变时不要重复下发同一任务。",
    "晚上没事、且附近或记忆里有床 → 给 sleep_in_bed（它自己走过去睡，别干等到天亮）。",
    "用户说\"找床/找箱子/找某某方块\" → 用 go_to_block（自己会寻路过去），不要用 explore；只查不走用 find_block。",
    "用户说\"记住这里/保存一个记录点\" → 用 remember_poi。",
    "判断在跟谁说话：看最近聊天上下文 + 在线玩家的 ← 标记；对你说话不一定喊名字。",
    "\"守住这里/别走开\" → guard（别自己编坐标，缺省用最近发话者的位置）；\"不用守了\" → guard_off。",
    "找结构/群系 → locate_structure（名字用 minecraft:village 这类 id，别写死成某一个）；坐标等游戏回话，reply 里别写坐标，真实的会补在后面。",
    "有人问\"在干嘛/要去哪\" → 顺手用 set_hud_note 把当前打算写进 HUD 描述词。",
    "回复必须和实际动作一致：写进 reply 的事必须真的作为候选下发；没下发的动作不要写进 reply。",
    "要穿甲 → equip_armor（唯一穿甲方式；背包里没有就去已知容器找，别现场造）。",
    "合成优先用自己背包（含快捷栏）的材料；缺料/没配方就如实说；没被授权不要开箱。",
    "容器里只为拿自己要的量：container_move 给明确 count，别整箱搬空；拿完 container_close。",
    "从仓库拿东西＝借：得先有人明确授权（见下）；没授权不动，如实说。",
    "借只拿够用的量（≤需求 2 倍、最多 256），整箱搬不干；用完/任务结束/被叫停都要把剩余放回原箱（borrow_item 拿、return_item 还），别随身携带大量资源。",
    "有外部事件时先判断值不值得开口（有人死了/掉到好东西/第一次到新地方才值得调侃一句），不值得就不说话；别把事件当指令去干活。",
)

# L1：机制说明（**静态**：解释下面 L2 里的数字怎么看；不随轮次变化）。
PLANNER_MECHANICS = (
    "机制说明（静态）：\n"
    "- 自然回血条件：饱食度 ≥18 且饱和度 >0（两者缺一都不回血）。\n"
    "- 血量不满就主动补到饱食度 18+；缺血优先高饱和度食物（金苹果/熟肉/面包），单纯饿优先高营养食物。\n"
    "- 生食是最后手段；腐肉/河豚/毒马铃薯/蜘蛛眼这类有害食物永远别自己吃；热栏没有吃的就先找/做。\n"
    "- day_time 只是 gameTime 取模的近似值、不准，判断白天黑夜一律看「是否夜晚」。"
)

# 参数名 → JSON Schema 类型（其余按 string；工具参数都可有默认值，故 required 留空）。
_NUMERIC_TOOL_ARGS = frozenset({"x", "y", "z", "radius", "limit", "count", "index",
                                "from_slot", "to_slot", "slot"})
_BOOLEAN_TOOL_ARGS = frozenset({"remember"})
# 数组参数：`run_server_command` 的 `args`（指令参数表，元素是字符串）。
_ARRAY_TOOL_ARGS = frozenset({"args"})


def tool_schema(args: str) -> dict:
    """把 `"x,y,z"` 这样的参数名串渲染成 JSON Schema 的 properties。"""
    properties: dict[str, dict] = {}
    for raw in str(args or "").split(","):
        name = raw.strip()
        if not name:
            continue
        if name in _NUMERIC_TOOL_ARGS:
            properties[name] = {"type": "number"}
        elif name in _BOOLEAN_TOOL_ARGS:
            properties[name] = {"type": "boolean"}
        elif name in _ARRAY_TOOL_ARGS:
            properties[name] = {"type": "array", "items": {"type": "string"}}
        else:
            properties[name] = {"type": "string"}
    return properties


def build_tool_schemas(phase: str = "idle") -> list[dict]:
    """L3：当前阶段该有的工具定义（OpenAI tools 格式）——走 API `tools` 参数，**不内联进提示词**。

    复用 `_actions_for_phase`，把 M3-3 的"只注入当前阶段相关动作"继续落实在真实请求上
    （idle 是全部动作组的超集）。顺序稳定，按动作名去重。
    """
    tools: list[dict] = []
    for name, args, desc in _actions_for_phase(phase):
        tools.append({
            "type": "function",
            "function": {
                "name": name,
                "description": desc,
                "parameters": {
                    "type": "object",
                    "properties": tool_schema(args),
                    "required": [],
                },
            },
        })
    return tools


@dataclass
class PromptLayers:
    """一次 LLM 调用的分层组装结果：system(L0+L1) / user(L2) / tools(L3)。"""
    system: str
    user: str
    tools: list = field(default_factory=list)
    l1_version: str = ""


def render_planner_rules() -> str:
    """规则段（静态）：紧凑表，一行一条。"""
    return "规则：\n" + "\n".join(f"- {rule}" for rule in PLANNER_RULES)


def render_tool_table(phase: str) -> str:
    """工具表的人类可读预览（真正发给模型的是 `tools` 参数里的 schema）。"""
    return "可用动作（\"tool\" 必须精确用下列名称；括号内是参数名）：\n" + _render_actions(
        _actions_for_phase(phase))


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


def _as_optional_int(value: Any) -> Optional[int]:
    """槽位/数量这类可选整数：缺失/空串/非法 → None（布尔不算数字）。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str) and not value.strip():
        return None
    try:
        return int(value)
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
    
    def __init__(self, llm_service, memory, skills, threshold_provider=None, chat_inbox=None,
                 permissions=None, borrow_rules=None, server_commands=None,
                 whisper_sender=None, tpa_responder=None, tpa_requester=None,
                 player_resolver=None):
        self.llm = llm_service
        self.memory = memory
        self.skills = skills
        self._is_planning = False
        self._last_plan_time = 0.0
        self._last_decision: dict = {}
        # 指令闸门（白名单/黑名单/二次确认）与私信/TPA 的注入口：**都从 session 注入**
        # （planner 不自己持有权限判定，避免同一套规则两处实现）。
        self.server_commands = server_commands
        self.whisper_sender = whisper_sender
        self.tpa_responder = tpa_responder
        self.tpa_requester = tpa_requester
        self.player_resolver = player_resolver
        # 置信度阈值来源（可注入）。默认用模块常量；注入后可在 UI 改并即时生效。
        self._threshold_provider = threshold_provider
        # /locate 要等聊天回复：注入 Session 的 ChatInbox（读 wire 读取线程喂的那份）。
        self.chat_inbox = chat_inbox
        # 本轮的 /locate 结果（`_locate_note()` 会把它如实附到回复后面）。
        self._last_locate_result: Optional[dict] = None
        # 能力位来源（craft / containers / autonomy_craft）。没注入＝一律不放行（默认拒绝）。
        self.permissions = permissions
        # 借用规则（对话授权的"仓库里所有铁可以用"）：没注入＝没有任何借用授权。
        self.borrow_rules = borrow_rules
        # 借用台账：上限判定 + 借/还审计 + 按 open_container 快照核对归还。
        self.borrow_ledger = BorrowLedger()
        # 本轮制作还没补上的材料缺口（bare item → 还差几个）：容器取用以它为上限。
        self._craft_need: dict[str, int] = {}
        # 本次制作涉及过的材料（含已补齐的）：用来把"已经够了"和"根本没登记"区分开。
        self._craft_tracked: set[str] = set()
        # 取用审计（谁在什么时候从哪一格拿了多少）——"用多少取多少"要能事后查。
        self._take_audit: list[dict] = []
        # 本轮如实说明的补充（缺料 / 没授权 / 已经够了），拼在回复后面。
        self._craft_note: Optional[str] = None
        # 本轮服务器指令/私信/TPA 的如实说明（被白名单拒了、要二次确认…）。
        self._action_note: Optional[str] = None
        # L1 版本键 + 上一次组装的 L0+L1 字符串：键不变就复用**同一份字符串**
        # （前缀缓存友好）；L3 工具 schema 按阶段各建一次（M3-3 的阶段过滤继续生效）。
        self._system_key: Optional[str] = None
        self._system_cache: str = ""
        self._tools_cache: dict[str, list[dict]] = {}

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
        # 本轮的"如实说明"也从零开始（缺料/没授权这类话不能串轮）。
        self._craft_note = None
        self._action_note = None

        try:
            # 第一阶段：planner 结合状态产出候选动作（JSON）。
            # L0+L1 走 system（稳定前缀），L2 走当轮 user，工具走 `tools` 参数（L3）。
            layers = self.build_prompt_layers(sender, message, context, bot_name)
            result = self.llm.chat(
                [{"role": "system", "content": layers.system},
                 {"role": "user", "content": layers.user}],
                agent="planner",
                tools=layers.tools,
            )
            plan_text = result.get("content", "")

            if not str(plan_text).strip():
                # 模型没回 JSON 正文，而是走**原生** tool_calls（工具从 tools 参数给出去后
                # 这是完全合法的一手）→ 折算成候选，仍然过一遍 decider 的置信度门。
                native = self._plan_from_tool_calls(result.get("tool_calls"))
                if native is None:
                    logger.warning("[Planner] LLM 返回空内容")
                    return None
                return self._with_notes(self._execute_decision(native, context))
            
            logger.info("[Planner] LLM 规划: %s", plan_text[:100])
            
            plan = self._parse_plan_json(plan_text)
            if plan is None:
                # 模型没按候选 JSON 契约输出 → 回退旧的自由文本解析
                logger.info("[Planner] 输出非候选 JSON，回退自由文本解析")
                return self._with_notes(
                    self._execute_plan(plan_text, sender, message, context))
            
            # 第二阶段：decider 打分并选出一个候选
            return self._with_notes(self._execute_decision(plan, context))
            
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

    def _with_notes(self, reply: Optional[str]) -> Optional[str]:
        """把本轮的**真实补充**拼到回复后面：/locate 的真实坐标 + 制作/取物的如实说明。

        `_with_locate_note` 保持原样（有测试直接打它）；这里只是多贴一条缺料/授权说明
        与一条"指令被拒/要确认"的说明 —— 都不许编，有什么说什么。
        """
        text = self._with_locate_note(reply)
        for note in (self._craft_note, self._action_note):
            if not note:
                continue
            text = f"{text}\n{note}" if text else note
        return text
    
    # ── 分层组装：L0 system 契约 / L1 人设·规则·授权 / L2 当轮动态 / L3 工具 ──

    def _render_persona(self, context: dict, bot_name: str) -> str:
        """L1 的人设段：三段块（身份/表达习惯/场景规则），没配就退回 legacy 两行。

        **不含** state_block —— 那是会变的动态值，属于 L2。
        """
        context = context if isinstance(context, dict) else {}
        persona = context.get("persona")
        persona = persona if isinstance(persona, dict) else {}
        name = persona.get("name") or bot_name
        triple = render_persona_triple(context)
        if triple:
            return (f"你是 {name}。你的人设分三块（各管一件事，别重复、别把状态写进身份）：\n"
                    f"{triple}")
        personality = persona.get("personality") or "友好、自然"
        speaking_style = persona.get("speaking_style") or "口语化、简短"
        return (f"你是 {name}。现在的设定是：\n"
                f"- 性格：{personality}\n"
                f"- 说话风格：{speaking_style}")

    def _render_borrow_authorization(self, context: dict) -> str:
        """L1 的借用授权段（规则性记忆）：**内容变更才重组**，所以它是 L1 的一部分。"""
        context = context if isinstance(context, dict) else {}
        rules_text = str(context.get("borrow_rules") or "").strip()
        if rules_text:
            return "借用授权（被人明确授权才可以动仓库里的东西）：\n" + rules_text
        return "借用授权：还没有人授权我从仓库取用任何东西——要用得先让人明说「可以用仓库里的…」。"

    def _build_l1(self, context: dict, bot_name: str) -> str:
        """L1：人设三段 + 规则 + 机制说明 + 借用授权 + 各层范围 —— 只在**内容变更**时才重组。

        「各层范围」（`scope_text`）默认是空的 —— 空就**不进 L1**，静态前缀预算只降不抬；
        操作者填了才随人设一起进（配了才生效，等于用配置换预算）。
        """
        parts = [
            self._render_persona(context, bot_name),
            render_planner_rules(),
            PLANNER_MECHANICS,
            self._render_borrow_authorization(context),
        ]
        scope = self._render_scope()
        if scope:
            parts.append(scope)
        return "\n\n".join(part for part in parts if part)

    def _render_scope(self) -> str:
        """各层"能干什么"的自然语言范围（硬门是权限表，这段只用于语义判断）。"""
        store = self.permissions
        if store is None:
            return ""
        try:
            scope = store.scope_text()
        except Exception:  # noqa: BLE001 —— 读不到就不加这一段
            return ""
        lines = [f"- {tier}：{text}" for tier, text in scope.items() if str(text or "").strip()]
        if not lines:
            return ""
        return "各层能做什么（语义范围；硬规则以权限表为准）：\n" + "\n".join(lines)

    def build_prompt_layers(self, sender: str, message: str,
                            context: dict, bot_name: str = "AI") -> PromptLayers:
        """把一次调用拆成四层：system = L0 + L1（稳定前缀），user = L2，tools = L3。

        L0 逐字节不变；L1 用内容 hash 作版本键，键不变就复用上一次的字符串
        （模型供应商的前缀缓存按前 N 字节命中，见 docs/dev/PROMPT_MODEL.md）。
        """
        context = context if isinstance(context, dict) else {}
        l1 = self._build_l1(context, bot_name)
        key = prompt_version(L0_PLANNER_CONTRACT, l1)
        if key != self._system_key:
            self._system_key = key
            self._system_cache = f"{L0_PLANNER_CONTRACT}\n\n{l1}"
        phase = _phase_from_task(context.get("task_state"))
        if phase not in self._tools_cache:
            self._tools_cache[phase] = build_tool_schemas(phase)
        return PromptLayers(
            system=self._system_cache,
            user=self._build_l2(sender, message, context, bot_name),
            tools=self._tools_cache[phase],
            l1_version=key,
        )

    def _build_planner_prompt(self, sender: str, message: str,
                               context: dict, bot_name: str) -> str:
        """人类可读的**全量预览**：L0+L1+L2 再拼上工具表（真实请求走 `build_prompt_layers`，
        工具走 `tools` 参数而不是内联）。诊断/测试用它一眼看全模型到底看到了什么。
        """
        context = context if isinstance(context, dict) else {}
        layers = self.build_prompt_layers(sender, message, context, bot_name)
        phase = _phase_from_task(context.get("task_state"))
        return f"{layers.system}\n\n{layers.user}\n\n{render_tool_table(phase)}"

    def _build_l2(self, sender: str, message: str,
                  context: dict, bot_name: str) -> str:
        """L2：每轮的动态上下文（状态/聊天/任务/HUD/欠账）——**只进当轮 user 消息**。"""
        memory_context = self.memory.build_context()
        
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

        # 借用授权（L1，见 `_render_borrow_authorization`）+ 当前欠账（L2：每轮都变）。
        borrow_pending_text = (context.get("borrow_pending") or "").strip() if isinstance(context, dict) else ""
        if borrow_pending_text:
            borrow_pending_text = f"\n- 现在还没还：{borrow_pending_text}（用完就放回去，别带着跑）"
        borrow_debt_text = ("欠账（借了还没还）：" + borrow_pending_text) if borrow_pending_text else ""

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

        # L2 的状态块：人设三段已进 L1（system），这里只留**会变**的 state_block，
        # 绝不与身份混写（"状态不是身份"）。
        persona_blocks = context.get("persona_blocks") if isinstance(context, dict) else None
        persona_blocks = persona_blocks if isinstance(persona_blocks, dict) else {}
        state_block = str(persona_blocks.get("state_block") or "").strip()
        state_block_text = (f"【此刻状态（会变，不是身份）】{state_block}" if state_block else "")

        # HUD 描述词（模组报回来的当前 HUD 文案）：说话/改 HUD 前先知道现在显示什么。
        hud_note = str(context.get("hud_note") or "").strip() if isinstance(context, dict) else ""
        hud_note_text = f"当前 HUD 描述词：{hud_note}" if hud_note else ""

        # 私信（悄悄话）：这条话是**定向**的，回话也该回私信（不要当着全服说）。
        whisper_from = str(context.get("whisper_from") or "").strip() if isinstance(context, dict) else ""
        whisper_text = (
            f"这条是 {whisper_from} 发来的**私信**（悄悄话）：只有你俩看得到。"
            f"要回就用 send_whisper 回给 {whisper_from}，并把 reply 留空（别在公开聊天里说）。"
            if whisper_from else ""
        )

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
        # （"怎么对待事件"的规则是静态的，在 L1 的规则表里。）
        event = context.get("event") if isinstance(context, dict) else None
        event = event if isinstance(event, dict) else {}
        event_section = ""
        if event:
            event_section = (
                "\n外部事件（**不是**玩家直接对你说的话，只是世界里发生了一件事）：\n"
                f"{json.dumps(event, ensure_ascii=False)}\n"
                "要不要说点什么完全由你决定：不想说就把 reply 留空、candidates 给空数组——"
                "硬找话聊比不说话更糟。\n"
            )

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

        # L2 只在**当轮 user 消息**里；工具表、规则、人设都不在这里（分别走 tools 与 system）。
        extras = "\n".join(part for part in (state_block_text, hud_note_text, whisper_text) if part)
        extras_text = f"{extras}\n" if extras else ""

        prompt = f"""当前状态：
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

{extras_text}当前任务状态：
{context.get('task_state', {'kind': 'idle', 'status': 'idle'})}

饥饿与回血（当前值；怎么解释这些数字见系统提示里的规则）：
- 血量 {health_text}｜饱食度 {hunger_text}/20｜饱和度 {saturation_text}（饱和度是隐藏值）

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

{borrow_debt_text}

已知箱子（历史记忆，可能不在当前视野内）：
{known_poi_text}

时间与睡眠：
- 现在是{night_text}（sky_darken={sky_darken_text}，{sleepable_text}）；day_time={day_time_text}
- 附近的床：{nearby_bed_text}

已知记录点（你自己记住的持久记忆，可能在很远的地方）：
{remembered_text}

附近实体：
{nearby_entity_text}

画面观察（来自截图，可能略有延迟）：
{vision_text}
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
                [{"role": "system", "content": L0_DECIDER_CONTRACT},
                 {"role": "user", "content": prompt}],
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
        """L2：decider 的当轮动态（状态摘要 + 编号候选）；输出契约在 L0 system 里。"""
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
        return (f"当前状态：血量 {player.get('health', '?')}/{player.get('max_health', 20)}，"
                f"饥饿 {player.get('hunger', '?')}/20，饱和度 {player.get('saturation', '?')}，"
                f"任务 {task_state.get('kind', 'idle')}/{task_state.get('status', 'idle')}{speaker_text}\n\n"
                f"候选动作：\n{options_text}")

    @staticmethod
    def _plan_from_tool_calls(tool_calls: Any) -> Optional[dict]:
        """把供应商的原生 `tool_calls` 折算成候选动作（L3 走 tools 参数后的合法回法）。

        参数可能是 JSON 字符串（OpenAI 约定）或已是对象；解析不了一律给空 args ——
        **绝不猜参数**（猜出来的坐标/数量会直接下发到游戏里）。
        """
        candidates = []
        for call in tool_calls or []:
            if not isinstance(call, dict):
                continue
            function = call.get("function")
            function = function if isinstance(function, dict) else {}
            name = str(function.get("name") or "").strip()
            if not name:
                continue
            args = function.get("arguments")
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {}
            candidates.append({
                "tool": name,
                "args": args if isinstance(args, dict) else {},
                "why": "模型直接调用",
            })
        if not candidates:
            return None
        return {"reply": "", "candidates": candidates}

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
        
        # 合成（自由文本回退路径：同样要过授权 + 库存优先那道门）
        if "craft(" in plan_lower:
            item_name = self._extract_between(plan_text, "craft(", ")")
            if item_name:
                parts = [part.strip() for part in item_name.split(",") if part.strip()]
                normalized_item = self._normalize_item_name(parts[0])
                craft_count = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 1
                if self._dispatch_craft({"item": normalized_item, "count": craft_count},
                                        context, command="craft_via_recipe"):
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
        elif tool_name in ("craft_item", "craft_via_recipe"):
            # 两条合成命令**同一道门**：授权 + 库存优先（用户口径：没被允许不许造）。
            return self._dispatch_craft(payload, context, command=tool_name)
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
        elif tool_name == "equip_armor":
            return self._dispatch_equip_armor(context)
        elif tool_name == "container_move":
            return self._dispatch_container_move(payload, context)
        elif tool_name == "container_click":
            return self._dispatch_container_click(payload, context)
        elif tool_name == "container_close":
            self.skills.container_close()
            return True
        elif tool_name == "borrow_item":
            return self._dispatch_borrow_item(payload, context)
        elif tool_name == "return_item":
            return self._dispatch_return_item(payload, context)
        elif tool_name == "select_hotbar":
            index = payload.get("index")
            if index is not None:
                self.skills.select_hotbar(int(index))
                return True
        elif tool_name == "build":
            if {"x", "y", "z", "structure"}.issubset(payload):
                self.skills.build(float(payload["x"]), float(payload["y"]), float(payload["z"]), str(payload["structure"]))
                return True
        elif tool_name == "run_server_command":
            if self.server_commands is None:
                return False
            target_tool = str(payload.get("tool") or payload.get("command")
                              or payload.get("name") or "")
            result = self.server_commands(target_tool, payload.get("args"),
                                          confirm=str(payload.get("confirm") or ""))
            self._action_note = self._server_command_note(target_tool, result)
            return bool(result.get("ok"))
        elif tool_name == "send_whisper":
            if self.whisper_sender is None:
                return False
            player = payload.get("player") or payload.get("to") or payload.get("target")
            text = payload.get("text") or payload.get("message") or payload.get("content")
            if not player or text is None or str(text).strip() == "":
                return False
            result = self.whisper_sender(str(player), str(text))
            self._action_note = self._server_command_note("私信", result)
            return bool(result.get("ok"))
        elif tool_name == "tpa_respond":
            if self.tpa_responder is None:
                return False
            action = payload.get("action") or payload.get("decision") or payload.get("answer")
            result = self.tpa_responder(str(action or ""), str(payload.get("player") or ""))
            self._action_note = self._server_command_note("传送请求回应", result)
            return bool(result.get("ok"))
        elif tool_name == "tpa_request":
            if self.tpa_requester is None:
                return False
            player = payload.get("player") or payload.get("target")
            if not player:
                return False
            result = self.tpa_requester(str(player))
            self._action_note = self._server_command_note("主动传送请求", result)
            return bool(result.get("ok"))
        elif tool_name == "stop":
            self.skills.stop_all()
            return True
        return False

    @staticmethod
    def _server_command_note(what: str, result: Any) -> Optional[str]:
        """把闸门/解析的结论翻成一句**如实**的补充（被拒就说被拒，不假装做了）。"""
        if not isinstance(result, dict):
            return None
        if result.get("ok"):
            command = str(result.get("command") or "")
            if command:
                return f"（已下发 /{command.lstrip('/')}）"
            return None
        reason = str(result.get("blocked") or result.get("reason") or "unknown")
        if reason == "not_whitelisted":
            return f"（{what}没被勾选进白名单，我没发）"
        if reason == "hard_blacklist":
            return f"（{what}在黑名单里，绝对不能发）"
        if reason == "needs_confirm":
            token = str(result.get("confirm") or "")
            return (f"（{what}风险高：要再确认一次 —— 把同一条动作重发一遍，"
                    f"args 里带上 confirm=\"{token}\"）")
        if reason == "invalid" or reason == "invalid_text":
            return f"（{what}的参数不合法，我没发）"
        if reason == "disabled":
            return f"（服务器指令自动化被关掉了，{what}没发）"
        if reason == "tier_not_allowed":
            return f"（{what}你的层级不够，我没做）"
        if reason in {"not_found", "ambiguous", "empty"}:
            candidates = "、".join(str(item) for item in (result.get("candidates") or []))
            tail = f"，候选：{candidates}" if candidates else ""
            return f"（{what}的目标没锁定（{reason}）{tail}，我不猜）"
        return f"（{what}没成：{reason}）"

    # ── 制作 / 容器：授权 + 库存优先（用户口径） ──────────────────

    def _craft_policy(self, context: dict) -> CraftPolicy:
        """本轮的制作/翻箱授权（**只查能力表**，不看 tier 分支）。

        `context["autonomous"]=True` 表示这一轮没人下指令（self-prompt / 事件注入）：
        能不能自造只看 `autonomy_craft`，并且**一律不许翻箱**。没有权限表来源时
        默认**拒绝**（宁可不做，也不越权动别人的箱子）。
        """
        ctx = context if isinstance(context, dict) else {}
        autonomous = bool(ctx.get("autonomous"))
        tier = str(ctx.get("actor_tier") or "")
        store = self.permissions
        if store is None:
            return CraftPolicy(tier=tier or TIER_GUEST, autonomous=autonomous,
                               may_craft=False, may_open_containers=False,
                               reason="no_permissions")
        return store.craft_policy(tier, autonomous=autonomous)

    def _track_craft_need(self, missing: Any) -> None:
        """登记本次制作的材料缺口（正数才算）：容器取用以它为上限，绝不搬空。"""
        need = {}
        for item, amount in (missing or {}).items() if isinstance(missing, dict) else ():
            try:
                value = int(amount)
            except (TypeError, ValueError):
                continue
            if value > 0:
                need[normalize_item(item)] = value
        self._craft_need = need
        self._craft_tracked = set(need)

    def _dispatch_craft(self, payload: dict, context: dict, *, command: str) -> bool:
        """合成：先过授权，再按**自己背包**评估材料，最后才下发。

        - 没被允许造 → 一句如实说明，**一帧都不发**；
        - 材料齐（或配方不在我们的表里）→ 就地用 `craft_via_recipe` 造（容器 0）；
        - 材料不齐：
            · 获授权翻箱 → 记下缺口（后续 `container_move` 以它为上限）、去已知容器拿；
            · 没获授权 → **如实说缺什么**，绝不开箱。
        """
        item = payload.get("item") or payload.get("item_name")
        if not item:
            return False
        item_id = self._normalize_item_name(str(item))
        try:
            count = int(payload.get("count", 1))
        except (TypeError, ValueError):
            count = 1
        count = max(1, count)

        policy = self._craft_policy(context)
        if not policy.may_craft:
            self._craft_note = self._auth_note(policy, "造东西")
            logger.info("[Planner] 合成 %s 被拒：%s", item_id, policy.reason)
            return False
        if self._is_duplicate_task(context, {"craft"}, item_id):
            return False

        inventory = context.get("inventory") if isinstance(context, dict) else None
        plan = evaluate_craft(item_id, count, inventory)
        if plan.known and plan.missing:
            missing = describe_missing(plan)
            if not policy.may_open_containers:
                self._track_craft_need({})
                self._craft_note = (f"（我自己背包里的材料不够：还缺 {missing}；"
                                    f"没被授权翻箱子，我不去翻）")
                logger.info("[Planner] 合成 %s 缺料且未授权翻箱：%s", item_id, missing)
                return False
            picked = route_containers(plan.missing,
                                      context.get("known_containers") if isinstance(context, dict) else None)
            if not picked:
                self._track_craft_need({})
                self._craft_note = f"（材料不够：还缺 {missing}；已知的容器里也没有，就没去动）"
                logger.info("[Planner] 合成 %s 缺料且已知容器补不上：%s", item_id, missing)
                return False
            # 缺口登记：后面的 container_move 只能按"还缺的量"取，绝不搬空。
            self._track_craft_need(plan.missing)
            pick = picked[0]
            self._craft_note = (f"（背包里的材料不够：还缺 {missing}；"
                                f"我记得 {self._pick_label(pick)} 那边有，我去拿）")
            self._dispatch_container_trip(pick, context)
            logger.info("[Planner] 合成 %s 缺料 → 去 %s 取", item_id, self._pick_label(pick))
            return True

        self._track_craft_need({})
        self._craft_note = None
        if command == "craft_via_recipe":
            self.skills.craft_via_recipe(item_id, count)
        else:
            self.skills.craft_item(item_id, count)
        logger.info("[Planner] 合成 %s x%d（库存优先，就地）", item_id, count)
        return True

    def _auth_note(self, policy: CraftPolicy, what: str) -> str:
        """如实说明为什么没动手（缺授权时的统一话术）。"""
        if policy.autonomous:
            return f"（没人在指挥我，我自己不许{what}（autonomy_craft 关着），所以没动手）"
        return f"（{policy.tier} 没被允许让我{what}，所以没动手）"

    @staticmethod
    def _pick_label(pick: ContainerPick) -> str:
        where = (f"({pick.x:.0f},{pick.y:.0f},{pick.z:.0f})"
                 if pick.x is not None and pick.z is not None else "记忆里")
        return f"{pick.block_id}{where}"

    def _dispatch_container_trip(self, pick: ContainerPick, context: dict) -> bool:
        """去已知容器的路上：走已有的 `go_to_block`（Java 侧负责寻路 + 到达后记 POI）。

        容器只有走到跟前、右键打开后才谈得上取用，所以这里只负责"去哪"这一步。
        """
        block = str(pick.block_id or "").strip() or "minecraft:chest"
        radius = int(max(32, (pick.distance or 0.0) + 8))
        if self._is_duplicate_task(context, {"go_to_block", "collect"}, block):
            return False
        self.skills.go_to_block(block, radius, True)
        return True

    def _dispatch_container_move(self, payload: dict, context: dict) -> bool:
        """容器取用：必须获授权，且**用多少取多少**（有缺口登记时绝不超、绝不整组搬空）。"""
        policy = self._craft_policy(context)
        if not policy.may_open_containers:
            self._craft_note = self._auth_note(policy, "翻箱子")
            logger.info("[Planner] container_move 被拒：%s", policy.reason)
            return False
        from_slot = _as_optional_int(payload.get("from_slot"))
        if from_slot is None:
            return False
        to_slot = _as_optional_int(payload.get("to_slot"))
        count = _as_optional_int(payload.get("count"))
        item = str(payload.get("item") or payload.get("name") or "").strip()
        if not item:
            item = self._item_at_slot(context, from_slot)
        item_id = normalize_item(item) if item else ""
        if item_id and item_id in self._craft_tracked:
            # 这一次制作登记过的材料：只按"还缺的量"取（已经够了就不许再拿）。
            remaining = self._craft_need.get(item_id, 0)
            if remaining <= 0:
                self._craft_note = f"（{item_id} 已经够了，不用再从箱子里拿）"
                logger.info("[Planner] container_move 被拒：%s 已够", item_id)
                return False
            take = clamp_take(item_id, count, self._craft_need)
            if take <= 0:
                self._craft_note = f"（{item_id} 已经够了，不用再从箱子里拿）"
                return False
            count = take
            left = remaining - take
            if left > 0:
                self._craft_need[item_id] = left
            else:
                del self._craft_need[item_id]
        elif self._craft_tracked:
            # 有缺口但认不出这一格是什么（Java 的 open_container 只报槽位数）：
            # 只允许**明确给 count**，且不超过总缺口 —— 整组搬空一律不放行。
            total = sum(self._craft_need.values())
            if count is None or count <= 0:
                self._craft_note = "（不确定这格是什么，不能整组搬；说清要拿哪件材料、拿多少）"
                logger.info("[Planner] container_move 被拒：缺口中不让整组搬")
                return False
            if count > total:
                self._craft_note = f"（只需要补 {total} 个，不能多拿）"
                logger.info("[Planner] container_move 被拒：%d > 缺口 %d", count, total)
                return False
        self._record_take(item_id or f"slot:{from_slot}", count or 0, from_slot, to_slot)
        self.skills.container_move(from_slot, to_slot, count)
        logger.info("[Planner] 容器取用 %s x%s（slot %s → %s）",
                    item_id or "?", count if count else "整组", from_slot, to_slot if to_slot is not None else "快速移动")
        return True

    def _dispatch_container_click(self, payload: dict, context: dict) -> bool:
        """点容器里的一格（"点容器找盔甲"）：同样是容器操作，没授权不动。"""
        policy = self._craft_policy(context)
        if not policy.may_open_containers:
            self._craft_note = self._auth_note(policy, "翻箱子")
            logger.info("[Planner] container_click 被拒：%s", policy.reason)
            return False
        slot = _as_optional_int(payload.get("slot"))
        if slot is None:
            return False
        click_type = str(payload.get("click_type") or "pickup").strip().lower() or "pickup"
        button = _as_optional_int(payload.get("button"))
        self.skills.container_click(slot, button if button is not None else 0, click_type)
        return True

    # ── 借用台账：从仓库拿东西＝借（授权 → 上限 → 归还 → 校验 → 审计）──

    @staticmethod
    def _open_container(context: dict) -> dict:
        """当前打开的容器快照（没有／不是 dict → 空 dict）。"""
        ctx = context if isinstance(context, dict) else {}
        container = ctx.get("open_container")
        return container if isinstance(container, dict) else {}

    def _borrow_grant(self, item: str) -> Optional[dict]:
        """这件物品有没有被口头授权借用（查规则记忆；没权限表来源 → 一律没有）。"""
        store = self.borrow_rules
        if store is None:
            return None
        try:
            return store.match(item)
        except Exception as exc:
            logger.warning("[Planner] 借用规则读取失败: %s", exc)
            return None

    def _borrow_need(self, item: str) -> Optional[int]:
        """这次制作这件物品还缺多少（没有登记就返回 None＝不限需求，只按 256 上限）。"""
        if item in self._craft_tracked:
            return self._craft_need.get(item)
        return None

    def _borrow_refusal_note(self, item: str, reason: str, cap: int) -> str:
        if reason == "whole_container_forbidden":
            return "（不给数量的取用＝整箱搬，不允许；说清要借多少）"
        if reason == "invalid_count":
            return "（借的数量得是正整数）"
        if reason == "over_limit":
            return f"（{item} 一次最多借 {cap} 个，不能整箱搬）"
        return f"（这次借不成：{reason}）"

    def _borrow_go_to_container(self, item: str, raw: str, count: int, context: dict) -> bool:
        """容器没开（或里面没有这件东西）：去**已知容器**拿，而不是凭空冒出来。"""
        containers = context.get("known_containers") if isinstance(context, dict) else None
        picks = route_containers({item: count}, containers)
        if not picks:
            self._craft_note = (f"（{raw or item} 这里没有：箱子里没找着，也没有打开的容器可以拿）")
            logger.info("[Planner] borrow_item：找不到 %s", item)
            return False
        pick = picks[0]
        self._craft_note = (f"（我记得 {self._pick_label(pick)} 里有 {raw or item}，"
                            f"过去拿 {count} 个，拿完就放回去）")
        return self._dispatch_container_trip(pick, context)

    def _dispatch_borrow_item(self, payload: dict, context: dict) -> bool:
        """从仓库**借**物品：先过翻箱授权，再过借用规则，最后卡上限。

        整箱搬（不给 count）一律拒绝；一次最多 `min(256, 需求×2)`。借出去的东西会被记进
        台账 —— 用完/任务结束/被叫停时由 `settle_borrows` 自动放回并核对。
        """
        policy = self._craft_policy(context)
        if not policy.may_open_containers:
            self._craft_note = self._auth_note(policy, "翻箱子")
            logger.info("[Planner] borrow_item 被拒：%s", policy.reason)
            return False
        raw = str(payload.get("item") or payload.get("name") or "").strip()
        item = normalize_item(raw)
        if not item:
            self._craft_note = "（要借什么得说清楚）"
            return False
        if self._borrow_grant(item) is None:
            self._craft_note = f"（没人授权我取用 {raw or item}；要用得先明说「可以用仓库里的……」）"
            logger.info("[Planner] borrow_item 被拒：无授权 %s", item)
            return False
        need = self._borrow_need(item)
        count = _as_optional_int(payload.get("count"))
        allowed, reason, cap = self.borrow_ledger.check(item, count, need=need)
        if not allowed:
            self._craft_note = self._borrow_refusal_note(item, reason, cap)
            logger.info("[Planner] borrow_item 被拒：%s %s", item, reason)
            return False
        container = self._open_container(context)
        plans = self.borrow_ledger.plan_moves_out(container, item, count) if container else []
        if not plans:
            return self._borrow_go_to_container(item, raw, count, context)
        reason_text = str(payload.get("reason") or payload.get("why")
                          or ("制作" if need is not None else "任务"))
        taken = 0
        for plan in plans:
            self.skills.container_move(plan["from_slot"], plan["to_slot"], plan["count"])
            taken += plan["count"]
        first = plans[0]
        hint = payload.get("container") if isinstance(payload.get("container"), dict) else {}
        self.borrow_ledger.record_borrow(
            item, taken, container=container,
            from_slot=first["from_slot"], to_slot=first["to_slot"], reason=reason_text,
            container_pos=self._container_pos(payload, context),
            container_block=str(hint.get("block_id") or hint.get("name") or ""))
        where = str(container.get("title") or "容器")
        short = "" if taken >= count else f"（箱里只有 {taken} 个，先拿这些）"
        self._craft_note = (f"（从「{where}」借了 {item} x{taken}{short}；"
                            f"用完/任务结束会放回去）")
        logger.info("[Planner] 借出 %s x%s（%s 条移动）", item, taken, len(plans))
        return True

    @staticmethod
    def _container_pos(payload: dict, context: dict) -> Optional[tuple]:
        """借出时记下容器坐标（payload 给了就用；没有就不编）。"""
        source = payload.get("container") if isinstance(payload, dict) else None
        source = source if isinstance(source, dict) else payload if isinstance(payload, dict) else {}
        try:
            if all(isinstance(source.get(axis), (int, float)) and not isinstance(source.get(axis), bool)
                   for axis in ("x", "y", "z")):
                return (float(source["x"]), float(source["y"]), float(source["z"]))
        except (TypeError, ValueError):
            return None
        return None

    def _send_return_trip(self, entry) -> bool:
        """回原容器的路上：走 `go_to_block`（Java 负责寻路 + 到达后记 POI）。"""
        if entry.trip_sent:
            return False
        entry.trip_sent = True
        self.skills.go_to_block(entry.container_block or "minecraft:chest", 16, True)
        return True

    def _container_in_reach(self, entry, context: dict) -> bool:
        """容器就在跟前？用 nearby_storage / known_containers 的距离与坐标判断（绝不猜）。"""
        ctx = context if isinstance(context, dict) else {}
        rows = []
        for key in ("nearby_storage", "known_containers"):
            value = ctx.get(key)
            if isinstance(value, (list, tuple)):
                rows.extend(row for row in value if isinstance(row, dict))
        for row in rows:
            title = str(row.get("title") or row.get("name") or "")
            if entry.container_title and title and title != entry.container_title:
                continue
            distance = row.get("distance")
            if isinstance(distance, (int, float)) and not isinstance(distance, bool) and distance <= 4.0:
                return True
            if entry.container_pos:
                try:
                    dx = float(row.get("x")) - entry.container_pos[0]
                    dy = float(row.get("y")) - entry.container_pos[1]
                    dz = float(row.get("z")) - entry.container_pos[2]
                except (TypeError, ValueError):
                    continue
                if abs(dx) <= 4 and abs(dy) <= 4 and abs(dz) <= 4:
                    return True
        return False

    def _return_entries(self, entries: list, context: dict, *, reason: str,
                        count: Optional[int] = None, final: bool = False) -> dict:
        """把一批借用放回原容器；返回 `{sent, pending, failed}`（都是 BorrowEntry）。

        - 容器开着且是原容器 → `container_move` 放回，等快照核对；
        - 容器没开但有坐标 → 走一趟 `go_to_block`，等到了再放；
        - 真的回不去（`final=True`）→ 记账为失败并**报出来**。

        **不查翻箱授权表**：把东西还回去是保守方向，也是用户口径里"必须做"的那一半；
        借出去那一步已经卡过授权了。显式的 `return_item` 工具才走授权门。
        """
        container = self._open_container(context)
        out: dict = {"sent": [], "pending": [], "failed": []}
        for entry in entries:
            if entry.awaiting_snapshot:
                out["pending"].append(entry)
                continue
            want = min(int(count), entry.remaining) if count else entry.remaining
            if want <= 0:
                continue
            if container and self.borrow_ledger.same_container(entry, container):
                plan = self.borrow_ledger.plan_move_back(container, entry.item, want)
                if plan:
                    self.skills.container_move(plan["from_slot"], plan["to_slot"], plan["count"])
                    self.borrow_ledger.begin_return(entry.id, count=plan["count"],
                                                    from_slot=plan["from_slot"],
                                                    to_slot=plan["to_slot"], reason=reason)
                    out["sent"].append(entry)
                    continue
                if final and self.borrow_ledger.holds(container, entry.item):
                    # 东西还在手上，可原容器塞不回去了（格子满了）→ 报出来。
                    self.borrow_ledger.mark_unreturnable(entry.id, f"{reason}:container_full")
                    out["failed"].append(entry)
                    continue
            if entry.container_pos and self._send_return_trip(entry):
                out["pending"].append(entry)
                continue
            if self._container_in_reach(entry, context):
                self.skills.open_container()
                out["pending"].append(entry)
                continue
            if final:
                self.borrow_ledger.mark_unreturnable(entry.id, f"{reason}:no_container")
                out["failed"].append(entry)
            else:
                out["pending"].append(entry)
        return out

    def _dispatch_return_item(self, payload: dict, context: dict) -> bool:
        """显式归还（工具 `return_item`）：放回原容器，数量缺省＝这笔借用的全部剩余。"""
        policy = self._craft_policy(context)
        if not policy.may_open_containers:
            self._craft_note = self._auth_note(policy, "翻箱子")
            return False
        item = normalize_item(payload.get("item") or payload.get("name"))
        targets = [e for e in self.borrow_ledger.outstanding() if not item or e.item == item]
        if not targets:
            self._craft_note = "（没有借了还没还的东西）"
            return False
        result = self._return_entries(targets, context, reason="tool",
                                      count=_as_optional_int(payload.get("count")), final=True)
        self._craft_note = self._return_note(result)
        return bool(result["sent"] or result["pending"] or result["failed"])

    @staticmethod
    def _return_note(result: dict) -> str:
        sent = ", ".join(f"{e.item} x{e.count - e.returned}" for e in result.get("sent", []))
        pending = ", ".join(f"{e.item} x{e.remaining}" for e in result.get("pending", []))
        failed = ", ".join(f"{e.item} x{e.remaining}（{e.failure or '回不去'}）"
                          for e in result.get("failed", []))
        parts = []
        if sent:
            parts.append(f"已放回原箱：{sent}")
        if pending:
            parts.append(f"正在放回/路上：{pending}")
        if failed:
            parts.append(f"**没能放回去**：{failed}")
        return "（" + "；".join(parts) + "）" if parts else "（没有要还的）"

    def settle_borrows(self, reason: str, context: Optional[dict] = None) -> dict:
        """任务结束/失败/取消/被中断/玩家接管时结算借用：归还剩余并报出回不去的。

        `final=True`：这一步是真结算，回不去的一律记账为失败并放进 `failed`（要报出来）。
        """
        if context is None:
            context = {}
        entries = self.borrow_ledger.outstanding()
        if not entries:
            return {"reason": reason, "sent": [], "pending": [], "failed": [], "note": ""}
        result = self._return_entries(entries, context, reason=reason, final=True)
        note = self._return_note(result)
        result["reason"] = reason
        result["note"] = note
        logger.info("[Planner] 借用结算（%s）：%s", reason, note)
        return result

    def auto_return_step(self, context: Optional[dict] = None) -> bool:
        """空闲时推进一步归还（开箱/放回）——不设 final，绝不误判失败。"""
        if context is None:
            context = {}
        entries = self.borrow_ledger.outstanding()
        if not entries:
            return False
        result = self._return_entries(entries, context, reason="auto", final=False)
        if result["sent"] or result["pending"]:
            self._craft_note = self._return_note(result)
            return True
        return False

    def verify_borrow_returns(self, open_container: Any) -> list[dict]:
        """用一份新的 `open_container` 快照核对归还（真的回去了才算数）。"""
        try:
            rows = self.borrow_ledger.verify(open_container)
        except Exception as exc:
            logger.warning("[Planner] 归还核对失败: %s", exc)
            return []
        for row in rows:
            if row.get("result") == "failed":
                self._craft_note = (f"（{row['item']} x{row.get('count')} **没能放回原箱**："
                                    f"{row.get('detail') or row.get('reason')}）")
        return rows

    def borrow_status(self) -> dict:
        """借用总览（欠账 + 审计尾部），供 /api/status 与 prompt。"""
        return self.borrow_ledger.status()

    def borrow_note(self) -> str:
        """借出未还的摘要（prompt 用）。"""
        return self.borrow_ledger.describe()

    def _dispatch_equip_armor(self, context: dict) -> bool:
        """穿甲：自己背包里有盔甲就直接穿；没有就去**已知容器**里找（"点容器找盔甲"）。

        翻箱那一步要 `containers` 授权 —— 没授权就如实说，绝不偷偷开箱。
        """
        inventory = context.get("inventory") if isinstance(context, dict) else None
        has_armor = any(is_armor(entry.get("name") or entry.get("item_id"))
                        for entry in (inventory or []) if isinstance(entry, dict))
        if has_armor:
            self.skills.equip_armor()
            return True
        picks = armor_in_containers(context.get("known_containers") if isinstance(context, dict) else None)
        if not picks:
            self._craft_note = "（我背包里没有盔甲，已知的容器里也没翻到）"
            return False
        policy = self._craft_policy(context)
        if not policy.may_open_containers:
            self._craft_note = "（背包里没有盔甲；没被授权翻箱子，我不去翻）"
            return False
        pick = picks[0]
        pieces = ", ".join(f"{name} x{amount}" for name, amount in pick.take.items())
        self._craft_note = f"（背包里没有盔甲；我记得 {self._pick_label(pick)} 里有 {pieces}，我过去拿）"
        self._dispatch_container_trip(pick, context)
        return True

    @staticmethod
    def _item_at_slot(context: dict, slot: int) -> str:
        """打开的容器里某格是什么（尽力而为：state 没报内容就返回空串，绝不猜物品）。"""
        ctx = context if isinstance(context, dict) else {}
        entries = ctx.get("container_items")
        if entries is None:
            container = ctx.get("open_container")
            container = container if isinstance(container, dict) else {}
            entries = container.get("items") or container.get("contents")
        if isinstance(entries, dict):
            entry = entries.get(str(slot)) or entries.get(slot)
            return str((entry or {}).get("item") or (entry or {}).get("item_id")
                       or (entry or {}).get("name") or "") if isinstance(entry, dict) else ""
        for entry in (entries or []):
            if not isinstance(entry, dict):
                continue
            if _as_optional_int(entry.get("slot")) == slot:
                return str(entry.get("item") or entry.get("item_id") or entry.get("name") or "")
        return ""

    def _record_take(self, item: str, count: int, from_slot: int, to_slot: Optional[int]) -> None:
        """取用审计：记下拿了什么、多少、从哪到哪（"用多少取多少"要能事后查）。"""
        self._take_audit.append({
            "ts": time.time(),
            "item": item,
            "count": int(count or 0),
            "from_slot": int(from_slot),
            "to_slot": int(to_slot) if to_slot is not None else None,
            "remaining_need": dict(self._craft_need),
        })
        del self._take_audit[:-50]

    def take_audit(self, n: int = 10) -> list[dict]:
        """最近 n 条取用审计（时间从早到晚）。"""
        rows = self._take_audit[-int(n):] if n and int(n) > 0 else list(self._take_audit)
        return [dict(row) for row in rows]

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
            # 还没补上的材料缺口 + 容器取用审计（"用多少取多少"要能事后查）。
            "craft_need": dict(self._craft_need),
            "take_audit": self.take_audit(10),
        }
