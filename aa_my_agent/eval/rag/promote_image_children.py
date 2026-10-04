"""把隔离测试库中已验证的图片子向量本地迁移到正式 Chroma。

本脚本不会调用 Embedding 或视觉模型。它直接复制隔离 Chroma 已保存的
向量、正文和元数据，并在写入前为正式 Collection、Manifest、配置和
BM25 建立可回滚备份。发生异常时会删除本轮新增子块并恢复索引清单。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any

import chromadb
from langchain_core.documents import Document

from ...config import (
    APP_DIR,
    CHROMA_DIR,
    RAG_BM25_INDEX_PATH,
    RAG_COLLECTION_NAME,
    RAG_MANIFEST_PATH,
)
from ...rag.image_parent_child import (
    IMAGE_CHILD_CONTENT_TYPE,
    IMAGE_CHILD_SCHEMA_VERSION,
    IMAGE_PARENT_CONTENT_TYPE,
    expand_image_parent_child_documents,
)
from ...rag.lexical_index import BM25Index
from ...rag.manifest import IndexManifest, ManifestStore
from .build_structured_test_index import (
    STRUCTURED_TEST_CHROMA_DIR,
    STRUCTURED_TEST_COLLECTION_NAME,
)


EXPECTED_CHILD_COUNT = 93
TARGET_PIPELINE_VERSION = "docling-parent-child-v2"
TARGET_PARENT_CHILD_VERSION = "image-parent-child-v1"
BACKUP_ROOT = Path(__file__).resolve().parent / "storage" / "production_backups"
REPORT_DIR = Path(__file__).resolve().parent / "reports" / "image_parent_child"
PROMOTION_REPORT_PATH = REPORT_DIR / "PRODUCTION_PROMOTION_REPORT.json"
PROMOTION_MARKDOWN_PATH = REPORT_DIR / "PRODUCTION_PROMOTION_REPORT.md"


@dataclass(frozen=True, slots=True)
class _ChromaRow:
    chunk_id: str
    document: str
    metadata: dict[str, Any]
    embedding: list[float]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _collection(path: Path, name: str):
    client = chromadb.PersistentClient(path=str(path.resolve()))
    return client, client.get_collection(name=name)


def _read_rows(collection) -> list[_ChromaRow]:
    payload = collection.get(include=["documents", "metadatas", "embeddings"])
    ids = payload.get("ids")
    documents = payload.get("documents")
    metadatas = payload.get("metadatas")
    embeddings = payload.get("embeddings")
    if embeddings is not None and hasattr(embeddings, "tolist"):
        embeddings = embeddings.tolist()
    if not (
        isinstance(ids, list)
        and isinstance(documents, list)
        and isinstance(metadatas, list)
        and isinstance(embeddings, list)
        and len(ids) == len(documents) == len(metadatas) == len(embeddings)
    ):
        raise RuntimeError("Chroma 返回的 ID、正文、元数据和向量数量不一致")

    rows: list[_ChromaRow] = []
    for chunk_id, document, metadata, embedding in zip(
        ids, documents, metadatas, embeddings, strict=True
    ):
        if not isinstance(document, str) or not isinstance(metadata, dict):
            raise RuntimeError("Chroma 返回了无效正文或元数据")
        if hasattr(embedding, "tolist"):
            embedding = embedding.tolist()
        if not isinstance(embedding, list) or not embedding:
            raise RuntimeError(f"Chunk 缺少有效向量：{chunk_id}")
        rows.append(
            _ChromaRow(
                chunk_id=str(chunk_id),
                document=document,
                metadata=dict(metadata),
                embedding=[float(value) for value in embedding],
            )
        )
    return rows


def _load_manifest() -> IndexManifest:
    payload = json.loads(RAG_MANIFEST_PATH.read_text(encoding="utf-8"))
    return IndexManifest.from_dict(payload)


def _same_row(left: _ChromaRow, right: _ChromaRow) -> bool:
    return (
        left.chunk_id == right.chunk_id
        and left.document == right.document
        and left.metadata == right.metadata
        and left.embedding == right.embedding
    )


def _preflight() -> tuple[list[_ChromaRow], list[_ChromaRow], IndexManifest]:
    _production_client, production = _collection(CHROMA_DIR, RAG_COLLECTION_NAME)
    _test_client, test = _collection(
        STRUCTURED_TEST_CHROMA_DIR,
        STRUCTURED_TEST_COLLECTION_NAME,
    )
    production_rows = _read_rows(production)
    test_rows = _read_rows(test)
    child_rows = [
        row
        for row in test_rows
        if row.metadata.get("content_type") == IMAGE_CHILD_CONTENT_TYPE
    ]
    base_rows = [
        row
        for row in test_rows
        if row.metadata.get("content_type") != IMAGE_CHILD_CONTENT_TYPE
    ]
    if len(child_rows) != EXPECTED_CHILD_COUNT:
        raise RuntimeError(
            f"隔离库图片子块应为 {EXPECTED_CHILD_COUNT} 个，实际为 {len(child_rows)} 个"
        )

    production_by_id = {row.chunk_id: row for row in production_rows}
    base_by_id = {row.chunk_id: row for row in base_rows}
    production_ids = set(production_by_id)
    base_ids = set(base_by_id)
    child_ids = {row.chunk_id for row in child_rows}
    if len(production_ids) != len(production_rows):
        raise RuntimeError("正式 Collection 存在重复 ID")
    if base_ids != production_ids:
        raise RuntimeError(
            "隔离库的非子块与正式库不一致："
            f"正式库缺少={len(base_ids - production_ids)}，"
            f"正式库多出={len(production_ids - base_ids)}"
        )
    mismatched_base_ids = [
        chunk_id
        for chunk_id in sorted(production_ids)
        if not _same_row(production_by_id[chunk_id], base_by_id[chunk_id])
    ]
    if mismatched_base_ids:
        raise RuntimeError(
            "隔离库基础数据与正式库正文、元数据或向量不一致："
            f"{len(mismatched_base_ids)} 个"
        )
    if child_ids & production_ids:
        raise RuntimeError("正式库已经包含部分图片子块，拒绝重复迁移")

    dimensions = {len(row.embedding) for row in production_rows + child_rows}
    if len(dimensions) != 1:
        raise RuntimeError(f"正式向量和子向量维度不一致：{sorted(dimensions)}")
    for row in child_rows:
        metadata = row.metadata
        if metadata.get("retrieval_role") != "child":
            raise RuntimeError(f"图片子块角色错误：{row.chunk_id}")
        if metadata.get("parent_child_schema") != IMAGE_CHILD_SCHEMA_VERSION:
            raise RuntimeError(f"图片子块结构版本错误：{row.chunk_id}")
        if metadata.get("parent_chunk_id") not in production_ids:
            raise RuntimeError(f"图片子块找不到正式父块：{row.chunk_id}")
        parent = production_by_id[str(metadata["parent_chunk_id"])]
        if parent.metadata.get("content_type") != IMAGE_PARENT_CONTENT_TYPE:
            raise RuntimeError(f"图片子块指向的不是图片父块：{row.chunk_id}")
        for field_name in ("source", "file_hash", "page"):
            if metadata.get(field_name) != parent.metadata.get(field_name):
                raise RuntimeError(
                    f"图片子块与父块的 {field_name} 不一致：{row.chunk_id}"
                )

    regenerated = expand_image_parent_child_documents(
        [
            Document(page_content=row.document, metadata=dict(row.metadata))
            for row in base_rows
        ]
    )
    regenerated_children = {
        str(document.metadata["chunk_id"]): document
        for document in regenerated
        if document.metadata.get("content_type") == IMAGE_CHILD_CONTENT_TYPE
    }
    if set(regenerated_children) != child_ids:
        raise RuntimeError("根据正式父块重新生成的图片子块 ID 与隔离库不一致")
    for row in child_rows:
        regenerated_child = regenerated_children[row.chunk_id]
        if regenerated_child.page_content != row.document:
            raise RuntimeError(f"图片子块正文无法由正式父块复现：{row.chunk_id}")
        for field_name in (
            "parent_chunk_id",
            "chunk_index",
            "total_chunks",
            "content_hash",
            "source",
            "file_hash",
            "page",
        ):
            if regenerated_child.metadata.get(field_name) != row.metadata.get(field_name):
                raise RuntimeError(
                    f"图片子块字段 {field_name} 无法稳定复现：{row.chunk_id}"
                )

    manifest = _load_manifest()
    manifest_ids = {
        chunk_id
        for record in manifest.sources.values()
        for chunk_id in record.chunk_ids
    }
    if manifest_ids != production_ids:
        raise RuntimeError("迁移前正式 Manifest 与 Chroma ID 不一致")
    for row in child_rows:
        source = row.metadata.get("source")
        if not isinstance(source, str) or source not in manifest.sources:
            raise RuntimeError(f"图片子块来源不在 Manifest：{row.chunk_id}")
    return production_rows, child_rows, manifest


def _copy_rows(collection, rows: list[_ChromaRow], batch_size: int = 100) -> None:
    for start in range(0, len(rows), batch_size):
        batch = rows[start : start + batch_size]
        collection.add(
            ids=[row.chunk_id for row in batch],
            documents=[row.document for row in batch],
            metadatas=[row.metadata for row in batch],
            embeddings=[row.embedding for row in batch],
        )


def _create_backup(production, production_rows: list[_ChromaRow]) -> Path:
    timestamp = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S")
    backup_dir = BACKUP_ROOT / f"parent_child_{timestamp}"
    backup_dir.mkdir(parents=True, exist_ok=False)
    shutil.copy2(APP_DIR / "config.py", backup_dir / "config.py")
    shutil.copy2(RAG_MANIFEST_PATH, backup_dir / "rag_manifest.json")
    if RAG_BM25_INDEX_PATH.is_file():
        shutil.copy2(RAG_BM25_INDEX_PATH, backup_dir / "rag_bm25_index.json")

    backup_client = chromadb.PersistentClient(path=str(backup_dir / "chroma"))
    backup_collection = backup_client.create_collection(
        name=RAG_COLLECTION_NAME,
        configuration=production.configuration,
        metadata=production.metadata,
    )
    _copy_rows(backup_collection, production_rows)
    if backup_collection.count() != len(production_rows):
        raise RuntimeError("正式 Chroma 逻辑备份数量不完整")
    backup_rows = _read_rows(backup_collection)
    original_by_id = {row.chunk_id: row for row in production_rows}
    if len(backup_rows) != len(original_by_id) or any(
        row.chunk_id not in original_by_id
        or not _same_row(row, original_by_id[row.chunk_id])
        for row in backup_rows
    ):
        raise RuntimeError("正式 Chroma 逻辑备份逐条复核失败")
    return backup_dir


def _target_manifest(
    manifest: IndexManifest,
    child_rows: list[_ChromaRow],
) -> IndexManifest:
    child_ids_by_source: dict[str, list[tuple[int, str]]] = {}
    for row in child_rows:
        source = str(row.metadata["source"])
        chunk_index = int(row.metadata["chunk_index"])
        child_ids_by_source.setdefault(source, []).append((chunk_index, row.chunk_id))

    sources = dict(manifest.sources)
    for source, indexed_children in child_ids_by_source.items():
        record = sources[source]
        ordered_child_ids = tuple(
            chunk_id for _index, chunk_id in sorted(indexed_children)
        )
        if set(record.chunk_ids) & set(ordered_child_ids):
            raise RuntimeError(f"Manifest 已包含图片子块：{source}")
        sources[source] = replace(
            record,
            chunk_ids=record.chunk_ids + ordered_child_ids,
        )

    signature = replace(
        manifest.index_signature,
        pipeline_version=TARGET_PIPELINE_VERSION,
        image_parent_child_version=TARGET_PARENT_CHILD_VERSION,
    )
    return IndexManifest(
        index_signature=signature,
        sources=sources,
        schema_version=manifest.schema_version,
    )


def preflight_summary() -> dict[str, Any]:
    production_rows, child_rows, manifest = _preflight()
    return {
        "production_before": len(production_rows),
        "children_ready": len(child_rows),
        "production_after_expected": len(production_rows) + len(child_rows),
        "source_count": len(manifest.sources),
        "embedding_dimension": len(child_rows[0].embedding),
        "external_embedding_called": False,
    }


def promote() -> dict[str, Any]:
    production_rows, child_rows, manifest = _preflight()
    _client, production = _collection(CHROMA_DIR, RAG_COLLECTION_NAME)
    backup_dir = _create_backup(production, production_rows)
    child_ids = [row.chunk_id for row in child_rows]
    original_ids = {row.chunk_id for row in production_rows}
    bm25_existed = RAG_BM25_INDEX_PATH.is_file()
    try:
        _copy_rows(production, child_rows)
        expected_ids = {row.chunk_id for row in production_rows + child_rows}
        actual_ids = set(production.get(include=[]).get("ids", []))
        if actual_ids != expected_ids:
            raise RuntimeError("写入图片子块后正式 Chroma ID 不完整")

        target_manifest = _target_manifest(manifest, child_rows)
        manifest_store = ManifestStore(
            manifest_path=RAG_MANIFEST_PATH,
            expected_signature=target_manifest.index_signature,
        )
        manifest_store.save(target_manifest)

        migrated_rows = _read_rows(production)
        documents = [
            Document(page_content=row.document, metadata=dict(row.metadata))
            for row in migrated_rows
        ]
        BM25Index(
            index_path=RAG_BM25_INDEX_PATH,
            collection_name=RAG_COLLECTION_NAME,
        ).rebuild(documents)
        if {row.chunk_id for row in migrated_rows} != expected_ids:
            raise RuntimeError("迁移后正式向量库复核失败")

        report = {
            "promoted_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "collection_name": RAG_COLLECTION_NAME,
            "production_before": len(production_rows),
            "children_added": len(child_rows),
            "production_after": len(expected_ids),
            "source_count": len(target_manifest.sources),
            "embedding_dimension": len(child_rows[0].embedding),
            "pipeline_version": TARGET_PIPELINE_VERSION,
            "image_parent_child_version": TARGET_PARENT_CHILD_VERSION,
            "backup_dir": str(backup_dir.resolve()),
            "backup_manifest_sha256": _sha256(backup_dir / "rag_manifest.json"),
            "external_embedding_called": False,
        }
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        PROMOTION_REPORT_PATH.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        PROMOTION_MARKDOWN_PATH.write_text(
            "\n".join(
                [
                    "# 图片父子向量正式迁移报告",
                    "",
                    f"- 迁移时间：{report['promoted_at']}",
                    f"- 正式 Collection：`{RAG_COLLECTION_NAME}`",
                    f"- 迁移前：{report['production_before']} 个 Chunk",
                    f"- 本地复制图片子块：{report['children_added']} 个",
                    f"- 迁移后：{report['production_after']} 个 Chunk",
                    f"- 来源文件：{report['source_count']} 个",
                    f"- 向量维度：{report['embedding_dimension']}",
                    f"- 流水线版本：`{TARGET_PIPELINE_VERSION}`",
                    f"- 父子结构版本：`{TARGET_PARENT_CHILD_VERSION}`",
                    f"- 回滚备份：`{report['backup_dir']}`",
                    "- 外部 Embedding 调用：无（复用隔离库现有向量）",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        return report
    except Exception:
        # 即使批量写入在中途抛错，也删除本轮预检时确认原本不存在的 ID。
        present_child_ids = production.get(ids=child_ids, include=[]).get("ids", [])
        if present_child_ids:
            production.delete(ids=list(present_child_ids))
        remaining_ids = set(production.get(include=[]).get("ids", []))
        if remaining_ids != original_ids:
            raise RuntimeError(
                "图片子块迁移失败，且自动回滚后的正式 Chroma ID 不完整；"
                f"请使用备份恢复：{backup_dir}"
            )
        shutil.copy2(backup_dir / "rag_manifest.json", RAG_MANIFEST_PATH)
        backup_bm25 = backup_dir / "rag_bm25_index.json"
        if backup_bm25.is_file():
            shutil.copy2(backup_bm25, RAG_BM25_INDEX_PATH)
        elif not bm25_existed and RAG_BM25_INDEX_PATH.exists():
            RAG_BM25_INDEX_PATH.unlink()
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description="本地迁移图片父子向量到正式 Chroma")
    parser.add_argument("action", choices=("preflight", "promote"))
    args = parser.parse_args()
    try:
        result = preflight_summary() if args.action == "preflight" else promote()
    except Exception as exc:
        print(f"[图片父子正式迁移失败] {type(exc).__name__}: {exc}")
        return 1

    print(json.dumps(result, ensure_ascii=False, indent=2))
    if args.action == "promote":
        print(f"迁移报告：{PROMOTION_MARKDOWN_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
