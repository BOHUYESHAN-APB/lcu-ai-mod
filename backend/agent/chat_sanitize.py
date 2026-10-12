"""出站聊天文本的净化（纯逻辑，零依赖）。

真机事故（2026-10-12 16:22，用户被服务器踢）：
模型与"如实说明"拼在一起的那句话里带了**换行符**（以及 Markdown 的 ``**``），
服务器判定聊天内容**非法** ⇒ 直接把玩家踢出。日志里那句话证实了这一点：
``→ send_chat {'message': '…没有执行……）\\n（不知道是哪个基地…）'}``。

聊天通道对字符**有约束**（原版客户端根本打不出换行），而这些字符在我们内部拼串时
（模型回复 + 如实说明 + 区域说明 + 命令）很容易被带进去。⇒ 约定：

* 所有出站文本在**唯一出口**过一遍 :func:`sanitize_chat`；
* **能修的修**：换行/制表符等空白 → 一个空格（保留可读性，不把两句话粘成一句）；
* **修不了的删**：C0/C1 控制字符、零宽与双向控制字符、软连字符、孤立代理项、
  以及 ``§``（染色码 —— 聊天里出现它就是"非法字符"，也可能被用来伪造格式）；
* 最后压缩连续空白、去首尾、按上限截断（原版上限 256，这里留余量到 200）。

纯函数、无副作用、可被零依赖自检直接做行为断言；:func:`find_illegal` 给诊断用
（"到底哪个字符害的"），**不参与发送路径**。
"""

from __future__ import annotations

# 原版聊天上限 256；留余量（长句 + 被服务器判成刷屏的风险）
CHAT_MAX_CHARS = 200

# 染色码：聊天里出现它就是非法字符（也是伪造格式的常见手法）
SECTION_SIGN = "\u00a7"

# 空白类：**修**成空格（保留句子边界，别把两句粘一起）
_WHITESPACE_TO_SPACE = (
    "\r\n", "\r", "\n", "\t", "\v", "\f",
    "\u0085",            # NEL
    "\u2028", "\u2029",  # LINE / PARAGRAPH SEPARATOR
)

# 零宽 / 双向控制 / 软连字符：**删**（不可见，只会被当成非法内容）
_ZERO_WIDTH = frozenset(
    "\u200b\u200c\u200d\u200e\u200f"   # ZWSP…RLM
    "\u202a\u202b\u202c\u202d\u202e"   # 双向控制
    "\u2060\u2061\u2062\u2063\u2064"   # word joiner / 不可见运算符
    "\ufeff"                            # BOM / ZWNBSP
    "\u00ad"                            # 软连字符
)


def _is_control(ch: str) -> bool:
    """C0（除被我们转成空格的空白）/ C1 / DEL，以及孤立代理项。"""
    code = ord(ch)
    if code < 0x20 or code == 0x7F:
        return True
    if 0x80 <= code <= 0x9F:
        return True
    if 0xD800 <= code <= 0xDFFF:   # 孤立代理项：非法码位，发出去就是坏数据
        return True
    return False


def find_illegal(text: str) -> list[str]:
    """诊断用：列出文本里会被净化掉的字符（去重、按出现顺序）。"""
    bad: list[str] = []
    for ch in str(text or ""):
        if ch in _WHITESPACE_TO_SPACE or ch in _ZERO_WIDTH or ch == SECTION_SIGN or _is_control(ch):
            if ch not in bad:
                bad.append(ch)
    return bad


def sanitize_chat(text: str, *, limit: int = CHAT_MAX_CHARS) -> str:
    """把一段文本净化成**能安全发进聊天**的样子（纯函数）。

    * ``None`` / 非字符串 ⇒ ``""``
    * 换行、制表、行分隔符 ⇒ 一个空格
    * 控制字符、零宽/双向控制、软连字符、孤立代理项、``§`` ⇒ 删除
    * 连续空白压成一个空格，去首尾
    * 超过 ``limit`` 截断并加省略号（不截半个代理对）
    """
    if not text:
        return ""
    src = text if isinstance(text, str) else str(text)

    # 快路径：干净文本**一次都不复制**（绝大多数回复都走这里）
    if _is_clean(src):
        return src if len(src) <= limit else _truncate(src, limit)

    out: list[str] = []
    for ch in src:
        if ch in _WHITESPACE_TO_SPACE:
            out.append(" ")
        elif ch in _ZERO_WIDTH or ch == SECTION_SIGN or _is_control(ch):
            continue
        else:
            out.append(ch)

    cleaned = " ".join("".join(out).split())   # 压空白 + 去首尾（split 不保留空段）
    if len(cleaned) > limit:
        cleaned = _truncate(cleaned, limit)
    return cleaned


def _is_clean(text: str) -> bool:
    for ch in text:
        if ch in _WHITESPACE_TO_SPACE or ch in _ZERO_WIDTH or ch == SECTION_SIGN or _is_control(ch):
            return False
    return True


def _truncate(text: str, limit: int) -> str:
    """截断到 ``limit``（含省略号），且不切在代理对中间。"""
    if limit <= 0:
        return ""
    cut = text[: max(0, limit - 1)]
    if cut and 0xD800 <= ord(cut[-1]) <= 0xDBFF:   # 高代理项后面没了低代理 ⇒ 去掉
        cut = cut[:-1]
    return cut.rstrip() + "…"
