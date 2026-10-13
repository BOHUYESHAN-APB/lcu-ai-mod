"""出站文本清洗 —— **单一出口**：任何会进聊天框的 AI 文本必须先过这里。

用户口径（原话）：

    「要避免他在主动对话过程当中输出 `/` …… 做一个**正则防御机制**，防止它输入
      输出的时候尝试输入指令。」

硬约束：`/` 开头的字符串永远不能到达服务端指令通道。原版客户端的约定是
"以 `/` 开头 = 执行指令"，所以一句以 `/` 开头（哪怕前面垫了空格）的 AI 回复
等于**以聊天为名执行指令**。这里把这种文本在**出站前**改写掉：

- 剥行首空白（含全角空格 U+3000、零宽字符 —— 它们能藏在 `/` 前面骗过朴素判断）；
- 剥所有前导 `/`（`//` 同理，Java 侧也会兜底剥一次）；
- **字符级净化**（`chat_sanitize.sanitize_chat`）：换行/制表等空白 → 空格，
  控制字符 / 零宽与双向控制 / 软连字符 / 孤立代理项 / `§` 染色码 → 删除，
  压缩连续空白并限长。真机事故（2026-10-12 16:22）：AI 回复里带了换行符，
  被服务器判为"非法聊天字符"**直接把玩家踢出**。
- **剥 markdown 标记**（真机事故 2026-10-12 21:20）：AI 把内部说明里的 `**加粗**`
  原样发上了公屏（"走过去**右键一次**"）—— 这里是**机械剥离**，不是内容审查：
  `**`/`__`/`` ` ``/`~~` 与行首的 `#`/`>`/`- `/`1. ` 标记一律去掉，正文一字不动。
- **公屏不报坐标**（同一次事故：回复里漏出了床的坐标）：公屏文本里的坐标片段
  一律替换成占位符 —— **坐标只进日志/后端**（`kind="whisper"` 这种私聊不走这条）。
- 两者都做，于是结果**绝不以 `/` 开头**、绝不含聊天通道不允许的字符；
  除上述之外，正文内容**一字不动**。

清洗一旦真的发生（原文 ≠ 清洗后），就回调所有已注册的审计 sink（谁 / 原文 /
清洗后）。sink 抛异常只 warning，绝不拖垮出站链路 —— 这句话是要发出去的。
"""

from __future__ import annotations

import logging
import re
import threading
from typing import Any, Callable

from .chat_sanitize import find_illegal, sanitize_chat

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

#: 公屏聊天（**必须不报坐标**）：坐标只进日志/后端。私聊（`whisper`）不算公屏。
PUBLIC_CHAT_KINDS = frozenset({"send_chat"})

#: 坐标占位符（替换掉公屏上的坐标片段；原文进审计，不静默丢）。
COORDS_REDACTED = "（坐标不报）"

# ── markdown 机械剥离 ─────────────────────────────────────────
# 只剥"标记"本身，正文一字不动：`**粗体**` → `粗体`，孤立的 `**` 直接去掉。
_MD_EMPHASIS_RE = re.compile(r"\*{2}|_{2}|~~")
#: 行内代码的反引号（成串的一起剥，内容保留）。
_MD_CODE_RE = re.compile(r"`+")
#: 行首标记：`# 标题` / `> 引用` / `- 列表` / `1. 列表`（**行首**才剥，正文里的不动）。
_MD_LEAD_RE = re.compile(r"^[ \t]*(?:#{1,6}[ \t]+|>[ \t]?|[-*+][ \t]+|\d+[.)][ \t]+)", re.MULTILINE)

# ── 坐标（公屏一律不报）────────────────────────────────────────
# 带括号的 `(851,63,-2191)` / `（851, 63, -2191）`（全角括号也认）。
_COORDS_PAREN_RE = re.compile(
    r"[(（][ \t]*(-?\d{1,6}(?:\.\d+)?)[ \t]*,[ \t]*(-?\d{1,6}(?:\.\d+)?)"
    r"[ \t]*,[ \t]*(-?\d{1,6}(?:\.\d+)?)[ \t]*[)）]")
# 不带括号的裸三元组 `851,63,-2191`（前后不能再粘着数字/字母/小数点，免得误伤正文）。
_COORDS_BARE_RE = re.compile(
    r"(?<![\w.])-?\d{1,6}(?:\.\d+)?[ \t]*,[ \t]*-?\d{1,6}(?:\.\d+)?"
    r"[ \t]*,[ \t]*-?\d{1,6}(?:\.\d+)?(?![\w.])")


def strip_markdown(text: str) -> str:
    """机械剥离 markdown 标记（**不是**内容审查：只去标记，正文一字不动）。"""
    text = _MD_LEAD_RE.sub("", text or "")
    text = _MD_CODE_RE.sub("", text)
    return _MD_EMPHASIS_RE.sub("", text)


def hide_coordinates(text: str) -> str:
    """把文本里的坐标片段换成占位符（**公屏**用；坐标只进日志/后端）。"""
    text = _COORDS_PAREN_RE.sub(COORDS_REDACTED, text or "")
    return _COORDS_BARE_RE.sub(COORDS_REDACTED, text)


def mentions_coordinates(text: Any) -> bool:
    """这段文本里有没有坐标（同一个判据，测试与调用方都能直接引）。"""
    cleaned = "" if text is None else str(text)
    return bool(_COORDS_PAREN_RE.search(cleaned) or _COORDS_BARE_RE.search(cleaned))

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

    四件事，按顺序：剥 markdown 标记 → 字符级净化（非法字符）→ 剥行首斜杠/空白
    → **公屏**（`kind="send_chat"`）把坐标换成占位符（坐标只进日志/后端）。

    参数里的 `kind` / `actor` / `server_key` 只用于审计（哪个出口、谁触发的），
    不影响清洗结果（`kind` 例外：只有它决定"要不要按公屏口径藏坐标"）。
    非字符串一律 `str()` 后处理（None → 空串）。

    `observer` 是**本调用方自己的**审计回调（session 通过 Skills 注入），与全局 sink
    并行触发 —— 全局 sink 适合进程级挂载，observer 适合"谁拥有这条出口谁记审计"。
    """
    original = "" if text is None else str(text)
    # ① **剥 markdown 标记**先做（趁换行还在，行首的 `#`/`- `/`1. ` 才认得出来）——
    #    真机事故（2026-10-12 21:20）：`走过去**右键一次**` 原样发上了公屏。
    stripped = strip_markdown(original)
    # ② **字符级净化**（换行/制表/控制字符/零宽/`§`）——
    #    真机事故（2026-10-12 16:22）：AI 回复里带了换行符，被服务器判为非法聊天字符**直接踢出**。
    #    放在"剥行首斜杠"**之前**，这样 `"\n/tp …"` 这种也躲不过（变成行首空白照样被剥）。
    cleaned = sanitize_chat(stripped)
    illegal = find_illegal(original)
    while cleaned and (_is_lead_blank(cleaned[0]) or cleaned[0] in _SLASH_CHARS):
        cleaned = cleaned[1:]
    # ③ **公屏不报坐标**：坐标只进日志/后端（私聊/任务详情不算公屏）。
    if str(kind or "") in PUBLIC_CHAT_KINDS:
        cleaned = hide_coordinates(cleaned)
    if cleaned != original:
        row = {
            "kind": str(kind or "outbound"),
            "actor": str(actor or ""),
            "server_key": str(server_key or ""),
            "original": original,
            "cleaned": cleaned,
            # 诊断用：被处理掉的字符（空列表 = 只剥了行首空白/斜杠）
            "illegal": illegal,
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
