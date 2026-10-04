"""RAG Manifest 的读取、校验和原子保存。

Manifest 表示“已经成功进入 Chroma 的状态”，不是当前文件扫描结果。
"""

import json
from dataclasses import dataclass, field
from pathlib import Path

from .models import (
    IndexedSourceRecord,
    IndexSignature,
)


MANIFEST_SCHEMA_VERSION = 1


class ManifestError(RuntimeError):
    """Manifest 无法被安全读取或保存。"""


class ManifestCompatibilityError(ManifestError):
    """现有 Manifest 与当前索引配置不兼容。"""


@dataclass(slots=True)
class IndexManifest:
    """知识库已经成功入库的状态。"""

    index_signature: IndexSignature
    sources: dict[
        str,
        IndexedSourceRecord,
    ] = field(default_factory=dict)
    schema_version: int = (
        MANIFEST_SCHEMA_VERSION
    )

    def to_dict(self) -> dict:
        """转换为可以写入 JSON 的字典。"""
        return {
            "schema_version": (
                self.schema_version
            ),
            "index_signature": (
                self.index_signature.to_dict()
            ),
            "sources": {
                relative_path: record.to_dict()
                for relative_path, record
                in sorted(self.sources.items())
            },
        }

    @classmethod
    def empty(
        cls,
        index_signature: IndexSignature,
    ) -> "IndexManifest":
        """创建一个没有任何已入库文件的 Manifest。"""
        return cls(
            index_signature=index_signature,
            sources={},
        )

    @classmethod
    def from_dict(
        cls,
        data: dict,
    ) -> "IndexManifest":
        """从 JSON 字典恢复 Manifest。"""
        if not isinstance(data, dict):
            raise ManifestError(
                "Manifest 根节点必须是 JSON 对象"
            )

        schema_version = data.get(
            "schema_version"
        )

        if (
            schema_version
            != MANIFEST_SCHEMA_VERSION
        ):
            raise ManifestCompatibilityError(
                "不支持的 Manifest 版本："
                f"{schema_version}"
            )

        signature_data = data.get(
            "index_signature"
        )

        if not isinstance(
            signature_data,
            dict,
        ):
            raise ManifestError(
                "Manifest 缺少 index_signature"
            )

        sources_data = data.get("sources")

        if not isinstance(sources_data, dict):
            raise ManifestError(
                "Manifest sources 必须是对象"
            )

        sources = {}

        for relative_path, record_data in (
            sources_data.items()
        ):
            if not isinstance(record_data, dict):
                raise ManifestError(
                    "Manifest 文件记录格式错误："
                    f"{relative_path}"
                )

            record = (
                IndexedSourceRecord.from_dict(
                    record_data
                )
            )

            if (
                relative_path
                != record.relative_path
            ):
                raise ManifestError(
                    "Manifest 路径键和记录不一致："
                    f"{relative_path}"
                )

            sources[relative_path] = record

        return cls(
            schema_version=schema_version,
            index_signature=(
                IndexSignature.from_dict(
                    signature_data
                )
            ),
            sources=sources,
        )


