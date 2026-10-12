"""关停路径回归（用户口径，2026-10-12："ctrl 加 c 居然关不掉，只能把整个终端杀掉"）。

根因证据（实测 + uvicorn 源码，见交付报告）：
  * `uvicorn/server.py` 的 `Server.shutdown` → `_wait_tasks_to_complete()` 是
    "**等** `server_state.connections` / `server_state.tasks` 自己变空"的死循环，
    只有**再收到一次 Ctrl+C**（`force_exit`）才出来 —— 任何一条没结束的连接/任务都会把
    "关掉后端"挂住；uvicorn 自己也**不会**替我们停线程/断线/关看门狗；
  * 我们这边当时没有关停钩子：接线循环是 `while True` + `time.sleep(0.05/5)`，
    wire 读线程、热载看门狗、视觉线程、聊天轮次都没人显式收；而 `/ws` 的聊天轮次是
    **在事件循环里同步调 LLM**（实测 3~5 秒、超时上限 120 秒），那段时间连 SIGINT
    都处理不了（观感就是"Ctrl+C 没反应"）。

本文件锁定五条：
  ① `wire_bridge_loop` 置位 stop ⇒ **立刻**退出（不再等一拍 / 一次 5 秒重试）；
  ② `stop_background_work()` 之后 `background_threads()` 为空（我们起的线程都 not alive）；
  ③ 我们起的每条线程都是 **daemon**（daemon 永远不拦解释器退出）；
  ④ `BoundedServer` 第一次信号后就**有界**收尾：到 grace 置 `force_exit`
     （uvicorn 自己的"第二次 Ctrl+C"机制，**不是** `os._exit()`）；
  ⑤ `_ChatWorker` 把一轮对话挪到 daemon 线程（不堵事件循环），停的时候不打断在飞的那一轮。
"""
import socket
import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import server  # noqa: E402
from protocol import wire_client  # noqa: E402
from protocol.wire_client import WireClient  # noqa: E402


class _FakeMod:
    """最小 wire 服务端：接受连接、吞掉来帧（够 `WireClient.connect()` 用）。"""

    def __init__(self):
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(2)
        self.port = self._sock.getsockname()[1]
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, name="fake-mod", daemon=True)

    def start(self):
        self._thread.start()
        return self

    def _serve(self):
        self._sock.settimeout(0.1)
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except (socket.timeout, OSError):
                continue
            conn.settimeout(0.1)
            while not self._stop.is_set():
                try:
                    if not conn.recv(4096):
                        break
                except socket.timeout:
                    continue
                except OSError:
                    break

    def stop(self):
        self._stop.set()
        try:
            self._sock.close()
        except OSError:
            pass


class _FakeOrchestrator:
    """只记 `start/stop/tick` 次数（接线循环的行为契约）。"""

    def __init__(self):
        self.started = 0
        self.stopped = 0
        self.ticks = 0

    def start(self):
        self.started += 1

    def stop(self):
        self.stopped += 1

    def tick(self):
        self.ticks += 1


def _swap_globals(**values):
    """换掉 `server` 的模块级引用，返回恢复用的回调（测试之间不许互相污染）。"""
    saved = {name: getattr(server, name) for name in values}
    for name, value in values.items():
        setattr(server, name, value)

    def restore():
        for name, old in saved.items():
            setattr(server, name, old)

    return restore


