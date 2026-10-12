"""单页 OBS 叠加层后端：布局读写 / 叠加层数据 / 原版贴图缓存。

判定方式（为什么这么测）：

- **不编数**：`/api/overlay/data` 的每个数字都必须能在喂进去的 `state_update` 里找到出处；
  状态里没有的字段（例如 `air`）必须是 `None`，而不是好看的默认值 —— 所以断言里专门
  盯着"没给的就该是空"。
- **不哑吞**：非法布局必须 400 + 机器可读 `code`，且**不写入**（再 GET 还是上一次/默认），
  **不夹取**（101 不能变成 100）。
- **不冒充**：`/api/icon/<id>` 只回真收下的 PNG 字节；没有的 id 必须 404，绝不回占位图。

用 `TestClient` 直接打接口（不进 lifespan、不起后台线程、不连模组）；落盘路径指到临时目录，
绝不碰仓库里的 `.local/`。
"""

import base64
import json
import struct
import sys
import tempfile
import unittest
import zlib
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

import server  # noqa: E402
from agent.config_store import ConfigStore  # noqa: E402
from api import overlay as overlay_api  # noqa: E402

ALL_KINDS = {"gear", "weapon", "vitals", "task"}


def _png(width: int = 2, height: int = 2, rgba: tuple = (255, 0, 0, 255)) -> bytes:
    """造一张**真的** PNG（stdlib zlib/struct；不用不存在的 ImageIO）。"""
    raw = b"".join(b"\x00" + bytes(rgba) * width for _ in range(height))

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


class _FakeSession:
    """只提供叠加层数据要的东西：`runtime`（最近一次 state_update）+ 事件入口。"""

    def __init__(self, runtime=None):
        self.runtime = dict(runtime or {})
        self.handled: list[tuple] = []

    def handle_event(self, event_type: str, data: dict) -> None:
        self.handled.append((event_type, data))


class _FakeOrchestrator:
    def __init__(self, session):
        self.session = session


class _OverlayCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.config_path = self.tmp / "config.json"
        self._saved = (server.config_store, server.orchestrator, server.wire)
        server.config_store = ConfigStore(self.config_path)
        server.orchestrator = None
        server.wire = None
        overlay_api.reset_icon_cache()
        self.client = TestClient(server.app)

    def tearDown(self):
        server.config_store, server.orchestrator, server.wire = self._saved
        overlay_api.reset_icon_cache()
        self._tmp.cleanup()

    # ── 工具 ──

    def _stored(self) -> dict:
        """**磁盘上**的存档（证明真的落盘，不只是内存里改了改）。"""
        if not self.config_path.exists():
            return {}
        return json.loads(self.config_path.read_text(encoding="utf-8"))

    def _connect(self, runtime: dict, icons: dict | None = None):
        """让 `/api/overlay/data` 看起来像"模组连着 + 刚上报过 state_update"。"""
        session = _FakeSession(runtime)
        server.orchestrator = _FakeOrchestrator(session)
        server.wire = SimpleNamespace(connected=True)
        overlay_api.install_icon_capture(server.orchestrator)
        if icons:
            session.handle_event("item_icons", {"icons": icons})
        return session


class LayoutDefaultTests(_OverlayCase):
    """① 没保存过布局 ⇒ 默认布局：四件齐全、不重叠、能直接拿去 OBS 用。"""

    def test_default_layout_has_every_component_in_range_and_not_overlapping(self):
        body = self.client.get("/api/overlay/layout").json()

        self.assertEqual(body["version"], 1)
        components = body["components"]
        self.assertEqual({component["kind"] for component in components}, ALL_KINDS)

        seen_y = set()
        for component in components:
            self.assertTrue(0 <= component["x"] <= 100)
            self.assertTrue(0 <= component["y"] <= 100)
            self.assertEqual(component["scale"], 1.0)
            self.assertEqual(component["opacity"], 1.0)
            self.assertIs(component["visible"], True)
            # 位置不重叠：四个组件不能都堆在同一个 y 上（拿进 OBS 会互相盖住）。
            self.assertNotIn(component["y"], seen_y)
            seen_y.add(component["y"])

    def test_default_layout_is_returned_for_a_fresh_store_every_time(self):
        first = self.client.get("/api/overlay/layout").json()
        first["components"][0]["x"] = 99  # 改返回值不该污染下一个请求
        second = self.client.get("/api/overlay/layout").json()

        self.assertNotEqual(second["components"][0]["x"], 99)


