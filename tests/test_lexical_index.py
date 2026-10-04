"""验证 BM25 中文技术词分词、持久化和精确词项召回。"""

from langchain_core.documents import Document

from aa_my_agent.rag.lexical_index import BM25Index, tokenize_for_bm25


def _document(chunk_id: str, content: str) -> Document:
    return Document(
        page_content=content,
        metadata={"chunk_id": chunk_id, "source": "sample.pdf", "page": 0},
    )


def test_tokenizer_preserves_technical_identifiers() -> None:
    tokens = tokenize_for_bm25("图3中SDN节点7的值为0.232，公式λ_ij。")

    assert "sdn" in tokens
    assert "节点" in tokens
    assert "7" in tokens
    assert "0.232" in tokens
    assert "λ_ij" in tokens


def test_bm25_search_and_reload(tmp_path) -> None:
    path = tmp_path / "bm25.json"
    documents = [
        _document("a" * 64, "普通网络资源调度说明"),
        _document(
            "b" * 64,
            "图3 SDN控制平面包含虚拟队列积压和历史决策经验",
        ),
    ]
    index = BM25Index(path, "test")
    index.rebuild(documents)

    first = index.search("图3中SDN控制平面有什么", k=2)
    assert first[0].document.metadata["chunk_id"] == "b" * 64
    assert first[0].score > 0
    assert first[0].query_coverage > 0

    reloaded = BM25Index(path, "test")

    class Store:
        collection_name = "test"

        def get_chunk_ids(self):
            return tuple(document.metadata["chunk_id"] for document in documents)

        def get_all_documents(self):
            raise AssertionError("兼容索引应直接从磁盘加载")

    assert reloaded.ensure_current(Store()) is False
    second = reloaded.search("历史决策经验", k=1)
    assert second[0].document.metadata["chunk_id"] == "b" * 64
