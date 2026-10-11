"""控制租约（``docs/dev/AGENT_ARCH.md §8``）的接口级验收测试。

本批**只落接口**（不接 VLA、不启用双脑仲裁、不改现有行为）。覆盖：

  ① 生命周期：获取 / 续租 / 转移 / 释放；
  ② 生存反射抢占：**必须带理由与时限**，审计留"谁 / 何时 / 为何 / 被抢的是什么"；
  ③ 非法转移被拒：右脑不能持租、非持租人不能转移/释放、未知身份拒绝；
  ④ 快照只读：改返回值不影响内部状态；
  ⑤ 意图 ≠ 直接控制：右脑只下发意图（不改变持租人）、直写控制被拒；
  ⑥ 能力位如实（``vla_attached=False`` / ``dual_brain_arbitration=False``…）；
  ⑦ **本批未改变现有行为**：`EatYield` 输出与 wire 字节照旧、L0/L1 静态前缀逐字节不变。
"""

import unittest

from agent.control_lease import (AUDIT_MAX, CAPABILITY_DEFAULTS, HOLDER_LEFT_LOCAL,
                                 HOLDER_LEFT_VLA, HOLDER_REFLEX_EAT, HOLDER_REFLEX_SWIM,
                                 HOLDER_RIGHT_LLM, INTENT_CRAFT, INTENT_GO_TO,
                                 LEASE_EXPIRE, LEASE_GRANT, LEASE_INTENT, LEASE_PREEMPT,
                                 LEASE_RELEASE, LEASE_TRANSFER, REASON_SURVIVAL_COMBAT,
                                 REASON_SURVIVAL_EAT, ControlLease, LeaseConflict,
                                 LeaseDenied, is_right_brain, register_reflex_preemption)
from agent.modes_engine import EatYield
from agent.planner import L0_PLANNER_CONTRACT, Planner


class FakeClock:
    """可推进的假时钟（抢占/到期要有确定时限）。"""

    def __init__(self, now: float = 1000.0):
        self.now = float(now)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += float(seconds)


class DummyMemory:
    def build_context(self):
        return {}

    def get_recent_context(self, _count):
        return []


class RecordingSkills:
    """只记录下发调用（不碰 wire）：用来证明"零字节下发"。"""

    def __init__(self):
        self.calls: list[tuple] = []

    def eat(self):
        self.calls.append(("eat",))


# ── ① 生命周期 ───────────────────────────────────────────────

class LifecycleTests(unittest.TestCase):
    def test_acquire_grants_and_snapshots(self):
        lease = ControlLease()
        event = lease.acquire(HOLDER_LEFT_LOCAL, "本地闭环接管", ttl=5.0)

        self.assertEqual(event["kind"], LEASE_GRANT)
        self.assertEqual(lease.holder, HOLDER_LEFT_LOCAL)
        self.assertEqual(lease.reason, "本地闭环接管")
        self.assertEqual(lease.snapshot()["holder"], HOLDER_LEFT_LOCAL)

    def test_acquire_conflicts_when_held_by_another(self):
        lease = ControlLease(holder=HOLDER_LEFT_LOCAL)

        with self.assertRaises(LeaseConflict):
            lease.acquire(HOLDER_LEFT_VLA, "抢")

    def test_reacquire_by_same_holder_renews(self):
        clock = FakeClock()
        lease = ControlLease(clock=clock)
        lease.acquire(HOLDER_LEFT_VLA, "vla 上线", ttl=10.0)
        clock.advance(4.0)
        lease.acquire(HOLDER_LEFT_VLA, "vla 续租", ttl=10.0)

        self.assertEqual(lease.holder, HOLDER_LEFT_VLA)
        self.assertEqual(lease.reason, "vla 续租")
        self.assertEqual(lease.expires_at, clock.now + 10.0)

    def test_transfer_moves_the_lease(self):
        lease = ControlLease(holder=HOLDER_LEFT_LOCAL)
        event = lease.transfer(HOLDER_LEFT_LOCAL, HOLDER_LEFT_VLA, "交给 VLA")

        self.assertEqual(event["kind"], LEASE_TRANSFER)
        self.assertEqual(event["previous"], HOLDER_LEFT_LOCAL)
        self.assertEqual(lease.holder, HOLDER_LEFT_VLA)

    def test_release_frees_the_lease(self):
        lease = ControlLease(holder=HOLDER_LEFT_LOCAL)
        event = lease.release(HOLDER_LEFT_LOCAL, "交还")

        self.assertEqual(event["kind"], LEASE_RELEASE)
        self.assertIsNone(lease.holder)

    def test_ttl_expiry_frees_and_audits(self):
        clock = FakeClock()
        lease = ControlLease(clock=clock)
        lease.acquire(HOLDER_LEFT_VLA, "临时接管", ttl=3.0)
        clock.advance(3.5)

        self.assertIsNone(lease.holder)
        self.assertEqual(lease.events()[-1]["kind"], LEASE_EXPIRE)
        self.assertEqual(lease.events()[-1]["previous"], HOLDER_LEFT_VLA)

    def test_ttl_must_be_positive(self):
        lease = ControlLease()

        with self.assertRaises(ValueError):
            lease.acquire(HOLDER_LEFT_VLA, "无界", ttl=0)