class WireBridgeLoopTests(unittest.TestCase):

    def setUp(self):
        self.mod = _FakeMod().start()
        self.addCleanup(self.mod.stop)
        server._stop_event.clear()
        self.addCleanup(server._stop_event.clear)

    def test_bridge_loop_connects_ticks_and_exits_on_stop(self):
        wire = WireClient("127.0.0.1", self.mod.port)
        orch = _FakeOrchestrator()
        stop_event = threading.Event()
        thread = threading.Thread(target=server.wire_bridge_loop,
                                  args=(wire, orch, stop_event),
                                  name="backend-connect", daemon=True)
        thread.start()
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and (not wire.connected or orch.ticks < 3):
            time.sleep(0.02)

        self.assertTrue(wire.connected, "应该连上假模组")
        self.assertEqual(orch.started, 1)
        self.assertGreaterEqual(orch.ticks, 3, "连上后要按 20Hz tick")
        self.assertTrue(any(t.name == "wire-reader" for t in threading.enumerate()))

        stop_event.set()
        thread.join(2.0)
        self.assertFalse(thread.is_alive(), "置位 stop ⇒ 立刻退出（不等 50ms 那一拍）")
        self.assertEqual(orch.stopped, 1, "退出前要把 orchestrator 停下来")

        restore = _swap_globals(wire=wire, orchestrator=orch, _connect_thread=thread)
        try:
            server.stop_background_work(timeout=1.0)
        finally:
            restore()
        self.assertFalse(wire.connected, "关停要断开 wire")
        self.assertFalse(any(t.name == "wire-reader" for t in threading.enumerate()),
                         "wire 读线程要真的停下")

    def test_disconnected_retry_sleep_is_interruptible(self):
        """没连上时的重试等待不许把关停拖住（以前是 `time.sleep(5)`，会白等到底）。

        连接本身另有一个 3 秒上限（`CONNECT_TIMEOUT_SECONDS`，见 `WireClient.connect`）：
        所以这里给 30 秒的重试间隔，断言"置位后在 3 秒窗口内就退了" —— 若还是 `sleep`，
        这条测试会等满 30 秒。
        """
        dead = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        dead.bind(("127.0.0.1", 0))
        dead_port = dead.getsockname()[1]
        dead.close()

        wire = WireClient("127.0.0.1", dead_port)
        orch = _FakeOrchestrator()
        stop_event = threading.Event()
        thread = threading.Thread(target=server.wire_bridge_loop,
                                  args=(wire, orch, stop_event),
                                  kwargs={"retry_seconds": 30.0},
                                  name="backend-connect", daemon=True)
        thread.start()
        time.sleep(0.05)                    # 让它进到 connect / 重试等待里
        stop_event.set()
        thread.join(wire_client.CONNECT_TIMEOUT_SECONDS + 1.0)
        self.assertFalse(thread.is_alive(), "重试等待期间也要能被立刻叫停（不许等满重试间隔）")

    def test_every_backend_thread_is_daemon(self):
        """我们起的线程一律 daemon ⇒ 永远不拦解释器退出（关不掉的一半根因就在这）。"""
        wire = WireClient("127.0.0.1", self.mod.port)
        orch = _FakeOrchestrator()
        stop_event = threading.Event()
        thread = threading.Thread(target=server.wire_bridge_loop,
                                  args=(wire, orch, stop_event),
                                  name="backend-connect", daemon=True)
        thread.start()
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and not wire.connected:
            time.sleep(0.02)
        try:
            names = {t.name for t in server.background_threads()}
            self.assertIn("backend-connect", names)
            self.assertIn("wire-reader", names)
            for thread_ in server.background_threads():
                self.assertTrue(thread_.daemon, f"{thread_.name} 必须是 daemon")
        finally:
            stop_event.set()
            thread.join(2.0)
            restore = _swap_globals(wire=wire, orchestrator=orch, _connect_thread=thread)
            try:
                server.stop_background_work(timeout=1.0)
            finally:
                restore()


