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

    def set_wire(self, wire):
        self.wire = wire

    def set_command_observer(self, observer):
        self._command_observer = observer

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
        return self._control.flush()

    def release_control(self) -> None:
        """松手：清空控制流按键与视角并立刻下发一次。"""
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

    def use_item(self) -> dict:
        """Use held item (eat, drink, activate)."""
        return self._send_cmd("use_item", {})

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

    # ── State Queries (mineflayer, mindcraft) ──────────────────

    def get_state(self) -> dict:
        """Get full player/world state."""
        return self._send_cmd("get_state", {})

    # ── Chat ────────────────────────────────────────────────────

    def send_chat(self, message: str) -> dict:
        """Send a chat message (rate-limited at 1.5s)."""
        return self._send_cmd("send_chat", {"message": message})

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

    def trade(self, villager_type: str) -> dict:
        """Trade with a villager of specified type."""
        return self._send_cmd("trade", {"villager_type": villager_type})

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

        传空文本＝立刻清空（Java 侧 HudNote 的行为）。
        """
        return self._send_cmd("set_hud_note", {"text": str(text or ""), "seconds": float(seconds)})

    def run_command(self, command: str) -> dict:
        """原版斜杠命令直通（如 `locate structure minecraft:village`）。

        前导 `/` 在这里就剥掉：`mc.player.connection.sendCommand()` 自己会再加一个，
        双斜杠会被原版当成"聊天内容"而不是命令。
        """
        cleaned = str(command or "").strip()
        while cleaned.startswith("/"):
            cleaned = cleaned[1:]
        return self._send_cmd("run_command", {"command": cleaned.strip()})

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
