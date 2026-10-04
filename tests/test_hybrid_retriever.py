"""验证 Chroma 与 BM25 双路召回、RRF 去重和安全回退。"""

from langchain_core.documents import Document

from aa_my_agent.rag.hybrid_retriever import HybridRetriever
from aa_my_agent.rag.lexical_index import BM25Index


def _document(chunk_id: str, content: str) -> Document:
    return Document(
        page_content=content,
        metadata={"chunk_id": chunk_id, "source": "sample.pdf", "page": 0},
    )


class _Store:
    collection_name = "test"

    def __init__(self, documents, dense_results):
        self.documents = documents
        self.dense_results = dense_results

    def get_chunk_ids(self):
        return tuple(document.metadata["chunk_id"] for document in self.documents)

    def get_all_documents(self):
        return list(self.documents)

    def search_with_scores(self, query, k):
        return list(self.dense_results[:k])

    def get_documents_by_ids(self, chunk_ids):
        requested = set(chunk_ids)
        return {
            document.metadata["chunk_id"]: document
            for document in self.documents
            if document.metadata["chunk_id"] in requested
        }


def test_rrf_promotes_result_supported_by_both_channels(tmp_path) -> None:
    semantic_only = _document("a" * 64, "网络资源动态调度概述")
    exact = _document(
        "b" * 64,
        "图3 SDN控制平面 虚拟队列积压 历史决策经验",
    )
    store = _Store(
        [semantic_only, exact],
        [(semantic_only, 0.40), (exact, 0.95)],
    )
    retriever = HybridRetriever(
        vector_store=store,  # type: ignore[arg-type]
        lexical_index=BM25Index(tmp_path / "bm25.json", "test"),
        dense_candidate_k=20,
        lexical_candidate_k=20,
        lexical_min_query_coverage=0.20,
        lexical_min_matched_terms=2,
    )

    result = retriever.retrieve("图3中SDN控制平面包含什么", final_k=2)

    assert result.mode == "hybrid"
    assert result.hits[0].document.metadata["chunk_id"] == "b" * 64
    assert result.hits[0].acceptance_reason == "bm25-evidence"
    assert len(
        {
            hit.document.metadata["chunk_id"]
            for hit in result.candidates
        }
    ) == len(result.candidates)


def test_weak_keyword_overlap_does_not_bypass_distance_gate(tmp_path) -> None:
    document = _document("a" * 64, "算法步骤与仿真参数说明")
    store = _Store([document], [(document, 1.30)])
    retriever = HybridRetriever(
        vector_store=store,  # type: ignore[arg-type]
        lexical_index=BM25Index(tmp_path / "bm25.json", "test"),
        lexical_min_query_coverage=0.30,
        lexical_min_matched_terms=3,
    )

    result = retriever.retrieve(
        "家用空调制冷剂不足时应该按照什么步骤检修和加注",
        final_k=3,
    )

    assert result.hits == ()


def test_bm25_failure_falls_back_to_dense_results() -> None:
    document = _document("a" * 64, "可靠的语义结果")
    store = _Store([document], [(document, 0.40)])

    class BrokenIndex:
        def ensure_current(self, vector_store):
            raise RuntimeError("broken")

    retriever = HybridRetriever(
        vector_store=store,  # type: ignore[arg-type]
        lexical_index=BrokenIndex(),  # type: ignore[arg-type]
    )

    result = retriever.retrieve("可靠结果", final_k=1)

    assert result.mode == "dense-fallback"
    assert result.lexical_error == "RuntimeError: broken"
    assert result.hits[0].document.metadata["chunk_id"] == "a" * 64


def test_image_child_hit_returns_full_parent_and_collapses_duplicate(tmp_path) -> None:
    parent = _document("a" * 64, "完整图片说明以及前后正文")
    parent.metadata["content_type"] = "image_summary"
    child = _document("b" * 64, "图2 节点7 节点11 0.232 0.992")
    child.metadata.update(
        {
            "content_type": "image_retrieval",
            "parent_chunk_id": "a" * 64,
            "retrieval_role": "child",
        }
    )
    store = _Store(
        [parent, child],
        [(child, 0.20), (parent, 0.40)],
    )
    retriever = HybridRetriever(
        vector_store=store,  # type: ignore[arg-type]
        lexical_index=BM25Index(tmp_path / "bm25.json", "test"),
    )

    result = retriever.retrieve("节点7到节点11的链路标注", final_k=5)

    assert len(result.hits) == 1
    assert result.hits[0].document.page_content == "完整图片说明以及前后正文"
    assert result.hits[0].document.metadata["chunk_id"] == "a" * 64
    assert result.hits[0].document.metadata["retrieved_via_role"] == "child"
    assert result.hits[0].document.metadata["retrieved_via_chunk_id"] == "b" * 64


def test_image_parent_keeps_child_bm25_evidence_after_collapse(tmp_path) -> None:
    parent = _document("a" * 64, "完整图片说明以及前后正文")
    parent.metadata["content_type"] = "image_summary"
    child = _document(
        "b" * 64,
        "图3 SDN控制平面 虚拟队列积压 历史决策经验",
    )
    child.metadata.update(
        {
            "content_type": "image_retrieval",
            "parent_chunk_id": "a" * 64,
            "retrieval_role": "child",
        }
    )
    store = _Store(
        [parent, child],
        [(parent, 0.95), (child, 1.10)],
    )
    retriever = HybridRetriever(
        vector_store=store,  # type: ignore[arg-type]
        lexical_index=BM25Index(tmp_path / "bm25.json", "test"),
        lexical_min_query_coverage=0.15,
        lexical_min_matched_terms=3,
    )

    result = retriever.retrieve(
        "图3中SDN控制平面包含虚拟队列积压和历史决策经验吗",
        final_k=5,
    )

    assert len(result.hits) == 1
    assert result.hits[0].document.metadata["chunk_id"] == "a" * 64
    assert result.hits[0].lexical_rank == 1
    assert result.hits[0].acceptance_reason == "bm25-evidence"
