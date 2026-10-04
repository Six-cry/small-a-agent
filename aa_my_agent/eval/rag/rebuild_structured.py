"""从 Docling 结构缓存快速重建语义 Document 和最终 Chunk。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from docling_core.types.doc import DoclingDocument

from ...rag.multimodal_parser import APP_DIR, DEFAULT_OUTPUT_ROOT, PictureRecord
from ...rag.structured_document_builder import build_structured_documents
from ...rag.text_splitter import create_default_text_splitter


def _write_jsonl(path: Path, documents: list[Any]) -> None:
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


def find_available_outputs() -> list[Path]:
    """列出同时具有 Manifest 和 Docling 缓存的可重建目录。"""
    if not DEFAULT_OUTPUT_ROOT.exists():
        return []
    return sorted(
        (
            path
            for path in DEFAULT_OUTPUT_ROOT.iterdir()
            if path.is_dir()
            and (path / "manifest.json").is_file()
            and (path / "docling_document.json").is_file()
        ),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )


def find_full_outputs() -> list[Path]:
    """只列出已完成全文解析（requested_pages=all）的缓存目录。"""
    result: list[Path] = []
    for output_dir in find_available_outputs():
        try:
            manifest = json.loads(
                (output_dir / "manifest.json").read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError):
            continue
        if manifest.get("requested_pages") == "all":
            result.append(output_dir)
    return result


def resolve_output_dir(output_dir: Path) -> Path:
    """解析完整路径，也接受 unified_parser 下的目录名称。"""
    expanded = output_dir.expanduser()
    candidates = [expanded]
    if not expanded.is_absolute():
        candidates.append(DEFAULT_OUTPUT_ROOT / expanded)

    for candidate in candidates:
        if candidate.is_dir():
            return candidate.resolve()

    raise FileNotFoundError(f"找不到解析结果目录：{output_dir}")


def rebuild(output_dir: Path) -> tuple[int, int]:
    resolved_output = resolve_output_dir(output_dir)
    manifest_path = resolved_output / "manifest.json"
    cache_path = resolved_output / "docling_document.json"
    if not manifest_path.is_file():
        raise FileNotFoundError(f"目录中缺少 manifest.json：{resolved_output}")
    if not cache_path.is_file():
        raise FileNotFoundError(
            "目录中缺少 docling_document.json，旧实验结果不能直接重建；"
            f"请重新运行统一解析器：{resolved_output}"
        )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    document = DoclingDocument.load_from_json(cache_path)
    pictures = [PictureRecord(**payload) for payload in manifest.get("pictures", [])]

    source_pdf = Path(manifest["source"]).resolve(strict=True)
    knowledge_dir = (APP_DIR / "rag" / "data" / "knowledge").resolve()
    try:
        source = source_pdf.relative_to(knowledge_dir).as_posix()
    except ValueError:
        source = source_pdf.as_posix()

    documents = build_structured_documents(
        document=document,
        picture_records=pictures,
        source=source,
        file_hash=str(manifest["source_hash"]),
        file_size=source_pdf.stat().st_size,
        file_name=source_pdf.name,
        file_type=source_pdf.suffix.casefold(),
    )
    chunks = create_default_text_splitter().split_documents(documents)
    documents_path = resolved_output / "documents.jsonl"
    chunks_path = resolved_output / "chunks.jsonl"
    _write_jsonl(documents_path, documents)
    _write_jsonl(chunks_path, chunks)

    counts_by_type: dict[str, int] = {}
    for item in documents:
        content_type = str(item.metadata.get("content_type", "unknown"))
        counts_by_type[content_type] = counts_by_type.get(content_type, 0) + 1
    manifest.update(
        {
            "structured_documents_path": str(documents_path),
            "structured_chunks_path": str(chunks_path),
            "structured_document_count": len(documents),
            "structured_chunk_count": len(chunks),
            "structured_counts_by_type": counts_by_type,
        }
    )
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return len(documents), len(chunks)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="不重新运行Docling模型，直接从结构缓存重建RAG分块",
    )
    parser.add_argument(
        "output_dir",
        type=Path,
        nargs="?",
        help="真实的统一解析器输出目录；不要直接填写示例中的占位文字",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="列出当前可以重建的解析结果目录",
    )
    parser.add_argument(
        "--all-complete",
        action="store_true",
        help="从缓存批量重建所有已完成全文解析的PDF，不重新运行Docling或视觉模型",
    )
    args = parser.parse_args()
    if args.all_complete:
        if args.output_dir is not None:
            parser.error("--all-complete 不能和 output_dir 同时使用")
        outputs = find_full_outputs()
        if not outputs:
            print("当前没有可批量重建的全文解析缓存。")
            return 1
        total_documents = 0
        total_chunks = 0
        failures: list[tuple[Path, Exception]] = []
        for output in outputs:
            try:
                document_count, chunk_count = rebuild(output)
            except (OSError, KeyError, json.JSONDecodeError, ValueError) as exc:
                failures.append((output, exc))
                print(f"[失败] {output.name}：{exc}")
                continue
            total_documents += document_count
            total_chunks += chunk_count
            print(
                f"[完成] {output.name}：{document_count} 个 Document -> "
                f"{chunk_count} 个 Chunk"
            )
        print(
            f"批量重建结束：成功 {len(outputs) - len(failures)}/{len(outputs)}，"
            f"合计 {total_documents} 个 Document -> {total_chunks} 个 Chunk"
        )
        return 1 if failures else 0

    if args.list or args.output_dir is None:
        outputs = find_available_outputs()
        if not outputs:
            print("当前没有同时包含 manifest.json 和 Docling 缓存的解析结果目录。")
            return 1
        print("可用的解析结果目录：")
        for output in outputs:
            print(f'- "{output}"')
        if args.output_dir is None and not args.list:
            print("\n请从上面复制一个真实目录，作为命令最后一个参数。")
        return 0

    try:
        document_count, chunk_count = rebuild(args.output_dir)
    except (OSError, KeyError, json.JSONDecodeError, ValueError) as exc:
        print(f"[重建失败] {exc}")
        outputs = find_available_outputs()
        if outputs:
            print("\n可用的解析结果目录：")
            for output in outputs:
                print(f'- "{output}"')
            print("\n也可以先运行：python -m aa_my_agent.eval.rag.rebuild_structured --list")
        return 1
    print(
        f"重建完成：{document_count} 个语义Document -> "
        f"{chunk_count} 个最终Chunk"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
