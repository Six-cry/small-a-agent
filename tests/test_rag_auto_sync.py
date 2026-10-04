import unittest
from dataclasses import dataclass

from aa_my_agent.rag.auto_sync import (
    _parse_enabled,
    _reset_auto_sync_state_for_tests,
    ensure_rag_index_current,
)


@dataclass
class FakeSyncResult:
    added_files: int = 1
    changed_files: int = 0
    deleted_files: int = 0
    total_chunks: int = 12


class RagAutoSyncTests(unittest.TestCase):
    def setUp(self):
        _reset_auto_sync_state_for_tests()

    def test_auto_sync_is_enabled_by_default(self):
        self.assertIs(_parse_enabled(None), True)
        self.assertIs(_parse_enabled("true"), True)
        self.assertIs(_parse_enabled("0"), False)

    def test_up_to_date_does_not_create_embeddings(self):
        sync_calls = []

        outcome = ensure_rag_index_current(
            force=True,
            enabled=True,
            has_changes=lambda: False,
            sync=lambda: sync_calls.append(True),
            output=lambda _message: None,
        )

        self.assertEqual(outcome.status, "up_to_date")
        self.assertEqual(sync_calls, [])

    def test_changes_trigger_incremental_sync(self):
        sync_calls = []

        def sync():
            sync_calls.append(True)
            return FakeSyncResult()

        outcome = ensure_rag_index_current(
            force=True,
            enabled=True,
            has_changes=lambda: True,
            sync=sync,
            output=lambda _message: None,
        )

        self.assertEqual(outcome.status, "synced")
        self.assertEqual(sync_calls, [True])
        self.assertIn("新增 1 个", outcome.message)

    def test_search_time_checks_are_throttled(self):
        checks = []

        def has_changes():
            checks.append(True)
            return False

        ensure_rag_index_current(
            enabled=True,
            check_interval_seconds=5,
            has_changes=has_changes,
            now=lambda: 10,
            output=lambda _message: None,
        )
        outcome = ensure_rag_index_current(
            enabled=True,
            check_interval_seconds=5,
            has_changes=has_changes,
            now=lambda: 12,
            output=lambda _message: None,
        )

        self.assertEqual(outcome.status, "up_to_date")
        self.assertEqual(checks, [True])

    def test_sync_failure_keeps_caller_running(self):
        def fail():
            raise RuntimeError("embedding service unavailable")

        outcome = ensure_rag_index_current(
            force=True,
            enabled=True,
            has_changes=lambda: True,
            sync=fail,
            output=lambda _message: None,
        )

        self.assertEqual(outcome.status, "failed")
        self.assertIs(outcome.succeeded, False)
        self.assertIn("原索引", outcome.message)


if __name__ == "__main__":
    unittest.main()
