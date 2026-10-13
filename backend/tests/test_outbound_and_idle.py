"""P2 公屏文案（剥 markdown / 不报坐标）+ P1 闲置自主活动（只注入自提示，绝不自己动手）。

真机事故（2026-10-12 21:20）：AI 把内部说明里的 `**加粗**` 与床的坐标原样发上了公屏。
真机原话（同日晚）："它不会自主活动了……本来它在一个地方站立发呆的时候，可能会主动往其他地方移动。"

这一批的纪律：**机械剥离，不是内容审查**（正文一字不动）；**坐标只进日志/后端**（私聊不算公屏）；
闲置活动只做"计时 + 限频"，动不动、动什么**由模型决定**（不新开线程/定时器、绝不自己下发动作）。
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.outbound import (COORDS_REDACTED, hide_coordinates, mentions_coordinates,  # noqa: E402
                            sanitize_outbound, strip_markdown)
from agent.self_prompter import DEFAULT_IDLE_PROMPT, SelfPrompter  # noqa: E402


class StripMarkdownTests(unittest.TestCase):
    """剥的是**标记本身**，正文一字不动。"""

    def test_the_exact_line_that_leaked_to_public_chat(self):
        self.assertEqual(strip_markdown("现在走过去**右键一次**那张床"),
                         "现在走过去右键一次那张床")

    def test_line_leading_markers_go_but_the_text_stays(self):
        for raw, want in (("# 标题", "标题"), ("> 引用", "引用"), ("- 列表项", "列表项"),
                          ("1. 列表项", "列表项"), ("  ## 缩进的标题", "缩进的标题")):
            self.assertEqual(strip_markdown(raw), want, raw)

    def test_inline_markers_go_but_inner_text_stays(self):
        self.assertEqual(strip_markdown("用 `set_home` 绑一下"), "用 set_home 绑一下")
        self.assertEqual(strip_markdown("~~删掉~~这行"), "删掉这行")
        self.assertEqual(strip_markdown("__强调__"), "强调")

    def test_it_is_not_content_censorship(self):
        """单个 `*`/`_` 不是标记：`2 * 3`、文件名里的下划线都要留着。"""
        for raw in ("2 * 3 = 6", "my_file_name", "a_b_c", "注意：*这里*不动"):
            self.assertEqual(strip_markdown(raw), raw, raw)


class HideCoordinatesTests(unittest.TestCase):
    """坐标只进日志/后端；判据同一个（`mentions_coordinates`）。"""

    def test_parenthesised_and_bare_triples_are_hidden(self):
        for raw in ("床在 (851,63,-2191)", "床在（851, 63, -2191）", "去 851,63,-2191 那里",
                    "坐标 851.5, 63.0, -2191.25"):
            self.assertFalse(mentions_coordinates(hide_coordinates(raw)), raw)
            self.assertIn(COORDS_REDACTED, hide_coordinates(raw), raw)

    def test_single_numbers_and_pairs_are_left_alone(self):
        for raw in ("还剩 3 个", "版本 26.3", "x=851 y=63 z=-2191", "比例 1:2", "价格 12, 13"):
            self.assertEqual(hide_coordinates(raw), raw, raw)

    def test_it_is_idempotent(self):
        once = hide_coordinates("去 (1,2,3) 那里")
        self.assertEqual(hide_coordinates(once), once)


class SanitizeOutboundTests(unittest.TestCase):
    """公屏口径 vs 私聊口径：只有 `kind="send_chat"` 才藏坐标。"""

    def test_public_chat_gets_markdown_stripped_and_coordinates_hidden(self):
        out = sanitize_outbound("行，我去旁边那张红床上绑一下。（在附近找到床 (851,63,-2191)）",
                                kind="send_chat")

        self.assertNotIn("**", out)
        self.assertFalse(mentions_coordinates(out), out)
        self.assertIn(COORDS_REDACTED, out)

    def test_a_whisper_is_not_public_chat(self):
        out = sanitize_outbound("床在 (851,63,-2191)", kind="whisper")

        self.assertTrue(mentions_coordinates(out), "私聊不算公屏，坐标照旧")

    def test_the_original_still_reaches_the_audit_sink(self):
        """坐标只从**公屏**上消失 —— 审计里必须留着原文（不静默丢）。"""
        seen: list[dict] = []
        sanitize_outbound("床在 (851,63,-2191)", kind="send_chat", actor="Owner",
                          observer=seen.append)

        self.assertTrue(seen, "清洗发生了就必须有审计")
        self.assertIn("(851,63,-2191)", str(seen[0].get("original")))

    def test_it_stays_idempotent_and_never_starts_with_a_slash(self):
        once = sanitize_outbound("/tp 过来 **快**", kind="send_chat")

        self.assertFalse(once.startswith("/"))
        self.assertEqual(sanitize_outbound(once, kind="send_chat"), once)


class _Clock:
    def __init__(self, now: float = 1000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class IdlePromptTests(unittest.TestCase):
    """闲置自主活动：只返回一条**自提示**，由调用方去请模型；这里绝不下发任何动作。"""

    def make(self, clock: _Clock, **config) -> SelfPrompter:
        base = {"enabled": True, "after_seconds": 30.0, "min_gap_seconds": 60.0,
                "max_per_hour": 3, "prompt": ""}
        base.update(config)
        return SelfPrompter(idle_config_provider=lambda: base, clock=clock)

    def test_nothing_is_injected_while_busy_or_not_idle(self):
        clock = _Clock()
        prompter = self.make(clock)
        prompter.mark_action()
        clock.advance(600)

        self.assertEqual(prompter.idle_prompt(is_idle=False, action_busy=False), "")
        self.assertEqual(prompter.idle_prompt(is_idle=True, action_busy=True), "")
        self.assertEqual(prompter.idle_status()["prompts_last_hour"], 0, "没注入就不许记一笔")

    def test_it_waits_the_configured_idle_time(self):
        clock = _Clock()
        prompter = self.make(clock)
        prompter.mark_action()

        clock.advance(29.0)
        self.assertEqual(prompter.idle_prompt(is_idle=True, action_busy=False), "")

        clock.advance(2.0)
        self.assertEqual(prompter.idle_prompt(is_idle=True, action_busy=False),
                         DEFAULT_IDLE_PROMPT)

    def test_it_is_rate_limited_per_hour_and_by_gap(self):
        clock = _Clock()
        prompter = self.make(clock, min_gap_seconds=60.0, max_per_hour=3)
        prompter.mark_action()

        clock.advance(31.0)
        self.assertTrue(prompter.idle_prompt(is_idle=True, action_busy=False))
        self.assertEqual(prompter.idle_prompt(is_idle=True, action_busy=False), "",
                         "刚注入过就别连着注入")

        clock.advance(61.0)
        self.assertTrue(prompter.idle_prompt(is_idle=True, action_busy=False))
        clock.advance(61.0)
        self.assertTrue(prompter.idle_prompt(is_idle=True, action_busy=False))
        clock.advance(61.0)
        self.assertEqual(prompter.idle_prompt(is_idle=True, action_busy=False), "",
                         "一小时上限用满就不再注入")

        clock.advance(3600.0)
        self.assertTrue(prompter.idle_prompt(is_idle=True, action_busy=False), "窗口滑出去后恢复")

    def test_the_window_is_bounded(self):
        clock = _Clock()
        prompter = self.make(clock, min_gap_seconds=0.0, max_per_hour=12)
        prompter.mark_action()
        for _ in range(50):
            clock.advance(61.0)
            prompter.idle_prompt(is_idle=True, action_busy=False)

        self.assertLessEqual(len(prompter._idle_prompts), 12, "只留一小时的窗口，有界")

    def test_an_explicit_prompt_is_used_verbatim_and_disabled_means_silence(self):
        clock = _Clock()
        prompter = self.make(clock, prompt="去看看熔炉", after_seconds=0.0)
        self.assertEqual(prompter.idle_prompt(is_idle=True, action_busy=False), "去看看熔炉")

        off = self.make(_Clock(), enabled=False, after_seconds=0.0)
        self.assertEqual(off.idle_prompt(is_idle=True, action_busy=False), "")

    def test_injecting_counts_as_activity_so_it_does_not_retrigger_immediately(self):
        clock = _Clock()
        prompter = self.make(clock, after_seconds=30.0, min_gap_seconds=0.0)
        prompter.mark_action()
        clock.advance(31.0)
        prompter.idle_prompt(is_idle=True, action_busy=False)

        self.assertEqual(prompter.idle_prompt(is_idle=True, action_busy=False), "",
                         "注入即算有动静，别几秒内重复打扰")

    def test_status_is_honest_before_anything_happened(self):
        prompter = SelfPrompter(idle_config_provider=lambda: {}, clock=_Clock())

        self.assertIsNone(prompter.idle_status()["idle_seconds"], "没动过就别说闲了多久")


if __name__ == "__main__":
    unittest.main()
