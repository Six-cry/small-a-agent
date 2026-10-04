"""RAG 可选精排器。

本文件只负责把“问题-候选 Chunk”交给本地 Cross-Encoder 重新打分。
它不修改 Chroma、Manifest 或 BM25，也不会自动把候选文本发送到外部服务。
模型采用延迟加载，便于先在隔离评测中验证，验证通过后再接入正式检索链路。
"""

from __future__ import annotations

import math
from typing import Protocol, Sequence

from langchain_core.documents import Document


class RerankerError(RuntimeError):
    """精排器不可用或返回了非法分数。"""


class DocumentReranker(Protocol):
    """精排器的最小接口，便于单元测试替换成本地假模型。"""

    def score(self, query: str, documents: Sequence[Document]) -> list[float]:
        """按 documents 原顺序返回每个候选的相关性分数。"""


class CrossEncoderReranker:
    """使用本地 sentence-transformers Cross-Encoder 对候选重新打分。

    依赖和模型均是可选的。只有显式创建该类并第一次调用 ``score`` 时，
    才会导入 sentence-transformers 并加载模型；因此不会影响当前正式 RAG。
    """

    def __init__(
        self,
        model_name_or_path: str,
        *,
        device: str | None = None,
        batch_size: int = 8,
        max_length: int = 512,
    ) -> None:
        if not isinstance(model_name_or_path, str) or not model_name_or_path.strip():
            raise ValueError("model_name_or_path 必须是非空字符串")
        if isinstance(batch_size, bool) or not isinstance(batch_size, int) or batch_size <= 0:
            raise ValueError("batch_size 必须是正整数")
        if isinstance(max_length, bool) or not isinstance(max_length, int) or max_length <= 0:
            raise ValueError("max_length 必须是正整数")

        self.model_name_or_path = model_name_or_path.strip()
        self.device = device.strip() if isinstance(device, str) and device.strip() else None
        self.batch_size = batch_size
        self.max_length = max_length
        self._model = None
        self._load_error: RerankerError | None = None

    def _get_model(self):
        if self._model is not None:
            return self._model
        if self._load_error is not None:
            raise self._load_error

        try:
            from sentence_transformers import CrossEncoder
        except ImportError as exc:  # pragma: no cover - 依赖缺失时由隔离命令覆盖
            self._load_error = RerankerError(
                "未安装 sentence-transformers，无法加载本地 Cross-Encoder；"
                "请安装 aa_my_agent/requirements-rag-reranker.txt"
            )
            raise self._load_error from exc

        kwargs = {"max_length": self.max_length}
        if self.device:
            kwargs["device"] = self.device
        try:
            self._model = CrossEncoder(self.model_name_or_path, **kwargs)
        except Exception as exc:  # pragma: no cover - 由具体模型环境决定
            self._load_error = RerankerError(
                f"加载精排模型失败：{self.model_name_or_path}；{type(exc).__name__}: {exc}"
            )
            raise self._load_error from exc
        return self._model

    def score(self, query: str, documents: Sequence[Document]) -> list[float]:
        if not isinstance(query, str) or not query.strip():
            raise ValueError("query 不能为空")
        document_list = list(documents)
        if not document_list:
            return []
        if any(not isinstance(document, Document) for document in document_list):
            raise TypeError("documents 必须全部是 LangChain Document")

        pairs = [(query.strip(), document.page_content) for document in document_list]
        try:
            raw_scores = self._get_model().predict(
                pairs,
                batch_size=self.batch_size,
                show_progress_bar=False,
            )
            scores = [float(value) for value in raw_scores]
        except RerankerError:
            raise
        except Exception as exc:  # pragma: no cover - 由具体模型环境决定
            raise RerankerError(
                f"精排模型计算失败：{type(exc).__name__}: {exc}"
            ) from exc

        if len(scores) != len(document_list) or any(
            not math.isfinite(score) for score in scores
        ):
            raise RerankerError("精排模型返回了数量不匹配或非有限分数")
        return scores
