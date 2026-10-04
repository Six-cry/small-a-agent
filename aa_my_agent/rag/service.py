"""RAG 对外检索服务层。

主要职责：

1. 为 Agent 提供统一的知识检索接口。
2. 延迟创建并缓存混合检索器与可选本地 Reranker。
3. 同时执行 Chroma 向量召回和 BM25 关键词召回。
4. 使用 RRF 融合、按 chunk_id 去重并过滤证据不足的结果。
5. 对通过证据门槛的候选执行可选 Cross-Encoder 精排。
6. 为精排后的核心证据补充同一文件的相邻 Chunk。
7. 限制最终返回给 Agent 的核心 Chunk 数量与邻接字符预算。
8. 整理来源、页码、Chunk ID、融合分数、精排分数和内容。
9. BM25、Reranker 或邻接扩展异常时安全回退。

调用链：

Agent
→ search_knowledge
→ RagService.search
→ HybridRetriever.retrieve
→ Chroma + BM25 + RRF + 可选 Reranker
→ 邻接 Chunk 补全 + 可追溯引用
"""

import math
from pathlib import Path
from time import perf_counter

from .index_status import check_rag_index_status
from ..config import (
    RAG_MAX_DISTANCE,
    RAG_TOP_K,
)
from .vector_store import (
    VectorStoreService,
)
from .hybrid_retriever import (
    HybridRetriever,
)
from .reranked_retriever import (
    RerankedRetriever,
    create_default_rag_retriever,
)
from ..telemetry import emit
from .adjacent_context import (
    AdjacentChunkExpander,
    ExpandedHitContext,
    create_default_adjacent_chunk_expander,
)