class LayoutRoundTripTests(_OverlayCase):
    """② 保存 → 读回（含真的写到了磁盘）。"""

    def test_saved_layout_reads_back_and_reaches_disk(self):
        layout = {"version": 1, "components": [
            {"kind": "vitals", "x": 6.5, "y": 78, "scale": 1.25, "opacity": 0.4, "visible": True},
            {"kind": "task", "x": 70, "y": 4, "scale": 2, "opacity": 1, "visible": False},
        ]}

        resp = self.client.put("/api/overlay/layout", json=layout)

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), layout)
        self.assertEqual(self.client.get("/api/overlay/layout").json(), layout)
        # 落了盘（重开一个 store 也能读到同一份）。
        self.assertEqual(self._stored()["overlay_layout"], layout)
        self.assertEqual(ConfigStore(self.config_path).raw()["overlay_layout"], layout)

    def test_saving_the_layout_keeps_unrelated_config_keys(self):
        server.config_store._data["patrol_radius"] = 7  # noqa: SLF001 —— 别人的键
        server.config_store.save()
        layout = {"version": 1, "components": [
            {"kind": "gear", "x": 1, "y": 1, "scale": 1.0, "opacity": 1.0, "visible": True}]}

        self.client.put("/api/overlay/layout", json=layout)

        self.assertEqual(self._stored()["patrol_radius"], 7)

    def test_broken_archive_is_treated_as_never_saved(self):
        for broken in ("not-a-layout", {"version": 1, "components": [{"kind": "hat", "x": 1, "y": 1}]},
                       {"version": 1, "components": "nope"}, 42):
            with self.subTest(broken=broken):
                server.config_store._data["overlay_layout"] = broken  # noqa: SLF001
                server.config_store.save()

                resp = self.client.get("/api/overlay/layout")

                self.assertEqual(resp.status_code, 200)  # 绝不 500
                self.assertEqual({c["kind"] for c in resp.json()["components"]}, ALL_KINDS)


class LayoutRejectionTests(_OverlayCase):
    """③ 非法布局：如实拒（机器可读 code）且**不写入**、不夹取。"""

    def _reject(self, payload: dict, code: str):
        before = self.client.get("/api/overlay/layout").json()
        resp = self.client.put("/api/overlay/layout", json=payload)

        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["detail"]["code"], code)
        # 拒了就是**没存**：读回来还是上一份（默认），脏值一个字节都没进去。
        self.assertEqual(self.client.get("/api/overlay/layout").json(), before)
        self.assertNotIn("overlay_layout", self._stored())

    def test_unknown_kind_is_rejected(self):
        self._reject({"components": [{"kind": "helmet", "x": 1, "y": 1}]}, "unknown_kind")

    def test_duplicate_kind_is_rejected(self):
        self._reject({"components": [{"kind": "gear", "x": 1, "y": 1},
                                     {"kind": "gear", "x": 40, "y": 40}]}, "duplicate_kind")

    def test_out_of_range_values_are_rejected_and_not_clamped(self):
        for field, value in (("x", 101), ("x", -0.5), ("y", 100.01), ("scale", 0.4),
                             ("scale", 3.5), ("opacity", 1.5), ("opacity", -0.1)):
            with self.subTest(field=field, value=value):
                entry = {"kind": "vitals", "x": 5, "y": 5, "scale": 1.0, "opacity": 1.0}
                entry[field] = value
                self._reject({"components": [entry]}, "out_of_range")

    def test_non_boolean_visible_and_non_numeric_values_are_rejected(self):
        self._reject({"components": [{"kind": "task", "x": 5, "y": 5, "visible": "yes"}]},
                     "invalid_value")
        self._reject({"components": [{"kind": "task", "x": "5", "y": 5}]}, "invalid_value")

    def test_layout_body_that_is_not_an_object_is_rejected(self):
        self._reject([{"kind": "gear"}], "invalid_layout")

    def test_valid_save_after_a_rejection_still_works(self):
        self.client.put("/api/overlay/layout", json={"components": [{"kind": "nope"}]})
        layout = {"version": 1, "components": [
            {"kind": "weapon", "x": 10, "y": 20, "scale": 1.0, "opacity": 1.0, "visible": True}]}

        self.assertEqual(self.client.put("/api/overlay/layout", json=layout).status_code, 200)
        self.assertEqual(self.client.get("/api/overlay/layout").json(), layout)


