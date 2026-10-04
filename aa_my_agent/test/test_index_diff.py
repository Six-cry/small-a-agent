"""IndexDiff 的离线测试。"""

from aa_my_agent.rag.index_diff import (
    ChangedSource,
    calculate_index_diff,
)
from aa_my_agent.rag.manifest import (
    IndexManifest,
)
from aa_my_agent.rag.models import (
    IndexedSourceRecord,
    IndexSignature,
    SourceSnapshot,
)


def create_signature() -> IndexSignature:
    return IndexSignature(
        collection_name="test",
        embedding_model="test-model",
        embedding_dimension=3,
        chunk_size=100,
        chunk_overlap=10,
        separators=("\n", ""),
    )


def create_snapshot(
    path: str,
    file_hash: str,
) -> SourceSnapshot:
    return SourceSnapshot(
        relative_path=path,
        file_hash=file_hash,
        file_size=100,
        modified_at_ns=123,
        file_type=".txt",
    )


def create_record(
    path: str,
    file_hash: str,
) -> IndexedSourceRecord:
    return IndexedSourceRecord(
        relative_path=path,
        file_hash=file_hash,
        file_size=100,
        modified_at_ns=123,
        file_type=".txt",
        chunk_ids=(
            f"{path}-chunk-1",
        ),
        indexed_at="2026-07-16",
    )


def test_all_files_are_added_when_manifest_empty():
    current = {
        "a.txt": create_snapshot(
            "a.txt",
            "hash-a",
        ),
        "b.txt": create_snapshot(
            "b.txt",
            "hash-b",
        ),
    }

    manifest = IndexManifest.empty(
        create_signature()
    )

    diff = calculate_index_diff(
        current,
        manifest,
    )

    assert len(diff.added) == 2
    assert diff.changed == ()
    assert diff.deleted == ()
    assert diff.unchanged == ()
    assert diff.has_changes is True


def test_unchanged_file():
    current = {
        "a.txt": create_snapshot(
            "a.txt",
            "same-hash",
        )
    }

    manifest = IndexManifest(
        index_signature=create_signature(),
        sources={
            "a.txt": create_record(
                "a.txt",
                "same-hash",
            )
        },
    )

    diff = calculate_index_diff(
        current,
        manifest,
    )

    assert diff.added == ()
    assert diff.changed == ()
    assert diff.deleted == ()
    assert len(diff.unchanged) == 1
    assert diff.has_changes is False


def test_changed_file():
    current = {
        "a.txt": create_snapshot(
            "a.txt",
            "new-hash",
        )
    }

    old_record = create_record(
        "a.txt",
        "old-hash",
    )

    manifest = IndexManifest(
        index_signature=create_signature(),
        sources={
            "a.txt": old_record
        },
    )

    diff = calculate_index_diff(
        current,
        manifest,
    )

    assert len(diff.changed) == 1

    changed = diff.changed[0]

    assert isinstance(
        changed,
        ChangedSource,
    )
    assert (
        changed.current.file_hash
        == "new-hash"
    )
    assert (
        changed.indexed.file_hash
        == "old-hash"
    )


def test_deleted_file():
    current = {}

    old_record = create_record(
        "deleted.txt",
        "old-hash",
    )

    manifest = IndexManifest(
        index_signature=create_signature(),
        sources={
            "deleted.txt": old_record
        },
    )

    diff = calculate_index_diff(
        current,
        manifest,
    )

    assert len(diff.deleted) == 1
    assert (
        diff.deleted[0].relative_path
        == "deleted.txt"
    )


def test_mixed_changes():
    current = {
        "added.txt": create_snapshot(
            "added.txt",
            "hash-added",
        ),
        "changed.txt": create_snapshot(
            "changed.txt",
            "new-hash",
        ),
        "same.txt": create_snapshot(
            "same.txt",
            "same-hash",
        ),
    }

    manifest = IndexManifest(
        index_signature=create_signature(),
        sources={
            "changed.txt": create_record(
                "changed.txt",
                "old-hash",
            ),
            "same.txt": create_record(
                "same.txt",
                "same-hash",
            ),
            "deleted.txt": create_record(
                "deleted.txt",
                "deleted-hash",
            ),
        },
    )

    diff = calculate_index_diff(
        current,
        manifest,
    )

    assert [
        item.relative_path
        for item in diff.added
    ] == ["added.txt"]

    assert [
        item.current.relative_path
        for item in diff.changed
    ] == ["changed.txt"]

    assert [
        item.relative_path
        for item in diff.deleted
    ] == ["deleted.txt"]

    assert [
        item.relative_path
        for item in diff.unchanged
    ] == ["same.txt"]