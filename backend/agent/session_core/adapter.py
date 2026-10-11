"""只读适配：把「投影结果」转成本项目现有 ``context`` 形状（供以后切换用）。

**不接线**：现有链路（``session.py`` / ``memory.py`` / ``llm_service.py``）纹丝未动；
这里只是提供等价映射 + 一条等价性测试，证明字段对得上。

映射口径与现有 ``Memory.build_context`` / ``LLMService`` 一致：

* ``interaction_summary``：最近的 user→assistant 对话摘要，格式
  ``"{sender}: {message}"`` / ``"AI: {response}"``（同 ``Memory._build_interaction_summary``）；
* ``recent_chat``：投影里 user 消息的 ``(sender, message)`` 列表；
* ``system_prompt``：投影里所有 system 段拼起来（含 L0 在前的稳定前缀）；
* ``persona_blocks``：若给了 ``system_sections``，把 ``PERSONA`` / ``RULES`` 折进去。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from .system_prompt import PERSONA, RULES

_MAX_SUMMARY_PAIRS = 5


def _pairs(messages: Iterable[dict]) -> list[dict]:
    """把投影里的 user/assistant 收成 ``{sender, message, response}``（同 memory 口径）。"""
    pairs: list[dict] = []
    pending: dict | None = None
    for m in messages or []:
        role = m.get("role")
        if role == "user":
            if pending is not None:
                pairs.append(pending)
            pending = {
                "sender": m.get("name") or "",
                "message": str(m.get("content") or ""),
                "response": "",
            }
        elif role == "assistant":
            text = str(m.get("content") or "")
            if pending is not None:
                pending["response"] = text
                pairs.append(pending)
                pending = None
            else:
                pairs.append({"sender": "AI", "message": "", "response": text})
    if pending is not None:
        pairs.append(pending)
    return pairs


def _interaction_summary(pairs: list[dict]) -> str:
    if not pairs:
        return "暂无对话记录"
    lines: list[str] = []
    for pair in pairs[-_MAX_SUMMARY_PAIRS:]:
        lines.append(f"{pair.get('sender', '?')}: {pair.get('message', '')}")
        if pair.get("response"):
            lines.append(f"AI: {pair['response']}")
    return "\n".join(lines)


def to_legacy_context(messages: list[dict], *,
                      system_sections: Mapping[str, str] | None = None,
                      base: dict | None = None) -> dict:
    """投影结果 → 现有 ``context`` 形状（纯读，不改动 messages）。"""
    ctx = dict(base or {})
    msgs = list(messages or [])

    ctx["system_prompt"] = "\n\n".join(
        str(m.get("content") or "") for m in msgs if m.get("role") == "system"
    )

    pairs = _pairs(msgs)
    ctx["chat_pairs"] = pairs
    ctx["interaction_summary"] = _interaction_summary(pairs)
    ctx["recent_chat"] = [
        {"sender": p["sender"], "message": p["message"]} for p in pairs if p.get("message")
    ]

    last_user = next((m for m in reversed(msgs) if m.get("role") == "user"), None)
    ctx["message"] = str(last_user.get("content") or "") if last_user else ""
    ctx["last_speaker"] = last_user.get("name") if last_user else None

    blocks = {
        "identity": "",
        "reply_style": "",
        "scene_rules": "",
        "state_block": "",
    }
    if system_sections:
        blocks["identity"] = str(system_sections.get(PERSONA) or "")
        blocks["scene_rules"] = str(system_sections.get(RULES) or "")
    ctx["persona_blocks"] = blocks

    ctx["messages"] = msgs
    return ctx


__all__ = ["to_legacy_context"]
