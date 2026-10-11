"""persona_store 的回归测试。

覆盖：默认值的中立性/第二人称/字数、缺文件与损坏 JSON 的回退、
三段块与 state_block 的分离、场景规则抽取、备选风格的按概率替换、
set() 落盘与未知键忽略。LLM 不参与，全部离线。
"""

import json
import tempfile
import unittest
from pathlib import Path

from agent import persona_store as ps
from agent.persona_store import (DEFAULT_PERSONA_PATH, SCENES, PersonaStore,
                                 _default_persona)

# 常见厂商 / 角色 IP：默认人设里一个都不许出现（大小写不敏感）。
BANNED_VENDORS = [
    "openai", "chatgpt", "gpt", "claude", "anthropic", "gemini", "google",
    "microsoft", "deepseek", "maibot", "qwen", "llama", "meta", "apple",
    "siri", "alexa", "xiaomi", "mimo", "通义", "文心", "豆包", "kimi", "派蒙", "原神",
]


class FakeRng:
    """固定随机：random() 恒返回 value，choice() 按 pick 取下标。"""

    def __init__(self, value: float = 0.0, pick: int = 0):
        self.value = value
        self.pick = pick
        self.random_calls = 0

    def random(self) -> float:
        self.random_calls += 1
        return self.value

    def choice(self, seq):
        return seq[self.pick % len(seq)]


