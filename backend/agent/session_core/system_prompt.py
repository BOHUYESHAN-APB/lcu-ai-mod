"""分段系统提示词：**命名分段** + 首次全量、之后只追加 patch。

对应 ``docs/dev/AGENT_ARCH.md`` §2 与 ``docs/dev/PROMPT_MODEL.md``：

* ``sections = name → text``；首次把全部分段记进第一条 ``system_message``；
* 之后内容变了 → **只追加一个 patch**（``name → 新文本``；``None`` 表示删掉这一段）；
* **按顺序重放 patch 即得当前状态**（不需要另设"提示词状态对象"）；
* **稳定前缀字节不变**：不变量段在前（``SECTION_ORDER``），未变的段逐字节相同。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

# ── 命名分段（对应 L0/L1；顺序 = 渲染顺序，"不变量在前"） ──────
L0_CONTRACT = "L0_CONTRACT"      # 基础契约 / 输出格式（常量，字节级跨轮不变）
TOOL_CONTRACT = "TOOL_CONTRACT"  # 工具清单的语义说明（schema 走 API tools 参数）
RULES = "RULES"                  # 规则
MECHANICS = "MECHANICS"          # 机制说明
AUTHZ = "AUTHZ"                  # 授权（借用 / 领地）
PERSONA = "PERSONA"              # 人设
SECTION_ORDER = (L0_CONTRACT, TOOL_CONTRACT, RULES, MECHANICS, AUTHZ, PERSONA)


def build_sections(**named: str | None) -> dict:
    """首次全量：把全部分段记进第一条 ``system_message``（``None`` 段跳过）。"""
    return {name: str(text) for name, text in named.items() if text is not None}


def diff_sections(previous: Mapping[str, str], new: Mapping[str, str]) -> dict:
    """算出"只追加的那一个 patch"：变更/新增 = 新文本，删除 = ``None``。

    没有任何变化 → 返回 **空 dict**（调用方据此**不追加**节点 → 前缀字节不变）。
    """
    prev = dict(previous or {})
    cur = dict(new or {})
    patch: dict = {}
    for name, text in cur.items():
        if prev.get(name) != text:
            patch[name] = text
    for name in prev:
        if name not in cur:
            patch[name] = None
    return patch


def apply_patch(sections: Mapping[str, str], patch: Mapping[str, str | None]) -> dict:
    """把一个 patch 应用到 sections 上（``None`` 删除该段）。"""
    out = dict(sections or {})
    for name, text in (patch or {}).items():
        if text is None:
            out.pop(name, None)
        else:
            out[name] = str(text)
    return out


def replay(patches: Iterable[Mapping[str, str | None]]) -> dict:
    """按顺序重放 patch → 当前 sections 状态（纯函数）。"""
    out: dict = {}
    for patch in patches:
        out = apply_patch(out, patch)
    return out


def ordered_names(sections: Mapping[str, str]) -> list[str]:
    """稳定顺序：已知段按 ``SECTION_ORDER``，其余按插入顺序追加在后。"""
    secs = dict(sections or {})
    known = [name for name in SECTION_ORDER if name in secs]
    extra = [name for name in secs if name not in SECTION_ORDER]
    return known + extra


def render(sections: Mapping[str, str], separator: str = "\n\n") -> str:
    """渲染当前 sections → 系统提示词字符串（纯函数、确定性顺序）。"""
    secs = dict(sections or {})
    parts = [str(secs[name]) for name in ordered_names(secs) if secs.get(name)]
    return separator.join(parts)


__all__ = [
    "AUTHZ",
    "L0_CONTRACT",
    "MECHANICS",
    "PERSONA",
    "RULES",
    "SECTION_ORDER",
    "TOOL_CONTRACT",
    "apply_patch",
    "build_sections",
    "diff_sections",
    "ordered_names",
    "render",
    "replay",
]
