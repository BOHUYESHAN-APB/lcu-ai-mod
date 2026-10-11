import unittest

from agent.server_command_policy import evaluate, normalize_policy


class ServerCommandPolicyTests(unittest.TestCase):
    def test_default_families_accept_requested_teleport_and_home_commands(self):
        policy = normalize_policy()

        self.assertEqual(evaluate("/home base", policy)["family"], "home")
        self.assertEqual(evaluate("/tpa Alice", policy)["family"], "tpa")
        self.assertEqual(evaluate("/tpaccept Alice", policy)["family"], "tpaccept")
        self.assertEqual(evaluate("/tp Alice Bob", policy)["family"], "tp")

    def test_unsafe_or_unknown_commands_are_rejected(self):
        policy = normalize_policy()

        for command in ("/op Alice", "/tp @a Bob", "//tp Alice", "/home base;op Alice", "/execute run tp Alice"):
            with self.subTest(command=command):
                with self.assertRaises(ValueError):
                    evaluate(command, policy)

    def test_policy_can_disable_or_remove_family(self):
        with self.assertRaisesRegex(ValueError, "disabled"):
            evaluate("/home", {"enabled": False})
        with self.assertRaisesRegex(ValueError, "not allowed"):
            evaluate("/tp Alice", {"allowed_families": ["home"]})


if __name__ == "__main__":
    unittest.main()