class RagService:
    """面向 Agent 的知识库检索服务。"""

    def __init__(
        self,
        max_distance: float = (
            RAG_MAX_DISTANCE
        ),
    ):
        if (
            isinstance(max_distance, bool)
            or not isinstance(
                max_distance,
                (int, float),
            )
        ):
            raise ValueError(
                "max_distance 必须是数字"
            )

        normalized_distance = float(
            max_distance
        )

        if (
            not math.isfinite(
                normalized_distance
            )
            or normalized_distance < 0
        ):
            raise ValueError(
                "max_distance 必须是大于或等于 "
                "0 的有限数字"
            )

        self.max_distance = (
            normalized_distance
        )

        self._vector_store: (
            VectorStoreService | None
        ) = None
        self._retriever: HybridRetriever | RerankedRetriever | None = None
        self._context_expander: AdjacentChunkExpander | None = None

    def _get_vector_store(
        self,
    ) -> VectorStoreService:
        """延迟创建并缓存向量库服务。"""
        if self._vector_store is None:
            self._vector_store = (
                VectorStoreService()
            )

        return self._vector_store

    def _get_retriever(self) -> HybridRetriever | RerankedRetriever:
        """延迟创建并缓存混合检索器及本地精排模型。"""
        if self._retriever is None:
            self._retriever = create_default_rag_retriever(
                vector_store=self._get_vector_store(),
                max_distance=self.max_distance,
            )
        return self._retriever

    def _get_context_expander(self) -> AdjacentChunkExpander | None:
        """按配置延迟创建并缓存邻接 Chunk 索引。"""
        from ..config import RAG_ENABLE_ADJACENT_CONTEXT

        if not RAG_ENABLE_ADJACENT_CONTEXT:
            return None
        if self._context_expander is None:
            self._context_expander = create_default_adjacent_chunk_expander(
                self._get_vector_store()
            )
        return self._context_expander

    def _filter_results(
        self,
        scored_results: list,
    ) -> list:
        """按照距离阈值过滤检索结果。

        Chroma 距离越小，表示越相关。

        保留条件：

        distance <= max_distance
        """
        filtered_results = []

        for document, distance in (
            scored_results
        ):
            if distance <= self.max_distance:
                filtered_results.append(
                    (document, distance)
                )

        return filtered_results

    @staticmethod
    def _format_page(
        page,
    ) -> str:
        """将 Loader 的零基页码转换成人类页码。"""
        if (
            isinstance(page, int)
            and not isinstance(page, bool)
        ):
            return f"第 {page + 1} 页"

        return "无页码"

    @classmethod
    def _citation_label(cls, document) -> str:
        """生成可由 Agent 原样引用的稳定来源标识。"""
        source = Path(str(document.metadata.get("source", "未知来源"))).name
        page = cls._format_page(document.metadata.get("page"))
        chunk_id = str(document.metadata.get("chunk_id", "未知"))
        return f"[{source}，{page}，Chunk {chunk_id}]"

    def search(
        self,
        query: str,
        top_k: int = RAG_TOP_K,
    ) -> str:
        """搜索知识库并返回可靠参考资料。"""
        if (
            not isinstance(query, str)
            or not query.strip()
        ):
            raise ValueError(
                "query 不能为空"
            )

        if (
            isinstance(top_k, bool)
            or not isinstance(top_k, int)
            or top_k <= 0
        ):
            raise ValueError(
                "top_k 必须是正整数"
            )

        started = perf_counter()
        emit(
            "RAG", "SearchStart",
            query_chars=len(query.strip()), requested_top_k=top_k,
            max_distance=f"{self.max_distance:.6f}",
        )

        # 在线阶段只检查文件与 Manifest 是否一致，绝不解析、向量化或写库。
        # 真正同步只能由独立 sync_knowledge 命令显式执行。
        index_status = check_rag_index_status(
            output=lambda _message: None,
        )
        if index_status.status in {"stale", "failed"}:
            emit(
                "RAG", "IndexCheck",
                status=index_status.status,
                message=index_status.message,
            )
        index_notice = (
            f"知识库状态提醒：{index_status.message}"
            if index_status.status in {"stale", "failed"}
            else None
        )

        retrieval = self._get_retriever().retrieve(
            query=query.strip(),
            final_k=top_k,
        )

        if retrieval.lexical_error is not None:
            emit(
                "RAG",
                "LexicalFallback",
                error=retrieval.lexical_error,
            )
        if retrieval.reranker_error is not None:
            emit(
                "RAG",
                "RerankerFallback",
                error=retrieval.reranker_error,
            )

        if not retrieval.candidates:
            emit(
                "RAG", "SearchEnd", raw=0, accepted=0,
                reason="no-results",
                mode=retrieval.mode,
                latency=f"{perf_counter() - started:.2f}s",
            )
            message = "知识库当前没有可用的检索结果。"
            return (
                f"{index_notice}\n\n{message}"
                if index_notice is not None
                else message
            )

        if not retrieval.hits:
            dense_distances = [
                hit.dense_distance
                for hit in retrieval.candidates
                if hit.dense_distance is not None
            ]
            best_distance = min(dense_distances) if dense_distances else None

            emit(
                "RAG", "SearchEnd",
                raw=len(retrieval.candidates), accepted=0,
                reason="evidence-filter",
                best_distance=(
                    f"{best_distance:.6f}"
                    if best_distance is not None
                    else "none"
                ),
                mode=retrieval.mode,
                latency=f"{perf_counter() - started:.2f}s",
            )
            lines = []
            if index_notice is not None:
                lines.extend([index_notice, ""])
            lines.extend(
                [
                    "知识库中没有找到足够相关的内容。",
                    f"当前检索模式：{retrieval.mode}",
                ]
            )
            if best_distance is not None:
                lines.extend(
                    [
                        f"最接近结果的距离：{best_distance:.6f}",
                        f"当前最大允许距离：{self.max_distance:.6f}",
                    ]
                )
            lines.append("请不要根据无关检索结果推测答案。")
            return "\n".join(lines)

        context_error: str | None = None
        expander = self._get_context_expander()
        if expander is None:
            expanded_hits = tuple(
                ExpandedHitContext(hit=hit) for hit in retrieval.hits
            )
        else:
            try:
                expanded_hits = expander.expand(retrieval.hits)
            except Exception as exc:
                context_error = f"{type(exc).__name__}: {exc}"
                expanded_hits = tuple(
                    ExpandedHitContext(hit=hit) for hit in retrieval.hits
                )
                emit("RAG", "AdjacentContextFallback", error=context_error)

        adjacent_count = sum(
            len(item.adjacent_chunks) for item in expanded_hits
        )

        results = [
            (
                "知识库检索结果：\n"
                f"检索模式：{retrieval.mode}\n"
                f"通过证据门槛的结果：{len(retrieval.hits)} 个\n"
                f"补充的相邻 Chunk：{adjacent_count} 个\n"
                f"最大允许向量距离：{self.max_distance:.6f}\n"
                "引用规则：核心证据已经通过检索门槛；相邻 Chunk 只用于补全上下文，"
                "不能被单独视为命中。回答时请引用对应的来源、页码；需要审计时附 Chunk ID。"
            )
        ]
        if index_notice is not None:
            results.insert(0, index_notice)

        for index, expanded_hit in enumerate(
            expanded_hits,
            start=1,
        ):
            hit = expanded_hit.hit
            document = hit.document
            source = document.metadata.get(
                "source",
                "未知来源",
            )

            source_name = Path(
                str(source)
            ).name

            page_text = self._format_page(
                document.metadata.get(
                    "page"
                )
            )

            chunk_id = document.metadata.get(
                "chunk_id",
                "未知",
            )

            chunk_index = (
                document.metadata.get(
                    "chunk_index",
                    "未知",
                )
            )

            distance_text = (
                f"{hit.dense_distance:.6f}"
                if hit.dense_distance is not None
                else "未进入稠密候选"
            )
            lexical_score_text = (
                f"{hit.lexical_score:.6f}"
                if hit.lexical_score is not None
                else "未命中"
            )
            rerank_score_text = (
                f"{hit.rerank_score:.6f}"
                if hit.rerank_score is not None
                else "未启用或已回退"
            )
            image_path = str(document.metadata.get("image_path", "")).strip()
            image_verification_text = ""
            if (
                image_path
                and document.metadata.get("content_type") == "image_summary"
            ):
                image_verification_text = (
                    "\n原图按问题核验：可用；如问题涉及节点、连线、箭头、"
                    "数值或包含关系，可调用 inspect_knowledge_image，"
                    f"传入本条 Chunk ID（裁图：{image_path}）"
                )
            results.append(
                f"[参考资料 {index}]\n"
                f"引用标识：{self._citation_label(document)}\n"
                f"来源：{source_name}\n"
                f"位置：{page_text}\n"
                f"Chunk Index：{chunk_index}\n"
                f"Chunk ID：{chunk_id}\n"
                f"向量距离：{distance_text}\n"
                f"BM25 排名：{hit.lexical_rank or '未命中'}\n"
                f"BM25 分数：{lexical_score_text}\n"
                f"RRF 融合分数：{hit.rrf_score:.6f}\n"
                f"Reranker 精排分数：{rerank_score_text}\n"
                f"通过原因：{hit.acceptance_reason}\n"
                f"核心证据内容：{document.page_content}"
                f"{image_verification_text}"
            )

            for adjacent_index, adjacent in enumerate(
                expanded_hit.adjacent_chunks,
                start=1,
            ):
                neighbor = adjacent.document
                neighbor_page = self._format_page(neighbor.metadata.get("page"))
                neighbor_id = neighbor.metadata.get("chunk_id", "未知")
                neighbor_type = neighbor.metadata.get("content_type", "text")
                results.append(
                    f"[参考资料 {index} 的相邻上下文 {adjacent_index}]\n"
                    f"用途：{adjacent.relation}，仅用于补全核心证据\n"
                    f"引用标识：{self._citation_label(neighbor)}\n"
                    f"位置：{neighbor_page}\n"
                    f"Chunk ID：{neighbor_id}\n"
                    f"内容类型：{neighbor_type}\n"
                    f"内容：{neighbor.page_content}"
                )

            emit(
                "RAG", f"Hit #{index}",
                source=source_name, page=page_text,
                chunk_index=chunk_index,
                distance=(
                    f"{hit.dense_distance:.6f}"
                    if hit.dense_distance is not None
                    else "none"
                ),
                lexical_rank=hit.lexical_rank or "none",
                rrf=f"{hit.rrf_score:.6f}",
                rerank=(
                    f"{hit.rerank_score:.6f}"
                    if hit.rerank_score is not None
                    else "none"
                ),
            )

        emit(
            "RAG", "SearchEnd",
            raw=len(retrieval.candidates), accepted=len(retrieval.hits),
            adjacent=adjacent_count,
            adjacent_fallback=context_error or "none",
            mode=retrieval.mode,
            latency=f"{perf_counter() - started:.2f}s",
        )

        return "\n\n".join(results)


_rag_service: RagService | None = None


def get_rag_service() -> RagService:
    """整个 Agent 进程共用一个 RagService。"""
    global _rag_service

    if _rag_service is None:
        _rag_service = RagService()

    return _rag_service


def main() -> None:
    """人工测试知识库阈值过滤。"""
    service = get_rag_service()

    relevant_result = service.search(
        query=(
            "RAG主要包括哪些阶段？"
        ),
        top_k=3,
    )

    print("\n相关问题测试：")
    print(relevant_result)

    unrelated_result = service.search(
        query=(
            "红烧肉应该怎么制作？"
        ),
        top_k=3,
    )

    print("\n无关问题测试：")
    print(unrelated_result)


if __name__ == "__main__":
    main()
