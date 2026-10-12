"""生物对待策略（高危险 / 敌对 / 中立 / 被动）的验收测试。

用户口径（他拍的板）：**高危险的不主动打；中立的不惹；只有该处理的才处理**。

权威那一份表在模组侧（`MobPolicy.java`）：状态里每行实体都带 `tier`
（DANGEROUS / HOSTILE / NEUTRAL / PASSIVE / UNKNOWN）。后端：
  ① **优先信任** `tier`（模组算的）—— 哪怕旧的关键词会把它认成敌人（末影人）；
  ② 没有 `tier`（旧客户端）才按关键词/旧 `type` 兜底，且兜底**也不会**把中立怪当成
     "可以主动攻击"的对象；
  ③ 认不出的 ⇒ UNKNOWN ⇒ 不算威胁、不当敌人（**不许默认 hostile**）;
  ④ 生存反射按档位走：SelfDefense 只打**敌对**（中立不进名单、高危险不许碰）；
     Cowardice 对**高危险**不看血量就先拉开距离；
  ⑤ 造床的"周围是否安全"按威胁档判（中立/被动不算威胁）。

本文件独立自足（自带 skills 假件），不改动别的测试文件。
"""

import unittest
from pathlib import Path

from agent.modes_engine import CowardiceMode, ModesEngine, SelfDefenseMode
from agent.territory import (TIER_DANGEROUS, TIER_HOSTILE, TIER_NEUTRAL, TIER_PASSIVE,
                             TIER_UNKNOWN, hostiles_in, keyword_tier, may_initiate,
                             may_retaliate, normalize_tier, tier_of, threatens_us)


def mob(name, *, type="mob", tier=None, distance=3.0, entity_id=1):
    """一行实体快照（形状与 Java `StateCollector` 一致；`tier` 是模组新发的字段）。"""
    row = {"type": type, "name": name, "distance": distance, "entity_id": entity_id,
           "x": 1.0, "y": 64.0, "z": 2.0}
    if tier is not None:
        row["tier"] = tier
    return row


def state(entities=None, *, health=20, reflex_combat=False, hurt=False):
    return {"player": {"health": health, "x": 0.0, "y": 64.0, "z": 0.0},
            "entities": list(entities or []), "reflex_combat": reflex_combat,
            "hurt": hurt}


class DummySkills:
    """记录所有下发（move_to / attack / look_at）。"""

    def __init__(self):
        self.calls: list[tuple] = []

    def look_at(self, x, y, z):
        self.calls.append(("look_at", x, y, z))

    def attack(self):
        self.calls.append(("attack",))

    def move_to(self, x, y, z, speed=1.0):
        self.calls.append(("move_to", x, y, z))

    def count(self, name):
        return sum(1 for call in self.calls if call[0] == name)

    def names(self):
        return [call[0] for call in self.calls]


def engine_with(mode):
    skills = DummySkills()
    engine = ModesEngine(skills)
    mode.activate_streak_required = 1      # 测试里不等滞回窗口
    engine.add_mode(mode)
    return engine, skills


# ── ① 模组给的 tier 是权威 ──────────────────────────────────────

class TierIsAuthoritativeTests(unittest.TestCase):
    def test_the_mod_field_wins_over_the_legacy_keywords(self):
        """末影人：模组说 NEUTRAL，就不再是"可主动攻击"的对象（旧关键词会认成敌人）。"""
        enderman = mob("末影人", type="hostile", tier="neutral")

        self.assertEqual(tier_of(enderman), TIER_NEUTRAL)
        self.assertFalse(may_initiate(tier_of(enderman)))
        self.assertFalse(threatens_us(enderman))

    def test_the_mod_field_is_accepted_case_insensitively(self):
        self.assertEqual(normalize_tier("HOSTILE"), TIER_HOSTILE)
        self.assertEqual(tier_of(mob("僵尸", tier="DANGEROUS")), TIER_DANGEROUS)
        self.assertEqual(normalize_tier("not-a-tier"), "")

    def test_a_confident_wrong_tier_still_needs_no_keywords(self):
        """有 `tier` 时不看名字：名字像苦力怕也不影响（表就是表，不猜两遍）。"""
        self.assertEqual(tier_of(mob("苦力怕", tier=TIER_PASSIVE)), TIER_PASSIVE)


