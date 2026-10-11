"""
Skills — Python skill library matching mineflayer's API surface.
Sends action commands to Java mod via wire protocol.

Reference projects:
  - mineflayer: lib/plugins/ (physics, digging, inventory, place_block, etc.)
  - mindcraft: commands/actions.js (25+ actions)
  - TouhouLittleMaid: entity/task/ (23 tasks)
"""

import json
import logging
import time
from contextlib import contextmanager
from typing import Any, Optional

from .outbound import sanitize_outbound
from .resource_lease import (OWNER_P0_REFLEX, RESOURCE_ATTACK, RESOURCE_LOOK, RESOURCE_MOVE,
                             RESOURCE_USE, register_survival_hold)

logger = logging.getLogger("skills")


class Skills:
    """
    Skill library — Python-side functions that send commands to Java mod.
    Mirrors mineflayer's bot API surface.

    Each method returns dict: {"success": bool, "message": str, "req_id": str}
    """

    def __init__(self, wire=None):
        self.wire = wire
        self._command_observer = None
        self._command_context = "default"
        self._control = None
        # 出站清洗的审计回调（session 注入）：清洗真的发生时把"原文/清洗后"记下来。
        self._outbound_observer = None
        # 资源租约（session 注入）：P0 反射走**生存通道**登记控制流占用（只登记、不拦，
        # 命比调度重要）。没注入＝行为与字节照旧。
        self._resource_lease = None
        self._lease_enabled_provider = None
        self._lease_held = frozenset()

    def set_wire(self, wire):
        self.wire = wire

    def set_resource_lease(self, lease, enabled_provider=None):
        """挂上资源租约（P0 反射控制流占用登记 + 总开关）。"""
        self._resource_lease = lease
        self._lease_enabled_provider = enabled_provider

    def set_command_observer(self, observer):
        self._command_observer = observer

    def set_outbound_observer(self, observer):
        """挂上出站清洗审计（谁调用、原文、清洗后）；None 表示不记。"""
        self._outbound_observer = observer if callable(observer) else None

    def _sanitize(self, text, kind: str) -> str:
        """出站文本的唯一清洗入口（本类所有出口都走它）。"""
        return sanitize_outbound(text, kind=kind, observer=self._outbound_observer)

    # ── 低层直驱（控制流）───────────────────────────────────────
    # 命令通道是请求/响应，适合离散动作；控制流无 id、无回执、latest-wins，
    # 适合 20-50Hz 连续运动/视角。应急逃生（P0）用这个——高层寻路在岩浆里
    # 既慢又容易失败。

    def set_control_stream(self, control):
        """挂上控制流出口（P0 反射层 / VLA 用）。"""
        self._control = control

    def drive(self, *, forward=False, back=False, left=False, right=False,
              jump=False, sneak=False, sprint=False, attack=False, use=False,
              look_dx=0.0, look_dy=0.0, look_yaw=None, look_pitch=None) -> bool:
        """低层直驱：写控制流，不等回执。返回是否成功下发。

        attack/use 是离散意图：按住会持续触发，且"按下又松开"落在两次 tick 之间
        也不会丢（Java 侧有边沿计数，见 ControlStream/EdgeTrigger）。

        注意：控制流活跃时会独占运动与视角（Java 侧它是最后写入者），
        用完必须 `release_control()`，否则高层寻路会被持续压制。
        """
        if self._control is None:
            return False
        self._control.set(forward=forward, back=back, left=left, right=right,
                          jump=jump, sneak=sneak, sprint=sprint,
                          attack=attack, use=use)
        if look_dx or look_dy:
            self._control.look(look_dx, look_dy)
        if look_yaw is not None or look_pitch is not None:
            self._control.look_at(
                look_yaw if look_yaw is not None else 0.0,
                look_pitch if look_pitch is not None else 0.0,
            )
        # P0 控制流的资源占用：**只登记、不拦**（生存通道，永远可抢）。
        self._note_survival_hold(forward=forward, back=back, left=left, right=right,
                                 jump=jump, sneak=sneak, sprint=sprint,
                                 attack=attack, use=use, look=bool(
                                     look_dx or look_dy or look_yaw is not None
                                     or look_pitch is not None))
        return self._control.flush()

    def _note_survival_hold(self, *, forward=False, back=False, left=False, right=False,
                            jump=False, sneak=False, sprint=False, attack=False,
                            use=False, look=False) -> None:
        """把 P0 反射当前占用的控制流资源登记进资源租约（生存优先级）。

        没接线 / 总开关关掉 ⇒ 什么都不做（行为照旧）。**只登记不拦**：P0 永不被本批挡住 ✗。
        """
        lease = self._resource_lease
        if lease is None:
            return
        provider = self._lease_enabled_provider
        if provider is not None:
            try:
                if not provider():
                    return
            except Exception as exc:  # noqa: BLE001 — 开关读失败按"开"处理（保守：登记）
                logger.debug("[Skills] 资源租约开关读取失败: %s", exc)
        resources = set()
        if any((forward, back, left, right, jump, sneak, sprint)):
            resources.add(RESOURCE_MOVE)
        if attack:
            resources.add(RESOURCE_ATTACK)
        if use:
            resources.add(RESOURCE_USE)
        if look:
            resources.add(RESOURCE_LOOK)
        frozen = frozenset(resources)
        if frozen == self._lease_held:
            return
        if self._lease_held:
            try:
                lease.release(OWNER_P0_REFLEX, "P0 控制流切换")
            except Exception as exc:  # noqa: BLE001
                logger.debug("[Skills] P0 资源释放失败: %s", exc)
            self._lease_held = frozenset()
        if frozen:
            register_survival_hold(lease, resources=frozen, reason="P0 反射控制流",
                                   kind="reflex", target="p0")
            self._lease_held = frozen

    def release_control(self) -> None:
        """松手：清空控制流按键与视角并立刻下发一次。"""
        if self._resource_lease is not None and self._lease_held:
            try:
                self._resource_lease.release(OWNER_P0_REFLEX, "P0 控制流松手")
            except Exception as exc:  # noqa: BLE001
                logger.debug("[Skills] P0 资源释放失败: %s", exc)
            self._lease_held = frozenset()
        if self._control is not None:
            self._control.release()

    @contextmanager
    def command_context(self, context: str):
        previous = self._command_context
        self._command_context = context
        try:
            yield
        finally:
            self._command_context = previous

    # ── Movement (mineflayer physics.js) ───────────────────────

    def set_control_state(self, control: str, state: bool) -> dict:
        """Set individual movement key state (mineflayer-style).
        Controls: forward, back, left, right, jump, sneak, sprint"""
        return self._send_cmd("set_control_state", {"control": control, "state": state})

    def move_to(self, x: float, y: float, z: float, speed: float = 1.0) -> dict:
        """Move to absolute coordinates (continuous W key)."""
        return self._send_cmd("move_to", {"x": x, "y": y, "z": z, "speed": speed})

    def look_at(self, x: float, y: float, z: float) -> dict:
        """Look at a coordinate."""
        return self._send_cmd("look_at", {"x": x, "y": y, "z": z})

    def look_at_entity(self, entity_id: int) -> dict:
        """Look at a specific entity by ID."""
        return self._send_cmd("look_at_entity", {"id": entity_id})

    def jump(self) -> dict:
        return self._send_cmd("jump", {})

    def sneak(self, activate: bool = True) -> dict:
        return self._send_cmd("sneak", {"sneak": activate})

    def sprint(self, activate: bool = True) -> dict:
        return self._send_cmd("sprint", {"sprint": activate})

    def stop_all(self) -> dict:
        """Stop all movement and actions."""
        return self._send_cmd("stop_all", {})

    def toggle_ai(self) -> dict:
        """Toggle AI/User control mode."""
        return self._send_cmd("toggle_ai", {})

    # ── Combat (mineflayer entities.js, mindcraft commands) ────

    def attack(self) -> dict:
        """Attack the entity currently being looked at."""
        return self._send_cmd("attack", {})

    def attack_entity(self, entity_id: int) -> dict:
        """Attack a specific entity by ID."""
        return self._send_cmd("attack_entity", {"entity_id": entity_id})

    # ── Block Interaction (mineflayer digging.js) ──────────────

    def mine_block(self) -> dict:
        """Start mining the block being looked at (auto-equips best tool)."""
        return self._send_cmd("mine_block", {})

    def stop_digging(self) -> dict:
        """Stop current digging."""
        return self._send_cmd("stop_digging", {})

    def auto_equip(self) -> dict:
        """Auto-equip best tool for targeted block."""
        return self._send_cmd("auto_equip", {})

    def place_block(self) -> dict:
        """Place held block at targeted position."""
        return self._send_cmd("place_block", {})

    def interact_block(self) -> dict:
        """Right-click the targeted block."""
        return self._send_cmd("interact_block", {})

    def use_on(self) -> dict:
        """Right-click whatever is targeted (block or entity)."""
        return self._send_cmd("use_on", {})

    def use_item(self, ticks: Optional[int] = None) -> dict:
        """Use held item (eat, drink, activate).

        `ticks` 可选：Java 侧是"按住右键 N tick（默认 4）再松开"的通用语义
        （锄地/播种靠它，不需要任何专门的农活命令）；不给就沿用 Java 的默认值。
        """
        args: dict[str, Any] = {}
        if ticks is not None:
            args["ticks"] = int(ticks)
        return self._send_cmd("use_item", args)

    # ── Entity Interaction (mineflayer) ─────────────────────────

    def use_on_entity(self, entity_id: int) -> dict:
        """Right-click a specific entity."""
        return self._send_cmd("use_on_entity", {"id": entity_id})

    # ── Inventory (mineflayer inventory.js) ─────────────────────

    def get_inventory(self) -> dict:
        """Get full player inventory."""
        return self._send_cmd("get_inventory", {})

    def select_hotbar(self, index: int) -> dict:
        """Select hotbar slot 0-8."""
        return self._send_cmd("select_hotbar", {"index": index})

    def equip(self, slot: str = "mainhand") -> dict:
        """Equip item from hotbar."""
        return self._send_cmd("equip", {"slot": slot})

    def equip_weapon(self) -> dict:
        """换成热栏里最好的武器（按物品 id 判定剑>三叉戟/锤>斧，材质越高越优先）。

        用户要求："他现在还要学会切物品栏，因为他的物品栏里边有装备" —— 这条就是让 LLM
        在动手前（或挨打后）主动换家伙；Java 侧的战斗反射也会自动换一次。
        """
        return self._send_cmd("equip_weapon", {})

    def drop(self, slot: int = -1, count: int = 1) -> dict:
        return self._send_cmd("drop", {"slot": slot, "count": count})

    # ── Container (mineflayer chest.js) ─────────────────────────

    def open_container(self) -> dict:
        """Open container by right-clicking targeted block."""
        return self._send_cmd("use_on", {})

    def get_container(self) -> dict:
        """Read contents of currently open container."""
        return self._send_cmd("get_container", {})

    def take_item(self, slot: int) -> dict:
        """Take item from container slot into player inventory."""
        return self._send_cmd("take_item", {"slot": slot})

    def put_item(self, slot: int) -> dict:
        """Put item from player inventory into container slot."""
        return self._send_cmd("put_item", {"slot": slot})

    def close_container(self) -> dict:
        """Close current container GUI."""
        return self._send_cmd("close_container", {})

    # ── 容器 / 装备（Java 新命令，字段名逐字对齐 ActionExecutor） ──
    # 容器槽位：0..slots-1（背包菜单即 0..45：5-8 盔甲 / 9-35 背包 / 36-44 快捷栏 /
    # 45 副手）；`player_inv_start` 是玩家背包区在箱子菜单里的起始槽位。

    def equip_armor(self) -> dict:
        """在背包/快捷栏里找盔甲并穿上（Java 侧 QUICK_MOVE 到装备槽并校验真的穿上）。

        用户口径："盔甲要点容器找盔甲" —— 这条就是那次点击；失败 reason="no_armor"。
        """
        return self._send_cmd("equip_armor", {})

    def craft_via_recipe(self, item: str, count: int = 1) -> dict:
        """用**原版配方**合成（Java 侧按配方放置，Python 不实现合成算法）。

        `count` 缺省 1；>1 时 Java 侧 useMaxItems=true。材料够不够由 Python 侧先评估
        （见 `agent.craft.evaluate_craft`），这里只负责把命令发出去。
        """
        return self._send_cmd("craft_via_recipe", {"item": str(item), "count": int(count)})

    def container_move(self, from_slot: int, to_slot: Optional[int] = None,
                       count: Optional[int] = None) -> dict:
        """在当前打开的容器里把 `from_slot` 的物品移到 `to_slot`。

        `to_slot` 省略（或 -1）＝ QUICK_MOVE（原版 shift 点击，背包↔快捷栏）；
        `count` 省略＝整组。**取用要按缺口给 count**，别整组搬空。
        """
        args: dict[str, Any] = {"from_slot": int(from_slot)}
        if to_slot is not None:
            args["to_slot"] = int(to_slot)
        if count is not None:
            args["count"] = int(count)
        return self._send_cmd("container_move", args)

    def container_click(self, slot: int, button: int = 0, click_type: str = "pickup") -> dict:
        """点当前容器菜单的某一格（`click_type` = Java `ContainerInput` 枚举小写）。"""
        return self._send_cmd("container_click", {
            "slot": int(slot), "button": int(button), "click_type": str(click_type)})

    def container_close(self) -> dict:
        """关掉当前打开的容器菜单（幂等：没开也返回成功）。"""
        return self._send_cmd("container_close", {})

    # ── State Queries (mineflayer, mindcraft) ──────────────────

    def get_state(self) -> dict:
        """Get full player/world state."""
        return self._send_cmd("get_state", {})

    # ── Chat ────────────────────────────────────────────────────

    def send_chat(self, message: str) -> dict:
        """Send a chat message (rate-limited at 1.5s).

        出站**唯一出口**之一：文本先过 `sanitize_outbound` —— 以 `/`（或前面垫了空格/
        全角空格的 `/`）开头的 AI 回复会被剥成普通聊天，**永远不变成指令**。
        """
        cleaned = self._sanitize(message, "send_chat")
        return self._send_cmd("send_chat", {"message": cleaned})

    # ── Behavior Control (mindcraft modes.js) ───────────────────

    def behavior_enable(self) -> dict:
        """Enable autonomous behavior modes."""
        return self._send_cmd("behavior_enable", {})

    def behavior_disable(self) -> dict:
        """Disable autonomous behavior modes."""
        return self._send_cmd("behavior_disable", {})

    # ── Advanced Actions ────────────────────────────────────────

    def follow_player(self, player_name: str) -> dict:
        """Follow a specific player."""
        return self._send_cmd("follow_player", {"player": player_name})

    def craft_item(self, item_name: str, count: int = 1) -> dict:
        """Craft an item by name."""
        return self._send_cmd("craft_item", {"item": item_name, "count": count})

    def collect_blocks(self, block_type: str, count: int = 1) -> dict:
        """Collect specified number of blocks."""
        return self._send_cmd("collect_blocks", {"block_type": block_type, "count": count})

    def explore(self, radius: int = 16) -> dict:
        """Explore the area within radius."""
        return self._send_cmd("explore", {"radius": radius})

    def trade(self, offer: int | None = None, item: str = "", list_only: bool = False,
              radius: float | None = None, entity_id: int | None = None) -> dict:
        """跟村民做买卖：`list_only` 先看报价；否则按 `offer`（序号）或 `item`（结果物）挑一条。

        参数名与模组侧 `trade` 命令**一一对应** ✓（那边实证过：`entity_id|radius, offer, item, list_only`）。
        """
        args: dict = {}
        if entity_id is not None:
            args["entity_id"] = int(entity_id)
        if radius is not None:
            args["radius"] = float(radius)
        if offer is not None:
            args["offer"] = int(offer)
        if item:
            args["item"] = str(item)
        if list_only:
            args["list_only"] = True
        return self._send_cmd("trade", args)

    def sleep(self) -> dict:
        """Sleep in a bed."""
        return self._send_cmd("sleep", {})

    def eat(self) -> dict:
        """Eat food from inventory."""
        return self._send_cmd("eat", {})

    def drop_item(self, item: str, count: int = 1) -> dict:
        """Drop specified item."""
        return self._send_cmd("drop_item", {"item": item, "count": count})

    def sort_inventory(self) -> dict:
        """Sort inventory items."""
        return self._send_cmd("sort_inventory", {})

    def build(self, x: float, y: float, z: float, structure: str) -> dict:
        """Build a structure at position."""
        return self._send_cmd("build", {"x": x, "y": y, "z": z, "structure": structure})

    # ── Locator / POI / Night (Java 侧扫描 + 自动寻路 + 持久记忆) ──
    # block 支持通配：精确 `minecraft:chest` / 后缀 `*_bed` / 前缀 `oak_*` /
    # 包含 `*chest*`。通配由 Java 侧实现，Python 只负责把字符串透传。

    def find_block(self, block: str, radius: int = 32, limit: int = 8) -> dict:
        """扫描附近是否存在某方块（只查不走），返回命中列表与最近一个。"""
        return self._send_cmd("find_block", {"block": block, "radius": radius, "limit": limit})

    def go_to_block(self, block: str, radius: int = 32, remember: bool = True) -> dict:
        """找最近的该方块并自动寻路过去；到达后写入 POI 记忆。"""
        return self._send_cmd(
            "go_to_block", {"block": block, "radius": radius, "remember": remember})

    def sleep_in_bed(self) -> dict:
        """走到最近的床并睡到天亮（Java 侧负责寻路 + 点床）。

        wire 命令名沿用已存在的 `sleep`。status=walking 表示"正在走去床边"，
        后续进度通过 task_state 反映 —— 不要重复下发第二次。
        """
        return self._send_cmd("sleep", {})

    def remember_poi(self, name: str = "", category: str = "waypoint",
                     x: Optional[float] = None, y: Optional[float] = None,
                     z: Optional[float] = None) -> dict:
        """把一个坐标记成命名记录点（持久）。省略坐标＝当前脚下。"""
        args: dict[str, Any] = {"name": name, "category": category}
        if x is not None:
            args["x"] = x
        if y is not None:
            args["y"] = y
        if z is not None:
            args["z"] = z
        return self._send_cmd("remember_poi", args)

    def remember_station(self, name: str, kind: Optional[str] = None,
                         x: Optional[float] = None, y: Optional[float] = None,
                         z: Optional[float] = None, yaw: Optional[float] = None,
                         pitch: Optional[float] = None,
                         radius: Optional[float] = None) -> dict:
        """记一个站点（显式坐标优先、缺省回退当前位姿；同名覆盖）。

        参数名与 Java `ActionExecutor.handleRememberStation` **逐字对齐**
        （name / kind? / x? / y? / z? / yaw? / pitch? / radius?）。后端回灌时把
        持久化的坐标整份传进来 —— 缺了坐标 Java 会回退到当前位姿，那不是回灌。
        """
        args: dict[str, Any] = {"name": str(name)}
        if kind is not None:
            args["kind"] = str(kind)
        for key, value in (("x", x), ("y", y), ("z", z),
                           ("yaw", yaw), ("pitch", pitch), ("radius", radius)):
            if value is not None:
                args[key] = float(value)
        return self._send_cmd("remember_station", args)

    def remember_route(self, name: str, points: list) -> dict:
        """把整条路径灌回模组（`points:[{x,y,z,yaw?}]`，**不抽稀**）。

        参数名与 Java `ActionExecutor.handleRememberRoute` 逐字对齐；点数 <2 或 >2000
        会被 Java 整条拒绝（被拒**不覆盖**同名旧条目）。
        """
        return self._send_cmd("remember_route",
                              {"name": str(name), "points": list(points or [])})

    def route_export(self, name: str) -> dict:
        """向模组**按需**取一条路径的点列（`route_export {name}`，Java 侧一手导出）。

        `state.routes[]` 只报点数（每帧几十 KB 不能塞点列），所以跨重启回灌时先来这里
        取点列，再用 `remember_route` 灌回去。回执里失败原因逐字对齐 Java：
        `unknown_route`（没这条）/ `no_geometry`（有点数没坐标）/ `need_name`。
        **点列不进每帧 state**，只有这一条按需出口。
        """
        return self._send_cmd("route_export", {"name": str(name)})

    def define_zone(self, name: str, kind: str = "", x: Optional[float] = None,
                    y: Optional[float] = None, z: Optional[float] = None,
                    radius: Optional[float] = None, box: Optional[dict] = None,
                    protect: Optional[list] = None) -> dict:
        """声明一个区域：**语义（kind）+ 硬保护（protect）**，硬拦在 Java 侧生效。

        参数名与 Java `ActionExecutor.handleDefineZone` 逐字对齐：`name / kind / x,y,z /
        radius? / box?{min_x..max_z} / protect[]`（radius = 球；box = 轴对齐长方体，两者都给
        按 box 判）。缺形状会被 Java 拒（`need_shape`），本地先挡一道。
        """
        args: dict[str, Any] = {"name": str(name), "kind": str(kind or "")}
        for key, value in (("x", x), ("y", y), ("z", z), ("radius", radius)):
            if value is not None:
                args[key] = float(value)
        if isinstance(box, dict):
            args["box"] = {key: float(box[key]) for key in
                           ("min_x", "min_y", "min_z", "max_x", "max_y", "max_z")
                           if key in box}
        if protect:
            args["protect"] = [str(item) for item in protect]
        return self._send_cmd("define_zone", args)

    def clear_zone(self, name: str) -> dict:
        """删掉一个区域（不存在则 Java 回 `unknown_zone`，如实上报）。"""
        return self._send_cmd("clear_zone", {"name": str(name)})

    # ── 守卫 / HUD 描述词 / 原版命令直通 ────────────────────────
    # 参数名与 Java `ActionExecutor` 逐字对齐（写错不会报错，只会落进 default）：
    #   guard        → x? / y? / z? / radius? / enabled?
    #   set_hud_note → text / seconds?
    #   run_command  → command（**不带前导斜杠**；Java 侧也会兜底剥一次）

    def guard(self, x: Optional[float] = None, y: Optional[float] = None,
              z: Optional[float] = None, radius: int = 32) -> dict:
        """在 (x,y,z) 的水平半径内驻守（越界自动走回）；省略坐标＝当前脚下。"""
        args: dict[str, Any] = {"radius": int(radius)}
        if x is not None:
            args["x"] = float(x)
        if y is not None:
            args["y"] = float(y)
        if z is not None:
            args["z"] = float(z)
        return self._send_cmd("guard", args)

    def guard_off(self) -> dict:
        """取消驻守（Java 侧收到 enabled=false）。"""
        return self._send_cmd("guard", {"enabled": False})

    def set_hud_note(self, text: str, seconds: float = 30.0) -> dict:
        """把一句描述词挂到 HUD 状态词后面；seconds<=0 表示留到被替换/清空。

        传空文本＝立刻清空（Java 侧 HudNote 的行为）。描述词同样过出站清洗
        （它是 AI 生成的文本，也一样不许以 `/` 开头）。
        """
        cleaned = self._sanitize(text, "set_hud_note")
        return self._send_cmd("set_hud_note", {"text": cleaned, "seconds": float(seconds)})

    def run_command(self, command: str) -> dict:
        """原版斜杠命令直通（如 `locate structure minecraft:village`）。

        前导 `/` 在这里就剥掉：`mc.player.connection.sendCommand()` 自己会再加一个，
        双斜杠会被原版当成"聊天内容"而不是命令。换行同样要清掉 —— 一条指令带上换行
        就等于两条，那是注入的标准手法。

        注意：这是**底层通道**，只允许被 `CommandGate`（白名单/黑名单/二次确认）
        放行过的指令调用；调用方不得从模型文本直接拼字符串进这里。
        """
        cleaned = self._sanitize(command, "run_command")
        cleaned = cleaned.replace("\r", " ").replace("\n", " ").strip()
        while cleaned.startswith("/"):
            cleaned = cleaned[1:]
        return self._send_cmd("run_command", {"command": cleaned.strip()})

    def click_screen_button(self, match: str) -> dict:
        """在**已打开**的界面里做**控件级**点击（Java 命令 `click_screen_button {match}`）。

        参数名与 Java 侧逐字对齐：`match`（字符串，按钮上的文字）。Java 侧不移动鼠标、
        不打开界面；这里也不做任何界面/鼠标模拟。`match` 同样过出站清洗（剥前导 `/`）。
        """
        target = self._sanitize(match, "click_screen_button").strip()
        return self._send_cmd("click_screen_button", {"match": target})

    # ── Internal ────────────────────────────────────────────────

    def _send_cmd(self, cmd: str, args: dict) -> dict:
        """Send a command via wire and return immediately (async)."""
        if not self.wire:
            logger.warning("[Skills] No wire, skipping %s", cmd)
            return {"success": False, "message": "No wire connection"}
        req_id = self.wire.send_command(cmd, args)
        if self._command_observer:
            try:
                self._command_observer(cmd, req_id, self._command_context, args)
            except Exception as exc:
                logger.debug("[Skills] Command observer failed for %s: %s", cmd, exc)
        logger.debug("[Skills] Sent: %s (req=%s)", cmd, req_id)
        return {"success": True, "message": f"{cmd} sent", "req_id": req_id}
