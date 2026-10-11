"""借用台账 + 规则性记忆（拿＝借 / 归还 / 校验 / 审计）。

覆盖用户口径（生电服）：
  ① 授权后才允许借；没授权仍拒（guest 没翻箱权也一样拒）；
  ② 单次取用不超上限（min(256, 需求×2)），整箱搬（不给 count）一律拒绝；
  ③ 任务结束自动归还剩余，并用 `open_container.items` 的真实快照校验"真的回去了"；
  ④ 回不去要**报出来**（结算失败 + 快照核对失败两条路都测）；
  ⑤ 口头授权落成带条件的规则、按 server 分区持久化，下次（换实例）仍生效；
  ⑥ 每次借/还有审计，字段完整（物品/数量/容器/原因/结果）。

全部离线：假 skills 记录下发帧，临时目录落盘，不碰真实 data/。
"""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from agent.borrow import (
    CONDITION_NO_CARRY,
    CONDITION_RETURN,
    BorrowLedger,
    BorrowRulesStore,
    parse_borrow_grant,
)
from agent.permissions import TIER_GUEST, TIER_MASTER, PermissionsStore
from agent.planner import Planner
from agent.session import Session
from agent.task_list import TaskList


class DummyMemory:
    def build_context(self):
        return {}

    def get_recent_context(self, _count):
        return []


class RecordingSkills:
    """记录每一条下发（`calls` 就是"发了哪些帧"的代表）。"""

    def __init__(self):
        self.calls = []

    def container_move(self, from_slot, to_slot=None, count=None):
        self.calls.append(("container_move", from_slot, to_slot, count))

    def container_close(self):
        self.calls.append(("container_close",))

    def go_to_block(self, block, radius=32, remember=True):
        self.calls.append(("go_to_block", block, radius, remember))

    def open_container(self):
        self.calls.append(("open_container",))

    def moves(self):
        return [call for call in self.calls if call[0] == "container_move"]


# 箱子：8 格 ×64 铁 = 512（叠到上限，贴近真实；别用"一格 500 个"这种不可能的假数据）。
IRON_BASELINE = 8 * 64


def chest(items=None, *, cid=3, title="仓库箱子", slots=63, player_start=27):
    return {
        "id": cid,
        "title": title,
        "slots": slots,
        "kind": "chest",
        "player_inv_start": player_start,
        "items": items if items is not None else [
            {"slot": slot, "item": "minecraft:iron_ingot", "count": 64} for slot in range(8)],
    }


# 借出 8 个之后：第 0 格剩 56，玩家的 27 格里躺着 8 个。
AFTER_BORROW = chest([
    {"slot": 0, "item": "minecraft:iron_ingot", "count": 56},
    *[{"slot": slot, "item": "minecraft:iron_ingot", "count": 64} for slot in range(1, 8)],
    {"slot": 27, "item": "minecraft:iron_ingot", "count": 8},
])
# 还没还：背包里已经没有铁，容器里也还是 504（缺 8）。
STILL_SHORT = chest([
    {"slot": 0, "item": "minecraft:iron_ingot", "count": 56},
    *[{"slot": slot, "item": "minecraft:iron_ingot", "count": 64} for slot in range(1, 8)],
])
# 还好了：容器回到 512。
RESTORED = chest()


