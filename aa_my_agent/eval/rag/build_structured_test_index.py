"""把结构化 JSONL Chunk 写入与生产环境隔离的 Chroma 测试库。"""

from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings

from ...config import (
    CHROMA_DIR,
    EMBEDDING_DIMENSION,
    EMBEDDING_MODEL,
    RAG_COLLECTION_NAME,
    RAG_MAX_DISTANCE,
)
from ...rag.vector_store import VectorStoreService


EVAL_RAG_DIR = Path(__file__).resolve().parent
STRUCTURED_TEST_CHROMA_DIR = EVAL_RAG_DIR / "storage" / "structured_test_chroma"
STRUCTURED_TEST_COLLECTION_NAME = "rag_structured_docling_test_v1"
STRUCTURED_TEST_REPORT_DIR = EVAL_RAG_DIR / "reports" / "structured_test_index"
DEFAULT_CHUNKS_PATH = (
    EVAL_RAG_DIR
    / "reports"
    / "full_knowledge_chunks"
    / "full_knowledge_chunks.jsonl"
)


@dataclass(frozen=True, slots=True)
class ProbeResult:
    name: str
    question: str
    passed: bool
    reason: str
    best_distance: float | None


@dataclass(frozen=True, slots=True)
class BuildSummary:
    chroma_dir: str
    collection_name: str
    input_paths: tuple[str, ...]
    chunk_count: int
    source_count: int
    content_type_counts: dict[str, int]
    probes: tuple[ProbeResult, ...]


def calculate_file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_chunks_jsonl(paths: list[Path]) -> list[Document]:
    """读取一个或多个结构化 chunks.jsonl，并检查重复 ID。"""
    documents: list[Document] = []
    seen_ids: set[str] = set()

    for raw_path in paths:
        path = raw_path.expanduser().resolve(strict=True)
        with path.open("r", encoding="utf-8") as stream:
            for line_number, line in enumerate(stream, start=1):
                if not line.strip():
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(
                        f"{path} 第 {line_number} 行不是有效 JSON：{exc}"
                    ) from exc
                if not isinstance(payload, dict):
                    raise ValueError(f"{path} 第 {line_number} 行必须是 JSON 对象")
                page_content = payload.get("page_content")
                metadata = payload.get("metadata")
                if not isinstance(page_content, str) or not page_content.strip():
                    raise ValueError(f"{path} 第 {line_number} 行缺少 page_content")
                if not isinstance(metadata, dict):
                    raise ValueError(f"{path} 第 {line_number} 行缺少 metadata")
                chunk_id = metadata.get("chunk_id")
                if not isinstance(chunk_id, str) or not chunk_id:
                    raise ValueError(f"{path} 第 {line_number} 行缺少 chunk_id")
                if chunk_id in seen_ids:
                    raise ValueError(f"输入文件中出现重复 chunk_id：{chunk_id}")
                seen_ids.add(chunk_id)
                documents.append(
                    Document(page_content=page_content, metadata=dict(metadata))
                )

    if not documents:
        raise ValueError("没有从输入 JSONL 读取到任何 Chunk")
    return documents


def assert_isolated_test_target(
    *, persist_directory: Path, collection_name: str
) -> None:
    """硬性禁止测试构建器指向生产目录或生产 Collection。"""
    target_dir = persist_directory.expanduser().resolve()
    production_dir = CHROMA_DIR.expanduser().resolve()
    if target_dir == production_dir:
        raise ValueError("测试 Chroma 目录不能与生产 CHROMA_DIR 相同")
    if collection_name.strip() == RAG_COLLECTION_NAME:
        raise ValueError("测试 Collection 名称不能与生产 Collection 相同")


def create_structured_test_store(
    embedding_model: Embeddings | None = None,
    *,
    persist_directory: Path = STRUCTURED_TEST_CHROMA_DIR,
    collection_name: str = STRUCTURED_TEST_COLLECTION_NAME,
) -> VectorStoreService:
    assert_isolated_test_target(
        persist_directory=persist_directory,
        collection_name=collection_name,
    )
    return VectorStoreService(
        embedding_model=embedding_model,
        collection_name=collection_name,
        persist_directory=persist_directory,
    )


def _normalize(value: str) -> str:
    return "".join(value.casefold().split()).replace("→", "->")


def run_smoke_probes(store: VectorStoreService) -> tuple[ProbeResult, ...]:
    probes = [
        (
            "table-01",
            "表1中，当源节点为2、目的节点为11时，两种算法分别选择哪条路径？",
            ("2→3→4→7→11", "2→5→6→9→10→11"),
        ),
        (
            "image-01",
            "图2的网络拓扑中，节点7到节点11之间链路标注是多少？",
            ("0.232", "0.992"),
        ),
    ]
    results: list[ProbeResult] = []
    for name, question, keywords in probes:
        hits = store.search_with_scores(question, k=5)
        accepted = [
            (document, distance)
            for document, distance in hits
            if distance <= RAG_MAX_DISTANCE
        ]
        keyword_hits = [
            (document, distance)
            for document, distance in accepted
            if all(
                _normalize(keyword) in _normalize(document.page_content)
                for keyword in keywords
            )
        ]
        if keyword_hits:
            best_distance = min(distance for _document, distance in keyword_hits)
            results.append(
                ProbeResult(
                    name=name,
                    question=question,
                    passed=True,
                    reason="命中预期内容且通过距离阈值",
                    best_distance=float(best_distance),
                )
            )
        else:
            best_distance = min((distance for _document, distance in hits), default=None)
            results.append(
                ProbeResult(
                    name=name,
                    question=question,
                    passed=False,
                    reason="前5条结果中没有同时命中预期关键词并通过距离阈值",
                    best_distance=(
                        float(best_distance) if best_distance is not None else None
                    ),
                )
            )
    return tuple(results)


