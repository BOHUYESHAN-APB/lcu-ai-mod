"""权限 / 频率门 / 成本控制（`agent.permissions`）的回归测试。

覆盖 acceptance 清单：
  1. public 服 guest → silent（零 token），private 服 guest → 可聊（effective≈1）；
  2. public_reply_guests=true → guest 不再 silent；
  3. can_command：private 人人 True，public 仅 master/friend；
  4. task_priority：master > friend > guest；
  5. 频率门公式 max(1, ceil(1/min(1,effective)))；
  6. pending 累加（effective=0.5 时第一条 wait、第二条 act）；
  7. 空窗补偿（隔过 window_seconds 后 pending 重新从 1 开始）；
  8. @点名旁路（冷却中 / pending 不足 都照样 act）；
  9. 1s 抗抖；
  10. poll()：抗抖窗内 None、窗外观测到 act；
  11. no_action 后 8s 冷却，期间被点名仍可旁路；
  12. 落盘 / 读回 / 缺文件回退默认。

时间一律用 FakeClock 或显式 now=，不 sleep：门控全是时间敏感逻辑，
真等 8 秒的测试既慢又会在慢机器上抖。
"""

import json
import random
import tempfile
import unittest
from pathlib import Path

from agent.permissions import (AUDIT_MAX, DEFAULT_COOLDOWN_SECONDS,
                               DEFAULT_DEBOUNCE_SECONDS, DEFAULT_SESSION_ADJUST,
                               DEFAULT_WINDOW_SECONDS, MEMORY_VISIBILITY,
                               PUBLIC_GUEST_REPLY_RATE, SESSION_ADJUST_MAX,
                               TASK_PRIORITIES, TIER_FRIEND, TIER_GUEST,
                               TIER_MASTER, TIERS, GateDecision, PermissionsStore,
                               ReplyGate, TierPolicy)


class FakeClock:
    """可手动推进的时钟：把 8s 冷却、30s 空窗压进毫秒级测试里。"""

    def __init__(self, start: float = 1000.0):
        self.now = float(start)

    def __call__(self) -> float:
        return self.now

    def set(self, value: float) -> float:
        self.now = float(value)
        return self.now

    def advance(self, seconds: float) -> float:
        self.now += float(seconds)
        return self.now


def _decision(kind: str = "continue") -> GateDecision:
    """手工造一个决策对象（调用方从语义门拿到的就是这种）。"""
    return GateDecision(decision=kind, reason="semantic_gate", tier=TIER_GUEST,
                        effective=1.0, threshold=1, pending=0, can_command=False,
                        memory_visibility="public", mentioned=False, event_kind=None,
                        audit={})


class _GateTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "permissions.json"
        self.store = PermissionsStore(self.path)
        self.clock = FakeClock(1000.0)

    def _gate(self, adjust=None, **patch) -> ReplyGate:
        """造一个 gate；patch 写进权限文件（同一个 store 实例，改动即时生效）。"""
        if patch:
            self.store.set(patch)
        gate = ReplyGate(self.store, clock=self.clock)
        if adjust is not None:
            gate.set_session_adjust(adjust)
        return gate


# ── 1. 配置落盘与宽容解析 ────────────────────────────────────────

