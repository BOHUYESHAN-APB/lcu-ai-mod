"""TPA 两路径 + 分层策略 + 目标解析（第三块）。

用户口径：「tpa 需要在对话列表去点同意 accept」（有的服是聊天里可点，有的服是**弹窗**）。
路径 A 走白名单指令（不点鼠标）；路径 B 发 Java `click_screen_button {match}`。
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.command_gate import CommandGate, CommandsStore  # noqa: E402
from agent.permissions import PermissionsStore  # noqa: E402
from agent.player_directory import resolve_player  # noqa: E402
from agent.session import Session  # noqa: E402
from agent.tpa import (  # noqa: E402
    TPA_ACCEPT,
    TPA_ASK,
    TPA_DENY,
    TpaCoordinator,
    TpaDetector,
    decision_for,
    may_initiate,
    normalize_tpa_policy,
)

PLAYERS = [{"name": "Steve", "uuid": "uuid-steve"},
           {"name": "Alex", "uuid": "uuid-alex"},
           {"name": "Steve_Builder", "uuid": "uuid-builder"}]


class ResolvePlayerTests(unittest.TestCase):
    def test_exact_match_wins(self):
        result = resolve_player("Steve", PLAYERS)

        self.assertTrue(result["ok"])
        self.assertEqual(result["name"], "Steve")
        self.assertEqual(result["uuid"], "uuid-steve")
        self.assertEqual(result["match"], "exact")

    def test_case_insensitive_exact_match(self):
        self.assertEqual(resolve_player("  steve ", PLAYERS)["name"], "Steve")

    def test_unique_fuzzy_match_is_accepted(self):
        result = resolve_player("Alexx", PLAYERS)

        self.assertTrue(result["ok"])
        self.assertEqual(result["name"], "Alex")
        self.assertEqual(result["match"], "fuzzy")

    def test_ambiguity_is_refused_with_candidates(self):
        result = resolve_player("steve_", PLAYERS)

        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "ambiguous")
        self.assertIn("Steve", result["candidates"])
        self.assertIn("Steve_Builder", result["candidates"])

    def test_offline_player_is_refused(self):
        result = resolve_player("Herobrine", PLAYERS)

        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "not_found")
        self.assertEqual(result["candidates"], [])

    def test_empty_name_is_refused(self):
        self.assertEqual(resolve_player("   ", PLAYERS)["reason"], "empty")


class DetectorTests(unittest.TestCase):
    def setUp(self):
        self.detector = TpaDetector()

    def test_english_request(self):
        found = self.detector.detect_request("Steve wants to teleport to you")

        self.assertEqual(found["kind"], "request")
        self.assertEqual(found["player"], "Steve")

    def test_chinese_request(self):
        found = self.detector.detect_request("小明 想要传送到你这里")

        self.assertEqual(found["player"], "小明")

    def test_english_popup_title(self):
        self.assertEqual(self.detector.detect_request("Teleport request from Alex")["player"],
                         "Alex")

    def test_normal_chat_is_not_a_request(self):
        for text in ("你好", "我在挖矿", "刚才谁死了？", "Steve: 走不走"):
            self.assertIsNone(self.detector.detect_request(text), text)

    def test_accept_and_deny_triggers(self):
        self.assertEqual(self.detector.detect_intent("接受")["action"], "accept")
        self.assertEqual(self.detector.detect_intent("同意传送")["action"], "accept")
        self.assertEqual(self.detector.detect_intent("accept")["action"], "accept")
        self.assertEqual(self.detector.detect_intent("拒绝")["action"], "deny")
        self.assertEqual(self.detector.detect_intent("deny Steve")["action"], "deny")
        self.assertEqual(self.detector.detect_intent("deny Steve")["player"], "Steve")

    def test_intent_recognizes_nothing_else(self):
        for text in ("同意你的看法", "拒绝服务攻击是什么", "ok"):
            self.assertIsNone(self.detector.detect_intent(text), text)

    def test_result_broadcast(self):
        self.assertEqual(self.detector.classify_result("Alex accepted your teleport request")["outcome"],
                         "accepted")
        self.assertEqual(self.detector.classify_result("传送请求已被 Alex 拒绝")["outcome"], "denied")

    def test_user_extra_patterns_are_appended(self):
        detector = TpaDetector(config_provider=lambda: {
            "request_patterns": [r"^(?P<player>\S+) 想瞬移过来$"]})

        self.assertEqual(detector.detect_request("Bob 想瞬移过来")["player"], "Bob")
        self.assertEqual(detector.detect_request("Bob wants to teleport to you")["player"], "Bob")


class PolicyTests(unittest.TestCase):
    def test_defaults(self):
        policy = normalize_tpa_policy(None)

        self.assertEqual(policy["master"], TPA_ACCEPT)
        self.assertEqual(policy["friend"], TPA_ASK)
        self.assertEqual(policy["guest"], TPA_DENY)
        self.assertTrue(policy["outgoing"]["master"])
        self.assertFalse(policy["outgoing"]["friend"])

    def test_decision_lookup(self):
        policy = normalize_tpa_policy(None)

        self.assertEqual(decision_for(policy, "master"), TPA_ACCEPT)
        self.assertEqual(decision_for(policy, "friend"), TPA_ASK)
        self.assertEqual(decision_for(policy, "guest"), TPA_DENY)
        self.assertEqual(decision_for(policy, "unknown-tier"), TPA_DENY)

    def test_initiate_only_master_by_default(self):
        policy = normalize_tpa_policy(None)

        self.assertTrue(may_initiate(policy, "master"))
        self.assertFalse(may_initiate(policy, "friend"))
        self.assertFalse(may_initiate(policy, "guest"))

    def test_friend_can_be_configured(self):
        policy = normalize_tpa_policy({"friend": "accept", "outgoing": {"friend": True}})

        self.assertEqual(decision_for(policy, "friend"), TPA_ACCEPT)
        self.assertTrue(may_initiate(policy, "friend"))

    def test_permissions_store_exposes_tpa_policy(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = PermissionsStore(path=os.path.join(tmp.name, "permissions.json"))

        self.assertEqual(store.tpa_decision("master"), TPA_ACCEPT)
        self.assertEqual(store.tpa_decision("guest"), TPA_DENY)
        self.assertTrue(store.tpa_may_initiate("master"))
        self.assertFalse(store.tpa_may_initiate("guest"))

        saved = store.set({"tpa": {"friend": "accept", "path": "popup"}})
        self.assertEqual(saved["tpa"]["friend"], "accept")
        self.assertEqual(saved["tpa"]["path"], "popup")
        # 没写的键仍是默认（补全而不是清空）。
        self.assertEqual(saved["tpa"]["master"], TPA_ACCEPT)


class _Recorder:
    def __init__(self, result=None):
        self.calls = []
        self._result = result or {"ok": True}

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return dict(self._result)


class CoordinatorTests(unittest.TestCase):
    def _coordinator(self, policy=None, resolve=None, command=None, click=None):
        rows = []
        coordinator = TpaCoordinator(
            TpaDetector(), policy_provider=lambda: (policy or {}),
            command=command or _Recorder({"ok": True, "command": "/tpaccept Steve"}),
            click=click or _Recorder({"ok": True, "match": "接受"}),
            resolve=resolve or (lambda name: {"ok": True, "name": name, "uuid": "u1"}),
            audit=rows.append)
        return coordinator, rows

    def test_master_request_is_auto_accepted_via_chat_command(self):
        command = _Recorder({"ok": True, "command": "/tpaccept Steve"})
        click = _Recorder()
        coordinator, rows = self._coordinator(command=command, click=click)

        result = coordinator.handle_request("Steve wants to teleport to you", tier="master")

        self.assertEqual(result["action"], "accept")
        self.assertEqual(result["path"], "chat")
        self.assertEqual(command.calls[0][0], ("tpaccept", ["Steve"]))
        self.assertEqual(click.calls, [], "路径 A 不该点任何界面")
        self.assertEqual(rows[-1]["outcome"], "accepted")
        self.assertEqual(rows[-1]["tier"], "master")

    def test_popup_uses_click_screen_button_match(self):
        command = _Recorder()
        click = _Recorder({"ok": True, "match": "接受"})
        coordinator, rows = self._coordinator(command=command, click=click)

        result = coordinator.handle_request("Teleport request from Steve", tier="master",
                                            via="popup")

        self.assertEqual(result["path"], "popup")
        self.assertEqual(click.calls[0][0], ("接受",))
        self.assertEqual(command.calls, [], "路径 B 不该下服务端指令")

    def test_guest_request_is_refused_without_any_action(self):
        command = _Recorder()
        click = _Recorder()
        coordinator, rows = self._coordinator(command=command, click=click)

        result = coordinator.handle_request("Steve wants to teleport to you", tier="guest")

        self.assertEqual(result["action"], "refused")
        self.assertEqual(command.calls, [])
        self.assertEqual(click.calls, [], "guest 默认不去点")
        self.assertEqual(rows[-1]["outcome"], "refused")

    def test_friend_defaults_to_ask(self):
        command = _Recorder()
        coordinator, rows = self._coordinator(command=command)

        result = coordinator.handle_request("Steve wants to teleport to you", tier="friend")

        self.assertEqual(result["action"], "ask")
        self.assertEqual(command.calls, [])
        self.assertEqual(rows[-1]["outcome"], "ask")

    def test_explicit_deny_sends_deny_command(self):
        command = _Recorder({"ok": True, "command": "/tpdeny Steve"})
        coordinator, rows = self._coordinator(command=command)

        result = coordinator.respond("deny", player="Steve", tier="master")

        self.assertEqual(result["action"], "deny")
        self.assertEqual(command.calls[0][0], ("tpdeny", ["Steve"]))
        self.assertEqual(rows[-1]["outcome"], "denied")

    def test_explicit_deny_in_popup_clicks_deny_button(self):
        click = _Recorder({"ok": True, "match": "拒绝"})
        coordinator, _ = self._coordinator(click=click, policy={"path": "popup"})

        coordinator.respond("deny", player="Steve", tier="master")

        self.assertEqual(click.calls[0][0], ("拒绝",))

    def test_guest_cannot_force_an_accept(self):
        command = _Recorder()
        coordinator, _ = self._coordinator(command=command)

        result = coordinator.respond("accept", player="Steve", tier="guest")

        self.assertEqual(result["action"], "refused")
        self.assertEqual(command.calls, [])

    def test_unknown_action_is_not_guessed(self):
        coordinator, _ = self._coordinator()

        self.assertEqual(coordinator.respond("maybe", player="Steve", tier="master")["reason"],
                         "bad_action")

    def test_unresolvable_target_refuses_and_reports_candidates(self):
        command = _Recorder()
        resolve = lambda name: {"ok": False, "reason": "ambiguous",  # noqa: E731
                                "candidates": ["Steve", "Steve_Builder"]}
        coordinator, rows = self._coordinator(command=command, resolve=resolve)

        result = coordinator.handle_request("Steve wants to teleport to you", tier="master")

        self.assertEqual(result["action"], "none")
        self.assertEqual(result["candidates"], ["Steve", "Steve_Builder"])
        self.assertEqual(command.calls, [])
        self.assertEqual(rows[-1]["outcome"], "unresolved")

    def test_configured_path_overrides_event_source(self):
        command = _Recorder()
        coordinator, _ = self._coordinator(command=command, policy={"path": "chat"})

        result = coordinator.handle_request("Steve wants to teleport to you", tier="master",
                                            via="popup")

        self.assertEqual(result["path"], "chat")
        self.assertEqual(command.calls[0][0], ("tpaccept", ["Steve"]))

    def test_unrecognized_text_is_a_no_op(self):
        coordinator, rows = self._coordinator()

        self.assertEqual(coordinator.handle_request("你好啊", tier="master")["reason"],
                         "not_detected")
        self.assertEqual(rows, [])

    def test_initiate_requires_authorized_tier(self):
        command = _Recorder({"ok": True, "command": "/tpa Steve"})
        coordinator, rows = self._coordinator(command=command)

        ok = coordinator.initiate("Steve", tier="master")
        refused = coordinator.initiate("Steve", tier="guest")

        self.assertTrue(ok["ok"])
        self.assertEqual(command.calls[0][0], ("tpa", ["Steve"]))
        self.assertEqual(refused["reason"], "tier_not_allowed")
        self.assertEqual(len(command.calls), 1)
        self.assertEqual(rows[-1]["outcome"], "refused")


class _Skills:
    def __init__(self):
        self.commands = []
        self.clicks = []

    def run_command(self, command):
        self.commands.append(str(command or "").lstrip("/"))
        return {"success": True}

    def click_screen_button(self, match):
        self.clicks.append(match)
        return {"success": True}


class SessionTpaTests(unittest.TestCase):
    """session 侧入口：真·单一指令口（白名单） + 系统消息里的请求也能接住。"""

    def _session(self, whitelist=None):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        session = Session.__new__(Session)
        session.skills = _Skills()
        session.runtime = {"players": PLAYERS, "world": {"server": "srv", "world_name": "w"}}
        session._actor = {"name": "Owner", "tier": "master"}
        session.in_world = True
        session.ui_screen = ""
        session._chat_audit = []
        session._security_audit = []
        session._security_log_failed = True
        session._whisper_sent_this_turn = set()
        session._reply_whisper_to = ""
        session._last_tpa_request = None
        session.commands_store = CommandsStore(path=os.path.join(tmp.name, "commands.json"))
        session.commands_store.set({"global": whitelist or ["tpaccept", "tpdeny", "tpa"],
                                    "tpa": {}})
        session.command_gate = CommandGate(session.commands_store,
                                           audit=session._on_security_audit)
        session.permissions = PermissionsStore(path=os.path.join(tmp.name, "permissions.json"))
        session.permissions.set({"master": ["Owner"], "friends": ["Buddy"]})
        from agent.contacts import ContactStore
        session.contacts = ContactStore(root=os.path.join(tmp.name, "contacts"))
        session.tpa_detector = TpaDetector(config_provider=session.commands_store.tpa_config)
        session.tpa = TpaCoordinator(
            session.tpa_detector, policy_provider=session.permissions.tpa_policy,
            command=session.run_server_command, click=session._click_screen_button,
            resolve=session.resolve_player, audit=session._on_security_audit)
        return session

    def test_master_request_from_system_message_is_accepted(self):
        session = self._session()
        session.permissions.set({"master": ["Steve"]})

        handled = session._handle_tpa_text("Steve wants to teleport to you",
                                           sender="Steve", is_system=True)

        self.assertTrue(handled)
        self.assertEqual(session.skills.commands, ["tpaccept Steve"])
        self.assertEqual(session._last_tpa_request["player"], "Steve")

    def test_guest_request_from_system_message_sends_nothing(self):
        session = self._session()

        handled = session._handle_tpa_text("Steve wants to teleport to you",
                                           sender="Steve", is_system=True)

        self.assertTrue(handled)
        self.assertEqual(session.skills.commands, [])
        self.assertEqual(session.skills.clicks, [])

    def test_friend_request_asks_instead_of_acting(self):
        session = self._session()
        session.permissions.set({"friends": ["Steve"]})
        asked = []
        session.inject_event = lambda kind, intent, **kw: asked.append((kind, intent)) or True

        handled = session._handle_tpa_text("Steve wants to teleport to you",
                                           sender="Steve", is_system=True)

        self.assertTrue(handled)
        self.assertEqual(session.skills.commands, [])
        self.assertEqual(asked[0][0], "tpa_request")

    def test_speaking_accept_with_a_name_goes_through_the_gate(self):
        session = self._session()
        session.permissions.set({"master": ["Owner"]})

        handled = session._handle_tpa_text("accept Steve", sender="Owner", is_system=False)

        self.assertTrue(handled)
        self.assertEqual(session.skills.commands, ["tpaccept Steve"])

    def test_system_messages_cannot_trigger_accept_words(self):
        session = self._session()

        self.assertFalse(session._handle_tpa_text("accept Steve", sender="Steve",
                                                  is_system=True))
        self.assertEqual(session.skills.commands, [])

    def test_accept_words_without_a_pending_request_are_just_chat(self):
        session = self._session()

        self.assertFalse(session._handle_tpa_text("接受", sender="Owner", is_system=False))

    def test_whitelist_still_wins(self):
        session = self._session(whitelist=["tpa"])
        session.permissions.set({"master": ["Steve"]})

        handled = session._handle_tpa_text("Steve wants to teleport to you",
                                           sender="Steve", is_system=True)

        self.assertTrue(handled)
        self.assertEqual(session.skills.commands, [], "没勾选 tpaccept 就不许发")

    def test_request_tpa_respects_tier(self):
        session = self._session()

        allowed = session.request_tpa("Steve")
        session._actor = {"name": "Guest", "tier": "guest"}
        refused = session.request_tpa("Steve")

        self.assertTrue(allowed["ok"])
        self.assertEqual(refused["reason"], "tier_not_allowed")
        self.assertEqual(session.skills.commands, ["tpa Steve"])


if __name__ == "__main__":
    unittest.main()
