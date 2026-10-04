import hashlib
import json

import pytest
from langchain_core.documents import Document

from aa_my_agent.config import CHROMA_DIR, RAG_COLLECTION_NAME
from aa_my_agent.eval.rag.build_structured_test_index import (
    assert_isolated_test_target,
    load_chunks_jsonl,
)


def _chunk_payload(content: str, chunk_id: str) -> dict:
    content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    return {
        "page_content": content,
        "metadata": {
            "source": "sample.pdf",
            "file_hash": "a" * 64,
            "content_hash": content_hash,
            "chunk_id": chunk_id,
            "chunk_index": 0,
            "total_chunks": 1,
            "page": 0,
        },
    }


def test_load_chunks_jsonl(tmp_path) -> None:
    path = tmp_path / "chunks.jsonl"
    payload = _chunk_payload("测试内容", "b" * 64)
    path.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")

    chunks = load_chunks_jsonl([path])

    assert chunks == [
        Document(page_content="测试内容", metadata=payload["metadata"])
    ]


def test_duplicate_chunk_ids_are_rejected(tmp_path) -> None:
    path = tmp_path / "chunks.jsonl"
    payload = _chunk_payload("测试内容", "b" * 64)
    path.write_text(
        json.dumps(payload, ensure_ascii=False)
        + "\n"
        + json.dumps(payload, ensure_ascii=False)
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="重复 chunk_id"):
        load_chunks_jsonl([path])


def test_production_chroma_target_is_rejected(tmp_path) -> None:
    with pytest.raises(ValueError, match="生产 CHROMA_DIR"):
        assert_isolated_test_target(
            persist_directory=CHROMA_DIR,
            collection_name="safe_test_collection",
        )

    with pytest.raises(ValueError, match="生产 Collection"):
        assert_isolated_test_target(
            persist_directory=tmp_path,
            collection_name=RAG_COLLECTION_NAME,
        )
