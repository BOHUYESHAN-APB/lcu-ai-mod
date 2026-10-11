"""人设存储 —— 三段自由文本 + 运行时状态块（M3-19 §3）。

为什么单独抽一个模块：人设是**可编辑的自由文本**，与决策/权限逻辑无关，
但 planner 每次组 prompt 都要取它。把"取文本 + 分场景抽取 + 运行时状态块"
集中在这里，避免 planner 里再长出一套人设拼接。

三条硬约束（照设计文档）：
1. `identity` / `reply_style` / `scene_rules` 是**三个独立块**，禁止混写；
   `state_block`（生命/饥饿/任务/在听谁说话）是**运行时**生成的，**必须与 identity 分离**，
   不能让人设被临时状态污染。
2. 缺文件 / 文件损坏一律回退内置默认值，**不抛异常**；落盘失败只 warning。
3. 默认人设中立温和，**不绑定任何具体角色 IP / 厂商**。
"""

from __future__ import annotations

import copy
import json
import logging
import random
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("persona")


DEFAULT_PERSONA_PATH = Path(__file__).parent.parent / ".local" / "persona.json"

# 场景规则里可识别的三个场景；顺序即 prompt_blocks 的抽取顺序。
SCENES = ("private", "public", "mentioned")

# set() 只认这些键，其余一律忽略（防止 UI 回传噪声写坏人设文件）。
KNOWN_KEYS = (
    "name",
    "wake_names",
    "identity",
    "reply_style",
    "multiple_reply_style",
    "multiple_probability",
    "scene_rules",
    "personality",
    "speaking_style",
)


# 场景规则的分节前缀：中英别名混用，都当同一场景处理。
# 前缀长的排前面，避免"公开"抢先吃掉"公开服"。
_SCENE_PREFIXES: dict[str, tuple[str, ...]] = {
    "private": ("私聊", "私信", "private", "dm"),
    "public": ("公开服", "公开", "公共", "public"),
    "mentioned": ("被点名", "点名", "mentioned", "mention"),
}

# 分节头后面紧跟的分隔符（中英冒号/竖线等）。
_HEADER_SEPS = ("：", ":", "｜", "|", "-", "—", " ", "\t")

_MAX_HEALTH_DEFAULT = 20.0
_MAX_HUNGER_DEFAULT = 20.0

# 任务状态的展示标签。
_TASK_KIND_LABELS = {
    "mine": "挖矿", "mining": "挖矿",
    "gather": "采集", "collect": "收集", "harvest": "收割", "farm": "耕种",
    "build": "建造", "place": "放置", "craft": "合成", "smelt": "烧炼",
    "attack": "战斗", "combat": "战斗", "hunt": "狩猎", "defend": "防守", "guard": "守卫",
    "follow": "跟随", "goto": "前往", "move": "移动", "explore": "探索", "travel": "赶路",
    "eat": "进食", "deliver": "送达", "return": "返回", "store": "存放", "idle": "待命",
}
_TASK_STATUS_LABELS = {
    "pending": "排队中", "queued": "排队中", "waiting": "等待中",
    "paused": "已暂停", "blocked": "受阻", "stuck": "卡住了",
    "failed": "失败", "error": "失败",
}
_TASK_RUNNING = {"running", "in_progress", "in-progress", "active", "doing", "executing", "working"}
# 已结束的任务不再占"当前任务"这位（下一条会顶上）。
_TASK_FINISHED = {"done", "completed", "complete", "success", "succeeded", "finished",
                  "cancelled", "canceled"}


def _default_persona() -> dict[str, Any]:
    """内置默认人设：中立、温和，**不绑定任何角色 IP / 厂商**。

    每次调用返回全新字典，避免实例之间通过默认值串改。
    """
    return {
        "name": "AI",
        "wake_names": ["AI", "小A"],
        "identity": (
            "你是一个在 Minecraft 里陪朋友一起玩的伙伴。你话不多，但一直待在旁边看着、听着、"
            "顺手搭一把；朋友找你说话时，回应要落到实处，不喊口号也不敷衍。你没有额外的身份包袱，"
            "也不装懂——不知道就问，做不到就直说。你更愿意把注意力放在当下的世界和朋友身上，"
            "而不是反复解释自己是谁。"
        ),
        "reply_style": (
            "句子短，多用口语，像正常人聊天那样，不要长篇大论。一次只说一件事，别用列表、"
            "标题或书面语。该沉默就沉默，开口就讲到点子上：不客套、不夸张、不复述对方的话。"
            "语气随和自然，不堆砌固定口头禅。"
        ),
        "multiple_reply_style": [],
        "multiple_probability": 0.0,
        "scene_rules": (
            "- 私聊：一对一的时候，可以放松一点，多讲一两句，回应更直接。\n"
            "- 公开服：人多时只说非说不可的话，简短克制，不刷屏、不主动插话。\n"
            "- 被点名：明确是在跟你说话就正面回应，别绕弯子；拿不准就先问一句确认。"
        ),
        "personality": {},
        "speaking_style": {},
    }


