"""制作/取物的门与线协议契约（planner 分发 + skills 真实出发的 JSONL 帧）。

验收清单：
  ① 未授权 craft / 翻箱被拒，且**一帧都不发**；
  ② 获授权后取用量 <= 需求（不搬空），并且取用有审计；
  ③ 背包材料齐时**不翻箱**（就地 craft_via_recipe，容器 0）；
  ④ 没人指挥（自主轮）时没有授权 → 不发起制作（也没有自主开关可开）；
  ⑤ 新工具（equip_armor / craft_via_recipe / container_move / container_click /
     container_close）的参数组装与 Java `ActionExecutor` 逐字对齐。

假 socket 走**真实的 `_send_line`**（含 UTF-8 与换行），抓真正上线的字节。
"""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from agent.commands import Commands
from agent.permissions import TIER_GUEST, TIER_MASTER, PermissionsStore
from agent.planner import Planner
from agent.session import Session
from agent.skills import Skills
from protocol.wire_client import WireClient


class DummyMemory:
    def build_context(self):
        return {}

    def get_recent_context(self, _count):
        return []


class RecordingSkills:
    """记录每一条下发（`skills.calls` 就是"发了哪些帧"的代表）。"""

    def __init__(self):
        self.calls = []

    def craft_item(self, item_name, count=1):
        self.calls.append(("craft_item", item_name, count))

    def craft_via_recipe(self, item, count=1):
        self.calls.append(("craft_via_recipe", item, count))

    def equip_armor(self):
        self.calls.append(("equip_armor",))

    def container_move(self, from_slot, to_slot=None, count=None):
        self.calls.append(("container_move", from_slot, to_slot, count))

    def container_click(self, slot, button=0, click_type="pickup"):
        self.calls.append(("container_click", slot, button, click_type))

    def container_close(self):
        self.calls.append(("container_close",))

    def go_to_block(self, block, radius=32, remember=True):
        self.calls.append(("go_to_block", block, radius, remember))


def _store(tmp: str, **patch) -> PermissionsStore:
    store = PermissionsStore(Path(tmp) / "permissions.json")
    if patch:
        store.set(patch)
    return store


def _planner(skills, store, **kwargs) -> Planner:
    return Planner(llm_service=None, memory=DummyMemory(), skills=skills,
                   permissions=store, **kwargs)


