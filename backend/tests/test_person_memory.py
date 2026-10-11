"""person 层记忆（跨服的「同一个人」稳定事实）。

用户口径："除非有一些**通用的记忆**，特别是 **uuid 识别出来的用户**，他是一个人的情况下，
那倒是**可以串一点点**。" —— 但必须有边界：只存稳定事实，**不**把服务器内的一次性上下文
（刚才聊了什么、在哪遇到的）提升上来。

覆盖：
  ① 同一个 uuid 在两个 server 都能读到他的 person 记忆（跨服可串一点点）；
  ② 同一 uuid 的一次性对话**不会**出现在 person 层（留在 server 层）——防串断言；
  ③ 同名不同 uuid 互不污染；
  ④ 没有 uuid 时按 server+name 落盘并 `degraded` 打标（不假装是同一个人）；
  ⑤ 非白名单键（chat/position/where/server…）一律写不进来；
  ⑥ world 层与 person 层互不影响。
"""

import tempfile
import unittest
from pathlib import Path

from agent.conversation import ConversationStore
from agent.person_memory import STABLE_FACT_KEYS, PersonMemoryStore
from agent.session import Session

STEVE = "069a79f4-44e9-4726-a5be-fca90e38aaf5"
ALEX = "853c80ef-3c37-49fd-aa49-938b674adae6"
SERVER_A = "play.example:25565"
SERVER_B = "play.other:25565"


class PersonStoreTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def store(self) -> PersonMemoryStore:
        return PersonMemoryStore(root=self.root / "people")

    # ── ① 跨服可串一点点 ──

    def test_same_uuid_is_readable_on_two_servers(self):
        store = self.store()
        store.identify(STEVE, "Steve", server_key=SERVER_A)
        ok, reason = store.remember_fact(STEVE, "Steve", "is_master", True,
                                         server_key=SERVER_A)
        self.assertTrue(ok, reason)

        # 换一台服：同一个 uuid 还是同一个人（person 层不按 server 分区）。
        self.assertEqual(store.facts(STEVE, "Steve", server_key=SERVER_B),
                         {"is_master": True})
        self.assertIn("is_master", store.describe(STEVE, "Steve", server_key=SERVER_B))

    def test_facts_survive_a_restart(self):
        self.store().remember_fact(STEVE, "Steve", "calls_me", "小助手", server_key=SERVER_A)

        reloaded = self.store()

        self.assertEqual(reloaded.facts(STEVE, "Steve"), {"calls_me": "小助手"})

    # ── ⑤ 边界：非稳定事实写不进来 ──

    def test_one_off_context_is_refused(self):
        store = self.store()
        for key in ("chat", "last_message", "position", "where", "server",
                    "dimension", "together", "seen_at"):
            ok, reason = store.remember_fact(STEVE, "Steve", key, "刚才聊的东西",
                                             server_key=SERVER_A)
            self.assertFalse(ok, f"{key} 不该能进 person 层")
            self.assertEqual(reason, "not_stable")

        self.assertEqual(store.facts(STEVE, "Steve"), {})

    def test_stable_keys_expand_only_through_the_whitelist(self):
        self.assertIn("is_master", STABLE_FACT_KEYS)
        self.assertNotIn("chat", STABLE_FACT_KEYS)
        self.assertNotIn("position", STABLE_FACT_KEYS)

    # ── ③ 同名不同人 ──

    def test_same_name_different_uuid_does_not_pollute(self):
        store = self.store()
        store.remember_fact(STEVE, "Steve", "is_master", True, server_key=SERVER_A)
        store.remember_fact(ALEX, "Steve", "role", "friend", server_key=SERVER_A)

        self.assertEqual(store.facts(STEVE, "Steve"), {"is_master": True})
        self.assertEqual(store.facts(ALEX, "Steve"), {"role": "friend"})
        self.assertNotEqual(store.path("uuid_" + STEVE), store.path("uuid_" + ALEX))

    # ── ④ 没有 uuid → 降级打标，且不跨服合并 ──

    def test_without_uuid_falls_back_to_server_and_name_with_a_degraded_mark(self):
        store = self.store()
        entry = store.identify("", "Steve", server_key=SERVER_A)

        self.assertTrue(entry["degraded"])
        self.assertTrue(entry["key"].startswith("name_"))
        store.remember_fact("", "Steve", "is_master", True, server_key=SERVER_A)

        self.assertEqual(store.facts("", "Steve", server_key=SERVER_A), {"is_master": True})
        # 降级键只在一台服里有效：换服读不到（同名不同人不能瞎合并）。
        self.assertEqual(store.facts("", "Steve", server_key=SERVER_B), {})

    def test_uuid_entries_are_not_degraded(self):
        entry = self.store().identify(STEVE, "Steve", server_key=SERVER_A)

        self.assertFalse(entry["degraded"])
        self.assertEqual(entry["uuid"], STEVE)


