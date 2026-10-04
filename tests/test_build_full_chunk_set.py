import json

import pytest
from langchain_core.documents import Document

from aa_my_agent.eval.rag.build_full_chunk_set import build_full_chunk_set
from aa_my_agent.rag.multimodal_parser import calculate_file_hash, get_output_dir
from aa_my_agent.rag.text_splitter import TextSplitter


def _make_pdf_cache(pdf, output_root) -> None:
    source_hash = calculate_file_hash(pdf)
    output_dir = get_output_dir(
        pdf,
        output_root=output_root,
        source_hash=source_hash,
    )
    output_dir.mkdir(parents=True)
    splitter = TextSplitter(
        chunk_size=100,
        chunk_overlap=0,
        split_separators=("\n", ""),
    )
    chunks = splitter.split_documents(
        [
            Document(
                page_content="PDF结构化内容",
                metadata={
                    "source": pdf.name,
                    "file_hash": source_hash,
                    "page": 0,
                    "content_type": "text",
                },
            )
        ]
    )
    with (output_dir / "chunks.jsonl").open("w", encoding="utf-8") as stream:
        for chunk in chunks:
            stream.write(
                json.dumps(
                    {"page_content": chunk.page_content, "metadata": chunk.metadata},
                    ensure_ascii=False,
                )
                + "\n"
            )
    (output_dir / "docling_document.json").write_text("{}", encoding="utf-8")
    (output_dir / "documents.jsonl").write_text("{}\n", encoding="utf-8")
    (output_dir / "manifest.json").write_text(
        json.dumps(
            {
                "source_hash": source_hash,
                "requested_pages": "all",
                "structured_chunk_count": len(chunks),
            }
        ),
        encoding="utf-8",
    )


def test_full_chunk_set_merges_docling_pdf_and_legacy_txt(tmp_path) -> None:
    knowledge = tmp_path / "knowledge"
    output_root = tmp_path / "pdf_outputs"
    report_dir = tmp_path / "reports"
    knowledge.mkdir()
    pdf = knowledge / "paper.pdf"
    pdf.write_bytes(b"pdf")
    (knowledge / "notes.txt").write_text("TXT原生内容", encoding="utf-8")
    _make_pdf_cache(pdf, output_root)

    chunks_path, payload = build_full_chunk_set(
        knowledge_dir=knowledge,
        pdf_output_root=output_root,
        report_dir=report_dir,
    )

    assert payload["source_count"] == 2
    assert payload["pdf_source_count"] == 1
    assert payload["non_pdf_source_count"] == 1
    contents = [
        json.loads(line)["page_content"]
        for line in chunks_path.read_text(encoding="utf-8").splitlines()
    ]
    assert "PDF结构化内容" in contents
    assert "TXT原生内容" in contents


def test_full_chunk_set_rejects_incomplete_pdf(tmp_path) -> None:
    knowledge = tmp_path / "knowledge"
    knowledge.mkdir()
    (knowledge / "paper.pdf").write_bytes(b"pdf")

    with pytest.raises(RuntimeError, match="尚未完成全文 Docling 解析"):
        build_full_chunk_set(
            knowledge_dir=knowledge,
            pdf_output_root=tmp_path / "missing_outputs",
            report_dir=tmp_path / "reports",
        )