class OverlayDataTests(_OverlayCase):
    """④ 叠加层数据：拿不到就 `connected:false` + 空对象，**不编数**。"""

    def test_no_session_reports_disconnected_with_empty_objects(self):
        body = self.client.get("/api/overlay/data").json()

        self.assertIs(body["connected"], False)
        self.assertEqual(body["gear"], {})
        self.assertEqual(body["vitals"], {})
        self.assertEqual(body["task"], {})

    def test_live_wire_without_any_state_update_still_reports_no_data(self):
        server.orchestrator = _FakeOrchestrator(_FakeSession({}))
        server.wire = SimpleNamespace(connected=True)

        body = self.client.get("/api/overlay/data").json()

        self.assertIs(body["connected"], False)
        self.assertEqual(body["vitals"], {})

    def test_connected_payload_passes_gear_through_and_maps_real_vitals(self):
        state = {
            "gear": {"armor": [{"slot": "head", "item": "minecraft:iron_helmet"}],
                     "main_hand": {"item": "minecraft:iron_sword"}},
            "player": {"health": 17.5, "max_health": 20, "hunger": 12, "saturation": 3.5},
            "task_state": {"kind": "come", "status": "running", "detail": "walking to player",
                           "progress": 0.4},
        }
        self._connect(state)

        body = self.client.get("/api/overlay/data").json()

        self.assertIs(body["connected"], True)
        # gear 原样透传（后端不改模组给的形状）。
        self.assertEqual(body["gear"], state["gear"])
        self.assertEqual(body["vitals"]["health"], 17.5)
        self.assertEqual(body["vitals"]["max_health"], 20)
        self.assertEqual(body["vitals"]["food"], 12)
        self.assertEqual(body["vitals"]["saturation"], 3.5)
        # 状态里没有的字段就是"没有"：不许编 0/300 之类的假值。
        self.assertIsNone(body["vitals"]["air"])
        self.assertIsNone(body["vitals"]["max_air"])
        self.assertEqual(body["task"]["kind"], "come")
        self.assertEqual(body["task"]["status"], "running")
        self.assertEqual(body["task"]["detail"], "walking to player")
        self.assertEqual(body["task"]["progress"], 0.4)

    def test_idle_task_state_reports_no_task(self):
        self._connect({"player": {"health": 20}, "task_state": {"kind": "idle", "status": "idle"}})

        self.assertEqual(self.client.get("/api/overlay/data").json()["task"], {})


