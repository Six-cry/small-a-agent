"""Unified experimental PDF parser for text, tables, formulas, and pictures.

This module does not update the production manifest or Chroma collection.  It
creates reviewable Markdown, JSON metadata, and cropped picture files so the
result can be inspected before a future loader integration.
"""

from __future__ import annotations

import argparse
import base64
from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any


APP_DIR = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_ROOT = APP_DIR / "eval" / "rag" / "reports" / "unified_parser"
DEFAULT_IMAGE_MODEL = "glm-4v-flash"
DEFAULT_IMAGE_SCALE = 4.0
IMAGE_PROMPT_VERSION = "conservative-v1"

CONSERVATIVE_IMAGE_PROMPT = """请描述这张PDF中的技术图片，只记录能够从图片中明确确认的内容。
要求：
1. 抄录清晰可见的标题、图例、框中文字、节点名称、坐标轴和关键数字。
2. 描述能够确认的包含关系、箭头方向和连接关系。
3. 对密集拓扑图不要猜测或补全完整边表；看不清时明确写“无法确认”。
4. 不解释图片背后的原理，不添加图片没有明确表达的结论。
5. 输出简洁中文，适合作为知识库检索摘要。"""

TOPOLOGY_INDEX_PROMPT = """请为这张密集网络拓扑图生成保守的知识库索引摘要。
只允许输出：图号/图注、可见节点编号范围、图片属于网络拓扑图，以及“具体边、箭头和数值关系需要针对问题查看原图”。
不要抄录任何数值对，不要列出任何节点连接，不要推断坐标、权重、路径或边的含义。"""


@dataclass(frozen=True)
class PictureRecord:
    picture_index: int
    page: int
    caption: str
    image_path: str | None
    image_hash: str | None
    width: int | None
    height: int | None
    bbox: str | None
    description: str
    description_model: str | None
    prompt_version: str | None
    quality_status: str
    error: str | None


@dataclass(frozen=True)
class PageRecord:
    page: int
    markdown_path: str
    picture_indexes: tuple[int, ...]


@dataclass(frozen=True)
class ParseSummary:
    source: str
    source_hash: str
    requested_pages: str
    image_question: str | None
    output_dir: str
    combined_markdown: str
    docling_document_path: str
    structured_documents_path: str
    structured_chunks_path: str
    structured_document_count: int
    structured_chunk_count: int
    structured_counts_by_type: dict[str, int]
    page_count: int
    text_item_count: int
    table_count: int
    table_quality_status: str
    formula_count: int
    undecoded_formula_count: int
    formula_quality_status: str
    picture_count: int
    described_picture_count: int
    pages: tuple[PageRecord, ...]
    pictures: tuple[PictureRecord, ...]


def _write_documents_jsonl(path: Path, documents: list[Any]) -> None:
    """以可审查、可重复读取的 JSONL 保存 LangChain Documents。"""
    with path.open("w", encoding="utf-8") as stream:
        for document in documents:
            payload = {
                "page_content": document.page_content,
                "metadata": document.metadata,
            }
            stream.write(json.dumps(payload, ensure_ascii=False) + "\n")


def calculate_file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def detect_pdf_text_quality_by_page(pdf_path: Path) -> dict[int, str]:
    """用原 PDF 文字层判断每页正文来自原生文本还是 OCR。"""
    try:
        from pypdf import PdfReader
    except ModuleNotFoundError as exc:
        raise RuntimeError("缺少 pypdf，无法标记 OCR 文字质量") from exc
    try:
        reader = PdfReader(str(pdf_path))
        return {
            page_no: (
                "native_text"
                if (page.extract_text() or "").strip()
                else "machine_ocr"
            )
            for page_no, page in enumerate(reader.pages, start=1)
        }
    except Exception:
        # 无法可靠证明存在原生文字层时采用更保守的机器 OCR 标记。
        return {1: "machine_ocr"}


