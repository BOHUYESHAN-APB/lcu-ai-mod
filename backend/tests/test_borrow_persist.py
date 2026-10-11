"""借用台账落盘：未归还的借出跨重启恢复（并且能继续结算 / 回不去要如实报出来）。

覆盖：
  ① 记一笔借出 → 台账文件落盘；
  ② 新实例（＝后端重启）按盘恢复欠账，容器信息（标题/坐标/基线）都在；
  ③ 恢复后能走原来的 settle 路径真的结算（放回 + 快照核对）；
  ④ 重启把"在途归还"冲掉 → 恢复成 `open`（不假装已经还了）；
  ⑤ 容器不可用 → 如实记为 `failed` 未归还，跨重启也照实报出来；
  ⑥ 会话接线：切 server 分区时按该分区的台账恢复（不是只记内存）。
"""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from agent.borrow import BorrowLedger, BorrowRulesStore
from agent.permissions import PermissionsStore
from agent.planner import Planner
from agent.session import Session


class DummyMemory:
    def build_context(self):
        return {}

    def get_recent_context(self, _count):
        return []


class RecordingSkills:
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


# 借出 8 个之后：第 0 格剩 56，玩家背包（第 27 格）里躺着 8 个。
AFTER_BORROW = chest([
    {"slot": 0, "item": "minecraft:iron_ingot", "count": 56},
    *[{"slot": slot, "item": "minecraft:iron_ingot", "count": 64} for slot in range(1, 8)],
    {"slot": 27, "item": "minecraft:iron_ingot", "count": 8},
])
# 还好了：容器回到 512（基线）。
RESTORED = chest()


class BorrowLedgerPersistenceTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.path = self.root / "rules" / "play.example_25565.ledger.json"

    def borrow(self, ledger, *, container=None, **kwargs):
        return ledger.record_borrow(
            "iron_ingot", 8, container=chest() if container is None else container,
            from_slot=0, to_slot=27,
            reason="craft", container_pos=(1.5, 64.0, 2.5),
            container_block="minecraft:chest", **kwargs)

    def test_outstanding_borrow_is_written_and_restored(self):
        ledger = BorrowLedger(ledger_path=self.path)
        entry = self.borrow(ledger)
        self.assertTrue(self.path.exists(), "借出去就该落盘")

        restarted = BorrowLedger(ledger_path=self.path)
        rows = restarted.outstanding()

        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0].item, rows[0].count, rows[0].remaining), ("iron_ingot", 8, 8))
        self.assertEqual(rows[0].container_title, "仓库箱子")
        self.assertEqual(rows[0].container_pos, (1.5, 64.0, 2.5))
        self.assertEqual(rows[0].baseline, IRON_BASELINE)
        self.assertEqual(rows[0].id, entry.id)
        self.assertEqual(rows[0].state, "open")

    def test_restored_borrow_can_be_settled_with_a_snapshot(self):
        ledger = BorrowLedger(ledger_path=self.path)
        self.borrow(ledger)

        restarted = BorrowLedger(ledger_path=self.path)
        entry = restarted.outstanding()[0]
        plan = restarted.plan_move_back(AFTER_BORROW, "iron_ingot")
        self.assertEqual(plan, {"from_slot": 27, "to_slot": 0, "count": 8})
        restarted.begin_return(entry.id, count=plan["count"], from_slot=27, to_slot=0)

        results = restarted.verify(RESTORED)

        self.assertEqual([row["result"] for row in results], ["verified"])
        self.assertEqual(restarted.outstanding(), [])
        # 还清了就不再写在盘上（重启用的是"未归还"这份账）。
        self.assertEqual(BorrowLedger(ledger_path=self.path).outstanding(), [])

    def test_in_flight_return_is_not_resumed_as_already_returned(self):
        ledger = BorrowLedger(ledger_path=self.path)
        entry = self.borrow(ledger)
        ledger.begin_return(entry.id, count=8, from_slot=27, to_slot=0)

        restarted = BorrowLedger(ledger_path=self.path)
        row = restarted.outstanding()[0]

        self.assertEqual(row.state, "open", "重启把在途动作冲掉了 → 当作还没还")
        self.assertFalse(row.awaiting_snapshot)
        self.assertFalse(row.trip_sent)

    def test_unreturnable_borrow_is_reported_and_stays_reported_after_restart(self):
        ledger = BorrowLedger(ledger_path=self.path)
        # 容器不可用：连容器身份/坐标都没有（没打开过箱子，也不知道箱子在哪）。
        entry = ledger.record_borrow("iron_ingot", 8, reason="craft")

        ledger.mark_unreturnable(entry.id, "task_failed:no_container")

        self.assertEqual(ledger.outstanding(), [])
        report = ledger.pending_report()
        self.assertEqual((report[0]["state"], report[0]["remaining"]), ("failed", 8))
        self.assertEqual(report[0]["failure"], "task_failed:no_container")

        restarted = BorrowLedger(ledger_path=self.path)
        again = restarted.pending_report()

        self.assertEqual((again[0]["state"], again[0]["remaining"]), ("failed", 8))
        self.assertEqual(again[0]["failure"], "task_failed:no_container")
        self.assertEqual(restarted.outstanding(), [], "已判失败的不再进自动归还")

    def test_switching_partitions_swaps_the_debt_book(self):
        ledger = BorrowLedger(ledger_path=self.root / "rules" / "srv-a.ledger.json")
        self.borrow(ledger)

        ledger.use_ledger_path(self.root / "rules" / "srv-b.ledger.json")

        self.assertEqual(ledger.outstanding(), [], "别的分区的欠账不能带过来")
        ledger.use_ledger_path(self.root / "rules" / "srv-a.ledger.json")
        self.assertEqual(len(ledger.outstanding()), 1)

    def test_memory_only_ledger_still_works(self):
        ledger = BorrowLedger()
        self.borrow(ledger)

        self.assertEqual(len(ledger.outstanding()), 1)
        self.assertFalse((self.root / "rules").exists())


