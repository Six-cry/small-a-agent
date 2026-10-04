"""合并全部 Docling PDF Chunk 与 DOCX/TXT 原流程 Chunk。"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

from langchain_core.documents import Document

from ...config import KNOWLEDGE_DIR
from ...rag.document_loader import DocumentLoader
from ...rag.multimodal_parser import DEFAULT_OUTPUT_ROOT, get_output_dir
from ...rag.source_scanner import SourceScanner
from ...rag.text_splitter import create_default_text_splitter
from .batch_parse_pdfs import _load_completed_summary, discover_pdfs
from .build_structured_test_index import load_chunks_jsonl


FULL_CHUNK_REPORT_DIR = Path(__file__).resolve().parent / "reports" / "full_knowledge_chunks"
FULL_CHUNKS_PATH = FULL_CHUNK_REPORT_DIR / "full_knowledge_chunks.jsonl"
FULL_MANIFEST_PATH = FULL_CHUNK_REPORT_DIR / "full_knowledge_manifest.json"
FULL_REPORT_PATH = FULL_CHUNK_REPORT_DIR / "BUILD_REPORT.md"


def _write_jsonl(path: Path, documents: list[Document]) -> None:
    with path.open("w", encoding="utf-8") as stream:
        for document in documents:
            stream.write(
                json.dumps(
                    {
                        "page_content": document.page_content,
                        "metadata": document.metadata,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )


def collect_pdf_chunks(
    *,
    knowledge_dir: Path,
    pdf_output_root: Path = DEFAULT_OUTPUT_ROOT,
) -> tuple[list[Document], list[dict[str, Any]]]:
    """收集所有完整 PDF 缓存；任一缺失就整体拒绝。"""
    pdf_paths = discover_pdfs(knowledge_dir)
    missing: list[str] = []
    chunk_paths: list[Path] = []
    source_records: list[dict[str, Any]] = []

    from ...rag.source_scanner import calculate_file_hash

    for pdf_path in pdf_paths:
        source_hash = calculate_file_hash(pdf_path)
        manifest = _load_completed_summary(
            pdf_path,
            output_root=pdf_output_root,
            source_hash=source_hash,
        )
        relative_source = pdf_path.relative_to(knowledge_dir).as_posix()
        if manifest is None:
            missing.append(relative_source)
            continue
        output_dir = get_output_dir(
            pdf_path,
            output_root=pdf_output_root,
            source_hash=source_hash,
        )
        chunks_path = output_dir / "chunks.jsonl"
        chunk_paths.append(chunks_path)
        source_records.append(
            {
                "source": relative_source,
                "file_type": ".pdf",
                "file_hash": source_hash,
                "parser": "docling",
                "chunks_path": str(chunks_path.resolve()),
                "chunk_count": int(manifest["structured_chunk_count"]),
            }
        )

    if missing:
        details = "\n- ".join(missing)
        raise RuntimeError(
            "以下 PDF 尚未完成全文 Docling 解析，不能生成完整知识库 Chunk：\n- "
            + details
        )
    return load_chunks_jsonl(chunk_paths), source_records


def collect_non_pdf_chunks(
    *, knowledge_dir: Path
) -> tuple[list[Document], list[dict[str, Any]]]:
    """DOCX/TXT 继续使用原 Loader 和原 TextSplitter。"""
    scanner = SourceScanner(
        knowledge_dir=knowledge_dir,
        allowed_extensions={".docx", ".txt"},
    )
    loader = DocumentLoader(
        knowledge_dir=knowledge_dir,
        allowed_extensions={".docx", ".txt"},
    )
    splitter = create_default_text_splitter()
    chunks: list[Document] = []
    source_records: list[dict[str, Any]] = []

    for snapshot in scanner.scan().values():
        documents = loader.load_snapshot(snapshot)
        normalized_documents = [
            Document(
                page_content=document.page_content,
                metadata={
                    **document.metadata,
                    "content_type": "text",
                    "quality_status": "native_text",
                    "preserve_as_unit": False,
                },
            )
            for document in documents
        ]
        source_chunks = splitter.split_documents(normalized_documents)
        chunks.extend(source_chunks)
        source_records.append(
            {
                "source": snapshot.relative_path,
                "file_type": snapshot.file_type,
                "file_hash": snapshot.file_hash,
                "parser": "legacy-specialized-loader",
                "chunks_path": None,
                "chunk_count": len(source_chunks),
            }
        )
    return chunks, source_records


def build_full_chunk_set(
    *,
    knowledge_dir: Path = KNOWLEDGE_DIR,
    pdf_output_root: Path = DEFAULT_OUTPUT_ROOT,
    report_dir: Path = FULL_CHUNK_REPORT_DIR,
) -> tuple[Path, dict[str, Any]]:
    root = knowledge_dir.expanduser().resolve(strict=True)
    pdf_chunks, pdf_records = collect_pdf_chunks(
        knowledge_dir=root,
        pdf_output_root=pdf_output_root,
    )
    other_chunks, other_records = collect_non_pdf_chunks(knowledge_dir=root)
    all_chunks = [*pdf_chunks, *other_chunks]
    if not all_chunks:
        raise RuntimeError("没有生成任何知识库 Chunk")

    seen_ids: set[str] = set()
    for chunk in all_chunks:
        chunk_id = str(chunk.metadata.get("chunk_id", ""))
        if not chunk_id:
            raise RuntimeError(
                f"Chunk 缺少 chunk_id：{chunk.metadata.get('source', '未知来源')}"
            )
        if chunk_id in seen_ids:
            raise RuntimeError(f"完整知识库中出现重复 chunk_id：{chunk_id}")
        seen_ids.add(chunk_id)

    report_dir.mkdir(parents=True, exist_ok=True)
    chunks_path = report_dir / FULL_CHUNKS_PATH.name
    manifest_path = report_dir / FULL_MANIFEST_PATH.name
    report_path = report_dir / FULL_REPORT_PATH.name
    _write_jsonl(chunks_path, all_chunks)

    type_counts = Counter(
        str(chunk.metadata.get("content_type", "unknown")) for chunk in all_chunks
    )
    source_records = [*pdf_records, *other_records]
    payload: dict[str, Any] = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "knowledge_dir": str(root),
        "chunks_path": str(chunks_path.resolve()),
        "source_count": len(source_records),
        "pdf_source_count": len(pdf_records),
        "non_pdf_source_count": len(other_records),
        "chunk_count": len(all_chunks),
        "content_type_counts": dict(sorted(type_counts.items())),
        "sources": source_records,
    }
    manifest_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    lines = [
        "# 完整知识库 Chunk 合并报告",
        "",
        f"- 生成时间：{payload['generated_at']}",
        f"- 来源文件：{payload['source_count']}",
        f"- PDF（Docling）：{payload['pdf_source_count']}",
        f"- DOCX/TXT（原专用 Loader）：{payload['non_pdf_source_count']}",
        f"- Chunk 总数：{payload['chunk_count']}",
        f"- 内容类型：`{payload['content_type_counts']}`",
        "- 跨文件重复 chunk_id：0",
        "- Chroma：尚未写入",
        "",
        "| 文件 | 类型 | 解析器 | Chunk 数 |",
        "|---|---|---|---:|",
    ]
    for record in source_records:
        lines.append(
            f"| {record['source']} | {record['file_type']} | "
            f"{record['parser']} | {record['chunk_count']} |"
        )
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return chunks_path, payload


def main() -> int:
    parser = argparse.ArgumentParser(
        description="合并全部Docling PDF Chunk与DOCX/TXT原流程Chunk",
    )
    parser.add_argument("--knowledge-dir", type=Path, default=KNOWLEDGE_DIR)
    args = parser.parse_args()
    try:
        chunks_path, payload = build_full_chunk_set(
            knowledge_dir=args.knowledge_dir,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"[完整Chunk合并失败] {type(exc).__name__}: {exc}")
        return 1
    print(
        f"合并完成：{payload['source_count']} 个来源 -> "
        f"{payload['chunk_count']} 个 Chunk"
    )
    print(f"完整 Chunk：{chunks_path}")
    print(f"报告：{FULL_REPORT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