class CraftGateTests(unittest.TestCase):
    """① / ③ / ④：授权门与库存优先。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_unauthorized_craft_is_rejected_without_any_frame(self):
        skills = RecordingSkills()
        planner = _planner(skills, _store(self.tmp.name))  # 默认表：guest 不许造

        dispatched = planner._dispatch_tool_call(
            "craft_via_recipe", {"item": "stick", "count": 4},
            {"actor_tier": TIER_GUEST, "inventory": [{"name": "oak_planks", "count": 64}]})

        self.assertFalse(dispatched)
        self.assertEqual(skills.calls, [], "没被允许造东西时一帧都不许发")
        self.assertIn("没被允许", planner._craft_note)

    def test_craft_without_permission_store_is_denied_by_default(self):
        skills = RecordingSkills()
        planner = Planner(llm_service=None, memory=DummyMemory(), skills=skills)

        self.assertFalse(planner._dispatch_tool_call(
            "craft_via_recipe", {"item": "stick"}, {"inventory": []}))
        self.assertEqual(skills.calls, [])

    def test_authorized_master_crafts_in_place_when_materials_are_in_the_backpack(self):
        """③ 库存齐 → 就地 craft_via_recipe（容器 0），绝不翻箱。"""
        skills = RecordingSkills()
        store = _store(self.tmp.name)  # 默认 craft[master]=True
        planner = _planner(skills, store)
        context = {
            "actor_tier": TIER_MASTER,
            "inventory": [{"name": "minecraft:iron_ingot", "count": 3},
                          {"name": "stick", "count": 2}],
            "known_containers": [
                {"block_id": "minecraft:chest", "x": 1, "y": 64, "z": 1,
                 "contents": [{"item_id": "iron_ingot", "count": 64}]}],
        }

        dispatched = planner._dispatch_tool_call(
            "craft_via_recipe", {"item": "iron_pickaxe", "count": 1}, context)

        self.assertTrue(dispatched)
        self.assertEqual(skills.calls, [("craft_via_recipe", "iron_pickaxe", 1)])
        self.assertEqual(planner._craft_need, {}, "材料够时不该登记缺口")

    def test_authorized_but_material_short_without_container_rights_does_not_craft(self):
        """材料不齐且没授权翻箱 → 如实说缺什么，一帧都不发（绝不偷偷开箱）。"""
        skills = RecordingSkills()
        store = _store(self.tmp.name, craft={"guest": True})  # 能造，但不能翻箱
        planner = _planner(skills, store)
        context = {
            "actor_tier": TIER_GUEST,
            "inventory": [{"name": "iron_ingot", "count": 1}],
            "known_containers": [
                {"block_id": "minecraft:chest", "x": 1, "y": 64, "z": 1,
                 "contents": [{"item_id": "iron_ingot", "count": 64}]}],
        }

        dispatched = planner._dispatch_tool_call(
            "craft_via_recipe", {"item": "iron_pickaxe", "count": 1}, context)

        self.assertFalse(dispatched)
        self.assertEqual(skills.calls, [])
        self.assertIn("iron_ingot x2", planner._craft_note)
        self.assertIn("没被授权翻箱", planner._craft_note)

    def test_authorized_shortfall_routes_to_a_known_container_without_crafting_yet(self):
        skills = RecordingSkills()
        store = _store(self.tmp.name)
        planner = _planner(skills, store)
        context = {
            "actor_tier": TIER_MASTER,
            "inventory": [{"name": "stick", "count": 2}],
            "known_containers": [
                {"block_id": "minecraft:chest", "x": 1, "y": 64, "z": 1, "distance": 6,
                 "contents": [{"item_id": "iron_ingot", "count": 64}]}],
        }

        dispatched = planner._dispatch_tool_call(
            "craft_via_recipe", {"item": "iron_pickaxe", "count": 1}, context)

        self.assertTrue(dispatched)
        self.assertEqual([call[0] for call in skills.calls], ["go_to_block"])
        self.assertEqual(planner._craft_need, {"iron_ingot": 3})
        self.assertIn("还缺 iron_ingot x3", planner._craft_note)

    def test_autonomous_craft_is_denied_without_a_commander(self):
        """④ 没人指挥（没有 actor_tier）→ 按 guest：不许造，也没有开关可开。"""
        skills = RecordingSkills()
        planner = _planner(skills, _store(self.tmp.name))

        dispatched = planner._dispatch_tool_call(
            "craft_via_recipe", {"item": "stick", "count": 1},
            {"inventory": [{"name": "oak_planks", "count": 64}]})

        self.assertFalse(dispatched)
        self.assertEqual(skills.calls, [])
        self.assertIn("guest", planner._craft_note)

    def test_autonomous_craft_cannot_be_switched_on(self):
        """自主开关已删除：权限表里没有 autonomy_craft 这个键，写了也不生效。"""
        skills = RecordingSkills()
        store = _store(self.tmp.name, autonomy_craft=True)

        dispatched = _planner(skills, store)._dispatch_tool_call(
            "craft_via_recipe", {"item": "stick", "count": 1},
            {"inventory": [{"name": "oak_planks", "count": 64}]})

        self.assertFalse(dispatched)
        self.assertEqual(skills.calls, [])
        self.assertNotIn("autonomy_craft", store.get())

    def test_autonomous_shortfall_never_opens_containers(self):
        skills = RecordingSkills()
        planner = _planner(skills, _store(self.tmp.name))
        context = {
            "inventory": [],
            "known_containers": [
                {"block_id": "minecraft:chest", "x": 1, "y": 64, "z": 1,
                 "contents": [{"item_id": "iron_ingot", "count": 64}]}],
        }

        self.assertFalse(planner._dispatch_tool_call(
            "craft_via_recipe", {"item": "iron_pickaxe", "count": 1}, context))
        self.assertEqual(skills.calls, [], "没人授权时不许为了自造去翻箱")


class ContainerGateTests(unittest.TestCase):
    """① / ② / ③：翻箱授权、用多少取多少、审计。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_unauthorized_container_move_and_click_are_rejected(self):
        skills = RecordingSkills()
        planner = _planner(skills, _store(self.tmp.name))

        move = planner._dispatch_tool_call(
            "container_move", {"from_slot": 5, "to_slot": 36, "count": 1},
            {"actor_tier": TIER_GUEST})
        click = planner._dispatch_tool_call(
            "container_click", {"slot": 1, "click_type": "quick_move"},
            {"actor_tier": TIER_GUEST})

        self.assertFalse(move)
        self.assertFalse(click)
        self.assertEqual(skills.calls, [], "没授权时容器操作一帧都不许发")

    def test_authorized_take_is_capped_by_the_shortfall(self):
        """② 授权后取用量 <= 需求：整组请求也会被压到"还缺的量"。"""
        skills = RecordingSkills()
        planner = _planner(skills, _store(self.tmp.name))
        planner._track_craft_need({"iron_ingot": 3})
        context = {
            "actor_tier": TIER_MASTER,
            "container_items": [{"slot": 0, "item": "minecraft:iron_ingot", "count": 64}],
        }

        dispatched = planner._dispatch_tool_call(
            "container_move", {"from_slot": 0, "to_slot": 36}, context)

        self.assertTrue(dispatched)
        self.assertEqual(skills.calls, [("container_move", 0, 36, 3)])
        self.assertEqual(planner._craft_need, {})
        audit = planner.take_audit()
        self.assertEqual(audit[-1]["item"], "iron_ingot")
        self.assertEqual(audit[-1]["count"], 3)

    def test_take_is_refused_once_the_need_is_already_filled(self):
        skills = RecordingSkills()
        planner = _planner(skills, _store(self.tmp.name))
        planner._track_craft_need({"iron_ingot": 2})
        context = {"actor_tier": TIER_MASTER,
                   "container_items": [{"slot": 0, "item": "iron_ingot", "count": 64}]}

        planner._dispatch_tool_call("container_move", {"from_slot": 0, "count": 2}, context)
        again = planner._dispatch_tool_call("container_move", {"from_slot": 0, "count": 2}, context)

        self.assertFalse(again, "已经够了就不该再拿（不搬空）")
        self.assertEqual([call[3] for call in skills.calls], [2])

    def test_partial_take_can_be_topped_up_later(self):
        skills = RecordingSkills()
        planner = _planner(skills, _store(self.tmp.name))
        planner._track_craft_need({"iron_ingot": 5})
        context = {"actor_tier": TIER_MASTER,
                   "container_items": [{"slot": 3, "item": "iron_ingot", "count": 64}]}

        planner._dispatch_tool_call("container_move", {"from_slot": 3, "count": 2}, context)
        planner._dispatch_tool_call("container_move", {"from_slot": 3, "count": 9}, context)

        self.assertEqual([call[3] for call in skills.calls], [2, 3])
        self.assertEqual(planner._craft_need, {})

    def test_container_move_can_still_be_used_without_a_craft_need(self):
        skills = RecordingSkills()
        planner = _planner(skills, _store(self.tmp.name))

        dispatched = planner._dispatch_tool_call(
            "container_move", {"from_slot": 5, "to_slot": 36, "count": 1},
            {"actor_tier": TIER_MASTER})

        self.assertTrue(dispatched)
        self.assertEqual(skills.calls, [("container_move", 5, 36, 1)])

    def test_unknown_slot_cannot_empty_a_stack_while_short(self):
        """认不出这一格是什么时（Java 的 open_container 只报槽位数）：不许整组搬空。"""
        skills = RecordingSkills()
        planner = _planner(skills, _store(self.tmp.name))
        planner._track_craft_need({"iron_ingot": 3})
        context = {"actor_tier": TIER_MASTER}

        whole = planner._dispatch_tool_call("container_move", {"from_slot": 0}, context)
        too_much = planner._dispatch_tool_call(
            "container_move", {"from_slot": 0, "count": 9}, context)
        fine = planner._dispatch_tool_call(
            "container_move", {"from_slot": 0, "count": 2}, context)

        self.assertFalse(whole)
        self.assertFalse(too_much)
        self.assertTrue(fine)
        self.assertEqual(skills.calls, [("container_move", 0, None, 2)])


