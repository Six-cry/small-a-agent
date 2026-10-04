"""验证隔离精排层只重排已通过证据门槛的结果，并能安全回退。"""

from langchain_core.documents import Document

from aa_my_agent.rag.hybrid_retriever import (
    HybridRetrievalResult,
    HybridSearchHit,
)
from aa_my_agent.rag.reranked_retriever import RerankedRetriever
from aa_my_agent.rag.reranked_retriever import create_default_rag_retriever


def _hit(chunk_id: str, score: float, accepted: bool = True) -> HybridSearchHit:
    return HybridSearchHit(
        document=Document(
            page_content=chunk_id,
            metadata={"chunk_id": chunk_id, "source": "sample.pdf"},
        ),
        rrf_score=score,
        accepted=accepted,
        acceptance_reason="dense-distance" if accepted else "insufficient-evidence",
        dense_distance=0.2 if accepted else 1.2,
    )


class _Scorer:
    def __init__(self, scores):
        self.scores = scores

    def score(self, query, documents):
        assert query == "目标问题"
        assert len(documents) == len(self.scores)
        return list(self.scores)


class _BrokenScorer:
    def score(self, query, documents):
        raise RuntimeError("模型不可用")


class _BaseRetriever:
    def __init__(self, result):
        self.result = result
        self.requested_k = None

    def retrieve(self, query, final_k):
        self.requested_k = final_k
        return self.result


def test_reranker_reorders_accepted_candidates_and_preserves_evidence_gate():
    first = _hit("a" * 64, 0.03)
    second = _hit("b" * 64, 0.02)
    rejected = _hit("c" * 64, 0.01, accepted=False)
    result = HybridRetrievalResult(
        mode="hybrid",
        candidates=(first, second, rejected),
        hits=(first, second),
    )
    base = _BaseRetriever(result)
    reranked = RerankedRetriever(base, _Scorer([0.1, 0.9]), candidate_k=20)

    output = reranked.retrieve("目标问题", final_k=1)

    assert base.requested_k == 20
    assert output.mode == "hybrid-reranked"
    assert output.hits[0].document.metadata["chunk_id"] == "b" * 64
    assert output.hits[0].rerank_score == 0.9
    assert all(hit.document.metadata["chunk_id"] != "c" * 64 for hit in output.hits)


def test_reranker_failure_returns_original_result_without_rescuing_rejected_hits():
    first = _hit("a" * 64, 0.03)
    result = HybridRetrievalResult(mode="hybrid", candidates=(first,), hits=(first,))
    base = _BaseRetriever(result)
    reranked = RerankedRetriever(base, _BrokenScorer())

    output = reranked.retrieve("目标问题", final_k=1)

    assert output.hits == (first,)
    assert output.mode == "hybrid"
    assert output.reranker_error == "RuntimeError: 模型不可用"


def test_default_factory_can_disable_reranker_without_loading_model(monkeypatch):
    import aa_my_agent.config as config
    from aa_my_agent.rag.hybrid_retriever import HybridRetriever

    monkeypatch.setattr(config, "RAG_ENABLE_RERANKER", False)

    output = create_default_rag_retriever(vector_store=object())

    assert isinstance(output, HybridRetriever)


def test_default_factory_enables_cached_reranker_without_loading_weights(monkeypatch):
    import aa_my_agent.config as config

    monkeypatch.setattr(config, "RAG_ENABLE_RERANKER", True)
    monkeypatch.setattr(config, "RAG_RERANKER_MODEL", "local-test-model")
    monkeypatch.setattr(config, "RAG_RERANKER_DEVICE", "")
    monkeypatch.setattr(config, "RAG_RERANKER_CANDIDATE_K", 10)

    output = create_default_rag_retriever(vector_store=object())

    assert isinstance(output, RerankedRetriever)
    assert output.candidate_k == 10
    assert output.reranker.model_name_or_path == "local-test-model"
    assert output.reranker._model is None