class PermissionsStoreTests(_GateTestBase):
    def test_missing_file_falls_back_to_defaults(self):
        """缺文件不是错误：默认 private 服、guest 可聊、名单为空。"""
        self.assertFalse(self.path.exists())
        data = self.store.get()
        self.assertEqual(data["server_mode"], "private")
        self.assertFalse(data["public_reply_guests"])
        self.assertEqual(data["master"], [])
        self.assertEqual(data["friends"], [])
        self.assertEqual(data["ban_patterns"], [])
        self.assertEqual(data["base_rates"]["public"][TIER_GUEST], 0.0)
        self.assertEqual(data["cooldown_seconds"], DEFAULT_COOLDOWN_SECONDS)
        self.assertEqual(data["debounce_seconds"], DEFAULT_DEBOUNCE_SECONDS)
        self.assertEqual(data["window_seconds"], DEFAULT_WINDOW_SECONDS)
        self.assertEqual(self.store.tier_of("任何人"), TIER_GUEST)

    def test_set_writes_to_disk_and_a_new_store_reads_it_back(self):
        self.store.set({"master": ["Steve", " alex "], "friends": ["Herobrine"],
                        "server_mode": "public", "public_reply_guests": "是",
                        "cooldown_seconds": "12"})
        on_disk = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(on_disk["master"], ["Steve", "alex"])  # 空格已清
        self.assertEqual(on_disk["friends"], ["Herobrine"])
        self.assertEqual(on_disk["server_mode"], "public")

        reopened = PermissionsStore(self.path)
        self.assertEqual(reopened.tier_of("steve"), TIER_MASTER)
        self.assertEqual(reopened.tier_of("ALEX"), TIER_MASTER)
        self.assertEqual(reopened.tier_of("herobrine"), TIER_FRIEND)
        self.assertEqual(reopened.server_mode(), "public")
        self.assertTrue(reopened.public_reply_guests())
        self.assertEqual(reopened.cooldown_seconds(), 12.0)

    def test_tier_of_is_case_and_space_insensitive_with_master_precedence(self):
        self.store.set({"master": ["Steve"], "friends": ["steve", "Alex"]})
        self.assertEqual(self.store.tier_of("STEVE"), TIER_MASTER)  # master > friend
        self.assertEqual(self.store.tier_of("  alex  "), TIER_FRIEND)
        self.assertEqual(self.store.tier_of("Herobrine"), TIER_GUEST)
        self.assertEqual(self.store.tier_of(""), TIER_GUEST)
        self.assertEqual(self.store.tier_of(None), TIER_GUEST)

    def test_set_accepts_loose_booleans(self):
        for value in (True, "true", "是", 1, "1", "yes", "on"):
            self.store.set({"public_reply_guests": value})
            self.assertTrue(self.store.public_reply_guests(), f"{value!r} 应解析为 True")
        for value in (False, "false", "否", 0, "0", "no", "off"):
            self.store.set({"public_reply_guests": value})
            self.assertFalse(self.store.public_reply_guests(), f"{value!r} 应解析为 False")
        self.store.set({"public_reply_guests": True})
        self.store.set({"public_reply_guests": "谁知道呢"})  # 认不出 → 回退默认
        self.assertFalse(self.store.public_reply_guests())

    def test_garbage_payload_never_raises(self):
        self.store.set({"master": 123, "friends": {"a": 1},
                        "ban_patterns": [None, "", "   ", "刷屏"],
                        "server_mode": "uh", "cooldown_seconds": "很久",
                        "base_rates": "not a dict", "session_adjust": float("nan")})
        data = self.store.get()
        self.assertEqual(data["master"], [])
        self.assertEqual(data["friends"], [])
        self.assertEqual(data["ban_patterns"], ["刷屏"])  # 空正则会命中一切，必须丢掉
        self.assertEqual(data["server_mode"], "private")
        self.assertEqual(data["cooldown_seconds"], DEFAULT_COOLDOWN_SECONDS)
        self.assertEqual(data["session_adjust"], DEFAULT_SESSION_ADJUST)
        self.assertEqual(data["base_rates"]["private"][TIER_GUEST], 1.0)

        self.store.set(None)  # 根本不是 dict
        self.store.set({"session_adjust": True})  # bool 不该被 float() 吃掉成 1.0
        self.assertEqual(self.store.get()["session_adjust"], DEFAULT_SESSION_ADJUST)

    def test_unknown_keys_are_ignored_and_missing_ones_kept(self):
        self.store.set({"master": ["A"], "evil": 1})
        data = self.store.get()
        self.assertNotIn("evil", data)
        self.assertEqual(data["master"], ["A"])
        self.assertEqual(data["friends"], [])

    def test_partial_base_rates_patch_keeps_other_cells(self):
        self.store.set({"base_rates": {"private": {"guest": 0.8}}})
        data = self.store.get()
        self.assertEqual(data["base_rates"]["private"][TIER_GUEST], 0.8)
        self.assertEqual(data["base_rates"]["private"][TIER_FRIEND], 1.0)
        self.assertEqual(data["base_rates"]["public"][TIER_FRIEND], 0.7)

    def test_negative_and_huge_rates_are_clamped(self):
        self.store.set({"base_rates": {"public": {"guest": -3}}})
        self.assertEqual(self.store.base_rate(TIER_GUEST, server_mode="public"), 0.0)
        self.store.set({"session_adjust": 999})
        self.assertEqual(self.store.session_adjust(), SESSION_ADJUST_MAX)
        self.assertEqual(self.store.get()["base_rates"]["private"][TIER_FRIEND], 1.0)

    def test_get_returns_an_isolated_deep_copy(self):
        self.store.set({"master": ["A"]})
        data = self.store.get()
        data["master"].append("Hacker")
        data["ban_patterns"].append("x")
        self.assertEqual(self.store.get()["master"], ["A"])
        self.assertEqual(self.store.get()["ban_patterns"], [])

    def test_reload_picks_up_an_external_edit(self):
        self.path.write_text(json.dumps({"master": ["Zed"]}), encoding="utf-8")
        self.assertEqual(self.store.tier_of("zed"), TIER_GUEST)  # 还没重读
        self.store.reload()
        self.assertEqual(self.store.tier_of("zed"), TIER_MASTER)

    def test_reload_survives_a_corrupt_file(self):
        self.store.set({"master": ["Steve"]})
        self.path.write_text("{ 这不是 JSON", encoding="utf-8")
        data = self.store.reload()  # 不能抛
        self.assertEqual(data["master"], [])
        self.assertEqual(data["server_mode"], "private")


