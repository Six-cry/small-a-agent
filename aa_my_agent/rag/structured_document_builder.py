"""将 Docling 的结构化结果转换为适合 RAG 的 LangChain Document。

这里不对整篇 Markdown 做固定长度切分，而是先按照文档结构组装语义单元：

* 普通正文仍交给原有 TextSplitter；
* 公式与相邻的引导句、变量解释放在一起；
* 表格与标题、引导句、表后说明放在一起；
* 图片与图题、上下文和视觉模型描述放在一起。

结构单元通过 ``preserve_as_unit`` 标记，后续分割器不得从中间拆开。
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Sequence
from typing import Any

from docling_core.types.doc import DoclingDocument
from docling_core.types.doc.items.node import DocItem
from docling_core.types.doc.items.picture.picture import PictureItem
from docling_core.types.doc.items.table.table import TableItem
from docling_core.types.doc.items.text import (
    FormulaItem,
    SectionHeaderItem,
    TextItem,
    TitleItem,
)
from docling_core.types.doc.labels import DocItemLabel
from langchain_core.documents import Document


_SKIPPED_TEXT_LABELS = {
    DocItemLabel.CAPTION,
    DocItemLabel.PAGE_FOOTER,
    DocItemLabel.PAGE_HEADER,
}
_FORMULA_INTRO_RE = re.compile(
    r"(?:如下|可得|得到|表示为|表达式|定义为|写为|计算为|公式|方程|模型|约束|目标函数|为)\s*[：:]?\s*$",
    re.IGNORECASE,
)
_FORMULA_EXPLANATION_RE = re.compile(
    r"^(?:式中|其中|这里|上式|上述公式|where\b)", re.IGNORECASE
)
_TABLE_INTRO_RE = re.compile(
    r"(?:见|如|如下|列于|给出|所示|结果为|参数为).*表\s*\d*|表\s*\d+.*(?:为|如下|所示)",
    re.IGNORECASE,
)
_TABLE_EXPLANATION_RE = re.compile(
    r"^(?:表中|由表|从表|根据表|其中)", re.IGNORECASE
)
_PICTURE_INTRO_RE = re.compile(
    r"(?:见|如|如下|所示|展示|示意).*图\s*\d*|图\s*\d+.*(?:为|如下|所示)",
    re.IGNORECASE,
)
_PICTURE_EXPLANATION_RE = re.compile(
    r"^(?:图中|由图|从图|根据图|其中)", re.IGNORECASE
)
_COORDINATE_FRAGMENT_RE = re.compile(
    r"^\s*[（(]\s*[-+]?\d+(?:\.\d+)?\s*[,，]\s*[-+]?\d+(?:\.\d+)?\s*[）)]\s*$"
)
_CJK_INTERNAL_SPACE_RE = re.compile(
    r"(?<=[\u3400-\u4dbf\u4e00-\u9fff])[ \t\u00a0]+"
    r"(?=[\u3400-\u4dbf\u4e00-\u9fff])"
)
_NODE_RANGE_RE = re.compile(
    r"(?:可见)?节点(?:编号)?范围\s*[:：]?\s*(\d+)\s*[-—–~～至到]\s*(\d+)"
)


def build_structured_documents(
    *,
    document: DoclingDocument,
    picture_records: Sequence[Any],
    source: str,
    file_hash: str,
    file_size: int,
    file_name: str,
    file_type: str = ".pdf",
    max_table_chars: int = 1_800,
    text_quality_by_page: dict[int, str] | None = None,
) -> list[Document]:
    """把 DoclingDocument 转成按语义组织的 LangChain Documents。"""

    items = [item for item, _level in document.iterate_items()]
    index_by_ref = {item.self_ref: index for index, item in enumerate(items)}
    section_by_ref = _build_section_paths(items)
    caption_refs = _collect_caption_refs(items)
    consumed_refs = set(caption_refs)
    context_by_ref: dict[str, list[DocItem]] = {}

    for index, item in enumerate(items):
        if isinstance(item, FormulaItem):
            context = _collect_context(
                items,
                index,
                before_predicate=_looks_like_formula_intro,
                after_predicate=_looks_like_formula_explanation,
            )
        elif isinstance(item, TableItem):
            context = _collect_context(
                items,
                index,
                before_predicate=_looks_like_table_intro,
                after_predicate=_looks_like_table_explanation,
                bridge_headings=True,
            )
        elif isinstance(item, PictureItem):
            context = _collect_context(
                items,
                index,
                before_predicate=_looks_like_picture_intro,
                after_predicate=_looks_like_picture_explanation,
            )
        else:
            continue

        context_by_ref[item.self_ref] = context
        consumed_refs.update(context_item.self_ref for context_item in context)

    picture_index_by_ref: dict[str, int] = {}
    picture_index = 0
    for item in items:
        if isinstance(item, PictureItem):
            picture_index_by_ref[item.self_ref] = picture_index
            picture_index += 1

    result: list[Document] = []
    text_buffer: list[DocItem] = []

    def flush_text_buffer() -> None:
        nonlocal text_buffer
        if not text_buffer:
            return
        result.extend(
            _build_text_documents(
                text_buffer,
                section_by_ref=section_by_ref,
                source=source,
                file_hash=file_hash,
                file_size=file_size,
                file_name=file_name,
                file_type=file_type,
                text_quality_by_page=text_quality_by_page,
            )
        )
        text_buffer = []

    for item in items:
        if isinstance(item, (TitleItem, SectionHeaderItem)):
            flush_text_buffer()
            text_buffer.append(item)
            continue

        if isinstance(item, FormulaItem):
            flush_text_buffer()
            result.append(
                _build_formula_document(
                    item,
                    context_by_ref.get(item.self_ref, []),
                    section_by_ref=section_by_ref,
                    source=source,
                    file_hash=file_hash,
                    file_size=file_size,
                    file_name=file_name,
                    file_type=file_type,
                )
            )
            continue

        if isinstance(item, TableItem):
            flush_text_buffer()
            result.extend(
                _build_table_documents(
                    document,
                    item,
                    context_by_ref.get(item.self_ref, []),
                    section_by_ref=section_by_ref,
                    source=source,
                    file_hash=file_hash,
                    file_size=file_size,
                    file_name=file_name,
                    file_type=file_type,
                    max_table_chars=max_table_chars,
                )
            )
            continue

        if isinstance(item, PictureItem):
            flush_text_buffer()
            record_index = picture_index_by_ref[item.self_ref]
            record = (
                picture_records[record_index]
                if record_index < len(picture_records)
                else None
            )
            picture_document = _build_picture_document(
                document,
                item,
                record,
                context_by_ref.get(item.self_ref, []),
                section_by_ref=section_by_ref,
                source=source,
                file_hash=file_hash,
                file_size=file_size,
                file_name=file_name,
                file_type=file_type,
            )
            if picture_document is not None:
                result.append(picture_document)
            continue

        if item.self_ref in consumed_refs or item.label in _SKIPPED_TEXT_LABELS:
            continue

        if isinstance(item, TextItem) and _item_text(item):
            if text_buffer and _page_no(text_buffer[-1]) != _page_no(item):
                flush_text_buffer()
            text_buffer.append(item)

    flush_text_buffer()

    # iterate_items 理论上不会重复 self_ref；这里保留确定性排序，确保重复运行 ID 稳定。
    result.sort(
        key=lambda doc: (
            int(doc.metadata["pdf_page"]),
            index_by_ref.get(str(doc.metadata.get("anchor_ref", "")), 10**9),
            int(doc.metadata.get("table_part_index", 0)),
        )
    )
    return result


def _build_section_paths(items: Sequence[DocItem]) -> dict[str, str]:
    title = ""
    levels: dict[int, str] = {}
    result: dict[str, str] = {}

    for item in items:
        if isinstance(item, TitleItem):
            title = _item_text(item)
            levels.clear()
        elif isinstance(item, SectionHeaderItem):
            level = max(int(getattr(item, "level", 1) or 1), 1)
            for existing_level in list(levels):
                if existing_level >= level:
                    del levels[existing_level]
            levels[level] = _item_text(item)

        parts = [title] if title else []
        parts.extend(levels[level] for level in sorted(levels) if levels[level])
        result[item.self_ref] = " > ".join(parts)

    return result


def _collect_caption_refs(items: Iterable[DocItem]) -> set[str]:
    result: set[str] = set()
    for item in items:
        if not isinstance(item, (TableItem, PictureItem)):
            continue
        for reference in getattr(item, "captions", []):
            ref = getattr(reference, "cref", "")
            if ref:
                result.add(ref)
    return result


def _collect_context(
    items: Sequence[DocItem],
    index: int,
    *,
    before_predicate: Any,
    after_predicate: Any,
    bridge_headings: bool = False,
) -> list[DocItem]:
    anchor = items[index]
    context: list[DocItem] = []

    before = _nearest_context_text(
        items,
        index,
        direction=-1,
        bridge_headings=bridge_headings,
    )
    if before is not None and before_predicate(_item_text(before)):
        context.append(before)

    after = _nearest_context_text(
        items,
        index,
        direction=1,
        bridge_headings=bridge_headings,
    )
    if after is not None and after_predicate(_item_text(after)):
        context.append(after)

    return sorted(
        context,
        key=lambda item: next(
            (position for position, candidate in enumerate(items) if candidate is item),
            10**9,
        ),
    )


def _nearest_context_text(
    items: Sequence[DocItem],
    index: int,
    *,
    direction: int,
    bridge_headings: bool,
) -> TextItem | None:
    anchor_page = _page_no(items[index])
    position = index + direction
    inspected = 0

    while 0 <= position < len(items) and inspected < 4:
        candidate = items[position]
        inspected += 1
        if _page_no(candidate) != anchor_page:
            return None
        if isinstance(candidate, (TitleItem, SectionHeaderItem)):
            if bridge_headings:
                position += direction
                continue
            return None
        if isinstance(candidate, (FormulaItem, TableItem, PictureItem)):
            return None
        if (
            isinstance(candidate, TextItem)
            and candidate.label not in _SKIPPED_TEXT_LABELS
            and _item_text(candidate)
        ):
            return candidate
        position += direction
    return None


def _build_text_documents(
    items: Sequence[DocItem],
    *,
    section_by_ref: dict[str, str],
    source: str,
    file_hash: str,
    file_size: int,
    file_name: str,
    file_type: str,
    text_quality_by_page: dict[int, str] | None = None,
) -> list[Document]:
    groups: list[list[DocItem]] = []
    current: list[DocItem] = []
    current_page: int | None = None

    for item in items:
        page = _page_no(item)
        if current and current_page != page:
            groups.append(current)
            current = []
        current.append(item)
        current_page = page
    if current:
        groups.append(current)

    documents: list[Document] = []
    for group in groups:
        if not any(
            not isinstance(item, (TitleItem, SectionHeaderItem))
            for item in group
        ):
            # 标题会写入后续正文或结构块的 section；不要单独制造
            # 只有标题、没有知识内容的向量块。
            continue
        lines: list[str] = []
        for item in group:
            text = _item_text(item)
            if not text:
                continue
            if isinstance(item, TitleItem):
                lines.append(f"# {text}")
            elif isinstance(item, SectionHeaderItem):
                level = min(max(int(getattr(item, "level", 1) or 1), 1) + 1, 6)
                lines.append(f"{'#' * level} {text}")
            else:
                lines.append(text)

        page_content = "\n\n".join(lines).strip()
        if not page_content:
            continue
        refs = [item.self_ref for item in group]
        anchor = group[0]
        page_no = _page_no(anchor)
        quality_status = "native_text"
        if text_quality_by_page is not None:
            quality_status = text_quality_by_page.get(page_no, "machine_ocr")
        documents.append(
            Document(
                page_content=page_content,
                metadata=_metadata(
                    source=source,
                    file_hash=file_hash,
                    file_size=file_size,
                    file_name=file_name,
                    file_type=file_type,
                    page_no=page_no,
                    content_type="text",
                    section=section_by_ref.get(anchor.self_ref, ""),
                    element_refs=refs,
                    anchor_ref=anchor.self_ref,
                    preserve_as_unit=False,
                    quality_status=quality_status,
                ),
            )
        )
    return documents


def _build_formula_document(
    item: FormulaItem,
    context: Sequence[DocItem],
    *,
    section_by_ref: dict[str, str],
    source: str,
    file_hash: str,
    file_size: int,
    file_name: str,
    file_type: str,
) -> Document:
    before, after = _split_context_around(item, context)
    lines = _context_lines(before)
    formula = _item_text(item)
    lines.append(f"$$\n{formula}\n$$")
    lines.extend(_context_lines(after))
    refs = [context_item.self_ref for context_item in before]
    refs.append(item.self_ref)
    refs.extend(context_item.self_ref for context_item in after)

    return Document(
        page_content=_with_section(section_by_ref.get(item.self_ref, ""), lines),
        metadata=_metadata(
            source=source,
            file_hash=file_hash,
            file_size=file_size,
            file_name=file_name,
            file_type=file_type,
            page_no=_page_no(item),
            content_type="formula",
            section=section_by_ref.get(item.self_ref, ""),
            element_refs=refs,
            anchor_ref=item.self_ref,
            preserve_as_unit=True,
            quality_status="machine_unverified",
        ),
    )


def _build_table_documents(
    document: DoclingDocument,
    item: TableItem,
    context: Sequence[DocItem],
    *,
    section_by_ref: dict[str, str],
    source: str,
    file_hash: str,
    file_size: int,
    file_name: str,
    file_type: str,
    max_table_chars: int,
) -> list[Document]:
    before, after = _split_context_around(item, context)
    caption = item.caption_text(document).strip()
    table_markdown = item.export_to_markdown(document).strip()
    table_parts = split_markdown_table(table_markdown, max_chars=max_table_chars)
    refs = [context_item.self_ref for context_item in before]
    refs.extend(getattr(reference, "cref", "") for reference in item.captions)
    refs.append(item.self_ref)
    refs.extend(context_item.self_ref for context_item in after)
    refs = [ref for ref in refs if ref]
    group_id = _group_id(source, file_hash, "table", refs)
    total_parts = len(table_parts)
    documents: list[Document] = []

    for part_index, table_part in enumerate(table_parts, start=1):
        lines = _context_lines(before)
        if caption:
            lines.append(f"表题：{caption}")
        if total_parts > 1:
            lines.append(f"表格分段：第 {part_index}/{total_parts} 部分")
        lines.append(table_part)
        lines.extend(_context_lines(after))
        metadata = _metadata(
            source=source,
            file_hash=file_hash,
            file_size=file_size,
            file_name=file_name,
            file_type=file_type,
            page_no=_page_no(item),
            content_type="table",
            section=section_by_ref.get(item.self_ref, ""),
            element_refs=refs,
            anchor_ref=item.self_ref,
            preserve_as_unit=True,
            quality_status="machine_unverified",
        )
        metadata.update(
            {
                "group_id": group_id,
                "table_part_index": part_index,
                "table_part_total": total_parts,
            }
        )
        documents.append(
            Document(
                page_content=_with_section(
                    section_by_ref.get(item.self_ref, ""), lines
                ),
                metadata=metadata,
            )
        )
    return documents


def _build_picture_document(
    document: DoclingDocument,
    item: PictureItem,
    record: Any,
    context: Sequence[DocItem],
    *,
    section_by_ref: dict[str, str],
    source: str,
    file_hash: str,
    file_size: int,
    file_name: str,
    file_type: str,
) -> Document | None:
    caption = item.caption_text(document).strip()
    description = str(getattr(record, "description", "") or "").strip()
    quality_status = str(getattr(record, "quality_status", "missing_record"))
    image_path = str(getattr(record, "image_path", "") or "")

    if quality_status in {"skipped_decorative", "api_error", "empty"} and not caption:
        return None
    if not description and not caption:
        return None

    before, after = _split_context_around(item, context)
    search_summary = _build_picture_search_summary(
        caption=caption,
        description=description,
        context=[*_context_lines(before), *_context_lines(after)],
    )
    lines = []
    if search_summary:
        lines.append(f"图片检索摘要：{search_summary}")
    lines.extend(_context_lines(before))
    if caption:
        lines.append(f"图题：{caption}")
    if description:
        lines.append(f"图片内容（机器生成，需结合原图核验）：{description}")
    if image_path:
        lines.append(f"图片文件：{image_path}")
    lines.extend(_context_lines(after))

    refs = [context_item.self_ref for context_item in before]
    refs.extend(getattr(reference, "cref", "") for reference in item.captions)
    refs.append(item.self_ref)
    refs.extend(context_item.self_ref for context_item in after)
    refs = [ref for ref in refs if ref]
    return Document(
        page_content=_with_section(section_by_ref.get(item.self_ref, ""), lines),
        metadata=_metadata(
            source=source,
            file_hash=file_hash,
            file_size=file_size,
            file_name=file_name,
            file_type=file_type,
            page_no=_page_no(item),
            content_type="image_summary",
            section=section_by_ref.get(item.self_ref, ""),
            element_refs=refs,
            anchor_ref=item.self_ref,
            preserve_as_unit=True,
            quality_status=quality_status,
            image_path=image_path,
        ),
    )


def split_markdown_table(table_markdown: str, *, max_chars: int) -> list[str]:
    """按数据行切分过大的 Markdown 表，并在每段重复表头。"""

    table_markdown = table_markdown.strip()
    if not table_markdown or len(table_markdown) <= max_chars:
        return [table_markdown]

    lines = [line for line in table_markdown.splitlines() if line.strip()]
    if len(lines) < 3 or "|" not in lines[0] or "|" not in lines[1]:
        return [table_markdown]

    header = lines[:2]
    rows = lines[2:]
    parts: list[str] = []
    current_rows: list[str] = []
    header_length = len("\n".join(header))

    for row in rows:
        projected = header_length + sum(len(value) + 1 for value in current_rows) + len(row) + 1
        if current_rows and projected > max_chars:
            parts.append("\n".join([*header, *current_rows]))
            current_rows = []
        current_rows.append(row)
    if current_rows:
        parts.append("\n".join([*header, *current_rows]))
    return parts or [table_markdown]


def _metadata(
    *,
    source: str,
    file_hash: str,
    file_size: int,
    file_name: str,
    file_type: str,
    page_no: int,
    content_type: str,
    section: str,
    element_refs: Sequence[str],
    anchor_ref: str,
    preserve_as_unit: bool,
    quality_status: str,
    image_path: str = "",
) -> dict[str, Any]:
    refs = [ref for ref in element_refs if ref]
    metadata: dict[str, Any] = {
        "source": source,
        "file_name": file_name,
        "file_type": file_type,
        "file_hash": file_hash,
        "file_size": file_size,
        "page": max(page_no - 1, 0),
        "pdf_page": page_no,
        "content_type": content_type,
        "section": section,
        "element_refs": ",".join(refs),
        "anchor_ref": anchor_ref,
        "group_id": _group_id(source, file_hash, content_type, refs),
        "preserve_as_unit": preserve_as_unit,
        "quality_status": quality_status,
    }
    if image_path:
        metadata["image_path"] = image_path
    return metadata


def _group_id(
    source: str, file_hash: str, content_type: str, refs: Sequence[str]
) -> str:
    raw = "\0".join([source, file_hash, content_type, *refs])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _split_context_around(
    anchor: DocItem, context: Sequence[DocItem]
) -> tuple[list[DocItem], list[DocItem]]:
    anchor_number = _ref_number(anchor.self_ref)
    before = [item for item in context if _ref_number(item.self_ref) < anchor_number]
    after = [item for item in context if _ref_number(item.self_ref) > anchor_number]
    return before, after


def _ref_number(ref: str) -> int:
    match = re.search(r"/(\d+)$", ref)
    return int(match.group(1)) if match else 10**9


def _context_lines(items: Sequence[DocItem]) -> list[str]:
    return [_item_text(item) for item in items if _item_text(item)]


def _with_section(section: str, lines: Sequence[str]) -> str:
    result = [f"章节：{section}"] if section else []
    result.extend(line for line in lines if line.strip())
    return "\n\n".join(result).strip()


def _page_no(item: DocItem) -> int:
    if item.prov:
        return int(item.prov[0].page_no)
    return 1


def _item_text(item: DocItem) -> str:
    return _normalize_extracted_text(
        str(getattr(item, "text", "") or "")
    ).strip()


def _normalize_extracted_text(text: str) -> str:
    """删除 Docling 偶发插入的中文词内空格，同时保留段落和中英边界。"""
    return _CJK_INTERNAL_SPACE_RE.sub("", text)


def _build_picture_search_summary(
    *,
    caption: str,
    description: str,
    context: Sequence[str],
    max_chars: int = 600,
) -> str:
    """为图片生成紧凑索引文本；只重排已有信息，不推断图片事实。"""
    raw = "\n".join(
        value for value in [caption, description, *context] if value.strip()
    )
    if not raw:
        return ""

    compact = re.sub(r"[#*_`>]+", " ", raw)
    compact = re.sub(r"\s+", " ", compact).strip()
    node_aliases: list[str] = []
    for match in _NODE_RANGE_RE.finditer(compact):
        first, last = int(match.group(1)), int(match.group(2))
        if 0 <= first <= last <= 100 and last - first <= 50:
            node_aliases.extend(f"节点{value}" for value in range(first, last + 1))

    summary = compact[:max_chars].rstrip()
    if node_aliases:
        summary += "；检索节点：" + "、".join(dict.fromkeys(node_aliases))
    return summary


def _looks_like_formula_intro(text: str) -> bool:
    normalized = text.strip()
    return len(normalized) <= 1_500 and (
        bool(_FORMULA_INTRO_RE.search(normalized))
        or bool(re.search(r"由此\s*[，,:：]?\s*$", normalized))
        or normalized.endswith(("：", ":"))
    )


def _looks_like_formula_explanation(text: str) -> bool:
    return len(text) <= 500 and bool(_FORMULA_EXPLANATION_RE.search(text.strip()))


def _looks_like_table_intro(text: str) -> bool:
    return len(text) <= 360 and bool(_TABLE_INTRO_RE.search(text.strip()))


def _looks_like_table_explanation(text: str) -> bool:
    return len(text) <= 500 and bool(_TABLE_EXPLANATION_RE.search(text.strip()))


def _looks_like_picture_intro(text: str) -> bool:
    normalized = text.strip()
    return len(normalized) <= 500 and (
        bool(_PICTURE_INTRO_RE.search(normalized))
        or bool(_COORDINATE_FRAGMENT_RE.fullmatch(normalized))
    )


def _looks_like_picture_explanation(text: str) -> bool:
    normalized = text.strip()
    return len(normalized) <= 700 and (
        bool(_PICTURE_EXPLANATION_RE.search(normalized))
        or bool(_PICTURE_INTRO_RE.search(normalized))
    )


__all__ = ["build_structured_documents", "split_markdown_table"]
