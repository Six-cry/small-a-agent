"""验证 RagService 能把混合检索结果稳定整理给 Agent。"""

from types import SimpleNamespace

from langchain_core.documents import Document

from aa_my_agent.rag.hybrid_retriever import (
    HybridRetrievalResult,
    HybridSearchHit,
)
from aa_my_agent.rag.adjacent_context import AdjacentChunk, ExpandedHitContext
from aa_my_agent.rag.service import RagService


def test_service_formats_hybrid_scores(monkeypatch) -> None:
    document = Document(
        page_content="虚拟队列积压和历史决策经验",
        metadata={
            "chunk_id": "a" * 64,
            "chunk_index": 2,
            "source": "sample.pdf",
            "page": 4,
        },
    )
    hit = HybridSearchHit(
        document=document,
        rrf_score=0.032,
        accepted=True,
        acceptance_reason="dense-and-bm25",
        dense_rank=2,
        dense_distance=0.81,
        lexical_rank=1,
        lexical_score=12.5,
        lexical_query_coverage=0.5,
        lexical_matched_terms=("控制", "平面"),
        rerank_score=3.25,
    )

    class Retriever:
        def retrieve(self, query, final_k):
            assert final_k == 3
            return HybridRetrievalResult(
                mode="hybrid-reranked",
                candidates=(hit,),
                hits=(hit,),
            )

    neighbor = Document(
        page_content="这是后一块中的补充说明",
        metadata={
            "chunk_id": "b" * 64,
            "chunk_index": 3,
            "source": "sample.pdf",
            "page": 5,
            "content_type": "text",
        },
    )

    class Expander:
        def expand(self, hits):
            assert hits == (hit,)
            return (
                ExpandedHitContext(
                    hit=hit,
                    adjacent_chunks=(AdjacentChunk(neighbor, 1),),
                ),
            )

        def invalidate(self):
            raise AssertionError("本测试没有发生同步")

    monkeypatch.setattr(
        "aa_my_agent.rag.service.check_rag_index_status",
        lambda **_kwargs: SimpleNamespace(status="up_to_date"),
    )
    service = RagService()
    service._retriever = Retriever()  # type: ignore[assignment]
    service._context_expander = Expander()  # type: ignore[assignment]

    result = service.search("图3中有什么", top_k=3)

    assert "检索模式：hybrid-reranked" in result
    assert "来源：sample.pdf" in result
    assert "位置：第 5 页" in result
    assert "向量距离：0.810000" in result
    assert "BM25 排名：1" in result
    assert "RRF 融合分数：0.032000" in result
    assert "Reranker 精排分数：3.250000" in result
    assert "虚拟队列积压和历史决策经验" in result
    assert "补充的相邻 Chunk：1 个" in result
    assert "引用标识：[sample.pdf，第 5 页，Chunk " + "a" * 64 + "]" in result
    assert "用途：后第 1 块，仅用于补全核心证据" in result
    assert "引用标识：[sample.pdf，第 6 页，Chunk " + "b" * 64 + "]" in result
    assert "这是后一块中的补充说明" in result


def test_service_falls_back_to_core_hits_when_adjacent_expansion_fails(
    monkeypatch,
) -> None:
    document = Document(
        page_content="仍然保留的核心证据",
        metadata={
            "chunk_id": "c" * 64,
            "chunk_index": 1,
            "source": "fallback.pdf",
            "page": 0,
        },
    )
    hit = HybridSearchHit(
        document=document,
        rrf_score=0.02,
        accepted=True,
        acceptance_reason="dense-distance",
        dense_distance=0.3,
    )

    class Retriever:
        def retrieve(self, query, final_k):
            return HybridRetrievalResult(
                mode="hybrid-reranked",
                candidates=(hit,),
                hits=(hit,),
            )

    class BrokenExpander:
        def expand(self, hits):
            raise RuntimeError("邻接索引损坏")

    events = []
    monkeypatch.setattr(
        "aa_my_agent.rag.service.check_rag_index_status",
        lambda **_kwargs: SimpleNamespace(status="up_to_date"),
    )
    monkeypatch.setattr(
        "aa_my_agent.rag.service.emit",
        lambda *args, **kwargs: events.append((args, kwargs)),
    )
    service = RagService()
    service._retriever = Retriever()  # type: ignore[assignment]
    service._context_expander = BrokenExpander()  # type: ignore[assignment]

    result = service.search("测试回退", top_k=1)

    assert "仍然保留的核心证据" in result
    assert "补充的相邻 Chunk：0 个" in result
    assert any(args[1] == "AdjacentContextFallback" for args, _kwargs in events)


def test_service_reports_stale_index_without_running_sync(monkeypatch) -> None:
    document = Document(
        page_content="旧索引中仍然可以读取的证据",
        metadata={
            "chunk_id": "d" * 64,
            "chunk_index": 0,
            "source": "stale.pdf",
            "page": 0,
        },
    )
    hit = HybridSearchHit(
        document=document,
        rrf_score=0.02,
        accepted=True,
        acceptance_reason="dense-distance",
        dense_distance=0.3,
    )

    class Retriever:
        def retrieve(self, query, final_k):
            return HybridRetrievalResult(
                mode="hybrid-reranked",
                candidates=(hit,),
                hits=(hit,),
            )

    monkeypatch.setattr(
        "aa_my_agent.rag.service.check_rag_index_status",
        lambda **_kwargs: SimpleNamespace(
            status="stale",
            message=(
                "检测到尚未入库的知识文件变化；"
                "请运行 sync_knowledge sync"
            ),
        ),
    )
    monkeypatch.setattr(
        "aa_my_agent.config.RAG_ENABLE_ADJACENT_CONTEXT",
        False,
    )
    service = RagService()
    service._retriever = Retriever()  # type: ignore[assignment]

    result = service.search("读取旧索引", top_k=1)

    assert "知识库状态提醒" in result
    assert "请运行 sync_knowledge sync" in result
    assert "旧索引中仍然可以读取的证据" in result
