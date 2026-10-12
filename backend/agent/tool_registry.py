"""Tool Registry —— 工具定义的**唯一事实来源**。

用户口径：「工具只好建立为标准」（工具要有标准、要成为唯一事实来源）。本模块把
每条工具的所有属性收在一处：

    name / description / parameters(JSON Schema) / groups / phase 过滤 /
    permission（tier 门 + 是否需要区域/容器/指令授权）/ handler（指向现有技能函数）

- L3 工具表（OpenAI `tools` 形状）由 `build_tool_schemas(phase)` 从这里生成，
  `planner` 只做转发，绝不另维护一份；
- 阶段过滤（`idle` / `craft` / `gather` / `travel` / `survive`）逐字沿用旧 `PHASE_GROUPS`
  的行为——注入哪些组、组内顺序、按名去重，全部不变；
- 派发前由注册表驱动做一次统一授权（`planner._authorize_tool`），不再散在各 handler。

背景：旧代码把工具清单拆在 `planner.ACTION_GROUPS`（分组动作表）与 `build_tool_schemas`
两处，另有一套 `PHASE_GROUPS` 阶段映射；新增一条工具要改多处、容易漏。这里全部收敛。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# 复合体（声明式能力复合，`docs/dev/AGENT_ARCH.md` §10.2 #4）：**定义**在 `agent/composites.py`
# ——这里只把声明投影成注册表条目（复合体也是**工具**，注册表仍是工具清单的唯一事实来源）。
from .composites import COMPOSITES, validate_composite

# ── 参数名 → JSON Schema 类型 ────────────────────────────────
# 工具参数都可有默认值，故 `required` 一律留空；类型按参数名约定推断。
_NUMERIC_TOOL_ARGS = frozenset({"x", "y", "z", "radius", "limit", "count", "index",
                                "from_slot", "to_slot", "slot", "yaw", "pitch",
                                "max_seconds"})
_BOOLEAN_TOOL_ARGS = frozenset({"remember", "feed", "craft", "mischief", "whole",
                                "keepalive"})
# 数组参数：`run_server_command` 的 `args`（字符串表）与 `haul` 的 `items`（物品表）。
_ARRAY_TOOL_ARGS = frozenset({"args"})
_ARRAY_OBJECT_ARGS = frozenset({"items"})


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
        elif name in _ARRAY_OBJECT_ARGS:
            properties[name] = {"type": "array", "items": {"type": "object"}}
        else:
            properties[name] = {"type": "string"}
    return properties


# ── 权限门（注册表字段）──────────────────────────────────────
# 「后台只留授权与偏好」——权限门本身是**授权**（谁能让它做什么），必须保留。
# 每条工具的权限用这个描述符声明，派发前由 `planner._authorize_tool` **统一**执行；
# 不再散落在各 handler 里各写各的 tier 判断。
@dataclass(frozen=True)
class ToolPermission:
    """一条工具的授权要求（`planner` 统一校验用；空描述符＝无额外门）。

    tier       : 需要的 tier 能力键（"" 表示无额外 tier 门）——
                 craft→`PermissionsStore.can_craft`、territory→`can_authorize_territory`、
                 zones→`can_define_zones`、containers→`can_authorize_containers`。
    container  : 派发前统一走容器访问授权（箱子的 free/borrow_only/no_touch 策略 + 借用规则）。
    read_only  : **只读**变体（`container=True` 时才有意义）：no_touch 一律拒，登记过的
                 （free/borrow_only）只读放行 —— 只看不拿，不需要翻箱授权；没登记的箱子
                 仍要翻箱授权（不去开别人没让我看的箱子）。
    command    : 走服务器指令白名单闸门（判定在 handler 内，因为需要白名单内容）。
    borrow     : 走借用台账（借用规则 + 数量上限 + 归还）。
    area       : 动手范围受领地/区域授权约束（handler 用 `may_act` 逐点判定）。
    deny_label : 被拒时如实说明里用的动作名（"把一片地的所有权交给我" 这类）。
    note       : 人读说明（进 TOOLS.md）。
    """

    tier: str = ""
    container: bool = False
    read_only: bool = False
    command: bool = False
    borrow: bool = False
    area: bool = False
    deny_label: str = ""
    note: str = ""


_NO_PERM = ToolPermission()


# ── 工具定义 ─────────────────────────────────────────────────
@dataclass(frozen=True)
class ToolSpec:
    """一条工具的全部定义（唯一事实来源）。"""
    name: str
    description: str
    args: str                                  # 参数名串（"x,y,z"，渲染成 JSON Schema）
    groups: tuple[str, ...]                    # 属于哪些动作组（用于阶段过滤）
    handler: str                               # Planner 上的派发方法名
    permission: ToolPermission = _NO_PERM
    # 是不是"要跑一段时间"的调用（长任务）——是的话派发时登记成 **job 句柄**
    # （`agent/job_registry.py`，四样门槛齐备才许登记），供查看/取消/卡死治理。
    job: bool = False

    def parameters(self) -> dict[str, Any]:
        """OpenAI `tools` 形状里的 parameters 对象。"""
        return {"type": "object", "properties": tool_schema(self.args), "required": []}

    def schema(self) -> dict:
        """本条工具的 OpenAI function schema。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters(),
            },
        }