# ── ② 抢占：理由 + 时限 + 审计 ────────────────────────────────

class PreemptionTests(unittest.TestCase):
    def test_preempt_records_reason_ttl_and_victim(self):
        clock = FakeClock()
        lease = ControlLease(clock=clock)
        lease.acquire(HOLDER_LEFT_VLA, "vla 在动", ttl=60.0)
        clock.advance(1.0)
        event = lease.preempt(HOLDER_REFLEX_EAT, REASON_SURVIVAL_EAT, ttl=8.0)

        self.assertEqual(event["kind"], LEASE_PREEMPT)
        self.assertEqual(event["holder"], HOLDER_REFLEX_EAT)     # 谁
        self.assertEqual(event["previous"], HOLDER_LEFT_VLA)     # 被抢的是什么
        self.assertEqual(event["reason"], REASON_SURVIVAL_EAT)   # 为何
        self.assertEqual(event["ttl"], 8.0)                      # 时限
        self.assertEqual(event["at"], clock.now)                 # 何时
        self.assertEqual(lease.holder, HOLDER_REFLEX_EAT)

    def test_preempt_without_reason_is_rejected(self):
        lease = ControlLease()

        with self.assertRaises(ValueError):
            lease.preempt(HOLDER_REFLEX_EAT, "", ttl=8.0)

    def test_preempt_without_ttl_is_rejected(self):
        lease = ControlLease()

        with self.assertRaises(ValueError):
            lease.preempt(HOLDER_REFLEX_EAT, REASON_SURVIVAL_COMBAT, ttl=0)

    def test_preemption_expires_back_to_free_and_audits(self):
        clock = FakeClock()
        lease = ControlLease(clock=clock)
        lease.preempt(HOLDER_REFLEX_SWIM, "溺水", ttl=2.0)
        clock.advance(2.5)

        self.assertIsNone(lease.holder)
        self.assertEqual(lease.expire() is None, True, "已到期读取后再调用 expire 不该重复记录")

    def test_audit_is_bounded_ring_buffer(self):
        lease = ControlLease()
        for i in range(AUDIT_MAX + 10):
            lease.preempt(HOLDER_REFLEX_EAT, f"episode {i}", ttl=1.0)

        self.assertEqual(len(lease.events()), AUDIT_MAX)
        self.assertEqual(lease.events()[-1]["seq"], AUDIT_MAX + 10)

    def test_register_reflex_preemption_fills_default_reason(self):
        lease = ControlLease()
        event = register_reflex_preemption(lease, holder=HOLDER_REFLEX_EAT, ttl=8.0)

        self.assertEqual(event["reason"], REASON_SURVIVAL_EAT)
        self.assertEqual(lease.holder, HOLDER_REFLEX_EAT)

    def test_register_reflex_preemption_without_lease_is_a_noop(self):
        self.assertIsNone(register_reflex_preemption(None, holder=HOLDER_REFLEX_EAT))


# ── ③ 非法转移被拒 ───────────────────────────────────────────

