from langchain_core.documents import Document

from aa_my_agent.rag.structured_document_builder import (
    _build_picture_search_summary,
    _normalize_extracted_text,
    split_markdown_table,
)
from aa_my_agent.rag.text_splitter import TextSplitter


def _metadata(**extra):
    return {
        "source": "sample.pdf",
        "file_hash": "a" * 64,
        "page": 0,
        **extra,
    }


def test_preserved_structure_is_not_split() -> None:
    content = "公式上下文" * 40
    splitter = TextSplitter(
        chunk_size=30,
        chunk_overlap=0,
        split_separators=("\n\n", "\n", "。", ""),
    )

    chunks = splitter.split_documents(
        [
            Document(
                page_content=content,
                metadata=_metadata(
                    content_type="formula",
                    preserve_as_unit=True,
                ),
            )
        ]
    )

    assert len(chunks) == 1
    assert chunks[0].page_content == content
    assert chunks[0].metadata["content_type"] == "formula"
    assert chunks[0].metadata["chunk_index"] == 0
    assert chunks[0].metadata["total_chunks"] == 1
    assert chunks[0].metadata["chunk_id"]


def test_plain_text_still_uses_existing_splitter() -> None:
    splitter = TextSplitter(
        chunk_size=20,
        chunk_overlap=0,
        split_separators=("。", ""),
    )

    chunks = splitter.split_documents(
        [
            Document(
                page_content="第一段正文很长很长。第二段正文也很长很长。第三段正文继续很长。",
                metadata=_metadata(
                    content_type="text",
                    preserve_as_unit=False,
                ),
            )
        ]
    )

    assert len(chunks) > 1
    assert [chunk.metadata["chunk_index"] for chunk in chunks] == list(
        range(len(chunks))
    )


def test_large_markdown_table_splits_by_rows_and_repeats_header() -> None:
    table = "\n".join(
        [
            "| 节点 | 数值 |",
            "| --- | --- |",
            *[f"| 节点{i} | {i * 10} |" for i in range(12)],
        ]
    )

    parts = split_markdown_table(table, max_chars=90)

    assert len(parts) > 1
    assert all(part.startswith("| 节点 | 数值 |\n| --- | --- |") for part in parts)
    merged_rows = "\n".join(
        row
        for part in parts
        for row in part.splitlines()[2:]
    )
    for index in range(12):
        assert merged_rows.count(f"| 节点{index} | {index * 10} |") == 1


def test_normalize_extracted_text_only_removes_spaces_between_cjk() -> None:
    text = "实现网 络资源、计\t算能力与统\u00a0一编 排。\n内生 6G 与 RAG 系统"

    normalized = _normalize_extracted_text(text)

    assert normalized == "实现网络资源、计算能力与统一编排。\n内生 6G 与 RAG 系统"


def test_picture_search_summary_expands_existing_node_range() -> None:
    summary = _build_picture_search_summary(
        caption="图2 网络拓扑",
        description="可见节点编号范围：1-12",
        context=["坐标为 (0.232, 0.992)"],
    )

    assert "图2 网络拓扑" in summary
    assert "节点7" in summary
    assert "节点11" in summary
    assert "0.232" in summary
    assert "0.992" in summary