class ArmorDispatchTests(unittest.TestCase):
    """盔甲：背包里有就穿；没有就去已知容器里找（"点容器找盔甲"）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_armor_in_backpack_equips_directly(self):
        skills = RecordingSkills()
        planner = _planner(skills, _store(self.tmp.name))

        dispatched = planner._dispatch_tool_call(
            "equip_armor", {}, {"actor_tier": TIER_MASTER,
                                "inventory": [{"name": "minecraft:iron_helmet", "count": 1}]})

        self.assertTrue(dispatched)
        self.assertEqual(skills.calls, [("equip_armor",)])

    def test_armor_in_a_known_container_sends_us_there_when_authorized(self):
        skills = RecordingSkills()
        planner = _planner(skills, _store(self.tmp.name))
        context = {
            "actor_tier": TIER_MASTER,
            "inventory": [{"name": "dirt", "count": 12}],
            "known_containers": [
                {"block_id": "minecraft:chest", "x": 8, "y": 64, "z": -4, "distance": 9,
                 "contents": [{"item_id": "iron_chestplate", "count": 1}]}],
        }

        dispatched = planner._dispatch_tool_call("equip_armor", {}, context)

        self.assertTrue(dispatched)
        self.assertEqual(skills.calls[0][0], "go_to_block")
        self.assertEqual(skills.calls[0][1], "minecraft:chest")
        self.assertIn("iron_chestplate", planner._craft_note)

    def test_armor_in_a_container_is_not_touched_without_container_rights(self):
        skills = RecordingSkills()
        planner = _planner(skills, _store(self.tmp.name))
        context = {
            "actor_tier": TIER_GUEST,  # 默认表：guest 不许授权翻箱
            "inventory": [],
            "known_containers": [
                {"block_id": "minecraft:chest", "x": 8, "y": 64, "z": -4,
                 "contents": [{"item_id": "iron_chestplate", "count": 1}]}],
        }

        self.assertFalse(planner._dispatch_tool_call("equip_armor", {}, context))
        self.assertEqual(skills.calls, [])
        self.assertIn("没被授权翻箱", planner._craft_note)

    def test_no_armor_anywhere_says_so_honestly(self):
        skills = RecordingSkills()
        planner = _planner(skills, _store(self.tmp.name))

        dispatched = planner._dispatch_tool_call(
            "equip_armor", {}, {"actor_tier": TIER_MASTER, "inventory": [], "known_containers": []})

        self.assertFalse(dispatched)
        self.assertEqual(skills.calls, [])
        self.assertIn("没有盔甲", planner._craft_note)


class CommandCraftGateTests(unittest.TestCase):
    """④ 控制台路的 `!craft` 也要过门（没人指挥 → 按 guest 拒绝）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_craft_command_is_refused_without_a_commander(self):
        skills = RecordingSkills()
        store = _store(self.tmp.name)
        commands = Commands(skills, authorizer=lambda: store.craft_policy(""))

        commands.parse_and_execute("!craft wooden_sword 1",
                                   {"inventory": [{"name": "oak_planks", "count": 8}]})

        self.assertEqual(skills.calls, [])

    def test_craft_command_runs_when_authorized_and_materials_are_held(self):
        skills = RecordingSkills()
        store = _store(self.tmp.name, craft={"guest": True})
        commands = Commands(skills, authorizer=lambda: store.craft_policy(""))

        commands.parse_and_execute("!craft wooden_sword 1",
                                   {"inventory": [{"name": "oak_planks", "count": 2},
                                                  {"name": "stick", "count": 1}]})

        self.assertEqual(skills.calls, [("craft_item", "wooden_sword", 1)])

    def test_craft_command_still_refuses_when_materials_are_short(self):
        """优先只用背包里的材料：缺料就什么都不做（也不许翻箱）。"""
        skills = RecordingSkills()
        store = _store(self.tmp.name, craft={"guest": True})
        commands = Commands(skills, authorizer=lambda: store.craft_policy(""))

        commands.parse_and_execute("!craft iron_pickaxe 1", {"inventory": []})

        self.assertEqual(skills.calls, [])

    def test_commands_without_an_authorizer_are_unchanged(self):
        skills = RecordingSkills()
        Commands(skills).parse_and_execute("!craft wooden_sword 1", {})

        self.assertEqual(skills.calls, [("craft_item", "wooden_sword", 1)])