# ── 2. TierPolicy 数据表 ────────────────────────────────────────

class TierPolicyTableTests(_GateTestBase):
    def test_can_command_private_allows_everyone(self):
        """私人服：所有人都可以跟他对话（用户明确要求）。"""
        for tier in TIERS:
            self.assertTrue(self.store.policy(tier, server_mode="private").can_command, tier)

    def test_can_command_public_only_master_and_friend(self):
        self.assertTrue(self.store.policy(TIER_MASTER, server_mode="public").can_command)
        self.assertTrue(self.store.policy(TIER_FRIEND, server_mode="public").can_command)
        self.assertFalse(self.store.policy(TIER_GUEST, server_mode="public").can_command)

    def test_can_command_follows_the_store_mode_by_default(self):
        self.assertTrue(self.store.policy(TIER_GUEST).can_command)  # 默认 private
        self.store.set({"server_mode": "public"})
        self.assertFalse(self.store.policy(TIER_GUEST).can_command)
        self.assertTrue(self.store.policy(TIER_FRIEND).can_command)

    def test_task_priority_orders_master_above_friend_above_guest(self):
        priority = self.store.task_priority
        self.assertGreater(priority(TIER_MASTER), priority(TIER_FRIEND))
        self.assertGreater(priority(TIER_FRIEND), priority(TIER_GUEST))
        self.assertEqual((priority(TIER_MASTER), priority(TIER_FRIEND), priority(TIER_GUEST)),
                         (TASK_PRIORITIES[TIER_MASTER], TASK_PRIORITIES[TIER_FRIEND],
                          TASK_PRIORITIES[TIER_GUEST]))
        self.assertTrue(self.store.policy(TIER_MASTER).task_priority >
                        self.store.policy(TIER_FRIEND).task_priority)

    def test_memory_visibility_table(self):
        self.assertEqual(self.store.policy(TIER_MASTER).memory_visibility, "all")
        self.assertEqual(self.store.policy(TIER_FRIEND).memory_visibility, "all")
        self.assertEqual(self.store.policy(TIER_GUEST).memory_visibility, "public")
        self.assertEqual(self.store.policy(TIER_GUEST).memory_visibility,
                         MEMORY_VISIBILITY[TIER_GUEST])

    def test_unknown_tier_degrades_to_guest(self):
        policy = self.store.policy("admin")
        self.assertIsInstance(policy, TierPolicy)
        self.assertEqual(policy.tier, TIER_GUEST)
        self.assertEqual(self.store.task_priority("admin"), TASK_PRIORITIES[TIER_GUEST])

    def test_base_rate_defaults(self):
        self.assertAlmostEqual(self.store.base_rate(TIER_MASTER, server_mode="private"), 1.0)
        self.assertAlmostEqual(self.store.base_rate(TIER_FRIEND, server_mode="private"), 1.0)
        self.assertAlmostEqual(self.store.base_rate(TIER_GUEST, server_mode="private"), 1.0)
        self.assertAlmostEqual(self.store.base_rate(TIER_MASTER, server_mode="public"), 1.0)
        self.assertAlmostEqual(self.store.base_rate(TIER_FRIEND, server_mode="public"), 0.7)
        self.assertAlmostEqual(self.store.base_rate(TIER_GUEST, server_mode="public"), 0.0)


