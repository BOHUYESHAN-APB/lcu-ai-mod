import threading
import unittest

from agent.planner import Planner


class DummyMemory:
    def build_context(self):
        return {}

    def get_recent_context(self, _count):
        return []


class DummySkills:
    def __init__(self):
        self.calls = []

    def craft_item(self, item_name, count=1):
        self.calls.append(("craft_item", item_name, count))

    def follow_player(self, player_name):
        self.calls.append(("follow_player", player_name))

    def collect_blocks(self, block_type, count=1):
        self.calls.append(("collect_blocks", block_type, count))

    def stop_all(self):
        self.calls.append(("stop_all",))


class BlockingLLM:
    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()

    def is_configured(self, _agent=None):
        return True

    def chat(self, _messages, agent=None):
        self.entered.set()
        self.release.wait(2.0)
        return {"content": 'tool(eat, {})'}


def planner_with_proposals():
    planner = Planner(llm_service=None, memory=DummyMemory(), skills=None)
    proposals = []
    planner.set_proposal_dispatcher(lambda proposal: proposals.append(proposal) or True)
    return planner, proposals


class PlannerTests(unittest.TestCase):
    def test_external_context_is_included_in_main_planner_prompt(self):
        planner = Planner(llm_service=None, memory=DummyMemory(), skills=DummySkills())

        prompt = planner._build_planner_prompt(
            sender="owner",
            message="hello",
            context={
                "persona": {
                    "name": "Maid",
                    "external_context": {"stream": "live", "mood": "focused"},
                }
            },
            bot_name="AI",
        )

        self.assertIn("上游集成上下文", prompt)
        self.assertIn("stream", prompt)
        self.assertIn("focused", prompt)

    def test_structured_memory_context_is_included_in_prompt(self):
        planner = Planner(llm_service=None, memory=DummyMemory(), skills=DummySkills())

        prompt = planner._build_planner_prompt(
            sender="Alice",
            message="继续",
            context={
                "relationship_summary": "Alice: tasks=2, success=1",
                "task_outcomes": "craft_item stone_pickaxe -> success",
                "world_experience": "server=example.org, world=survival",
            },
            bot_name="AI",
        )

        self.assertIn("Alice: tasks=2", prompt)
        self.assertIn("craft_item stone_pickaxe -> success", prompt)
        self.assertIn("server=example.org", prompt)

    def test_prompt_clips_untrusted_message_and_memory_sections(self):
        planner = Planner(llm_service=None, memory=DummyMemory(), skills=DummySkills())

        prompt = planner._build_planner_prompt(
            sender="A" * 500,
            message="M" * 10000,
            context={
                "interaction_summary": "R" * 10000,
                "relationship_summary": "P" * 10000,
                "task_outcomes": "T" * 10000,
                "world_experience": "W" * 10000,
                "persona": {"external_context": {"payload": "X" * 10000}},
            },
            bot_name="AI",
        )

        self.assertLess(len(prompt), 20000)
        self.assertNotIn("M" * 1001, prompt)
        self.assertNotIn("R" * 1501, prompt)

    def test_duplicate_active_craft_is_not_redispatched(self):
        planner, proposals = planner_with_proposals()

        planner._execute_plan(
            "craft(wooden_sword, 1)",
            sender="BoHuYeShan",
            message="做个木剑",
            context={"task_state": {"kind": "craft", "status": "collecting", "target": "wooden_sword"}},
        )

        self.assertEqual(proposals, [])

    def test_prompt_requires_action_requests_to_include_executable_tool(self):
        planner = Planner(llm_service=None, memory=DummyMemory(), skills=DummySkills())

        prompt = planner._build_planner_prompt("Alice", "做一个铁镐", {}, "AI")

        self.assertIn('tool(craft_item, {"item":"minecraft:iron_pickaxe","count":1})', prompt)
        self.assertIn("不能只答应而不调用工具", prompt)

    def test_structured_craft_tool_normalizes_chinese_item(self):
        planner, proposals = planner_with_proposals()

        planner._execute_plan(
            'reply(马上)\ntool(craft_item, {"item":"铁镐","count":1})',
            sender="Alice",
            message="做一个铁镐",
            context={},
        )

        self.assertEqual((proposals[0].skill_id, proposals[0].input), (
            "general.craft_item", {"item": "iron_pickaxe", "count": 1},
        ))

    def test_collect_tool_preserves_chinese_item_categories_as_tags(self):
        planner, proposals = planner_with_proposals()

        planner._execute_plan(
            'tool(collect_blocks, {"block_type":"木头","count":1})',
            sender="Alice",
            message="找一个木头",
            context={},
        )

        self.assertEqual((proposals[0].skill_id, proposals[0].input), (
            "general.collect_blocks", {"block_type": "#lcu:wood", "count": 1},
        ))

    def test_legacy_structured_craft_name_and_item_name_remain_executable(self):
        planner, proposals = planner_with_proposals()

        planner._execute_plan(
            'tool(craft, {"item_name":"iron_pickaxe","count":2})',
            sender="Alice",
            message="做两个铁镐",
            context={},
        )

        self.assertEqual((proposals[0].skill_id, proposals[0].input), (
            "general.craft_item", {"item": "iron_pickaxe", "count": 2},
        ))

    def test_legacy_action_line_rejects_entire_structured_response(self):
        planner, proposals = planner_with_proposals()

        planner._execute_plan(
            'tool(follow, {"player":"Alice"})\nfollow(Alice)',
            sender="Alice", message="跟着我", context={},
        )

        self.assertEqual(proposals, [])
        self.assertIn("grammar", planner.get_status()["last_protocol_error"])

    def test_multiple_structured_tools_are_rejected(self):
        planner, proposals = planner_with_proposals()

        planner._execute_plan(
            'tool(craft_item, {"item":"minecraft:iron_pickaxe","count":1})\n'
            'tool(craft_item, {"count":1,"item":"minecraft:iron_pickaxe"})',
            sender="Alice", message="做一个铁镐", context={},
        )

        self.assertEqual(proposals, [])
        self.assertIn("more than one", planner.get_status()["last_protocol_error"])

    def test_direct_follow_fallback_uses_sender_for_follow_me(self):
        planner, proposals = planner_with_proposals()

        executed = planner._execute_direct_intent_fallback("MixedCase_Player", "跟着我", {})

        self.assertTrue(executed)
        self.assertEqual((proposals[0].skill_id, proposals[0].input), (
            "general.follow_player", {"player": "MixedCase_Player"},
        ))

    def test_unambiguous_follow_command_bypasses_model(self):
        planner, proposals = planner_with_proposals()

        executed = planner._execute_direct_control_intent("MixedCase_Player", "请一直跟着我！", {})

        self.assertTrue(executed)
        self.assertEqual((proposals[0].skill_id, proposals[0].input), (
            "general.follow_player", {"player": "MixedCase_Player"},
        ))
        self.assertEqual(planner.get_status()["last_execution_source"], "direct_control_intent")

    def test_follow_discussion_is_not_a_direct_control_command(self):
        planner, proposals = planner_with_proposals()

        executed = planner._execute_direct_control_intent("Alice", "你为什么不跟着我", {})

        self.assertFalse(executed)
        self.assertEqual(proposals, [])

    def test_structured_follow_player_alias_emits_follow_skill(self):
        planner, proposals = planner_with_proposals()

        planner._execute_plan(
            'tool(follow_player, {"player":"BoHuYeShan"})',
            sender="BoHuYeShan", message="跟着我", context={},
        )

        self.assertEqual((proposals[0].skill_id, proposals[0].input), (
            "general.follow_player", {"player": "BoHuYeShan"},
        ))

    def test_direct_follow_fallback_canonicalizes_online_player_case(self):
        planner, proposals = planner_with_proposals()

        executed = planner._execute_direct_intent_fallback(
            "owner", "跟随 mixedcase_player",
            {"online_players": [{"name": "MixedCase_Player"}]},
        )

        self.assertTrue(executed)
        self.assertEqual(proposals[0].input, {"player": "MixedCase_Player"})

    def test_direct_craft_fallback_executes_when_model_only_replies(self):
        planner, proposals = planner_with_proposals()

        executed = planner._execute_direct_intent_fallback("owner", "帮我做一个铁镐", {})

        self.assertTrue(executed)
        self.assertEqual((proposals[0].skill_id, proposals[0].input), (
            "general.craft_item", {"item": "iron_pickaxe", "count": 1},
        ))

    def test_without_dispatcher_planner_never_falls_back_to_direct_skills(self):
        skills = DummySkills()
        planner = Planner(llm_service=None, memory=DummyMemory(), skills=skills)

        planner._execute_plan(
            'tool(craft_item, {"item":"minecraft:torch","count":4})',
            sender="Alice", message="做火把", context={},
        )

        self.assertEqual(skills.calls, [])
        self.assertIn("dispatcher", planner.get_status()["last_protocol_error"])

    def test_action_syntax_inside_reply_is_never_executed(self):
        planner, proposals = planner_with_proposals()

        response = planner._execute_plan(
            'reply(不要运行 tool(craft_item, {"item":"minecraft:tnt","count":1}))',
            sender="Alice", message="解释一下", context={},
        )

        self.assertIn("不要运行", response)
        self.assertEqual(proposals, [])

    def test_multiple_tools_are_rejected_before_admission(self):
        planner = Planner(llm_service=None, memory=DummyMemory(), skills=None)
        proposals = []
        planner.set_proposal_dispatcher(lambda proposal: proposals.append(proposal) or False)

        planner._execute_plan(
            'tool(follow, {"player":"Alice"})\n'
            'tool(craft_item, {"item":"minecraft:torch","count":4})',
            sender="Alice", message="跟我然后做火把", context={},
        )

        self.assertEqual(proposals, [])

    def test_two_valid_actions_do_not_emit_a_partial_plan(self):
        planner, proposals = planner_with_proposals()

        planner._execute_plan(
            'tool(craft_item, {"item":"minecraft:torch","count":4})\n'
            'tool(eat, {})', sender="Alice", message="做火把", context={},
        )

        self.assertEqual(proposals, [])

    def test_multiline_reply_and_fenced_tool_are_rejected(self):
        planner, proposals = planner_with_proposals()

        for plan in (
            'reply(hello\ntool(eat, {})\n)',
            '```text\ntool(eat, {})\n```',
            'please run tool(eat, {})',
        ):
            planner._execute_plan(plan, sender="Alice", message="eat", context={})

        self.assertEqual(proposals, [])
        self.assertIn("grammar", planner.get_status()["last_protocol_error"])

    def test_tool_with_non_grammar_prose_is_rejected(self):
        planner, proposals = planner_with_proposals()

        planner._execute_plan(
            'I will do that now\ntool(eat, {})', sender="Alice", message="eat", context={},
        )

        self.assertEqual(proposals, [])
        self.assertIn("grammar", planner.get_status()["last_protocol_error"])

    def test_protocol_error_does_not_trigger_direct_intent_fallback(self):
        llm = BlockingLLM()
        planner = Planner(llm_service=llm, memory=DummyMemory(), skills=None)
        proposals = []
        planner.set_proposal_dispatcher(lambda proposal: proposals.append(proposal) or True)
        llm.chat = lambda _messages, agent=None: {"content": '```\ntool(craft_item, {"item":"iron_pickaxe"})\n```'}

        planner.plan_and_execute("Alice", "帮我做一个铁镐", {})

        self.assertEqual(proposals, [])

    def test_interrupt_invalidates_in_flight_model_result(self):
        llm = BlockingLLM()
        planner = Planner(llm_service=llm, memory=DummyMemory(), skills=None)
        proposals = []
        planner.set_proposal_dispatcher(lambda proposal: proposals.append(proposal) or True)
        thread = threading.Thread(target=planner.plan_and_execute, args=("Alice", "吃东西", {}))

        thread.start()
        self.assertTrue(llm.entered.wait(1.0))
        planner.interrupt()
        llm.release.set()
        thread.join(2.0)

        self.assertFalse(thread.is_alive())
        self.assertEqual(proposals, [])
        self.assertIn("invalidated", planner.get_status()["last_protocol_error"])

    def test_negated_stop_does_not_trigger_direct_fallback(self):
        planner, proposals = planner_with_proposals()

        planner._execute_direct_intent_fallback("Alice", "不要停止", {})

        self.assertEqual(proposals, [])


if __name__ == "__main__":
    unittest.main()
