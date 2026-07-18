import os
import queue
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path

from agent.agent_state import AgentStateDB
from agent.orchestrator import Orchestrator
from agent.skill_registry import SkillRegistry
from agent.task_coordinator import TaskCoordinator
from protocol.wire_client import WireClient


ROOT = Path(__file__).resolve().parents[3]


@unittest.skipUnless(
    os.getenv("LCU_RUN_JAVA_INTEGRATION") == "1",
    "set LCU_RUN_JAVA_INTEGRATION=1 to launch the real Java WireServer fixture",
)
class JavaWireProtocolIntegrationTests(unittest.TestCase):
    def test_java_wire_to_python_orchestrator_durable_run(self):
        token = "java-python-integration"
        process = subprocess.Popen(
            [
                str(ROOT / "gradlew.bat"), "runWireFixture", "--no-daemon", "-q",
                "-PwireFixturePort=0", f"-PwireFixtureToken={token}",
            ],
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        output: queue.Queue[str] = queue.Queue()

        def read_output():
            assert process.stdout is not None
            for line in process.stdout:
                output.put(line.rstrip())

        reader = threading.Thread(target=read_output, daemon=True)
        reader.start()
        wire = None
        orchestrator = None
        state = None
        temporary = None
        try:
            port = self._wait_ready(process, output)
            wire = WireClient("127.0.0.1", port, token=token)
            self.assertTrue(wire.connect())
            self.assertEqual(wire.peer_info["role"], "body_client")

            temporary = tempfile.TemporaryDirectory()
            root = Path(temporary.name)
            orchestrator = Orchestrator(
                wire, companion_id="java-integration", storage_root=root, legacy_root=None,
            )
            state = AgentStateDB(root / "agent_state.db")
            coordinator = TaskCoordinator(state, SkillRegistry(), wire)
            orchestrator.set_task_coordinator(coordinator)
            orchestrator.start()

            self._tick_until(
                orchestrator,
                lambda: orchestrator.session.runtime.get("fixture") == "java-production-wire",
            )
            self.assertEqual(orchestrator.session.runtime["player"]["health"], 20.0)
            self.assertTrue(orchestrator.session.runtime["control_state"]["ai_controlled"])

            run = coordinator.create_run("core.jump", {})
            self.assertIn(run["status"], {"dispatched", "running"})
            self._tick_until(
                orchestrator,
                lambda: state.get_run(run["id"])["status"] == "succeeded",
            )
            completed = state.get_run(run["id"])
            self.assertEqual(completed["request_id"], run["id"])
            self.assertIn("fixture accepted jump", completed["detail"])
        finally:
            if wire and wire.is_connected:
                try:
                    wire.send_command("fixture_shutdown")
                except ConnectionError:
                    pass
            if orchestrator:
                orchestrator.stop()
                orchestrator.session.stop()
            if state:
                state.close()
            if wire:
                wire.disconnect()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.terminate()
                process.wait(timeout=5)
            if process.stdout:
                process.stdout.close()
            if temporary:
                temporary.cleanup()

    @staticmethod
    def _wait_ready(process, output: queue.Queue[str]) -> int:
        deadline = time.time() + 90
        seen = []
        while time.time() < deadline:
            if process.poll() is not None and output.empty():
                raise AssertionError(f"Java fixture exited before READY: {' | '.join(seen[-20:])}")
            try:
                line = output.get(timeout=0.2)
            except queue.Empty:
                continue
            seen.append(line)
            if line.startswith("READY "):
                return int(line.split()[1])
        raise AssertionError(f"Timed out waiting for Java fixture: {' | '.join(seen[-20:])}")

    @staticmethod
    def _tick_until(orchestrator, predicate, timeout=5.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            orchestrator.tick()
            if predicate():
                return
            time.sleep(0.01)
        raise AssertionError("orchestrator condition did not become true")


if __name__ == "__main__":
    unittest.main()
