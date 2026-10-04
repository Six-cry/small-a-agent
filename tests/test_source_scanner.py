"""知识源扫描器的离线测试。"""

import hashlib

from aa_my_agent.rag.models import SourceSnapshot
from aa_my_agent.rag.models import IndexedSourceRecord
from aa_my_agent.rag.source_scanner import (
    SourceScanner,
    calculate_file_hash,
    normalize_extensions,
)


def test_normalize_extensions():
    assert normalize_extensions(["txt", ".PDF", " txt "]) == {
        ".txt",
        ".pdf",
    }


def test_scan_supported_files_recursively(tmp_path):
    nested = tmp_path / "routing"
    nested.mkdir()

    txt_file = tmp_path / "notes.txt"
    pdf_file = nested / "paper.PDF"
    ignored_file = tmp_path / "secret.env"

    txt_file.write_text("RAG knowledge", encoding="utf-8")
    pdf_file.write_bytes(b"fake pdf content")
    ignored_file.write_text("SECRET=value", encoding="utf-8")

    scanner = SourceScanner(
        knowledge_dir=tmp_path,
        allowed_extensions={".txt", ".pdf"},
    )

    snapshots = scanner.scan()

    assert list(snapshots) == [
        "notes.txt",
        "routing/paper.PDF",
    ]
    assert isinstance(snapshots["notes.txt"], SourceSnapshot)
    assert snapshots["notes.txt"].file_type == ".txt"
    assert snapshots["routing/paper.PDF"].file_type == ".pdf"
    assert "secret.env" not in snapshots


def test_hash_changes_when_content_changes(tmp_path):
    source = tmp_path / "knowledge.txt"
    source.write_text("version one", encoding="utf-8")

    first_hash = calculate_file_hash(source)

    source.write_text("version two", encoding="utf-8")
    second_hash = calculate_file_hash(source)

    assert first_hash != second_hash


def test_file_hash_is_sha256(tmp_path):
    source = tmp_path / "knowledge.txt"
    content = b"known content"
    source.write_bytes(content)

    assert calculate_file_hash(source) == hashlib.sha256(
        content
    ).hexdigest()


def test_missing_knowledge_directory_is_empty(tmp_path):
    scanner = SourceScanner(
        knowledge_dir=tmp_path / "missing",
        allowed_extensions={".txt", ".pdf"},
    )

    assert scanner.scan() == {}


def test_metadata_scan_reuses_hash_without_reading_file_content(tmp_path):
    source = tmp_path / "knowledge.txt"
    source.write_text("indexed content", encoding="utf-8")
    full = SourceScanner(tmp_path, {".txt"}).scan()["knowledge.txt"]
    record = IndexedSourceRecord.from_snapshot(
        full,
        chunk_ids=("a" * 64,),
        indexed_at="2026-09-28T00:00:00+08:00",
    )

    metadata = SourceScanner(tmp_path, {".txt"}).scan_metadata(
        {"knowledge.txt": record}
    )

    assert metadata["knowledge.txt"].file_hash == full.file_hash


def test_metadata_scan_marks_size_or_mtime_change_without_full_hash(tmp_path):
    source = tmp_path / "knowledge.txt"
    source.write_text("old", encoding="utf-8")
    full = SourceScanner(tmp_path, {".txt"}).scan()["knowledge.txt"]
    record = IndexedSourceRecord.from_snapshot(
        full,
        chunk_ids=("a" * 64,),
        indexed_at="2026-09-28T00:00:00+08:00",
    )
    source.write_text("new content", encoding="utf-8")

    metadata = SourceScanner(tmp_path, {".txt"}).scan_metadata(
        {"knowledge.txt": record}
    )

    assert metadata["knowledge.txt"].file_hash.startswith("metadata-change:")
    assert metadata["knowledge.txt"].file_hash != full.file_hash
