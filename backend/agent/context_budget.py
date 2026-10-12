"""上下文预算与主动压缩的**纯计算**部分（零依赖，**不 import 任何其它 agent 模块**）。

为什么单独一个模块：估算与阈值判断必须是纯函数 —— session、日志、测试都能稳定复用，
也不会造出 import 环。

⚠️ 本模块算出来的 token 数是 **估算（estimate）**，**不是**真实用量：
真实用量只有 provider 返回的 `usage` 才算数（`agent/llm_service.py::_track_usage`）。
估算只有两个用途：

1. 在**花掉一次 API 调用之前**判断"这个 prompt 是不是该压了"；
2. 在日志里和实际 usage **并排**打印，让偏差看得见 —— 绝不把估算说成实际。

⚠️ 模型窗口同样是**配置值/假设值**：provider 不返回模型窗口，`deepseek-v4.1-flash` 的
窗口未经证实，所以 `context_window` 必须可配（见 `Budget`），日志一律按"配置"汇报。

压缩是**迟滞**的（Schmitt 触发）：过线压一次之后，占用必须先回落到
`compress_at - hysteresis` 以下（说明确实腾出了空间）才会重新武装，**不许每轮都压**。
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any

# ── 估算口径 ──────────────────────────────────────────────────
# ASCII：≈ 1 token / 4 字符（英文常态）；其它非 CJK：≈ 1 token / 2 字符（保守）；
# CJK（中日韩）：≈ 1 token / 字（一个字一个 token 的保守口径，宁多算不少算）。
_ASCII_CHARS_PER_TOKEN = 4
_OTHER_CHARS_PER_TOKEN = 2

# 连续空白折成一个空格再算（"空白压缩"）；首尾空白不计。
_WHITESPACE_RUN = re.compile(r"\s+")

# CJK 相关码点段（含假名/谚文/全角/CJK 扩展 A+兼容/表情编号段里的中日韩区）。
_CJK_RANGES: tuple[tuple[int, int], ...] = (
    (0x1100, 0x11FF),    # 谚文字母
    (0x2E80, 0x2EFF),    # CJK 部首补充
    (0x3000, 0x303F),    # CJK 标点（U+3000 是全角空格，已在空白压缩里被去掉）
    (0x3040, 0x30FF),    # 平假名 / 片假名
    (0x3100, 0x312F),    # 注音符号
    (0x3130, 0x318F),    # 谚文兼容字母
    (0x31C0, 0x31EF),    # CJK 笔画
    (0x3200, 0x32FF),    # 带圈 CJK 字母
    (0x3400, 0x4DBF),    # CJK 扩展 A
    (0x4E00, 0x9FFF),    # CJK 基本区
    (0xA960, 0xA97F),    # 谚文字母扩展 A
    (0xAC00, 0xD7AF),    # 谚文音节
    (0xF900, 0xFAFF),    # CJK 兼容表意文字
    (0xFE30, 0xFE4F),    # CJK 兼容形式
    (0xFF00, 0xFFEF),    # 全角字符
    (0x20000, 0x2FA1F),  # CJK 扩展 B..F + 兼容补充
)


def _is_cjk(code: int) -> bool:
    for low, high in _CJK_RANGES:
        if low <= code <= high:
            return True
    return False


def estimate_tokens(text: Any) -> int:
    """**估算**一段文本要多少 token（不是真实用量，见模块头）。

    口径：
      * CJK（中日韩）≈ 1 token / 字；
      * ASCII ≈ 1 token / 4 字符；
      * 其它 ≈ 1 token / 2 字符；
      * 连续空白折成一个空格（首尾空白不计）。
    单调：给同样的文本追加字符，返回值只增不减；空串/纯空白 = 0。
    """
    if text is None:
        return 0
    body = _WHITESPACE_RUN.sub(" ", str(text).strip())
    if not body:
        return 0
    cjk = ascii_count = other = 0
    for char in body:
        code = ord(char)
        if code < 128:
            ascii_count += 1
        elif _is_cjk(code):
            cjk += 1
        else:
            other += 1
    return (cjk
            + math.ceil(ascii_count / _ASCII_CHARS_PER_TOKEN)
            + math.ceil(other / _OTHER_CHARS_PER_TOKEN))


# ── 消息 / 分层文本 → (标签, 文本) ────────────────────────────
# 一条"消息"可能是：纯字符串、对话事件 dict（type/sender/message）、
# 分层上下文 dict（summary/keywords/recent）或它们的嵌套列表。
# 逐层拆开算，好处是能指出"谁最占"，而不是只给一个总数。

def _entry_label(item: dict) -> str:
    """日志用的短标签：`[chat] 张三` / `[summary]` / `[user]`。"""
    kind = str(item.get("type") or item.get("role") or "").strip()
    who = str(item.get("sender") or "").strip()
    if kind and who:
        return f"[{kind}] {who}"
    if kind:
        return f"[{kind}]"
    return who


def _pieces(item: Any) -> list[tuple[str, str]]:
    """把一个消息/记录/分层文本拆成 `(标签, 文本)` 列表（保持确定性顺序）。"""
    if item is None:
        return []
    if isinstance(item, str):
        return [("", item)]
    if isinstance(item, dict):
        # 单条对话事件：{'type','sender','message','cause',...}
        if "message" in item or "text" in item:
            body = str(item.get("message") or item.get("text") or "")
            cause = str(item.get("cause") or "")
            if cause:
                body = f"{body}（{cause}）"
            return [(_entry_label(item), body)]
        # 分层上下文（conversation 层）：summary + keywords + recent 逐层算。
        layered: list[tuple[str, str]] = []
        if item.get("summary"):
            layered.append(("summary", str(item["summary"])))
        keywords = item.get("keywords")
        if isinstance(keywords, (list, tuple, set)) and keywords:
            layered.append(("keywords", " ".join(str(word) for word in keywords)))
        for key in ("recent", "messages", "items"):
            nested = item.get(key)
            if isinstance(nested, (list, tuple)):
                for index, sub in enumerate(nested):
                    for label, text in _pieces(sub):
                        prefix = f"{key}[{index}]"
                        layered.append((f"{prefix} {label}".strip(), text))
        if layered or any(key in item for key in ("recent", "summary", "keywords")):
            # 认得出这是"对话记忆层"就只算它（空层 = 0）—— 别把 namespace 之类的元数据算进来。
            return layered
        # 兜底：其它 dict 的字符串值都算进来（标签用 key）。
        return [(str(key), str(value)) for key, value in item.items()
                if isinstance(value, str)]
    if isinstance(item, (list, tuple)):
        flat: list[tuple[str, str]] = []
        for index, sub in enumerate(item):
            for label, text in _pieces(sub):
                flat.append((f"[{index}]{label}", text))
        return flat
    return [("", str(item))]


def describe_messages(messages: Any) -> dict:
    """把一串消息折成 `{estimated, count, heaviest, items}`（**估算**，非真实用量）。

    * `estimated`：逐条估算后求和（所以 `items` 的 token 之和 == 总数，自洽）；
    * `heaviest`：最贵的那条（`label`/`tokens`/`share`/`preview`），用来在日志里指出谁最占；
    * `items`：每条（或每层）的 `{label, tokens}`。
    """
    items: list[dict] = []
    for label, text in _pieces(messages):
        tokens = estimate_tokens(text)
        items.append({"label": label, "tokens": tokens,
                      "preview": _WHITESPACE_RUN.sub(" ", str(text).strip())[:40]})
    total = sum(item["tokens"] for item in items)
    heaviest: dict | None = None
    for index, item in enumerate(items):
        if heaviest is None or item["tokens"] > heaviest["tokens"]:
            heaviest = {"index": index, "label": item["label"], "tokens": item["tokens"],
                        "preview": item["preview"],
                        "share": (item["tokens"] / total) if total else 0.0}
    return {"estimated": total, "count": len(items), "heaviest": heaviest, "items": items}


def estimate_messages(messages: Any) -> int:
    """**估算**一串消息一共多少 token（= `describe_messages(...)['estimated']`）。"""
    return int(describe_messages(messages)["estimated"])


# ── 预算 ──────────────────────────────────────────────────────

def _whole(value: Any, default: int, minimum: int = 0) -> int:
    try:
        return max(minimum, int(float(value)))
    except (TypeError, ValueError):
        return int(default)


def _ratio(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _truthy(value: Any, default: bool) -> bool:
    """宽松布尔归一化（配置可能来自 UI 的字符串）；认不出就用 default。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"true", "1", "yes", "on", "是"}:
            return True
        if text in {"false", "0", "no", "off", "否"}:
            return False
    return default


