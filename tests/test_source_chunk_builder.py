import json
from pathlib import Path

from langchain_core.documents import Document

from aa_my_agent.rag.models import SourceSnapshot
from aa_my_agent.rag.multimodal_parser import get_output_dir
from aa_my_agent.rag.source_chunk_builder import SourceChunkBuilder
from aa_my_agent.rag.source_scanner import calculate_file_hash
from aa_my_agent.rag.structured_pdf_loader import StructuredPdfChunkLoader


def _snapshot(path: Path, root: Path) -> SourceSnapshot:
    stat = path.stat()
    return SourceSnapshot(
        relative_path=path.relative_to(root).as_posix(),
        file_hash=calculate_file_hash(path),
        file_size=stat.st_size,
        modified_at_ns=stat.st_mtime_ns,
        file_type=path.suffix.casefold(),
    )


class _PdfLoader:
    def __init__(self) -> None:
        self.calls = 0

    def load_snapshot(self, snapshot: SourceSnapshot) -> list[Document]:
        self.calls += 1
        return [
            Document(
                page_content="结构化PDF",
                metadata={"source": snapshot.relative_path},
            )
        ]


class _DocumentLoader:
    def __init__(self) -> None:
        self.calls = 0

    def load_snapshot(self, snapshot: SourceSnapshot) -> list[Document]:
        self.calls += 1
        return [
            Document(
                page_content="普通文档",
                metadata={"source": snapshot.relative_path},
            )
        ]


class _Splitter:
    def __init__(self) -> None:
        self.received: list[Document] = []

    def split_documents(self, documents: list[Document]) -> list[Document]:
        self.received = documents
        return documents


def test_pdf_routes_to_structured_loader() -> None:
    pdf_loader = _PdfLoader()
    document_loader = _DocumentLoader()
    splitter = _Splitter()
    builder = SourceChunkBuilder(
        document_loader=document_loader,  # type: ignore[arg-type]
        text_splitter=splitter,  # type: ignore[arg-type]
        pdf_loader=pdf_loader,  # type: ignore[arg-type]
    )
    snapshot = SourceSnapshot("sample.pdf", "a" * 64, 1, 1, ".pdf")

    chunks = builder.build(snapshot)

    assert chunks[0].page_content == "结构化PDF"
    assert pdf_loader.calls == 1
    assert document_loader.calls == 0
    assert splitter.received == []


def test_docx_keeps_specialized_loader_and_original_splitter() -> None:
    pdf_loader = _PdfLoader()
    document_loader = _DocumentLoader()
    splitter = _Splitter()
    builder = SourceChunkBuilder(
        document_loader=document_loader,  # type: ignore[arg-type]
        text_splitter=splitter,  # type: ignore[arg-type]
        pdf_loader=pdf_loader,  # type: ignore[arg-type]
    )
    snapshot = SourceSnapshot("sample.docx", "b" * 64, 1, 1, ".docx")

    chunks = builder.build(snapshot)

    assert chunks[0].metadata["content_type"] == "text"
    assert chunks[0].metadata["quality_status"] == "native_text"
    assert chunks[0].metadata["preserve_as_unit"] is False
    assert pdf_loader.calls == 0
    assert document_loader.calls == 1
    assert len(splitter.received) == 1


def test_structured_pdf_loader_reuses_hash_matching_cache(
    tmp_path: Path,
    monkeypatch,
) -> None:
    knowledge_dir = tmp_path / "knowledge"
    output_root = tmp_path / "cache"
    knowledge_dir.mkdir()
    pdf_path = knowledge_dir / "sample.pdf"
    pdf_path.write_bytes(b"minimal-pdf-fixture")
    snapshot = _snapshot(pdf_path, knowledge_dir)
    output_dir = get_output_dir(
        pdf_path,
        output_root=output_root,
        source_hash=snapshot.file_hash,
    )
    output_dir.mkdir(parents=True)
    (output_dir / "docling_document.json").write_text("{}", encoding="utf-8")
    (output_dir / "documents.jsonl").write_text("{}\n", encoding="utf-8")
    (output_dir / "manifest.json").write_text(
        json.dumps(
            {
                "source_hash": snapshot.file_hash,
                "requested_pages": "all",
                "structured_chunk_count": 1,
            }
        ),
        encoding="utf-8",
    )
    (output_dir / "chunks.jsonl").write_text(
        json.dumps(
            {
                "page_content": "缓存中的结构化PDF",
                "metadata": {
                    "source": snapshot.relative_path,
                    "file_hash": snapshot.file_hash,
                },
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    loader = StructuredPdfChunkLoader(
        knowledge_dir=knowledge_dir,
        output_root=output_root,
        fallback_cache_roots=(),
        describe_images=False,
    )
    monkeypatch.setattr(
        loader,
        "_parse",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("命中缓存时不应运行Docling")
        ),
    )

    chunks = loader.load_snapshot(snapshot)

    assert len(chunks) == 1
    assert chunks[0].page_content == "缓存中的结构化PDF"