class BorrowTestCase(unittest.TestCase):
    """公共装配：临时目录 + 授权规则 + planner。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def permissions(self, **patch) -> PermissionsStore:
        store = PermissionsStore(self.root / "permissions.json")
        if patch:
            store.set(patch)
        return store

    def rules(self, server="play.example:25565", **kwargs) -> BorrowRulesStore:
        kwargs.setdefault("root", self.root / "rules")
        return BorrowRulesStore(server=server, **kwargs)

    def planner(self, skills, permissions, rules) -> Planner:
        planner = Planner(llm_service=None, memory=DummyMemory(), skills=skills,
                          permissions=permissions, borrow_rules=rules)
        planner.borrow_ledger.use_audit_path(self.root / "rules" / "audit.jsonl")
        return planner

    @staticmethod
    def grant(rules, *, granted_by="Steve", tier=TIER_MASTER):
        text = "你可以使用仓库里所有铁，因为我们有刷铁机，用完记得放回去，不要随身携带"
        parsed = parse_borrow_grant(text)
        assert parsed, "这句话必须能被认成仓库授权"
        return rules.grant(parsed, granted_by=granted_by, granted_tier=tier, source=text)


# ── ① 授权门 ────────────────────────────────────────────────────

class BorrowAuthorizationTests(BorrowTestCase):
    def test_borrow_is_refused_without_a_spoken_grant(self):
        """"授权"是前提：没说过就不许借，且**一帧都不发**。"""
        skills = RecordingSkills()
        planner = self.planner(skills, self.permissions(), self.rules())
        context = {"actor_tier": TIER_MASTER, "open_container": chest()}

        dispatched = planner._dispatch_tool_call(
            "borrow_item", {"item": "iron_ingot", "count": 8}, context)

        self.assertFalse(dispatched)
        self.assertEqual(skills.calls, [], "没授权时一帧都不许发")
        self.assertEqual(planner.borrow_ledger.outstanding(), [])
        self.assertIn("授权", planner._craft_note)

    def test_borrow_allowed_only_after_the_grant(self):
        skills = RecordingSkills()
        rules = self.rules()
        planner = self.planner(skills, self.permissions(), rules)
        context = {"actor_tier": TIER_MASTER, "open_container": chest()}

        self.assertFalse(planner._dispatch_tool_call(
            "borrow_item", {"item": "iron_ingot", "count": 8}, context), "授权前拒绝")
        self.grant(rules)
        self.assertTrue(planner._dispatch_tool_call(
            "borrow_item", {"item": "iron_ingot", "count": 8}, context), "授权后可以借")

        self.assertEqual(skills.moves(), [("container_move", 0, 27, 8)])
        outstanding = planner.borrow_ledger.outstanding()
        self.assertEqual(len(outstanding), 1)
        self.assertEqual((outstanding[0].item, outstanding[0].count), ("iron_ingot", 8))

    def test_iron_family_is_covered_by_the_same_rule(self):
        """授权"所有铁"＝铁类：铁锭/粗铁/铁块都算。"""
        skills = RecordingSkills()
        rules = self.rules()
        self.grant(rules)
        planner = self.planner(skills, self.permissions(), rules)
        context = {"actor_tier": TIER_MASTER, "open_container": chest(items=[
            {"slot": 0, "item": "minecraft:raw_iron", "count": 64}])}

        self.assertTrue(planner._dispatch_tool_call(
            "borrow_item", {"item": "raw_iron", "count": 4}, context))
        self.assertEqual(skills.moves(), [("container_move", 0, 27, 4)])
        self.assertIsNone(rules.match("stone"), "石头的取用不该被这条授权覆盖")

    def test_guest_still_cannot_borrow_even_with_a_rule(self):
        """规则归规则，能力位归能力位：guest 没有翻箱授权，借不成（查表，不写 tier 分支）。"""
        skills = RecordingSkills()
        rules = self.rules()
        self.grant(rules)
        planner = self.planner(skills, self.permissions(), rules)

        dispatched = planner._dispatch_tool_call(
            "borrow_item", {"item": "iron_ingot", "count": 2},
            {"actor_tier": TIER_GUEST, "open_container": chest()})

        self.assertFalse(dispatched)
        self.assertEqual(skills.calls, [])


# ── ② 上限 / 整箱搬 ─────────────────────────────────────────────

class BorrowLimitTests(BorrowTestCase):
    def setUp(self):
        super().setUp()
        self.skills = RecordingSkills()
        self.rules = self.rules()
        self.grant(self.rules)
        self.planner = self.planner(self.skills, self.permissions(), self.rules)
        self.context = {"actor_tier": TIER_MASTER, "open_container": chest()}

    def test_whole_chest_request_is_refused(self):
        """不给 count＝想整箱搬 → 拒绝（口径：整箱搬一律拒绝）。"""
        dispatched = self.planner._dispatch_tool_call(
            "borrow_item", {"item": "iron_ingot"}, self.context)

        self.assertFalse(dispatched)
        self.assertEqual(self.skills.calls, [])
        self.assertIn("整箱搬", self.planner._craft_note)

    def test_single_take_is_capped_by_need_times_two(self):
        """需求 10 → 一次最多 20（2×需求）；要 50 被拒，要 20 放行。"""
        self.planner._track_craft_need({"iron_ingot": 10})

        over = self.planner._dispatch_tool_call(
            "borrow_item", {"item": "iron_ingot", "count": 50}, self.context)

        self.assertFalse(over, "超上限必须拒绝")
        self.assertIn("最多借 20 个", self.planner._craft_note)
        self.assertEqual(self.skills.calls, [], "被拒时一帧都不发")

        fine = self.planner._dispatch_tool_call(
            "borrow_item", {"item": "iron_ingot", "count": 20}, self.context)

        self.assertTrue(fine)
        self.assertEqual(self.skills.moves(), [("container_move", 0, 27, 20)])

    def test_no_need_still_caps_at_256(self):
        """没有制作需求时按绝对上限 256 卡（不能一次把 27 格全搬走）。"""
        self.planner._track_craft_need({})  # 没登记需求 → 只按 256
        self.assertFalse(self.planner._dispatch_tool_call(
            "borrow_item", {"item": "iron_ingot", "count": 257}, self.context))
        self.assertTrue(self.planner._dispatch_tool_call(
            "borrow_item", {"item": "iron_ingot", "count": 256}, self.context))
        # 一格里最多 64：256 个要跨 4 格搬，四条移动加起来正好 256。
        moves = self.skills.moves()
        self.assertEqual([move[3] for move in moves], [64, 64, 64, 64])
        self.assertEqual(sum(move[3] for move in moves), 256)
        self.assertEqual(self.planner.borrow_ledger.remaining("iron_ingot"), 256)

    def test_ledger_limit_is_two_times_need_or_256(self):
        ledger = BorrowLedger()
        self.assertEqual(ledger.limit_for("iron_ingot", 10), 20)
        self.assertEqual(ledger.limit_for("iron_ingot", 1000), 256)
        self.assertEqual(ledger.limit_for("iron_ingot"), 256)


# ── ③ 任务结束自动归还 + 回执校验 ───────────────────────────────

class BorrowReturnTests(BorrowTestCase):
    def setUp(self):
        super().setUp()
        self.skills = RecordingSkills()
        self.rules = self.rules()
        self.grant(self.rules)
        self.planner = self.planner(self.skills, self.permissions(), self.rules)
        self.context = {"actor_tier": TIER_MASTER, "open_container": chest()}
        self.assertTrue(self.planner._dispatch_tool_call(
            "borrow_item", {"item": "iron_ingot", "count": 8}, self.context))
        self.skills.calls.clear()

    def test_task_end_returns_the_rest_and_verifies_the_snapshot(self):
        result = self.planner.settle_borrows(
            "task_done", {"open_container": AFTER_BORROW})

        self.assertEqual(len(result["sent"]), 1, "结算要真的下发归还")
        self.assertEqual(self.skills.moves(), [("container_move", 27, 0, 8)])
        self.assertEqual(self.planner.borrow_ledger.remaining(), 8, "还没核对回执前不算还完")

        # 新快照：容器回到 512 → 核对通过。
        rows = self.planner.verify_borrow_returns(RESTORED)

        self.assertEqual([row["result"] for row in rows], ["verified"])
        self.assertEqual(self.planner.borrow_ledger.outstanding(), [])
        self.assertEqual(self.planner.borrow_ledger.remaining(), 0)
        entry = self.planner.borrow_ledger.entries()[-1]
        self.assertEqual(entry.state, "returned")
        self.assertEqual(entry.returned, 8)

    def test_interrupt_and_takeover_also_settle(self):
        """被叫停/玩家接管也是"任务结束"：一样把剩余还回去。"""
        for reason in ("player_takeover", "task_cancelled", "task_failed"):
            after = AFTER_BORROW
            self.planner.settle_borrows(reason, {"open_container": after})
        self.assertTrue(self.skills.moves(), "接管/中断也要下发归还")

    def test_return_tool_does_not_need_the_task_to_end(self):
        dispatched = self.planner._dispatch_tool_call(
            "return_item", {"item": "iron_ingot"},
            {"actor_tier": TIER_MASTER, "open_container": AFTER_BORROW})

        self.assertTrue(dispatched)
        self.assertEqual(self.skills.moves(), [("container_move", 27, 0, 8)])
        self.assertIn("放回", self.planner._craft_note)

    def test_auto_step_opens_the_container_when_it_is_in_reach(self):
        """容器就在跟前但界面没开：自动归还推进一步 = 先开箱（不猜）。"""
        context = {"open_container": {}, "nearby_storage": [
            {"block_id": "minecraft:chest", "title": "仓库箱子", "distance": 2.0}]}

        stepped = self.planner.auto_return_step(context)

        self.assertTrue(stepped)
        self.assertEqual(self.skills.calls, [("open_container",)])


# ── ④ 归还失败要报出来 ──────────────────────────────────────────

class BorrowFailureTests(BorrowTestCase):
    def setUp(self):
        super().setUp()
        self.skills = RecordingSkills()
        self.rules = self.rules()
        self.grant(self.rules)
        self.planner = self.planner(self.skills, self.permissions(), self.rules)
        self.assertTrue(self.planner._dispatch_tool_call(
            "borrow_item", {"item": "iron_ingot", "count": 8},
            {"actor_tier": TIER_MASTER, "open_container": chest()}))
        self.skills.calls.clear()

    def test_unreturnable_settlement_is_reported(self):
        """容器没开、也没记下坐标 → 结算时记失败并**写在说明里**。"""
        result = self.planner.settle_borrows("task_failed", {"open_container": {}})

        self.assertEqual(len(result["failed"]), 1)
        self.assertIn("没能放回去", result["note"])
        entry = self.planner.borrow_ledger.entries()[-1]
        self.assertEqual(entry.state, "failed")
        self.assertEqual(self.skills.moves(), [], "回不去时不该瞎发移动指令")
        actions = [row["action"] for row in self.planner.borrow_ledger.audit()]
        self.assertIn("return_failed", actions)

    def test_container_full_is_reported(self):
        """东西还在手上、可原箱塞满了 → 结算报失败（不能假装还了）。"""
        full = chest([
            {"slot": slot, "item": "minecraft:cobblestone", "count": 64} for slot in range(27)
        ] + [{"slot": 27, "item": "minecraft:iron_ingot", "count": 8}])

        result = self.planner.settle_borrows("task_done", {"open_container": full})

        self.assertEqual(len(result["failed"]), 1)
        self.assertIn("没能放回去", result["note"])
        self.assertEqual(self.skills.moves(), [], "塞不进去时不该瞎发移动指令")
        self.assertEqual(self.planner.borrow_ledger.entries()[-1].failure,
                         "task_done:container_full")

    def test_snapshot_shows_the_return_failed(self):
        """道具没了、容器也没补齐 → 快照核对判失败（reason=still_short）。"""
        self.planner.settle_borrows("task_done", {"open_container": AFTER_BORROW})

        rows = self.planner.verify_borrow_returns(STILL_SHORT)

        self.assertEqual([row["result"] for row in rows], ["failed"])
        self.assertEqual(rows[0]["reason"], "still_short")
        self.assertIn("没能放回原箱", self.planner._craft_note)
        self.assertEqual(self.planner.borrow_ledger.entries()[-1].state, "failed")


# ── ⑤ 规则从对话授权落存 + 下次仍生效 ───────────────────────────

class BorrowRuleMemoryTests(BorrowTestCase):
    def test_sentence_is_parsed_into_a_conditional_rule(self):
        parsed = parse_borrow_grant(
            "你可以使用仓库里所有铁，因为我们有刷铁机，东西用完记得放回去，不要随身携带")

        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["family"], "iron")
        self.assertIn("iron_*", parsed["patterns"])
        self.assertEqual(set(parsed["conditions"]), {CONDITION_RETURN, CONDITION_NO_CARRY})
        self.assertEqual(set(parsed["explicit_conditions"]),
                         {CONDITION_RETURN, CONDITION_NO_CARRY}, "两句条件都明说了")

    def test_negation_and_chit_chat_are_not_grants(self):
        self.assertIsNone(parse_borrow_grant("今天天气不错"))
        self.assertIsNone(parse_borrow_grant("不许用仓库里的铁"))
        self.assertIsNone(parse_borrow_grant("这箱子挺好看"))

    def test_questions_are_not_authorizations(self):
        """问句不是许可：宁可漏授权，也不误授权。"""
        self.assertIsNone(parse_borrow_grant("我可以用仓库里的铁吗？"))
        self.assertIsNone(parse_borrow_grant("can I use the iron in the chest?"))

    def test_english_words_are_matched_whole(self):
        """英文关键字按整词匹配："because" 里的 "use" 不算授权。"""
        self.assertIsNone(parse_borrow_grant("the iron is in the chest because of the farm"))
        self.assertIsNotNone(parse_borrow_grant("you can use all the iron in the chest"))

    def test_plain_item_names_can_be_authorized_too(self):
        parsed = parse_borrow_grant("你可以用仓库里的圆石")

        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["family"], "", "圆石不属于任何族")
        self.assertIn("cobblestone", parsed["patterns"])

    def test_grant_is_stored_per_server_and_survives_a_reload(self):
        rules = self.rules(server="play.example:25565")
        rule = self.grant(rules)

        self.assertTrue(rules.path.exists(), "授权必须落盘")
        raw = json.loads(rules.path.read_text(encoding="utf-8"))
        self.assertEqual(raw["rules"][0]["id"], rule["id"])
        self.assertEqual(raw["rules"][0]["granted_by"], "Steve")

        reloaded = self.rules(server="play.example:25565")
        self.assertIsNotNone(reloaded.match("minecraft:iron_ingot"), "重启/换实例后仍生效")
        self.assertIsNotNone(reloaded.match("iron_block"))
        # 另一个服务器分区不该捡到这条授权。
        self.assertIsNone(self.rules(server="other.example:25565").match("iron_ingot"))

    def test_session_absorbs_the_spoken_grant(self):
        """对话路径：能授权的人说这句话 → 落成规则；没能力位的人说了不算。"""
        session = Session.__new__(Session)
        session.borrow_rules = self.rules(server="play.example:25565")
        session.permissions = self.permissions(master=["Steve"])
        session._borrow_rule_audit = []

        session._absorb_borrow_speech("Steve", "你可以使用仓库里所有铁，因为我们有刷铁机")
        session._absorb_borrow_speech("Bob", "你可以用仓库里所有铁")

        self.assertIsNotNone(session.borrow_rules.match("iron_ingot"))
        results = [row["result"] for row in session._borrow_rule_audit]
        self.assertEqual(results, ["granted", "denied"], "没授权的人说了不算")

    def test_revoking_by_speech_drops_the_rule(self):
        """"别用仓库里的铁了" → 授权收回去（规则性记忆的两头都要能落地）。"""
        session = Session.__new__(Session)
        session.borrow_rules = self.rules(server="play.example:25565")
        session.permissions = self.permissions(master=["Steve"])
        session._borrow_rule_audit = []
        session._absorb_borrow_speech("Steve", "你可以使用仓库里所有铁，因为我们有刷铁机")
        self.assertIsNotNone(session.borrow_rules.match("iron_ingot"))

        session._absorb_borrow_speech("Steve", "别用仓库里的铁了")

        self.assertIsNone(session.borrow_rules.match("iron_ingot"), "收回后不该再有授权")
        self.assertEqual(session._borrow_rule_audit[-1]["result"], "revoked")
        # 收回也要落盘：换个实例读同一个分区，仍然是"没有授权"。
        self.assertIsNone(self.rules(server="play.example:25565").match("iron_ingot"))

    def test_prompt_carries_rules_and_debt(self):
        rules = self.rules()
        self.grant(rules)
        planner = self.planner(RecordingSkills(), self.permissions(), rules)
        prompt = planner._build_planner_prompt(
            "Steve", "继续", {"borrow_rules": rules.describe(), "borrow_pending": "iron_ingot x8"},
            "AI")

        self.assertIn("铁类", prompt)
        self.assertIn("用完必须放回原箱", prompt)
        self.assertIn("iron_ingot x8", prompt)
        self.assertIn("borrow_item", prompt, "工具表要广告这两个工具")
        self.assertIn("return_item", prompt)


# ── ⑥ 审计字段完整 ──────────────────────────────────────────────

class BorrowAuditTests(BorrowTestCase):
    def test_borrow_and_return_audit_rows_carry_every_field(self):
        skills = RecordingSkills()
        rules = self.rules()
        self.grant(rules)
        planner = self.planner(skills, self.permissions(), rules)
        planner.borrow_ledger.use_audit_path(self.root / "rules" / "audit.jsonl")
        planner._dispatch_tool_call(
            "borrow_item", {"item": "iron_ingot", "count": 8, "reason": "制作铁镐"},
            {"actor_tier": TIER_MASTER, "open_container": chest()})
        planner.settle_borrows("task_done", {"open_container": AFTER_BORROW})
        planner.verify_borrow_returns(RESTORED)

        rows = planner.borrow_ledger.audit(50)
        borrow = next(row for row in rows if row["action"] == "borrow")
        returned = next(row for row in rows if row["action"] == "return")
        confirmed = next(row for row in rows if row["action"] == "return_confirmed")

        for row in (borrow, returned, confirmed):
            for field in ("item", "count", "container", "reason", "result", "ts", "entry_id"):
                self.assertIn(field, row, f"审计缺字段 {field}")
        self.assertEqual((borrow["item"], borrow["count"], borrow["container"]),
                         ("iron_ingot", 8, "仓库箱子"))
        self.assertEqual(borrow["reason"], "制作铁镐")
        self.assertEqual(borrow["result"], "taken")
        self.assertEqual((returned["result"], confirmed["result"]), ("sent", "verified"))

        # 审计也要真的落盘（一行一行 JSON）。
        audit_path = self.root / "rules" / "audit.jsonl"
        self.assertTrue(audit_path.exists())
        lines = [json.loads(line) for line in
                 audit_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        self.assertGreaterEqual(len(lines), 3)
        self.assertEqual(lines[-1]["action"], "return_confirmed")


# ── ⑦ 会话接线：任务终态结算 + 快照核对真的挂在事件路径上 ───────

class SessionBorrowWiringTests(BorrowTestCase):
    """不测 planner 内部，只证"事件进来 → 该归还/该核对"这条线是通的。"""

    def _session(self, skills, rules):
        session = Session.__new__(Session)
        session.runtime = {"open_container": {}, "task_state": {}}
        session.tasks = TaskList()
        session.tasks.add("craft", owner="Steve", owner_tier=TIER_MASTER, status="active")
        session.poi = SimpleNamespace(find=lambda _category: [],
                                      use_namespace=lambda *_a: "",
                                      observe_state=lambda *_a: 0,
                                      describe=lambda *_a: "")
        session._actor = {"name": "Steve", "tier": TIER_MASTER}
        session.permissions = self.permissions(master=["Steve"])
        session.borrow_rules = rules
        session._borrow_rule_audit = []
        session._manual_command_until = 0.0
        session._manual_action_reqs = set()
        session._manual_task_kind = None
        session._task_idle_since = None
        session.conversation = None
        session.planner = self.planner(skills, session.permissions, rules)
        return session

    def test_task_terminal_settles_and_the_snapshot_confirms(self):
        skills = RecordingSkills()
        rules = self.rules()
        self.grant(rules)
        session = self._session(skills, rules)
        session.planner._dispatch_tool_call(
            "borrow_item", {"item": "iron_ingot", "count": 8},
            {"actor_tier": TIER_MASTER, "open_container": chest()})
        skills.calls.clear()
        session.runtime["open_container"] = AFTER_BORROW

        session.handle_event("task_state", {"kind": "craft", "status": "done"})

        self.assertEqual(skills.moves(), [("container_move", 27, 0, 8)],
                         "task_state 终态要触发自动归还")
        self.assertEqual(session.planner.borrow_ledger.remaining("iron_ingot"), 8)

        session.handle_event("state_update", {"open_container": RESTORED})

        self.assertEqual(session.planner.borrow_ledger.outstanding(), [],
                         "新快照核对通过后欠账清零")

    def test_stop_all_is_a_player_takeover_that_settles(self):
        skills = RecordingSkills()
        rules = self.rules()
        self.grant(rules)
        session = self._session(skills, rules)
        session.action_manager = SimpleNamespace(is_busy=False, stop=lambda: None,
                                                 track=lambda *_a, **_k: None)
        session.self_prompter = SimpleNamespace(mark_action=lambda: None)
        session._hud_note_local = ""
        session.planner._dispatch_tool_call(
            "borrow_item", {"item": "iron_ingot", "count": 8},
            {"actor_tier": TIER_MASTER, "open_container": chest()})
        skills.calls.clear()
        session.runtime["open_container"] = AFTER_BORROW

        session._on_skill_command("stop_all", "req-1", "manual_chat", {})

        self.assertEqual(skills.moves(), [("container_move", 27, 0, 8)],
                         "玩家叫停＝接管，借的东西要归位")


if __name__ == "__main__":
    unittest.main()
