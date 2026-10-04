"""为图片 Chunk 建立“短检索子块 -> 完整内容父块”关系。

这个模块不重新解析 PDF，也不调用图片模型。它在已有的结构化 Chunk 上：

1. 保留原 ``image_summary`` Chunk 作为完整父块；
2. 从父块抽取图题、章节和图片检索摘要，生成较短的检索子块；
3. 用 ``parent_chunk_id`` 将子块稳定地指向父块；
4. 保持原父块 ``chunk_id`` 不变，避免无意义地重做已有向量。
"""

from __future__ import annotations

from collections import defaultdict

from langchain_core.documents import Document

from .text_splitter import calculate_text_hash, create_chunk_id


IMAGE_PARENT_CONTENT_TYPE = "image_summary"
IMAGE_CHILD_CONTENT_TYPE = "image_retrieval"
IMAGE_CHILD_SCHEMA_VERSION = "image-parent-child-v1"


def _required_text(metadata: dict, field_name: str) -> str:
    value = metadata.get(field_name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"图片父块缺少有效元数据：{field_name}")
    return value.strip()


def _selected_lines(content: str) -> list[str]:
    # 章节优先从结构化 metadata 读取，避免“章节/所属章节”重复。
    prefixes = ("图题：", "图片检索摘要：")
    selected: list[str] = []
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if line.startswith(prefixes) and line not in selected:
            selected.append(line)
    return selected


def build_image_retrieval_text(parent: Document) -> str:
    """从完整图片父块生成紧凑但可独立理解的检索文本。"""
    metadata = parent.metadata
    lines = ["图片检索卡片"]

    source = str(metadata.get("source", "")).strip()
    if source:
        lines.append(f"来源文件：{source}")

    pdf_page = metadata.get("pdf_page")
    if isinstance(pdf_page, int) and pdf_page > 0:
        lines.append(f"PDF页码：{pdf_page}")

    section = str(metadata.get("section", "")).strip()
    if section:
        lines.append(f"所属章节：{section}")

    selected = _selected_lines(parent.page_content)
    lines.extend(line for line in selected if line not in lines)

    # 兼容旧缓存：极少数图片块可能没有显式“图片检索摘要”行。
    # 此时保留一段有限长度的原文，不能生成空的检索子块。
    if not selected:
        compact = " ".join(parent.page_content.split())
        if compact:
            lines.append(f"图片检索摘要：{compact[:600]}")

    return "\n".join(lines).strip()


def _copy_document(document: Document) -> Document:
    return Document(
        page_content=document.page_content,
        metadata=dict(document.metadata),
    )


def expand_image_parent_child_documents(
    documents: list[Document],
) -> list[Document]:
    """在每个来源文件末尾追加图片检索子块。

    输入应为已经具有 ``chunk_id``、``chunk_index`` 和 ``total_chunks``
    的最终 Chunk。重复调用是幂等的：已存在 v1 子块时不会再次追加。
    """
    if not documents:
        return []

    by_source: dict[str, list[Document]] = defaultdict(list)
    source_order: list[str] = []
    for document in documents:
        source = _required_text(document.metadata, "source")
        if source not in by_source:
            source_order.append(source)
        by_source[source].append(_copy_document(document))

    expanded: list[Document] = []
    for source in source_order:
        source_documents = by_source[source]
        existing_child_ids = {
            _required_text(document.metadata, "parent_chunk_id"):
            _required_text(document.metadata, "chunk_id")
            for document in source_documents
            if document.metadata.get("content_type") == IMAGE_CHILD_CONTENT_TYPE
            and document.metadata.get("parent_child_schema")
            == IMAGE_CHILD_SCHEMA_VERSION
        }
        # Chroma 读回按 chunk_id 排序，解析器按原文顺序返回；两种入口
        # 必须为同一图片生成相同子块序号/ID。已入库 v1 子块采用 ID 顺序。
        parents = sorted(
            (
                document
                for document in source_documents
                if document.metadata.get("content_type") == IMAGE_PARENT_CONTENT_TYPE
                and _required_text(document.metadata, "chunk_id")
                not in existing_child_ids
            ),
            key=lambda document: _required_text(document.metadata, "chunk_id"),
        )
        final_total = len(source_documents) + len(parents)

        for document in source_documents:
            document.metadata["total_chunks"] = final_total
            if document.metadata.get("content_type") == IMAGE_PARENT_CONTENT_TYPE:
                document.metadata["retrieval_role"] = "parent"
                parent_id = _required_text(document.metadata, "chunk_id")
                if parent_id in existing_child_ids:
                    # 旧隔离入库仅写入子块，原父块尚未写回关联字段。
                    document.metadata["retrieval_child_id"] = existing_child_ids[parent_id]
            expanded.append(document)

        next_index = len(source_documents)
        for parent in parents:
            parent_id = _required_text(parent.metadata, "chunk_id")
            file_hash = _required_text(parent.metadata, "file_hash")
            content = build_image_retrieval_text(parent)
            metadata = dict(parent.metadata)
            metadata.update(
                {
                    "content_type": IMAGE_CHILD_CONTENT_TYPE,
                    "retrieval_role": "child",
                    "parent_chunk_id": parent_id,
                    "parent_content_type": IMAGE_PARENT_CONTENT_TYPE,
                    "parent_child_schema": IMAGE_CHILD_SCHEMA_VERSION,
                    "preserve_as_unit": True,
                    "chunk_index": next_index,
                    "total_chunks": final_total,
                    "content_hash": calculate_text_hash(content),
                }
            )
            page = metadata.get("page")
            if not isinstance(page, int):
                page = None
            metadata["chunk_id"] = create_chunk_id(
                source=source,
                file_hash=file_hash,
                page=page,
                chunk_index=next_index,
                content=content,
            )
            expanded.append(Document(page_content=content, metadata=metadata))
            parent.metadata["retrieval_child_id"] = metadata["chunk_id"]
            next_index += 1

    return expanded


__all__ = [
    "IMAGE_CHILD_CONTENT_TYPE",
    "IMAGE_CHILD_SCHEMA_VERSION",
    "IMAGE_PARENT_CONTENT_TYPE",
    "build_image_retrieval_text",
    "expand_image_parent_child_documents",
]