class ItemIconCacheTests(_OverlayCase):
    """⑤ `item_icons` 累积 → `icons` 列表 → `/api/icon/<id>` 命中与 404。"""

    def test_icons_accumulate_across_events_and_are_listed_for_the_pages(self):
        first = {"minecraft:iron_helmet": _b64(_png()),
                 "minecraft:iron_sword": _b64(_png(2, 2, (0, 255, 0, 255)))}
        session = self._connect({"player": {"health": 20}}, first)

        session.handle_event("item_icons", {"icons": {"minecraft:bread": _b64(_png())}})

        icons = self.client.get("/api/overlay/data").json()["icons"]
        for item_id in first:
            self.assertIn(item_id, icons)
        self.assertIn("minecraft:bread", icons)

    def test_icon_endpoint_serves_the_exact_received_bytes(self):
        helmet = _png(2, 2, (10, 20, 30, 255))
        self._connect({"player": {}}, {"minecraft:iron_helmet": _b64(helmet)})

        resp = self.client.get("/api/icon/minecraft:iron_helmet")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.headers["content-type"], "image/png")
        self.assertEqual(resp.content, helmet)

    def test_resent_icon_overwrites_the_cached_bytes(self):
        session = self._connect({"player": {}}, {"minecraft:coal": _b64(_png(2, 2, (0, 0, 0, 255)))})
        updated = _png(3, 3, (9, 9, 9, 255))

        session.handle_event("item_icons", {"icons": {"minecraft:coal": _b64(updated)}})

        self.assertEqual(self.client.get("/api/icon/minecraft:coal").content, updated)

    def test_unknown_id_is_404_with_a_code_and_never_a_placeholder(self):
        self._connect({"player": {}}, {"minecraft:iron_helmet": _b64(_png())})

        resp = self.client.get("/api/icon/minecraft:diamond")

        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.json()["detail"]["code"], "icon_unknown")
        self.assertNotIn("image/png", resp.headers.get("content-type", ""))

    def test_garbage_entries_are_dropped_not_faked(self):
        session = self._connect({"player": {}}, {"minecraft:iron_helmet": _b64(_png())})

        session.handle_event("item_icons", {"icons": {
            "minecraft:not_base64": "!!!not base64!!!",
            "minecraft:not_png": _b64(b"<html>definitely not a png</html>"),
            "../../etc/passwd": _b64(_png()),
            "minecraft:empty": "",
        }})

        self.assertEqual(self.client.get("/api/icon/minecraft:not_base64").status_code, 404)
        self.assertEqual(self.client.get("/api/icon/minecraft:not_png").status_code, 404)
        self.assertEqual(self.client.get("/api/icon/minecraft:empty").status_code, 404)
        icons = self.client.get("/api/overlay/data").json()["icons"]
        for bogus in ("minecraft:not_base64", "minecraft:not_png", "minecraft:empty"):
            self.assertNotIn(bogus, icons)

    def test_traversal_shaped_path_is_refused_not_read_from_disk(self):
        self._connect({"player": {}}, {"minecraft:iron_helmet": _b64(_png())})

        resp = self.client.get("/api/icon/minecraft:..%2F..%2F.local%2Fconfig.json")

        # 只可能是"没有这个 id"：绝不把本地文件当贴图读出来回给请求方。
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.json()["detail"]["code"], "icon_unknown")
        self.assertNotIn("image/png", resp.headers.get("content-type", ""))
        self.assertNotIn('"llm"', resp.text)  # 存档里的键一个都不该出现

    def test_capture_installs_once_and_never_swallows_other_events(self):
        session = _FakeSession({"player": {}})
        orchestrator = _FakeOrchestrator(session)

        self.assertIs(overlay_api.install_icon_capture(orchestrator), True)
        self.assertIs(overlay_api.install_icon_capture(orchestrator), False)  # 幂等
        session.handle_event("state_update", {"player": {"health": 3}})
        session.handle_event("item_icons", {"icons": {"minecraft:apple": _b64(_png())}})

        # 原实现照样收到事件（旁听，不改行为）。
        self.assertEqual([event for event, _ in session.handled],
                         ["state_update", "item_icons"])
        self.assertEqual(self.client.get("/api/icon/minecraft:apple").status_code, 200)


class OverlayPageRouteTests(_OverlayCase):
    """⑥ 页面路由：`/overlay`（单页）与 `/overlay/editor`（编辑器），老组件页不变。"""

    def test_stage_and_editor_pages_are_served(self):
        stage = self.client.get("/overlay")
        editor = self.client.get("/overlay/editor")

        self.assertEqual(stage.status_code, 200)
        self.assertEqual(editor.status_code, 200)
        self.assertIn("text/html", stage.headers["content-type"])
        self.assertIn("text/html", editor.headers["content-type"])
        # `/overlay/editor` 没有被 `/overlay/{component}` 吞掉（吞掉会回 404 未知组件）。
        self.assertNotIn("unknown overlay component", editor.text)

    def test_legacy_component_pages_still_work(self):
        for name in ("status", "task", "players", "state", "backend"):
            with self.subTest(component=name):
                self.assertEqual(self.client.get(f"/overlay/{name}").status_code, 200)
        self.assertEqual(self.client.get("/overlay/definitely_not_a_component").status_code, 404)
        names = [entry["name"] for entry in self.client.get("/api/overlay/components").json()["components"]]
        self.assertEqual(set(names), set(server.OVERLAY_COMPONENTS))


if __name__ == "__main__":
    unittest.main()