# ── ② 缺字段（旧客户端）的兜底：认得出但**不把中立怪当敌人** ──────

class KeywordFallbackTests(unittest.TestCase):
    def test_no_tier_falls_back_to_keywords(self):
        self.assertIsNone(mob("僵尸").get("tier"))
        self.assertEqual(keyword_tier(mob("zombie")), TIER_HOSTILE)
        self.assertEqual(tier_of(mob("zombie")), TIER_HOSTILE)

    def test_fallback_keeps_neutral_mobs_out_of_the_target_list(self):
        """兜底路径的关键一条：**末影人不是"可以主动打"的对象**。"""
        for name in ("enderman", "minecraft:enderman", "Enderman", "末影人"):
            row = {"type": "mob", "name": name}
            tier = tier_of(row)
            self.assertEqual(tier, TIER_NEUTRAL, name)
            self.assertFalse(may_initiate(tier), name)
            self.assertFalse(threatens_us(row), name)

    def test_fallback_recognizes_the_rest_of_the_ladder(self):
        self.assertEqual(tier_of({"type": "mob", "name": "wolf"}), TIER_NEUTRAL)
        self.assertEqual(tier_of({"type": "mob", "name": "iron_golem"}), TIER_NEUTRAL)
        self.assertEqual(tier_of({"type": "mob", "name": "warden"}), TIER_DANGEROUS)
        self.assertEqual(tier_of({"type": "mob", "name": "cow"}), TIER_PASSIVE)

    def test_fallback_treats_piglin_brute_as_hostile_not_neutral(self):
        """`piglin_brute` 里含着中立词 `piglin`，但它见到玩家就打 ⇒ 例外表先判。"""
        self.assertEqual(tier_of({"type": "mob", "name": "piglin_brute"}), TIER_HOSTILE)
        self.assertEqual(tier_of({"type": "mob", "name": "piglin"}), TIER_NEUTRAL)

    def test_fallback_treats_wither_skeleton_as_hostile_not_dangerous(self):
        self.assertEqual(tier_of({"type": "mob", "name": "wither_skeleton"}), TIER_HOSTILE)
        self.assertEqual(tier_of({"type": "mob", "name": "wither"}), TIER_DANGEROUS)

    def test_legacy_type_field_is_read_before_the_name(self):
        """旧客户端的中文显示名匹配不上英文关键词 ⇒ 至少还有旧 `type` 兜底。"""
        self.assertEqual(tier_of({"type": "hostile", "name": "僵尸"}), TIER_HOSTILE)
        self.assertEqual(tier_of({"type": "animal", "name": "牛"}), TIER_PASSIVE)

    def test_items_and_players_are_never_mobs(self):
        """`creeper_head` 掉落物、名叫 `creeper_fan` 的玩家都不是威胁（旧兜底的误报）。"""
        self.assertEqual(tier_of({"type": "item", "name": "creeper_head"}), TIER_UNKNOWN)
        self.assertEqual(tier_of({"type": "player", "name": "creeper_fan"}), TIER_UNKNOWN)
        self.assertEqual(hostiles_in([{"type": "item", "name": "creeper_head"},
                                      {"type": "player", "name": "creeper_fan"}]), [])


# ── ③ 认不出的 ⇒ UNKNOWN，不做任何默认 ───────────────────────────

