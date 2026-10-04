"""生产 RAG 使用的 Docling PDF Chunk 加载器。

优先复用文件 Hash 对应的完整结构化缓存；缓存不存在时才运行 Docling。
公式候选 PDF 使用逐页检查点，普通 PDF 使用快速模式，并在发现未解码
公式时回退到逐页公式富化。
"""

from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path
from typing import Any, Iterable

from langchain_core.documents import Document

from .models import SourceSnapshot
from .multimodal_parser import (
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


PRODUCTION_PDF_CACHE_ROOT = APP_DIR / "storage" / "rag_pdf_cache"
PDF_BACKEND = "pypdfium2"
_FORMULA_SIGNAL_RE = re.compile(
    r"[α-ωΑ-Ω∑∫√≤≥≈]|(?:公式|式中|equation\b|where\s*[:,])",
    re.IGNORECASE,
)


class StructuredPdfLoadError(RuntimeError):
    """PDF 无法从结构化缓存读取或无法完成 Docling 解析。"""


def _read_chunks_jsonl(path: Path) -> list[Document]:
    chunks: list[Document] = []
    with path.open("r", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, start=1):
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as exc:
                raise StructuredPdfLoadError(
                    f"结构化 Chunk 第 {line_number} 行不是有效 JSON：{path}"
                ) from exc
            content = payload.get("page_content")
            metadata = payload.get("metadata")
            if not isinstance(content, str) or not content.strip():
                raise StructuredPdfLoadError(
                    f"结构化 Chunk 第 {line_number} 行缺少正文：{path}"
                )
            if not isinstance(metadata, dict):
                raise StructuredPdfLoadError(
                    f"结构化 Chunk 第 {line_number} 行缺少元数据：{path}"
                )
            chunks.append(Document(page_content=content, metadata=dict(metadata)))
    if not chunks:
        raise StructuredPdfLoadError(f"结构化缓存没有任何 Chunk：{path}")
    return chunks


def _load_complete_cache(
    pdf_path: Path,
    *,
    output_root: Path,
    source_hash: str,
) -> tuple[Path, dict[str, Any]] | None:
    output_dir = get_output_dir(
        pdf_path,
        output_root=output_root,
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
        or manifest.get("requested_pages") != "all"
        or not isinstance(manifest.get("structured_chunk_count"), int)
    ):
        return None
    return output_dir, manifest


def _load_page_cache(
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


def _pdf_has_formula_signals(pdf_path: Path) -> bool:
    try:
        from pypdf import PdfReader
    except ModuleNotFoundError as exc:
        raise StructuredPdfLoadError("缺少 pypdf，无法进行公式候选预检") from exc
    reader = PdfReader(str(pdf_path))
    text = "\n".join((page.extract_text() or "") for page in reader.pages)
    return bool(_FORMULA_SIGNAL_RE.search(text))


def _page_count(pdf_path: Path) -> int:
    try:
        from pypdf import PdfReader
    except ModuleNotFoundError as exc:
        raise StructuredPdfLoadError("缺少 pypdf，无法读取 PDF 页数") from exc
    return len(PdfReader(str(pdf_path)).pages)


def _merge_page_outputs(
    pdf_path: Path,
    *,
    output_root: Path,
    source_hash: str,
    page_count: int,
) -> Any:
    from docling_core.types.doc import DoclingDocument

    page_documents = []
    page_outputs: list[tuple[Path, dict[str, Any]]] = []
    for page_no in range(1, page_count + 1):
        loaded = _load_page_cache(
            pdf_path,
            output_root=output_root,
            source_hash=source_hash,
            page_no=page_no,
        )
        if loaded is None:
            raise StructuredPdfLoadError(
                f"第 {page_no} 页检查点不完整，不能合并全文"
            )
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
            "pdf_backend": PDF_BACKEND,
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
) -> Any:
    page_count = _page_count(pdf_path)
    for page_no in range(1, page_count + 1):
        if _load_page_cache(
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


class StructuredPdfChunkLoader:
    """把 PDF 快照转换成可直接写入 Chroma 的结构化 Chunk。"""

    def __init__(
        self,
        *,
        knowledge_dir: Path,
        output_root: Path = PRODUCTION_PDF_CACHE_ROOT,
        fallback_cache_roots: Iterable[Path] = (DEFAULT_OUTPUT_ROOT,),
        describe_images: bool = True,
    ):
        self.knowledge_dir = Path(knowledge_dir).resolve()
        self.output_root = Path(output_root).resolve()
        self.fallback_cache_roots = tuple(
            Path(root).resolve() for root in fallback_cache_roots
        )
        self.describe_images = bool(describe_images)

    def _resolve_and_validate(self, snapshot: SourceSnapshot) -> Path:
        if snapshot.file_type != ".pdf":
            raise StructuredPdfLoadError(
                f"结构化 PDF Loader 不接受文件类型：{snapshot.file_type}"
            )
        candidate = self.knowledge_dir / snapshot.relative_path
        if candidate.is_symlink():
            raise StructuredPdfLoadError(
                f"知识文件暂不接受符号链接：{snapshot.relative_path}"
            )
        try:
            resolved = candidate.resolve(strict=True)
        except OSError as exc:
            raise StructuredPdfLoadError(
                f"PDF 不存在或无法访问：{snapshot.relative_path}"
            ) from exc
        if not resolved.is_relative_to(self.knowledge_dir):
            raise StructuredPdfLoadError(
                f"PDF 越过知识目录边界：{snapshot.relative_path}"
            )
        stat = resolved.stat()
        if (
            stat.st_size != snapshot.file_size
            or stat.st_mtime_ns != snapshot.modified_at_ns
            or calculate_file_hash(resolved) != snapshot.file_hash
        ):
            raise StructuredPdfLoadError(
                f"PDF 在扫描后发生变化：{snapshot.relative_path}"
            )
        return resolved

    def _find_cache(self, pdf_path: Path, source_hash: str) -> Path | None:
        for root in (self.output_root, *self.fallback_cache_roots):
            loaded = _load_complete_cache(
                pdf_path,
                output_root=root,
                source_hash=source_hash,
            )
            if loaded is not None:
                return loaded[0]
        return None

    def _parse(self, pdf_path: Path, source_hash: str) -> Path:
        self.output_root.mkdir(parents=True, exist_ok=True)
        image_client = create_glm_image_client() if self.describe_images else None
        if _pdf_has_formula_signals(pdf_path):
            print("  - 检测到公式特征：启用逐页公式富化")
            converter = create_docling_converter(
                DEFAULT_IMAGE_SCALE,
                pdf_backend=PDF_BACKEND,
                do_formula_enrichment=True,
            )
            summary = _parse_formula_pdf_by_page(
                pdf_path,
                output_root=self.output_root,
                source_hash=source_hash,
                converter=converter,
                image_client=image_client,
                describe_images=self.describe_images,
            )
        else:
            print("  - 未检测到公式特征：使用 Docling 快速模式")
            converter = create_docling_converter(
                DEFAULT_IMAGE_SCALE,
                pdf_backend=PDF_BACKEND,
                do_formula_enrichment=False,
            )
            summary = parse_pdf(
                pdf_path,
                output_root=self.output_root,
                describe_images=self.describe_images,
                converter=converter,
                image_client=image_client,
            )
            if summary.undecoded_formula_count:
                print("  - 快速模式发现未解码公式，切换逐页公式富化")
                formula_converter = create_docling_converter(
                    DEFAULT_IMAGE_SCALE,
                    pdf_backend=PDF_BACKEND,
                    do_formula_enrichment=True,
                )
                summary = _parse_formula_pdf_by_page(
                    pdf_path,
                    output_root=self.output_root,
                    source_hash=source_hash,
                    converter=formula_converter,
                    image_client=image_client,
                    describe_images=self.describe_images,
                )
        return Path(summary.output_dir).resolve()

    def load_snapshot(self, snapshot: SourceSnapshot) -> list[Document]:
        pdf_path = self._resolve_and_validate(snapshot)
        output_dir = self._find_cache(pdf_path, snapshot.file_hash)
        if output_dir is None:
            print(f"开始 Docling 解析：{snapshot.relative_path}")
            output_dir = self._parse(pdf_path, snapshot.file_hash)
        else:
            print(f"复用 Docling 缓存：{snapshot.relative_path}")

        chunks = _read_chunks_jsonl(output_dir / "chunks.jsonl")
        for chunk in chunks:
            if chunk.metadata.get("source") != snapshot.relative_path:
                raise StructuredPdfLoadError(
                    f"PDF Chunk 来源不一致：{snapshot.relative_path}"
                )
            if chunk.metadata.get("file_hash") != snapshot.file_hash:
                raise StructuredPdfLoadError(
                    f"PDF Chunk Hash 不一致：{snapshot.relative_path}"
                )
        self._resolve_and_validate(snapshot)
        print(
            f"结构化读取完成：{snapshot.relative_path}，"
            f"得到 {len(chunks)} 个 Chunk"
        )
        return chunks


def create_default_structured_pdf_loader() -> StructuredPdfChunkLoader:
    from ..config import KNOWLEDGE_DIR

    raw_describe_images = os.getenv("RAG_DESCRIBE_PDF_IMAGES", "true")
    normalized = raw_describe_images.strip().casefold()
    if normalized in {"1", "true", "yes", "on"}:
        describe_images = True
    elif normalized in {"0", "false", "no", "off"}:
        describe_images = False
    else:
        raise ValueError(
            "RAG_DESCRIBE_PDF_IMAGES 必须是 "
            "1/0、true/false、yes/no 或 on/off"
        )

    return StructuredPdfChunkLoader(
        knowledge_dir=KNOWLEDGE_DIR,
        describe_images=describe_images,
    )


__all__ = [
    "PRODUCTION_PDF_CACHE_ROOT",
    "StructuredPdfChunkLoader",
    "StructuredPdfLoadError",
    "create_default_structured_pdf_loader",
]
