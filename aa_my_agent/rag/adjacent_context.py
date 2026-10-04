"""为精排后的核心 Chunk 补充同一文件中的相邻上下文。

本模块只读取已经存在于 Chroma 的 Chunk，不重新解析文档，也不生成
Embedding。相邻块只用于补全被切开的说明，不参与召回、证据门槛或
Reranker 排名；图片检索短子块会被排除，避免把索引卡片当成正文。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from langchain_core.documents import Document

from .hybrid_retriever import HybridSearchHit
from .image_parent_child import IMAGE_CHILD_CONTENT_TYPE
from .vector_store import VectorStoreService


class AdjacentContextError(RuntimeError):
    """邻接索引无法被可靠建立。"""


@dataclass(frozen=True, slots=True)
class AdjacentChunk:
    """一条相对于核心证据的相邻 Chunk。"""

    document: Document
    offset: int

    @property
    def relation(self) -> str:
        """返回便于展示的相对位置。"""
        if self.offset < 0:
            return f"前第 {abs(self.offset)} 块"
        return f"后第 {self.offset} 块"


@dataclass(frozen=True, slots=True)
class ExpandedHitContext:
    """一条核心命中以及它的补充上下文。"""

    hit: HybridSearchHit
    adjacent_chunks: tuple[AdjacentChunk, ...] = ()


def _required_chunk_id(document: Document) -> str:
    value = document.metadata.get("chunk_id")
    if not isinstance(value, str) or not value.strip():
        raise AdjacentContextError("Chunk 缺少有效的 chunk_id")
    return value.strip()


def _source(document: Document) -> str | None:
    value = document.metadata.get("source")
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()


def _chunk_index(document: Document) -> int | None:
    value = document.metadata.get("chunk_index")
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


class AdjacentChunkExpander:
    """按文件级 chunk_index 为核心命中查找前后 Chunk。"""

    def __init__(
        self,
        vector_store: VectorStoreService,
        *,
        window_size: int = 1,
        max_total_chars: int = 8000,
    ) -> None:
        if (
            isinstance(window_size, bool)
            or not isinstance(window_size, int)
            or window_size <= 0
        ):
            raise ValueError("window_size 必须是正整数")
        if (
            isinstance(max_total_chars, bool)
            or not isinstance(max_total_chars, int)
            or max_total_chars <= 0
        ):
            raise ValueError("max_total_chars 必须是正整数")

        self.vector_store = vector_store
        self.window_size = window_size
        self.max_total_chars = max_total_chars
        self._chunk_ids: tuple[str, ...] | None = None
        self._by_source_and_index: dict[str, dict[int, Document]] = {}

    def invalidate(self) -> None:
        """知识库同步后丢弃旧的邻接索引。"""
        self._chunk_ids = None
        self._by_source_and_index = {}

    def _refresh(self, current_ids: tuple[str, ...]) -> None:
        documents = self.vector_store.get_all_documents()
        loaded_ids = tuple(sorted(_required_chunk_id(document) for document in documents))
        if loaded_ids != current_ids:
            raise AdjacentContextError("读取 Chunk 时知识库内容发生变化")

        by_source: dict[str, dict[int, Document]] = {}
        for document in documents:
            # image_retrieval 是追加在文件末尾的检索短卡片，不属于原文
            # 顺序；图片父块 image_summary 仍保留并可正常参与邻接扩展。
            if document.metadata.get("content_type") == IMAGE_CHILD_CONTENT_TYPE:
                continue
            source = _source(document)
            index = _chunk_index(document)
            if source is None or index is None:
                continue
            source_index = by_source.setdefault(source, {})
            if index in source_index:
                raise AdjacentContextError(
                    f"同一来源存在重复 chunk_index：{source}#{index}"
                )
            source_index[index] = document

        self._by_source_and_index = by_source
        self._chunk_ids = current_ids

    def _ensure_current(self) -> None:
        current_ids = tuple(sorted(self.vector_store.get_chunk_ids()))
        if self._chunk_ids != current_ids:
            self._refresh(current_ids)

    def expand(
        self,
        hits: Sequence[HybridSearchHit],
    ) -> tuple[ExpandedHitContext, ...]:
        """补充相邻块，同时保证核心命中、去重和总字符预算。"""
        if not hits:
            return ()

        self._ensure_current()
        core_ids = {_required_chunk_id(hit.document) for hit in hits}
        used_ids = set(core_ids)
        used_chars = 0
        expanded: list[ExpandedHitContext] = []

        for hit in hits:
            core = hit.document
            source = _source(core)
            index = _chunk_index(core)
            adjacent: list[AdjacentChunk] = []
            if source is None or index is None:
                expanded.append(ExpandedHitContext(hit=hit))
                continue

            source_index = self._by_source_and_index.get(source, {})
            offsets: list[int] = []
            for distance in range(1, self.window_size + 1):
                offsets.extend((-distance, distance))

            for offset in offsets:
                document = source_index.get(index + offset)
                if document is None:
                    continue
                chunk_id = _required_chunk_id(document)
                if chunk_id in used_ids:
                    continue
                content_chars = len(document.page_content)
                if used_chars + content_chars > self.max_total_chars:
                    continue
                used_ids.add(chunk_id)
                used_chars += content_chars
                adjacent.append(AdjacentChunk(document=document, offset=offset))

            expanded.append(
                ExpandedHitContext(
                    hit=hit,
                    adjacent_chunks=tuple(adjacent),
                )
            )

        return tuple(expanded)


def create_default_adjacent_chunk_expander(
    vector_store: VectorStoreService,
) -> AdjacentChunkExpander:
    """按正式配置创建邻接 Chunk 扩展器。"""
    from ..config import (
        RAG_ADJACENT_MAX_TOTAL_CHARS,
        RAG_ADJACENT_WINDOW_SIZE,
    )

    return AdjacentChunkExpander(
        vector_store,
        window_size=RAG_ADJACENT_WINDOW_SIZE,
        max_total_chars=RAG_ADJACENT_MAX_TOTAL_CHARS,
    )


__all__ = [
    "AdjacentChunk",
    "AdjacentChunkExpander",
    "AdjacentContextError",
    "ExpandedHitContext",
    "create_default_adjacent_chunk_expander",
]