def _clamp_probability(value: Any, default: float = 0.0) -> float:
    """概率夹到 [0, 1]；非法值回退 default。"""
    try:
        num = float(value)
    except (TypeError, ValueError):
        return default
    if num != num:  # NaN
        return default
    return min(1.0, max(0.0, num))


def _coerce(key: str, value: Any, default: Any) -> Any:
    """把落盘/UI 回传的值归一化成该键期望的类型；认不出就用 default。"""
    if value is None:
        return default
    if key in ("identity", "reply_style", "scene_rules", "name"):
        return value if isinstance(value, str) else str(value)
    if key == "wake_names":
        if isinstance(value, (list, tuple)):
            return [str(item) for item in value if str(item).strip()]
        return default
    if key == "multiple_reply_style":
        if isinstance(value, (list, tuple)):
            return [str(item) for item in value if str(item).strip()]
        return default
    if key == "multiple_probability":
        return _clamp_probability(value, default)
    if key in ("personality", "speaking_style"):
        return dict(value) if isinstance(value, dict) else default
    return value


def _normalize(raw: dict[str, Any]) -> dict[str, Any]:
    """只保留已知键，缺失的用默认值补全（文件里多写的噪声键直接丢掉）。"""
    base = _default_persona()
    for key in KNOWN_KEYS:
        if key in raw:
            base[key] = _coerce(key, raw[key], base[key])
    return base


def _match_header(probe: str) -> Optional[tuple[str, str]]:
    """判断一行是不是场景分节头；是则返回 (scene, 该节首行内容)，否则 None。"""
    for scene in SCENES:
        for prefix in sorted(_SCENE_PREFIXES[scene], key=len, reverse=True):
            if not probe.startswith(prefix):
                continue
            rest = probe[len(prefix):]
            if rest and rest[0] not in _HEADER_SEPS:
                continue  # "私聊的时候……" 这种正文，不是分节头
            content = rest.lstrip("".join(_HEADER_SEPS)).strip()
            return scene, content
    return None


def _split_scenes(text: str) -> dict[str, str]:
    """把 scene_rules 按分节头切成 {scene: 内容}。认不出的正文行归到当前节。"""
    sections: dict[str, list[str]] = {}
    current: Optional[str] = None
    for raw_line in text.splitlines():
        body = raw_line.strip()
        if not body:
            continue
        probe = body.lstrip("-*•").strip()
        matched = _match_header(probe)
        if matched is not None:
            current, first = matched
            sections.setdefault(current, [])
            if first:
                sections[current].append(first)
        elif current is not None:
            sections[current].append(body)
    return {scene: "\n".join(lines).strip() for scene, lines in sections.items()}