class StopBackgroundWorkTests(unittest.TestCase):

    def test_stop_reports_and_leaves_nothing_alive(self):
        """`stop_background_work()` 之后：报告的 stopped 非空、仍活着的为空。"""
        mod = _FakeMod().start()
        self.addCleanup(mod.stop)
        server._stop_event.clear()
        wire = WireClient("127.0.0.1", mod.port)
        orch = _FakeOrchestrator()
        thread = threading.Thread(target=server.wire_bridge_loop,
                                  args=(wire, orch, server._stop_event),
                                  name="backend-connect", daemon=True)
        thread.start()
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and not wire.connected:
            time.sleep(0.02)

        restore = _swap_globals(wire=wire, orchestrator=orch, _connect_thread=thread)
        try:
            report = server.stop_background_work(timeout=1.0)
            self.assertIn("wire", report["stopped"])
            self.assertEqual(report["still_alive"], [],
                             f"关停后不该还有我们的线程在跑：{report}")
            self.assertEqual(server.background_threads(), [],
                             "background_threads() 必须为空（自检口径）")
        finally:
            restore()

    def test_stop_is_idempotent(self):
        server._stop_event.clear()
        restore = _swap_globals(wire=None, orchestrator=None, _connect_thread=None)
        try:
            first = server.stop_background_work(timeout=0.1)
            second = server.stop_background_work(timeout=0.1)
        finally:
            restore()
        self.assertEqual(first["still_alive"], [])
        self.assertEqual(second["still_alive"], [])


class BoundedServerTests(unittest.TestCase):
    """uvicorn 的"第二次 Ctrl+C"由我们**定时**替操作者按（有界 ≤ grace），不是 os._exit。"""

    def _config(self):
        import uvicorn

        return uvicorn.Config("server:app", host="127.0.0.1", port=0, reload=False,
                              log_level="critical")

    def test_first_signal_schedules_a_bounded_force(self):
        import signal

        import main as main_module

        bounded = main_module.BoundedServer(self._config(), grace_seconds=0.15)
        self.assertFalse(bounded.should_exit)
        bounded.handle_exit(signal.SIGINT, None)
        self.assertTrue(bounded.should_exit, "第一次信号就要进入优雅关停")
        self.assertFalse(bounded.force_exit, "不许立刻强杀（先给优雅关停机会）")
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and not bounded.force_exit:
            time.sleep(0.02)
        self.assertTrue(bounded.force_exit,
                        "到了 grace 还没关完 ⇒ 置 force_exit（uvicorn 自带的收尾开关）")

    def test_grace_default_is_under_two_seconds(self):
        import main as main_module

        self.assertLessEqual(main_module.SHUTDOWN_GRACE_SECONDS, 2.0,
                             "用户口径：Ctrl+C 后 ≤2 秒干净退出")


class ChatWorkerTests(unittest.TestCase):

    def test_job_runs_on_a_daemon_thread_not_the_caller(self):
        worker = server._ChatWorker()
        worker.start()
        self.addCleanup(worker.stop)
        seen = {}

        def job(value):
            seen["thread"] = threading.current_thread()
            seen["daemon"] = threading.current_thread().daemon
            return value * 2

        result = worker.submit(job, 21).result(timeout=5)
        self.assertEqual(result, 42)
        self.assertIsNot(seen["thread"], threading.current_thread())
        self.assertEqual(seen["thread"].name, "lcu-chat")
        self.assertTrue(seen["daemon"], "必须是 daemon：默认线程池的线程会被 atexit join，"
                                        "在飞的 120 秒请求能把进程卡在出口")
        self.assertIn("lcu-chat", {t.name for t in server.background_threads()})

    def test_stop_does_not_interrupt_an_inflight_turn(self):
        worker = server._ChatWorker()
        worker.start()
        entered = threading.Event()
        release = threading.Event()

        def job():
            entered.set()
            release.wait(5)
            return "done"

        future = worker.submit(job)
        self.assertTrue(entered.wait(2))
        self.assertFalse(worker.stop(timeout=0.1), "在飞的那一轮没回来 ⇒ 如实报还没停")
        release.set()
        self.assertEqual(future.result(timeout=5), "done")
        self.assertTrue(worker.stop(timeout=2.0), "这一轮结束后应立即能停下")

    def test_stopped_worker_starts_again(self):
        worker = server._ChatWorker()
        worker.start()
        worker.stop()
        worker.start()
        self.assertTrue(worker.running)
        worker.stop()


if __name__ == "__main__":
    unittest.main()
