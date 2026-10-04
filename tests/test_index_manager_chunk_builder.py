from langchain_core.documents import Document
import pytest

from aa_my_agent.rag.index_manager import IndexManager
from aa_my_agent.rag.manifest import IndexManifest
from aa_my_agent.rag.models import IndexedSourceRecord, IndexSignature, SourceSnapshot


SIGNATURE = IndexSignature(
    collection_name="test",
    embedding_model="test-embedding",
    embedding_dimension=3,
    chunk_size=100,
    chunk_overlap=10,
    separators=("\n",),
)


class _Scanner:
    def __init__(self, snapshots: dict[str, SourceSnapshot]) -> None:
        self.snapshots = snapshots

    def scan(self) -> dict[str, SourceSnapshot]:
        return self.snapshots


class _ManifestStore:
    def __init__(self, manifest: IndexManifest) -> None:
        self.manifest = manifest
        self.expected_signature = SIGNATURE

    def load(self) -> IndexManifest:
        return self.manifest

    def save(self, manifest: IndexManifest) -> None:
        self.manifest = manifest


class _VectorStore:
    def __init__(self, ids: set[str] | None = None) -> None:
        self.ids = set(ids or set())

    def get_chunk_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self.ids))

    def add_chunks(self, chunks: list[Document]) -> tuple[str, ...]:
        ids = tuple(str(chunk.metadata["chunk_id"]) for chunk in chunks)
        self.ids.update(ids)
        return ids

    def delete_chunks(self, chunk_ids) -> None:
        self.ids.difference_update(chunk_ids)

    def count(self) -> int:
        return len(self.ids)


class _ChunkBuilder:
    def __init__(self, chunks: list[Document] | None = None, error=None) -> None:
        self.chunks = chunks or []
        self.error = error
        self.calls: list[SourceSnapshot] = []

    def build(self, snapshot: SourceSnapshot) -> list[Document]:
        self.calls.append(snapshot)
        if self.error is not None:
            raise self.error
        return self.chunks


def _manager(scanner, manifest_store, vector_store, builder) -> IndexManager:
    return IndexManager(
        scanner=scanner,
        manifest_store=manifest_store,
        document_loader=object(),  # type: ignore[arg-type]
        text_splitter=object(),  # type: ignore[arg-type]
        vector_store=vector_store,  # type: ignore[arg-type]
        chunk_builder=builder,  # type: ignore[arg-type]
    )


def test_sync_uses_chunk_builder_for_new_pdf() -> None:
    snapshot = SourceSnapshot("new.pdf", "a" * 64, 10, 20, ".pdf")
    chunk = Document(
        page_content="结构化内容",
        metadata={"source": "new.pdf", "chunk_id": "c" * 64},
    )
    manifest_store = _ManifestStore(IndexManifest.empty(SIGNATURE))
    vector_store = _VectorStore()
    builder = _ChunkBuilder([chunk])
    manager = _manager(
        _Scanner({"new.pdf": snapshot}),
        manifest_store,
        vector_store,
        builder,
    )

    result = manager.sync()

    assert result.added_files == 1
    assert result.total_chunks == 1
    assert len(builder.calls) == 1
    assert vector_store.ids == {"c" * 64}
    assert manifest_store.manifest.sources["new.pdf"].chunk_ids == ("c" * 64,)


def test_changed_pdf_keeps_old_index_when_builder_fails() -> None:
    old_snapshot = SourceSnapshot("changed.pdf", "a" * 64, 10, 20, ".pdf")
    new_snapshot = SourceSnapshot("changed.pdf", "b" * 64, 11, 21, ".pdf")
    old_id = "d" * 64
    old_record = IndexedSourceRecord.from_snapshot(
        snapshot=old_snapshot,
        chunk_ids=(old_id,),
        indexed_at="2026-09-19T00:00:00+00:00",
    )
    manifest = IndexManifest(
        index_signature=SIGNATURE,
        sources={"changed.pdf": old_record},
    )
    manifest_store = _ManifestStore(manifest)
    vector_store = _VectorStore({old_id})
    builder = _ChunkBuilder(error=RuntimeError("Docling failed"))
    manager = _manager(
        _Scanner({"changed.pdf": new_snapshot}),
        manifest_store,
        vector_store,
        builder,
    )

    with pytest.raises(RuntimeError, match="Docling failed"):
        manager.sync()

    assert vector_store.ids == {old_id}
    assert manifest_store.manifest.sources["changed.pdf"] == old_record