# ── 3. 频率门公式 ───────────────────────────────────────────────

class ThresholdFormulaTests(_GateTestBase):
    def test_threshold_formula(self):
        cases = {1.0: 1, 0.5: 2, 0.34: 3, 0.2: 5, 0.99: 2, 0.0: 0, -1.0: 0,
                 1.5: 1, 4.0: 1}  # >1 夹到 1（没有"比每条都回更频繁"的语义）
        for effective, expected in cases.items():
            self.assertEqual(ReplyGate.threshold_for(effective), expected, f"effective={effective}")

    def test_threshold_formula_rejects_garbage(self):
        for value in (None, "abc", float("nan"), float("inf"), True):
            self.assertEqual(ReplyGate.threshold_for(value), 0, f"{value!r}")

    def test_effective_is_base_times_session_adjust(self):
        gate = self._gate()  # 默认 adjust=1.0
        self.assertAlmostEqual(gate.effective_for(TIER_GUEST), 1.0)
        gate.set_session_adjust(0.5)
        self.assertAlmostEqual(gate.effective_for(TIER_GUEST), 0.5)
        self.assertEqual(gate.threshold_for(gate.effective_for(TIER_GUEST)), 2)
        self.store.set({"server_mode": "public"})
        self.assertAlmostEqual(gate.effective_for(TIER_FRIEND), 0.35)
        self.assertEqual(gate.threshold_for(gate.effective_for(TIER_FRIEND)), 3)


# ── 4. 静默模式（零 token） ──────────────────────────────────────

class SilentModeTests(_GateTestBase):
    def test_private_server_guest_is_chattable(self):
        decision = self._gate(1.0).submit("随机玩家", "在吗", now=1000.0)
        self.assertEqual(decision.tier, TIER_GUEST)
        self.assertEqual(decision.decision, "act")
        self.assertEqual(decision.reason, "frequency_pass")
        self.assertAlmostEqual(decision.effective, 1.0)
        self.assertEqual(decision.threshold, 1)
        self.assertTrue(decision.can_command)  # private 服人人可下指令

    def test_public_server_guest_is_silent(self):
        """公开服 guest：只记录、不回、零 token（消息仍由调用方写进记忆）。"""
        gate = self._gate(1.0, server_mode="public")
        decision = gate.submit("路人", "帮我挖矿", now=1000.0)
        self.assertEqual(decision.decision, "silent")
        self.assertEqual(decision.reason, "silent_mode")
        self.assertEqual(decision.effective, 0.0)
        self.assertEqual(decision.threshold, 0)
        self.assertFalse(decision.can_command)
        self.assertEqual(decision.memory_visibility, "public")
        self.assertNotEqual(decision.decision, "drop")  # 静默 ≠ 丢弃
        self.assertEqual(gate.status()["pending"], 0)

    def test_public_server_silence_is_not_bypassed_by_a_mention(self):
        """guest 静默是硬成本控制：一条 @ 不该破防，要放开请开 public_reply_guests。"""
        gate = self._gate(server_mode="public")
        decision = gate.submit("路人", "@AI 在吗", now=1000.0, mentioned=True)
        self.assertEqual(decision.decision, "silent")
        self.assertTrue(decision.mentioned)

    def test_public_reply_guests_turns_silence_into_a_rate(self):
        gate = self._gate(server_mode="public", public_reply_guests=True)
        decision = gate.submit("路人", "在吗", now=1000.0)
        self.assertNotEqual(decision.decision, "silent")
        self.assertAlmostEqual(decision.effective, PUBLIC_GUEST_REPLY_RATE)
        self.assertEqual(decision.threshold, 2)
        self.assertEqual(decision.decision, "wait")
        self.assertEqual(decision.reason, "frequency(1/2)")
        self.assertFalse(decision.can_command)  # 能聊 ≠ 能指挥

    def test_master_and_friend_are_heard_in_public(self):
        gate = self._gate(server_mode="public", master=["Steve"], friends=["Alex"])
        steve = gate.submit("Steve", "来我这边", now=1000.0)
        self.assertEqual(steve.decision, "act")
        self.assertTrue(steve.can_command)
        # friend 在公开服的 base 是 0.7 → 两条放一条，但绝不像 guest 那样直接静默
        first = gate.submit("Alex", "在吗", now=1002.0)
        self.assertEqual(first.decision, "wait")
        self.assertEqual(first.reason, "frequency(1/2)")
        self.assertTrue(first.can_command)
        self.assertEqual(gate.submit("Alex", "在吗", now=1003.5).decision, "act")
        self.assertEqual(gate.submit("路人", "在吗", now=1005.0).decision, "silent")

    def test_system_messages_are_never_answered(self):
        gate = self._gate(1.0)
        decision = gate.submit("Steve", "[Mod] 已到达", is_system=True, now=1000.0)
        self.assertEqual(decision.decision, "none")
        self.assertEqual(decision.reason, "system_message")
        self.assertEqual(decision.pending, 0)
        self.assertEqual(gate.status()["pending"], 0)
        # 静默模式的大门之前就返回，也不该被当成"要回复"
        silent = self._gate(server_mode="public").submit("x", "y", is_system=True, now=1000.0)
        self.assertEqual(silent.decision, "none")


