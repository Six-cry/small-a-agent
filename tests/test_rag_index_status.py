"""验证在线索引检查始终只读，并能稳定提醒独立同步。"""

from dataclasses import dataclass

from aa_my_agent.rag.index_status import (
    SYNC_COMMAND,
    _reset_index_status_state_for_tests,
    check_rag_index_status,
)


@dataclass
class _Diff:
    added: tuple = ()
    changed: tuple = ()
    deleted: tuple = ()

    @property
    def has_changes(self):
        return bool(self.added or self.changed or self.deleted)


def setup_function():
    _reset_index_status_state_for_tests()


def test_up_to_date_status_is_read_only():
    outcome = check_rag_index_status(
        force=True,
        diff_provider=lambda: _Diff(),
        output=lambda _message: None,
    )

    assert outcome.status == "up_to_date"
    assert not outcome.is_stale


def test_changes_only_return_stale_reminder_and_sync_command():
    outcome = check_rag_index_status(
        force=True,
        diff_provider=lambda: _Diff(
            added=(object(),),
            changed=(object(), object()),
            deleted=(object(),),
        ),
        output=lambda _message: None,
    )

    assert outcome.status == "stale"
    assert outcome.added_files == 1
    assert outcome.changed_files == 2
    assert outcome.deleted_files == 1
    assert SYNC_COMMAND in outcome.message
    assert "继续只读使用上一次成功索引" in outcome.message


def test_search_time_status_checks_are_throttled():
    calls = []

    def diff_provider():
        calls.append(True)
        return _Diff()

    check_rag_index_status(
        diff_provider=diff_provider,
        check_interval_seconds=30,
        now=lambda: 10,
    )
    outcome = check_rag_index_status(
        diff_provider=diff_provider,
        check_interval_seconds=30,
        now=lambda: 20,
    )

    assert outcome.status == "up_to_date"
    assert calls == [True]


def test_check_failure_never_attempts_to_modify_index():
    def fail():
        raise RuntimeError("manifest unavailable")

    outcome = check_rag_index_status(
        force=True,
        diff_provider=fail,
        output=lambda _message: None,
    )

    assert outcome.status == "failed"
    assert not outcome.succeeded
    assert "不会自动修改索引" in outcome.message