# ── 复合体（声明式，唯一事实来源在 `agent/composites.py`）────────
# 复合体在模型眼里就是**一条工具**：L3 里照常出现（只减不抬的静态前缀 L0/L1 不受影响 ✗），
# 派发口与别的工具**同一个**（`Planner._run_composite` → 逐步走 `_dispatch_tool_call`）。
# `job=True`：复合体是长任务，登记 job 句柄（逐条声明都齐备四样门槛，见 `composites.py`）。
COMPOSITE_GROUP = "composite"


def composite_specs() -> tuple[ToolSpec, ...]:
    """把 `COMPOSITES` 的声明投影成注册表条目（handler = Planner 上的那个入口）。"""
    return tuple(
        ToolSpec(comp.name, comp.description, ",".join(comp.args), (COMPOSITE_GROUP,),
                 f"_dispatch_composite_{comp.name}", job=True,
                 permission=ToolPermission(
                     note="复合体：内部只调**已注册**工具，每一步都走它自己的门"
                          "（tier/白名单/区域/容器/借用），失败会说清卡在第几步"))
        for comp in COMPOSITES)


# 顺序即动作组内顺序：**逐字沿用**旧 `ACTION_GROUPS` 的组序与组内序，
# 保证阶段过滤后的工具序列（进而 `build_tool_schemas` 的输出）与旧行为逐字节一致。
TOOL_SPECS: tuple[ToolSpec, ...] = (
    # ── move ──
    ToolSpec("move_to", "移动到坐标（走到掉落物上会**自动捡起**；捡不捡先自己判断：看上下文里的"
                        "掉落物括号 —— 谁丢的、掉多久了、还有多久消失，背包满了就别去）",
             "x,y,z", ("move",), "_tool_move_to", job=True),
    ToolSpec("follow", "**只在玩家明确要求跟随**（「跟着我 / 别走开 / 跟我来」）时用：长驻跟住他、"
                       "他走我走（目标会动）。玩家说「过来 / 来我这」是**一次性**走到他那里 ⇒ 用 "
                       "come_to_player（或 move_to 到他的坐标），**不要**用 follow",
             "player", ("move",), "_tool_follow", job=True),
    ToolSpec("come_to_player", "一次性走到某玩家**现在**站的位置，到了就结束这一次"
                               "（玩家说「过来 / 来我这」时用它；他之后又走开不跟着他 —— 那是 "
                               "follow 的事）。到不了也结束，要再走一趟就再叫一次",
             "player", ("move",), "_tool_come_to_player", job=True),
    ToolSpec("explore", "探索半径", "radius", ("move",), "_tool_explore", job=True),
    ToolSpec("stop", "停止动作", "", ("move",), "_tool_stop"),
    # ── locate ──
    ToolSpec("where_is", "问「某东西在哪」：先调 where_is 拿候选清单（在线玩家/记忆点/站点/"
                         "容器/区域/基地），每条带坐标、距离与来源；再从候选里挑一个用 "
                         "move_to/go_to_block/come_to_player/follow 过去——「先问位置、挑一个、再过去」的闭环。"
                         "玩家说「过来」用 come_to_player，长驻跟随才用 follow。"
                         "只答位置：不移动、不扫描、不猜坐标，问不到就如实说不知道"
                         "（kind=auto|player|poi|station|container|zone，默认 auto）",
             "target,kind", ("locate",), "_tool_where_is"),
    ToolSpec("find_entity", "找**附近实体**（只查不走）：按 kind 列出手边快照里的实体候选"
                            "（牛/羊/村民/僵尸这类，写英文 cow/villager/zombie 或中文「牛」"
                            "「村民」都行；也能按类别 animal|hostile|item|player|mob），"
                            "每条带坐标、距离与类别——数据只来自 state 快照（我周围约 16 格），"
                            "要过去就另外调 move_to/go_to_block/come_to_player/follow（我只答，不移动、不扫描、"
                            "不编坐标）；快照里没有就如实说没有。kind 留空＝把附近实体都列出来",
             "kind,limit", ("locate",), "_tool_find_entity"),
    ToolSpec("find_block", "扫附近有无该方块（只查不走）", "block,radius,limit",
             ("locate",), "_tool_find_block"),
    ToolSpec("scan_entities", "**按需扫描附近实体**（要坐标就用它 —— 这是唯一的坐标出口）："
                              "快照里默认只上报**威胁 / 玩家**的坐标，被动生物等档位只报计数；"
                              "喂动物、找某一只、要血量/实体号时先扫一趟。"
                              "radius 1..32（默认 16，超上限**如实拒绝、不放大**）；"
                              "kind=all|dangerous|hostile|neutral|passive|player|item|unknown"
                              "（默认 all）；limit 1..48（默认 24，超上限钳到 48）。"
                              "回执带 total（截断前真实总数）/returned（这一趟回来几条）/truncated"
                              "—— **truncated=true 时绝不等于「附近就这些」**；每条带坐标、距离、"
                              "生物还带 tier/health、物品带 item_id，可用 use_on_entity/attack_entity"
                              "（按 id）或 move_to（按坐标）接着办。不在世界 ⇒ 如实失败，不假成功",
             "radius,kind,limit", ("locate",), "_tool_scan_entities"),
    ToolSpec("go_to_block", "寻路过去，到达后记住", "block,radius,remember",
             ("locate",), "_tool_go_to_block", job=True),
    ToolSpec("locate_structure", "/locate 查结构/群系（kind=structure|biome）", "name,kind",
             ("locate",), "_tool_locate_structure"),
    # ── gather ──
    ToolSpec("mine_block", "挖面前方块", "", ("gather",), "_tool_mine_block",
             ToolPermission(area=True, note="就近动手；受区域硬保护约束"), job=True),
    ToolSpec("collect_blocks", "采集方块", "block_type,count", ("gather",),
             "_tool_collect_blocks", ToolPermission(area=True, note="就近采集"), job=True),
    ToolSpec("drop_item", "丢弃物品", "item,count", ("gather",), "_tool_drop_item"),
    ToolSpec("sort_inventory", "整理背包", "", ("gather",), "_tool_sort_inventory"),
    ToolSpec("get_inventory", "查看背包", "", ("gather",), "_tool_get_inventory"),
    # ── craft ──
    ToolSpec("craft_item", "合成物品", "item,count", ("craft",), "_tool_craft_item",
             ToolPermission(tier="craft", deny_label="造东西",
                            note="须被授权（craft 能力表）；缺料先只用背包，未授权不开箱"), job=True),
    ToolSpec("craft_via_recipe", "原版配方合成（先看背包材料）", "item,count", ("craft",),
             "_tool_craft_via_recipe",
             ToolPermission(tier="craft", deny_label="造东西",
                            note="同一道授权门；材料优先用自己背包"), job=True),
    ToolSpec("place_block", "放置手中方块", "", ("craft",), "_tool_place_block",
             ToolPermission(area=True, note="就近放置；受区域硬保护约束")),
    ToolSpec("build", "真放一批方块：结构 wall/pen/platform/pillar；**material 必须点名**（本地不做材料启发式）；"
                      "一次只放伸手够得着的那批，够不着的如实报 remaining 并让你先走过去再叫一次",
             "x,y,z,structure,material,size,height", ("craft",), "_tool_build",
             ToolPermission(area=True, note="在坐标处建造"), job=True),
    # ── container ──
    ToolSpec("container_move", "容器内移物（省 to_slot＝背包↔快捷栏）",
             "from_slot,to_slot,count", ("container",), "_dispatch_container_move",
             ToolPermission(container=True, deny_label="翻箱子",
                            note="须获授权；no_touch 不碰、free 随取、其余走借用；用多少取多少")),
    ToolSpec("container_click", "点容器一格（pickup|quick_move|swap|throw）",
             "slot,click_type", ("container",), "_dispatch_container_click",
             ToolPermission(container=True, deny_label="翻箱子",
                            note="同容器访问授权；no_touch 不碰")),
    ToolSpec("container_close", "关掉容器界面", "", ("container",), "_tool_container_close"),
    ToolSpec("view_container", "**只读**看一眼容器里有什么（对照 !viewChest）：列出"
                               "{items:[{name,count,slot}], empty, truncated}，看完关掉界面，"
                               "**什么都不拿** —— 这是 haul「取多少 / 搬什么」**之前那一步**"
                               "（先看清有几件，再决定取多少、搬什么）。要有开着的容器"
                               "（先 go_to_block 过去 + use_on 右键打开，再叫我一次）；"
                               "no_touch 的箱子一律不给看，borrow_only 只读可以",
             "target,limit", ("container",), "_tool_view_container",
             ToolPermission(container=True, read_only=True, deny_label="翻箱子",
                            note="只读：no_touch 不碰、borrow_only 只读可以；只列内容不拿东西；"
                                 "没登记的箱子仍要翻箱授权")),
    ToolSpec("borrow_item", "从仓库借（须授权；不给 count＝整箱搬，拒绝）",
             "item,count,container", ("container",), "_dispatch_borrow_item",
             ToolPermission(container=True, borrow=True, deny_label="翻箱子",
                            note="容器授权 + 借用规则；上限 min(256, 需求×2)；拒绝整箱搬")),
    ToolSpec("return_item", "放回原容器（省 count＝全部剩余）", "item,count,container",
             ("container",), "_dispatch_return_item",
             ToolPermission(borrow=True,
                            note="归还借用台账里的物品并核对；需翻箱授权")),
    # ── farm ──
    ToolSpec("haul", "在容器之间搬运（from/to＝已记住的容器名或 self；省 item＝搬全部可搬的；"
                     "items=[{item,count}]＝一次搬多种；mode=item|whole|inside：whole＝整箱连"
                     "内容一起搬（潜影盒不拆散）、inside＝先取出盒里的这件再搬、箱名放 box；"
                     "单趟≤256、源箱留底不搬空、两侧对账（含内容物），卡住就报在哪一步）",
             "from,to,item,count,items,mode,box", ("farm",), "_dispatch_haul",
             ToolPermission(note="no_touch 不碰、borrow_only 只借不搬；单趟≤256、源箱留底")),
    ToolSpec("farm", "在授权农田里干活（action=till锄地|plant播种|harvest收割|replant补种；"
                     "mischief=true 表示这就是对别人家田的恶作剧，只有权限表放开才准；"
                     "全走右键/挖，缺种子或锄头如实报）", "plot,action,mischief",
             ("farm",), "_dispatch_farm",
             ToolPermission(area=True, deny_label="种田",
                            note="只在已授权农田动手；保护区里的作物不碰；别人家田看权限表")),
    ToolSpec("farm_cycle", "农场复合链：收割→补种→（田边取料）→可选合成→喂动物（feed=true；"
                           "喂食用通用右键、只喂授权畜栏）；craft=true 时缺料就地合成；"
                           "每一步产物/缺口都如实入账）", "plot,feed,craft,animal",
             ("farm",), "_dispatch_farm_cycle",
             ToolPermission(area=True, deny_label="种田",
                            note="全链只在授权地块/畜栏里动手；缺口如实报，不硬凑")),
    # ── combat ──
    ToolSpec("attack", "攻击附近敌人", "", ("combat",), "_tool_attack"),
    ToolSpec("attack_in_range",
             "**站在刷怪塔底部持续打怪**用这个：在半径 radius 内（默认 8，钳 1..32）持续出手，"
             "最多 max_seconds 秒（默认 30，钳 1..300）。kind=hostile（默认，**只打敌对**）| "
             "passive（连被动一起打）| any（除受保护档位外都打）—— 中立（村民/铁傀儡）与"
             "高危险（监守者）**无论 kind 是什么都不打**，玩家永不作为目标。"
             "**它不会移动、也不寻路**（要站位先用 go_afk，或就走过去站好）；"
             "拿不准要不要打就先问主人。结束（到点 / 没目标 / 被打断）你会收到回执"
             "（命中几次 + 退出原因码）—— 「已经发出去了」不等于「打到了」，看命中数。"
             "**主人专属**：持续出手等于把身体交出去打怪，只有 master 能命令。",
             "radius,kind,max_seconds", ("combat",), "_dispatch_attack_in_range", job=True),
    # ── items ──
    ToolSpec("equip_armor", "穿背包里的盔甲（没有就去容器找）", "", ("items",),
             "_tool_equip_armor",
             ToolPermission(note="背包没有时按翻箱授权（may_open_containers）去已知容器找")),
    ToolSpec("equip_weapon", "换上热栏里最好的武器", "", ("items",), "_tool_equip_weapon"),
    ToolSpec("select_hotbar", "切到热栏第 index 格（0-8）", "index", ("items",),
             "_tool_select_hotbar"),
    ToolSpec("use_on", "对目标使用**手里**的物品（右键）：点火把、倒水、剪羊毛、挤奶、喂食"
                       "这类原版交互都走它。target＝实体名/类型（从 state 快照里挑最近的），"
                       "或配合 x,y,z 对坐标/方块右键；想指定手里该拿哪件就带 item（给的这件"
                       "不在手上会如实报 wrong_item，我不替你乱换）。够不着（>4 格）报 "
                       "out_of_range，先 go_to_block/move_to 靠近。占 use 资源",
             "target,item,x,y,z", ("items",), "_tool_use_on"),
    # ── survive ──
    ToolSpec("eat", "吃东西", "", ("survive",), "_tool_eat", job=True),
    ToolSpec("sleep", "睡觉", "", ("survive",), "_tool_sleep", job=True),
    ToolSpec("trade", "与村民交易：list_only 先看报价清单，再按 offer（序号）或 item（结果物）挑一条成交；"
                      "全程不开界面，失败带 code（no_villager/out_of_range/no_menu/not_enough_items/offer_unavailable/trade_no_effect）",
             "entity_id,radius,offer,item,list_only", ("survive",), "_tool_trade", job=True),
    # ── night ──
    ToolSpec("sleep_in_bed", "走到最近的床睡到天亮（只能夜里）", "", ("night",),
             "_tool_sleep_in_bed", job=True),
    # ── guard ──
    ToolSpec("guard", "水平半径内驻守（默认 32；省坐标＝最近发话者位置）。"
                      "**要精准站位（刷怪塔底部 / 交易所那种半径 1 格）请用 go_afk** —— "
                      "guard 最小半径 4、尺度粗，只是「这一片别走远」",
             "x,y,z,radius", ("guard",), "_dispatch_guard", job=True),
    ToolSpec("guard_off", "取消驻守", "", ("guard",), "_tool_guard_off"),
    # ── guard / 站点（挂机点）──
    # "什么时候用我"写进描述：记站点 + 挂站点是**主人专属**（见 planner 的 master 门）；
    # afk_stop 不加门（停下是安全方向，谁都能把它从挂机里拉出来）。
    ToolSpec("remember_station",
             "把**现在站的这个位置**记成一个站点（挂机点）：坐标/朝向省略时＝照我此刻的位姿记"
             "（要精准记下刷怪塔底部就这么用）；kind 是站点类型标签，取 "
             "mob_farm | enderman_farm | iron_farm | crop | portal | custom（默认 custom）；"
             "radius 是驻守容差（0.5~16，默认 1 格）。之后用 go_afk 按名字挂上去。"
             "**主人专属**：站点表是持久的、挂上去还会夺走身体，只有 master 能让我记。",
             "name,kind,x,y,z,yaw,pitch,radius", ("guard",), "_dispatch_remember_station"),
    ToolSpec("go_afk",
             "**站到指定站点并驻守**：先寻路到那个点，然后「漂移超过半径就自己走回」（带滞回，"
             "不会在边界上一步一停）；需要精准站位（刷怪塔底部 / 交易所）就用它 —— "
             "guard 半径粗（最小 4），尺度不对。station＝站点名（先用 remember_station 记过或"
             "用 where_is 查）；keepalive=true 时每 3 秒小幅转头/摆臂防踢，**不移动**。"
             "站点不存在我**如实拒绝**（不猜、不自动新建）。**主人专属**：驻守期间身体被占住、"
             "不再响应别人，只有 master 能命令我挂。",
             "station,keepalive", ("guard",), "_dispatch_go_afk", job=True),
    ToolSpec("afk_stop",
             "结束站点驻守（玩家说「别挂了 / 回来吧」）。**不加门**：停下是安全方向，"
             "任何人都该能把我从挂机里拉出来。",
             "", ("guard",), "_dispatch_afk_stop"),
    # ── hud ──
    ToolSpec("set_hud_note", "把当前打算写进 HUD 描述词", "text", ("hud",),
             "_tool_set_hud_note"),
    # ── server ──
    ToolSpec("run_server_command", "下一条原版指令（须已在白名单勾选；未勾选/黑名单一律不发）",
             "command,args,confirm", ("server",), "_tool_run_server_command",
             ToolPermission(command=True, deny_label="下服务器指令",
                            note="须白名单勾选；黑名单/未确认一律不发")),
    ToolSpec("send_whisper", "给某人发私信；用了它 reply 请留空", "player,text", ("server",),
             "_tool_send_whisper", ToolPermission(note="私信走 outbound 清洗")),
    ToolSpec("tpa_respond", "回应传送请求：action=accept|deny", "action,player", ("server",),
             "_tool_tpa_respond", ToolPermission(note="按 TPA 分层策略回应")),
    ToolSpec("tpa_request", "主动请求传送到某人（默认只有主人可以）", "player", ("server",),
             "_tool_tpa_request", ToolPermission(note="TPA 策略决定谁能主动发起")),
    # ── memory ──
    ToolSpec("remember_poi", "记住一个位置坐标（**只记，不走过去、不交互**；要设家/绑重生点请用 set_home）",
             "name,category,x,y,z", ("memory",),
             "_tool_remember_poi"),
    # ── goal（模型的长期目标：写进任务列表，要显式结束）──
    ToolSpec("set_goal", "给自己立一个**长期目标**（会写进任务列表、一直占着，直到你用 "
                         "end_goal 显式结束它）：玩家交代一件要跨很多轮才做得完的事（"
                         "\"把这片林子砍完\"\"把仓库填满木板\"）时用它把目标记下来，之后按"
                         "事件推进；不要用它记一次性小动作",
             "text,priority", ("goal",), "_tool_set_goal"),
    ToolSpec("end_goal", "结束一个**长期目标**（省 id ＝结束当前那条）；**目标不显式结束就会"
                         "一直占着**，所以做完/放弃时一定要调它",
             "id,reason", ("goal",), "_tool_end_goal"),
    # ── territory ──
    ToolSpec("list_territory", "列出领地/基地/容器及其授权与保护项", "", ("territory",),
             "_tool_list_territory"),
    ToolSpec("grant_territory", "把这片区域的所有权授予我（master；grants=take,place,break,farm,store）",
             "name,kind,radius,grants", ("territory",), "_dispatch_grant_territory",
             ToolPermission(tier="territory", area=True, deny_label="把一片地的所有权交给我",
                            note="只有 master 能授予区域所有权")),
    ToolSpec("revoke_territory", "收回领地的所有权", "name", ("territory",),
             "_dispatch_revoke_territory",
             ToolPermission(tier="territory", deny_label="收回领地授权", note="master-only")),
    ToolSpec("set_base", "设/更新一个基地（省坐标＝脚下）", "name,kind,radius,x,y,z",
             ("territory",), "_dispatch_set_base",
             ToolPermission(tier="territory", deny_label="设基地", note="master-only")),
    ToolSpec("del_base", "删掉一个基地", "name", ("territory",), "_dispatch_del_base",
             ToolPermission(tier="territory", deny_label="删基地", note="master-only")),
    ToolSpec("remember_container", "记住箱子的确切位置与策略（free随取|borrow_only传递|no_touch不碰）",
             "name,x,y,z,kind,policy", ("territory",), "_dispatch_remember_container",
             ToolPermission(note="master 任意；friend 只能标自己的、且只到传递权（container_grant_policy）")),
    ToolSpec("grant_container", "给箱子设/改访问策略（friend 只能给自己的，且只到传递）",
             "name,policy", ("territory",), "_dispatch_grant_container",
             ToolPermission(note="master 任意；friend 只能标自己的、且只到传递权（container_grant_policy）")),
    ToolSpec("revoke_container", "收回箱子的访问授权（立即生效）", "name", ("territory",),
             "_dispatch_revoke_container",
             ToolPermission(note="master 任意；friend 只能收回自己的（can_authorize_containers + owner）")),
    ToolSpec("define_zone", "声明区域语义与硬保护（protect=animals,crops,blocks,containers）",
             "name,kind,x,y,z,radius,protect", ("territory",), "_dispatch_define_zone",
             ToolPermission(tier="zones", area=True, deny_label="声明区域保护", note="master-only")),
    ToolSpec("clear_zone", "删掉一个区域", "name", ("territory",), "_dispatch_clear_zone",
             ToolPermission(tier="zones", deny_label="删除区域保护", note="master-only")),
    ToolSpec("set_home", "把某个地方变成家：自己找床（找不到就造），找到后**走过去右键一次**把重生点绑到它；"
                        "只记坐标不等于绑上（缺料/不安全/没绑上都会如实报）",
             "base", ("territory",), "_dispatch_set_home"),
    ToolSpec("go_home", "回基地的床位（没床就回锚点）", "base", ("territory",), "_dispatch_go_home"),
    ToolSpec("go_to_death_point",
             "回**上次死亡点**去捡上次死时掉在那里的东西（**不是回家** —— 回家是 go_home）。"
             "默认最近一次；index=1/2 可回上上次/上上上次（最多记着 3 次）。"
             "没有死亡记录时如实拒绝（code=no_death_point）；我**不会**自己往回走，"
             "只有你（或玩家）叫我才去", "index", ("territory",),
             "_dispatch_go_to_death_point"),
) + composite_specs()
# ↑ 末尾接上复合体条目（声明在 `agent/composites.py`；顺序＝注册表顺序＝L3 展示顺序）。

