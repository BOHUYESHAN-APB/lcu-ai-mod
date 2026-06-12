"""
Action Agent — 行动处理 Agent。
专门处理移动、战斗、合成等游戏动作。
与 Conversation Agent 分离，实现并行处理。

参考 mineflayer 的 controlState 系统：
- 独立的控制状态管理
- 持续的行为循环
- 优先级-based 任务调度
"""

import asyncio
import logging
import time
from typing import Optional, Dict, Any, Callable
from dataclasses import dataclass, field
from enum import Enum

logger = logging.getLogger("action_agent")


class ActionPriority(Enum):
    """动作优先级"""
    CRITICAL = 0    # 紧急：躲避危险、自救
    HIGH = 1        # 高：战斗、跟随
    MEDIUM = 2      # 中：采集、合成
    LOW = 3         # 低：探索、闲逛


@dataclass
class ActionTask:
    """动作任务"""
    name: str
    action: Callable
    priority: ActionPriority
    created_at: float = field(default_factory=time.time)
    timeout: float = 60.0
    is_blocking: bool = False  # 是否阻塞其他动作
    
    @property
    def is_expired(self) -> bool:
        return time.time() - self.created_at > self.timeout


class ActionAgent:
    """
    Action Agent — 处理所有游戏动作。
    
    核心职责：
    1. 接收动作指令
    2. 管理控制状态
    3. 执行移动、战斗、合成等
    4. 优先级调度
    5. 持续行为循环
    
    与 Conversation Agent 的关系：
    - Conversation Agent 生成动作指令
    - Action Agent 执行动作
    - 两者通过消息队列通信
    """
    
    def __init__(self, skills):
        self.skills = skills
        
        # 任务队列（按优先级排序）
        self.task_queue: asyncio.PriorityQueue = asyncio.PriorityQueue()
        
        # 当前执行的任务
        self.current_task: Optional[ActionTask] = None
        
        # 控制状态（类似 mineflayer 的 controlState）
        self.control_state = {
            "forward": False,
            "back": False,
            "left": False,
            "right": False,
            "jump": False,
            "sneak": False,
            "sprint": False,
        }
        
        # 行为状态
        self.behavior_state = "idle"  # idle, moving, fighting, collecting, etc.
        
        # 异步处理任务
        self._processing_task: Optional[asyncio.Task] = None
        self._behavior_task: Optional[asyncio.Task] = None
        self._is_running = False
    
    async def start(self):
        """启动 Action Agent"""
        self._is_running = True
        self._processing_task = asyncio.create_task(self._processing_loop())
        self._behavior_task = asyncio.create_task(self._behavior_loop())
        logger.info("[ActionAgent] Started")
    
    async def stop(self):
        """停止 Action Agent"""
        self._is_running = False
        
        if self._processing_task:
            self._processing_task.cancel()
            try:
                await self._processing_task
            except asyncio.CancelledError:
                pass
        
        if self._behavior_task:
            self._behavior_task.cancel()
            try:
                await self._behavior_task
            except asyncio.CancelledError:
                pass
        
        # 清除所有控制状态
        self.clear_controls()
        
        logger.info("[ActionAgent] Stopped")
    
    async def _processing_loop(self):
        """主处理循环 — 处理任务队列"""
        while self._is_running:
            try:
                # 获取最高优先级任务
                try:
                    priority, task = await asyncio.wait_for(
                        self.task_queue.get(),
                        timeout=0.1
                    )
                    
                    # 检查任务是否过期
                    if task.is_expired:
                        logger.warning("[ActionAgent] Task expired: %s", task.name)
                        continue
                    
                    # 执行任务
                    self.current_task = task
                    await self._execute_task(task)
                    self.current_task = None
                    
                except asyncio.TimeoutError:
                    continue
                    
            except Exception as e:
                logger.error("[ActionAgent] Error in processing loop: %s", e)
                await asyncio.sleep(1)
    
    async def _behavior_loop(self):
        """行为循环 — 持续执行自主行为"""
        while self._is_running:
            try:
                # 如果没有任务，执行自主行为
                if self.task_queue.empty() and self.current_task is None:
                    await self._autonomous_behavior()
                
                await asyncio.sleep(0.05)  # 50ms 间隔（20 TPS）
                
            except Exception as e:
                logger.error("[ActionAgent] Error in behavior loop: %s", e)
                await asyncio.sleep(1)
    
    async def _execute_task(self, task: ActionTask):
        """执行单个任务"""
        logger.info("[ActionAgent] Executing task: %s (priority=%s)", 
                   task.name, task.priority.name)
        
        self.behavior_state = task.name
        
        try:
            # 执行动作
            if asyncio.iscoroutinefunction(task.action):
                await task.action()
            else:
                await asyncio.to_thread(task.action)
        except Exception as e:
            logger.error("[ActionAgent] Task %s failed: %s", task.name, e)
        finally:
            self.behavior_state = "idle"
    
    async def _autonomous_behavior(self):
        """自主行为 — 没有任务时自动执行"""
        # 这里可以调用 Java 端的自主行为系统
        # 或者执行简单的巡逻、采集等
        pass
    
    # ── 任务提交 API ──
    
    async def submit_action(self, name: str, action: Callable, 
                           priority: ActionPriority = ActionPriority.MEDIUM,
                           timeout: float = 60.0, is_blocking: bool = False):
        """提交动作任务"""
        task = ActionTask(
            name=name,
            action=action,
            priority=priority,
            timeout=timeout,
            is_blocking=is_blocking
        )
        
        # 优先级越低（数字越小）越先执行
        await self.task_queue.put((priority.value, task))
        
        logger.info("[ActionAgent] Task submitted: %s (priority=%s)", 
                   name, priority.name)
    
    async def move_to(self, x: float, y: float, z: float, speed: float = 1.0):
        """移动到指定坐标"""
        async def _move():
            self.skills.move_to(x, y, z, speed)
        
        await self.submit_action(
            name="move_to",
            action=_move,
            priority=ActionPriority.HIGH,
            timeout=30.0
        )
    
    async def follow_player(self, player_name: str):
        """跟随玩家"""
        async def _follow():
            self.skills.follow_player(player_name)
        
        await self.submit_action(
            name="follow",
            action=_follow,
            priority=ActionPriority.HIGH,
            timeout=60.0
        )
    
    async def attack(self):
        """攻击"""
        async def _attack():
            self.skills.attack()
        
        await self.submit_action(
            name="attack",
            action=_attack,
            priority=ActionPriority.HIGH,
            timeout=10.0
        )
    
    async def mine_block(self):
        """挖掘方块"""
        async def _mine():
            self.skills.mine_block()
        
        await self.submit_action(
            name="mine",
            action=_mine,
            priority=ActionPriority.MEDIUM,
            timeout=30.0
        )
    
    async def craft_item(self, item_name: str, count: int = 1):
        """合成物品"""
        async def _craft():
            self.skills.craft_item(item_name, count)
        
        await self.submit_action(
            name="craft",
            action=_craft,
            priority=ActionPriority.MEDIUM,
            timeout=60.0
        )
    
    async def collect_blocks(self, block_type: str, count: int = 1):
        """收集方块"""
        async def _collect():
            self.skills.collect_blocks(block_type, count)
        
        await self.submit_action(
            name="collect",
            action=_collect,
            priority=ActionPriority.MEDIUM,
            timeout=120.0
        )
    
    # ── 控制状态管理 ──
    
    def set_control(self, control: str, state: bool):
        """设置控制状态"""
        if control in self.control_state:
            self.control_state[control] = state
            # 同步到 Java 端
            self.skills.set_control_state(control, state)
    
    def clear_controls(self):
        """清除所有控制状态"""
        for control in self.control_state:
            self.control_state[control] = False
        self.skills.stop_all()
    
    # ── 状态查询 ──
    
    def get_status(self) -> dict:
        """获取状态"""
        return {
            "is_running": self._is_running,
            "behavior_state": self.behavior_state,
            "current_task": self.current_task.name if self.current_task else None,
            "queue_size": self.task_queue.qsize(),
            "control_state": self.control_state.copy(),
        }
    
    def is_busy(self) -> bool:
        """是否正在执行任务"""
        return self.current_task is not None or not self.task_queue.empty()
