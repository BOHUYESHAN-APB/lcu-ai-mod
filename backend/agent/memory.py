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
        self.max_recent = 50  # 保留最近 50 条消息
        
        # 长期记忆：重要事件
        self.events: list[dict] = []
        self.max_events = 500
        
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
            self.events.pop(0)
        
        logger.info("[Memory] 记录事件: %s - %s", event_type, description[:50])
    
    def record_action(self, action: str, success: bool):
        """记录一个动作。"""
        self.total_actions += 1
        self.events.append({
            "time": time.time(),
            "type": "action",
            "description": f"{action} ({'成功' if success else '失败'})",
        })
    
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
        
        # 更新常用词（简单实现）
        words = message.lower().split()
        for word in words:
            if len(word) > 1:
                profile["common_words"][word] = profile["common_words"].get(word, 0) + 1
    
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
            self.recent_messages = data.get("recent_messages", [])
            self.events = data.get("events", [])
            self.player_profiles = data.get("player_profiles", {})
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
        }
        self.path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        self._dirty = False
        logger.debug("[PoiMemory] 保存 %s：%d 个 POI", self._namespace, len(self._entries))
