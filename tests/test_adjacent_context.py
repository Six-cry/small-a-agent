"""验证邻接 Chunk 只补充精排后的核心证据，并受去重与预算约束。"""

from langchain_core.documents import Document

from aa_my_agent.rag.adjacent_context import AdjacentChunkExpander
from aa_my_agent.rag.hybrid_retriever import HybridSearchHit


def _document(index: int, content: str, *, content_type: str = "text") -> Document:
    return Document(
        page_content=content,
        metadata={
            "source": "sample.pdf",
            "page": 0 if index < 2 else 1,
            "chunk_index": index,
            "chunk_id": f"{index:064x}",
            "content_type": content_type,
        },
    )


def _hit(document: Document) -> HybridSearchHit:
    return HybridSearchHit(
        document=document,
        rrf_score=0.03,
        accepted=True,
        acceptance_reason="dense-distance",
        dense_distance=0.2,
    )


class _Store:
    def __init__(self, documents):
        self.documents = list(documents)
        self.read_count = 0

    def get_chunk_ids(self):
        return tuple(document.metadata["chunk_id"] for document in self.documents)

    def get_all_documents(self):
        self.read_count += 1
        return list(self.documents)


def test_expands_previous_and_next_primary_chunks_but_skips_image_child():
    documents = [
        _document(0, "前文说明"),
        _document(1, "核心公式"),
        _document(2, "变量解释"),
        _document(3, "图片检索卡片", content_type="image_retrieval"),
    ]
    store = _Store(documents)
    expander = AdjacentChunkExpander(store, window_size=1, max_total_chars=100)

    output = expander.expand([_hit(documents[1])])

    assert [item.offset for item in output[0].adjacent_chunks] == [-1, 1]
    assert [item.document.page_content for item in output[0].adjacent_chunks] == [
        "前文说明",
        "变量解释",
    ]
    assert store.read_count == 1


def test_does_not_repeat_another_core_hit_as_adjacent_context():
    documents = [_document(0, "甲"), _document(1, "乙"), _document(2, "丙")]
    expander = AdjacentChunkExpander(_Store(documents), max_total_chars=100)

    output = expander.expand([_hit(documents[1]), _hit(documents[2])])

    assert [item.document.page_content for item in output[0].adjacent_chunks] == ["甲"]
    assert output[1].adjacent_chunks == ()


def test_respects_global_character_budget_without_cutting_a_chunk():
    documents = [_document(0, "前文很长"), _document(1, "核心"), _document(2, "后文")]
    expander = AdjacentChunkExpander(_Store(documents), max_total_chars=1)

    output = expander.expand([_hit(documents[1])])

    assert output[0].adjacent_chunks == ()


def test_rebuilds_cached_index_when_collection_count_changes():
    documents = [_document(0, "甲"), _document(1, "乙")]
    store = _Store(documents)
    expander = AdjacentChunkExpander(store, max_total_chars=100)
    expander.expand([_hit(documents[0])])
    store.documents.append(_document(2, "丙"))

    output = expander.expand([_hit(store.documents[1])])

    assert store.read_count == 2
    assert [item.document.page_content for item in output[0].adjacent_chunks] == [
        "甲",
        "丙",
    ]


def test_rebuilds_cached_index_when_ids_change_but_count_does_not():
    documents = [_document(0, "甲"), _document(1, "乙"), _document(2, "旧丙")]
    store = _Store(documents)
    expander = AdjacentChunkExpander(store, max_total_chars=100)
    expander.expand([_hit(documents[1])])
    replacement = _document(2, "新丙")
    replacement.metadata["chunk_id"] = "f" * 64
    store.documents[2] = replacement

    output = expander.expand([_hit(store.documents[1])])

    assert store.read_count == 2
    assert output[0].adjacent_chunks[1].document.page_content == "新丙"
