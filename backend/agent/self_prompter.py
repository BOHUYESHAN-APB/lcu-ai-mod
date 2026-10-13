"""
Self-Prompter — Continuous autonomous prompting when idle.
Replicates mindcraft's self_prompter.js architecture.

When the agent is idle (no pending tasks, no active action, no mode running)
for a configurable cooldown period, the self-prompter triggers a self-prompt
cycle: it calls the LLM with the current goal context to generate new actions.

This enables continuous autonomous behavior without requiring user chat input.
"""

import logging
import time
from typing import Optional

logger = logging.getLogger("self_prompter")

#: 空闲自提示的默认文案（`config.json → idle.prompt` 为空时用它）。
DEFAULT_IDLE_PROMPT = (
    "我现在闲着：没有人跟我说话，也没有在做的任务。可以自己找点事做 —— "
    "四处转转看看（explore / move_to）、整理背包、看看附近有什么，或者什么都不做也行。"
    "自己决定，别为了动而动。"
)


class SelfPrompter:
    """
    Self-prompting loop for autonomous behavior.

    States:
        idle: Waiting for cooldown
        prompting: Generating self-prompt
        waiting: Waiting for action completion
        stopped: Disabled

    Flow:
        idle → (cooldown expires) → prompting → (send prompt) → waiting
        → (action completes) → idle
        → (no action generated for 3 cycles) → stopped

    第二套（**默认开**，见 `idle_prompt`）：闲置自主活动。用户口径（2026-10-12 真机原话）：
    "让这个 AI 有一定的自主活动范围……它不会自主活动了。本来它在一个地方站立发呆的时候，
    可能会主动往其他地方移动。" —— 真的闲着够久就返回一条**自提示**，由调用方（session.tick）
    请模型回来一轮；动不动、动什么**由模型自己决定**（走既有 planner/工具）。它只做两件事：
    计时 + 限频，**绝不**自己下发任何动作，也不新开线程/定时器。
    """

    def __init__(self, cooldown: float = 15.0, max_idle_cycles: int = 3, *,
                 idle_config_provider=None, clock=None):
        self.cooldown = cooldown
        self.max_idle_cycles = max_idle_cycles
        self._last_action_time: float = 0.0
        self._idle_cycles = 0
        self._enabled = True
        self._goal = ""
        # 空闲自提示：配置来源（session 注入 → UI 改完即时生效）+ 可注入时钟（测试用）。
        self._idle_config_provider = idle_config_provider
        self._clock = clock or time.time
        # 最近发生的自提示时刻（只留一小时的滑动窗口，用来限频；**有界**）。
        self._idle_prompts: list[float] = []

    def set_goal(self, goal: str):
        """Set the autonomous goal."""
        self._goal = goal
        self._enabled = True
        self._idle_cycles = 0
        logger.info("[SelfPrompter] Goal set: %s", goal)

    def disable(self):
        """Disable self-prompting."""
        self._enabled = False
        logger.info("[SelfPrompter] Disabled")

    def enable(self):
        """Enable self-prompting."""
        self._enabled = True
        self._idle_cycles = 0
        logger.info("[SelfPrompter] Enabled")

    def mark_action(self):
        """Called when any action is taken (resets idle counter)."""
        self._last_action_time = self._clock()
        self._idle_cycles = 0

    def should_prompt(self, is_idle: bool, action_busy: bool) -> bool:
        """
        Check if a self-prompt should be triggered.

        Args:
            is_idle: Whether the agent is in idle state
            action_busy: Whether an action is currently executing

        Returns:
            True if a self-prompt should be generated
        """
        if not self._enabled or not self._goal:
            return False
        if action_busy:
            return False  # Don't interrupt running actions
        if not is_idle:
            self._idle_cycles = 0
            return False  # Agent is busy with non-idle work

        # Check cooldown
        elapsed = time.time() - self._last_action_time
        if elapsed < self.cooldown:
            return False

        # Check max idle cycles
        if self._idle_cycles >= self.max_idle_cycles:
            logger.info("[SelfPrompter] Max idle cycles reached, stopping")
            self._enabled = False
            return False

        return True

    def build_prompt(self) -> str:
        """
        Build the self-prompt message for the LLM.

        Returns:
            Prompt string that mimics: "You are self-prompting with the goal: '{goal}'"
        """
        self._idle_cycles += 1
        cycle_info = f" (self-prompt cycle {self._idle_cycles}/{self.max_idle_cycles})"
        return f"You are self-prompting with the goal: '{self._goal}'. What should you do next?{cycle_info}"

    def on_prompt_sent(self):
        """Called after a self-prompt is sent to the LLM."""
        self._last_action_time = self._clock()
        logger.debug("[SelfPrompter] Prompt sent (cycle %d/%d)",
                     self._idle_cycles, self.max_idle_cycles)

    # ── 空闲自主活动（第二套；默认开，见类文档）─────────────────────

    def _idle_config(self) -> dict:
        """空闲配置（provider 读不到就按默认开）。"""
        provider = self._idle_config_provider
        if provider is None:
            return {"enabled": True, "after_seconds": 30.0, "min_gap_seconds": 60.0,
                    "max_per_hour": 12, "prompt": ""}
        try:
            config = provider()
        except Exception as exc:  # noqa: BLE001 —— 配置读不到绝不拖垮心跳
            logger.debug("[SelfPrompter] 空闲配置读取失败: %s", exc)
            return {"enabled": True, "after_seconds": 30.0, "min_gap_seconds": 60.0,
                    "max_per_hour": 12, "prompt": ""}
        return config if isinstance(config, dict) else {}

    def idle_prompt(self, is_idle: bool, action_busy: bool) -> str:
        """真的闲够久 ⇒ 返回一条**自提示**；不该注入时返回空串（**一个字节都不发**）。

        判据（全部只看事实）：
        - `enabled`（`config.json → idle.enabled`，默认开）；
        - `is_idle`（没有模式在跑 / 没有在途动作 / 没有活跃任务）+ `action_busy=False`；
        - 距离上一次"有动静"（`mark_action()`，即任何指令/动作/说话）至少 `after_seconds`；
        - 限频：距上一次自提示至少 `min_gap_seconds`，且一小时内不超过 `max_per_hour` 次。

        调用方（session.tick）拿到非空文本才去请模型 —— **这里绝不下发任何动作**。
        """
        config = self._idle_config()
        if not config.get("enabled", True):
            return ""
        if not is_idle or action_busy:
            return ""
        now = self._clock()
        after = float(config.get("after_seconds") or 30.0)
        if now - float(self._last_action_time or 0.0) < after:
            return ""
        window = [stamp for stamp in self._idle_prompts if now - stamp < 3600.0]
        self._idle_prompts = window
        min_gap = float(config.get("min_gap_seconds") or 0.0)
        if window and now - window[-1] < min_gap:
            return ""
        if len(window) >= int(config.get("max_per_hour") or 12):
            return ""
        self._idle_prompts.append(now)
        self._last_action_time = now       # 注入即算"有动静"（模型在想事的这几秒别重复触发）
        return str(config.get("prompt") or "").strip() or DEFAULT_IDLE_PROMPT

    def idle_status(self) -> dict:
        """空闲自主活动的如实状态（仪表盘用；没有就说没有）。"""
        config = self._idle_config()
        now = self._clock()
        recent = [stamp for stamp in self._idle_prompts if now - stamp < 3600.0]
        return {
            "enabled": bool(config.get("enabled", True)),
            "after_seconds": config.get("after_seconds"),
            "min_gap_seconds": config.get("min_gap_seconds"),
            "max_per_hour": config.get("max_per_hour"),
            "prompts_last_hour": len(recent),
            "idle_seconds": (round(now - float(self._last_action_time), 1)
                             if self._last_action_time else None),
        }

    def get_status(self) -> dict:
        return {
            "enabled": self._enabled,
            "goal": self._goal,
            "idle_cycles": self._idle_cycles,
            "last_action_seconds_ago": round(time.time() - self._last_action_time, 1) if self._last_action_time else None,
        }