# ── 5. L0 ban 过滤 ──────────────────────────────────────────────

class BanFilterTests(_GateTestBase):
    def test_ban_pattern_drops_the_whole_message(self):
        gate = self._gate(1.0, ban_patterns=[r"free\s*gift", "刷屏"])
        decision = gate.submit("路人", "FREE GIFT click here", now=1000.0)
        self.assertEqual(decision.decision, "drop")
        self.assertEqual(decision.reason, "ban_match")
        self.assertEqual(gate.status()["pending"], 0)  # 丢掉的整条不进 pending
        self.assertEqual(gate.submit("路人", "我们去挖矿吧", now=1001.5).decision, "act")

    def test_bad_regex_is_ignored_not_fatal(self):
        gate = self._gate(1.0, ban_patterns=["("])
        decision = gate.submit("路人", "在吗", now=1000.0)
        self.assertEqual(decision.decision, "act")


# ── 6. L2 频率门：累加 + 空窗补偿 ───────────────────────────────

class FrequencyGateTests(_GateTestBase):
    def test_threshold_one_acts_on_every_message(self):
        gate = self._gate(1.0)
        for index in range(3):
            decision = gate.submit("A", f"m{index}", now=1000.0 + index * 2.0)
            self.assertEqual(decision.decision, "act", decision.reason)
            self.assertEqual(decision.reason, "frequency_pass")

    def test_half_rate_needs_two_messages(self):
        """effective=0.5 → 两条里放行一条；两条间隔要大于抗抖窗。"""
        gate = self._gate(0.5)
        first = gate.submit("A", "1", now=1000.0)
        self.assertEqual(first.decision, "wait")
        self.assertEqual(first.reason, "frequency(1/2)")
        self.assertEqual(first.pending, 1)
        second = gate.submit("A", "2", now=1001.5)
        self.assertEqual(second.decision, "act")
        self.assertEqual(second.reason, "frequency_pass")
        self.assertEqual(second.pending, 2)  # 审计里留下"是第 2 条触发的"
        self.assertEqual(gate.status()["pending"], 0)  # 放行即消化

    def test_window_prunes_old_pending_with_the_default_window(self):
        gate = self._gate(0.5)
        self.assertEqual(gate.submit("A", "1", now=1000.0).reason, "frequency(1/2)")
        later = gate.submit("A", "2", now=1000.0 + DEFAULT_WINDOW_SECONDS + 1.0)
        self.assertEqual(later.decision, "wait")
        self.assertEqual(later.reason, "frequency(1/2)")  # 旧的被清掉，重新从 1 开始
        self.assertEqual(later.pending, 1)

    def test_window_compensation_with_a_custom_window(self):
        gate = self._gate(0.5, window_seconds=5.0)
        self.assertEqual(gate.submit("A", "1", now=1000.0).reason, "frequency(1/2)")
        self.assertEqual(gate.submit("A", "2", now=1001.5).decision, "act")
        self.assertEqual(gate.submit("A", "3", now=1002.5).reason, "frequency(1/2)")
        # 隔了 6s（> 5s 窗）：老条目全部作废
        restarted = gate.submit("A", "4", now=1008.5)
        self.assertEqual(restarted.decision, "wait")
        self.assertEqual(restarted.reason, "frequency(1/2)")
        self.assertEqual(restarted.pending, 1)


