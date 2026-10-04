"""把已验证的结构化 Chunk 安全提升为 Agent 使用的生产索引。

采用两阶段切换：

1. stage：在正式 Chroma 目录中构建新的独立 Collection，保留旧 Collection；
2. activate：配置切换后，核对新 Collection 并原子更新生产 Manifest。

这样不会在向量化尚未完成时破坏旧索引，也为人工回退保留原 Collection、
原 Manifest 和原 config.py。
"""

from __future__ import annotations

import argparse
import json
import shutil
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from langchain_core.documents import Document

from ...config import APP_DIR, CHROMA_DIR, RAG_COLLECTION_NAME, RAG_MANIFEST_PATH
from ...rag.index_diff import calculate_index_diff
from ...rag.manifest import IndexManifest, create_default_index_signature
from ...rag.models import IndexedSourceRecord, SourceSnapshot
from ...rag.source_scanner import create_default_source_scanner
from ...rag.vector_store import VectorStoreService
from .build_structured_test_index import (
    DEFAULT_CHUNKS_PATH,
    calculate_file_hash,
    load_chunks_jsonl,
    run_smoke_probes,
)


TARGET_COLLECTION_NAME = "rag_collection_knowledge_docling_v1"
REPORT_DIR = Path(__file__).resolve().parent / "reports" / "production_promotion"
STAGE_REPORT_PATH = REPORT_DIR / "stage_report.json"
ACTIVATION_REPORT_PATH = REPORT_DIR / "activation_report.json"
BACKUP_ROOT = Path(__file__).resolve().parent / "storage" / "production_backups"


def _group_chunks(chunks: list[Document]) -> dict[str, list[Document]]:
    grouped: dict[str, list[Document]] = defaultdict(list)
    for chunk in chunks:
        source = chunk.metadata.get("source")
        if not isinstance(source, str) or not source.strip():
            raise ValueError("Chunk 缺少有效 source")
        grouped[source.strip()].append(chunk)
    return dict(grouped)


def validate_chunks_against_sources(
    chunks: list[Document],
    snapshots: dict[str, SourceSnapshot],
) -> dict[str, list[Document]]:
    """确认待提升 Chunk 与知识目录当前文件完全对应。"""
    grouped = _group_chunks(chunks)
    chunk_sources = set(grouped)
    source_paths = set(snapshots)
    if chunk_sources != source_paths:
        missing = sorted(source_paths - chunk_sources)
        extra = sorted(chunk_sources - source_paths)
        raise ValueError(
            "完整 Chunk 与当前知识目录来源不一致："
            f"缺少={missing}，多出={extra}"
        )

    for source, source_chunks in grouped.items():
        snapshot = snapshots[source]
        for chunk in source_chunks:
            if chunk.metadata.get("file_hash") != snapshot.file_hash:
                raise ValueError(f"Chunk 文件 Hash 已过期：{source}")
            file_size = chunk.metadata.get("file_size")
            if int(file_size) != snapshot.file_size:
                raise ValueError(f"Chunk 文件大小已过期：{source}")
            if chunk.metadata.get("file_type") != snapshot.file_type:
                raise ValueError(f"Chunk 文件类型不一致：{source}")
    return grouped


def build_production_manifest(
    *,
    grouped_chunks: dict[str, list[Document]],
    snapshots: dict[str, SourceSnapshot],
) -> IndexManifest:
    """使用当前源快照和结构化 Chunk ID 构造生产 Manifest。"""
    indexed_at = datetime.now(timezone.utc).isoformat()
    manifest = IndexManifest.empty(create_default_index_signature())
    for source in sorted(snapshots):
        chunk_ids = tuple(
            str(chunk.metadata["chunk_id"])
            for chunk in grouped_chunks[source]
        )
        manifest.sources[source] = IndexedSourceRecord.from_snapshot(
            snapshot=snapshots[source],
            chunk_ids=chunk_ids,
            indexed_at=indexed_at,
        )
    return manifest


def _create_backup() -> Path:
    timestamp = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S")
    backup_dir = BACKUP_ROOT / timestamp
    backup_dir.mkdir(parents=True, exist_ok=False)
    config_path = APP_DIR / "config.py"
    shutil.copy2(config_path, backup_dir / "config.py")
    if RAG_MANIFEST_PATH.is_file():
        shutil.copy2(RAG_MANIFEST_PATH, backup_dir / "rag_manifest.json")
    return backup_dir


