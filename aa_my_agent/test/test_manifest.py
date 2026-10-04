"""RAG Manifest 的离线测试。"""

import json
from dataclasses import replace

import pytest

from aa_my_agent.rag.manifest import (
    IndexManifest,
    ManifestCompatibilityError,
    ManifestError,
    ManifestStore,
)
from aa_my_agent.rag.models import (
    IndexedSourceRecord,
    IndexSignature,
)


def create_signature(
    dimension: int = 1024,
) -> IndexSignature:
    return IndexSignature(
        collection_name="test_collection",
        embedding_model="test_embedding",
        embedding_dimension=dimension,
        chunk_size=500,
        chunk_overlap=50,
        separators=(
            "\n\n",
            "\n",
            "。",
            "",
        ),
    )


def test_missing_manifest_returns_empty(
    tmp_path,
):
    signature = create_signature()
    store = ManifestStore(
        manifest_path=(
            tmp_path / "manifest.json"
        ),
        expected_signature=signature,
    )

    manifest = store.load()

    assert manifest.sources == {}
    assert (
        manifest.index_signature
        == signature
    )


def test_manifest_save_and_load(
    tmp_path,
):
    signature = create_signature()
    manifest_path = (
        tmp_path / "manifest.json"
    )

    store = ManifestStore(
        manifest_path=manifest_path,
        expected_signature=signature,
    )

    record = IndexedSourceRecord(
        relative_path="paper.pdf",
        file_hash="abc123",
        file_size=100,
        modified_at_ns=123456,
        file_type=".pdf",
        chunk_ids=(
            "chunk-1",
            "chunk-2",
        ),
        indexed_at=(
            "2026-07-16T12:00:00+08:00"
        ),
    )

    manifest = IndexManifest(
        index_signature=signature,
        sources={
            record.relative_path: record
        },
    )

    store.save(manifest)
    loaded = store.load()

    assert loaded == manifest
    assert manifest_path.exists()
    assert not (
        tmp_path / "manifest.json.tmp"
    ).exists()


def test_invalid_json_raises_error(
    tmp_path,
):
    manifest_path = (
        tmp_path / "manifest.json"
    )
    manifest_path.write_text(
        "{invalid json",
        encoding="utf-8",
    )

    store = ManifestStore(
        manifest_path=manifest_path,
        expected_signature=(
            create_signature()
        ),
    )

    with pytest.raises(ManifestError):
        store.load()


def test_incompatible_signature_raises(
    tmp_path,
):
    old_signature = create_signature(
        dimension=1024
    )
    new_signature = create_signature(
        dimension=1536
    )

    manifest_path = (
        tmp_path / "manifest.json"
    )

    old_store = ManifestStore(
        manifest_path=manifest_path,
        expected_signature=old_signature,
    )
    old_store.save(
        IndexManifest.empty(old_signature)
    )

    new_store = ManifestStore(
        manifest_path=manifest_path,
        expected_signature=new_signature,
    )

    with pytest.raises(
        ManifestCompatibilityError
    ):
        new_store.load()


def test_pipeline_version_change_requires_rebuild(tmp_path) -> None:
    old_signature = create_signature()
    new_signature = replace(
        old_signature,
        pipeline_version="docling-parent-child-v2",
        image_parent_child_version="image-parent-child-v1",
    )
    manifest_path = tmp_path / "manifest.json"
    ManifestStore(manifest_path, old_signature).save(
        IndexManifest.empty(old_signature)
    )

    with pytest.raises(ManifestCompatibilityError):
        ManifestStore(manifest_path, new_signature).load()


def test_legacy_signature_fields_load_as_current_655_chunk_version() -> None:
    payload = create_signature().to_dict()
    for field_name in (
        "pipeline_version",
        "pdf_parser",
        "formula_mode",
        "image_prompt_version",
        "image_parent_child_version",
    ):
        payload.pop(field_name)

    restored = IndexSignature.from_dict(payload)

    assert restored.pipeline_version == "docling-structured-v1"
    assert restored.image_parent_child_version == "disabled-v1"


def test_path_key_must_match_record(
    tmp_path,
):
    manifest_path = (
        tmp_path / "manifest.json"
    )

    raw_data = {
        "schema_version": 1,
        "index_signature": (
            create_signature().to_dict()
        ),
        "sources": {
            "wrong-name.pdf": {
                "relative_path": (
                    "actual-name.pdf"
                ),
                "file_hash": "abc",
                "file_size": 100,
                "modified_at_ns": 123,
                "file_type": ".pdf",
                "chunk_ids": ["chunk-1"],
                "indexed_at": (
                    "2026-07-16"
                ),
            }
        },
    }

    manifest_path.write_text(
        json.dumps(
            raw_data,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    store = ManifestStore(
        manifest_path=manifest_path,
        expected_signature=(
            create_signature()
        ),
    )

    with pytest.raises(ManifestError):
        store.load()