class SessionLedgerWiringTests(unittest.TestCase):
    """切 server 分区 → 台账跟着换文件、按盘恢复欠账（后端重启不赖账）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def session(self, *args, **kwargs) -> Session:
        session = Session.__new__(Session)
        session.runtime = {}
        session.conversation = SimpleNamespace(use_namespace=lambda *a: "")
        session.borrow_rules = BorrowRulesStore(root=self.root / "rules")
        session.permissions = PermissionsStore(self.root / "permissions.json")
        session.permissions.set({"masters": ["Steve"]})
        session.poi = SimpleNamespace(use_namespace=lambda *a: "", observe_state=lambda *a: 0)
        session.places = None
        session.people = None
        session._replay_layers = set()
        session.skills = RecordingSkills()
        session.planner = Planner(llm_service=None, memory=DummyMemory(), skills=session.skills,
                                  permissions=session.permissions,
                                  borrow_rules=session.borrow_rules)
        return session

    def state(self, server="play.example:25565"):
        return {"world": {"server": server, "world_name": "world-a"}}

    def test_debt_is_restored_when_the_borrow_ledger_is_rewired(self):
        first = self.session()
        first.handle_event("state_update", self.state())
        first.planner.borrow_ledger.record_borrow(
            "iron_ingot", 8, container=chest(), from_slot=0, to_slot=27,
            reason="craft", container_pos=(1.5, 64.0, 2.5),
            container_block="minecraft:chest")
        ledger_file = first.borrow_rules.ledger_path
        self.assertTrue(ledger_file.exists())

        # 重启：全新的 session 走到同一个 server 分区 → 欠账按盘恢复，继续自动归还。
        second = self.session()
        second.handle_event("state_update", self.state())

        outstanding = second.planner.borrow_ledger.outstanding()
        self.assertEqual(len(outstanding), 1)
        self.assertEqual((outstanding[0].item, outstanding[0].remaining), ("iron_ingot", 8))
        second.runtime["open_container"] = AFTER_BORROW
        self.assertTrue(second._auto_return_step(), "恢复后条件允许就继续走归还")
        self.assertEqual(second.skills.moves(), [("container_move", 27, 0, 8)])

    def test_chat_message_is_not_required_for_the_ledger(self):
        """台账恢复只依赖 server 分区，与聊天/权限无关（这里连 reply_gate 都没有）。"""
        session = self.session()
        session.permissions.set({"masters": []})

        session.handle_event("state_update", self.state())

        self.assertEqual(session.planner.borrow_ledger.outstanding(), [])


if __name__ == "__main__":
    unittest.main()