class IllegalTransitionTests(unittest.TestCase):
    def test_right_brain_is_never_a_valid_holder(self):
        self.assertTrue(is_right_brain(HOLDER_RIGHT_LLM))
        lease = ControlLease()

        with self.assertRaises(LeaseDenied):
            lease.acquire(HOLDER_RIGHT_LLM, "LLM 想接管")

    def test_right_brain_cannot_be_transfer_target(self):
        lease = ControlLease(holder=HOLDER_LEFT_LOCAL)

        with self.assertRaises(LeaseDenied):
            lease.transfer(HOLDER_LEFT_LOCAL, HOLDER_RIGHT_LLM, "全权委托")

    def test_right_brain_cannot_preempt(self):
        lease = ControlLease()

        with self.assertRaises(LeaseDenied):
            lease.preempt(HOLDER_RIGHT_LLM, "LLM 抢控制", ttl=5.0)

    def test_non_holder_cannot_transfer(self):
        lease = ControlLease(holder=HOLDER_LEFT_LOCAL)

        with self.assertRaises(LeaseConflict):
            lease.transfer(HOLDER_LEFT_VLA, HOLDER_LEFT_VLA, "自封")

    def test_non_holder_cannot_release(self):
        lease = ControlLease(holder=HOLDER_LEFT_LOCAL)

        with self.assertRaises(LeaseConflict):
            lease.release(HOLDER_LEFT_VLA)

    def test_unknown_holder_is_rejected(self):
        lease = ControlLease()

        with self.assertRaises(LeaseDenied):
            lease.acquire("martian:brain", "谁？")


# ── ④ 快照只读 ───────────────────────────────────────────────

class ReadOnlySnapshotTests(unittest.TestCase):
    def test_mutating_snapshot_does_not_leak(self):
        lease = ControlLease(holder=HOLDER_LEFT_LOCAL, reason="原样")
        snapshot = lease.snapshot()
        snapshot["holder"] = "tampered"
        snapshot["capabilities"]["vla_attached"] = True

        self.assertEqual(lease.holder, HOLDER_LEFT_LOCAL)
        self.assertFalse(lease.capabilities()["vla_attached"])

    def test_mutating_event_copy_does_not_leak(self):
        lease = ControlLease()
        lease.preempt(HOLDER_REFLEX_EAT, REASON_SURVIVAL_EAT, ttl=1.0)
        row = lease.events()[-1]
        row["reason"] = "tampered"
        row["detail"]["x"] = 1

        self.assertEqual(lease.events()[-1]["reason"], REASON_SURVIVAL_EAT)
        self.assertNotIn("x", lease.events()[-1]["detail"])


# ── ⑤ 意图 ≠ 直接控制 ────────────────────────────────────────

class IntentChannelTests(unittest.TestCase):
    def test_intent_does_not_change_the_holder(self):
        lease = ControlLease(holder=HOLDER_LEFT_LOCAL)
        event = lease.submit_intent(INTENT_GO_TO, {"x": 1, "y": 64, "z": 2})

        self.assertEqual(event["kind"], LEASE_INTENT)
        self.assertEqual(event["detail"]["x"], 1)
        self.assertEqual(lease.holder, HOLDER_LEFT_LOCAL, "意图不该夺走控制权")

    def test_unknown_intent_is_rejected(self):
        lease = ControlLease()

        with self.assertRaises(ValueError):
            lease.submit_intent("take_over_the_world")

    def test_right_brain_cannot_write_control_directly(self):
        lease = ControlLease(holder=HOLDER_LEFT_LOCAL)

        with self.assertRaises(LeaseDenied):
            lease.direct_control(HOLDER_RIGHT_LLM)

    def test_only_the_holder_may_write_control(self):
        lease = ControlLease(holder=HOLDER_LEFT_LOCAL)

        self.assertTrue(lease.direct_control(HOLDER_LEFT_LOCAL))
        with self.assertRaises(LeaseDenied):
            lease.direct_control(HOLDER_LEFT_VLA)

    def test_submit_intent_is_audited(self):
        lease = ControlLease()
        lease.submit_intent(INTENT_CRAFT, {"item": "wooden_pickaxe"})

        self.assertEqual(lease.audit_log(1)[0]["reason"], INTENT_CRAFT)


# ── ⑥ 能力位如实 ─────────────────────────────────────────────

