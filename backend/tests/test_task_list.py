"""任务列表（M3-19 §6）的回归测试。

覆盖：入队/去重/优先级/进度夹取、排序、权限、插队、完成与失败原因、touch 上报、
prune 回收、以及 `/api/status` 快照必须可 JSON 序列化。
"""

import json
import unittest

from agent.task_list import (STATUS_ACTIVE, STATUS_CANCELLED, STATUS_DONE,
                             STATUS_FAILED, STATUS_PENDING, TERMINAL_STATUSES,
                             TaskItem, TaskList)


class Clock:
    """可控时钟：每次调用 +1，保证 created_at/updated_at 严格递增，排序可断言。"""

    def __init__(self, start: float = 1000.0):
        self.t = start

    def __call__(self) -> float:
        self.t += 1.0
        return self.t


class AddTests(unittest.TestCase):
    def test_default_priority_by_tier(self):
        tl = TaskList(clock=Clock())
        self.assertEqual(tl.add("mine", owner="a", owner_tier="master").priority, 80)
        self.assertEqual(tl.add("build", owner="b", owner_tier="friend").priority, 50)
        self.assertEqual(tl.add("look", owner="c", owner_tier="guest").priority, 20)

    def test_explicit_priority_and_progress_clamped(self):
        tl = TaskList(clock=Clock())
        item = tl.add("mine", owner="a", owner_tier="guest", priority=999, progress=3.0)
        self.assertEqual(item.priority, 100)
        self.assertEqual(item.progress, 1.0)
        item2 = tl.add("t2", owner="a", priority=-5, progress=-1.0)
        self.assertEqual(item2.priority, 0)
        self.assertEqual(item2.progress, 0.0)

    def test_default_ids_increment(self):
        tl = TaskList(clock=Clock())
        self.assertEqual(tl.add("a").id, "task_1")
        self.assertEqual(tl.add("b").id, "task_2")

    def test_dedupe_active_same_kind_and_owner(self):
        tl = TaskList(clock=Clock())
        first = tl.add("mine", owner="steve", detail="挖钻石")
        again = tl.add("mine", owner="steve", detail="重复上报")
        self.assertIs(first, again)
        self.assertEqual(len(tl.items()), 1)
        # 不同 owner / 不同 kind 各自独立。
        tl.add("mine", owner="alex")
        tl.add("build", owner="steve")
        self.assertEqual(len(tl.items()), 3)

    def test_dedupe_releases_after_terminal(self):
        tl = TaskList(clock=Clock())
        first = tl.add("mine", owner="steve")
        tl.complete(first.id)
        second = tl.add("mine", owner="steve")
        self.assertIsNot(first, second)
        self.assertEqual(len(tl.items()), 2)


class SortTests(unittest.TestCase):
    def test_active_first_then_priority_then_created(self):
        tl = TaskList(clock=Clock())
        low = tl.add("low", owner="a", priority=10)
        high = tl.add("high", owner="b", priority=90)
        act = tl.add("act", owner="c", priority=1, status=STATUS_ACTIVE)
        done = tl.add("done", owner="d", priority=100)
        tl.complete(done.id)
        order = [i.kind for i in tl.items()]
        self.assertEqual(order, ["act", "high", "low", "done"])

    def test_limit(self):
        tl = TaskList(clock=Clock())
        for n in range(5):
            tl.add(f"k{n}", owner=f"o{n}")
        self.assertEqual(len(tl.items(limit=2)), 2)


class PermissionTests(unittest.TestCase):
    def setUp(self):
        self.tl = TaskList(clock=Clock())
        self.item = self.tl.add("mine", owner="steve", owner_tier="friend")

    def test_master_manages_anything(self):
        self.assertTrue(TaskList.can_manage(self.item, actor_tier="master", actor="admin"))
        ok, why = self.tl.cancel(self.item.id, actor_tier="master", actor="admin")
        self.assertTrue(ok)
        self.assertEqual(why, "cancelled")

    def test_friend_only_own(self):
        self.assertTrue(TaskList.can_manage(self.item, actor_tier="friend", actor="steve"))
        other = self.tl.add("other", owner="alex", owner_tier="friend")
        self.assertFalse(TaskList.can_manage(other, actor_tier="friend", actor="steve"))
        ok, why = self.tl.cancel(other.id, actor_tier="friend", actor="steve")
        self.assertFalse(ok)
        self.assertEqual(why, "denied")

    def test_friend_may_manage_ownerless_friend_task(self):
        orphan = self.tl.add("chore", owner="", owner_tier="friend")
        self.assertTrue(TaskList.can_manage(orphan, actor_tier="friend", actor="steve"))

    def test_guest_denied(self):
        ok, why = self.tl.cancel(self.item.id, actor_tier="guest", actor="steve")
        self.assertFalse(ok)
        self.assertEqual(why, "denied")

    def test_missing_and_already_terminal(self):
        ok, why = self.tl.cancel("nope", actor_tier="master", actor="admin")
        self.assertEqual((ok, why), (False, "not_found"))
        self.tl.complete(self.item.id)
        ok, why = self.tl.cancel(self.item.id, actor_tier="master", actor="admin")
        self.assertEqual((ok, why), (False, "already_terminal"))