# ── 7. L3 点名旁路 ──────────────────────────────────────────────

class MentionBypassTests(_GateTestBase):
    def test_mention_acts_even_when_pending_is_short(self):
        gate = self._gate(0.5)  # threshold=2
        decision = gate.submit("A", "@AI 过来", now=1000.0, mentioned=True)
        self.assertEqual(decision.decision, "act")
        self.assertEqual(decision.reason, "mention_bypass")
        self.assertTrue(decision.mentioned)
        self.assertEqual(decision.pending, 0)

    def test_mention_bypasses_the_no_action_cooldown(self):
        gate = self._gate(0.5)
        gate.mark_no_action(now=1000.0)  # 冷却到 1008
        blocked = gate.submit("A", "在吗", now=1002.0)
        self.assertEqual(blocked.decision, "cooldown")
        bypass = gate.submit("A", "@AI 别走", now=1003.0, mentioned=True)
        self.assertEqual(bypass.decision, "act")
        self.assertEqual(bypass.reason, "mention_bypass")

    def test_mention_clears_the_pending_queue(self):
        gate = self._gate(0.5)
        self.assertEqual(gate.submit("A", "1", now=1000.0).decision, "wait")
        self.assertEqual(gate.status()["pending"], 1)
        gate.submit("A", "2", now=1000.1, mentioned=True)
        self.assertEqual(gate.status()["pending"], 0)

    def test_canonical_identity_decides_the_tier(self):
        gate = self._gate(server_mode="public", master=["Steve"])
        decision = gate.submit("Steve_Alt", "来我这边", now=1000.0, canonical="Steve")
        self.assertEqual(decision.tier, TIER_MASTER)
        self.assertTrue(decision.can_command)
        self.assertEqual(decision.audit["sender"], "Steve")  # 审计追到解析后的身份
        self.assertEqual(decision.decision, "act")


# ── 8. 抗抖（1s） ───────────────────────────────────────────────

class DebounceTests(_GateTestBase):
    def test_first_message_is_not_delayed(self):
        decision = self._gate(1.0).submit("A", "在吗", now=1000.0)
        self.assertEqual(decision.decision, "act")

    def test_rapid_follow_up_waits_for_the_debounce_window(self):
        gate = self._gate(1.0)
        gate.submit("A", "1", now=1000.0)  # act
        waited = gate.submit("A", "2", now=1000.5)  # 0.5s < 1s
        self.assertEqual(waited.decision, "wait")
        self.assertEqual(waited.reason, "debounce")
        self.assertEqual(waited.pending, 1)  # 抗抖分支同样要把本条累加进 pending
        self.assertEqual(gate.submit("A", "3", now=1002.0).decision, "act")

    def test_debounce_respects_the_configured_seconds(self):
        gate = self._gate(1.0, debounce_seconds=3.0)
        gate.submit("A", "1", now=1000.0)
        self.assertEqual(gate.submit("A", "2", now=1002.0).reason, "debounce")
        self.assertEqual(gate.submit("A", "3", now=1006.0).decision, "act")  # 4s > 3s


# ── 9. poll()：让 wait 住的消息在 tick 里发出去 ──────────────────

class PollTests(_GateTestBase):
    def test_poll_is_none_when_nothing_is_pending(self):
        self.assertIsNone(self._gate(1.0).poll(now=1000.0))

    def test_poll_waits_for_the_debounce_window_then_acts(self):
        gate = self._gate(1.0)
        gate.submit("A", "1", now=1000.0)             # act
        gate.submit("A", "2", now=1000.2)             # 抗抖 → wait，pending 留下 1 条
        self.assertIsNone(gate.poll(now=1000.3))
        late = gate.poll(now=1001.5)                  # 1.3s 后抗抖过完
        self.assertIsNotNone(late)
        self.assertEqual(late.decision, "act")
        self.assertEqual(late.reason, "debounce_elapsed")
        self.assertEqual(late.pending, 1)
        self.assertEqual(gate.status()["pending"], 0)  # 已消化
        self.assertIsNone(gate.poll(now=1002.0))

    def test_poll_keeps_waiting_while_the_threshold_is_not_reached(self):
        gate = self._gate(0.5)                        # threshold=2
        gate.submit("A", "1", now=1000.0)             # wait(1/2)
        self.assertIsNone(gate.poll(now=1001.5))      # 抗抖过完但只攒了 1 条
        self.assertEqual(gate.status()["pending"], 1)

    def test_poll_is_blocked_by_the_cooldown(self):
        gate = self._gate(0.5)                        # threshold=2
        gate.submit("A", "1", now=1000.0)             # wait(1/2)
        gate.submit("A", "2", now=1000.5)             # 抗抖 → wait，pending 攒到 2
        gate.mark_no_action(now=1001.0)               # 冷却到 1009
        self.assertIsNone(gate.poll(now=1005.0))      # 攒够了但冷却中
        late = gate.poll(now=1009.5)
        self.assertIsNotNone(late)
        self.assertEqual(late.reason, "debounce_elapsed")


