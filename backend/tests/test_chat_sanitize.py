"""出站聊天的字符净化（真机事故回归）。

2026-10-12 16:22 实机：AI 那句话里带了**换行符**（"回复 + 如实说明"拼出来的），
被服务器判为"非法聊天字符"**直接把玩家踢出**。日志原文：

    → send_chat {'message': '…没有执行……）\\n（不知道是哪个基地…）'}

这里的测试钉住三件事：
1. 换行/制表等空白 **修**成空格（不把两句话粘死）；
2. 控制字符 / 零宽 / 双向控制 / 软连字符 / 孤立代理项 / `§` **删**掉；
3. 真实出口 `agent.outbound.sanitize_outbound` 真的过了一遍（不是只在模块里躺着），
   且审计行里带着"到底哪个字符被处理了"（下次不用再靠猜）。
"""

import unittest

from agent.chat_sanitize import CHAT_MAX_CHARS, find_illegal, sanitize_chat
from agent.outbound import sanitize_outbound


class SanitizeChatTests(unittest.TestCase):
    def test_newline_becomes_a_space(self):
        # 真机那条消息的形状
        text = "绑重生点得指名基地。（补一句实话：动作没有执行）\n（不知道是哪个基地）"
        out = sanitize_chat(text)
        self.assertNotIn("\n", out)
        self.assertIn("没有执行", out)
        self.assertIn("不知道是哪个基地", out)
        # 两句话仍然分得开（换行被修成一个空格，且连续空白会压成一个）
        self.assertIn("） （", out)

    def test_all_whitespace_kinds_are_repaired_not_deleted(self):
        for sep in ("\r\n", "\r", "\n", "\t", "\v", "\f", "\u0085", "\u2028", "\u2029"):
            with self.subTest(sep=repr(sep)):
                self.assertEqual(sanitize_chat(f"a{sep}b"), "a b")

    def test_illegal_characters_are_removed(self):
        # § 染色码 / 零宽 / 双向控制 / 软连字符 / C0 / C1 / 孤立代理项
        bad = "\u00a7\u200b\u202e\u00ad\x00\x1b\x7f\u0085x"
        # \u0085（NEL）是空白 ⇒ 变空格；其余删掉
        out = sanitize_chat(f"a{bad}b")
        self.assertEqual(out, "a xb")
        self.assertNotIn("\u00a7", out)

    def test_zero_width_hidden_before_slash_cannot_survive(self):
        # 行首净化在 outbound 里做；这里只保证零宽字符不会活下来
        self.assertEqual(sanitize_chat("\u200b/x"), "/x")

    def test_spaces_collapse_and_trim(self):
        self.assertEqual(sanitize_chat("  a \n\n  b  "), "a b")

    def test_length_is_capped_without_splitting_a_surrogate_pair(self):
        long_text = "啊" * (CHAT_MAX_CHARS + 50)
        out = sanitize_chat(long_text)
        self.assertEqual(len(out), CHAT_MAX_CHARS)
        self.assertTrue(out.endswith("…"))
        # 高代理项收尾（后面没有低代理）必须被切掉，半个码位发出去就是坏数据
        emoji_tail = "🙂" * 300
        out2 = sanitize_chat(emoji_tail, limit=10)
        self.assertLessEqual(len(out2), 10)
        self.assertEqual(out2[-1], "…")
        self.assertFalse(0xD800 <= ord(out2[-2]) <= 0xDBFF)

    def test_clean_text_is_returned_unchanged_and_idempotent(self):
        clean = "来了，这就过去。（顺手把你的镐子放在原地了）"
        self.assertIs(sanitize_chat(clean), clean)      # 快路径：不复制
        self.assertEqual(sanitize_chat(sanitize_chat(clean)), clean)

    def test_none_and_non_string_are_safe(self):
        self.assertEqual(sanitize_chat(None), "")
        self.assertEqual(sanitize_chat(""), "")
        self.assertEqual(sanitize_chat(123), "123")

    def test_find_illegal_reports_the_culprits(self):
        self.assertEqual(find_illegal("a\nb\u00a7c"), ["\n", "\u00a7"])
        self.assertEqual(find_illegal("干净的一句话"), [])


class SanitizeOutboundIntegrationTests(unittest.TestCase):
    """真出口：`sanitize_outbound` 必须同时做"防指令"和"防非法字符"两件事。"""

    def test_real_outbound_choke_point_strips_the_newline(self):
        out = sanitize_outbound("镐子给你。\n（补一句实话：动作没有执行）", kind="send_chat")
        self.assertNotIn("\n", out)
        self.assertIn("没有执行", out)

    def test_real_outbound_choke_point_still_kills_leading_slash(self):
        # 零宽 + 换行 + 斜杠的组合：都不许活下来（否则等于以聊天为名执行指令）
        out = sanitize_outbound("\u200b\n/tp @s ~ ~ ~", kind="send_chat")
        self.assertFalse(out.startswith("/"))
        self.assertNotIn("\n", out)
        self.assertNotIn("\u200b", out)

    def test_audit_row_names_the_illegal_characters(self):
        rows = []
        sanitize_outbound("a\nb\u00a7c", kind="send_chat", observer=rows.append)
        self.assertEqual(len(rows), 1)
        # 审计行里带着"到底哪个字符被处理了"，下次不用再猜
        self.assertEqual(rows[0]["illegal"], ["\n", "\u00a7"])
        self.assertNotIn("\n", rows[0]["cleaned"])


if __name__ == "__main__":
    unittest.main()
