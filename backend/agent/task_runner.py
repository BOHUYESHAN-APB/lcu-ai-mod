"""
Task runner — orchestrates Python-side tasks by sending commands to the Java mod.
"""

from protocol import WireClient
from .tasks.follow_player import FollowPlayerTask
from .tasks.combat import RangedCombatTask
from .tasks.farming import SpecialFarmTask
from .tasks.resources import HoneyTask, FishTask, GrassSnowTask
from .tasks.animal_care import AnimalCareTask, ExtinguishTask


class TaskRunner:
    """Runs tasks by composing primitive commands and sending them to the mod."""

    def __init__(self, wire: WireClient):
        self.wire = wire
        self.tasks = {
            "follow_player": FollowPlayerTask(wire),
            "ranged_combat": RangedCombatTask(wire),
            "special_farm": SpecialFarmTask(wire),
            "honey": HoneyTask(wire),
            "fish": FishTask(wire),
            "grass_snow": GrassSnowTask(wire),
            "animal_care": AnimalCareTask(wire),
            "extinguish": ExtinguishTask(wire),
        }
        self.current_task = None
        self.task_queue = []

    def start_task(self, task_id: str) -> bool:
        """Start a task by ID."""
        task = self.tasks.get(task_id)
        if task and task.can_start():
            self.current_task = task
            task.start(task_id)
            print(f"[TaskRunner] 启动任务: {task.name}")
            return True
        return False

    def tick(self):
        """Called every event loop cycle. Ticks the current task."""
        if self.current_task:
            done = self.current_task.tick()
            if done:
                print(f"[TaskRunner] 任务完成: {self.current_task.name}")
                self.current_task.stop()
                self.current_task = None
                # Start next in queue
                if self.task_queue:
                    self.start_task(self.task_queue.pop(0))

    def queue_task(self, task_id: str):
        self.task_queue.append(task_id)

    def stop_all(self):
        if self.current_task:
            self.current_task.stop()
            self.current_task = None
        self.task_queue.clear()

    def get_status(self) -> dict:
        return {
            "current": self.current_task.name if self.current_task else None,
            "queue": self.task_queue.copy(),
            "available": list(self.tasks.keys()),
        }
