"""Real cross-language wire-protocol test: Python backend ⇄ Java mod.

This is the only test in the repository that proves the two sides actually
speak the same protocol. It launches the headless Java fixture
(``gradlew runWireFixture`` → ``com.lcu.lcumod.network.WireServerFixtureMain``),
which runs the **production** ``WireServer`` (real accept loop, real UTF-8
JSONL reader/writer, real shared command queue, real ``sendEvent`` /
``sendResponse`` / ``sendProgress`` / ``sendOutcome`` and the real
``ControlStream`` branch of the reader) with no game client, then talks to it
through the **real** backend code path (``protocol.wire_client.WireClient``).

Honesty notes, so nobody mistakes this for more than it is:

* There is no Python fake server anywhere in this file — the other end of the
  socket is the Java mod's own production class.
* There is nothing in the fixture that pretends to be the game: the Java main
  drains the same ``WireServer.commandQueue`` that ``ActionExecutor`` drains
  in-game and answers with the same message shapes. Everything below the
  socket is production code; only the game loop is absent.
* No handshake/auth exists on the 26.3 wire (the 1.21.1 token exchange was
  dropped), so the first frame is an ordinary ``state_update`` event.

Why this file has no ``__init__.py`` next to it: ``unittest discover -s tests``
only recurses into real packages, so the default backend suite stays exactly
1103 tests with zero skips (that number is a maintained baseline), while this
heavier test — it compiles and boots the mod against the real MC 26.3 classes
(tens of seconds even warm, minutes on a cold CI runner) — is run explicitly:

    cd backend
    python -m unittest discover -s tests/integration -t tests/integration -v

CI runs exactly that command in the ``integration`` job. It never silently
skips: if the Java fixture cannot be launched or does not report ``READY``,
``setUpClass`` fails with the fixture's own captured output.
"""

from __future__ import annotations

import os
import queue
import subprocess
import threading
import time
import unittest
from pathlib import Path

from protocol.wire_client import WireClient

ROOT = Path(__file__).resolve().parents[3]

# Cold Gradle + ModDevGradle + NeoForge on a fresh CI runner (downloading the
# neoform runtime and the MC libraries) can take minutes; override with
# LCU_WIRE_FIXTURE_READY_TIMEOUT if a runner is slower than that.
READY_TIMEOUT = float(os.getenv("LCU_WIRE_FIXTURE_READY_TIMEOUT", "600"))
SHUTDOWN_TIMEOUT = 60.0


def _launcher() -> list[str]:
    """The gradle wrapper, in the form each OS accepts.

    ``gradlew`` is committed as 100644, so POSIX needs ``sh gradlew``; Windows
    goes through ``cmd /c`` (a bare ``.bat`` is not a real executable).
    """
    if os.name == "nt":
        return ["cmd", "/c", str(ROOT / "gradlew.bat")]
    return ["sh", str(ROOT / "gradlew")]


