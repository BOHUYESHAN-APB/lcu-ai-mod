"""指令闸门（第二块）：白名单（唯一来源，默认空）+ 硬黑名单 + 高风险二次确认。

用户口径：「有些指令**绝对不可以**用：kill、kick 之类」；白名单没勾选的一律拒。
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.command_gate import (  # noqa: E402
    HARD_BLACKLIST,
    HIGH_RISK,
    CommandGate,
    CommandsStore,
    normalize_commands_config,
)
from agent.session import Session  # noqa: E402

_COMMANDS_WITH_WHISPER = {
    "global": ["tpaccept", "tpdeny", "msg"],
    "whisper_family": "msg",
}


class _Wire:
    def __init__(self):
        self.frames = []

    def send_command(self, cmd, args):
        self.frames.append((cmd, dict(args)))
        return "req"


class _Skills:
    """最小 skills 替身：只记 run_command 的字符串（按真 Skills 的口径先剥前导 `/`）。"""

    def __init__(self):
        self.commands = []

    def run_command(self, command):
        cleaned = str(command or "").lstrip("/")
        self.commands.append(cleaned)
        return {"success": True, "message": "sent", "req_id": "req"}


def _issue(gate, tool, args):
    """走一次高风险首调用，拿到一次性确认标识（测试辅助）。"""
    result = gate.evaluate(tool, args, tier="master")
    assert result.get("blocked") == "needs_confirm", result
    return result["confirm"]


class _StoreMixin(unittest.TestCase):
    def _store(self, payload=None, **kwargs):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        path = os.path.join(self._tmp.name, "commands.json")
        store = CommandsStore(path=path)
        if payload is not None:
            store.set(payload)
        return store


class WhitelistTests(_StoreMixin):
    def test_default_whitelist_is_empty_and_everything_is_refused(self):
        store = self._store()
        gate = CommandGate(store)

        for tool in ("msg", "tpaccept", "home", "locate"):
            result = gate.evaluate(tool, ["Steve"])
            self.assertFalse(result["ok"], tool)
            self.assertEqual(result["blocked"], "not_whitelisted", tool)
        self.assertEqual(store.get()["global"], [])

    def test_whitelisted_command_is_allowed_and_built(self):
        store = self._store({"global": ["tpaccept"]})
        gate = CommandGate(store)

        result = gate.evaluate("tpaccept", ["Alice"], tier="master")

        self.assertTrue(result["ok"])
        self.assertEqual(result["command"], "/tpaccept Alice")
        self.assertEqual(result["family"], "tpaccept")

    def test_server_specific_whitelist_only_applies_to_that_server(self):
        store = self._store({"global": [], "servers": {"srvA": ["home"]}})
        gate = CommandGate(store)

        self.assertTrue(gate.evaluate("home", [], server_key="srvA", tier="master")["ok"])
        blocked = gate.evaluate("home", [], server_key="srvB")
        self.assertFalse(blocked["ok"])
        self.assertEqual(blocked["blocked"], "not_whitelisted")
        self.assertIn("home", store.whitelist_for("srva"), "服名比较应大小写不敏感")

    def test_disabled_switch_refuses_everything(self):
        store = self._store({"enabled": False, "global": ["tpaccept"]})
        gate = CommandGate(store)

        result = gate.evaluate("tpaccept", ["Alice"], tier="master")
        self.assertFalse(result["ok"])
        self.assertEqual(result["blocked"], "disabled")

    def test_illegal_tokens_are_refused(self):
        store = self._store({"global": ["msg", "tp"]})
        gate = CommandGate(store)

        for args in (["a b"], ["@a"], ["x;y"], ["a\nb"], [{"nested": 1}]):
            result = gate.evaluate("msg", args, tier="master")
            self.assertFalse(result["ok"], args)
            self.assertEqual(result["blocked"], "invalid", args)

    def test_relative_coordinates_are_allowed_but_nbt_is_not(self):
        store = self._store({"global": ["setblock", "summon"]})
        gate = CommandGate(store)

        coords = gate.evaluate("setblock", ["~", "~1", "~-2", "minecraft:stone"],
                               tier="master",
                               confirm=_issue(gate, "setblock",
                                              ["~", "~1", "~-2", "minecraft:stone"]))
        self.assertTrue(coords["ok"])
        self.assertEqual(coords["command"], "/setblock ~ ~1 ~-2 minecraft:stone")

        nbt = gate.evaluate("summon", ["zombie", "~", "~", "~", "{NoAI:1b}"], tier="master")
        self.assertEqual(nbt["blocked"], "invalid")

    def test_empty_tool_is_refused(self):
        gate = CommandGate(self._store())
        self.assertEqual(gate.evaluate("", []), {"ok": False, "blocked": "empty",
                                                 "reason": "empty"})


class HardBlacklistTests(_StoreMixin):
    def test_blacklisted_entries_cannot_be_whitelisted_by_config(self):
        store = self._store({"global": ["kill", "kick", "tpaccept"]})

        self.assertEqual(store.get()["global"], ["tpaccept"], "黑名单条目必须被剔掉")
        self.assertFalse(store.is_whitelisted("kill"))

    def test_blacklisted_command_is_refused_even_if_whitelisted_in_memory(self):
        store = self._store({"global": ["tpaccept"]})
        # 直接往里塞（模拟手改内存/文件被绕过）：判定仍然必须拒。
        store._data["global"] = ["kill", "tpaccept"]
        gate = CommandGate(store)

        result = gate.evaluate("kill", ["Steve"])
        self.assertFalse(result["ok"])
        self.assertEqual(result["blocked"], "hard_blacklist")

    def test_namespaced_blacklist_is_caught(self):
        gate = CommandGate(self._store({"global": ["minecraft:kill"]}))

        result = gate.evaluate("minecraft:kill", ["Steve"])
        self.assertEqual(result["blocked"], "hard_blacklist")

    def test_hard_blacklist_covers_the_named_dangerous_commands(self):
        for name in ("kill", "kick", "ban", "ban-ip", "pardon", "op", "deop", "stop",
                     "restart", "whitelist", "difficulty", "gamerule", "save-off", "save-all"):
            self.assertIn(name, HARD_BLACKLIST)


class HighRiskConfirmTests(_StoreMixin):
    def _gate(self, clock=None):
        store = self._store({"global": ["tp", "give", "tpaccept"]})
        return CommandGate(store, clock=clock or __import__("time").time), store

    def test_first_call_only_asks_for_confirmation(self):
        gate, _ = self._gate()

        first = gate.evaluate("give", ["Steve", "diamond"], tier="master")

        self.assertFalse(first["ok"])
        self.assertEqual(first["blocked"], "needs_confirm")
        self.assertTrue(first["confirm"])

    def test_confirmed_call_executes_once(self):
        gate, _ = self._gate()
        first = gate.evaluate("tp", ["Steve"], tier="master")

        second = gate.evaluate("tp", ["Steve"], tier="master", confirm=first["confirm"])
        self.assertTrue(second["ok"])
        self.assertEqual(second["command"], "/tp Steve")

        third = gate.evaluate("tp", ["Steve"], tier="master", confirm=first["confirm"])
        self.assertEqual(third["blocked"], "needs_confirm", "确认标识必须是一次性的")

    def test_confirm_does_not_carry_to_another_command(self):
        gate, _ = self._gate()
        first = gate.evaluate("tp", ["Steve"], tier="master")

        other = gate.evaluate("give", ["Steve", "diamond"], tier="master",
                              confirm=first["confirm"])
        self.assertEqual(other["blocked"], "needs_confirm")

    def test_expired_confirm_is_useless(self):
        now = {"t": 1000.0}
        gate, _ = self._gate(clock=lambda: now["t"])
        first = gate.evaluate("tp", ["Steve"], tier="master")

        now["t"] += 10_000.0
        again = gate.evaluate("tp", ["Steve"], tier="master", confirm=first["confirm"])
        self.assertEqual(again["blocked"], "needs_confirm")

    def test_non_high_risk_command_needs_no_confirm(self):
        gate, _ = self._gate()
        self.assertTrue(gate.evaluate("tpaccept", ["Steve"], tier="master")["ok"])

    def test_high_risk_set_matches_the_user_list(self):
        for name in ("setblock", "fill", "clone", "summon", "give", "tp", "effect",
                     "time", "weather"):
            self.assertIn(name, HIGH_RISK)


class FreeTextTests(_StoreMixin):
    def test_whisper_free_text_allows_spaces(self):
        store = self._store({"global": ["msg"]})
        gate = CommandGate(store)

        result = gate.evaluate("msg", ["Steve"], tier="master", free_text="hi there friend")

        self.assertTrue(result["ok"])
        self.assertEqual(result["command"], "/msg Steve hi there friend")

    def test_newlines_in_free_text_are_refused(self):
        store = self._store({"global": ["msg"]})
        gate = CommandGate(store)

        result = gate.evaluate("msg", ["Steve"], tier="master", free_text="hi\ntp Alice")

        self.assertFalse(result["ok"])
        self.assertEqual(result["blocked"], "invalid_text")

    def test_free_text_only_for_whisper_families(self):
        store = self._store({"global": ["home"]})
        gate = CommandGate(store)

        result = gate.evaluate("home", ["base"], tier="master", free_text="hi there")

        self.assertFalse(result["ok"])
        self.assertEqual(result["blocked"], "invalid_text")


class AuditTests(_StoreMixin):
    def test_both_ends_of_the_confirm_flow_are_audited(self):
        rows = []
        store = self._store({"global": ["give"]})
        gate = CommandGate(store, audit=rows.append)

        first = gate.evaluate("give", ["Steve", "diamond"], actor="AI", tier="master")
        gate.evaluate("give", ["Steve", "diamond"], actor="AI", tier="master",
                      confirm=first["confirm"])

        outcomes = [row["outcome"] for row in rows]
        self.assertEqual(outcomes, ["needs_confirm", "confirmed", "allowed"])
        self.assertEqual(rows[0]["actor"], "AI")
        self.assertEqual(rows[0]["tier"], "master")

    def test_refusals_record_the_blocked_reason(self):
        rows = []
        gate = CommandGate(self._store(), audit=rows.append)

        gate.evaluate("msg", ["Steve"], tier="master")

        self.assertEqual(rows[-1]["outcome"], "blocked")
        self.assertEqual(rows[-1]["blocked"], "not_whitelisted")

    def test_gate_audit_log_is_readable(self):
        gate = CommandGate(self._store({"global": ["tpaccept"]}))
        gate.evaluate("tpaccept", ["Steve"], tier="master")

        self.assertEqual(gate.audit_log(1)[0]["family"], "tpaccept")


class ConfigNormalizationTests(unittest.TestCase):
    def test_config_is_tolerant(self):
        normalized = normalize_commands_config("nonsense")

        self.assertEqual(normalized["global"], [])
        self.assertTrue(normalized["enabled"])
        self.assertEqual(normalized["whisper_family"], "msg")

    def test_whisper_family_falls_back_to_msg(self):
        self.assertEqual(normalize_commands_config({"whisper_family": "nope"})["whisper_family"],
                         "msg")
        for family in ("msg", "tell", "w"):
            self.assertEqual(normalize_commands_config({"whisper_family": family})["whisper_family"],
                             family)

    def test_unknown_keys_are_ignored_on_write(self):
        store = CommandsStore(path=os.path.join(tempfile.mkdtemp(), "commands.json"))

        saved = store.set({"global": ["tpaccept"], "bogus": 1, "kill_everything": True})

        self.assertNotIn("bogus", saved)
        self.assertNotIn("kill_everything", saved)


class CommandEntryTests(_StoreMixin):
    """设计文档 `docs/dev/SERVER_COMMANDS.md` 的条目形状：tiers / cooldown / 用法说明。"""

    def test_entries_round_trip(self):
        store = self._store({"commands": [
            {"name": "tpaccept", "usage": "/tpaccept [player]", "desc": "同意传送请求",
             "tiers": ["master", "friend"], "args": {"player": "string?"}, "cooldown": 3},
            "home",
        ]})

        entries = {item["name"]: item for item in store.get()["commands"]}
        self.assertEqual(entries["tpaccept"]["tiers"], ["master", "friend"])
        self.assertEqual(entries["tpaccept"]["cooldown"], 3.0)
        self.assertEqual(entries["tpaccept"]["usage"], "/tpaccept [player]")
        self.assertEqual(entries["home"]["tiers"], ["master", "friend"], "没写 tiers 用默认")
        self.assertEqual(store.get()["global"], ["tpaccept", "home"], "名字视图同步")

    def test_guest_can_never_run_a_whitelisted_command(self):
        store = self._store({"commands": ["home"]})
        gate = CommandGate(store)

        result = gate.evaluate("home", [], tier="guest")

        self.assertFalse(result["ok"])
        self.assertEqual(result["blocked"], "tier_not_allowed")

    def test_declared_tiers_are_enforced(self):
        store = self._store({"commands": [{"name": "tp", "tiers": ["master"]}]})
        gate = CommandGate(store)

        self.assertEqual(gate.evaluate("tp", ["Steve"], tier="friend")["blocked"],
                         "tier_not_allowed")

    def test_server_scoped_entries_override_the_global_one(self):
        store = self._store({
            "commands": ["home"],
            "servers": {"srvA": [{"name": "home", "tiers": ["master"], "cooldown": 5}]},
        })

        self.assertEqual(store.tiers_for("home", "srvB"), ["master", "friend"])
        self.assertEqual(store.tiers_for("home", "srva"), ["master"])
        self.assertEqual(store.cooldown_for("home", "srva"), 5.0)

    def test_cooldown_blocks_the_second_call(self):
        now = {"t": 100.0}
        store = self._store({"commands": [{"name": "home", "cooldown": 10}]})
        gate = CommandGate(store, clock=lambda: now["t"])

        self.assertTrue(gate.evaluate("home", [], tier="master")["ok"])
        blocked = gate.evaluate("home", [], tier="master")
        self.assertEqual(blocked["blocked"], "cooldown")

        now["t"] += 11.0
        self.assertTrue(gate.evaluate("home", [], tier="master")["ok"])

    def test_disabled_entry_is_not_selectable(self):
        store = self._store({"commands": [{"name": "home", "enabled": False}]})

        self.assertEqual(store.whitelist_for(""), [])
        self.assertFalse(store.is_whitelisted("home"))

    def test_blacklisted_entry_is_dropped_from_the_commands_list(self):
        store = self._store({"commands": ["kill", "home"]})

        self.assertEqual(store.get()["global"], ["home"])


class SessionCommandPortTests(unittest.TestCase):
    """`Session.run_server_command` 是**单一指令口**：闸门放行才真的下发。"""

    def _session(self, payload):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        session = Session.__new__(Session)
        session.skills = _Skills()
        session.runtime = {"players": [{"name": "Steve"}]}
        session._actor = {"name": "Owner", "tier": "master"}
        session._security_audit = []
        session._security_log_failed = True  # 测试不落盘
        session.commands_store = CommandsStore(path=os.path.join(tmp.name, "commands.json"))
        session.commands_store.set(payload)
        session.command_gate = CommandGate(session.commands_store,
                                           audit=session._on_security_audit)
        return session

    def test_refused_command_never_reaches_the_wire(self):
        session = self._session({})

        result = session.run_server_command("kick", ["Steve"])

        self.assertFalse(result["ok"])
        self.assertEqual(result["blocked"], "hard_blacklist")
        self.assertEqual(session.skills.commands, [])

    def test_not_whitelisted_never_reaches_the_wire(self):
        session = self._session({})

        result = session.run_server_command("tpaccept", ["Steve"])

        self.assertEqual(result["blocked"], "not_whitelisted")
        self.assertEqual(session.skills.commands, [])

    def test_whitelisted_command_reaches_the_wire_without_slash(self):
        session = self._session({"global": ["tpaccept"]})

        result = session.run_server_command("tpaccept", ["Steve"])

        self.assertTrue(result["ok"])
        self.assertEqual(session.skills.commands, ["tpaccept Steve"])

    def test_confirm_flow_through_the_session(self):
        session = self._session({"global": ["give"]})

        first = session.run_server_command("give", ["Steve", "diamond"])
        self.assertEqual(first["blocked"], "needs_confirm")
        self.assertEqual(session.skills.commands, [])

        second = session.run_server_command("give", ["Steve", "diamond"],
                                            confirm=first["confirm"])
        self.assertTrue(second["ok"])
        self.assertEqual(session.skills.commands, ["give Steve diamond"])

    def test_security_audit_keeps_the_rows(self):
        session = self._session({"global": ["tpaccept"]})

        session.run_server_command("tpaccept", ["Steve"])

        self.assertTrue(session.security_audit())
        self.assertIn(session.security_audit()[-1]["outcome"], {"allowed"})


class TpTargetResolutionTests(unittest.TestCase):
    """`tp` 的目标必须解析成唯一在线玩家：歧义/不在线/坐标都要按规矩来。"""

    def _session(self, whitelist=None):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        session = Session.__new__(Session)
        session.skills = _Skills()
        session.runtime = {"players": [{"name": "Steve", "uuid": "u1"},
                                       {"name": "Steve_Builder", "uuid": "u2"}],
                           "world": {"server": "srv", "world_name": "w"}}
        session._actor = {"name": "Owner", "tier": "master"}
        session._security_audit = []
        session._security_log_failed = True
        session.commands_store = CommandsStore(path=os.path.join(tmp.name, "commands.json"))
        session.commands_store.set({"global": whitelist or ["tp"]})
        session.command_gate = CommandGate(session.commands_store,
                                           audit=session._on_security_audit)
        return session

    def _issue(self, session, args):
        first = session.run_server_command("tp", args)
        self.assertEqual(first.get("blocked"), "needs_confirm", first)
        return first["confirm"]

    def test_fuzzy_name_is_resolved_to_the_exact_player(self):
        session = self._session()
        confirm = self._issue(session, ["Stvee", "~", "~", "~"])

        result = session.run_server_command("tp", ["Stvee", "~", "~", "~"], confirm=confirm)

        self.assertTrue(result["ok"])
        self.assertEqual(session.skills.commands, ["tp Steve ~ ~ ~"])

    def test_ambiguous_name_is_refused_with_candidates(self):
        session = self._session()

        result = session.run_server_command("tp", ["steve_"])

        self.assertFalse(result["ok"])
        self.assertEqual(result["blocked"], "ambiguous")
        self.assertIn("Steve_Builder", result["candidates"])
        self.assertEqual(session.skills.commands, [], "没锁死目标就绝不发指令")

    def test_offline_player_is_refused(self):
        session = self._session()

        result = session.run_server_command("tp", ["Herobrine"])

        self.assertEqual(result["blocked"], "not_found")
        self.assertEqual(session.skills.commands, [])

    def test_coordinates_are_not_treated_as_player_names(self):
        session = self._session()
        confirm = self._issue(session, ["100", "64", "100"])

        result = session.run_server_command("tp", ["100", "64", "100"], confirm=confirm)

        self.assertTrue(result["ok"], result)
        self.assertEqual(session.skills.commands, ["tp 100 64 100"])

    def test_tpaccept_resolves_its_target(self):
        session = self._session(["tpaccept"])
        session.runtime["players"] = [{"name": "Steve", "uuid": "u1"}]

        result = session.run_server_command("tpaccept", ["steve"])

        self.assertTrue(result["ok"])
        self.assertEqual(session.skills.commands, ["tpaccept Steve"])


if __name__ == "__main__":
    unittest.main()
