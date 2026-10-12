"""
Memory System — 长期记忆 + 上下文管理。
参考 MaiBot 的 A_memorix 子系统。

功能：
1. 短期记忆：当前对话上下文
2. 长期记忆：重要事件、玩家互动、位置记忆
3. 人物画像：记住玩家的说话风格、偏好
4. 上下文注入：在 LLM 调用前注入相关记忆
"""

import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("memory")


# 死亡点：模组只报"死在哪、什么时候"，后端只**记**（跨重启），最多留最近几次。
# 绝不自动往回走 —— 回不回去由模型调 `go_to_death_point` 决定（用户口径）。
DEATH_POINT_LIMIT = 3

# 内存上界（保留功能、只封顶）：短期交互、事件台账、玩家画像词表、POI 记忆。
# 这些是"长期跑的后端"里唯一会随运行时间/世界规模线性长的东西；封顶后内存不再增长，
# 超出的历史仍在各自的落盘文件里（memory json / poi json），只是不再常驻。
MAX_RECENT_MESSAGES = 50
MAX_EVENTS = 500
# 玩家画像里的常用词：只用于画像统计，留最常见的 N 个（否则一个话痨能把词表撑到几千条）。
PROFILE_WORD_LIMIT = 200
# POI 记忆：一个世界里唯一的箱子/工作站/记录点条目数上限（按 last_seen 淘汰最旧的）。
# 500 条 ≈ 190KB；4096 条 ≈ 1.5MB —— 足够覆盖正常存档，又不随"玩多久"无限长。
MAX_POI_ENTRIES = 4096


