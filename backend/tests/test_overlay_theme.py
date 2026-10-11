"""OBS 页主题（真透明 + 可选 bg/fg/accent/font_scale）的测试。

判定方式（为什么这么测）：
- 透明是**客户端**行为：OBS 浏览器源实际执行页面里的 JS，URL 参数 `bg=transparent`
  只加一个 `html.ov-transparent` 类，样式再由 CSS 解析。Python 测试无法跑 JS，因此：
  * “不带透明” = 静态确认**默认没有透明触发**：既没保存过透明主题（GET 返回空），
    页面里所有透明声明都**以 `html.ov-transparent` 类选择器为前缀**（不被默认应用）。
  * “确实透明” = 静态解析页面样式：**每个会画底的表面**（body/顶栏/卡片/面板/输入框）
    的背景都只经由 CSS 变量 `--ov-bg` / `--ov-surface` 取色，而 `html.ov-transparent`
    恰好把这些变量改成 `transparent` —— 所以一旦挂上该类，就没有不透明底色残留。
- 后端端点做**同一套格式校验**，非法值一律回默认，落盘前校验，绝不把用户串原样拼进页面。
"""

import os
import re
import sys
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import server  # noqa: E402
from agent.config_store import ConfigStore  # noqa: E402


def _style(html: str) -> str:
    css = re.search(r"<style>(.*?)</style>", html, re.S).group(1)
    return re.sub(r"/\*.*?\*/", "", css, flags=re.S)  # 去注释，避免注释里的字样干扰解析


def _base_css(html: str) -> str:
    """去掉 ov-transparent 覆写规则后的基础样式（用于取原始表面的缺省背景）。"""
    css = _style(html)
    return re.sub(r"[^{}]*ov-transparent[^{}]*\{[^{}]*\}", "", css)


def _block(css: str, selector: str) -> str:
    """取 `selector{...}` 的声明块（避免 `html,body{}` 里的 body / 后代选择器被误配）。"""
    m = re.search(r"(?<![-\w.,#>])" + re.escape(selector) + r"\s*\{([^}]*)\}", css)
    return m.group(1) if m else ""


