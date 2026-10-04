"""验证图片短检索子块的生成、稳定关系和重复执行安全性。"""

from langchain_core.documents import Document

from aa_my_agent.rag.image_parent_child import (
    IMAGE_CHILD_CONTENT_TYPE,
    expand_image_parent_child_documents,
)
from aa_my_agent.rag.text_splitter import calculate_text_hash, create_chunk_id


def _parent() -> Document:
    content = (
        "所属章节：网络模型\n"
        "图片检索摘要：节点7与节点11之间标有0.232和0.992。\n"
        "图题：图2 网络拓扑\n"
        "图片内容（机器生成，需结合原图核验）：这里是完整的长图片说明。"
    )
    source = "论文.pdf"
    file_hash = "a" * 64
    chunk_id = create_chunk_id(source, file_hash, 3, 0, content)
    return Document(
        page_content=content,
        metadata={
            "source": source,
            "file_hash": file_hash,
            "page": 3,
            "pdf_page": 4,
            "chunk_index": 0,
            "total_chunks": 1,
            "content_hash": calculate_text_hash(content),
            "chunk_id": chunk_id,
            "content_type": "image_summary",
            "section": "网络模型",
            "preserve_as_unit": True,
        },
    )


def test_builds_short_child_without_changing_parent_id() -> None:
    parent = _parent()
    original_id = parent.metadata["chunk_id"]

    expanded = expand_image_parent_child_documents([parent])

    assert len(expanded) == 2
    stored_parent, child = expanded
    assert stored_parent.metadata["chunk_id"] == original_id
    assert stored_parent.metadata["retrieval_role"] == "parent"
    assert child.metadata["content_type"] == IMAGE_CHILD_CONTENT_TYPE
    assert child.metadata["parent_chunk_id"] == original_id
    assert child.metadata["retrieval_role"] == "child"
    assert "0.232" in child.page_content
    assert "完整的长图片说明" not in child.page_content
    assert all(document.metadata["total_chunks"] == 2 for document in expanded)


def test_expansion_is_idempotent() -> None:
    once = expand_image_parent_child_documents([_parent()])
    twice = expand_image_parent_child_documents(once)

    assert len(twice) == len(once)
    assert [item.metadata["chunk_id"] for item in twice] == [
        item.metadata["chunk_id"] for item in once
    ]


def test_child_ids_are_stable_across_parser_and_chroma_order() -> None:
    # 父块 ID 作为不透明的持久化标识；刻意让其顺序不同于原文序号。
    parents = []
    for index, chunk_id in enumerate(("f" * 64, "a" * 64, "c" * 64)):
        parent = _parent()
        parent.metadata.update(
            chunk_id=chunk_id, chunk_index=index, total_chunks=3
        )
        parents.append(parent)

    def child_identity(documents):
        return {
            item.metadata["parent_chunk_id"]: (
                item.metadata["chunk_id"], item.metadata["chunk_index"]
            )
            for item in expand_image_parent_child_documents(documents)
            if item.metadata["content_type"] == IMAGE_CHILD_CONTENT_TYPE
        }

    natural_order = child_identity(parents)
    chroma_order = child_identity(
        sorted(parents, key=lambda item: item.metadata["chunk_id"])
    )
    shuffled_order = child_identity([parents[2], parents[0], parents[1]])

    assert natural_order == chroma_order == shuffled_order
    assert natural_order["a" * 64][1] == 3
    assert natural_order["c" * 64][1] == 4
    assert natural_order["f" * 64][1] == 5
    assert [item.metadata["chunk_index"] for item in parents] == [0, 1, 2]


def test_existing_child_restores_parent_link_without_new_id() -> None:
    old_parent = _parent()
    stored_child = expand_image_parent_child_documents([old_parent])[1]

    expanded = expand_image_parent_child_documents([stored_child, old_parent])
    stored_parent = next(
        item for item in expanded if item.metadata["content_type"] == "image_summary"
    )

    assert len(expanded) == 2
    assert stored_parent.metadata["chunk_id"] == old_parent.metadata["chunk_id"]
    assert stored_parent.metadata["retrieval_role"] == "parent"
    assert stored_parent.metadata["retrieval_child_id"] == stored_child.metadata["chunk_id"]
    assert all(item.metadata["total_chunks"] == 2 for item in expanded)
    assert "retrieval_child_id" not in old_parent.metadata
    assert expand_image_parent_child_documents(expanded) == expanded