class UnknownTests(unittest.TestCase):
    def test_unknown_is_not_hostile(self):
        row = mob("some_new_mod_mob")
        self.assertEqual(tier_of(row), TIER_UNKNOWN)
        self.assertNotEqual(tier_of(row), TIER_HOSTILE)
        self.assertFalse(threatens_us(row))
        self.assertEqual(tier_of({}), TIER_UNKNOWN)
        self.assertEqual(tier_of(None), TIER_UNKNOWN)

    def test_unknown_is_retaliated_against_only_after_it_struck_first(self):
        # 与 Java `MobPolicy` 同口径：表里没写 ⇒ 不预先禁止（may_initiate 为真），
        # 但**威胁判定**仍然把它排除在外（"认不出就不当敌人"）。
        self.assertTrue(may_initiate(TIER_UNKNOWN))
        self.assertTrue(may_retaliate(TIER_UNKNOWN, True))
        self.assertTrue(may_retaliate(TIER_UNKNOWN, False))
        self.assertFalse(threatens_us(mob("some_new_mod_mob")))

    def test_garbage_tier_values_fall_back_to_the_keywords(self):
        """认不出的 `tier` 值 = 缺字段（不是"认了"）：走兜底，而不是当成某档。"""
        self.assertEqual(tier_of(mob("zombie", tier="??")), TIER_HOSTILE)
        self.assertEqual(tier_of(mob("some_new_mod_mob", tier="??")), TIER_UNKNOWN)


# ── ④ 四档的允许/还手口径（与 Java `MobPolicy` 同表）───────────────

class PolicyLadderTests(unittest.TestCase):
    def test_dangerous_is_never_fought(self):
        for row in (mob("监守者", tier=TIER_DANGEROUS), mob("warden")):
            self.assertEqual(tier_of(row), TIER_DANGEROUS)
            self.assertFalse(may_initiate(tier_of(row)))
            self.assertFalse(may_retaliate(tier_of(row), True),
                             "高危险：连还手都不还手，交回模型/逃生")
            self.assertTrue(threatens_us(row), "但它是威胁：该拉开距离")

    def test_hostile_is_fought(self):
        self.assertTrue(may_initiate(TIER_HOSTILE))
        self.assertTrue(may_retaliate(TIER_HOSTILE, True))
        self.assertTrue(may_retaliate(TIER_HOSTILE, False))

    def test_neutral_is_never_initiated_and_only_retaliates_after_being_hit(self):
        self.assertFalse(may_initiate(TIER_NEUTRAL))
        self.assertFalse(may_retaliate(TIER_NEUTRAL, False))
        self.assertTrue(may_retaliate(TIER_NEUTRAL, True))

    def test_passive_is_huntable_but_harmless(self):
        self.assertTrue(may_initiate(TIER_PASSIVE))
        self.assertFalse(threatens_us(mob("cow", tier=TIER_PASSIVE)))

    def test_villagers_and_traders_are_never_prey(self):
        for row in (mob("村民", tier=TIER_NEUTRAL), mob("villager"), mob("wandering_trader")):
            self.assertFalse(may_initiate(tier_of(row)), row["name"])

    def test_threats_are_dangerous_plus_hostile_only(self):
        rows = [mob("僵尸", tier=TIER_HOSTILE), mob("监守者", tier=TIER_DANGEROUS),
                mob("末影人", tier=TIER_NEUTRAL), mob("牛", tier=TIER_PASSIVE),
                mob("???", tier=TIER_UNKNOWN)]
        self.assertEqual(hostiles_in(rows), ["僵尸", "监守者"])

    def test_bed_attempt_uses_the_same_threat_list(self):
        from agent.territory import bed_attempt

        materials = [{"name": "white_wool", "count": 3}, {"name": "oak_planks", "count": 3}]
        neutral = bed_attempt(materials, [mob("末影人", tier=TIER_NEUTRAL, distance=2.0)])
        hostile = bed_attempt(materials, [mob("僵尸", tier=TIER_HOSTILE, distance=2.0)])

        self.assertEqual(neutral["action"], "craft", "中立怪在旁边不算不安全")
        self.assertEqual(hostile["action"], "unsafe")


