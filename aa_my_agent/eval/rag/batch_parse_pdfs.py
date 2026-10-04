"""批量解析知识库全部 PDF，并支持按文件 Hash 断点续跑。"""

from __future__ import annotations

import argparse
import json
import re
import shutil
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from time import perf_counter
from typing import Any

from ...config import KNOWLEDGE_DIR
from ...rag.multimodal_parser import (
    APP_DIR,
    DEFAULT_IMAGE_SCALE,
    DEFAULT_OUTPUT_ROOT,
    PictureRecord,
    calculate_file_hash,
    create_docling_converter,
    create_glm_image_client,
    get_output_dir,
    parse_pdf,
    write_parsed_document_artifacts,
)


BATCH_JSON_PATH = DEFAULT_OUTPUT_ROOT / "batch_all_pdfs.json"
BATCH_MARKDOWN_PATH = DEFAULT_OUTPUT_ROOT / "BATCH_ALL_PDFS.md"
BATCH_PDF_BACKEND = "pypdfium2"
_FORMULA_SIGNAL_RE = re.compile(
    r"[α-ωΑ-Ω∑∫√≤≥≈]|(?:公式|式中|equation\b|where\s*[:,])",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class BatchFileResult:
    source: str
    source_hash: str
    status: str
    output_dir: str
    page_count: int | None
    document_count: int | None
    chunk_count: int | None
    content_type_counts: dict[str, int]
    elapsed_seconds: float
    error: str | None


def discover_pdfs(knowledge_dir: Path = KNOWLEDGE_DIR) -> list[Path]:
    """递归发现知识库中的 PDF，按相对路径稳定排序。"""
    root = knowledge_dir.expanduser().resolve(strict=True)
    return sorted(
        (
            path
            for path in root.rglob("*")
            if path.is_file() and path.suffix.casefold() == ".pdf"
        ),
        key=lambda path: path.relative_to(root).as_posix().casefold(),
    )


def pdf_has_formula_signals(pdf_path: Path) -> bool:
    """用 PDF 原生文本做低成本预检，决定是否开启公式富化。"""
    try:
        from pypdf import PdfReader
    except ModuleNotFoundError as exc:
        raise RuntimeError("缺少 pypdf，无法执行公式候选预检") from exc

    reader = PdfReader(str(pdf_path))
    text = "\n".join((page.extract_text() or "") for page in reader.pages)
    return bool(_FORMULA_SIGNAL_RE.search(text))


def _load_page_summary(
    pdf_path: Path,
    *,
    output_root: Path,
    source_hash: str,
    page_no: int,
) -> tuple[Path, dict[str, Any]] | None:
    output_dir = get_output_dir(
        pdf_path,
        output_root=output_root,
        page_range=(page_no, page_no),
        source_hash=source_hash,
    )
    manifest_path = output_dir / "manifest.json"
    required = (
        output_dir / "docling_document.json",
        output_dir / "documents.jsonl",
        output_dir / "chunks.jsonl",
    )
    if not manifest_path.is_file() or not all(path.is_file() for path in required):
        return None
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if (
        manifest.get("source_hash") != source_hash
        or manifest.get("requested_pages") != f"{page_no}-{page_no}"
    ):
        return None
    return output_dir, manifest


def _page_count(pdf_path: Path) -> int:
    try:
        from pypdf import PdfReader
    except ModuleNotFoundError as exc:
        raise RuntimeError("缺少 pypdf，无法读取 PDF 页数") from exc
    return len(PdfReader(str(pdf_path)).pages)


def _merge_page_outputs(
    pdf_path: Path,
    *,
    output_root: Path,
    source_hash: str,
    page_count: int,
) -> Any:
    """把逐页 Docling 检查点重新合成与整本解析相同的全文产物。"""
    from docling_core.types.doc import DoclingDocument

    page_documents = []
    page_outputs: list[tuple[Path, dict[str, Any]]] = []
    for page_no in range(1, page_count + 1):
        loaded = _load_page_summary(
            pdf_path,
            output_root=output_root,
            source_hash=source_hash,
            page_no=page_no,
        )
        if loaded is None:
            raise RuntimeError(f"第 {page_no} 页检查点不完整，不能合并全文")
        page_dir, manifest = loaded
        page_outputs.append((page_dir, manifest))
        page_documents.append(
            DoclingDocument.load_from_json(page_dir / "docling_document.json")
        )

    merged_document = DoclingDocument.concatenate(page_documents)
    output_dir = get_output_dir(
        pdf_path,
        output_root=output_root,
        source_hash=source_hash,
    )
    image_dir = output_dir / "images"
    image_dir.mkdir(parents=True, exist_ok=True)

    picture_records: list[PictureRecord] = []
    for page_dir, manifest in page_outputs:
        for raw_picture in manifest.get("pictures", []):
            payload = dict(raw_picture)
            picture_index = len(picture_records) + 1
            original_rel = payload.get("image_path")
            final_rel: str | None = None
            if original_rel:
                original_image = page_dir / str(original_rel)
                if original_image.is_file():
                    final_image = image_dir / (
                        f"page_{int(payload.get('page', 0)):04d}_"
                        f"picture_{picture_index:03d}{original_image.suffix.casefold()}"
                    )
                    shutil.copy2(original_image, final_image)
                    final_rel = final_image.relative_to(output_dir).as_posix()
                else:
                    payload["error"] = (
                        (str(payload.get("error") or "") + "; ").lstrip("; ")
                        + "逐页裁图文件缺失"
                    )
            payload["picture_index"] = picture_index
            payload["image_path"] = final_rel
            picture_records.append(PictureRecord(**payload))

    summary = write_parsed_document_artifacts(
        resolved_pdf=pdf_path.resolve(strict=True),
        document=merged_document,
        output_dir=output_dir,
        source_hash=source_hash,
        requested_pages="all",
        picture_records=picture_records,
    )
    manifest_path = output_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.update(
        {
            "parse_mode": "page_checkpoint_merge",
            "pdf_backend": BATCH_PDF_BACKEND,
            "formula_enrichment": True,
            "page_checkpoint_count": page_count,
        }
    )
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return summary


def _parse_formula_pdf_by_page(
    pdf_path: Path,
    *,
    output_root: Path,
    source_hash: str,
    converter: Any,
    image_client: Any | None,
    describe_images: bool,
    force: bool = False,
) -> Any:
    page_count = _page_count(pdf_path)
    for page_no in range(1, page_count + 1):
        if not force and _load_page_summary(
            pdf_path,
            output_root=output_root,
            source_hash=source_hash,
            page_no=page_no,
        ) is not None:
            print(f"  - 第 {page_no}/{page_count} 页：复用检查点")
            continue
        print(f"  - 第 {page_no}/{page_count} 页：开始")
        parse_pdf(
            pdf_path,
            output_root=output_root,
            page_range=(page_no, page_no),
            describe_images=describe_images,
            converter=converter,
            image_client=image_client,
        )
        print(f"  - 第 {page_no}/{page_count} 页：完成")
    return _merge_page_outputs(
        pdf_path,
        output_root=output_root,
        source_hash=source_hash,
        page_count=page_count,
    )


def _load_completed_summary(
    pdf_path: Path,
    *,
    output_root: Path,
    source_hash: str,
) -> dict[str, Any] | None:
    output_dir = get_output_dir(
        pdf_path,
        output_root=output_root,
        page_range=None,
        source_hash=source_hash,
    )
    manifest_path = output_dir / "manifest.json"
    required_paths = (
        output_dir / "docling_document.json",
        output_dir / "documents.jsonl",
        output_dir / "chunks.jsonl",
    )
    if not manifest_path.is_file() or not all(path.is_file() for path in required_paths):
        return None
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if (
        manifest.get("source_hash") != source_hash
        or manifest.get("requested_pages") != "all"
        or not isinstance(manifest.get("structured_chunk_count"), int)
    ):
        return None
    return manifest


def _write_batch_report(
    results: list[BatchFileResult],
    *,
    knowledge_dir: Path,
    describe_images: bool,
) -> None:
    DEFAULT_OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    generated_at = datetime.now().astimezone().isoformat(timespec="seconds")
    status_counts: dict[str, int] = {}
    for result in results:
        status_counts[result.status] = status_counts.get(result.status, 0) + 1
    payload = {
        "generated_at": generated_at,
        "knowledge_dir": str(knowledge_dir.resolve()),
        "output_root": str(DEFAULT_OUTPUT_ROOT.resolve()),
        "describe_images": describe_images,
        "summary": {
            "total": len(results),
            "status_counts": status_counts,
            "total_documents": sum(item.document_count or 0 for item in results),
            "total_chunks": sum(item.chunk_count or 0 for item in results),
            "elapsed_seconds": sum(item.elapsed_seconds for item in results),
        },
        "files": [asdict(result) for result in results],
    }
    BATCH_JSON_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    lines = [
        "# 知识库全量 PDF 结构化解析报告",
        "",
        f"- 更新时间：{generated_at}",
        f"- 知识库：`{knowledge_dir.resolve()}`",
        f"- PDF 总数：{len(results)}",
        f"- 状态统计：`{status_counts}`",
        f"- 语义 Documents：{payload['summary']['total_documents']}",
        f"- 最终 Chunks：{payload['summary']['total_chunks']}",
        f"- 图片描述：{'启用 GLM' if describe_images else '关闭'}",
        "- 正式 Chroma：未修改",
        "",
        "| 文件 | 状态 | 页数 | Documents | Chunks | 耗时（秒） | 错误 |",
        "|---|---|---:|---:|---:|---:|---|",
    ]
    for result in results:
        error = (result.error or "").replace("|", "\\|").replace("\n", " ")
        lines.append(
            f"| {Path(result.source).name} | {result.status} | "
            f"{result.page_count if result.page_count is not None else '-'} | "
            f"{result.document_count if result.document_count is not None else '-'} | "
            f"{result.chunk_count if result.chunk_count is not None else '-'} | "
            f"{result.elapsed_seconds:.1f} | {error} |"
        )
    BATCH_MARKDOWN_PATH.write_text(
        "\n".join(lines).rstrip() + "\n",
        encoding="utf-8",
    )


def batch_parse_pdfs(
    *,
    knowledge_dir: Path = KNOWLEDGE_DIR,
    output_root: Path = DEFAULT_OUTPUT_ROOT,
    describe_images: bool = True,
    force: bool = False,
    limit: int | None = None,
) -> list[BatchFileResult]:
    pdf_paths = discover_pdfs(knowledge_dir)
    if limit is not None:
        if limit <= 0:
            raise ValueError("limit 必须大于0")
        pdf_paths = pdf_paths[:limit]
    if not pdf_paths:
        raise RuntimeError(f"知识库中没有 PDF：{knowledge_dir}")

    pending: list[tuple[Path, str]] = []
    cached: dict[Path, tuple[str, dict[str, Any]]] = {}
    for pdf_path in pdf_paths:
        source_hash = calculate_file_hash(pdf_path)
        manifest = None if force else _load_completed_summary(
            pdf_path,
            output_root=output_root,
            source_hash=source_hash,
        )
        if manifest is None:
            pending.append((pdf_path, source_hash))
        else:
            cached[pdf_path] = (source_hash, manifest)

    formula_candidates = {
        path: pdf_has_formula_signals(path) for path, _source_hash in pending
    }
    formula_converter: Any | None = None
    plain_converter: Any | None = None
    image_client = create_glm_image_client() if describe_images and pending else None
    results: list[BatchFileResult] = []

    for index, pdf_path in enumerate(pdf_paths, start=1):
        relative_source = pdf_path.relative_to(knowledge_dir.resolve()).as_posix()
        started = perf_counter()
        if pdf_path in cached:
            source_hash, manifest = cached[pdf_path]
            result = BatchFileResult(
                source=relative_source,
                source_hash=source_hash,
                status="reused",
                output_dir=str(get_output_dir(
                    pdf_path,
                    output_root=output_root,
                    source_hash=source_hash,
                )),
                page_count=int(manifest.get("page_count", 0)),
                document_count=int(manifest.get("structured_document_count", 0)),
                chunk_count=int(manifest.get("structured_chunk_count", 0)),
                content_type_counts=dict(manifest.get("structured_counts_by_type", {})),
                elapsed_seconds=perf_counter() - started,
                error=None,
            )
            print(f"[{index}/{len(pdf_paths)}] 复用：{relative_source}")
        else:
            source_hash = next(value for path, value in pending if path == pdf_path)
            print(f"[{index}/{len(pdf_paths)}] 开始：{relative_source}")
            try:
                if formula_candidates[pdf_path]:
                    print("  - 检测到公式特征：启用逐页公式富化")
                    if formula_converter is None:
                        formula_converter = create_docling_converter(
                            DEFAULT_IMAGE_SCALE,
                            pdf_backend=BATCH_PDF_BACKEND,
                            do_formula_enrichment=True,
                        )
                    summary = _parse_formula_pdf_by_page(
                        pdf_path,
                        output_root=output_root,
                        source_hash=source_hash,
                        converter=formula_converter,
                        image_client=image_client,
                        describe_images=describe_images,
                        force=force,
                    )
                else:
                    print("  - 未检测到公式特征：使用 Docling 快速模式")
                    if plain_converter is None:
                        plain_converter = create_docling_converter(
                            DEFAULT_IMAGE_SCALE,
                            pdf_backend=BATCH_PDF_BACKEND,
                            do_formula_enrichment=False,
                        )
                    summary = parse_pdf(
                        pdf_path,
                        output_root=output_root,
                        describe_images=describe_images,
                        converter=plain_converter,
                        image_client=image_client,
                    )
                    if summary.undecoded_formula_count:
                        print("  - 快速模式发现未解码公式，切换逐页公式富化")
                        if formula_converter is None:
                            formula_converter = create_docling_converter(
                                DEFAULT_IMAGE_SCALE,
                                pdf_backend=BATCH_PDF_BACKEND,
                                do_formula_enrichment=True,
                            )
                        summary = _parse_formula_pdf_by_page(
                            pdf_path,
                            output_root=output_root,
                            source_hash=source_hash,
                            converter=formula_converter,
                            image_client=image_client,
                            describe_images=describe_images,
                            force=force,
                        )
                result = BatchFileResult(
                    source=relative_source,
                    source_hash=source_hash,
                    status="completed",
                    output_dir=summary.output_dir,
                    page_count=summary.page_count,
                    document_count=summary.structured_document_count,
                    chunk_count=summary.structured_chunk_count,
                    content_type_counts=dict(summary.structured_counts_by_type),
                    elapsed_seconds=perf_counter() - started,
                    error=None,
                )
                print(
                    f"[{index}/{len(pdf_paths)}] 完成：{relative_source} -> "
                    f"{summary.structured_chunk_count} Chunks"
                )
            except Exception as exc:
                result = BatchFileResult(
                    source=relative_source,
                    source_hash=source_hash,
                    status="failed",
                    output_dir=str(get_output_dir(
                        pdf_path,
                        output_root=output_root,
                        source_hash=source_hash,
                    )),
                    page_count=None,
                    document_count=None,
                    chunk_count=None,
                    content_type_counts={},
                    elapsed_seconds=perf_counter() - started,
                    error=f"{type(exc).__name__}: {exc}",
                )
                print(f"[{index}/{len(pdf_paths)}] 失败：{relative_source} - {result.error}")
        results.append(result)
        _write_batch_report(
            results,
            knowledge_dir=knowledge_dir,
            describe_images=describe_images,
        )

    return results


def main() -> int:
    try:
        from dotenv import load_dotenv
    except ModuleNotFoundError:
        print("缺少python-dotenv，请先安装项目依赖")
        return 2
    load_dotenv(APP_DIR / ".env", override=True)

    parser = argparse.ArgumentParser(
        description="批量解析知识库全部PDF，支持Hash缓存与断点续跑",
    )
    parser.add_argument("--knowledge-dir", type=Path, default=KNOWLEDGE_DIR)
    parser.add_argument(
        "--skip-image-description",
        action="store_true",
        help="保留裁图但不调用GLM图片描述",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="忽略完整缓存，强制重新解析全部PDF",
    )
    parser.add_argument(
        "--limit",
        type=int,
        help="只处理排序后的前N个PDF，用于调试",
    )
    args = parser.parse_args()

    try:
        results = batch_parse_pdfs(
            knowledge_dir=args.knowledge_dir.resolve(),
            describe_images=not args.skip_image_description,
            force=args.force,
            limit=args.limit,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"[批量解析失败] {type(exc).__name__}: {exc}")
        return 2

    failed = [result for result in results if result.status == "failed"]
    print(
        "批量解析结束："
        f"共 {len(results)} 个PDF，"
        f"成功/复用 {len(results) - len(failed)}，失败 {len(failed)}"
    )
    print(f"报告：{BATCH_MARKDOWN_PATH}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
