"""RAG 可插拔精排检索器。

本文件把现有 HybridRetriever 的 RRF 结果交给可插拔精排器重新排序。
精排只处理已经通过原有证据门槛的候选，失败时保留原 RRF 结果，
既可用于隔离评测，也可由正式 RagService 按配置启用。
"""

from __future__ import annotations

import math
from dataclasses import replace
from typing import Sequence

from langchain_core.documents import Document

from .hybrid_retriever import (
    HybridRetrievalResult,
    HybridRetriever,
    HybridSearchHit,
)
from .reranker import CrossEncoderReranker, DocumentReranker


class RerankedRetriever:
    """在混合召回结果上增加安全的本地精排层。"""

    def __init__(
        self,
        base_retriever: HybridRetriever,
        reranker: DocumentReranker,
        *,
        candidate_k: int = 20,
    ) -> None:
        if isinstance(candidate_k, bool) or not isinstance(candidate_k, int) or candidate_k <= 0:
            raise ValueError("candidate_k 必须是正整数")
        self.base_retriever = base_retriever
        self.reranker = reranker
        self.candidate_k = candidate_k

    @staticmethod
    def _sort_key(hit: HybridSearchHit) -> tuple[float, float, float, int, str]:
        return (
            -(hit.rerank_score if hit.rerank_score is not None else -math.inf),
            -hit.rrf_score,
            hit.dense_distance if hit.dense_distance is not None else math.inf,
            hit.lexical_rank if hit.lexical_rank is not None else math.inf,
            str(hit.document.metadata.get("chunk_id", "")),
        )

    def rerank_result(
        self,
        query: str,
        retrieval: HybridRetrievalResult,
        *,
        final_k: int,
    ) -> HybridRetrievalResult:
        """对一份已经完成 RRF 和证据过滤的结果做精排。"""
        if not isinstance(query, str) or not query.strip():
            raise ValueError("query 不能为空")
        if isinstance(final_k, bool) or not isinstance(final_k, int) or final_k <= 0:
            raise ValueError("final_k 必须是正整数")

        accepted = list(retrieval.hits)
        if not accepted:
            return retrieval

        documents: Sequence[Document] = [hit.document for hit in accepted]
        try:
            scores = self.reranker.score(query.strip(), documents)
            if len(scores) != len(accepted):
                raise ValueError("精排分数数量与候选数量不一致")
            reranked = [
                replace(hit, rerank_score=float(score))
                for hit, score in zip(accepted, scores, strict=True)
            ]
            reranked.sort(key=self._sort_key)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            return replace(retrieval, reranker_error=error)

        reranked_ids = {
            str(hit.document.metadata.get("chunk_id", "")) for hit in reranked
        }
        untouched = [
            hit
            for hit in retrieval.candidates
            if str(hit.document.metadata.get("chunk_id", "")) not in reranked_ids
        ]
        ordered_candidates = tuple(reranked + untouched)
        return replace(
            retrieval,
            mode=f"{retrieval.mode}-reranked",
            candidates=ordered_candidates,
            hits=tuple(reranked[:final_k]),
            reranker_error=None,
        )

    def retrieve(self, query: str, final_k: int) -> HybridRetrievalResult:
        """先扩大混合召回候选，再对通过证据门槛的候选精排。"""
        if isinstance(final_k, bool) or not isinstance(final_k, int) or final_k <= 0:
            raise ValueError("final_k 必须是正整数")
        retrieval = self.base_retriever.retrieve(
            query=query,
            final_k=max(final_k, self.candidate_k),
        )
        return self.rerank_result(query, retrieval, final_k=final_k)


def create_default_rag_retriever(
    vector_store=None,
    *,
    max_distance: float | None = None,
) -> HybridRetriever | RerankedRetriever:
    """按正式配置创建 RRF 检索器，并在开启时包裹本地精排层。"""
    from ..config import (
        RAG_ENABLE_RERANKER,
        RAG_RERANKER_CANDIDATE_K,
        RAG_RERANKER_DEVICE,
        RAG_RERANKER_MODEL,
    )
    from .hybrid_retriever import create_default_hybrid_retriever

    base = create_default_hybrid_retriever(
        vector_store=vector_store,
        max_distance=max_distance,
    )
    if not RAG_ENABLE_RERANKER:
        return base
    if not RAG_RERANKER_MODEL:
        raise ValueError("启用 Reranker 时 RAG_RERANKER_MODEL 不能为空")
    return RerankedRetriever(
        base,
        CrossEncoderReranker(
            RAG_RERANKER_MODEL,
            device=RAG_RERANKER_DEVICE or None,
        ),
        candidate_k=RAG_RERANKER_CANDIDATE_K,
    )