@dataclass
class Budget:
    """一个模型的上下文预算。

    字段（也是 `config.json` → `context_budget` 的键）：

    * `context_window`：模型窗口。**这是配置/假设值，不是该模型被证实的事实** ——
      默认 128000 只是"先按这个跑"的假设，必须能按模型改。
    * `reserve_output`：给输出留的 token（`available()` 会扣掉它）。
    * `compress_at`：触发线（占用 = 估算 / 可用输入预算 的比例，默认 0.70）。
    * `hysteresis`：迟滞带宽（默认 0.10）—— 压过之后要掉到
      `compress_at - hysteresis` 以下才重新武装。
    * `keep_recent`：压缩时**至少**保留的最近轮数（默认 6）。
    * `enabled`：关掉 = 回到"只有窗口裁切才被动摘要"的老行为。
    """

    context_window: int = 128_000
    reserve_output: int = 16_384
    compress_at: float = 0.70
    keep_recent: int = 6
    hysteresis: float = 0.10
    enabled: bool = True

    def available(self) -> int:
        """可用**输入**预算 = 窗口 - 输出预留（不小于 0）。"""
        return max(0, int(self.context_window) - int(self.reserve_output))

    def high_line(self) -> int:
        """触发线（token）：占用达到它就考虑压缩。"""
        return math.ceil(self.available() * self.compress_at)

    def low_line(self) -> int:
        """迟滞下限（token）：压过之后占用要掉到这里以下才重新武装。"""
        return math.floor(self.available() * max(0.0, self.compress_at - self.hysteresis))

    def should_compress(self, estimated: int, *, already_compressed_at=None) -> bool:
        """是否该压缩（带迟滞，**不许每轮都压**）。

        `already_compressed_at`：上一次压缩发生时的占用比例（0..1）。
        非空且 ≥ `compress_at` 表示"刚压过、还没被证明腾出了空间" → 一律 False；
        调用方看到占用掉到 `compress_at - hysteresis` 以下时把它清成 None 重新武装
        （`plan()` 会把这步算好并放进返回值的 `next_marker`）。
        """
        if not self.enabled:
            return False
        if self.available() <= 0:
            return False
        marker = _marker_ratio(already_compressed_at)
        if marker is not None and marker >= self.compress_at:
            return False
        return int(estimated) >= self.high_line()

    @classmethod
    def from_config(cls, config: Any) -> "Budget":
        """从配置 dict（或已有的 `Budget`）构造，并把越界值夹回安全范围。"""
        if isinstance(config, cls):
            return config
        source = config if isinstance(config, dict) else {}
        default = cls()
        window = _whole(source.get("context_window", default.context_window),
                        default.context_window, minimum=1)
        reserve = _whole(source.get("reserve_output", default.reserve_output),
                         default.reserve_output, minimum=0)
        # 预留不可能比窗口还大；真配错了就夹住，`available()` 会变 0 并在日志里说明。
        reserve = min(reserve, window)
        compress_at = min(max(_ratio(source.get("compress_at", default.compress_at),
                                    default.compress_at), 0.05), 1.0)
        hysteresis = _ratio(source.get("hysteresis", default.hysteresis), default.hysteresis)
        hysteresis = min(max(hysteresis, 0.0), compress_at)
        return cls(
            context_window=window,
            reserve_output=reserve,
            compress_at=compress_at,
            keep_recent=_whole(source.get("keep_recent", default.keep_recent),
                               default.keep_recent, minimum=0),
            hysteresis=hysteresis,
            enabled=_truthy(source.get("enabled", default.enabled), default.enabled),
        )

    def as_config(self) -> dict:
        """回写成配置 dict（默认值只有这一份来源，config_store 从这里取）。"""
        return {
            "enabled": self.enabled,
            "context_window": self.context_window,
            "reserve_output": self.reserve_output,
            "compress_at": self.compress_at,
            "hysteresis": self.hysteresis,
            "keep_recent": self.keep_recent,
        }


