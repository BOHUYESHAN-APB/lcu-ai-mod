"""从工具注册表生成 `docs/dev/TOOLS.md` —— 工具目录**不许手写**。

用法（仓库根目录或任意目录都行）：

    python backend/scripts/dump_tools.py

同一份注册表 → 逐字节相同的文档（分组、参数摘要、权限、阶段过滤都可复现）。
改工具只需改 `backend/agent/tool_registry.py`，再重跑本脚本。
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT / "backend") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "backend"))

from agent.tool_registry import PHASE_GROUPS, TOOL_SPECS  # noqa: E402

OUTPUT = REPO_ROOT / "docs" / "dev" / "TOOLS.md"


def _phases_for(group: str) -> list[str]:
    return [phase for phase, groups in PHASE_GROUPS.items() if group in groups]


def _permission_text(spec) -> str:
    perm = spec.permission
    bits: list[str] = []
    if perm.tier:
        bits.append(f"tier={perm.tier}")
    if perm.container:
        bits.append("容器授权")
    if perm.command:
        bits.append("指令白名单")
    if perm.borrow:
        bits.append("借用台账")
    if perm.area:
        bits.append("区域约束")
    if not bits:
        bits.append("—")
    text = "、".join(bits)
    return f"{text}（{perm.note}）" if perm.note else text


def render() -> str:
    all_groups = sorted({group for spec in TOOL_SPECS for group in spec.groups})
    lines: list[str] = []
    lines.append("# TOOLS —— 工具目录（自动生成，请勿手改）")
    lines.append("")
    lines.append("> 本文件由 `python backend/scripts/dump_tools.py` 从 "
                 "`backend/agent/tool_registry.py` 生成，是工具的唯一事实来源的**投影**。")
    lines.append("> 要改工具：改注册表，再重跑脚本；**不要手写本文件**。")
    lines.append("")
    lines.append(f"- 工具总数：**{len(TOOL_SPECS)}**")
    lines.append("- 动作组：" + ", ".join(all_groups))
    lines.append("")

    lines.append("## 阶段 → 注入的动作组（L3 `tools` 过滤）")
    lines.append("")
    lines.append("| 阶段 | 注入的组（顺序即展示顺序） |")
    lines.append("| --- | --- |")
    for phase, groups in PHASE_GROUPS.items():
        lines.append(f"| `{phase}` | {' → '.join(groups)} |")
    lines.append("")

    lines.append("## 工具（按注册表顺序）")
    lines.append("")
    lines.append("| 工具 | 参数 | 组 | 出现阶段 | 权限 | handler |")
    lines.append("| --- | --- | --- | --- | --- | --- |")
    for spec in TOOL_SPECS:
        args = spec.args or "—"
        group = "/".join(spec.groups)
        phases = "/".join(_phases_for(spec.groups[0]))
        lines.append(f"| `{spec.name}` | {args} | {group} | {phases} | "
                     f"{_permission_text(spec)} | `{spec.handler}` |")
    lines.append("")

    lines.append("## 说明")
    lines.append("")
    lines.append("- **权限门**是注册表字段：派发前由 `Planner._authorize_tool` 统一执行"
                 "（tier 能力位 + 容器访问），不再散在各 handler。")
    lines.append("- **handler** 指向 Planner 上真正下发的技能函数；未登记的 tool 一律拒绝。")
    lines.append("- 阶段过滤沿用旧 `PHASE_GROUPS` 行为（注入哪些组、组内顺序、按名去重都不变）。")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(render(), encoding="utf-8", newline="\n")
    print(f"wrote {OUTPUT} ({len(TOOL_SPECS)} tools)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