# name → 规格（派发查表用）。
TOOL_BY_NAME: dict[str, ToolSpec] = {spec.name: spec for spec in TOOL_SPECS}

# 复合体声明校验（唯一判定处）：每一步**只准**引用已注册的工具（没有第二份清单可漏）。
def _validate_composites() -> None:
    for comp in COMPOSITES:
        validate_composite(comp, is_registered=lambda name: name in TOOL_BY_NAME)


_validate_composites()

# ── 阶段 → 注入哪些组（顺序即 prompt 中的展示顺序）──────────────
# night / guard / hud / memory 全局注入：睡觉、守点、HUD 描述词与记忆点在任何阶段
# 都可能有意义（用户说"在这儿守着"时他可能正在看着 AI 挖矿）。
PHASE_GROUPS: dict[str, tuple[str, ...]] = {
    "idle": ("move", "locate", "gather", "craft", "container", "farm", "guard", "items",
             "survive", "combat", "hud", "night", "server", "memory", "goal", "territory",
             COMPOSITE_GROUP),
    "travel": ("move", "locate", "combat", "guard", "items", "container", "farm", "survive",
               "hud", "night", "server", "memory", "goal", "territory", COMPOSITE_GROUP),
    "gather": ("move", "locate", "gather", "guard", "items", "craft", "container", "farm",
               "hud", "night", "server", "memory", "goal", "territory", COMPOSITE_GROUP),
    "craft": ("move", "locate", "craft", "container", "farm", "guard", "items", "gather",
              "hud", "night", "server", "memory", "goal", "territory", COMPOSITE_GROUP),
    "survive": ("survive", "hud", "night", "server", "memory", "goal", "guard", "farm",
                "items", "move", "locate", "territory", COMPOSITE_GROUP),
}