def create_docling_converter(
    image_scale: float = DEFAULT_IMAGE_SCALE,
    *,
    pdf_backend: str = "docling_parse",
    do_formula_enrichment: bool = True,
) -> Any:
    """创建 Docling PDF 转换器，并允许避开不稳定的原生解析后端。"""
    if image_scale <= 0:
        raise ValueError("image_scale 必须大于0")
    try:
        from docling.backend.pypdfium2_backend import PyPdfiumDocumentBackend
        from docling.datamodel.base_models import InputFormat
        from docling.datamodel.pipeline_options import PdfPipelineOptions
        from docling.document_converter import DocumentConverter, PdfFormatOption
    except ModuleNotFoundError as exc:
        raise RuntimeError("缺少 docling，请先安装多模态RAG依赖") from exc

    pipeline_options = PdfPipelineOptions()
    pipeline_options.do_formula_enrichment = do_formula_enrichment
    pipeline_options.generate_picture_images = True
    pipeline_options.images_scale = float(image_scale)
    if pdf_backend == "docling_parse":
        format_option = PdfFormatOption(pipeline_options=pipeline_options)
    elif pdf_backend == "pypdfium2":
        format_option = PdfFormatOption(
            pipeline_options=pipeline_options,
            backend=PyPdfiumDocumentBackend,
        )
    else:
        raise ValueError(
            "pdf_backend 只支持 docling_parse 或 pypdfium2"
        )
    return DocumentConverter(
        format_options={
            InputFormat.PDF: format_option,
        }
    )


def create_glm_image_client() -> Any | None:
    """使用当前环境创建 GLM 图片客户端；没有 Key 时返回 None。"""
    api_key = os.getenv("ZHIPUAI_API_KEY", "").strip()
    if not api_key:
        return None
    try:
        from openai import OpenAI
    except ModuleNotFoundError as exc:
        raise RuntimeError("缺少 openai，无法调用 GLM 图片描述 API") from exc
    return OpenAI(
        api_key=api_key,
        base_url="https://open.bigmodel.cn/api/paas/v4",
    )


def get_output_dir(
    pdf_path: Path,
    *,
    output_root: Path = DEFAULT_OUTPUT_ROOT,
    page_range: tuple[int, int] | None = None,
    image_question: str | None = None,
    source_hash: str | None = None,
) -> Path:
    """按源文件 Hash、页码范围和图片问题生成确定性输出目录。"""
    resolved_pdf = pdf_path.expanduser().resolve(strict=True)
    file_hash = source_hash or calculate_file_hash(resolved_pdf)
    safe_stem = re.sub(
        r"[^\w.-]+", "_", resolved_pdf.stem, flags=re.UNICODE
    ).strip("_")
    range_label = (
        "all"
        if page_range is None
        else f"pages_{page_range[0]:04d}-{page_range[1]:04d}"
    )
    if image_question:
        question_hash = hashlib.sha256(
            image_question.strip().encode("utf-8")
        ).hexdigest()[:8]
        range_label += f"_question_{question_hash}"
    return (output_root / f"{safe_stem}_{file_hash[:12]}_{range_label}").resolve()


def parse_page_range(value: str | None) -> tuple[int, int] | None:
    if value is None:
        return None
    match = re.fullmatch(r"\s*(\d+)(?:\s*-\s*(\d+))?\s*", value)
    if match is None:
        raise ValueError("--pages 必须是单页数字或起止页，例如 4 或 2-5")
    first = int(match.group(1))
    last = int(match.group(2) or first)
    if first <= 0 or last < first:
        raise ValueError("--pages 页码必须从1开始，结束页不能小于开始页")
    return first, last


def _bbox_to_text(picture: Any) -> str | None:
    if not getattr(picture, "prov", None):
        return None
    bbox = picture.prov[0].bbox
    values = (
        getattr(bbox, "l", None),
        getattr(bbox, "t", None),
        getattr(bbox, "r", None),
        getattr(bbox, "b", None),
    )
    if any(value is None for value in values):
        return None
    return ",".join(f"{float(value):.3f}" for value in values)


def _build_image_prompt(caption: str, image_question: str | None) -> tuple[str, str]:
    if image_question:
        prompt = (
            "请只根据图片回答下面的问题；看不清时明确回答无法确认，"
            "不要利用常识猜测。\n"
            f"问题：{image_question.strip()}"
        )
        version = "question-focused-v1"
    else:
        normalized_caption = "".join(caption.casefold().split())
        if "网络拓扑" in normalized_caption:
            prompt = TOPOLOGY_INDEX_PROMPT
            version = "topology-index-safe-v1"
        else:
            prompt = CONSERVATIVE_IMAGE_PROMPT
            version = IMAGE_PROMPT_VERSION
    if caption:
        prompt += f"\n原文图注：{caption}"
    return prompt, version


