"""验证按问题读图的路径边界、外发开关和结构化返回结果。"""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain_core.documents import Document

from aa_my_agent.rag.image_verifier import (
    ImageVerificationError,
    ImageVerificationService,
)
from aa_my_agent.rag.multimodal_parser import get_output_dir


class _Store:
    def __init__(self, document: Document) -> None:
        self.document = document

    def get_documents_by_ids(self, chunk_ids):
        if self.document.metadata["chunk_id"] in set(chunk_ids):
            return {self.document.metadata["chunk_id"]: self.document}
        return {}


class _Client:
    def __init__(self) -> None:
        message = SimpleNamespace(content="节点7到节点11标注为(0.232,0.992)。")
        choice = SimpleNamespace(message=message)
        completions = SimpleNamespace(
            create=lambda **_kwargs: SimpleNamespace(choices=[choice])
        )
        self.chat = SimpleNamespace(completions=completions)


def _fixture(tmp_path: Path) -> tuple[Document, Path, Path]:
    knowledge = tmp_path / "knowledge"
    cache = tmp_path / "cache"
    knowledge.mkdir()
    pdf = knowledge / "sample.pdf"
    pdf.write_bytes(b"pdf")
    file_hash = "a" * 64
    output = get_output_dir(pdf, output_root=cache, source_hash=file_hash)
    image = output / "images" / "figure.png"
    image.parent.mkdir(parents=True)
    image.write_bytes(b"png")
    document = Document(
        page_content="图题：图2 网络拓扑",
        metadata={
            "chunk_id": "b" * 64,
            "source": "sample.pdf",
            "file_hash": file_hash,
            "content_type": "image_summary",
            "image_path": "images/figure.png",
            "pdf_page": 4,
        },
    )
    return document, knowledge, cache


def test_external_switch_blocks_call(tmp_path) -> None:
    document, knowledge, cache = _fixture(tmp_path)
    service = ImageVerificationService(
        vector_store=_Store(document),  # type: ignore[arg-type]
        allow_external=False,
        client=_Client(),
        knowledge_dir=knowledge,
        cache_roots=(cache,),
    )

    with pytest.raises(ImageVerificationError, match="显式启用"):
        service.verify(chunk_id="b" * 64, question="标注是什么？")


def test_returns_answer_without_writing_back(tmp_path) -> None:
    document, knowledge, cache = _fixture(tmp_path)
    service = ImageVerificationService(
        vector_store=_Store(document),  # type: ignore[arg-type]
        allow_external=True,
        client=_Client(),
        knowledge_dir=knowledge,
        cache_roots=(cache,),
    )

    result = json.loads(
        service.verify(chunk_id="b" * 64, question="节点7到11标注是什么？")
    )

    assert result["status"] == "machine_unverified"
    assert result["answer"] == "节点7到节点11标注为(0.232,0.992)。"
    assert result["prompt_version"] == "question-focused-v1"
    assert result["source"] == "sample.pdf"