def _marker_ratio(value: Any) -> float | None:
    """迟滞标记归一化：0/负/非数一律当"没有标记"。"""
    if value is None:
        return None
    try:
        ratio = float(value)
    except (TypeError, ValueError):
        return None
    return ratio if ratio > 0.0 else None


def plan(estimated: int, budget: Any, *, already_compressed_at=None) -> dict:
    """预算决策 + **可直接进日志的理由**。

    返回 `{compress, reason, estimated, window, threshold, available, ratio,
    high_line, low_line, hysteresis, keep_recent, already_compressed_at,
    next_marker, enabled}`。

    `next_marker` 是**调用方下一轮要传回来的迟滞标记**：压过就记成这次的占用比例；
    占用已掉到迟滞下限以下就清成 None（重新武装）；否则维持原标记。
    """
    rules = budget if isinstance(budget, Budget) else Budget.from_config(budget)
    estimated = max(0, int(estimated or 0))
    available = rules.available()
    ratio = (estimated / available) if available > 0 else 0.0
    marker = _marker_ratio(already_compressed_at)
    if marker is not None and marker < rules.compress_at:
        # 低于触发线的标记不可能来自一次真的过线压缩 → 当没有，别拿它挡住该压的轮次。
        marker = None
    next_marker = marker
    if marker is not None and estimated <= rules.low_line():
        next_marker = None  # 占用已回落 → 重新武装
    compress = rules.should_compress(estimated, already_compressed_at=next_marker)
    # `next_marker_idle` = "这一轮没有真的压成"时的下一个标记（已含重新武装）。
    # 调用方只有在**确实挪走了条目**时才用 `next_marker`（= 记下本次占用比例）。
    next_marker_idle = next_marker
    if compress:
        next_marker = ratio

    base = (f"估算 {estimated:,} / 窗口 {rules.context_window:,}"
            f"（可用输入 {available:,}，占用 {ratio:.3f}）")
    if not rules.enabled:
        reason = f"{base}：上下文预算已关闭（context_budget.enabled=false），不压"
    elif available <= 0:
        reason = (f"{base}：可用输入预算为 0（输出预留 {rules.reserve_output:,} ≥ 窗口 "
                  f"{rules.context_window:,}），配置自相矛盾 → 不压")
    elif compress:
        reason = (f"{base} ≥ 触发线 {rules.compress_at:.2f} → 压缩"
                  f"（保留最近 {rules.keep_recent} 轮）")
    elif next_marker is not None and estimated >= rules.high_line():
        reason = (f"{base} ≥ 触发线 {rules.compress_at:.2f}，但上次已在 {next_marker:.3f} 压过、"
                  f"尚未回落到迟滞下限 {max(0.0, rules.compress_at - rules.hysteresis):.2f} 以下"
                  f" → 暂不压（迟滞：不许每轮都压）")
    else:
        reason = f"{base} < 触发线 {rules.compress_at:.2f}，不压"
    return {
        "compress": compress,
        "reason": reason,
        "estimated": estimated,
        "window": rules.context_window,
        "threshold": rules.compress_at,
        "available": available,
        "ratio": ratio,
        "high_line": rules.high_line(),
        "low_line": rules.low_line(),
        "hysteresis": rules.hysteresis,
        "keep_recent": rules.keep_recent,
        "already_compressed_at": marker,
        "next_marker": next_marker,
        "next_marker_idle": next_marker_idle,
        "enabled": rules.enabled,
    }