class ManifestStore:
    """负责 Manifest 文件的读取和原子保存。"""

    def __init__(
        self,
        manifest_path: Path,
        expected_signature: IndexSignature,
    ):
        self.manifest_path = Path(
            manifest_path
        )
        self.expected_signature = (
            expected_signature
        )

    def load(self) -> IndexManifest:
        """读取 Manifest。

        文件不存在时返回空 Manifest，不会自动写入磁盘。
        """
        if not self.manifest_path.exists():
            return IndexManifest.empty(
                self.expected_signature
            )

        try:
            raw_text = (
                self.manifest_path.read_text(
                    encoding="utf-8"
                )
            )
            raw_data = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            raise ManifestError(
                "Manifest 不是有效 JSON："
                f"{self.manifest_path}"
            ) from exc
        except OSError as exc:
            raise ManifestError(
                "无法读取 Manifest："
                f"{self.manifest_path}，"
                f"错误：{exc}"
            ) from exc

        manifest = IndexManifest.from_dict(
            raw_data
        )

        if (
            manifest.index_signature
            != self.expected_signature
        ):
            raise ManifestCompatibilityError(
                "现有知识库索引配置与当前配置不一致，"
                "需要执行完整重建"
            )

        return manifest

    def save(
        self,
        manifest: IndexManifest,
    ) -> None:
        """以临时文件加原子替换的方式保存 Manifest。"""
        if (
            manifest.index_signature
            != self.expected_signature
        ):
            raise ManifestCompatibilityError(
                "拒绝保存配置不兼容的 Manifest"
            )

        self.manifest_path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        temporary_path = (
            self.manifest_path.with_name(
                self.manifest_path.name + ".tmp"
            )
        )

        json_text = json.dumps(
            manifest.to_dict(),
            ensure_ascii=False,
            indent=2,
        )

        try:
            temporary_path.write_text(
                json_text + "\n",
                encoding="utf-8",
            )
            temporary_path.replace(
                self.manifest_path
            )
        except OSError as exc:
            try:
                temporary_path.unlink(
                    missing_ok=True
                )
            except OSError:
                pass

            raise ManifestError(
                "无法保存 Manifest："
                f"{self.manifest_path}，"
                f"错误：{exc}"
            ) from exc


def create_default_index_signature() -> (
    IndexSignature
):
    """根据 aa_my_agent 当前配置生成索引签名。"""
    from ..config import (
        EMBEDDING_DIMENSION,
        EMBEDDING_MODEL,
        RAG_CHUNK_OVERLAP,
        RAG_CHUNK_SIZE,
        RAG_COLLECTION_NAME,
        RAG_FORMULA_MODE_SIGNATURE,
        RAG_IMAGE_PARENT_CHILD_VERSION,
        RAG_IMAGE_PROMPT_SIGNATURE,
        RAG_INDEX_PIPELINE_VERSION,
        RAG_PDF_PARSER_SIGNATURE,
        separators,
    )

    return IndexSignature(
        collection_name=RAG_COLLECTION_NAME,
        embedding_model=EMBEDDING_MODEL,
        embedding_dimension=(
            EMBEDDING_DIMENSION
        ),
        chunk_size=RAG_CHUNK_SIZE,
        chunk_overlap=RAG_CHUNK_OVERLAP,
        separators=tuple(separators),
        pipeline_version=RAG_INDEX_PIPELINE_VERSION,
        pdf_parser=RAG_PDF_PARSER_SIGNATURE,
        formula_mode=RAG_FORMULA_MODE_SIGNATURE,
        image_prompt_version=RAG_IMAGE_PROMPT_SIGNATURE,
        image_parent_child_version=RAG_IMAGE_PARENT_CHILD_VERSION,
    )


def create_default_manifest_store() -> (
    ManifestStore
):
    """使用正式项目配置创建 ManifestStore。"""
    from ..config import RAG_MANIFEST_PATH

    return ManifestStore(
        manifest_path=RAG_MANIFEST_PATH,
        expected_signature=(
            create_default_index_signature()
        ),
    )

def main() -> None:
    """创建或读取 Manifest，并显示当前状态。"""
    store = create_default_manifest_store()

    manifest_existed = (
        store.manifest_path.exists()
    )

    manifest = store.load()

    if not manifest_existed:
        store.save(manifest)

        print(
            "已创建 Manifest："
            f"{store.manifest_path}"
        )
    else:
        print(
            "已读取 Manifest："
            f"{store.manifest_path}"
        )

    print(
        "已记录入库文件数量："
        f"{len(manifest.sources)}"
    )

    print(
        "Collection："
        f"{manifest.index_signature.collection_name}"
    )

    print(
        "Embedding 模型："
        f"{manifest.index_signature.embedding_model}"
    )

    print(
        "Embedding 维度："
        f"{manifest.index_signature.embedding_dimension}"
    )

    print(
        "Chunk Size："
        f"{manifest.index_signature.chunk_size}"
    )

    print(
        "Chunk Overlap："
        f"{manifest.index_signature.chunk_overlap}"
    )


if __name__ == "__main__":
    main()
