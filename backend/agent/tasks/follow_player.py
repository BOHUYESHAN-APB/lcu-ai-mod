"""
Follow a player, with path clearing and bridging.
Sends move_to commands periodically — Java mod handles actual A* pathfinding.
"""

import math
from protocol import WireClient
from . import Task


class FollowPlayerTask(Task):
    """Follow a target player. Recalculates path every 2 seconds."""

    def __init__(self, wire: WireClient):
        super().__init__(wire)
        self.target_name = ""
        self.target_uuid = ""
        self._target_pos = None
        self._tick_count = 0

    @property
    def name(self) -> str:
        return f"跟随 {self.target_name}" if self.target_name else "跟随玩家"

    def set_target(self, name: str, uuid: str):
        self.target_name = name
        self.target_uuid = uuid

    def can_start(self) -> bool:
        return bool(self.target_name)

    def on_start(self):
        print(f"[Task] 开始跟随 {self.target_name}")
        self._tick_count = 0

    def tick(self) -> bool:
        self._tick_count += 1
        # Recalculate path every 40 ticks (~2 seconds)
        if self._tick_count % 40 != 0:
            return False

        # Get our position and target position
        # In the full version, this would track the target player's position
        # from state updates and send move_to commands

        # For now, send a get_state to find target
        self.wire.send_command("get_state", {})
        return False

    def on_stop(self):
        print(f"[Task] 停止跟随 {self.target_name}")
