"""
Token usage tracking per player and per session.

Tracks:
- How many messages each player sent to AI
- Estimated token cost (input + output)
- Suspicious activity flagging
- Export to CSV for hosting cost analysis
"""

import csv
import time
from dataclasses import dataclass, field


@dataclass
class PlayerTokenRecord:
    player_name: str
    player_uuid: str
    message_count: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    tasks_requested: int = 0
    tasks_completed: int = 0
    first_seen: float = 0.0
    last_seen: float = 0.0
    flagged: bool = False
    flag_reason: str = ""

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def cost_estimate(self) -> float:
        """Estimate cost at ~$0.01 per 1K tokens (configurable)."""
        return self.total_tokens * 0.00001


class TokenTracker:
    """
    Tracks token usage per player.

    The persona agent reports message lengths, and the tracker estimates
    token counts. Provides a dashboard-friendly summary.
    """

    def __init__(self, cost_per_1k: float = 0.01):
        self.cost_per_1k = cost_per_1k
        self.records: dict[str, PlayerTokenRecord] = {}
        self._suspicion_threshold_messages = 50  # messages without a task

    def get_or_create(self, player_name: str, player_uuid: str) -> PlayerTokenRecord:
        if player_uuid not in self.records:
            now = time.time()
            self.records[player_uuid] = PlayerTokenRecord(
                player_name=player_name,
                player_uuid=player_uuid,
                first_seen=now,
                last_seen=now,
            )
        return self.records[player_uuid]

    def record_message(
        self,
        player_name: str,
        player_uuid: str,
        message: str,
        estimated_input_tokens: int | None = None,
    ):
        """Record a player's message and estimate token cost."""
        rec = self.get_or_create(player_name, player_uuid)
        rec.message_count += 1
        rec.last_seen = time.time()

        # Rough estimate: 1 token ≈ 4 characters for English, ~2 for CJK
        if estimated_input_tokens is None:
            estimated_input_tokens = len(message) // 2

        rec.input_tokens += estimated_input_tokens

        # Suspicion check: many messages, few tasks
        if (
            rec.message_count > self._suspicion_threshold_messages
            and rec.tasks_requested == 0
        ):
            rec.flagged = True
            rec.flag_reason = (
                f"{rec.message_count} messages, 0 tasks — "
                f"suspicious token waste"
            )

    def record_output(self, player_uuid: str, output_text: str):
        """Record AI response tokens attributed to a player."""
        rec = self.records.get(player_uuid)
        if rec:
            rec.output_tokens += len(output_text) // 2

    def record_task(self, player_uuid: str, completed: bool = False):
        """Record that a player requested a task."""
        rec = self.records.get(player_uuid)
        if rec:
            rec.tasks_requested += 1
            if completed:
                rec.tasks_completed += 1
            # Clear suspicion flag if they're actually doing tasks
            if rec.tasks_requested > 2:
                rec.flagged = False
                rec.flag_reason = ""

    def get_summary(self) -> dict:
        """Get a summary suitable for the web dashboard."""
        players = []
        total_tokens = 0
        total_cost = 0.0

        for rec in sorted(
            self.records.values(), key=lambda r: r.total_tokens, reverse=True
        ):
            players.append({
                "name": rec.player_name,
                "uuid": rec.player_uuid,
                "messages": rec.message_count,
                "tokens": rec.total_tokens,
                "tasks": rec.tasks_requested,
                "tasks_done": rec.tasks_completed,
                "cost": round(rec.cost_estimate, 4),
                "flagged": rec.flagged,
                "flag_reason": rec.flag_reason,
            })
            total_tokens += rec.total_tokens
            total_cost += rec.cost_estimate

        return {
            "players": players,
            "total_tokens": total_tokens,
            "total_cost": round(total_cost, 4),
            "flagged_count": sum(1 for p in players if p["flagged"]),
        }

    def export_csv(self, filepath: str):
        """Export token usage to CSV for hosting cost analysis."""
        with open(filepath, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow([
                "Player", "UUID", "Messages", "Input Tokens",
                "Output Tokens", "Total Tokens", "Tasks",
                "Cost (USD)", "Flagged", "Flag Reason",
            ])
            for rec in self.records.values():
                w.writerow([
                    rec.player_name,
                    rec.player_uuid,
                    rec.message_count,
                    rec.input_tokens,
                    rec.output_tokens,
                    rec.total_tokens,
                    rec.tasks_requested,
                    round(rec.cost_estimate, 4),
                    rec.flagged,
                    rec.flag_reason,
                ])