# ── ⑤ 生存反射按档位走 ──────────────────────────────────────────

class SelfDefenseTierTests(unittest.TestCase):
    """P3 降级后：**被打才还手**，没挨打绝不先动手（用户口径：后台不许自动开战）。"""

    def test_hostile_in_range_without_being_hurt_does_not_attack(self):
        """(1) 8 格内有敌对、**没挨打** => no frame sent (old code auto-attacked)."""
        engine, skills = engine_with(SelfDefenseMode())
        no_hurt = state([mob("僵尸", tier=TIER_HOSTILE, distance=3.0)], hurt=False)

        engine.tick(no_hurt)
        engine.tick(no_hurt)

        self.assertEqual(skills.calls, [], "must not open fire when not hurt")
        self.assertIsNone(engine.get_status()["active"])

    def test_recent_hurt_makes_it_retaliate(self):
        """(2) recently hurt => retaliate against that hostile."""
        engine, skills = engine_with(SelfDefenseMode())

        engine.tick(state([mob("僵尸", tier=TIER_HOSTILE, distance=3.0)], hurt=True))

        self.assertIn("attack", skills.names())
        self.assertEqual(engine.get_status()["active"], "self_defense")

    def test_health_drop_is_the_proxy_when_no_hurt_field_exists(self):
        """No hurt field in state => fall back to the health-drop proxy."""
        engine, skills = engine_with(SelfDefenseMode())
        hostile = mob("僵尸", tier=TIER_HOSTILE, distance=3.0)

        engine.tick(state([hostile], health=20))      # baseline, no drop -> no attack
        self.assertEqual(skills.calls, [])

        engine.tick(state([hostile], health=15))      # health dropped -> proxy says hurt

        self.assertIn("attack", skills.names())

    def test_self_defense_ignores_a_neutral_mob(self):
        """中立（末影人/狼/铁傀儡）**不进 SelfDefense 名单** —— 用户口径"中立的不惹"。"""
        for row in (mob("末影人", tier=TIER_NEUTRAL, distance=1.0),
                    mob("enderman", distance=1.0),
                    mob("wolf", distance=1.0),
                    mob("iron_golem", distance=1.0)):
            engine, skills = engine_with(SelfDefenseMode())

            engine.tick(state([row], hurt=True))

            self.assertEqual(skills.calls, [], row["name"])
            self.assertIsNone(engine.get_status()["active"], row["name"])

    def test_self_defense_never_touches_a_dangerous_mob(self):
        engine, skills = engine_with(SelfDefenseMode())

        engine.tick(state([mob("监守者", tier=TIER_DANGEROUS, distance=1.0)], hurt=True))

        self.assertEqual(skills.calls, [])

    def test_self_defense_ignores_unknown_and_passive(self):
        engine, skills = engine_with(SelfDefenseMode())

        engine.tick(state([mob("cow", tier=TIER_PASSIVE, distance=1.0),
                           mob("some_new_mod_mob", distance=1.0)], hurt=True))

        self.assertEqual(skills.calls, [])

    def test_legacy_client_without_tier_still_retaliates_against_a_zombie(self):
        engine, skills = engine_with(SelfDefenseMode())

        engine.tick(state([{"type": "hostile", "name": "僵尸", "distance": 3.0}],
                          hurt=True))

        self.assertIn("attack", skills.names())