class FakeSocket:
    def __init__(self):
        self.chunks: list[bytes] = []

    def sendall(self, data: bytes) -> None:
        self.chunks.append(data)

    def close(self) -> None:
        pass


class NewCommandWireContractTests(unittest.TestCase):
    """⑤ 真实帧的字段名/槽位/click_type 必须与 Java `ActionExecutor` 逐字对齐。"""

    def setUp(self):
        self.wire = WireClient("127.0.0.1", 25568)
        self.fake = FakeSocket()
        self.wire.sock = self.fake
        self.wire._running = True
        self.skills = Skills(wire=self.wire)

    def _frames(self):
        raw = b"".join(self.fake.chunks).decode("utf-8")
        return [json.loads(line) for line in raw.splitlines()]

    def test_equip_armor_sends_no_args(self):
        self.skills.equip_armor()
        frame = self._frames()[-1]
        self.assertEqual(frame["type"], "command")
        self.assertEqual(frame["cmd"], "equip_armor")
        self.assertEqual(frame["args"], {})
        self.assertTrue(frame["id"].startswith("req_"))

    def test_craft_via_recipe_sends_item_and_count(self):
        self.skills.craft_via_recipe("minecraft:iron_pickaxe", 2)
        self.assertEqual(self._frames()[-1]["args"], {"item": "minecraft:iron_pickaxe", "count": 2})

        self.skills.craft_via_recipe("stick")
        self.assertEqual(self._frames()[-1]["args"], {"item": "stick", "count": 1})

    def test_container_move_slots_and_count(self):
        self.skills.container_move(5, 36, 3)
        self.assertEqual(self._frames()[-1]["args"],
                         {"from_slot": 5, "to_slot": 36, "count": 3})

    def test_container_move_omits_to_slot_for_quick_move_and_count_for_display(self):
        self.skills.container_move(9)
        # to_slot 省略＝QUICK_MOVE（Java 侧原版 shift 点击）；count 省略＝整组
        self.assertEqual(self._frames()[-1]["args"], {"from_slot": 9})

    def test_container_move_command_name_is_the_new_one(self):
        self.skills.container_move(9, -1, 2)
        frame = self._frames()[-1]
        self.assertEqual(frame["cmd"], "container_move")
        self.assertEqual(frame["args"], {"from_slot": 9, "to_slot": -1, "count": 2})

    def test_container_click_sends_slot_button_and_click_type(self):
        self.skills.container_click(1, 0, "quick_move")
        self.assertEqual(self._frames()[-1]["args"],
                         {"slot": 1, "button": 0, "click_type": "quick_move"})

        self.skills.container_click(42)
        self.assertEqual(self._frames()[-1]["args"],
                         {"slot": 42, "button": 0, "click_type": "pickup"})

    def test_container_close_sends_no_args(self):
        self.skills.container_close()
        frame = self._frames()[-1]
        self.assertEqual(frame["cmd"], "container_close")
        self.assertEqual(frame["args"], {})

    def test_all_new_frames_are_newline_terminated_utf8_jsonl(self):
        self.skills.equip_armor()
        self.skills.craft_via_recipe("stick", 1)
        self.skills.container_move(5, 36, 1)
        self.skills.container_click(1, 0, "pickup")
        self.skills.container_close()

        for chunk in self.fake.chunks:
            json.loads(chunk.decode("utf-8"))


