"""锚点（Anchor）：把「这里 / 这附近」折成一个**确定的圆心**。

用户口径（原话）：
    "用户说『来到这里』，那么 AI 就会探查用户说的话这一瞬间，用户所在的位置，然后到这个位置。
     如果后面附加『在附近干什么』，那么就基于这个点，然后再按照一定的半径去找对应的方块或者对应的东西。"

所以锚点 = **用户说那句话那一瞬间他站的位置**（快照），不是 AI 自己的位置，也不是实时跟随
（"跟着走"是另一个能力 `follow`）。解析顺序（高到低）：

  1. **模型显式给的位置**（"去 100,64,100"）—— 永远优先；
  2. **本轮锚点**（限时，默认 3 分钟）—— 「这里」「这附近」指的就是它；
  3. **AI 自己的位置** —— 兜底，但**必须在文本里说明"我按自己的位置算了"**，不许含糊；
  4. 都没有 ⇒ **没有圆心**，调用方要**如实失败**（不许拿 (0,0,0) 或世界出生点顶上）。

本模块只做这一件事：**纯函数、零依赖、不 import 其它 agent 模块**，方便自检直接做行为断言。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional, Tuple

# 锚点的保质期：用户说完这句之后 3 分钟内的"这里"还算原话指的地方；
# 超过就当过期（他早走开了），退回用 AI 自己的位置，并**说明过期了**。
MAX_AGE_SECONDS = 180.0

# 文本里给坐标的精度（格）。日志/提示不刷小数位。
_COORD_DIGITS = 1


def _coord(value: Any) -> Optional[float]:
    """把一个坐标字段读成 float；读不出来 ⇒ None（**绝不退化成 0**）。"""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _fmt(center: Tuple[float, float, float]) -> str:
    x, y, z = center
    out = []
    for value in (x, y, z):
        rounded = round(float(value), _COORD_DIGITS)
        out.append(str(int(rounded)) if float(rounded).is_integer() else f"{rounded:g}")
    return "(" + ", ".join(out) + ")"


def _age_text(seconds: float) -> str:
    if seconds < 1:
        return "刚刚"
    if seconds < 60:
        return f"{int(seconds)} 秒前"
    minutes = seconds / 60.0
    if minutes < 60:
        return f"{minutes:.1f} 分钟前"
    return f"{minutes / 60.0:.1f} 小时前"


@dataclass(frozen=True)
class Anchor:
    """用户某句话发生时的位置快照。"""

    x: float
    y: float
    z: float
    ts: float
    player: str = ""
    dimension: str = ""

    @classmethod
    def from_speaker(cls, speaker: Any, *, ts: Optional[float] = None) -> Optional["Anchor"]:
        """从"最近发话者"那条记录里读锚点；坐标读不全 ⇒ **None**（不编一个 (0,0,0)）。

        `speaker` 就是模组随聊天事件发来的那条（`{"x","y","z","name"/"player","dimension"…}`）；
        `ts` 传**我们收到这句话的时间**（单调时钟），这样没带时间戳的聊天也能算保质期。
        读不出坐标、或读不出时间 ⇒ None —— 宁可不锚，也不锚错。
        """
        if not isinstance(speaker, dict) or ts is None:
            return None
        x = _coord(speaker.get("x"))
        y = _coord(speaker.get("y"))
        z = _coord(speaker.get("z"))
        if x is None or y is None or z is None:
            return None
        name = speaker.get("player") or speaker.get("name") or ""
        dimension = speaker.get("dimension") or speaker.get("world") or ""
        return cls(x=x, y=y, z=z, ts=float(ts),
                   player=str(name or ""), dimension=str(dimension or ""))

    def age(self, now: float) -> float:
        """从说话到现在过了多少秒（时钟倒退 ⇒ 0，不返回负数）。"""
        return max(0.0, float(now) - float(self.ts))

    def is_fresh(self, now: float, *, max_age: float = MAX_AGE_SECONDS) -> bool:
        """还在保质期内？（**含边界**：正好 `max_age` 算新鲜）"""
        return self.age(now) <= float(max_age)

    def center(self) -> Tuple[float, float, float]:
        return (self.x, self.y, self.z)

    def text(self, now: Optional[float] = None) -> str:
        """给日志/提示的一行（带谁说的、多久之前）。"""
        who = f" · {self.player}" if self.player else ""
        when = f" · {_age_text(self.age(now))}" if now is not None else ""
        return f"{_fmt(self.center())}{who}{when}"


def resolve(explicit: Any = None,
            *,
            anchor: Optional[Anchor] = None,
            self_pos: Any = None,
            now: Optional[float] = None,
            max_age: float = MAX_AGE_SECONDS) -> dict:
    """决定**圆心用哪个**，并把"用了哪个"如实写进 `note`。

    返回 `{"center", "source", "note", "age"}`：
      - `center` = `(x, y, z)` 或 **None**（没有圆心 ⇒ 调用方要如实失败）；
      - `source` ∈ `"explicit" | "anchor" | "self" | "none"`；
      - `note`   = 给模型看的一句话（**必须**说明圆心来自哪、是不是过期后改用了自己）；
      - `age`    = 锚点的年龄（秒），没有锚点 ⇒ None。

    规则：显式坐标 > 新鲜锚点 > 自身位置 > 没有；**过期锚点不会静默消失** —— 会明说
    "刚才那句话的位置过期了（N 分钟前），我改用自己现在的位置"。
    """
    explicit_center = _read_point(explicit)
    if explicit_center is not None:
        return {"center": explicit_center, "source": "explicit", "age": None,
                "note": f"圆心用你给的坐标 {_fmt(explicit_center)}。"}

    if anchor is not None and now is not None and anchor.is_fresh(now, max_age=max_age):
        return {"center": anchor.center(), "source": "anchor", "age": anchor.age(now),
                "note": f"圆心用你刚才说话时的位置 {anchor.text(now)}（这就是「这里」）。"}

    self_center = _read_point(self_pos)
    if anchor is not None and now is not None and self_center is not None:
        # 有过期锚点 + 有自身位置：说明为什么换圆心，别让人以为还在用他刚才站的地方
        return {"center": self_center, "source": "self", "age": anchor.age(now),
                "note": (f"你刚才那句话的位置已经过期（{_age_text(anchor.age(now))}，"
                         f"超过 {_age_text(max_age)}），圆心改用我自己现在的位置 "
                         f"{_fmt(self_center)}。")}

    if self_center is not None:
        return {"center": self_center, "source": "self", "age": None,
                "note": f"你没说位置、也没有新鲜的锚点，圆心用我自己现在的位置 {_fmt(self_center)}。"}

    return {"center": None, "source": "none", "age": None,
            "note": "没有可用圆心：你没给坐标，也没有「你刚才说话时的位置」，我自己也不在世界里。"}


def _read_point(value: Any) -> Optional[Tuple[float, float, float]]:
    """把显式坐标 / 自身位置读成 `(x, y, z)`；读不全 ⇒ None（不编、不补 0）。"""
    if isinstance(value, dict):
        x, y, z = _coord(value.get("x")), _coord(value.get("y")), _coord(value.get("z"))
    elif isinstance(value, (list, tuple)) and len(value) >= 3:
        x, y, z = _coord(value[0]), _coord(value[1]), _coord(value[2])
    else:
        return None
    if x is None or y is None or z is None:
        return None
    return (x, y, z)
