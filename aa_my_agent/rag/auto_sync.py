"""为显式评测命令保留的增量同步兼容层。

正式小 A 的启动与检索链路不再调用本模块。只有评测脚本显式传入
``--sync`` 等场景才会使用它；日常知识库写入统一通过
``python -m aa_my_agent.rag.sync_knowledge sync`` 完成。
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from time import monotonic
from typing import Callable


DEFAULT_CHECK_INTERVAL_SECONDS = 5.0
TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
FALSE_VALUES = frozenset({"0", "false", "no", "off"})


@dataclass(frozen=True, slots=True)
class AutoSyncOutcome:
    """Result of one automatic index check."""

    status: str
    message: str

    @property
    def succeeded(self) -> bool:
        return self.status != "failed"


_sync_lock = threading.Lock()
_last_check_at: float | None = None
_last_outcome = AutoSyncOutcome(
    status="not_checked",
    message="知识库尚未检查。",
)


def _parse_enabled(value: str | None) -> bool:
    """Read RAG_AUTO_SYNC, defaulting to enabled."""
    if value is None or not value.strip():
        return True

    normalized = value.strip().casefold()
    if normalized in TRUE_VALUES:
        return True
    if normalized in FALSE_VALUES:
        return False

    raise ValueError(
        "RAG_AUTO_SYNC 必须是 1/0、true/false、yes/no 或 on/off"
    )


def _parse_interval(value: str | None) -> float:
    """Read the optional search-time check interval."""
    if value is None or not value.strip():
        return DEFAULT_CHECK_INTERVAL_SECONDS

    try:
        interval = float(value)
    except ValueError as exc:
        raise ValueError(
            "RAG_AUTO_SYNC_INTERVAL_SECONDS 必须是非负数"
        ) from exc

    if interval < 0:
        raise ValueError(
            "RAG_AUTO_SYNC_INTERVAL_SECONDS 必须是非负数"
        )

    return interval


def _default_has_changes() -> bool:
    """Compare source snapshots with the manifest without opening Chroma."""
    from .index_diff import calculate_index_diff
    from .manifest import create_default_manifest_store
    from .source_scanner import create_default_source_scanner

    manifest = create_default_manifest_store().load()
    current_sources = create_default_source_scanner().scan()
    diff = calculate_index_diff(
        current_sources=current_sources,
        manifest=manifest,
    )
    return diff.has_changes


def _default_sync():
    """Run the existing safe incremental synchronization workflow."""
    from .index_manager import create_default_index_manager

    return create_default_index_manager().sync()


def _sync_message(result) -> str:
    added = getattr(result, "added_files", 0)
    changed = getattr(result, "changed_files", 0)
    deleted = getattr(result, "deleted_files", 0)
    chunks = getattr(result, "total_chunks", 0)
    return (
        "知识库自动同步完成："
        f"新增 {added} 个，更新 {changed} 个，删除 {deleted} 个；"
        f"当前共 {chunks} 个文本块。"
    )


def ensure_rag_index_current(
    *,
    force: bool = False,
    announce: bool = False,
    enabled: bool | None = None,
    check_interval_seconds: float | None = None,
    has_changes: Callable[[], bool] | None = None,
    sync: Callable[[], object] | None = None,
    now: Callable[[], float] = monotonic,
    output: Callable[[str], None] = print,
) -> AutoSyncOutcome:
    """供显式调用者检查并同步索引；在线 Agent 不得调用。"""
    global _last_check_at, _last_outcome

    try:
        if enabled is None:
            enabled = _parse_enabled(os.getenv("RAG_AUTO_SYNC"))

        if check_interval_seconds is None:
            check_interval_seconds = _parse_interval(
                os.getenv("RAG_AUTO_SYNC_INTERVAL_SECONDS")
            )

        if check_interval_seconds < 0:
            raise ValueError("check_interval_seconds 必须是非负数")
    except Exception as exc:
        outcome = AutoSyncOutcome(
            status="failed",
            message=f"知识库自动同步配置错误：{exc}",
        )
        if announce:
            output(f"[RAG] {outcome.message}")
        return outcome

    if not enabled:
        outcome = AutoSyncOutcome(
            status="disabled",
            message="知识库自动同步已关闭。",
        )
        if announce:
            output(f"[RAG] {outcome.message}")
        return outcome

    current_time = now()
    with _sync_lock:
        if (
            not force
            and _last_check_at is not None
            and current_time - _last_check_at < check_interval_seconds
        ):
            return _last_outcome

        previous_outcome = _last_outcome
        try:
            change_checker = has_changes or _default_has_changes
            if not change_checker():
                outcome = AutoSyncOutcome(
                    status="up_to_date",
                    message="知识库已是最新状态。",
                )
            else:
                sync_operation = sync or _default_sync
                outcome = AutoSyncOutcome(
                    status="synced",
                    message=_sync_message(sync_operation()),
                )
        except Exception as exc:
            outcome = AutoSyncOutcome(
                status="failed",
                message=(
                    "知识库自动同步失败，暂时继续使用原索引："
                    f"{type(exc).__name__}: {exc}"
                ),
            )

        _last_check_at = now()
        _last_outcome = outcome

    should_announce = (
        announce
        or outcome.status == "synced"
        or (
            outcome.status == "failed"
            and outcome != previous_outcome
        )
    )
    if should_announce:
        output(f"[RAG] {outcome.message}")

    return outcome


def _reset_auto_sync_state_for_tests() -> None:
    """Reset throttling state; intended only for isolated tests."""
    global _last_check_at, _last_outcome

    with _sync_lock:
        _last_check_at = None
        _last_outcome = AutoSyncOutcome(
            status="not_checked",
            message="知识库尚未检查。",
        )