class CowardiceTierTests(unittest.TestCase):
    def test_a_dangerous_mob_pulls_away_even_at_full_health(self):
        engine, skills = engine_with(CowardiceMode())

        engine.tick(state([mob("监守者", tier=TIER_DANGEROUS, distance=7.0)], health=20))

        self.assertEqual(engine.get_status()["active"], "cowardice")
        self.assertEqual(skills.count("move_to"), 1)

    def test_low_health_flees_a_hostile(self):
        engine, skills = engine_with(CowardiceMode())

        engine.tick(state([mob("僵尸", tier=TIER_HOSTILE, distance=4.0)], health=5))

        self.assertEqual(skills.count("move_to"), 1)

    def test_low_health_does_not_flee_a_neutral_mob(self):
        engine, skills = engine_with(CowardiceMode())

        engine.tick(state([mob("wolf", tier=TIER_NEUTRAL, distance=2.0)], health=5))

        self.assertEqual(skills.calls, [])
        self.assertIsNone(engine.get_status()["active"])

    def test_full_health_ignores_a_distant_dangerous_mob(self):
        engine, skills = engine_with(CowardiceMode())

        engine.tick(state([mob("监守者", tier=TIER_DANGEROUS, distance=40.0)], health=20))

        self.assertEqual(skills.calls, [])

    def test_legacy_client_low_health_still_flees_a_zombie(self):
        engine, skills = engine_with(CowardiceMode())

        engine.tick(state([{"type": "hostile", "name": "僵尸", "distance": 3.0}], health=5))

        self.assertEqual(skills.count("move_to"), 1)


# ── ⑥ 两边真的是同一张表（跨文件契约）───────────────────────────

REPO_ROOT = Path(__file__).resolve().parents[2]
JAVA_POLICY = REPO_ROOT / "src" / "main" / "java" / "com" / "lcu" / "lcumod" / "action" / "MobPolicy.java"
JAVA_REFLEX = JAVA_POLICY.parent / "CombatReflex.java"
JAVA_AUTONOMOUS = JAVA_POLICY.parent / "JavaAutonomousBehavior.java"


class SharedTableTests(unittest.TestCase):
    """后端判据与 Java `MobPolicy` 必须对得上（否则"同一张表"就只是口号）。"""

    @classmethod
    def setUpClass(cls):
        cls.policy = JAVA_POLICY.read_text(encoding="utf-8")
        cls.reflex = JAVA_REFLEX.read_text(encoding="utf-8")
        cls.autonomous = JAVA_AUTONOMOUS.read_text(encoding="utf-8")

    def test_the_java_table_lists_the_same_names(self):
        for name in ("warden", "wither", "ender_dragon", "creeper", "phantom", "enderman",
                     "zombified_piglin", "piglin", "wolf", "bee", "polar_bear", "iron_golem",
                     "snow_golem", "llama", "dolphin", "villager", "wandering_trader",
                     "cow", "pig", "sheep", "chicken", "rabbit"):
            self.assertIn(f'"{name}"', self.policy, name)

    def test_java_exposes_the_same_four_questions(self):
        for method in ("Tier tier(", "boolean mayInitiate(", "boolean mayRetaliate(",
                       "double preferDistance("):
            self.assertIn(method, self.policy, method)

    def test_both_local_attack_paths_go_through_the_table(self):
        self.assertIn("MobPolicy.mayInitiate(", self.reflex)
        self.assertIn("MobPolicy.mayRetaliate(", self.reflex)
        self.assertIn("MobPolicy.mayInitiate(", self.autonomous)
        self.assertIn("MobPolicy.preferDistance(", self.autonomous)

    def test_the_model_sees_the_same_tier_in_its_context(self):
        """L2 的"附近实体"按 `tier` 渲染（旧的 `type` 只是没有 tier 时的回落）。"""
        from agent.planner import Planner

        planner = Planner(llm_service=None, memory=_DummyMemory(), skills=_DummySkills())
        context = {"entities": [mob("狼", tier="NEUTRAL", distance=4.0),
                                {"type": "player", "name": "Steve", "distance": 2.0}]}

        layers = planner.build_prompt_layers("Steve", "hi", context, "AI")

        self.assertIn("neutral:狼@4.0m", layers.user)
        self.assertIn("player:Steve@2.0m", layers.user, "没有 tier 的行仍按 type 渲染")


class _DummyMemory:
    def build_context(self):
        return {}

    def get_recent_context(self, _count):
        return []


class _DummySkills:
    pass


if __name__ == "__main__":
    unittest.main()