# ── 日志文案（估算 vs 实际，两个来源分得清清楚楚）────────────────

def format_compression_event(*, before: int, after: int, budget: Any, removed: int,
                             kept: int, summarized: bool = True,
                             heaviest: dict | None = None,
                             actual_prompt: int | None = None,
                             agent: str = "planner") -> str:
    """压缩事件文案：`[Context] 压缩：8 轮 → 1 段摘要（估算 12,480 → 3,100 token…）`。

    同时给出"估算"和"实际"的位置，且标清来源：估算是本模块算的，
    实际是 provider 返回的 `usage`（拿不到就**如实说未知**，绝不拿估算冒充）。
    """
    rules = budget if isinstance(budget, Budget) else Budget.from_config(budget)
    text = (f"[Context] 压缩：{int(removed)} 轮 → 1 段摘要"
            f"（估算 {int(before):,} → {int(after):,} token（对话层口径的估算，非真实用量）；"
            f"模型窗口按配置 {rules.context_window:,}（未经该模型证实）；"
            f"触发线 {rules.compress_at:.2f}；保留最近 {int(kept)} 轮；模型角色 {agent}")
    if heaviest:
        text += (f"；最占的一条 {heaviest.get('label') or '?'} "
                 f"{int(heaviest.get('tokens') or 0):,} token")
    text += (f"；实际用量：未知（该模型未返回 usage）"
             if actual_prompt is None else
             f"；实际用量（最近一次 API 返回的输入 token）：{int(actual_prompt):,}")
    if not summarized:
        text += ("；⚠️ 摘要未生成（LLM 不可用/失败）：这些旧条目已移出窗口、留在 pending 等下次，"
                 "本轮 prompt 少一段历史 —— 如实降级，不是静默丢弃")
    return text + "）"


def format_estimate_vs_actual(estimated: int, actual_prompt: int | None, *,
                              requests: int = 0, agent: str = "planner") -> str:
    """把"估算 vs 实际"对照行打出来（同一轮里两个数直接并排）。"""
    if actual_prompt is None:
        if int(requests) <= 0:
            return (f"[Context] 估算 vs 实际（{agent}）：估算 prompt {int(estimated):,} token；"
                    f"实际未知（本轮没有发生 API 调用）")
        return (f"[Context] 估算 vs 实际（{agent}）：估算 prompt {int(estimated):,} token；"
                f"实际未知（本轮 {int(requests)} 次 API 调用都没有返回 usage）")
    estimated = int(estimated)
    actual = int(actual_prompt)
    drift = ((actual - estimated) / actual * 100.0) if actual else 0.0
    return (f"[Context] 估算 vs 实际（{agent}）：估算 {estimated:,} token / "
            f"实际 {actual:,} token（API 返回的输入用量，偏差 {drift:+.0f}%）")