class SessionWiringTests(unittest.TestCase):
    """session → planner 的接线：谁在说话、已知容器都要传下去。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _session(self):
        session = Session.__new__(Session)
        session.runtime = {
            "inventory": [{"name": "oak_log", "count": 2}],
            "players": [],
            "open_container": {"id": 3, "title": "chest", "slots": 63, "kind": "chest"},
        }
        session.memory = SimpleNamespace(build_context=lambda: {})
        session.vision = SimpleNamespace(latest=lambda: "")
        session.poi = SimpleNamespace(
            describe=lambda: "",
            find=lambda _category: [{"block_id": "minecraft:chest", "x": 1, "y": 64, "z": 1,
                                     "contents": [{"item_id": "iron_ingot", "count": 8}]}])
        session.last_speaker = {}
        session.chat_inbox = None
        session._hud_note = lambda: ""
        session.persona_blocks = lambda *_a, **_k: {}
        session.conversation = SimpleNamespace(describe=lambda _n: {})
        session.llm_active = lambda: True
        session._actor = {"name": "Owner", "tier": TIER_MASTER}
        return session

    def test_runtime_context_carries_actor_tier_and_containers(self):
        context = Session._runtime_context(self._session())

        self.assertEqual(context["actor_tier"], TIER_MASTER)
        self.assertNotIn("autonomous", context, "已无自主开关这一说")
        self.assertEqual(context["known_containers"][0]["block_id"], "minecraft:chest")
        self.assertEqual(context["open_container"]["slots"], 63)

    def test_command_craft_policy_is_guest_deny_by_default(self):
        session = self._session()
        session.permissions = PermissionsStore(Path(self.tmp.name) / "p.json")

        policy = Session._command_craft_policy(session)
        self.assertFalse(policy.may_craft)
        self.assertFalse(policy.may_open_containers)
        self.assertEqual(policy.tier, TIER_GUEST)

        session.permissions.set({"craft": {"guest": True}})
        allowed = Session._command_craft_policy(session)
        self.assertTrue(allowed.may_craft)
        self.assertFalse(allowed.may_open_containers, "没人指挥也不许翻箱")


class PromptWiringTests(unittest.TestCase):
    """工具表与规则真的进了 prompt（模型才知道有这些动作）。"""

    def _prompt(self, task_state=None):
        planner = Planner(llm_service=None, memory=DummyMemory(), skills=RecordingSkills())
        return planner._build_planner_prompt(
            "Steve", "帮我造个铁镐", {"task_state": task_state or {}}, "AI")

    def test_new_tool_names_are_advertised(self):
        prompt = self._prompt()
        for tool in ("equip_armor", "craft_via_recipe", "container_move",
                     "container_click", "container_close"):
            self.assertIn(tool, prompt, f"prompt 里缺少工具 {tool}")

    def test_rules_cover_armor_inventory_first_and_no_sneaky_containers(self):
        prompt = self._prompt()
        self.assertIn("要穿甲 → equip_armor", prompt)
        self.assertIn("合成优先用自己背包", prompt)
        self.assertIn("没被授权不要开箱", prompt)
        self.assertIn("别整箱搬空", prompt)

    def test_container_tools_are_available_in_craft_phase(self):
        from agent.planner import _actions_for_phase

        for phase in ("idle", "craft", "gather"):
            names = [name for name, _, _ in _actions_for_phase(phase)]
            for tool in ("container_move", "container_click", "container_close", "equip_armor"):
                self.assertIn(tool, names, f"{phase} 阶段缺少 {tool}")


if __name__ == "__main__":
    unittest.main()
