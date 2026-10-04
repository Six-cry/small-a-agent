"""RAG 本地 BM25 关键词索引。

这个文件负责把已经进入 Chroma 的同一批 Chunk 建成第二套词法索引，
用于补充向量检索不擅长的图号、节点编号、数字、英文缩写和固定短语。
它不会重新解析 PDF，也不会调用 Embedding 或大模型 API。
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import threading
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from langchain_core.documents import Document


BM25_SCHEMA_VERSION = 1
_CJK_PATTERN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]+")
_NON_CJK_TOKEN_PATTERN = re.compile(
    r"(?:[A-Za-z][A-Za-z0-9_.-]*)"
    r"|(?:[Α-Ωα-ωλμθτΔ][A-Za-z0-9_Α-Ωα-ω]*)"
    r"|(?:\d+(?:\.\d+)?%?)"
)
_CJK_STOP_TERMS = frozenset(
    {
        "一个",
        "一些",
        "以及",
        "什么",
        "分别",
        "可以",
        "如何",
        "应该",
        "是否",
        "这个",
        "这些",
        "进行",
        "其中",
        "之间",
        "中的",
        "通过",
    }
)


class LexicalVectorStore(Protocol):
    """BM25 同步所需的最小向量库接口。"""

    collection_name: str

    def get_chunk_ids(self) -> tuple[str, ...]: ...

    def get_all_documents(self) -> list[Document]: ...


class LexicalIndexError(RuntimeError):
    """BM25 索引无法安全读取、构建或查询。"""


@dataclass(frozen=True, slots=True)
class LexicalSearchHit:
    """一条 BM25 检索结果及其可解释证据。"""

    document: Document
    score: float
    rank: int
    matched_terms: tuple[str, ...]
    query_coverage: float


def _cjk_ngrams(text: str) -> list[str]:
    """把连续中文生成二元词和三元词，避免强依赖外部分词包。"""
    if len(text) == 1:
        return [text]

    terms: list[str] = []
    for size in (2, 3):
        if len(text) < size:
            continue
        for start in range(len(text) - size + 1):
            term = text[start : start + size]
            if term not in _CJK_STOP_TERMS:
                terms.append(term)
    return terms


def tokenize_for_bm25(text: str) -> tuple[str, ...]:
    """生成适合中文技术资料的 BM25 词项。"""
    if not isinstance(text, str):
        raise TypeError("BM25 分词输入必须是字符串")

    normalized = text.casefold()
    terms: list[str] = []
    for match in _CJK_PATTERN.finditer(normalized):
        terms.extend(_cjk_ngrams(match.group(0)))
    terms.extend(
        match.group(0)
        for match in _NON_CJK_TOKEN_PATTERN.finditer(normalized)
    )
    return tuple(term for term in terms if term.strip())


def _chunk_id(document: Document) -> str:
    value = document.metadata.get("chunk_id")
    if not isinstance(value, str) or not value.strip():
        raise LexicalIndexError("BM25 文档缺少有效 chunk_id")
    return value.strip()


def _ids_signature(chunk_ids: tuple[str, ...]) -> str:
    digest = hashlib.sha256()
    for chunk_id in sorted(chunk_ids):
        digest.update(chunk_id.encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


class BM25Index:
    """持久化并查询一套与 Chroma Chunk 对齐的 BM25 索引。"""

    def __init__(
        self,
        index_path: Path | str,
        collection_name: str,
        *,
        k1: float = 1.5,
        b: float = 0.75,
    ) -> None:
        if not isinstance(collection_name, str) or not collection_name.strip():
            raise ValueError("collection_name 不能为空")
        if not math.isfinite(k1) or k1 <= 0:
            raise ValueError("BM25 k1 必须是正数")
        if not math.isfinite(b) or not 0 <= b <= 1:
            raise ValueError("BM25 b 必须位于 0 到 1")

        self.index_path = Path(index_path)
        self.collection_name = collection_name.strip()
        self.k1 = float(k1)
        self.b = float(b)
        self._lock = threading.RLock()
        self._signature: str | None = None
        self._documents: tuple[Document, ...] = ()
        self._tokens: tuple[tuple[str, ...], ...] = ()
        self._term_frequencies: tuple[Counter[str], ...] = ()
        self._document_frequencies: Counter[str] = Counter()
        self._average_length = 0.0

    @property
    def count(self) -> int:
        """返回当前内存索引包含的 Chunk 数量。"""
        return len(self._documents)

    def _install(
        self,
        documents: list[Document],
        token_rows: list[tuple[str, ...]],
        signature: str,
    ) -> None:
        if len(documents) != len(token_rows):
            raise LexicalIndexError("BM25 文档数量与词项数量不一致")

        frequencies = tuple(Counter(tokens) for tokens in token_rows)
        document_frequencies: Counter[str] = Counter()
        for frequency in frequencies:
            document_frequencies.update(frequency.keys())

        self._documents = tuple(documents)
        self._tokens = tuple(token_rows)
        self._term_frequencies = frequencies
        self._document_frequencies = document_frequencies
        self._average_length = (
            sum(len(tokens) for tokens in token_rows) / len(token_rows)
            if token_rows
            else 0.0
        )
        self._signature = signature

    def _payload(self) -> dict:
        rows = []
        for document, tokens in zip(self._documents, self._tokens, strict=True):
            rows.append(
                {
                    "chunk_id": _chunk_id(document),
                    "page_content": document.page_content,
                    "metadata": document.metadata,
                    "tokens": list(tokens),
                }
            )
        return {
            "schema_version": BM25_SCHEMA_VERSION,
            "collection_name": self.collection_name,
            "chunk_ids_signature": self._signature,
            "k1": self.k1,
            "b": self.b,
            "rows": rows,
        }

    def _save(self) -> None:
        self.index_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = self.index_path.with_suffix(
            f"{self.index_path.suffix}.tmp"
        )
        temporary_path.write_text(
            json.dumps(self._payload(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary_path.replace(self.index_path)

    def _load_if_compatible(self, expected_signature: str) -> bool:
        if not self.index_path.exists():
            return False
        try:
            payload = json.loads(self.index_path.read_text(encoding="utf-8"))
            if payload.get("schema_version") != BM25_SCHEMA_VERSION:
                return False
            if payload.get("collection_name") != self.collection_name:
                return False
            if payload.get("chunk_ids_signature") != expected_signature:
                return False
            rows = payload.get("rows")
            if not isinstance(rows, list):
                return False

            documents: list[Document] = []
            token_rows: list[tuple[str, ...]] = []
            seen_ids: set[str] = set()
            for row in rows:
                if not isinstance(row, dict):
                    return False
                metadata = row.get("metadata")
                page_content = row.get("page_content")
                tokens = row.get("tokens")
                if (
                    not isinstance(metadata, dict)
                    or not isinstance(page_content, str)
                    or not isinstance(tokens, list)
                    or any(not isinstance(term, str) for term in tokens)
                ):
                    return False
                document = Document(page_content=page_content, metadata=metadata)
                chunk_id = _chunk_id(document)
                if chunk_id in seen_ids:
                    return False
                seen_ids.add(chunk_id)
                documents.append(document)
                token_rows.append(tuple(tokens))

            actual_signature = _ids_signature(tuple(seen_ids))
            if actual_signature != expected_signature:
                return False
            self._install(documents, token_rows, expected_signature)
            return True
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return False

    def rebuild(self, documents: list[Document]) -> None:
        """使用完整 Chunk 集合原子重建 BM25 索引。"""
        normalized_documents: list[Document] = []
        token_rows: list[tuple[str, ...]] = []
        seen_ids: set[str] = set()
        for document in sorted(documents, key=_chunk_id):
            chunk_id = _chunk_id(document)
            if chunk_id in seen_ids:
                raise LexicalIndexError(f"BM25 出现重复 chunk_id：{chunk_id}")
            if not isinstance(document.page_content, str) or not document.page_content.strip():
                raise LexicalIndexError(f"BM25 Chunk 内容为空：{chunk_id}")
            seen_ids.add(chunk_id)
            normalized_documents.append(document)
            token_rows.append(tokenize_for_bm25(document.page_content))

        signature = _ids_signature(tuple(seen_ids))
        with self._lock:
            self._install(normalized_documents, token_rows, signature)
            self._save()

    def ensure_current(self, vector_store: LexicalVectorStore) -> bool:
        """确认索引与 Chroma ID 一致；必要时从 Chroma 自动重建。"""
        chunk_ids = tuple(vector_store.get_chunk_ids())
        expected_signature = _ids_signature(chunk_ids)
        with self._lock:
            if self._signature == expected_signature:
                return False
            if self._load_if_compatible(expected_signature):
                return False

            documents = vector_store.get_all_documents()
            document_ids = tuple(_chunk_id(document) for document in documents)
            if set(document_ids) != set(chunk_ids):
                raise LexicalIndexError("Chroma 文档内容与 Chunk ID 列表不一致")
            self.rebuild(documents)
            if self._signature != expected_signature:
                raise LexicalIndexError("BM25 重建后的指纹与 Chroma 不一致")
            return True

    def _idf(self, term: str) -> float:
        document_count = len(self._documents)
        frequency = self._document_frequencies.get(term, 0)
        return math.log(
            1.0 + (document_count - frequency + 0.5) / (frequency + 0.5)
        )

    def search(self, query: str, k: int) -> list[LexicalSearchHit]:
        """查询 BM25，并返回排名、分数及关键词覆盖率。"""
        if not isinstance(query, str) or not query.strip():
            raise ValueError("query 不能为空")
        if isinstance(k, bool) or not isinstance(k, int) or k <= 0:
            raise ValueError("k 必须是正整数")

        with self._lock:
            if not self._documents:
                return []
            query_terms = tuple(dict.fromkeys(tokenize_for_bm25(query)))
            if not query_terms:
                return []
            query_weight = sum(self._idf(term) for term in query_terms)
            scored: list[tuple[float, str, Document, tuple[str, ...], float]] = []
            for document, frequency, tokens in zip(
                self._documents,
                self._term_frequencies,
                self._tokens,
                strict=True,
            ):
                document_length = len(tokens)
                score = 0.0
                matched_terms: list[str] = []
                for term in query_terms:
                    term_frequency = frequency.get(term, 0)
                    if term_frequency <= 0:
                        continue
                    matched_terms.append(term)
                    denominator = term_frequency + self.k1 * (
                        1.0
                        - self.b
                        + self.b
                        * document_length
                        / max(self._average_length, 1.0)
                    )
                    score += self._idf(term) * (
                        term_frequency * (self.k1 + 1.0) / denominator
                    )
                if score <= 0:
                    continue
                matched_weight = sum(self._idf(term) for term in matched_terms)
                coverage = matched_weight / query_weight if query_weight else 0.0
                scored.append(
                    (
                        score,
                        _chunk_id(document),
                        document,
                        tuple(matched_terms),
                        coverage,
                    )
                )

            scored.sort(key=lambda item: (-item[0], item[1]))
            return [
                LexicalSearchHit(
                    document=document,
                    score=score,
                    rank=rank,
                    matched_terms=matched_terms,
                    query_coverage=coverage,
                )
                for rank, (
                    score,
                    _identifier,
                    document,
                    matched_terms,
                    coverage,
                ) in enumerate(scored[:k], start=1)
            ]


def create_default_bm25_index() -> BM25Index:
    """根据正式配置创建 BM25 旁路索引。"""
    from ..config import RAG_BM25_INDEX_PATH, RAG_COLLECTION_NAME

    return BM25Index(
        index_path=RAG_BM25_INDEX_PATH,
        collection_name=RAG_COLLECTION_NAME,
    )
