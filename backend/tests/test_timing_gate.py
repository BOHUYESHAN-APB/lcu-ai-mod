import unittest

from agent.timing_gate import TimingGate


class TimingGateTests(unittest.TestCase):
    def test_wake_name_forces_response(self):
        gate = TimingGate(llm_service=None)

        should_respond, reason = gate.should_respond(
            sender="BoHuYeShan",
            message="小A 跟着我",
            recent_messages=[],
            bot_name="AI",
            wake_names=["AI", "小A"],
        )

        self.assertTrue(should_respond)
        self.assertEqual(reason, "mentioned:小A")


if __name__ == "__main__":
    unittest.main()
