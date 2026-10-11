"""出站文本清洗 —— **单一出口**：任何会进聊天框的 AI 文本必须先过这里。

用户口径（原话）：

    「要避免他在主动对话过程当中输出 `/` …… 做一个**正则防御机制**，防止它输入
      输出的时候尝试输入指令。」

硬约束：`/` 开头的字符串永远不能到达服务端指令通道。原版客户端的约定是
"以 `/` 开头 = 执行指令"，所以一句以 `/` 开头（哪怕前面垫了空格）的 AI 回复
等于**以聊天为名执行指令**。这里把这种文本在**出站前**改写掉：

- 剥行首空白（含全角空格 U+3000、零宽字符 —— 它们能藏在 `/` 前面骗过朴素判断）；
- 剥所有前导 `/`（`//` 同理，Java 侧也会兜底剥一次）；
- 两者循环执行直到稳定 —— 于是结果**绝不以 `/` 开头**，也绝不以空白开头；
- 内部内容**一字不动**（不是过滤聊天，只是不让它变成指令）。

清洗一旦真的发生（原文 ≠ 清洗后），就回调所有已注册的审计 sink（谁 / 原文 /
清洗后）。sink 抛异常只 warning，绝不拖垮出站链路 —— 这句话是要发出去的。
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable

logger = logging.getLogger("outbound")

# 行首"看起来像空白"的字符：
#   - Unicode 空白（`str.isspace()`，U+3000 全角空格天然在内，这里再显式兜一层）；
#   - 零宽字符（零宽空格/连接符/方向符/BOM）—— 肉眼看不见，却是绕过 `startswith("/")`
#     最顺手的一招，必须一并剥掉再判断。
_BLANK_CHARS = frozenset("\u3000\u00a0\u200b\u200c\u200d\u2060\ufeff")

# 前导斜杠：半角 `/`（原版判定用）与全角 `／`（U+FF0F，中文输入法很容易打出来）。
_SLASH_CHARS = frozenset("/\uff0f")

# 清洗后必须满足的不变式（测试与调用方都可以直接引它）。
FORBIDDEN_LEADING = "/"

_lock = threading.RLock()
_sinks: list[Callable[[dict], None]] = []


def add_audit_sink(sink: Callable[[dict], None]) -> None:
    """注册一个清洗审计 sink（进程级）。重复注册同一个不叠加。"""
    if not callable(sink):
        return
    with _lock:
        if sink not in _sinks:
            _sinks.append(sink)


def clear_audit_sinks() -> None:
    """清空 sink（测试用；生产不需要）。"""
    with _lock:
        _sinks.clear()


def _is_lead_blank(char: str) -> bool:
    return char in _BLANK_CHARS or char.isspace()


def _notify(row: dict) -> None:
    """把一行审计交给所有 sink；sink 的异常只 warning（出站不能被审计拖死）。"""
    with _lock:
        sinks = list(_sinks)
    for sink in sinks:
        try:
            sink(dict(row))
        except Exception as exc:  # noqa: BLE001 —— 审计永远不能影响出站
            logger.warning("[Outbound] 审计 sink 失败: %s", exc)


def sanitize_outbound(text: Any, *, kind: str = "", actor: str = "",
                      server_key: str = "", observer: Optional[Callable[[dict], None]] = None) -> str:
    """把任意 AI 文本清洗成"绝不可能是指令"的字符串（幂等）。

    参数里的 `kind` / `actor` / `server_key` 只用于审计（哪个出口、谁触发的），
    不影响清洗结果。非字符串一律 `str()` 后处理（None → 空串）。

    `observer` 是**本调用方自己的**审计回调（session 通过 Skills 注入），与全局 sink
    并行触发 —— 全局 sink 适合进程级挂载，observer 适合"谁拥有这条出口谁记审计"。
    """
    original = "" if text is None else str(text)
    cleaned = original
    while cleaned and (_is_lead_blank(cleaned[0]) or cleaned[0] in _SLASH_CHARS):
        cleaned = cleaned[1:]
    if cleaned != original:
        row = {
            "kind": str(kind or "outbound"),
            "actor": str(actor or ""),
            "server_key": str(server_key or ""),
            "original": original,
            "cleaned": cleaned,
        }
        if callable(observer):
            try:
                observer(dict(row))
            except Exception as exc:  # noqa: BLE001 —— 审计永远不能影响出站
                logger.warning("[Outbound] 审计 observer 失败: %s", exc)
        _notify(row)
    return cleaned


def is_safe_outbound(text: Any) -> bool:
    """出站是否已经安全（不以 `/` / 空白 / 全角斜杠开头）。"""
    cleaned = "" if text is None else str(text)
    return not cleaned or not (_is_lead_blank(cleaned[0]) or cleaned[0] in _SLASH_CHARS)