def _group_actions() -> dict[str, tuple[tuple[str, str, str], ...]]:
    """从 `TOOL_SPECS` 生成分组动作表（组序、组内序沿用注册表顺序）。"""
    grouped: dict[str, list[tuple[str, str, str]]] = {}
    for spec in TOOL_SPECS:
        for group in spec.groups:
            grouped.setdefault(group, []).append((spec.name, spec.args, spec.description))
    return {group: tuple(actions) for group, actions in grouped.items()}


# ACTION_GROUPS：旧接口保留（测试与预览用），但**由注册表生成**，不再是第二份手写表。
ACTION_GROUPS: dict[str, tuple[tuple[str, str, str], ...]] = _group_actions()


def _actions_for_phase(phase: str) -> list[tuple[str, str, str]]:
    """当前阶段该注入的动作（去重、保持顺序）。行为与旧实现逐字一致。"""
    groups = PHASE_GROUPS.get(phase, PHASE_GROUPS["idle"])
    seen: set[str] = set()
    out: list[tuple[str, str, str]] = []
    for group in groups:
        for action in ACTION_GROUPS.get(group, ()):
            if action[0] not in seen:
                seen.add(action[0])
                out.append(action)
    return out


def specs_for_phase(phase: str) -> list[ToolSpec]:
    """当前阶段该注入的工具规格（顺序稳定、按名去重）。"""
    return [TOOL_BY_NAME[name] for name, _, _ in _actions_for_phase(phase)]


def build_tool_schemas(phase: str = "idle") -> list[dict]:
    """L3：当前阶段该有的工具定义（OpenAI tools 格式）——由注册表生成。

    工具走 API `tools` 参数，**不内联进提示词**；阶段过滤沿用 `PHASE_GROUPS`。
    """
    return [spec.schema() for spec in specs_for_phase(phase)]