def _as_number(value: Any) -> Optional[float]:
    """宽松取数：认不出（含 bool / 空 / 非数字串）返回 None。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _trim(value: float) -> str:
    """12.0 → "12"，3.5 → "3.5"。"""
    return str(int(value)) if float(value).is_integer() else f"{value:g}"


def _task_label(kind: str, status: str) -> str:
    """任务的一句话标签，例如 kind=mine + status=running → "挖矿中"。"""
    kind_label = _TASK_KIND_LABELS.get(kind, "")
    if status in _TASK_RUNNING and kind_label:
        return f"{kind_label}中"
    return _TASK_STATUS_LABELS.get(status) or kind_label or kind or "进行中"


class PersonaStore:
    """`.local/persona.json` 的读写与 prompt 组装。

    线程安全：单实例读写都在同一事件循环里；跨线程共享请由调用方加锁
    （与 ConfigStore 不同，这里没有高频并发，不做内部锁）。
    """

    def __init__(self, path: str | Path = DEFAULT_PERSONA_PATH, rng=None):
        self.path = Path(path)
        # 允许注入 rng（测试固定随机；生产用标准库 random）。
        self._rng = rng or random
        self._data: dict[str, Any] = _default_persona()
        self.reload()

    # ── 读写 ──

    def _load_from_disk(self) -> dict[str, Any]:
        """读盘；缺文件 / 损坏 / 非对象一律回退默认，**绝不抛**。"""
        try:
            if not self.path.exists():
                return _default_persona()
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                logger.warning("[Persona] %s 不是 JSON 对象，回退默认人设", self.path)
                return _default_persona()
            return _normalize(raw)
        except Exception as e:  # 文件损坏 / 权限 / 是目录……都只 warning
            logger.warning("[Persona] 读取 %s 失败，回退默认人设: %s", self.path, e)
            return _default_persona()

    def _save(self) -> None:
        """原子写盘（先写 .tmp 再 replace），失败只 warning。"""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self.path)
        except Exception as e:
            logger.warning("[Persona] 保存 %s 失败: %s", self.path, e)

    def get(self) -> dict:
        """当前人设的深拷贝（调用方随便改，不会污染 store）。"""
        return copy.deepcopy(self._data)

    def set(self, payload: dict) -> dict:
        """合并已知键并落盘；返回合并后的完整人设。未知键直接忽略。"""
        if not isinstance(payload, dict):
            return self.get()
        merged = copy.deepcopy(self._data)
        for key, value in payload.items():
            if key in KNOWN_KEYS:
                merged[key] = _coerce(key, value, merged.get(key))
        self._data = _normalize(merged)
        self._save()
        return self.get()

    def reload(self) -> dict:
        """从磁盘重读（UI 直接改了文件时用）。"""
        self._data = self._load_from_disk()
        return self.get()

    # ── 三个自由文本块 ──

    def identity(self) -> str:
        return str(self._data.get("identity") or "")

    def reply_style(self) -> str:
        """主风格；命中 multiple_probability 时换成备选之一。"""
        styles = self._data.get("multiple_reply_style") or []
        probability = _clamp_probability(self._data.get("multiple_probability"), 0.0)
        if styles and probability > 0.0 and self._rng.random() < probability:
            try:
                return str(self._rng.choice(styles))
            except Exception as e:  # 注入的 rng 不完整也不许把主循环拖垮
                logger.warning("[Persona] 备选风格抽取失败，回退主风格: %s", e)
        return str(self._data.get("reply_style") or "")

    def scene_rules(self, scene: str | None = None) -> str:
        """取某个场景那一段；认不出场景或没分节 → 返回整段原文。"""
        text = str(self._data.get("scene_rules") or "")
        if scene not in SCENES or not text.strip():
            return text
        part = _split_scenes(text).get(scene, "").strip()
        return part or text

    # ── 运行时状态块（与 identity 分离）──

    def state_block(self, state: dict) -> str:
        """由某次 state_update 生成状态块；键缺失就少一行，不报错。"""
        state = state if isinstance(state, dict) else {}
        lines: list[str] = []

        vitals: list[str] = []
        health = _as_number(state.get("health"))
        if health is not None:
            max_health = _as_number(state.get("max_health"))
            vitals.append(f"生命 {_trim(health)}/{_trim(max_health if max_health is not None else _MAX_HEALTH_DEFAULT)}")
        hunger = _as_number(state.get("hunger"))
        if hunger is not None:
            max_hunger = _as_number(state.get("max_hunger"))
            vitals.append(f"饥饿 {_trim(hunger)}/{_trim(max_hunger if max_hunger is not None else _MAX_HUNGER_DEFAULT)}")
        saturation = _as_number(state.get("saturation"))
        if saturation is not None:
            vitals.append(f"饱和度 {saturation:.1f}")
        if vitals:
            lines.append(" ｜ ".join(vitals))

        task = state.get("task")
        if isinstance(task, dict):
            status = str(task.get("status") or "").strip().lower()
            kind = str(task.get("kind") or "").strip()
            inner = " ".join(
                part for part in (
                    kind,
                    str(task.get("target") or "").strip(),
                    str(task.get("progress") or "").strip(),
                    str(task.get("detail") or "").strip(),
                ) if part
            )
            if status not in _TASK_FINISHED and (inner or status or kind):
                label = _task_label(kind, status)
                lines.append(f"当前任务：{label}（{inner}）" if inner else f"当前任务：{label}")

        who = state.get("listening_to")
        if isinstance(who, dict):
            who = who.get("name")
        if isinstance(who, str) and who.strip():
            lines.append(f"正在听你说的是：{who.strip()}")

        return "\n".join(lines)

    def prompt_blocks(self, state: dict | None = None, scene: str | None = None) -> dict:
        """组 prompt 用的四个独立块；identity 与 state_block 绝不互相污染。"""
        return {
            "identity": self.identity(),
            "reply_style": self.reply_style(),
            "scene_rules": self.scene_rules(scene),
            "state_block": self.state_block(state or {}),
        }

    # ── UI ──

    def status(self) -> dict:
        """给仪表盘看的概要：路径 + 各块长度 + 备选风格条数。"""
        styles = self._data.get("multiple_reply_style") or []
        return {
            "path": str(self.path),
            "name": str(self._data.get("name") or ""),
            "identity_chars": len(self.identity()),
            "reply_style_chars": len(self.reply_style()),
            "multiple_count": len(styles) if isinstance(styles, (list, tuple)) else 0,
            "scene_rules_chars": len(self.scene_rules()),
        }
