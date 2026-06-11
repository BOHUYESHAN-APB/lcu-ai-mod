"""
Persona Agent — handles conversation, character, and player interaction.
Receives chat from the mod, decides how to respond, and sends commands to the execution layer.

This is the "大脑" — it talks to the LLM and makes high-level decisions.
The execution layer (Java mod) is the "手脚".

In the future, the persona agent can be replaced by an external AI system
that calls our SDK directly.
"""

from typing import Optional
from .token_tracker import TokenTracker
from llm_provider import MiMoProvider


class PersonaAgent:
    """
    The persona agent manages:
    - Character/personality (from persona.yml)
    - Conversation history
    - Player trust levels (who can give commands)
    - Token tracking per player
    - Task delegation to the execution agent
    """

    def __init__(self, tracker: TokenTracker | None = None, name: str = "AIBot"):
        self.name = name
        self.tracker = tracker or TokenTracker()
        self.conversation_history: list[dict] = []
        self.player_token_usage: dict[str, int] = {}

    def handle_chat(self, sender: str, uuid: str, message: str) -> Optional[str]:
        """
        Process an incoming chat message.
        Returns a response to send back, or None if no response needed.

        In the full implementation, this calls the LLM with context.
        """
        # Record message
        self.conversation_history.append({
            "role": "user",
            "sender": sender,
            "uuid": uuid,
            "content": message,
        })

        # TODO: Call LLM here to understand intent
        # TODO: Check player permission level
        # TODO: If task requested, queue task to execution agent

        # For now, echo back
        return f"收到 {sender} 的消息: {message}"

    def get_player_token_usage(self, player_uuid: str) -> int:
        """Get total tokens consumed by a player."""
        return self.player_token_usage.get(player_uuid, 0)

    def get_token_report(self) -> dict:
        """Get a summary of token usage per player."""
        return {
            "by_player": self.player_token_usage,
            "total": sum(self.player_token_usage.values()),
        }