def _describe_image(
    *,
    client: Any,
    model: str,
    image_path: Path,
    caption: str,
    image_question: str | None,
) -> tuple[str, str]:
    prompt, prompt_version = _build_image_prompt(caption, image_question)
    encoded = base64.b64encode(image_path.read_bytes()).decode("ascii")
    response = client.chat.completions.create(
        model=model,
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": "data:image/png;base64," + encoded,
                        },
                    },
                ],
            }
        ],
        temperature=0.0,
        max_tokens=1000,
    )
    description = (response.choices[0].message.content or "").strip()
    return description, prompt_version


def describe_image_for_question(
    *,
    client: Any,
    image_path: Path,
    question: str,
    caption: str = "",
    model: str = DEFAULT_IMAGE_MODEL,
) -> tuple[str, str]:
    """公开的按问题读图入口；只处理一张已存在的本地裁图。"""
    resolved_image = image_path.expanduser().resolve(strict=True)
    if not resolved_image.is_file():
        raise ValueError("image_path 必须是存在的图片文件")
    if not isinstance(question, str) or not question.strip():
        raise ValueError("question 不能为空")
    return _describe_image(
        client=client,
        model=model,
        image_path=resolved_image,
        caption=caption.strip(),
        image_question=question.strip(),
    )


def write_parsed_document_artifacts(
    *,
    resolved_pdf: Path,
    document: Any,
    output_dir: Path,
    source_hash: str,
    requested_pages: str,
    picture_records: list[PictureRecord],
    image_question: str | None = None,
) -> ParseSummary:
    """把已经转换好的 DoclingDocument 写成统一、可入库的全部产物。"""
    try:
        from docling_core.types.doc.base import ImageRefMode
    except ModuleNotFoundError as exc:
        raise RuntimeError("缺少 docling，请先安装多模态RAG依赖") from exc

    output_dir.mkdir(parents=True, exist_ok=True)
    page_dir = output_dir / "pages"
    page_dir.mkdir(parents=True, exist_ok=True)
    docling_document_path = output_dir / "docling_document.json"
    document.save_as_json(
        docling_document_path,
        image_mode=ImageRefMode.PLACEHOLDER,
        ensure_ascii=False,
    )

    pictures_by_page: dict[int, list[PictureRecord]] = {}
    for picture in picture_records:
        pictures_by_page.setdefault(picture.page, []).append(picture)

    page_records: list[PageRecord] = []
    combined_parts = [
        f"# {resolved_pdf.name}",
        "",
        "> 由统一多模态解析器生成。公式和图片描述属于机器识别结果，未经人工核验时不能视为原文的绝对准确转录。",
        "",
    ]
    for page_no in sorted(document.pages):
        page_markdown = document.export_to_markdown(
            page_no=page_no,
            traverse_pictures=False,
        ).strip()
        page_pictures = pictures_by_page.get(page_no, [])
        page_parts = [f"# PDF 第 {page_no} 页", "", page_markdown]
        if page_pictures:
            page_parts.extend(["", "## 图片识别补充", ""])
            for picture in page_pictures:
                page_parts.extend(
                    [
                        f"### 图片 {picture.picture_index}",
                        "",
                        f"- 图注：{picture.caption or '无'}",
                        f"- 裁图：`{picture.image_path or '未生成'}`",
                        f"- 质量状态：`{picture.quality_status}`",
                        "",
                        picture.description or "（没有生成图片描述）",
                    ]
                )
                if picture.error:
                    page_parts.extend(["", f"- 处理说明：{picture.error}"])
                page_parts.append("")
        page_text = "\n".join(page_parts).strip() + "\n"
        page_path = page_dir / f"page_{page_no:04d}.md"
        page_path.write_text(page_text, encoding="utf-8")
        page_records.append(
            PageRecord(
                page=page_no,
                markdown_path=page_path.relative_to(output_dir).as_posix(),
                picture_indexes=tuple(item.picture_index for item in page_pictures),
            )
        )
        combined_parts.extend([page_text.rstrip(), ""])

    combined_markdown = output_dir / "combined.md"
    combined_text = "\n".join(combined_parts).rstrip() + "\n"
    combined_markdown.write_text(combined_text, encoding="utf-8")

    from .structured_document_builder import build_structured_documents
    from .text_splitter import create_default_text_splitter

    knowledge_dir = (APP_DIR / "rag" / "data" / "knowledge").resolve()
    try:
        source = resolved_pdf.relative_to(knowledge_dir).as_posix()
    except ValueError:
        source = resolved_pdf.as_posix()

    structured_documents = build_structured_documents(
        document=document,
        picture_records=picture_records,
        source=source,
        file_hash=source_hash,
        file_size=resolved_pdf.stat().st_size,
        file_name=resolved_pdf.name,
        file_type=resolved_pdf.suffix.casefold(),
        text_quality_by_page=detect_pdf_text_quality_by_page(resolved_pdf),
    )
    structured_documents_path = output_dir / "documents.jsonl"
    _write_documents_jsonl(structured_documents_path, structured_documents)

    structured_chunks = create_default_text_splitter().split_documents(
        structured_documents
    )
    structured_chunks_path = output_dir / "chunks.jsonl"
    _write_documents_jsonl(structured_chunks_path, structured_chunks)
    structured_counts_by_type: dict[str, int] = {}
    for structured_document in structured_documents:
        content_type = str(
            structured_document.metadata.get("content_type", "unknown")
        )
        structured_counts_by_type[content_type] = (
            structured_counts_by_type.get(content_type, 0) + 1
        )

    formula_blocks = re.findall(r"\$\$(.+?)\$\$", combined_text, flags=re.DOTALL)
    undecoded_count = combined_text.count("<!-- formula-not-decoded -->")
    summary = ParseSummary(
        source=str(resolved_pdf),
        source_hash=source_hash,
        requested_pages=requested_pages,
        image_question=image_question.strip() if image_question else None,
        output_dir=str(output_dir),
        combined_markdown=str(combined_markdown),
        docling_document_path=str(docling_document_path),
        structured_documents_path=str(structured_documents_path),
        structured_chunks_path=str(structured_chunks_path),
        structured_document_count=len(structured_documents),
        structured_chunk_count=len(structured_chunks),
        structured_counts_by_type=structured_counts_by_type,
        page_count=len(document.pages),
        text_item_count=len(getattr(document, "texts", [])),
        table_count=len(document.tables),
        table_quality_status="machine_unverified",
        formula_count=len(formula_blocks),
        undecoded_formula_count=undecoded_count,
        formula_quality_status="machine_unverified",
        picture_count=len(picture_records),
        described_picture_count=sum(bool(item.description) for item in picture_records),
        pages=tuple(page_records),
        pictures=tuple(picture_records),
    )
    (output_dir / "manifest.json").write_text(
        json.dumps(asdict(summary), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return summary


def parse_pdf(
    pdf_path: Path,
    *,
    output_root: Path = DEFAULT_OUTPUT_ROOT,
    page_range: tuple[int, int] | None = None,
    image_model: str = DEFAULT_IMAGE_MODEL,
    image_scale: float = DEFAULT_IMAGE_SCALE,
    describe_images: bool = True,
    image_question: str | None = None,
    converter: Any | None = None,
    image_client: Any | None = None,
) -> ParseSummary:
    """Parse one PDF and write inspectable multimodal artifacts."""
    resolved_pdf = pdf_path.expanduser().resolve(strict=True)
    if resolved_pdf.suffix.casefold() != ".pdf":
        raise ValueError("统一多模态解析器当前只接受 PDF 文件")
    if image_scale <= 0:
        raise ValueError("image_scale 必须大于0")

    source_hash = calculate_file_hash(resolved_pdf)
    output_dir = get_output_dir(
        resolved_pdf,
        output_root=output_root,
        page_range=page_range,
        image_question=image_question,
        source_hash=source_hash,
    )
    image_dir = output_dir / "images"
    page_dir = output_dir / "pages"
    image_dir.mkdir(parents=True, exist_ok=True)
    page_dir.mkdir(parents=True, exist_ok=True)

    if converter is None:
        converter = create_docling_converter(image_scale=image_scale)

    convert_kwargs: dict[str, Any] = {}
    if page_range is not None:
        convert_kwargs["page_range"] = page_range
    result = converter.convert(str(resolved_pdf), **convert_kwargs)
    document = result.document
    client = image_client
    if describe_images and client is None:
        client = create_glm_image_client()

    picture_records: list[PictureRecord] = []
    pictures_by_page: dict[int, list[PictureRecord]] = {}
    for picture_index, picture in enumerate(document.pictures, start=1):
        page_no = picture.prov[0].page_no if picture.prov else 0
        caption = picture.caption_text(document).strip()
        picture_image = picture.get_image(document)
        image_path: Path | None = None
        image_hash: str | None = None
        width: int | None = None
        height: int | None = None
        if picture_image is not None:
            image_path = image_dir / f"page_{page_no:04d}_picture_{picture_index:03d}.png"
            picture_image.save(image_path, format="PNG")
            width, height = picture_image.size
            image_hash = calculate_file_hash(image_path)

        description = ""
        model_used: str | None = None
        prompt_version: str | None = None
        error: str | None = None
        quality_status = "caption_only"
        is_probable_decoration = (
            image_path is not None
            and not caption
            and (width or 0) < 160
            and (height or 0) < 160
        )
        if is_probable_decoration:
            quality_status = "skipped_decorative"
        elif describe_images and client is None:
            error = "未配置ZHIPUAI_API_KEY，保留裁图和图注但未生成图片描述"
        elif describe_images and image_path is not None:
            model_used = image_model
            try:
                description, prompt_version = _describe_image(
                    client=client,
                    model=image_model,
                    image_path=image_path,
                    caption=caption,
                    image_question=image_question,
                )
                quality_status = "unverified" if description else "empty"
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                quality_status = "api_error"

        record = PictureRecord(
            picture_index=picture_index,
            page=page_no,
            caption=caption,
            image_path=(
                image_path.relative_to(output_dir).as_posix()
                if image_path is not None
                else None
            ),
            image_hash=image_hash,
            width=width,
            height=height,
            bbox=_bbox_to_text(picture),
            description=description,
            description_model=model_used,
            prompt_version=prompt_version,
            quality_status=quality_status,
            error=error,
        )
        picture_records.append(record)
        pictures_by_page.setdefault(page_no, []).append(record)

    return write_parsed_document_artifacts(
        resolved_pdf=resolved_pdf,
        document=document,
        output_dir=output_dir,
        source_hash=source_hash,
        requested_pages=(
            "all" if page_range is None else f"{page_range[0]}-{page_range[1]}"
        ),
        picture_records=picture_records,
        image_question=image_question,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="统一解析PDF中的文字、表格、公式和图片",
    )
    parser.add_argument("pdf", type=Path, help="需要解析的PDF路径")
    parser.add_argument(
        "--pages",
        help="只解析单页或连续页，例如4或2-5；不填则解析全文",
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        default=DEFAULT_OUTPUT_ROOT,
        help="输出根目录",
    )
    parser.add_argument(
        "--skip-image-description",
        action="store_true",
        help="只裁图，不调用GLM生成图片描述",
    )
    parser.add_argument(
        "--image-question",
        help="可选：让视觉模型只围绕这个具体问题读取每张图片",
    )
    return parser.parse_args()


def main() -> int:
    try:
        from dotenv import load_dotenv
    except ModuleNotFoundError:
        print("缺少python-dotenv，请先安装项目依赖")
        return 2

    load_dotenv(APP_DIR / ".env", override=True)
    args = parse_args()
    try:
        page_range = parse_page_range(args.pages)
        summary = parse_pdf(
            args.pdf,
            output_root=args.output_root,
            page_range=page_range,
            describe_images=not args.skip_image_description,
            image_question=args.image_question,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"[解析失败] {exc}")
        return 1

    print(f"解析完成：{summary.source}")
    print(f"统一Markdown：{summary.combined_markdown}")
    print(f"Docling结构缓存：{summary.docling_document_path}")
    print(f"结构化Document：{summary.structured_documents_path}")
    print(f"最终Chunk：{summary.structured_chunks_path}")
    print(f"机器清单：{Path(summary.output_dir) / 'manifest.json'}")
    print(
        "统计："
        f"页面 {summary.page_count}，"
        f"文本项 {summary.text_item_count}，"
        f"表格 {summary.table_count}，"
        f"LaTeX公式 {summary.formula_count}，"
        f"未解码公式 {summary.undecoded_formula_count}，"
        f"图片 {summary.picture_count}，"
        f"已描述图片 {summary.described_picture_count}"
    )
    print(
        "结构化结果："
        f"{summary.structured_document_count} 个语义Document -> "
        f"{summary.structured_chunk_count} 个最终Chunk，"
        f"类型统计 {summary.structured_counts_by_type}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
