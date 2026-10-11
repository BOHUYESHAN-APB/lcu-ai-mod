import unittest

from agent.storage_policy import enforce_storage_policy


class StoragePolicyTests(unittest.TestCase):
    def test_default_development_policy_allows_only_local_sqlite(self):
        policy = enforce_storage_policy("development", "sqlite", "")

        self.assertEqual(policy.environment, "development")
        self.assertEqual(policy.backend, "sqlite")
        self.assertFalse(policy.production_ready)

    def test_production_rejects_sqlite_without_fallback(self):
        with self.assertRaisesRegex(RuntimeError, "SQLite is not permitted"):
            enforce_storage_policy("production", "sqlite", "")

    def test_production_requires_postgresql_url(self):
        with self.assertRaisesRegex(RuntimeError, "PostgreSQL DATABASE_URL"):
            enforce_storage_policy("production", "postgresql", "sqlite:///local.db")

    def test_production_remains_blocked_until_adapter_exists(self):
        with self.assertRaisesRegex(RuntimeError, "production storage is not implemented"):
            enforce_storage_policy("production", "postgresql", "postgresql://db.example/lcu")

    def test_unknown_environment_and_backend_are_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "LCU_ENV"):
            enforce_storage_policy("staging", "sqlite", "")
        with self.assertRaisesRegex(RuntimeError, "LCU_STORAGE_BACKEND"):
            enforce_storage_policy("development", "memory", "")


if __name__ == "__main__":
    unittest.main()