def stage(chunks_path: Path) -> dict:
    if RAG_COLLECTION_NAME == TARGET_COLLECTION_NAME:
        raise RuntimeError("当前配置已经指向目标 Collection，不能重复执行 stage")

    resolved_chunks = chunks_path.expanduser().resolve(strict=True)
    chunks = load_chunks_jsonl([resolved_chunks])
    snapshots = create_default_source_scanner().scan()
    validate_chunks_against_sources(chunks, snapshots)
    backup_dir = _create_backup()

    store = VectorStoreService(
        collection_name=TARGET_COLLECTION_NAME,
        persist_directory=CHROMA_DIR,
    )
    existing_ids = store.get_chunk_ids()
    if existing_ids:
        store.delete_chunks(existing_ids)
    written_ids = store.add_chunks(chunks)
    expected_ids = {str(chunk.metadata["chunk_id"]) for chunk in chunks}
    actual_ids = set(store.get_chunk_ids())
    if set(written_ids) != expected_ids or actual_ids != expected_ids:
        raise RuntimeError("候选生产 Collection 的 Chunk ID 与输入不一致")

    probes = run_smoke_probes(store)
    payload = {
        "staged_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "chunks_path": str(resolved_chunks),
        "chunks_sha256": calculate_file_hash(resolved_chunks),
        "chunk_count": len(actual_ids),
        "source_count": len(snapshots),
        "old_collection_name": RAG_COLLECTION_NAME,
        "target_collection_name": TARGET_COLLECTION_NAME,
        "chroma_dir": str(CHROMA_DIR.resolve()),
        "backup_dir": str(backup_dir.resolve()),
        "probes": [
            {
                "name": probe.name,
                "passed": probe.passed,
                "reason": probe.reason,
                "best_distance": probe.best_distance,
            }
            for probe in probes
        ],
    }
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    STAGE_REPORT_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return payload


def activate(chunks_path: Path) -> dict:
    if RAG_COLLECTION_NAME != TARGET_COLLECTION_NAME:
        raise RuntimeError(
            "当前配置尚未指向目标 Collection："
            f"配置={RAG_COLLECTION_NAME}，目标={TARGET_COLLECTION_NAME}"
        )
    if not STAGE_REPORT_PATH.is_file():
        raise FileNotFoundError(f"缺少 stage 报告：{STAGE_REPORT_PATH}")

    stage_report = json.loads(STAGE_REPORT_PATH.read_text(encoding="utf-8"))
    resolved_chunks = chunks_path.expanduser().resolve(strict=True)
    if calculate_file_hash(resolved_chunks) != stage_report.get("chunks_sha256"):
        raise RuntimeError("stage 后完整 Chunk 文件发生变化，拒绝激活")

    chunks = load_chunks_jsonl([resolved_chunks])
    snapshots = create_default_source_scanner().scan()
    grouped = validate_chunks_against_sources(chunks, snapshots)
    expected_ids = {str(chunk.metadata["chunk_id"]) for chunk in chunks}

    store = VectorStoreService()
    actual_ids = set(store.get_chunk_ids())
    if actual_ids != expected_ids:
        raise RuntimeError(
            "目标生产 Collection 与完整 Chunk 不一致："
            f"缺少={len(expected_ids - actual_ids)}，"
            f"多出={len(actual_ids - expected_ids)}"
        )

    manifest = build_production_manifest(
        grouped_chunks=grouped,
        snapshots=snapshots,
    )
    from ...rag.manifest import create_default_manifest_store

    manifest_store = create_default_manifest_store()
    manifest_store.save(manifest)
    if set(
        chunk_id
        for record in manifest.sources.values()
        for chunk_id in record.chunk_ids
    ) != actual_ids:
        raise RuntimeError("生产 Manifest 保存后与 Chroma ID 不一致")

    diff = calculate_index_diff(current_sources=snapshots, manifest=manifest)
    if diff.has_changes:
        raise RuntimeError("激活后源文件仍被判定为有变化")

    payload = {
        "activated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "collection_name": RAG_COLLECTION_NAME,
        "chroma_dir": str(CHROMA_DIR.resolve()),
        "manifest_path": str(RAG_MANIFEST_PATH.resolve()),
        "chunk_count": len(actual_ids),
        "source_count": len(manifest.sources),
        "unchanged_sources": len(diff.unchanged),
        "backup_dir": stage_report["backup_dir"],
    }
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    ACTIVATION_REPORT_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description="提升结构化 RAG 索引到生产环境")
    parser.add_argument("action", choices=("stage", "activate"))
    parser.add_argument("--chunks", type=Path, default=DEFAULT_CHUNKS_PATH)
    args = parser.parse_args()
    try:
        result = stage(args.chunks) if args.action == "stage" else activate(args.chunks)
    except Exception as exc:
        print(f"[生产索引提升失败] {type(exc).__name__}: {exc}")
        return 1

    if args.action == "stage":
        print(f"候选生产 Collection：{result['target_collection_name']}")
        print(f"写入并核对：{result['chunk_count']} 个 Chunk")
        for probe in result["probes"]:
            print(f"[{'通过' if probe['passed'] else '失败'}] {probe['name']}")
        print(f"旧配置与 Manifest 备份：{result['backup_dir']}")
        print(f"stage 报告：{STAGE_REPORT_PATH}")
    else:
        print(f"生产 Collection 已激活：{result['collection_name']}")
        print(
            f"Manifest/Chroma 一致：{result['source_count']} 个来源，"
            f"{result['chunk_count']} 个 Chunk"
        )
        print(f"激活报告：{ACTIVATION_REPORT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