class _TempStoreCase(unittest.TestCase):
    """每个用例一个临时目录（不碰仓库里的 .local）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "persona.json"
        self.store = PersonaStore(self.path)


class DefaultsTest(unittest.TestCase):
    def test_path_and_scenes(self):
        self.assertEqual(
            DEFAULT_PERSONA_PATH,
            Path(ps.__file__).parent.parent / ".local" / "persona.json",
        )
        self.assertEqual(SCENES, ("private", "public", "mentioned"))

    def test_identity_is_second_person_and_in_range(self):
        ident = _default_persona()["identity"]
        self.assertTrue(ident.startswith("你"), ident)
        self.assertGreaterEqual(len(ident), 80)
        self.assertLessEqual(len(ident), 220)

    def test_defaults_are_neutral(self):
        data = _default_persona()
        for field in ("identity", "reply_style", "scene_rules"):
            low = data[field].lower()
            for bad in BANNED_VENDORS:
                self.assertNotIn(bad, low, f"{field} 含厂商/IP 名 {bad!r}")

    def test_default_scalar_values(self):
        data = _default_persona()
        self.assertEqual(data["name"], "AI")
        self.assertEqual(data["wake_names"], ["AI", "小A"])
        self.assertEqual(data["multiple_reply_style"], [])
        self.assertEqual(data["multiple_probability"], 0.0)

    def test_defaults_are_fresh_copies(self):
        first = _default_persona()
        first["wake_names"].append("污染")
        self.assertNotIn("污染", _default_persona()["wake_names"])


class FallbackTest(_TempStoreCase):
    def test_missing_file_uses_default(self):
        # 文件根本不存在：默认人设可用且不抛。
        self.assertFalse(self.path.exists())
        self.assertEqual(self.store.identity(), _default_persona()["identity"])
        self.assertEqual(self.store.get()["name"], "AI")

    def test_corrupt_json_falls_back(self):
        self.path.write_text("{ 这不是 JSON ,,,", encoding="utf-8")
        store = PersonaStore(self.path)
        self.assertEqual(store.identity(), _default_persona()["identity"])
        # reload 也不抛，仍回退默认。
        self.assertEqual(store.reload()["identity"], _default_persona()["identity"])

    def test_non_object_json_falls_back(self):
        self.path.write_text("[1, 2, 3]", encoding="utf-8")
        store = PersonaStore(self.path)
        self.assertEqual(store.get()["name"], "AI")

    def test_path_is_directory_does_not_raise(self):
        target = Path(self._tmp.name) / "a_dir"
        target.mkdir()
        store = PersonaStore(target)
        self.assertTrue(store.identity())
        # 写不进目录也只 warning，不向外抛。
        merged = store.set({"name": "改名"})
        self.assertEqual(merged["name"], "改名")


class PromptBlocksTest(_TempStoreCase):
    STATE = {
        "health": 1, "max_health": 20, "hunger": 4, "saturation": 0.5,
        "task": {"kind": "mine", "status": "running", "target": "石头", "progress": "1/64"},
        "listening_to": "Steve",
    }

    def test_four_independent_blocks(self):
        blocks = self.store.prompt_blocks(self.STATE, "private")
        self.assertEqual(
            set(blocks),
            {"identity", "reply_style", "scene_rules", "state_block"},
        )
        self.assertEqual(blocks["identity"], self.store.identity())
        self.assertEqual(blocks["reply_style"], self.store.reply_style())
        self.assertEqual(blocks["scene_rules"], self.store.scene_rules("private"))

    def test_state_does_not_leak_into_identity(self):
        blocks = self.store.prompt_blocks(self.STATE)
        for token in ("生命", "饥饿", "饱和度", "Steve", "1/64", "当前任务"):
            self.assertNotIn(token, blocks["identity"])
        # 持久层也不该出现运行时状态。
        self.assertNotIn("state_block", self.store.get())

    def test_state_block_carries_runtime_facts(self):
        sb = self.store.prompt_blocks(self.STATE)["state_block"]
        self.assertIn("生命 1/20", sb)
        self.assertIn("饥饿 4/20", sb)
        self.assertIn("饱和度 0.5", sb)
        self.assertIn("当前任务：挖矿中（mine 石头 1/64）", sb)
        self.assertIn("正在听你说的是：Steve", sb)

    def test_missing_state_keys_tolerated(self):
        self.assertEqual(self.store.state_block({}), "")
        self.assertEqual(self.store.state_block(None), "")
        partial = self.store.state_block({"health": 5})
        self.assertIn("生命 5/20", partial)
        self.assertNotIn("饥饿", partial)
        self.assertNotIn("当前任务", partial)

    def test_finished_task_not_shown(self):
        sb = self.store.state_block({"task": {"kind": "mine", "status": "done"}})
        self.assertNotIn("当前任务", sb)


class ReplyStyleTest(_TempStoreCase):
    def test_probability_one_uses_alternative(self):
        for pick, expect in ((0, "风格甲"), (1, "风格乙")):
            store = PersonaStore(self.path, rng=FakeRng(value=0.0, pick=pick))
            store.set({"multiple_reply_style": ["风格甲", "风格乙"], "multiple_probability": 1.0})
            self.assertEqual(store.reply_style(), expect)

    def test_probability_zero_keeps_main(self):
        store = PersonaStore(self.path, rng=FakeRng(value=0.0, pick=0))
        main = store.set({"multiple_reply_style": ["风格甲"], "multiple_probability": 0.0})["reply_style"]
        self.assertEqual(store.reply_style(), main)
        self.assertEqual(store.reply_style(), main)  # 稳定，不会随机漂

    def test_high_roll_keeps_main(self):
        store = PersonaStore(self.path, rng=FakeRng(value=0.99, pick=1))
        main = store.set({"multiple_reply_style": ["风格甲", "风格乙"], "multiple_probability": 0.5})["reply_style"]
        self.assertEqual(store.reply_style(), main)

    def test_empty_alternatives_keep_main(self):
        self.assertEqual(self.store.reply_style(), self.store.get()["reply_style"])

    def test_probability_clamped(self):
        self.assertEqual(self.store.set({"multiple_probability": 5.0})["multiple_probability"], 1.0)
        self.assertEqual(self.store.set({"multiple_probability": -3})["multiple_probability"], 0.0)
        self.assertEqual(self.store.set({"multiple_probability": "abc"})["multiple_probability"], 0.0)


class SceneRulesTest(_TempStoreCase):
    def test_default_scenes_distinct_and_nonempty(self):
        parts = {scene: self.store.scene_rules(scene) for scene in SCENES}
        for scene, part in parts.items():
            self.assertTrue(part.strip(), scene)
        self.assertEqual(len(set(parts.values())), 3)
        self.assertNotIn("公开服", parts["private"])
        self.assertNotIn("被点名", parts["public"])

    def test_unknown_scene_returns_whole(self):
        whole = self.store.scene_rules()
        self.assertEqual(self.store.scene_rules("group"), whole)
        self.assertEqual(self.store.scene_rules(None), whole)

    def test_custom_sections(self):
        self.store.set({
            "scene_rules": "总则：看情况。\n- 私聊：只对你说。\n- 公开服：只看不说。\n- 被点名：先确认。\n",
        })
        self.assertEqual(self.store.scene_rules("private"), "只对你说。")
        self.assertEqual(self.store.scene_rules("public"), "只看不说。")
        self.assertEqual(self.store.scene_rules("mentioned"), "先确认。")
        self.assertIn("总则", self.store.scene_rules())

    def test_unsectioned_rules_return_whole(self):
        text = "一段没有分节的规则，怎么说话全看情况。"
        self.store.set({"scene_rules": text})
        self.assertEqual(self.store.scene_rules("private"), text)


class SetPersistTest(_TempStoreCase):
    def test_set_persists_and_ignores_unknown_keys(self):
        out = self.store.set({
            "name": "伴侣", "identity": "你是新身份。",
            "wake_names": ["喂"], "nonsense": 123,
        })
        self.assertEqual(out["name"], "伴侣")
        self.assertEqual(out["identity"], "你是新身份。")
        self.assertNotIn("nonsense", out)
        self.assertEqual(set(out), set(ps.KNOWN_KEYS))
        # 新建 store 读得回。
        again = PersonaStore(self.path).get()
        self.assertEqual(again["name"], "伴侣")
        self.assertEqual(again["identity"], "你是新身份。")
        self.assertEqual(again["wake_names"], ["喂"])
        # 文件确实是合法 JSON 且中文未被转义。
        self.assertIn("伴侣", self.path.read_text(encoding="utf-8"))

    def test_get_is_deep_copy(self):
        data = self.store.get()
        data["wake_names"].append("污染")
        data["identity"] = "被外部改掉"
        self.assertNotIn("污染", self.store.get()["wake_names"])
        self.assertNotEqual(self.store.identity(), "被外部改掉")

    def test_set_non_dict_returns_current(self):
        self.assertEqual(self.store.set(None)["identity"], self.store.identity())

    def test_reload_picks_up_external_edit(self):
        self.store.set({"identity": "旧身份"})
        self.path.write_text(json.dumps({"identity": "外面改的"}, ensure_ascii=False), encoding="utf-8")
        self.assertEqual(self.store.identity(), "旧身份")  # 未 reload 不自动变
        self.assertEqual(self.store.reload()["identity"], "外面改的")

    def test_status_shape(self):
        self.store.set({"multiple_reply_style": ["a", "b"], "multiple_probability": 0.5})
        status = self.store.status()
        self.assertEqual(
            set(status),
            {"path", "name", "identity_chars", "reply_style_chars",
             "multiple_count", "scene_rules_chars"},
        )
        self.assertEqual(status["path"], str(self.path))
        self.assertEqual(status["multiple_count"], 2)
        self.assertEqual(status["identity_chars"], len(self.store.identity()))
        self.assertEqual(status["scene_rules_chars"], len(self.store.scene_rules()))


if __name__ == "__main__":
    unittest.main()
