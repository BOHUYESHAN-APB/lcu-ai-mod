import tempfile
import unittest
from pathlib import Path

from agent.scoped_knowledge import ScopedKnowledgeStore


class ScopedKnowledgeStoreTests(unittest.TestCase):
    def test_more_specific_scope_overrides_shared_template(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ScopedKnowledgeStore(Path(tmp) / "knowledge.json")
            context = {"pack_fingerprint": "pack-a", "server_id": "server-a", "world_id": "world-a"}
            chain = store.scope_chain("companion", context)
            store.put(chain[0], "machine.start", {
                "steps": [{"action": "key_press", "mapping_id": "key.start"}],
                "state": "approved",
            })
            store.put(chain[-1], "machine.start", {
                "steps": [{"action": "ui_click", "target": "start"}],
                "state": "approved",
            })

            effective = store.resolve(chain)

            self.assertEqual(effective[0]["steps"][0]["action"], "ui_click")
            self.assertEqual(effective[0]["scope_key"], chain[-1])

    def test_tombstone_suppresses_shared_template(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ScopedKnowledgeStore(Path(tmp) / "knowledge.json")
            chain = store.scope_chain("companion", {"server_id": "server-a"})
            store.put(chain[0], "shared", {"steps": [{"action": "observe_gui"}]})
            store.put(chain[-1], "shared", {"steps": [{"action": "observe_gui"}], "state": "tombstone"})

            self.assertEqual(store.resolve(chain), [])

    def test_executable_code_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ScopedKnowledgeStore(Path(tmp) / "knowledge.json")
            with self.assertRaisesRegex(ValueError, "code"):
                store.put("companion:global", "unsafe", {
                    "steps": [{"action": "ui_click", "script": "click()"}],
                })


if __name__ == "__main__":
    unittest.main()
