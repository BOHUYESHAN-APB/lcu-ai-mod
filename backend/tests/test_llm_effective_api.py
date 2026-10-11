"""两个"让你一眼看出配置有没有生效"的接口，用 TestClient 直接打（不起 lifespan，故不会连模组）。

背景：用户改了 key/URL 却仍在打旧地址 —— 因为角色级旧值把 default 覆盖了，
且界面上只看得到"你填了什么"，看不到"后端此刻真正在用的是什么"。
"""
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

import server  # noqa: E402


class EffectiveConfigEndpointTests(unittest.TestCase):
    def setUp(self):
        # 不进入 with 上下文 → 不触发 startup（避免后台重连线程）
        self.client = TestClient(server.app)

    def test_effective_endpoint_reports_every_role_with_runtime_values(self):
        resp = self.client.get("/api/llm/effective")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()

        roles = {r["id"]: r for r in data["roles"]}
        self.assertEqual(set(roles), {"default", "planner", "decider", "vision", "embedding"})

        default = roles["default"]
        # runtime 是"真正跑 AI 的实例（session.llm）此刻生效"的值
        self.assertIn("base_url", default["runtime"])
        self.assertIn("model", default["runtime"])
        self.assertIn("key_present", default["runtime"])
        self.assertIn("last_error", default)

        # 角色要么给出自己的 provider，要么明确列出继承了哪些字段
        self.assertTrue("provider" in default and "inherited" in default)

    def test_status_reports_mod_connection_separately_from_backend(self):
        resp = self.client.get("/api/status")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()

        # 没有 orchestrator 时也必须给出可判断的模组连接信息（而不是让前端瞎猜）
        self.assertIn("mod_connected", data)
        self.assertFalse(data["mod_connected"])


if __name__ == "__main__":
    unittest.main()
