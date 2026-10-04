"""在线阶段只读检查知识文件是否存在尚未入库的变化。

本模块只比较知识目录快照与 Manifest，不解析文档、不生成 Embedding，
也不写入 Chroma 或 BM25。真正的入库必须由独立的 sync_knowledge
命令显式执行，从而保证小 A 启动和问答阶段始终是只读检索流程。
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from time import monotonic
from typing import Callable


DEFAULT_STATUS_INTERVAL_SECONDS = 30.0
SYNC_COMMAND = "python -m aa_my_agent.rag.sync_knowledge sync"


@dataclass(frozen=True, slots=True)
class RagIndexStatus:
    """一次只读索引状态检查的结果。"""

    status: str
    message: str
    added_files: int = 0
    changed_files: int = 0
    deleted_files: int = 0
    added_paths: tuple[str, ...] = ()
    changed_paths: tuple[str, ...] = ()
    deleted_paths: tuple[str, ...] = ()

    @property
    def is_stale(self) -> bool:
        return self.status == "stale"

    @property
    def succeeded(self) -> bool:
        return self.status != "failed"


_status_lock = threading.Lock()
_last_check_at: float | None = None
_last_status = RagIndexStatus(
    status="not_checked",
    message="知识库尚未检查。",
)


def _parse_interval(value: str | None) -> float:
    if value is None or not value.strip():
        return DEFAULT_STATUS_INTERVAL_SECONDS
    try:
        interval = float(value)
    except ValueError as exc:
        raise ValueError(
            "RAG_INDEX_STATUS_CHECK_INTERVAL_SECONDS 必须是非负数"
        ) from exc
    if interval < 0:
        raise ValueError(
            "RAG_INDEX_STATUS_CHECK_INTERVAL_SECONDS 必须是非负数"
        )
    return interval


def _default_diff():
    """只比较文件快照和 Manifest，不打开写入流程。"""
    from .index_diff import calculate_index_diff
    from .manifest import create_default_manifest_store
    from .source_scanner import create_default_source_scanner

    manifest = create_default_manifest_store().load()
    current_sources = create_default_source_scanner().scan_metadata(
        manifest.sources
    )
    return calculate_index_diff(
        current_sources=current_sources,
        manifest=manifest,
    )


def _count_items(diff, field_name: str) -> int:
    value = getattr(diff, field_name, ())
    try:
        return len(value)
    except TypeError as exc:
        raise ValueError(f"索引差异字段无效：{field_name}") from exc


def _paths(diff, field_name: str) -> tuple[str, ...]:
    paths = []
    for item in getattr(diff, field_name, ()):
        if field_name == "changed":
            item = getattr(item, "current", item)
        value = getattr(item, "relative_path", None)
        paths.append(str(value) if value is not None else str(item))
    return tuple(paths)


def _status_from_diff(diff) -> RagIndexStatus:
    added = _count_items(diff, "added")
    changed = _count_items(diff, "changed")
    deleted = _count_items(diff, "deleted")
    has_changes = bool(getattr(diff, "has_changes", added or changed or deleted))
    if not has_changes:
        return RagIndexStatus(
            status="up_to_date",
            message="知识库文件与当前索引一致。",
        )
    return RagIndexStatus(
        status="stale",
        message=(
            "检测到尚未入库的知识文件变化："
            f"新增 {added} 个，修改 {changed} 个，删除 {deleted} 个。"
            "小 A 将继续只读使用上一次成功索引；请退出小 A 后运行："
            f"{SYNC_COMMAND}"
        ),
        added_files=added,
        changed_files=changed,
        deleted_files=deleted,
        added_paths=_paths(diff, "added"),
        changed_paths=_paths(diff, "changed"),
        deleted_paths=_paths(diff, "deleted"),
    )


def check_rag_index_status(
    *,
    force: bool = False,
    announce: bool = False,
    check_interval_seconds: float | None = None,
    diff_provider: Callable[[], object] | None = None,
    now: Callable[[], float] = monotonic,
    output: Callable[[str], None] = print,
) -> RagIndexStatus:
    """检查索引是否过期；无论结果如何都不会执行同步。"""
    global _last_check_at, _last_status

    try:
        if check_interval_seconds is None:
            check_interval_seconds = _parse_interval(
                os.getenv("RAG_INDEX_STATUS_CHECK_INTERVAL_SECONDS")
            )
        if check_interval_seconds < 0:
            raise ValueError("check_interval_seconds 必须是非负数")
    except Exception as exc:
        status = RagIndexStatus(
            status="failed",
            message=f"知识库只读状态检查配置错误：{exc}",
        )
        if announce:
            output(f"[RAG] {status.message}")
        return status

    current_time = now()
    with _status_lock:
        if (
            not force
            and _last_check_at is not None
            and current_time - _last_check_at < check_interval_seconds
        ):
            status = _last_status
        else:
            try:
                status = _status_from_diff((diff_provider or _default_diff)())
            except Exception as exc:
                status = RagIndexStatus(
                    status="failed",
                    message=(
                        "知识库只读状态检查失败；小 A 不会自动修改索引："
                        f"{type(exc).__name__}: {exc}"
                    ),
                )
            _last_check_at = now()
            _last_status = status

    if announce and status.status in {"stale", "failed"}:
        output(f"[RAG] {status.message}")
    return status


def _reset_index_status_state_for_tests() -> None:
    """重置节流缓存，仅供隔离测试。"""
    global _last_check_at, _last_status
    with _status_lock:
        _last_check_at = None
        _last_status = RagIndexStatus(
            status="not_checked",
            message="知识库尚未检查。",
        )


__all__ = [
    "RagIndexStatus",
    "SYNC_COMMAND",
    "check_rag_index_status",
]