def _number(value: Any) -> Optional[float]:
    """数值字段归一化；bool / 字符串 / 缺值一律 None（**不猜**，也不硬转）。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def normalize_death_point(row: Any, dimension: str = "") -> Optional[dict]:
    """模组上报的一条死亡点 → 归一化的事实条目；坐标缺/非法就 None（绝不编坐标）。

    只留模组报过的四个事实：`x` / `y` / `z` / `dimension`，外加 `at`（死亡那一刻的
    墙上时间**毫秒**，`System.currentTimeMillis()`）。`at` 报缺就不放这个键 —— 宁可
    不知道"多久以前"，也不填一个假时间。
    """
    if not isinstance(row, dict):
        return None
    x, y, z = _number(row.get("x")), _number(row.get("y")), _number(row.get("z"))
    if x is None or y is None or z is None:
        return None
    entry = {"x": x, "y": y, "z": z,
             "dimension": str(row.get("dimension") or dimension or "")}
    at = _number(row.get("at"))
    if at is not None and at > 0:
        entry["at"] = at
    return entry


def death_point_key(row: dict) -> tuple:
    """同一次死亡的判据：**死亡时刻**优先（毫秒足够唯一），没有时刻才退回坐标+维度。"""
    at = _number(row.get("at"))
    if at is not None:
        return ("at", at)
    return ("xyz", str(row.get("dimension") or ""),
            round(float(row["x"]), 2), round(float(row["y"]), 2), round(float(row["z"]), 2))


def _tag_list(value: Any) -> list[str]:
    """标签数组归一化：去空白、小写、保序去重；不是数组或全空 → `[]`。"""
    if not isinstance(value, (list, tuple, set)):
        return []
    rows: list[str] = []
    for item in value:
        tag = str(item or "").strip().lower()
        if tag and tag not in rows:
            rows.append(tag)
    return rows


class Memory:
    """
    分层记忆系统。
    
    层级：
    1. 短期记忆（对话上下文）— 最近 N 条消息
    2. 长期记忆（事件记忆）— 重要事件持久化
    3. 人物画像（玩家档案）— 玩家的说话风格、偏好
    4. 位置记忆（命名位置）— 记住重要坐标
    """
    
    def __init__(self, path: str = "data/memory.json"):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        
        # 短期记忆：最近对话
        self.recent_messages: list[dict] = []
        self.max_recent = MAX_RECENT_MESSAGES  # 保留最近 50 条消息
        
        # 长期记忆：重要事件
        self.events: list[dict] = []
        self.max_events = MAX_EVENTS
        
        # 人物画像：玩家档案
        self.player_profiles: dict[str, dict] = {}
        
        # 位置记忆：命名位置
        self.locations: dict[str, dict] = {}
        
        # 统计
        self.interaction_count = 0
        self.total_actions = 0
        
        # 加载持久化数据
        self._load()
    
    # ── 短期记忆 ──
    
    def add_interaction(self, sender: str, message: str, response: str = "",
                        action: str = "", success: Optional[bool] = None):
        """记录一次交互。"""
        entry = {
            "time": time.time(),
            "sender": sender,
            "message": message,
            "response": response,
            "action": action,
            "success": success,
        }
        self.recent_messages.append(entry)
        if len(self.recent_messages) > self.max_recent:
            self.recent_messages.pop(0)
        
        self.interaction_count += 1
        
        # 更新玩家画像
        if sender and sender != "system":
            self._update_player_profile(sender, message)
    
    def get_recent_context(self, n: int = 10) -> list[dict]:
        """获取最近 N 条消息。"""
        return self.recent_messages[-n:]
    
    # ── 长期记忆 ──
    
    def record_event(self, event_type: str, description: str, 
                     location: Optional[dict] = None, players: Optional[list] = None):
        """记录一个重要事件。"""
        event = {
            "time": time.time(),
            "type": event_type,
            "description": description,
            "location": location,
            "players": players or [],
        }
        self.events.append(event)
        if len(self.events) > self.max_events:
            del self.events[:len(self.events) - self.max_events]
        
        logger.info("[Memory] 记录事件: %s - %s", event_type, description[:50])
    
    def record_action(self, action: str, success: bool):
        """记录一个动作。"""
        self.total_actions += 1
        self.events.append({
            "time": time.time(),
            "type": "action",
            "description": f"{action} ({'成功' if success else '失败'})",
        })
        # 和 `record_event` 同一套上界：动作记录此前**没**封顶，长期跑会一直长
        # （mode 切换、指令回执都往这里写），而 `_build_action_insights` 每轮还要整表扫一遍。
        if len(self.events) > self.max_events:
            del self.events[:len(self.events) - self.max_events]
    
    # ── 人物画像 ──
    
    def _update_player_profile(self, sender: str, message: str):
        """更新玩家画像。"""
        if sender not in self.player_profiles:
            self.player_profiles[sender] = {
                "first_seen": time.time(),
                "message_count": 0,
                "avg_message_length": 0,
                "common_words": {},
                "last_active": time.time(),
            }
        
        profile = self.player_profiles[sender]
        profile["message_count"] += 1
        profile["last_active"] = time.time()
        
        # 更新平均消息长度
        msg_len = len(message)
        profile["avg_message_length"] = (
            (profile["avg_message_length"] * (profile["message_count"] - 1) + msg_len) 
            / profile["message_count"]
        )
        
        # 更新常用词（简单实现）：留最常见的 `PROFILE_WORD_LIMIT` 个 —— 词表不封顶的话，
        # 一个话痨玩家的画像能长到几千条（没人读它，纯占内存）。
        words = message.lower().split()
        for word in words:
            if len(word) > 1:
                profile["common_words"][word] = profile["common_words"].get(word, 0) + 1
        if len(profile["common_words"]) > PROFILE_WORD_LIMIT:
            top = sorted(profile["common_words"].items(), key=lambda kv: kv[1], reverse=True)
            profile["common_words"] = dict(top[:PROFILE_WORD_LIMIT])
    
    def get_player_profile(self, sender: str) -> Optional[dict]:
        """获取玩家画像。"""
        return self.player_profiles.get(sender)
    
    # ── 位置记忆 ──
    
    def save_location(self, name: str, x: float, y: float, z: float, 
                      dimension: str = "overworld", description: str = ""):
        """保存一个命名位置。"""
        self.locations[name] = {
            "x": x, "y": y, "z": z,
            "dimension": dimension,
            "description": description,
            "saved_at": time.time(),
        }
        logger.info("[Memory] 保存位置: %s (%.0f, %.0f, %.0f)", name, x, y, z)
    
    def get_location(self, name: str) -> Optional[dict]:
        """获取命名位置。"""
        return self.locations.get(name)
    
    # ── 上下文构建 ──
    
    def build_context(self) -> dict:
        """构建 LLM 上下文。"""
        context = {
            "interaction_summary": self._build_interaction_summary(),
            "recent_events": self._build_recent_events(),
            "player_profiles": self._build_player_profiles_summary(),
            "locations": self._build_locations_summary(),
            "action_insights": self._build_action_insights(),
        }
        return context
    
    def _build_interaction_summary(self) -> str:
        """构建最近交互摘要。"""
        if not self.recent_messages:
            return "暂无对话记录"
        
        lines = []
        for msg in self.recent_messages[-5:]:
            sender = msg.get("sender", "?")
            message = msg.get("message", "")
            response = msg.get("response", "")
            if response:
                lines.append(f"{sender}: {message}")
                lines.append(f"AI: {response}")
            else:
                lines.append(f"{sender}: {message}")
        return "\n".join(lines)
    
    def _build_recent_events(self) -> str:
        """构建最近事件摘要。"""
        if not self.events:
            return "暂无重要事件"
        
        lines = []
        for event in self.events[-5:]:
            event_type = event.get("type", "?")
            desc = event.get("description", "")
            lines.append(f"[{event_type}] {desc}")
        return "\n".join(lines)
    
    def _build_player_profiles_summary(self) -> str:
        """构建玩家画像摘要。"""
        if not self.player_profiles:
            return "暂无玩家画像"
        
        lines = []
        for name, profile in self.player_profiles.items():
            msg_count = profile.get("message_count", 0)
            avg_len = profile.get("avg_message_length", 0)
            lines.append(f"{name}: 发送了 {msg_count} 条消息，平均长度 {avg_len:.0f} 字")
        return "\n".join(lines)
    
    def _build_locations_summary(self) -> str:
        """构建位置记忆摘要。"""
        if not self.locations:
            return "暂无保存的位置"
        
        lines = []
        for name, loc in self.locations.items():
            x, y, z = loc.get("x", 0), loc.get("y", 0), loc.get("z", 0)
            desc = loc.get("description", "")
            lines.append(f"{name}: ({x:.0f}, {y:.0f}, {z:.0f}) {desc}")
        return "\n".join(lines)
    
    def _build_action_insights(self) -> str:
        """构建动作洞察。"""
        if self.total_actions == 0:
            return "暂无动作记录"
        
        success_count = sum(1 for e in self.events 
                           if e.get("type") == "action" and "成功" in e.get("description", ""))
        success_rate = success_count / max(1, self.total_actions) * 100
        return f"共执行 {self.total_actions} 个动作，成功率 {success_rate:.0f}%"
    
    # ── 持久化 ──
    
    def _load(self):
        """加载持久化数据。"""
        if not self.path.exists():
            return
        
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            # 落盘文件可能很大（历史版本没有上界）：载入时**就地裁到上界**，
            # 否则一次重启就把整份旧账重新常驻内存（并让每轮的动作统计更慢）。
            recent = data.get("recent_messages", [])
            self.recent_messages = list(recent)[-self.max_recent:] if isinstance(recent, list) else []
            events = data.get("events", [])
            self.events = list(events)[-self.max_events:] if isinstance(events, list) else []
            self.player_profiles = data.get("player_profiles", {})
            # 老文件里的词表同样可能没封顶：载入时就地裁到上界。
            if isinstance(self.player_profiles, dict):
                for profile in self.player_profiles.values():
                    if isinstance(profile, dict) and isinstance(profile.get("common_words"), dict):
                        words_map = profile["common_words"]
                        if len(words_map) > PROFILE_WORD_LIMIT:
                            top = sorted(words_map.items(), key=lambda kv: kv[1], reverse=True)
                            profile["common_words"] = dict(top[:PROFILE_WORD_LIMIT])
            self.locations = data.get("locations", {})
            self.interaction_count = data.get("interaction_count", 0)
            self.total_actions = data.get("total_actions", 0)
            logger.info("[Memory] 加载了 %d 条消息、%d 个事件、%d 个玩家画像",
                       len(self.recent_messages), len(self.events), len(self.player_profiles))
        except Exception as e:
            logger.warning("[Memory] 加载失败: %s", e)
    
    def save(self):
        """保存持久化数据。"""
        data = {
            "recent_messages": self.recent_messages,
            "events": self.events,
            "player_profiles": self.player_profiles,
            "locations": self.locations,
            "interaction_count": self.interaction_count,
            "total_actions": self.total_actions,
        }
        self.path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        logger.debug("[Memory] 保存了 %d 条消息、%d 个事件", 
                    len(self.recent_messages), len(self.events))


class WorldPoiMemory:
    """按「服务器 + 世界 + 维度」命名空间持久化的 POI 记忆（箱子 / 工作站）。

    Java 的 PoiMemory 只活在内存里，离开视野 5 分钟后过期、重启即丢；这一层把
    观测到的 POI 落盘，并按服务器/存档/**维度**分开存，避免跨服跨档跨维度串数据。

    维度是必需的一层（缺的层用固定占位，绝不省略）：同一服务器里主世界与下界的
    xyz 完全不是一回事，少了这一段两个世界就会互相污染。
    """

    def __init__(self, root: str = "data/poi"):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._namespace = ""
        self._legacy_namespace = ""
        self._entries: dict[str, dict] = {}
        # 死亡点（最新在前，最多 `DEATH_POINT_LIMIT` 条）：与 POI 同一份分区文件。
        self._deaths: list[dict] = []
        self._dirty = False

    # ── 命名空间 ──

    @property
    def namespace(self) -> str:
        return self._namespace

    @property
    def path(self) -> Path:
        return self.root / f"{self._namespace or 'unscoped'}.json"

    def use_namespace(self, server: str, world: str, dimension: str = "") -> str:
        """切换到指定「服务器 / 存档 / 维度」；与当前不同则先落盘旧的，再载入新的。"""
        key = "__".join((
            self._slug(server, "unknown-server"),
            self._slug(world, "unknown-world"),
            self._slug(dimension, "unknown-dimension"),
        ))
        if key == self._namespace:
            return key
        if self._namespace:
            self.save()
        self._namespace = key
        # 旧版只有「服务器 + 存档」两段：历史文件还能读回来（下次落盘写成三段式）。
        self._legacy_namespace = f"{self._slug(server, 'unknown-server')}__{self._slug(world, 'unknown-world')}"
        self._entries = {}
        self._deaths = []
        self._dirty = False
        self._load()
        return key

    @staticmethod
    def _slug(text: str, fallback: str) -> str:
        cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", str(text or "").strip()).strip("_")
        return cleaned[:60] or fallback

    # ── 记录 / 查询 ──

    def observe(self, category: str, poi: dict, dimension: str = "") -> bool:
        """吸收一个观测到的 POI；位置、方块或内容有变化时返回 True。"""
        x, y, z = poi.get("x"), poi.get("y"), poi.get("z")
        if x is None or y is None or z is None:
            return False

        entry = {
            "category": category,
            "block_id": poi.get("block_id", "") or poi.get("id", ""),
            "name": poi.get("name", ""),
            "x": x, "y": y, "z": z,
            "dimension": dimension,
            "last_seen": time.time(),
        }
        # 带标签的记忆点（模组主动记的地标）：标签原样留着（`home`/`bed`…），
        # 它是"这个点到底是什么"的真相来源（`category` 只是主标签）。
        tags = _tag_list(poi.get("tags"))
        if tags:
            entry["tags"] = tags
        contents = poi.get("contents")
        if contents:
            entry["contents"] = [
                {"item_id": c.get("item_id", ""), "count": c.get("count", 1)}
                for c in contents if isinstance(c, dict)
            ]

        key = self._key(dimension, x, y, z)
        existing = self._entries.get(key)
        if existing is not None and self._same(existing, entry):
            existing["last_seen"] = entry["last_seen"]
            return False
        self._entries[key] = entry
        self._dirty = True
        # 上界（保留功能、只封顶）：一个世界里观测到的 POI 已经很多时，
        # 淘汰 last_seen 最旧的那条 —— 新建/最近见过的留下，久未再见的让位。
        if len(self._entries) > MAX_POI_ENTRIES:
            oldest = min(self._entries.items(), key=lambda kv: kv[1].get("last_seen", 0.0))
            self._entries.pop(oldest[0], None)
        return True

    def observe_state(self, state: dict) -> int:
        """从一次 state_update 里吸收所有 POI，返回新增/更新的数量。"""
        dimension = (state.get("player") or {}).get("dimension", "")
        changed = 0
        for category, field in (("storage", "nearby_storage"),
                                ("workstation", "nearby_workstations")):
            for poi in state.get(field) or []:
                if isinstance(poi, dict) and self.observe(category, poi, dimension):
                    changed += 1
        # Java 侧点名的持久记录点（remember_poi 的产物）：按 poi 自带 category
        # 归类，缺省 waypoint；坐标与 id 已由 Java 保证。
        for poi in state.get("remembered_pois") or []:
            if not isinstance(poi, dict):
                continue
            category = str(poi.get("category") or "").strip() or "waypoint"
            if self.observe(category, poi, dimension):
                changed += 1
        return changed

    def observe_deaths(self, rows: Any, dimension: str = "") -> int:
        """吸收模组上报的死亡点（契约：**最新在前**，最多 3 条），返回新记下的条数。

        用户口径：模组只报**事实**（死在哪、什么时候），后端只**记** —— 这里不做任何
        "自动回去"（没有任何定时器/后台动作，回去由模型调 `go_to_death_point`）。
        同一**次**死亡只留一条（判据见 `death_point_key`），重复/更旧的排在后面，
        整体按死亡时刻倒序、只留最近 `DEATH_POINT_LIMIT` 条（不无限长）。
        """
        if not isinstance(rows, (list, tuple)):
            return 0
        fresh = [row for row in (normalize_death_point(item, dimension) for item in rows)
                 if row is not None]
        if not fresh:
            return 0
        merged: list[dict] = []
        seen: set = set()
        # 模组这一轮报的在前（它是真相），旧记忆里没重复的接在后面 —— 顺序即"谁更新"。
        for row in fresh + [dict(entry) for entry in self._deaths]:
            key = death_point_key(row)
            if key in seen:
                continue
            seen.add(key)
            merged.append(row)
        merged.sort(key=lambda row: _number(row.get("at")) or float("-inf"), reverse=True)
        merged = merged[:DEATH_POINT_LIMIT]
        known = {death_point_key(entry) for entry in self._deaths}
        added = sum(1 for row in merged if death_point_key(row) not in known)
        if merged != self._deaths:
            self._deaths = merged
            self._dirty = True
        return added

    def death_points(self) -> list[dict]:
        """记着的死亡点（最新在前，最多 `DEATH_POINT_LIMIT` 条）——返回**副本**。"""
        return [dict(row) for row in self._deaths]

    def find(self, category: Optional[str] = None) -> list[dict]:
        entries = [e for e in self._entries.values()
                   if category is None or e.get("category") == category]
        return sorted(entries, key=lambda e: e.get("last_seen", 0.0), reverse=True)

    _CATEGORY_LABELS = {
        "storage": "箱子",
        "workstation": "工作站",
        "waypoint": "记录点",
        "bed": "床",
    }

    def describe(self, category: str = "storage", limit: int = 12) -> str:
        """给 planner 用的已知 POI 摘要（跨局记忆，可能不在当前视野内）。"""
        entries = self.find(category)[:limit]
        label = self._CATEGORY_LABELS.get(category, category)
        if not entries:
            return f"暂无已知{label}"
        lines = []
        for e in entries:
            name = str(e.get("name") or "").strip()
            coords = f"({e.get('x')},{e.get('y')},{e.get('z')})"
            if category in {"waypoint", "bed"}:
                # 记录点/床：坐标 + 名字（记忆里最需要的两件事），没有"内容"概念。
                tag = e.get("block_id") or label
                lines.append(f"{name + ' ' if name else ''}{tag}@{coords}")
                continue
            contents = e.get("contents") or []
            items = ", ".join(f"{c.get('item_id')}x{c.get('count')}" for c in contents[:5])
            lines.append(
                f"{name + ' ' if name else ''}{e.get('block_id')}@{coords} "
                f"[{e.get('dimension', '?')}]: {items or '内容未知'}"
            )
        return "\n".join(lines)

    @staticmethod
    def _key(dimension: str, x, y, z) -> str:
        return f"{dimension}|{int(x)},{int(y)},{int(z)}"

    @staticmethod
    def _same(a: dict, b: dict) -> bool:
        """同一个点没有变化吗？——**类别与标签一起比**。

        只比 block_id/contents 会让"床被拆了、主标签从 `bed` 退成 `home`"这种变化被吞掉，
        记忆里就会一直留着一个过期的 `bed`（陈旧读取）。位置变了由 `_key` 负责。
        """
        return (a.get("block_id") == b.get("block_id")
                and a.get("contents") == b.get("contents")
                and str(a.get("category") or "") == str(b.get("category") or "")
                and sorted(a.get("tags") or []) == sorted(b.get("tags") or []))

    # ── 持久化 ──

    def _load(self):
        path = self.path
        legacy = False
        if not path.exists() and self._legacy_namespace:
            # 迁移：旧的两段式（server__world）文件仍然读得到；下次 save 写成三段式。
            candidate = self.root / f"{self._legacy_namespace}.json"
            if candidate.exists():
                path, legacy = candidate, True
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            entries = data.get("entries", {})
            self._entries = entries if isinstance(entries, dict) else {}
            # 老文件可能超过上界：载入时就地保留 last_seen 最新的 `MAX_POI_ENTRIES` 条。
            if len(self._entries) > MAX_POI_ENTRIES:
                keep = sorted(self._entries.items(),
                              key=lambda kv: kv[1].get("last_seen", 0.0), reverse=True)[:MAX_POI_ENTRIES]
                self._entries = dict(keep)
            # 死亡点（跨重启还在）：坏条目一律丢掉（宁可少记，也不留半条假记录）。
            deaths = data.get("deaths")
            rows = [row for row in (normalize_death_point(item) for item in deaths or [])
                    if row is not None]
            self._deaths = rows[:DEATH_POINT_LIMIT]
            self._dirty = legacy
            logger.info("[PoiMemory] 载入 %s：%d 个 POI%s", self._namespace, len(self._entries),
                        "（旧两段式文件）" if legacy else "")
        except Exception as e:
            logger.warning("[PoiMemory] 载入失败: %s", e)

    def save(self):
        if not self._dirty or not self._namespace:
            return
        data = {
            "namespace": self._namespace,
            "saved_at": time.time(),
            "entries": self._entries,
            "deaths": self._deaths,
        }
        self.path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        self._dirty = False
        logger.debug("[PoiMemory] 保存 %s：%d 个 POI", self._namespace, len(self._entries))