class SessionPersonWiringTests(unittest.TestCase):
    """会话接线：聊天 → person 层（只记身份）；在场玩家 → 读出稳定事实。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def session(self) -> Session:
        session = Session.__new__(Session)
        session.runtime = {"world": {"server": SERVER_A, "world_name": "world-a"}}
        session.people = PersonMemoryStore(root=self.root / "people")
        session.conversation = ConversationStore(root=self.root / "conversations")
        session.poi = None
        session._people_seen = {}
        session._present_person_facts = {}
        session._replay_layers = set()
        session.places = None
        return session

    def test_chat_records_identity_but_not_the_conversation(self):
        session = self.session()

        session._note_person({"sender": "Steve", "uuid": STEVE,
                              "message": "帮我在这里挖一条隧道到 300 格"})
        session._record_conversation_chat({"sender": "Steve", "uuid": STEVE,
                                           "message": "帮我在这里挖一条隧道到 300 格"})

        # person 层：认出了人，但聊天内容一个字都没进去。
        entry = session.people.entry(STEVE, "Steve")
        self.assertEqual(entry["uuid"], STEVE)
        self.assertEqual(entry["facts"], {})
        person_file = (self.root / "people" / f"uuid_{STEVE}.json").read_text(encoding="utf-8")
        self.assertNotIn("隧道", person_file)
        # server 层（对话记忆）里才有那条一次性上下文。
        chat_log = session.conversation.events_path.read_text(encoding="utf-8")
        self.assertIn("隧道", chat_log)

    def test_present_players_expose_their_stable_facts(self):
        session = self.session()
        session.people.remember_fact(STEVE, "Steve", "is_master", True, server_key=SERVER_A)
        session._note_person({"sender": "Steve", "uuid": STEVE, "message": "在吗"})

        session._absorb_people({"players": [{"name": "Steve", "distance": 3.0}]})

        self.assertEqual(session._present_person_facts, {"Steve": {"is_master": True}})

    def test_person_facts_are_not_scoped_to_the_world(self):
        session = self.session()
        session.people.remember_fact(STEVE, "Steve", "calls_me", "阿助", server_key=SERVER_A)
        session._note_person({"sender": "Steve", "uuid": STEVE, "message": "在吗"})

        session.runtime["world"] = {"server": SERVER_A, "world_name": "world-b"}
        session._absorb_people({"players": [{"name": "Steve", "distance": 1.0}]})

        self.assertEqual(session._present_person_facts, {"Steve": {"calls_me": "阿助"}})

    def test_uuid_heard_on_another_server_is_not_reused(self):
        """换服后同名的人不拿上一台服的 uuid 记录（名字兜底必须是降级键）。"""
        session = self.session()
        session._note_person({"sender": "Steve", "uuid": STEVE, "message": "在吗"})
        session.people.remember_fact(STEVE, "Steve", "is_master", True, server_key=SERVER_A)

        session.runtime["world"] = {"server": SERVER_B, "world_name": "world-a"}
        session._absorb_people({"players": [{"name": "Steve", "distance": 1.0}]})

        self.assertEqual(session._present_person_facts, {})


if __name__ == "__main__":
    unittest.main()
