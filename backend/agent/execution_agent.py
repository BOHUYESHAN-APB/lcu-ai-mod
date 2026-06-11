"""
Execution Agent — translates high-level tasks into low-level mod commands.
Also handles autonomous behavior (wandering, research, etc.).

Commands are sent via the wire protocol to the Java mod.
"""

from typing import Optional
from protocol import WireClient


class ExecutionAgent:
    """
    Takes task templates (like "dig_region", "brew_potions") and
    decomposes them into primitive actions (move_to, mine_block, etc.).

    Also manages:
    - Autonomous behavior loop (when no tasks)
    - Chest memory / logistics
    - Tool selection
    """

    def __init__(self, wire: WireClient):
        self.wire = wire
        self.task_queue: list[dict] = []
        self.current_task: Optional[dict] = None

    def execute_task(self, task_name: str, params: dict) -> str:
        """
        Execute a high-level task.
        Returns a request ID that can be used to track progress.
        """
        # TODO: Task decomposition
        match task_name:
            case "move_to":
                return self.wire.send_command("move_to", params)
            case "mine_block":
                return self.wire.send_command("mine_block", params)
            case "send_chat":
                return self.wire.send_command("send_chat", params)
            case "get_state":
                return self.wire.send_command("get_state", params)
            case _:
                # Try sending raw command
                return self.wire.send_command(task_name, params)

    def wait_for_idle(self):
        """Autonomous behavior when no tasks in queue."""
        # TODO: Wander, observe, research mods
        pass
