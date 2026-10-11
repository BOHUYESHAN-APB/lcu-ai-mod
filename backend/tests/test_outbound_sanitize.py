"""出站防注入（第一块）：单一清洗函数 / 所有出口都走它 / 清洗即审计。

用户口径：「防止它输入输出的时候尝试输入指令」。硬不变式：**清洗后绝不以 `/` 开头**。
"""

import random
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.outbound import (  # noqa: E402
    add_audit_sink,
    clear_audit_sinks,
    is_safe_outbound,
    sanitize_outbound,
)
from agent.skills import Skills  # noqa: E402


class _Wire:
    """记下每一帧命令的假 wire。"""

    def __init__(self):
        self.frames = []

    def send_command(self, cmd, args):
        self.frames.append((cmd, dict(args)))
        return f"req{len(self.frames)}"


class SanitizeUnitTests(unittest.TestCase):
    def setUp(self):
        clear_audit_sinks()

    def tearDown(self):
        clear_audit_sinks()

    # ── 用户点名的三类输入 ──

    def test_full_width_space_is_stripped(self):
        self.assertEqual(sanitize_outbound("\u3000\u3000/op Steve"), "op Steve")
        self.assertEqual(sanitize_outbound("\u3000你好"), "你好")

    def test_repeated_slashes_are_stripped(self):
        self.assertEqual(sanitize_outbound("//tp Alice"), "tp Alice")
        self.assertEqual(sanitize_outbound("/ /kill"), "kill")
        self.assertEqual(sanitize_outbound("  //  /home  "), "home  ")

    def test_normal_text_is_untouched(self):
        for text in ("你好", "hello /world", "1/2 的伤害", "Steve: 走不走？", ""):
            self.assertEqual(sanitize_outbound(text), text, text)

    def test_non_string_input_is_coerced(self):
        self.assertEqual(sanitize_outbound(None), "")
        self.assertEqual(sanitize_outbound(42), "42")

    def test_sanitize_is_idempotent(self):
        once = sanitize_outbound(" \u3000//op Steve")
        self.assertEqual(sanitize_outbound(once), once)

    # ── 属性式：任意输入清洗后都不以 `/`（或空白）开头 ──

    def test_no_input_survives_with_a_leading_slash(self):
        prefixes = ("", " ", "\u3000", "\t", "//", " / ", "/\u3000/", "\u200b/", "\ufeff//",
                    "\u00a0/", "\u2060/", "／", " ／/")
        bodies = ("", "op me", "/kill", "hello", "／cmd", "/ / /", "  spaced  ")
        for prefix in prefixes:
            for body in bodies:
                out = sanitize_outbound(prefix + body)
                self.assertFalse(out.startswith("/"), f"{prefix + body!r} → {out!r}")
                self.assertFalse(out[:1].isspace(), f"{prefix + body!r} → {out!r}")
                self.assertTrue(is_safe_outbound(out))
                self.assertEqual(sanitize_outbound(out), out)

    def test_fuzz_arbitrary_strings_never_start_with_slash(self):
        alphabet = "/ \u3000\t\u200babZ01/／"
        rng = random.Random(20261011)
        for _ in range(600):
            raw = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 12)))
            out = sanitize_outbound(raw)
            self.assertFalse(out.startswith("/"), repr(raw))
            self.assertTrue(is_safe_outbound(out), repr(raw))

    # ── 审计：洗了就记（谁/原文/清洗后），没洗不记 ──

    def test_audit_sink_records_original_and_cleaned(self):
        rows = []
        add_audit_sink(rows.append)

        sanitize_outbound(" \u3000//op Steve", kind="send_chat", actor="AI")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["original"], " \u3000//op Steve")
        self.assertEqual(rows[0]["cleaned"], "op Steve")
        self.assertEqual(rows[0]["kind"], "send_chat")
        self.assertEqual(rows[0]["actor"], "AI")

        sanitize_outbound("你好", kind="send_chat")
        self.assertEqual(len(rows), 1, "没清洗就不该记审计")

    def test_broken_sink_does_not_break_outbound(self):
        def boom(_row):
            raise RuntimeError("sink down")

        add_audit_sink(boom)
        self.assertEqual(sanitize_outbound("/op Steve"), "op Steve")


class SkillsOutletTests(unittest.TestCase):
    """所有出口（send_chat / set_hud_note / run_command / click_screen_button）都走清洗。"""

    def setUp(self):
        clear_audit_sinks()
        self.wire = _Wire()
        self.skills = Skills(self.wire)

    def tearDown(self):
        clear_audit_sinks()

    def test_send_chat_strips_leading_slash(self):
        self.skills.send_chat("\u3000//op Steve")

        cmd, args = self.wire.frames[-1]
        self.assertEqual(cmd, "send_chat")
        self.assertEqual(args["message"], "op Steve")
        self.assertFalse(args["message"].startswith("/"))

    def test_send_chat_leaves_normal_reply_alone(self):
        self.skills.send_chat("好啊，我这就过去")

        self.assertEqual(self.wire.frames[-1][1]["message"], "好啊，我这就过去")

    def test_set_hud_note_is_sanitized(self):
        self.skills.set_hud_note(" /挖矿中")

        self.assertEqual(self.wire.frames[-1][1]["text"], "挖矿中")

    def test_run_command_strips_slashes_and_newlines(self):
        self.skills.run_command(" //locate biome minecraft:desert  ")

        cmd, args = self.wire.frames[-1]
        self.assertEqual(cmd, "run_command")
        self.assertEqual(args["command"], "locate biome minecraft:desert")

        self.skills.run_command("/say hi\ntp Alice")
        self.assertNotIn("\n", self.wire.frames[-1][1]["command"])
        self.assertEqual(self.wire.frames[-1][1]["command"], "say hi tp Alice")

    def test_click_screen_button_uses_match_argument(self):
        self.skills.click_screen_button("接受")

        cmd, args = self.wire.frames[-1]
        self.assertEqual(cmd, "click_screen_button")
        self.assertEqual(args, {"match": "接受"})

    def test_observer_is_told_which_outlet_was_cleaned(self):
        rows = []
        self.skills.set_outbound_observer(rows.append)

        self.skills.send_chat("/op me")

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["kind"], "send_chat")
        self.assertEqual(rows[0]["cleaned"], "op me")


if __name__ == "__main__":
    unittest.main()