class CapabilityTests(unittest.TestCase):
    def test_defaults_are_honest(self):
        lease = ControlLease()
        caps = lease.capabilities()

        self.assertFalse(caps["vla_attached"], "没有 VLA endpoint/key → 必须如实为 False")
        self.assertFalse(caps["dual_brain_arbitration"], "本批不启用双脑仲裁")
        self.assertTrue(caps["intent_channel"])
        self.assertTrue(caps["preempt_audit"])
        self.assertEqual(caps, dict(CAPABILITY_DEFAULTS))

    def test_overrides_are_merged(self):
        lease = ControlLease(capabilities={"vla_attached": True})

        self.assertTrue(lease.capabilities()["vla_attached"])
        self.assertFalse(lease.capabilities()["dual_brain_arbitration"])


# ── ⑦ 本批未改变现有行为 ─────────────────────────────────────

class NoBehaviorChangeTests(unittest.TestCase):
    def test_eat_yield_without_lease_is_byte_identical(self):
        """没注入租约（旧构造方式）→ 输出与历史逐字相同、不产生任何副作用。"""
        guard = EatYield()

        self.assertEqual(guard.allow({"reflex_eat": True}),
                         (False, "（本地饥饿反射正在吃，我不抢）"))
        self.assertEqual(guard.allow({}), (True, ""))

    def test_reflex_hold_is_registered_once_per_episode(self):
        lease = ControlLease()
        guard = EatYield(control_lease=lease)

        for _ in range(3):
            self.assertEqual(guard.allow({"reflex_eat": True}),
                             (False, "（本地饥饿反射正在吃，我不抢）"))
        self.assertEqual(len(lease.events()), 1, "一次连续反射只登记一次审计")

        guard.allow({})                       # 反射结束
        guard.allow({"reflex_eat": True})     # 新一段
        self.assertEqual(len(lease.events()), 2)

    def test_planner_wires_the_lease_without_extra_behavior(self):
        planner = Planner(llm_service=None, memory=DummyMemory(), skills=RecordingSkills())

        self.assertIsInstance(planner.control_lease, ControlLease)
        self.assertIs(planner.eat_yield._control_lease, planner.control_lease)
        self.assertIsNone(planner.control_lease.holder, "初始无持租人（不启用仲裁）")

    def test_eat_dispatch_sends_nothing_and_audits_the_preemption(self):
        skills = RecordingSkills()
        planner = Planner(llm_service=None, memory=DummyMemory(), skills=skills)

        handled = planner._dispatch_tool_call("eat", {}, {"reflex_eat": True})

        self.assertFalse(handled, "本地反射在吃时后端不许抢")
        self.assertEqual(skills.calls, [], "零下发")
        events = planner.control_lease.events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["kind"], LEASE_PREEMPT)
        self.assertEqual(events[0]["holder"], HOLDER_REFLEX_EAT)
        self.assertEqual(events[0]["reason"], REASON_SURVIVAL_EAT)

    def test_static_prefix_is_byte_identical_and_within_budget(self):
        """L0/L1 静态前缀不因本批改变（只减不抬；实测 1648 / 预算 1700）。"""
        first = Planner(llm_service=None, memory=DummyMemory(), skills=RecordingSkills())
        second = Planner(llm_service=None, memory=DummyMemory(), skills=RecordingSkills())

        layers_a = first.build_prompt_layers("Steve", "hi", {}, "AI")
        layers_b = second.build_prompt_layers("Steve", "hi", {}, "AI")

        self.assertEqual(layers_a.system, layers_b.system)
        self.assertLessEqual(len(layers_a.system), 1700, "静态前缀预算只减不抬")
        self.assertEqual(len(layers_a.system), 1648, "本批零 prompt 改动（逐字节计数不变）")

    def test_static_prefix_carries_no_lease_vocabulary(self):
        planner = Planner(llm_service=None, memory=DummyMemory(), skills=RecordingSkills())
        system = planner.build_prompt_layers("Steve", "hi", {}, "AI").system

        for token in ("租约", "control_lease", "vla_attached", "intent", "双脑", "抢占"):
            self.assertNotIn(token, system, f"新接口不许进 L0/L1：{token}")
        self.assertIn(L0_PLANNER_CONTRACT[:16], system, "静态核心仍在（只是零新增）")


if __name__ == "__main__":
    unittest.main()