class JavaWireProtocolIntegrationTests(unittest.TestCase):
    """Drives the real Java WireServer through the real Python WireClient."""

    process: subprocess.Popen | None = None
    wire: WireClient | None = None
    port: int = 0
    fixture_lines: list[str] = []
    _lines_lock = threading.Lock()

    # ── fixture lifecycle ────────────────────────────────────────────────

    @classmethod
    def setUpClass(cls):
        cls.fixture_lines = []
        argv = _launcher() + [
            "--no-daemon",
            "--console=plain",
            "-q",
            "runWireFixture",
        ] + cls._fixture_properties()
        cls.process = subprocess.Popen(
            argv,
            cwd=str(ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        stream = queue.Queue()

        def pump():
            assert cls.process is not None and cls.process.stdout is not None
            for line in cls.process.stdout:
                line = line.rstrip()
                with cls._lines_lock:
                    cls.fixture_lines.append(line)
                stream.put(line)

        threading.Thread(target=pump, daemon=True, name="fixture-stdout").start()

        try:
            cls.port = cls._await_ready(stream)
            cls.wire = cls._make_client(cls.port)
            cls._connect_with_retry()
        except BaseException:
            cls._teardown_fixture()
            raise

    @classmethod
    def tearDownClass(cls):
        cls._teardown_fixture()

    @classmethod
    def _teardown_fixture(cls):
        if cls.wire is not None:
            if cls.wire.connected:
                # Graceful: the fixture then leaves its loop, stops the server and exits 0.
                try:
                    cls.wire.send_command("fixture_shutdown")
                except Exception:
                    pass
            # 不管这一句送没送到：夹具**看到客户端断开就会自己退出**（见 WireServerFixtureMain），
            # 所以收尾不依赖这条命令能不能发出去（连接被服务端关掉时它是送不到的 ✗）。
            cls.wire.disconnect()
            cls.wire = None
        proc = cls.process
        cls.process = None
        if proc is None:
            return
        try:
            proc.wait(timeout=SHUTDOWN_TIMEOUT)
        except subprocess.TimeoutExpired:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=10)
            raise AssertionError(
                "the Java fixture did not exit after `fixture_shutdown`; it was killed.\n"
                + cls.fixture_tail()
            )
        if proc.stdout is not None:
            proc.stdout.close()

    @classmethod
    def fixture_tail(cls, n: int = 30) -> str:
        with cls._lines_lock:
            lines = list(cls.fixture_lines)
        return "--- java fixture output (last %d lines) ---\n%s" % (n, "\n".join(lines[-n:]))

    @classmethod
    def _fixture_properties(cls) -> list[str]:
        """Gradle -P properties for the fixture launch; subclasses add e.g. a token."""
        return ["-PwireFixturePort=0", "-PwireFixtureBind=127.0.0.1"]

    @classmethod
    def _make_client(cls, port: int) -> WireClient:
        """The client the harness connects with; subclasses may pass a token."""
        return WireClient("127.0.0.1", port)

    @classmethod
    def _await_ready(cls, stream: queue.Queue) -> int:
        deadline = time.time() + READY_TIMEOUT
        while time.time() < deadline:
            if cls.process is not None and cls.process.poll() is not None and stream.empty():
                raise AssertionError(
                    "the Java fixture exited (code %s) before printing READY — it never came up.\n%s"
                    % (cls.process.returncode, cls.fixture_tail())
                )
            try:
                line = stream.get(timeout=0.2)
            except queue.Empty:
                continue
            if line.startswith("READY "):
                return int(line.split()[1])
            if line.startswith("FIXTURE-ERROR"):
                raise AssertionError("the Java fixture refused to start: %s\n%s"
                                     % (line, cls.fixture_tail()))
        raise AssertionError(
            "timed out after %.0fs waiting for the Java fixture's READY line.\n%s"
            % (READY_TIMEOUT, cls.fixture_tail())
        )

    @classmethod
    def _connect_with_retry(cls, timeout: float = 30.0):
        assert cls.wire is not None
        deadline = time.time() + timeout
        while time.time() < deadline:
            if cls.wire.connect():
                return
            time.sleep(0.2)
        raise AssertionError("could not connect to the Java fixture on 127.0.0.1:%d\n%s"
                             % (cls.port, cls.fixture_tail()))

    # ── helpers ──────────────────────────────────────────────────────────

    def setUp(self):
        # Messages drained while looking for something else must not be thrown away:
        # the fixture answers `fixture_progress` with progress 0.5 + 1.0 + outcome in
        # one burst, so a lossy drain would hide the tail of the round trip.
        self._pending = []

    def wait_for(self, predicate, timeout: float = 20.0, what: str = "message"):
        """Buffer real WireClient messages until `predicate(msg)` matches."""
        assert self.wire is not None
        deadline = time.time() + timeout
        while time.time() < deadline:
            self._pending.extend(self.wire.drain())
            for index, msg in enumerate(self._pending):
                if predicate(msg):
                    del self._pending[index]
                    return msg
            time.sleep(0.01)
        saw = [(m.type, m.data.get("event") or m.data.get("id")) for m in self._pending[-15:]]
        raise AssertionError(
            "no %s within %.0fs; saw %s\n%s" % (what, timeout, saw, self.fixture_tail())
        )

    def wait_fixture_line(self, needle: str, timeout: float = 20.0) -> str:
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._lines_lock:
                for line in self.fixture_lines:
                    if needle in line:
                        return line
            time.sleep(0.01)
        raise AssertionError("the Java fixture never printed %r\n%s" % (needle, self.fixture_tail()))

    def is_event(self, name: str):
        return lambda m: m.type == "event" and m.data.get("event") == name

    # ── tests ────────────────────────────────────────────────────────────

    def test_01_java_pushes_a_real_state_event(self):
        msg = self.wait_for(
            lambda m: self.is_event("state_update")(m)
            and m.data.get("data", {}).get("fixture") == "java-production-wire",
            what="state_update carrying the fixture marker",
        )
        state = msg.data["data"]
        self.assertTrue(state["in_world"])
        self.assertEqual(state["player"]["health"], 20.0)
        self.assertEqual(state["world"]["world_name"], "wire-fixture")
        self.assertIsInstance(msg.data["ts"], int)  # WireServer.sendEvent stamps every frame
        self.assertTrue(self.wire.connected)

    def test_02_command_round_trip_java_to_python(self):
        req_id = self.wire.send_command("fixture_ping", {"n": 7, "text": "hello"})
        msg = self.wait_for(
            lambda m: m.type == "response" and m.data.get("id") == req_id,
            what="response for %s" % req_id,
        )
        self.assertIs(msg.data["success"], True)
        self.assertEqual(msg.data["data"]["message"], "fixture accepted fixture_ping")
        self.assertEqual(msg.data["data"]["echo"], {"n": 7, "text": "hello"})
        self.assertIsInstance(msg.data["ts"], int)
        # The Java side really received it: its own trace of the decoded command.
        line = self.wait_fixture_line('RECV {"id":"%s","cmd":"fixture_ping"' % req_id)
        self.assertIn('"n":7', line)

    def test_03_progress_and_outcome_are_carried_back(self):
        req_id = self.wire.send_command("fixture_progress", {})
        first = self.wait_for(
            lambda m: m.type == "progress" and m.data.get("id") == req_id,
            what="first progress frame for %s" % req_id,
        )
        self.assertEqual(first.data["progress"], 0.5)
        self.assertEqual(first.data["message"], "fixture half way")

        second = self.wait_for(
            lambda m: m.type == "progress" and m.data.get("id") == req_id
            and m.data.get("progress") == 1.0,
            what="final progress frame for %s" % req_id,
        )
        self.assertEqual(second.data["message"], "fixture arrived")

        outcome = self.wait_for(
            lambda m: m.type == "outcome" and m.data.get("id") == req_id,
            what="outcome frame for %s" % req_id,
        )
        self.assertEqual(outcome.data["status"], "succeeded")
        self.assertEqual(outcome.data["message"], "fixture finished")
        self.assertNotIn("code", outcome.data)

    def test_04_failure_response_keeps_its_reason(self):
        req_id = self.wire.send_command("fixture_fail", {})
        msg = self.wait_for(
            lambda m: m.type == "response" and m.data.get("id") == req_id
            and m.data.get("success") is False,
            what="failing response for %s" % req_id,
        )
        self.assertEqual(msg.data["error"], "fixture said no")

    def test_05_control_frame_reaches_the_java_control_path(self):
        # Fire-and-forget: no id, no response. The only proof it landed is the Java-side
        # ControlStream state, which the fixture folds into the next state_update.
        self.wire.send_control({"forward": True, "jump": True, "sprint": False})
        self.wait_for(
            lambda m: self.is_event("state_update")(m)
            and m.data.get("data", {}).get("control_state", {}).get("forward") is True
            and m.data["data"]["control_state"].get("jump") is True,
            what="state_update reflecting the control frame",
        )
        state = self.wait_for(self.is_event("state_update"), timeout=5.0)
        control = state.data["data"]["control_state"]
        self.assertTrue(control["active"], "ControlStream.apply must have marked the stream active")
        self.assertGreaterEqual(control["stale_ms"], 0)
        self.assertIs(control["sprint"], False)
        # ...and the reader thread survived the unknown-shaped frame: commands still work.
        req_id = self.wire.send_command("fixture_ping", {})
        self.wait_for(
            lambda m: m.type == "response" and m.data.get("id") == req_id,
            what="response after the control frame",
        )


class JavaWireTokenGateTests(JavaWireProtocolIntegrationTests):
    """Token handshake end to end: the **real Java gate** decides, not a Python stand-in.

    The fixture is launched with ``-PwireFixtureToken``, so ``WireServer`` requires a matching
    ``hello`` before it accepts anything. The base class's own cases assume "no token configured",
    so they are unregistered below instead of being left to fail or to silently skip.
    """

    TOKEN = "fixture-token"

    @classmethod
    def _fixture_properties(cls):
        return super()._fixture_properties() + ["-PwireFixtureToken=" + cls.TOKEN]

    @classmethod
    def _make_client(cls, port):
        return WireClient("127.0.0.1", port, token=cls.TOKEN)

    def test_00_valid_hello_is_accepted_and_events_flow(self):
        self.wait_fixture_line("TOKEN required")
        self.wait_fixture_line("Hello accepted")
        msg = self.wait_for(
            lambda m: self.is_event("state_update")(m)
            and m.data.get("data", {}).get("fixture") == "java-production-wire",
            what="state_update after a valid hello",
        )
        self.assertTrue(msg.data["data"]["in_world"])

    def test_01_wrong_token_is_rejected_and_nothing_is_delivered(self):
        bad = WireClient("127.0.0.1", self.port, token="wrong-token")
        try:
            bad.connect()
            line = self.wait_fixture_line("bad token", timeout=20.0)
            self.assertIn("Rejected", line)
            # 反证：被拒绝之后**一个事件都不许到**（否则就是"假拒绝"）。
            time.sleep(1.5)
            self.assertEqual([m for m in bad.drain() if m.type == "event"], [])
        finally:
            bad.disconnect()


# 基类那 5 个用例假定"模组没设 token"；在带 token 的夹具下它们必然失败 ✗ ——
# 明确注销（而不是让它们变红、或者偷偷 skip 掉）。
for _name in [n for n in dir(JavaWireProtocolIntegrationTests) if n.startswith("test_")]:
    setattr(JavaWireTokenGateTests, _name, None)


if __name__ == "__main__":
    unittest.main()