# ── 10. L5 no_action 冷却 ──────────────────────────────────────

class CooldownTests(_GateTestBase):
    def test_no_action_silences_for_eight_seconds(self):
        gate = self._gate(1.0)
        gate.mark_no_action(now=1000.0)
        self.assertAlmostEqual(gate.status()["cooldown_remaining"], DEFAULT_COOLDOWN_SECONDS)
        blocked = gate.submit("A", "在吗", now=1004.0)
        self.assertEqual(blocked.decision, "cooldown")
        self.assertEqual(blocked.reason, "no_action_cooldown")
        recovered = gate.submit("A", "在吗", now=1008.5)  # 8s 之后
        self.assertEqual(recovered.decision, "act")
        self.assertEqual(recovered.reason, "frequency_pass")

    def test_mentioned_still_acts_during_the_cooldown(self):
        gate = self._gate(1.0)
        gate.mark_no_action(now=1000.0)
        self.assertEqual(gate.submit("A", "喂", now=1002.0).decision, "cooldown")
        self.assertEqual(
            gate.submit("A", "@AI 喂", now=1002.0, mentioned=True).decision, "act")

    def test_note_decision_only_cools_down_for_no_action(self):
        gate = self._gate(1.0)
        gate.note_decision(_decision("continue"))
        self.assertEqual(gate.status()["cooldown_remaining"], 0.0)
        gate.note_decision(_decision("wait"))
        self.assertEqual(gate.status()["cooldown_remaining"], 0.0)
        gate.note_decision(_decision("no_action"))
        self.assertAlmostEqual(gate.status()["cooldown_remaining"], DEFAULT_COOLDOWN_SECONDS)
        self.assertEqual(gate.submit("A", "在吗", now=1000.0).decision, "cooldown")

    def test_note_decision_also_accepts_a_plain_dict(self):
        gate = self._gate(1.0)
        gate.note_decision({"decision": "no_action"})
        self.assertAlmostEqual(gate.status()["cooldown_remaining"], DEFAULT_COOLDOWN_SECONDS)

    def test_cooldown_length_follows_the_config(self):
        gate = self._gate(1.0, cooldown_seconds=2.0)
        gate.mark_no_action(now=1000.0)
        self.assertEqual(gate.submit("A", "喂", now=1001.0).decision, "cooldown")
        self.assertEqual(gate.submit("A", "喂", now=1002.5).decision, "act")


# ── 11. session_adjust 与事件注入 ──────────────────────────────

class SessionAdjustTests(_GateTestBase):
    def test_default_comes_from_the_permission_file_and_is_live(self):
        gate = ReplyGate(self.store, clock=self.clock, rng=random.Random(7))
        self.assertAlmostEqual(gate.session_adjust(), DEFAULT_SESSION_ADJUST)
        self.store.set({"session_adjust": 0.25})  # UI 改完（同一 store 实例）
        self.assertAlmostEqual(gate.session_adjust(), 0.25)  # 即时生效，不用重启
        self.assertAlmostEqual(gate.effective_for(TIER_GUEST), 0.25)

    def test_set_session_adjust_clamps_and_falls_back(self):
        gate = self._gate()
        gate.set_session_adjust("乱写")
        self.assertAlmostEqual(gate.session_adjust(), 1.0)
        gate.set_session_adjust(99)
        self.assertAlmostEqual(gate.session_adjust(), SESSION_ADJUST_MAX)
        gate.set_session_adjust(-2)
        self.assertAlmostEqual(gate.session_adjust(), 0.0)
        self.assertEqual(gate.submit("A", "hi", now=1000.0).decision, "silent")

    def test_event_injection_is_carried_on_the_decision(self):
        gate = self._gate(1.0)
        decision = gate.submit("Steve", "你被骷髅射死了", event_kind="death", now=1000.0)
        self.assertEqual(decision.event_kind, "death")
        self.assertEqual(decision.decision, "act")
        plain = gate.submit("Steve", "在吗", now=1002.0)
        self.assertIsNone(plain.event_kind)


