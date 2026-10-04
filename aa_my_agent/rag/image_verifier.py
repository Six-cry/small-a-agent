"""对 RAG 命中的原始 PDF 裁图执行按问题视觉核验。

普通检索只使用已入库的保守图片摘要；当问题需要确认节点、连线、箭头、
数值或包含关系时，本模块根据父 Chunk 的来源、文件 Hash 和 ``image_path``
定位原裁图，再把“当前问题 + 单张裁图”发送给视觉模型。结果只返回本次
会话，不自动写回知识库，避免把一次模型误判沉淀为长期事实。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

from langchain_core.documents import Document

from ..config import KNOWLEDGE_DIR
from .multimodal_parser import (
    DEFAULT_IMAGE_MODEL,
    DEFAULT_OUTPUT_ROOT,
    create_glm_image_client,
    describe_image_for_question,
    get_output_dir,
)
from .structured_pdf_loader import PRODUCTION_PDF_CACHE_ROOT
from .vector_store import VectorStoreService


class ImageVerificationError(RuntimeError):
    """图片父块、裁图路径或视觉核验配置不满足要求。"""


def _caption_from_document(document: Document) -> str:
    for raw_line in document.page_content.splitlines():
        line = raw_line.strip()
        if line.startswith("图题："):
            return line.removeprefix("图题：").strip()
    return ""


def resolve_image_path(
    document: Document,
    *,
    knowledge_dir: Path = KNOWLEDGE_DIR,
    cache_roots: Iterable[Path] = (
        PRODUCTION_PDF_CACHE_ROOT,
        DEFAULT_OUTPUT_ROOT,
    ),
) -> Path:
    """只在知识目录对应的结构化缓存中解析裁图路径。"""
    source = str(document.metadata.get("source", "")).strip()
    source_hash = str(document.metadata.get("file_hash", "")).strip()
    image_path = str(document.metadata.get("image_path", "")).strip()
    if not source or not source_hash or not image_path:
        raise ImageVerificationError("图片 Chunk 缺少 source、file_hash 或 image_path")
    if document.metadata.get("content_type") != "image_summary":
        raise ImageVerificationError("只有完整 image_summary 父块可以核验原图")

    knowledge_root = knowledge_dir.expanduser().resolve(strict=True)
    source_path = (knowledge_root / source).resolve(strict=True)
    if not source_path.is_relative_to(knowledge_root):
        raise ImageVerificationError("图片来源越过知识目录边界")

    for raw_root in cache_roots:
        root = Path(raw_root).expanduser().resolve()
        output_dir = get_output_dir(
            source_path,
            output_root=root,
            source_hash=source_hash,
        )
        candidate = (output_dir / image_path).resolve()
        if not candidate.is_relative_to(output_dir):
            raise ImageVerificationError("image_path 越过结构化缓存目录")
        if candidate.is_file():
            return candidate
    raise ImageVerificationError("没有在允许的结构化缓存中找到原始裁图")


class ImageVerificationService:
    """根据图片父 Chunk ID 定位并核验单张原裁图。"""

    def __init__(
        self,
        vector_store: VectorStoreService | None = None,
        *,
        allow_external: bool = False,
        client: Any | None = None,
        knowledge_dir: Path = KNOWLEDGE_DIR,
        cache_roots: Iterable[Path] | None = None,
    ) -> None:
        self.vector_store = vector_store or VectorStoreService()
        self.allow_external = bool(allow_external)
        self.client = client
        self.knowledge_dir = Path(knowledge_dir)
        self.cache_roots = tuple(cache_roots) if cache_roots is not None else None

    def verify(self, *, chunk_id: str, question: str) -> str:
        if not isinstance(chunk_id, str) or not chunk_id.strip():
            raise ValueError("chunk_id 不能为空")
        if not isinstance(question, str) or not question.strip():
            raise ValueError("question 不能为空")
        normalized_question = question.strip()
        if len(normalized_question) > 1000:
            raise ValueError("question 最多 1000 个字符")
        if not self.allow_external:
            raise ImageVerificationError(
                "按问题读图会把单张裁图发送给外部视觉模型；"
                "请先显式启用 RAG_ALLOW_ON_DEMAND_IMAGE_VERIFY=true"
            )

        found = self.vector_store.get_documents_by_ids([chunk_id.strip()])
        document = found.get(chunk_id.strip())
        if document is None:
            raise ImageVerificationError("知识库中不存在指定 Chunk ID")
        resolve_kwargs: dict[str, Any] = {"knowledge_dir": self.knowledge_dir}
        if self.cache_roots is not None:
            resolve_kwargs["cache_roots"] = self.cache_roots
        image_path = resolve_image_path(document, **resolve_kwargs)

        client = self.client or create_glm_image_client()
        if client is None:
            raise ImageVerificationError("缺少 ZHIPUAI_API_KEY，无法核验原图")
        answer, prompt_version = describe_image_for_question(
            client=client,
            image_path=image_path,
            question=normalized_question,
            caption=_caption_from_document(document),
            model=DEFAULT_IMAGE_MODEL,
        )
        payload = {
            "status": "machine_unverified",
            "source": document.metadata.get("source", "未知来源"),
            "page": document.metadata.get("pdf_page"),
            "chunk_id": document.metadata.get("chunk_id"),
            "image_path": str(image_path),
            "model": DEFAULT_IMAGE_MODEL,
            "prompt_version": prompt_version,
            "question": normalized_question,
            "answer": answer,
            "notice": "这是针对当前问题的视觉模型核验结果，未人工复核，且未写回知识库。",
        }
        return json.dumps(payload, ensure_ascii=False, indent=2)


__all__ = [
    "ImageVerificationError",
    "ImageVerificationService",
    "resolve_image_path",
]
