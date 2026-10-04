import json

from aa_my_agent.eval.rag.batch_parse_pdfs import (
    _load_completed_summary,
    discover_pdfs,
)
from aa_my_agent.rag.multimodal_parser import get_output_dir


def test_discover_pdfs_is_recursive_and_case_insensitive(tmp_path) -> None:
    nested = tmp_path / "nested"
    nested.mkdir()
    (tmp_path / "b.pdf").write_bytes(b"b")
    (nested / "A.PDF").write_bytes(b"a")
    (tmp_path / "ignore.txt").write_text("x", encoding="utf-8")

    paths = discover_pdfs(tmp_path)

    assert [path.relative_to(tmp_path).as_posix() for path in paths] == [
        "b.pdf",
        "nested/A.PDF",
    ]


def test_completed_cache_requires_all_structured_outputs(tmp_path) -> None:
    knowledge = tmp_path / "knowledge"
    output_root = tmp_path / "outputs"
    knowledge.mkdir()
    pdf = knowledge / "sample.pdf"
    pdf.write_bytes(b"sample")
    source_hash = "a" * 64
    output_dir = get_output_dir(
        pdf,
        output_root=output_root,
        source_hash=source_hash,
    )
    output_dir.mkdir(parents=True)
    (output_dir / "manifest.json").write_text(
        json.dumps(
            {
                "source_hash": source_hash,
                "requested_pages": "all",
                "structured_chunk_count": 3,
            }
        ),
        encoding="utf-8",
    )

    assert _load_completed_summary(
        pdf,
        output_root=output_root,
        source_hash=source_hash,
    ) is None

    for name in ("docling_document.json", "documents.jsonl", "chunks.jsonl"):
        (output_dir / name).write_text("{}", encoding="utf-8")

    assert _load_completed_summary(
        pdf,
        output_root=output_root,
        source_hash=source_hash,
    ) is not None