class OverlayThemeEndpointTests(unittest.TestCase):
    """后端主题端点：校验 / 回退 / 持久化 / 不丢既有配置键。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self._saved_store = server.config_store
        # 用临时配置文件，避免弄脏仓库里的 .local/config.json。
        server.config_store = ConfigStore(self.tmp / "config.json")
        self.client = TestClient(server.app)

    def tearDown(self):
        server.config_store = self._saved_store
        self._tmp.cleanup()

    # ① 默认：从未保存过 ⇒ 主题全空（不触发透明）
    def test_default_theme_is_empty_and_does_not_request_transparency(self):
        body = self.client.get("/api/overlay/theme").json()

        self.assertEqual(body["theme"], {"bg": "", "fg": "", "accent": "", "font_scale": None})
        self.assertEqual(body["defaults"], {"bg": "", "fg": "", "accent": "", "font_scale": None})

    # ③ 非法值：一律回默认，且不落盘（防注入：先校验格式再存）
    def test_invalid_values_fall_back_to_default_and_are_not_persisted(self):
        resp = self.client.post("/api/overlay/theme", json={
            "bg": "javascript:alert(1)",
            "fg": "<script>alert(1)</script>",
            "accent": "red;}body{display:none",
            "font_scale": "not-a-number",
        })

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["theme"], {"bg": "", "fg": "", "accent": "", "font_scale": None})
        # 再读一次仍然是默认（脏值没被存下去）。
        self.assertEqual(self.client.get("/api/overlay/theme").json()["theme"]["bg"], "")

    def test_accepts_transparent_and_valid_colors_only(self):
        ok = self.client.post("/api/overlay/theme", json={
            "bg": "transparent", "fg": "#fff", "accent": "rgba(88,166,255,0.5)", "font_scale": 2,
        }).json()["theme"]
        self.assertEqual(ok, {"bg": "transparent", "fg": "#fff", "accent": "rgba(88,166,255,0.5)", "font_scale": 2})

        out_of_range = self.client.post("/api/overlay/theme", json={"font_scale": 999}).json()["theme"]
        self.assertEqual(out_of_range["font_scale"], 8)

    # ④ 持久化往返（同一配置文件重开仍在）
    def test_theme_round_trips_and_survives_reopen(self):
        saved = self.client.post("/api/overlay/theme", json={
            "bg": "#101820", "fg": "#ffcc00", "accent": "#58a6ff", "font_scale": 1.5,
        }).json()["theme"]

        self.assertEqual(self.client.get("/api/overlay/theme").json()["theme"], saved)

        reopened = ConfigStore(self.tmp / "config.json")
        stored = reopened.raw(redact=True)["overlay_theme"]
        self.assertEqual(stored, saved)

    # Main 的硬约束：写 overlay 不许静默抹掉既有配置键（两个方向都测）。
    def test_saving_overlay_theme_keeps_existing_config_keys(self):
        self.client.post("/api/config", json={"patrol_radius": 13, "capability_level": 2})
        self.client.post("/api/overlay/theme", json={"bg": "transparent"})

        raw = server.config_store.raw(redact=True)
        self.assertEqual(raw["patrol_radius"], 13)
        self.assertEqual(raw["capability_level"], 2)
        self.assertEqual(raw["overlay_theme"]["bg"], "transparent")

        # 反过来：改 pagon_radius 也不能把 overlay_theme 冲掉。
        self.client.post("/api/config", json={"patrol_radius": 5})
        raw = server.config_store.raw(redact=True)
        self.assertEqual(raw["patrol_radius"], 5)
        self.assertIn("overlay_theme", raw)


class OverlayPageMarkupTests(unittest.TestCase):
    """页面标记：默认不透明 / 透明靠 var 生效 / 页面内置默认外观不变。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._saved_store = server.config_store
        server.config_store = ConfigStore(Path(self._tmp.name) / "config.json")
        self.client = TestClient(server.app)

    def tearDown(self):
        server.config_store = self._saved_store
        self._tmp.cleanup()

    OPAQUE_SURFACES = {
        "contacts": [
            ("body", "--ov-bg"), (".topbar", "--ov-surface"), (".contacts", "--ov-bg"),
            (".group-title", "--ov-surface"), (".chat-head", "--ov-surface"), (".chat-foot", "--ov-surface"),
        ],
        "dashboard": [
            ("body", "--ov-bg"), (".topbar", "--ov-surface"), (".tabs", "--ov-surface"),
            (".card", "--ov-surface"), (".memory-pane", "--ov-surface"), ("dialog", "--ov-surface"),
        ],
    }

    # ① 无参数时不带透明：透明类只在 `bg==='transparent'` 时挂上，且 CSS 里所有透明声明
    #    都以 `html.ov-transparent` 为前提（默认不挂类 → 一律不生效）。
    def test_pages_do_not_apply_transparency_without_the_param(self):
        for path in ("/contacts", "/", "/overlay/state"):
            html = self.client.get(path).text
            css = _style(html)
            for selector in re.findall(r"([^{}]+)\{", css):
                if "ov-transparent" not in selector:
                    continue
                # 出现 ov-transparent 的选择器必须是 html.ov-transparent 形态（受类约束）。
                for part in selector.split(","):
                    self.assertTrue(part.strip().startswith("html.ov-transparent"),
                                    f"{path} 里 ov-transparent 未被类约束: {part.strip()!r}")
            # 开关本身受 bg 值判定，默认（空）不会挂类。
            self.assertIn("classList.toggle('ov-transparent', bg === 'transparent')",
                          html, f"{path} 缺少受控的透明开关")

    def test_default_opaque_colors_are_intact(self):
        contacts = _style(self.client.get("/contacts").text)
        self.assertIn(":root{--ov-bg:#0d1117;--ov-surface:#161b22", contacts)
        base = _base_css(self.client.get("/contacts").text)
        self.assertEqual(_block(base, "body"), "font-family:'Segoe UI','PingFang SC','Microsoft YaHei',system-ui,sans-serif;\n"
                         "  background:var(--ov-bg,#0d1117);color:var(--ov-fg,#c9d1d9);display:flex;flex-direction:column;overflow:hidden")

        dashboard = _style(self.client.get("/").text)
        self.assertIn(":root{--ov-bg:#0d1117;--ov-surface:#161b22", dashboard)

    # ② bg=transparent ⇒ 所有画底表面经变量取色，且变量在 ov-transparent 下变 transparent。
    def test_transparent_mode_clears_every_opaque_surface(self):
        for path, surfaces in (("/contacts", self.OPAQUE_SURFACES["contacts"]),
                               ("/", self.OPAQUE_SURFACES["dashboard"])):
            override = _block(_style(self.client.get(path).text), "html.ov-transparent")
            self.assertIn("--ov-bg:transparent", override, path)
            self.assertIn("--ov-surface:transparent", override, path)
            base = _base_css(self.client.get(path).text)
            for selector, var in surfaces:
                decls = _block(base, selector)
                self.assertIn(f"background:var({var}", decls,
                              f"{path} 的 {selector} 背景未走 {var}，透明模式下会残留不透明底")

    def test_overlay_default_is_transparent_by_design(self):
        css = _base_css(self.client.get("/overlay/state").text)
        self.assertIn("html,body{", css)
        self.assertIn("background:transparent", _block(css, "html,body").replace(" ", ""))
        # 面板色跟随 --ov-bg，缺省仍是 transparent（外观与以前一致）。
        self.assertIn("--panel:var(--ov-bg,transparent)", css)

    # 页面都带共享外观脚本：URL 参数优先、非法值回退、只走任意 /api/overlay/theme。
    def test_pages_share_the_appearance_script(self):
        for path in ("/contacts", "/", "/overlay/state"):
            html = self.client.get(path).text
            self.assertIn("ov-transparent", html)
            self.assertIn("'--ov-fg'", html)
            self.assertIn("/api/overlay/theme", html)
            self.assertIn("params.has(k)", html)
        # 仪表盘设置区要有可编辑控件 + 保存函数。
        dashboard = self.client.get("/").text
        self.assertIn('id="ovThemeBg"', dashboard)
        self.assertIn("saveOverlayTheme", dashboard)


class AppearanceScriptValidationTests(unittest.TestCase):
    """URL 参数校验：非法值回退（静态确认脚本只认合法格式且不写进 HTML）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._saved_store = server.config_store
        server.config_store = ConfigStore(Path(self._tmp.name) / "config.json")
        self.client = TestClient(server.app)

    def tearDown(self):
        server.config_store = self._saved_store
        self._tmp.cleanup()

    def test_appearance_script_validates_before_applying(self):
        html = self.client.get("/overlay/state").text
        # 只接受透明关键字 / #rgb / #rrggbb / rgb()/rgba()；其余回空串。
        self.assertIn(r"#[0-9a-fA-F]{3}", html)
        self.assertIn("rgba?", html)
        self.assertIn("t === 'transparent' ? t : color(t)", html)
        # 值经 style.setProperty 落 CSS，绝不拼进 innerHTML。
        self.assertIn("root.style.setProperty('--ov-bg'", html)
        self.assertNotIn("innerHTML += theme", html)


if __name__ == "__main__":
    unittest.main()