# ── 12. 审计与仪表盘 ────────────────────────────────────────────

class AuditAndStatusTests(_GateTestBase):
    def test_audit_row_shape_and_order(self):
        gate = self._gate(1.0)
        gate.submit("Steve", "1", now=1000.0)
        gate.submit("Alex", "2", now=1002.0)
        rows = gate.audit_log(10)
        self.assertEqual(len(rows), 2)
        self.assertEqual(set(rows[0]), {"ts", "sender", "tier", "effective", "threshold",
                                        "pending", "decision", "reason"})
        self.assertLess(rows[0]["ts"], rows[1]["ts"])       # 从早到晚
        self.assertEqual(rows[1]["sender"], "Alex")
        self.assertEqual(rows[1]["tier"], TIER_GUEST)
        self.assertEqual(rows[1]["decision"], "act")
        self.assertEqual(rows[1]["reason"], "frequency_pass")
        self.assertEqual(gate.audit_log(1), [rows[-1]])
        self.assertEqual(gate.audit_log(0), [])

    def test_audit_caps_at_fifty_rows(self):
        gate = self._gate(1.0)
        for index in range(AUDIT_MAX + 10):
            gate.submit("A", f"m{index}", now=1000.0 + index * 2.0)
        rows = gate.audit_log(AUDIT_MAX + 10)
        self.assertEqual(len(rows), AUDIT_MAX)
        self.assertEqual(rows[-1]["reason"], "frequency_pass")
        self.assertEqual(rows[0]["ts"], 1000.0 + 10 * 2.0)  # 最老的 10 条已被挤掉

    def test_audit_records_silent_and_drop_too(self):
        """审计要能回答"他刚才为什么不理我"：静默/丢弃也留痕。"""
        gate = self._gate(server_mode="public", ban_patterns=["刷屏"], master=["Steve"])
        gate.submit("路人", "在吗", now=1000.0)          # guest 静默
        gate.submit("Steve", "刷屏", now=1001.0)          # master 但踩了 ban
        decisions = [row["decision"] for row in gate.audit_log(10)]
        self.assertEqual(decisions, ["silent", "drop"])
        self.assertEqual([row["reason"] for row in gate.audit_log(10)],
                         ["silent_mode", "ban_match"])

    def test_status_snapshot(self):
        gate = self._gate(1.0, server_mode="public", friends=["Alex"])
        status = gate.status()
        self.assertEqual(set(status), {"server_mode", "session_adjust", "effective",
                                       "threshold", "pending", "cooldown_remaining",
                                       "last_decisions"})
        self.assertEqual(status["server_mode"], "public")
        self.assertAlmostEqual(status["session_adjust"], 1.0)
        self.assertEqual(set(status["effective"]), set(TIERS))
        self.assertAlmostEqual(status["effective"][TIER_GUEST], 0.0)
        self.assertAlmostEqual(status["effective"][TIER_FRIEND], 0.7)
        self.assertEqual(status["threshold"][TIER_GUEST], 0)
        self.assertEqual(status["threshold"][TIER_FRIEND], 2)
        self.assertEqual(status["threshold"][TIER_MASTER], 1)
        self.assertEqual(status["pending"], 0)
        self.assertEqual(status["cooldown_remaining"], 0.0)
        self.assertEqual(status["last_decisions"], [])

        gate.submit("路人", "在吗", now=1000.0)
        self.assertEqual(len(gate.status()["last_decisions"]), 1)
        # friend 在公开服 base 0.7 → 两条放一条，第一条先攒进 pending
        waiting = gate.submit("Alex", "1", now=1001.5)
        self.assertEqual(waiting.decision, "wait")
        self.assertEqual(gate.status()["pending"], 1)
        self.clock.set(1002.0)
        gate.mark_no_action()
        self.assertAlmostEqual(gate.status()["cooldown_remaining"], DEFAULT_COOLDOWN_SECONDS)


if __name__ == "__main__":
    unittest.main()
