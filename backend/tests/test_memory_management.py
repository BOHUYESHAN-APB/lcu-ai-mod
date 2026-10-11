import unittest

from agent.memory_catalog import MemoryQuery
from agent.memory_management import MemoryPreviewError, MemoryPreviewStore
from agent.memory_overlay import MemoryOverlayStore
import tempfile
from pathlib import Path


class DummyMemory:
    def __init__(self, save_result=True):
        self.summaries = []
        self._save_blocked_reason = None
        self.save_result = save_result

    def add_summary(self, summary):
        self.summaries.append(dict(summary))

    def save(self):
        return self.save_result


class MemoryPreviewStoreTests(unittest.TestCase):
    def setUp(self):
        self.records = [{
            "id": "message:1",
            "category": "message",
            "occurred_at": 10.0,
            "updated_at": 10.0,
            "content": {"sender": "Alice", "message": "find diamonds"},
        }]
        self.store = MemoryPreviewStore(ttl_seconds=60)
        self.preview = self.store.create_summary_preview(
            query=MemoryQuery(categories=frozenset({"message"})),
            records=self.records,
            source_revision="revision-one",
            summary="Alice wants to find diamonds.",
            agent="default",
            model="test-model",
            target_tokens=128,
        )

    def test_commit_persists_summary_and_retains_source_contract(self):
        memory = DummyMemory()

        summary = self.store.commit(
            self.preview["id"], current_revision="revision-one",
            current_records=self.records, memory=memory,
        )
        repeated = self.store.commit(
            self.preview["id"], current_revision="revision-one",
            current_records=self.records, memory=memory,
        )

        self.assertEqual(summary, repeated)
        self.assertEqual(len(memory.summaries), 1)
        self.assertTrue(summary["source_records_retained"])
        self.assertEqual(summary["source_ids"], ["message:1"])

    def test_stale_revision_or_changed_source_is_rejected(self):
        with self.assertRaisesRegex(MemoryPreviewError, "changed"):
            self.store.commit(
                self.preview["id"], current_revision="revision-two",
                current_records=self.records, memory=DummyMemory(),
            )
        changed = [{**self.records[0], "content": {"message": "changed"}}]
        with self.assertRaisesRegex(MemoryPreviewError, "changed"):
            self.store.commit(
                self.preview["id"], current_revision="revision-one",
                current_records=changed, memory=DummyMemory(),
            )

    def test_failed_save_rolls_back_in_memory_summary(self):
        memory = DummyMemory(save_result=False)

        with self.assertRaisesRegex(MemoryPreviewError, "persist"):
            self.store.commit(
                self.preview["id"], current_revision="revision-one",
                current_records=self.records, memory=memory,
            )

        self.assertEqual(memory.summaries, [])

    def test_empty_selection_or_summary_is_rejected(self):
        with self.assertRaises(MemoryPreviewError):
            self.store.create_summary_preview(
                query=MemoryQuery(), records=[], source_revision="r", summary="x",
                agent="default", model="model", target_tokens=64,
            )

    def test_state_preview_requires_confirmation_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            overlay = MemoryOverlayStore(Path(tmp) / "memory_management.db")
            records = [{**self.records[0], "state": "active", "category": "message"}]
            preview = self.store.create_state_preview(
                action="archive", records=records, source_revision="revision-one",
                changes={"archived": ["message:1"]}, reason="manual",
            )

            with self.assertRaisesRegex(MemoryPreviewError, "token"):
                self.store.commit_state_preview(
                    preview["id"], confirmation_token="wrong",
                    confirmation_text=preview["confirmation_text"], current_revision="revision-one",
                    current_records=records, overlay_store=overlay, scope_id="global",
                )
            result = self.store.commit_state_preview(
                preview["id"], confirmation_token=preview["confirmation_token"],
                confirmation_text=preview["confirmation_text"], current_revision="revision-one",
                current_records=records, overlay_store=overlay, scope_id="global",
            )
            repeated = self.store.commit_state_preview(
                preview["id"], confirmation_token=preview["confirmation_token"],
                confirmation_text=preview["confirmation_text"], current_revision="revision-one",
                current_records=records, overlay_store=overlay, scope_id="global",
            )
            states = overlay.get_states("global")
            overlay.close()

        self.assertEqual(result, repeated)
        self.assertEqual(states["message:1"], "archived")
        with self.assertRaises(MemoryPreviewError):
            self.store.create_summary_preview(
                query=MemoryQuery(), records=self.records, source_revision="r", summary=" ",
                agent="default", model="model", target_tokens=64,
            )


if __name__ == "__main__":
    unittest.main()