def _write_reports(summary: BuildSummary, input_hashes: dict[str, str]) -> None:
    STRUCTURED_TEST_REPORT_DIR.mkdir(parents=True, exist_ok=True)
    generated_at = datetime.now().astimezone().isoformat(timespec="seconds")
    payload: dict[str, Any] = {
        "generated_at": generated_at,
        **asdict(summary),
        "embedding_model": EMBEDDING_MODEL,
        "embedding_dimension": EMBEDDING_DIMENSION,
        "max_distance": RAG_MAX_DISTANCE,
        "input_hashes": input_hashes,
        "production_chroma_dir": str(CHROMA_DIR.resolve()),
        "production_collection_name": RAG_COLLECTION_NAME,
    }
    (STRUCTURED_TEST_REPORT_DIR / "index_manifest.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    lines = [
        "# 结构化 Chunk 测试 Chroma 构建报告",
        "",
        f"- 生成时间：{generated_at}",
        f"- 测试目录：`{summary.chroma_dir}`",
        f"- 测试 Collection：`{summary.collection_name}`",
        f"- Chunk 数量：{summary.chunk_count}",
        f"- 来源文件数量：{summary.source_count}",
        f"- 内容类型：`{summary.content_type_counts}`",
        f"- Embedding：`{EMBEDDING_MODEL}`，{EMBEDDING_DIMENSION} 维",
        f"- 生产目录：`{CHROMA_DIR.resolve()}`（未修改）",
        f"- 生产 Collection：`{RAG_COLLECTION_NAME}`（未修改）",
        "",
        "## 冒烟检索",
        "",
    ]
    for probe in summary.probes:
        distance = (
            f"{probe.best_distance:.6f}"
            if probe.best_distance is not None
            else "无"
        )
        lines.extend(
            [
                f"### {probe.name}：{'通过' if probe.passed else '失败'}",
                "",
                f"- 问题：{probe.question}",
                f"- 最佳相关距离：{distance}",
                f"- 结论：{probe.reason}",
                "",
            ]
        )
    (STRUCTURED_TEST_REPORT_DIR / "BUILD_REPORT.md").write_text(
        "\n".join(lines).rstrip() + "\n",
        encoding="utf-8",
    )


def build_test_index(
    chunk_paths: list[Path],
    *,
    embedding_model: Embeddings | None = None,
    persist_directory: Path = STRUCTURED_TEST_CHROMA_DIR,
    collection_name: str = STRUCTURED_TEST_COLLECTION_NAME,
    run_probes: bool = True,
) -> BuildSummary:
    chunks = load_chunks_jsonl(chunk_paths)
    store = create_structured_test_store(
        embedding_model,
        persist_directory=persist_directory,
        collection_name=collection_name,
    )

    existing_ids = store.get_chunk_ids()
    if existing_ids:
        store.delete_chunks(existing_ids)
    written_ids = store.add_chunks(chunks)
    actual_ids = store.get_chunk_ids()
    expected_ids = {str(chunk.metadata["chunk_id"]) for chunk in chunks}
    if set(written_ids) != expected_ids or set(actual_ids) != expected_ids:
        raise RuntimeError("测试 Chroma 中的 ID 与输入 Chunk 不一致")

    content_type_counts: dict[str, int] = {}
    for chunk in chunks:
        content_type = str(chunk.metadata.get("content_type", "unknown"))
        content_type_counts[content_type] = content_type_counts.get(content_type, 0) + 1
    probes = run_smoke_probes(store) if run_probes else ()
    summary = BuildSummary(
        chroma_dir=str(persist_directory.resolve()),
        collection_name=collection_name,
        input_paths=tuple(str(path.resolve()) for path in chunk_paths),
        chunk_count=len(actual_ids),
        source_count=len({str(chunk.metadata["source"]) for chunk in chunks}),
        content_type_counts=content_type_counts,
        probes=probes,
    )
    input_hashes = {
        str(path.resolve()): calculate_file_hash(path.resolve())
        for path in chunk_paths
    }
    _write_reports(summary, input_hashes)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(
        description="将结构化 Chunk 写入隔离的 Chroma 测试库",
    )
    parser.add_argument(
        "chunks",
        type=Path,
        nargs="*",
        help="一个或多个 chunks.jsonl；不填则使用完整知识库合并结果",
    )
    parser.add_argument(
        "--skip-probes",
        action="store_true",
        help="只构建索引，不运行表格与图片冒烟检索",
    )
    args = parser.parse_args()
    paths = args.chunks or [DEFAULT_CHUNKS_PATH]
    try:
        summary = build_test_index(paths, run_probes=not args.skip_probes)
    except Exception as exc:
        print(f"[测试索引构建失败] {type(exc).__name__}: {exc}")
        return 1

    print(f"测试 Chroma：{summary.chroma_dir}")
    print(f"测试 Collection：{summary.collection_name}")
    print(f"成功写入并核对：{summary.chunk_count} 个 Chunk")
    for probe in summary.probes:
        distance = (
            f"{probe.best_distance:.6f}"
            if probe.best_distance is not None
            else "无"
        )
        print(
            f"[{'通过' if probe.passed else '失败'}] {probe.name} - "
            f"{probe.reason}；最佳距离 {distance}"
        )
    print(f"报告：{STRUCTURED_TEST_REPORT_DIR / 'BUILD_REPORT.md'}")
    return 0 if all(probe.passed for probe in summary.probes) else 1


if __name__ == "__main__":
    raise SystemExit(main())
