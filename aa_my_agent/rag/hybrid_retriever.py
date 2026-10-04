"""RAG 混合检索器。

这个文件同时执行 Chroma 稠密向量召回和 BM25 关键词召回，随后按
chunk_id 去重，并使用 RRF（倒数排名融合）生成统一排名。最终的证据
门槛既保留原向量距离保护，也允许关键词证据充分的结果得到补救。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from langchain_core.documents import Document

from .lexical_index import BM25Index, LexicalSearchHit, create_default_bm25_index
from .vector_store import VectorStoreService


@dataclass(frozen=True, slots=True)
class HybridSearchHit:
    """融合后的一条候选结果。"""

    document: Document
    rrf_score: float
    accepted: bool
    acceptance_reason: str
    dense_rank: int | None = None
    dense_distance: float | None = None
    lexical_rank: int | None = None
    lexical_score: float | None = None
    lexical_query_coverage: float = 0.0
    lexical_matched_terms: tuple[str, ...] = ()
    # 只有隔离精排开启时才有值；默认 RRF 检索保持 None。
    rerank_score: float | None = None


@dataclass(frozen=True, slots=True)
class HybridRetrievalResult:
    """一次混合检索的完整结果，包含候选和最终命中。"""

    mode: str
    candidates: tuple[HybridSearchHit, ...]
    hits: tuple[HybridSearchHit, ...]
    lexical_error: str | None = None
    reranker_error: str | None = None


@dataclass(slots=True)
class _Candidate:
    document: Document
    dense_rank: int | None = None
    dense_distance: float | None = None
    lexical_rank: int | None = None
    lexical_score: float | None = None
    lexical_query_coverage: float = 0.0
    lexical_matched_terms: tuple[str, ...] = ()


def _chunk_id(document: Document) -> str:
    value = document.metadata.get("chunk_id")
    if not isinstance(value, str) or not value.strip():
        raise ValueError("混合检索候选缺少有效 chunk_id")
    return value.strip()


class HybridRetriever:
    """执行双路召回、RRF 融合、去重和可靠性过滤。"""

    def __init__(
        self,
        vector_store: VectorStoreService,
        lexical_index: BM25Index,
        *,
        mode: str = "hybrid",
        max_distance: float = 0.90,
        dense_candidate_k: int = 20,
        lexical_candidate_k: int = 20,
        rrf_k: int = 60,
        lexical_rescue_max_rank: int = 5,
        lexical_min_query_coverage: float = 0.14,
        lexical_min_matched_terms: int = 3,
    ) -> None:
        normalized_mode = mode.strip().casefold()
        if normalized_mode not in {"dense", "hybrid"}:
            raise ValueError("RAG 检索模式只能是 dense 或 hybrid")
        for name, value in {
            "dense_candidate_k": dense_candidate_k,
            "lexical_candidate_k": lexical_candidate_k,
            "rrf_k": rrf_k,
            "lexical_rescue_max_rank": lexical_rescue_max_rank,
            "lexical_min_matched_terms": lexical_min_matched_terms,
        }.items():
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} 必须是正整数")
        if not math.isfinite(max_distance) or max_distance < 0:
            raise ValueError("max_distance 必须是非负有限数字")
        if (
            not math.isfinite(lexical_min_query_coverage)
            or not 0 <= lexical_min_query_coverage <= 1
        ):
            raise ValueError("lexical_min_query_coverage 必须位于 0 到 1")

        self.vector_store = vector_store
        self.lexical_index = lexical_index
        self.mode = normalized_mode
        self.max_distance = float(max_distance)
        self.dense_candidate_k = dense_candidate_k
        self.lexical_candidate_k = lexical_candidate_k
        self.rrf_k = rrf_k
        self.lexical_rescue_max_rank = lexical_rescue_max_rank
        self.lexical_min_query_coverage = lexical_min_query_coverage
        self.lexical_min_matched_terms = lexical_min_matched_terms

    def _lexical_is_strong(self, candidate: _Candidate) -> bool:
        return (
            candidate.lexical_rank is not None
            and candidate.lexical_rank <= self.lexical_rescue_max_rank
            and candidate.lexical_query_coverage
            >= self.lexical_min_query_coverage
            and len(candidate.lexical_matched_terms)
            >= self.lexical_min_matched_terms
        )

    def _to_hit(self, candidate: _Candidate) -> HybridSearchHit:
        score = 0.0
        if candidate.dense_rank is not None:
            score += 1.0 / (self.rrf_k + candidate.dense_rank)
        if candidate.lexical_rank is not None:
            score += 1.0 / (self.rrf_k + candidate.lexical_rank)

        dense_accepted = (
            candidate.dense_distance is not None
            and candidate.dense_distance <= self.max_distance
        )
        lexical_accepted = self._lexical_is_strong(candidate)
        if dense_accepted and lexical_accepted:
            reason = "dense-and-bm25"
        elif dense_accepted:
            reason = "dense-distance"
        elif lexical_accepted:
            reason = "bm25-evidence"
        else:
            reason = "insufficient-evidence"

        return HybridSearchHit(
            document=candidate.document,
            rrf_score=score,
            accepted=dense_accepted or lexical_accepted,
            acceptance_reason=reason,
            dense_rank=candidate.dense_rank,
            dense_distance=candidate.dense_distance,
            lexical_rank=candidate.lexical_rank,
            lexical_score=candidate.lexical_score,
            lexical_query_coverage=candidate.lexical_query_coverage,
            lexical_matched_terms=candidate.lexical_matched_terms,
        )

    def _collapse_image_parent_candidates(
        self,
        candidates: dict[str, _Candidate],
    ) -> dict[str, _Candidate]:
        """按图片父块合并子/父候选，再统一计算融合分数和证据门槛。"""
        parent_ids = {
            str(candidate.document.metadata.get("parent_chunk_id", "")).strip()
            for candidate in candidates.values()
            if str(
                candidate.document.metadata.get("parent_chunk_id", "")
            ).strip()
        }
        if not parent_ids:
            return candidates

        parent_documents = self.vector_store.get_documents_by_ids(parent_ids)
        collapsed: dict[str, _Candidate] = {}
        child_ids_by_parent: dict[str, list[str]] = {}
        for candidate in candidates.values():
            child_id = _chunk_id(candidate.document)
            parent_id = str(
                candidate.document.metadata.get("parent_chunk_id", "")
            ).strip()
            effective_id = parent_id or child_id
            document = candidate.document
            if parent_id and parent_id in parent_documents:
                parent = parent_documents[parent_id]
                metadata = dict(parent.metadata)
                child_ids_by_parent.setdefault(parent_id, []).append(child_id)
                metadata["retrieved_via_role"] = "child"
                metadata["retrieved_via_chunk_id"] = child_id
                document = Document(
                    page_content=parent.page_content,
                    metadata=metadata,
                )

            existing = collapsed.get(effective_id)
            if existing is None:
                collapsed[effective_id] = _Candidate(
                    document=document,
                    dense_rank=candidate.dense_rank,
                    dense_distance=candidate.dense_distance,
                    lexical_rank=candidate.lexical_rank,
                    lexical_score=candidate.lexical_score,
                    lexical_query_coverage=candidate.lexical_query_coverage,
                    lexical_matched_terms=candidate.lexical_matched_terms,
                )
                continue

            # 同一图片的父块和短子块是同一语义单元：每个召回通道只取
            # 其中最强的一次排名，避免重复加分，同时不能丢掉子块证据。
            if (
                candidate.dense_rank is not None
                and (
                    existing.dense_rank is None
                    or candidate.dense_rank < existing.dense_rank
                )
            ):
                existing.dense_rank = candidate.dense_rank
                existing.dense_distance = candidate.dense_distance
            if (
                candidate.lexical_rank is not None
                and (
                    existing.lexical_rank is None
                    or candidate.lexical_rank < existing.lexical_rank
                )
            ):
                existing.lexical_rank = candidate.lexical_rank
            if candidate.lexical_score is not None:
                existing.lexical_score = max(
                    existing.lexical_score or 0.0,
                    candidate.lexical_score,
                )
            existing.lexical_query_coverage = max(
                existing.lexical_query_coverage,
                candidate.lexical_query_coverage,
            )
            existing.lexical_matched_terms = tuple(
                sorted(
                    set(existing.lexical_matched_terms)
                    | set(candidate.lexical_matched_terms)
                )
            )
            if parent_id and parent_id in parent_documents:
                existing.document = document

        for parent_id, child_ids in child_ids_by_parent.items():
            resolved = collapsed.get(parent_id)
            if resolved is None:
                continue
            metadata = dict(resolved.document.metadata)
            metadata["retrieved_via_role"] = "child"
            metadata["retrieved_via_chunk_id"] = ",".join(sorted(set(child_ids)))
            resolved.document = Document(
                page_content=resolved.document.page_content,
                metadata=metadata,
            )
        return collapsed

    @staticmethod
    def _merge_lexical_hit(
        candidates: dict[str, _Candidate],
        lexical_hit: LexicalSearchHit,
    ) -> None:
        chunk_id = _chunk_id(lexical_hit.document)
        candidate = candidates.get(chunk_id)
        if candidate is None:
            candidate = _Candidate(document=lexical_hit.document)
            candidates[chunk_id] = candidate
        candidate.lexical_rank = lexical_hit.rank
        candidate.lexical_score = lexical_hit.score
        candidate.lexical_query_coverage = lexical_hit.query_coverage
        candidate.lexical_matched_terms = lexical_hit.matched_terms

    def retrieve(self, query: str, final_k: int) -> HybridRetrievalResult:
        """获取候选并返回通过门槛的最终 Top K。"""
        if not isinstance(query, str) or not query.strip():
            raise ValueError("query 不能为空")
        if isinstance(final_k, bool) or not isinstance(final_k, int) or final_k <= 0:
            raise ValueError("final_k 必须是正整数")

        normalized_query = query.strip()
        dense_results = self.vector_store.search_with_scores(
            query=normalized_query,
            k=max(final_k, self.dense_candidate_k),
        )
        candidates: dict[str, _Candidate] = {}
        for rank, (document, distance) in enumerate(dense_results, start=1):
            chunk_id = _chunk_id(document)
            candidates[chunk_id] = _Candidate(
                document=document,
                dense_rank=rank,
                dense_distance=float(distance),
            )

        actual_mode = self.mode
        lexical_error: str | None = None
        if self.mode == "hybrid":
            try:
                self.lexical_index.ensure_current(self.vector_store)
                lexical_results = self.lexical_index.search(
                    normalized_query,
                    k=self.lexical_candidate_k,
                )
                for lexical_hit in lexical_results:
                    self._merge_lexical_hit(candidates, lexical_hit)
            except Exception as exc:
                actual_mode = "dense-fallback"
                lexical_error = f"{type(exc).__name__}: {exc}"

        candidates = self._collapse_image_parent_candidates(candidates)
        hits = [self._to_hit(candidate) for candidate in candidates.values()]
        hits.sort(
            key=lambda hit: (
                -hit.rrf_score,
                hit.dense_distance
                if hit.dense_distance is not None
                else math.inf,
                hit.lexical_rank
                if hit.lexical_rank is not None
                else math.inf,
                _chunk_id(hit.document),
            )
        )
        accepted = tuple(hit for hit in hits if hit.accepted)[:final_k]
        return HybridRetrievalResult(
            mode=actual_mode,
            candidates=tuple(hits),
            hits=accepted,
            lexical_error=lexical_error,
        )


def create_default_hybrid_retriever(
    vector_store: VectorStoreService | None = None,
    *,
    max_distance: float | None = None,
) -> HybridRetriever:
    """根据正式配置创建混合检索器。"""
    from ..config import (
        RAG_BM25_CANDIDATE_K,
        RAG_BM25_MIN_MATCHED_TERMS,
        RAG_BM25_MIN_QUERY_COVERAGE,
        RAG_BM25_RESCUE_MAX_RANK,
        RAG_DENSE_CANDIDATE_K,
        RAG_MAX_DISTANCE,
        RAG_RETRIEVAL_MODE,
        RAG_RRF_K,
    )

    store = vector_store or VectorStoreService()
    effective_max_distance = (
        RAG_MAX_DISTANCE if max_distance is None else max_distance
    )
    return HybridRetriever(
        vector_store=store,
        lexical_index=create_default_bm25_index(),
        mode=RAG_RETRIEVAL_MODE,
        max_distance=effective_max_distance,
        dense_candidate_k=RAG_DENSE_CANDIDATE_K,
        lexical_candidate_k=RAG_BM25_CANDIDATE_K,
        rrf_k=RAG_RRF_K,
        lexical_rescue_max_rank=RAG_BM25_RESCUE_MAX_RANK,
        lexical_min_query_coverage=RAG_BM25_MIN_QUERY_COVERAGE,
        lexical_min_matched_terms=RAG_BM25_MIN_MATCHED_TERMS,
    )