class ActivateTests(unittest.TestCase):
    def test_activate_jumps_queue_and_demotes_others(self):
        tl = TaskList(clock=Clock())
        a = tl.add("a", owner="o", priority=30, status=STATUS_ACTIVE)
        b = tl.add("b", owner="o2", priority=90, status=STATUS_ACTIVE)
        target = tl.add("urgent", owner="steve", owner_tier="friend", priority=5)
        ok, why = tl.activate(target.id, actor_tier="friend", actor="steve")
        self.assertTrue(ok)
        self.assertEqual(why, "activated")
        self.assertEqual(target.status, STATUS_ACTIVE)
        self.assertIs(tl.active(), target)
        self.assertEqual(target.priority, 100)  # 抬到最高 +1 后夹到 100
        self.assertEqual(a.status, STATUS_PENDING)
        self.assertEqual(b.status, STATUS_PENDING)
        self.assertEqual(tl.active().kind, "urgent")

    def test_activate_denied_for_guest(self):
        tl = TaskList(clock=Clock())
        t = tl.add("a", owner="steve", owner_tier="friend")
        self.assertEqual(tl.activate(t.id, actor_tier="guest"), (False, "denied"))
        self.assertNotEqual(t.status, STATUS_ACTIVE)


class CompleteTests(unittest.TestCase):
    def test_done_sets_progress_one(self):
        tl = TaskList(clock=Clock())
        t = tl.add("a", owner="o", progress=0.4)
        tl.complete(t.id)
        self.assertEqual(t.status, STATUS_DONE)
        self.assertEqual(t.progress, 1.0)

    def test_failure_reason_readable(self):
        tl = TaskList(clock=Clock())
        t = tl.add("mine", owner="o")
        tl.complete(t.id, success=False, reason="stuck: 找不到目标方块")
        self.assertEqual(t.status, STATUS_FAILED)
        self.assertEqual(t.failure_reason, "stuck: 找不到目标方块")
        self.assertIn("stuck", t.to_dict()["failure_reason"])

    def test_complete_missing(self):
        self.assertIsNone(TaskList(clock=Clock()).complete("nope"))


class UpdateTests(unittest.TestCase):
    def test_progress_clamp_and_status_terminal(self):
        tl = TaskList(clock=Clock())
        t = tl.add("a")
        tl.update(t.id, progress=5.0)
        self.assertEqual(t.progress, 1.0)
        tl.update(t.id, status=STATUS_CANCELLED, failure_reason="用户取消")
        self.assertEqual(t.status, STATUS_CANCELLED)
        self.assertEqual(t.failure_reason, "用户取消")

    def test_unknown_field_ignored(self):
        tl = TaskList(clock=Clock())
        t = tl.add("a")
        tl.update(t.id, bogus="x")
        self.assertFalse(hasattr(t, "bogus"))


class TouchTests(unittest.TestCase):
    def test_touch_updates_existing(self):
        tl = TaskList(clock=Clock())
        t = tl.add("mine", owner="steve", detail="开始挖")
        out = tl.touch("mine", owner="steve", progress=0.5)
        self.assertIs(out, t)
        self.assertEqual(t.progress, 0.5)
        self.assertEqual(t.detail, "开始挖")  # 未提供 detail 不覆盖

    def test_touch_creates_when_missing(self):
        tl = TaskList(clock=Clock())
        out = tl.touch("swim", owner="steve", detail="向上游", progress=0.1)
        self.assertIsNotNone(out)
        self.assertEqual(out.kind, "swim")
        self.assertEqual(out.detail, "向上游")
        self.assertEqual(len(tl.items()), 1)

    def test_touch_reports_failure(self):
        tl = TaskList(clock=Clock())
        t = tl.add("mine", owner="steve")
        tl.touch("mine", owner="steve", status=STATUS_FAILED, failure_reason="stuck: 卡墙")
        self.assertEqual(t.status, STATUS_FAILED)
        self.assertEqual(t.failure_reason, "stuck: 卡墙")


class PruneTests(unittest.TestCase):
    def test_prune_only_terminal(self):
        tl = TaskList(clock=Clock())
        for n in range(3):
            t = tl.add(f"term{n}", owner=f"o{n}")
            tl.complete(t.id)
        live = tl.add("live", owner="live")
        removed = tl.prune(keep=1)
        self.assertEqual(removed, 2)
        ids = {i.id for i in tl.items()}
        self.assertIn(live.id, ids)
        self.assertEqual(sum(1 for i in tl.items() if i.status in TERMINAL_STATUSES), 1)

    def test_prune_keeps_most_recent(self):
        tl = TaskList(clock=Clock())
        first = tl.add("a", owner="o")
        tl.complete(first.id)
        last = tl.add("b", owner="o2")
        tl.complete(last.id)
        tl.prune(keep=1)
        self.assertIsNone(tl.get(first.id))
        self.assertIsNotNone(tl.get(last.id))


class SnapshotTests(unittest.TestCase):
    def test_status_is_json_serializable(self):
        tl = TaskList(clock=Clock())
        act = tl.add("a", owner="steve", owner_tier="friend", priority=50,
                     detail="挖矿", status=STATUS_ACTIVE, progress=0.3)
        t = tl.add("b", owner="alex")
        tl.complete(t.id, success=False, reason="stuck: 超时")
        snap = tl.status()
        self.assertEqual(snap["count"], 2)
        self.assertEqual(snap["pending"], 0)
        self.assertEqual(snap["failed"], 1)
        self.assertEqual(snap["active"]["id"], act.id)
        # 关键：主循环会把它塞进 /api/status 的 JSON 响应。
        dumped = json.dumps(snap, ensure_ascii=False)
        self.assertIn("stuck: 超时", dumped)
        self.assertEqual(set(snap["items"][0]), set(TaskItem("", "").to_dict()))

    def test_status_active_none(self):
        tl = TaskList(clock=Clock())
        tl.add("a")
        self.assertIsNone(tl.status()["active"])

    def test_empty_status(self):
        snap = TaskList(clock=Clock()).status()
        self.assertEqual(snap, {"items": [], "active": None, "count": 0,
                                "pending": 0, "failed": 0})


if __name__ == "__main__":
    unittest.main()
