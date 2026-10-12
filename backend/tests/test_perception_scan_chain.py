"""「需要的时候再取坐标」那条链的**闭环**回归：扫描回执 → 候选 → 动手。

用户口径（原话）："有些东西需要的时候再获取坐标，不需要的情况下默认当无关信息屏蔽掉。"

所以默认快照不带被动生物的坐标，但**取回来之后必须真的能用**：`scan_entities`（只读、有界）
拿回带坐标的行 → 这些行落进 planner → `find_entity` 看得见、`use_on` 对得上目标。
本文件锁死四件事：

  ① 只有**按 req_id 配对上的**那条回执里的行会被留下，别家的 `command_response` 不许污染；
  ② 留下之后 `find_entity` 能找到摘要里**没有坐标**的档位（例如一只牛），坐标逐字来自模组；
  ③ `use_on` 能真的对上那只（走 `use_on_entity(id)`），而**没扫之前如实失败**（`no_target`）；
  ④ 重复扫描是**替换**不是堆积，且有条数上限。
"""

import unittest

from agent.planner import Planner
from agent.use_on import REACH

SCAN_REQ = "req-scan-1"
COW = {"id": 7, "name": "牛", "type": "passive", "tier": "PASSIVE",
       "x": 851.0, "y": 63.0, "z": -2189.0, "distance": 2.5, "health": 10.0}


def entity_summary() -> dict:
    """分级摘要：只有威胁/玩家带坐标，被动生物**只有计数**（用户要的默认屏蔽）。"""
    return {
        "count": 13,
        "by_kind": {"dangerous": 0, "hostile": 1, "neutral": 0, "passive": 9,
                    "player": 1, "item": 0, "unknown": 0},
        "nearest": {"distance": 4.0, "name": "BoHuYeShan", "type": "player", "health": 20.0},
        "threats": [{"name": "僵尸", "type": "hostile", "tier": "HOSTILE",
                     "x": 853.0, "y": 63.0, "z": -2190.0, "distance": 8.1}],
        "players": [{"name": "BoHuYeShan", "x": 850.0, "y": 63.0, "z": -2190.0,
                     "distance": 4.0, "health": 20.0}],
        "items": {"count": 0, "nearest": None},
    }


def summary_state() -> dict:
    return {"entities": entity_summary(),
            "nearby_blocks": {"count": 0, "by_block": {}, "nearest": None}}


class DummyMemory:
    def build_context(self):
        return {}

    def get_recent_context(self, _count=5):
        return []


class RecordingSkills:
    """记下发出去的每一帧（`scan_entities` 走 `_send_cmd`，`use_on_entity` 走原语）。"""

    def __init__(self, req_id=SCAN_REQ):
        self.calls = []
        self.req_id = req_id

    def __getattr__(self, item):
        def recorder(*args, **kwargs):
            self.calls.append((item, args, kwargs))
            return {"success": True, "req_id": self.req_id}
        return recorder

    def sent(self, name):
        return [call for call in self.calls if call[0] == name]


def make_planner(skills=None) -> Planner:
    return Planner(llm_service=None, memory=DummyMemory(),
                   skills=skills if skills is not None else RecordingSkills())


def scan_receipt(rows, *, req_id=SCAN_REQ, ok=True) -> dict:
    return {"id": req_id, "ok": ok, "radius": 16, "kind": "passive",
            "total": len(rows), "returned": len(rows), "truncated": False,
            "entities": list(rows)}


class ScanChainTests(unittest.TestCase):

    def _scanned(self, rows=None):
        planner = make_planner()
        planner._dispatch_scan(16, "passive", 48)
        self.assertTrue(planner.absorb_command_response(scan_receipt(rows or [dict(COW)])))
        return planner

    # ① 只有配对上的回执算数

    def test_an_unrelated_receipt_never_lands_as_entity_rows(self):
        planner = self._scanned()
        before = list(planner._scan_rows)
        self.assertFalse(planner.absorb_command_response(
            {"id": "some-other-request", "ok": True,
             "entities": [{"id": 99, "name": "假牛", "x": 0, "y": 0, "z": 0}]}))
        self.assertEqual(planner._scan_rows, before)

    def test_a_scan_that_failed_leaves_no_rows(self):
        planner = make_planner()
        planner._dispatch_scan(16, "all", 48)
        planner.absorb_command_response({"id": SCAN_REQ, "ok": False, "entities": [dict(COW)]})
        self.assertEqual(planner._scan_rows, [])

    # ② 扫描行让摘要里没有坐标的档位变得看得见

    def test_find_entity_sees_the_scanned_mob(self):
        planner = self._scanned()
        planner._tool_find_entity({"kind": "牛"}, summary_state())
        self.assertIn("牛", planner._action_note)

    def test_without_a_scan_the_snapshot_alone_cannot_see_it(self):
        # 摘要里只有计数、没有坐标 ⇒ 光靠快照**找不到**那只牛（也不许编出坐标来）
        from agent import perception
        from agent.entities import find as find_entities
        view = perception.entities_rows(summary_state())
        result = find_entities("牛", entities=view["rows"], partial=view["partial"])
        self.assertEqual(result.get("candidates"), [])
        self.assertTrue(view["partial"])
        self.assertIn("按需", str(result.get("note") or ""))      # 如实说"要细节得按需扫描"

    # ③ use_on 真的对得上；没扫之前如实失败

    def test_use_on_resolves_the_scanned_mob_by_its_id(self):
        skills = RecordingSkills()
        planner = make_planner(skills)
        planner._dispatch_scan(16, "passive", 48)
        planner.absorb_command_response(scan_receipt([dict(COW)]))
        self.assertTrue(planner._tool_use_on({"target": "牛"}, summary_state()))
        self.assertEqual(skills.sent("use_on_entity")[0][1], (7,))

    def test_use_on_fails_honestly_before_the_scan_and_points_at_it(self):
        planner = make_planner()
        self.assertFalse(planner._tool_use_on({"target": "牛"}, summary_state()))
        note = planner._action_note
        self.assertIn("no_target", note)
        self.assertIn("scan_entities", note)     # 指到真正的出口（唯一的坐标出口）
        self.assertIn("不等于", note)            # "摘要里没有" != "附近没有"

    def test_a_scanned_mob_far_away_is_refused_for_reach_not_for_missing(self):
        far = dict(COW, distance=REACH + 5)
        planner = self._scanned([far])
        self.assertFalse(planner._tool_use_on({"target": "牛"}, summary_state()))
        self.assertIn("out_of_range", planner._action_note)

    # ④ 替换而不是堆积 + 上限

    def test_a_second_scan_replaces_the_first(self):
        planner = self._scanned()
        planner._dispatch_scan(16, "all", 48)
        other = {"id": 12, "name": "羊", "type": "passive", "x": 1.0, "y": 64.0, "z": 1.0,
                 "distance": 1.0}
        planner.absorb_command_response(scan_receipt([other]))
        self.assertEqual([row.get("name") for row in planner._scan_rows], ["羊"])

    def test_the_kept_rows_are_bounded(self):
        rows = [dict(COW, id=1000 + index, distance=1.0 + index) for index in range(200)]
        planner = self._scanned(rows)
        self.assertEqual(len(planner._scan_rows), Planner._SCAN_ROW_CAP)

    def test_snapshot_and_scan_rows_are_merged_without_duplicates(self):
        planner = self._scanned()
        zombie = {"id": 9, "name": "僵尸", "type": "hostile", "tier": "HOSTILE",
                  "x": 853.0, "y": 63.0, "z": -2190.0, "distance": 8.1}
        planner._dispatch_scan(16, "all", 48)
        planner.absorb_command_response(scan_receipt([zombie, dict(COW)]))
        rows = planner._entity_rows_for_tools(summary_state())
        ids = [row.get("id") for row in rows]
        self.assertEqual(ids.count(9), 1)         # 快照里的僵尸 + 扫描里的同一只 ⇒ 只留一份
        self.assertIn(7, ids)


if __name__ == "__main__":
    unittest.main()
