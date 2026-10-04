"""RAG 索引协调管理层。

负责把下面这些模块串联起来：

SourceScanner
    ↓
IndexDiff
    ↓
SourceChunkBuilder
    ├─ PDF → Docling结构化缓存/解析
    └─ DOCX/TXT → DocumentLoader + TextSplitter
    ↓
VectorStoreService
    ↓
ManifestStore

支持两种索引方式：

1. sync()
   增量同步，只处理新增、修改和删除的文件。

2. rebuild()
   完整重建，清空原有 Collection 后重新处理所有文件。
"""

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone

from langchain_core.documents import Document

from .document_loader import (
    DocumentLoader,
    create_default_document_loader,
)
from .index_diff import (
    IndexDiff,
    calculate_index_diff,
    print_diff_details,
)
from .manifest import (
    IndexManifest,
    ManifestStore,
    create_default_manifest_store,
)
from .models import (
    IndexedSourceRecord,
    SourceSnapshot,
)
from .source_scanner import (
    SourceScanner,
    create_default_source_scanner,
)
from .source_chunk_builder import (
    SourceChunkBuilder,
    create_default_source_chunk_builder,
)
from .text_splitter import (
    TextSplitter,
    create_default_text_splitter,
)
from .vector_store import (
    VectorStoreService,
)


class IndexManagerError(RuntimeError):
    """知识索引同步失败。"""


class IndexStateError(IndexManagerError):
    """Manifest 和 Chroma 的状态不一致。"""


@dataclass(frozen=True, slots=True)
class IndexUpdateResult:
    """一次索引操作的最终结果。"""

    mode: str
    added_files: int
    changed_files: int
    deleted_files: int
    unchanged_files: int
    total_chunks: int

    def summary(self) -> str:
        """生成同步结果摘要。"""
        return (
            f"执行方式：{self.mode}\n"
            f"新增文件：{self.added_files}\n"
            f"修改文件：{self.changed_files}\n"
            f"删除文件：{self.deleted_files}\n"
            f"未变化文件：{self.unchanged_files}\n"
            f"当前 Chunk 总数：{self.total_chunks}"
        )


class IndexManager:
    """协调完成知识库索引更新。"""

    def __init__(
        self,
        scanner: SourceScanner,
        manifest_store: ManifestStore,
        document_loader: DocumentLoader,
        text_splitter: TextSplitter,
        vector_store: VectorStoreService,
        chunk_builder: SourceChunkBuilder | None = None,
    ):
        self.scanner = scanner
        self.manifest_store = manifest_store
        self.document_loader = (
            document_loader
        )
        self.text_splitter = text_splitter
        self.vector_store = vector_store
        self.chunk_builder = chunk_builder

    @staticmethod
    def _create_indexed_record(
        snapshot: SourceSnapshot,
        chunk_ids: tuple[str, ...],
    ) -> IndexedSourceRecord:
        """根据成功入库的结果生成 Manifest 记录。"""
        indexed_at = datetime.now(
            timezone.utc
        ).isoformat()

        return IndexedSourceRecord.from_snapshot(
            snapshot=snapshot,
            chunk_ids=chunk_ids,
            indexed_at=indexed_at,
        )

    @staticmethod
    def _get_chunk_ids(
        chunks: list[Document],
    ) -> tuple[str, ...]:
        """从 Chunk 元数据中读取 chunk_id。"""
        chunk_ids = []

        for chunk in chunks:
            chunk_id = chunk.metadata.get(
                "chunk_id"
            )

            if (
                not isinstance(chunk_id, str)
                or not chunk_id.strip()
            ):
                raise IndexManagerError(
                    "TextSplitter 生成的 Chunk "
                    "缺少有效 chunk_id"
                )

            chunk_ids.append(
                chunk_id.strip()
            )

        return tuple(chunk_ids)

    def _build_chunks(
        self,
        snapshot: SourceSnapshot,
    ) -> list[Document]:
        """加载并切分一个知识文件。"""
        if self.chunk_builder is not None:
            chunks = self.chunk_builder.build(snapshot)
        else:
            # 保留旧构造方式，供现有测试和外部自定义 IndexManager 使用。
            documents = self.document_loader.load_snapshot(snapshot)
            chunks = self.text_splitter.split_documents(documents)

        if not chunks:
            raise IndexManagerError(
                "知识文件切分后没有得到 Chunk："
                f"{snapshot.relative_path}"
            )

        # 再次确认所有 Chunk 都属于当前文件。
        for chunk in chunks:
            source = chunk.metadata.get(
                "source"
            )

            if source != snapshot.relative_path:
                raise IndexManagerError(
                    "Chunk 来源与当前知识文件不一致："
                    f"{snapshot.relative_path}"
                )

        return chunks

    def _write_chunks(
        self,
        snapshot: SourceSnapshot,
        chunks: list[Document],
    ) -> tuple[str, ...]:
        """将一个文件的所有 Chunk 写入 Chroma。

        如果写入失败，会尝试删除本次可能产生的 Chunk，
        避免留下不受 Manifest 管理的孤立向量。
        """
        candidate_ids = self._get_chunk_ids(
            chunks
        )

        try:
            return self.vector_store.add_chunks(
                chunks
            )
        except Exception as exc:
            try:
                self.vector_store.delete_chunks(
                    candidate_ids
                )
            except Exception:
                pass

            raise IndexManagerError(
                "知识文件向量化入库失败："
                f"{snapshot.relative_path}"
            ) from exc

    @staticmethod
    def _manifest_chunk_ids(
        manifest: IndexManifest,
    ) -> set[str]:
        """读取 Manifest 管理的全部 Chunk ID。"""
        chunk_ids = set()

        for record in manifest.sources.values():
            for chunk_id in record.chunk_ids:
                if chunk_id in chunk_ids:
                    raise IndexStateError(
                        "Manifest 中出现重复的 "
                        f"chunk_id：{chunk_id}"
                    )

                chunk_ids.add(chunk_id)

        return chunk_ids

    def validate_index_state(
        self,
        manifest: IndexManifest,
    ) -> None:
        """检查 Manifest 和 Chroma 是否完全一致。"""
        manifest_ids = (
            self._manifest_chunk_ids(
                manifest
            )
        )

        chroma_ids = set(
            self.vector_store.get_chunk_ids()
        )

        if manifest_ids == chroma_ids:
            return

        missing_ids = (
            manifest_ids - chroma_ids
        )

        extra_ids = (
            chroma_ids - manifest_ids
        )

        raise IndexStateError(
            "Manifest 和 Chroma 状态不一致。\n"
            f"Chroma 缺少：{len(missing_ids)} 个 Chunk\n"
            f"Chroma 多出：{len(extra_ids)} 个 Chunk\n"
            "请执行 rebuild 完整重建知识库。"
        )

    def status(
        self,
    ) -> tuple[IndexManifest, IndexDiff]:
        """只检查文件状态，不修改知识库。"""
        manifest = (
            self.manifest_store.load()
        )

        current_sources = (
            self.scanner.scan()
        )

        diff = calculate_index_diff(
            current_sources=current_sources,
            manifest=manifest,
        )

        return manifest, diff

    def _add_source(
        self,
        snapshot: SourceSnapshot,
        manifest: IndexManifest,
    ) -> None:
        """处理一个新增文件。"""
        print(
            "\n开始添加文件："
            f"{snapshot.relative_path}"
        )

        chunks = self._build_chunks(
            snapshot
        )

        chunk_ids = self._write_chunks(
            snapshot=snapshot,
            chunks=chunks,
        )

        record = (
            self._create_indexed_record(
                snapshot=snapshot,
                chunk_ids=chunk_ids,
            )
        )

        manifest.sources[
            snapshot.relative_path
        ] = record

        try:
            self.manifest_store.save(
                manifest
            )
        except Exception as exc:
            # Manifest 没有保存成功时，
            # 删除刚刚加入的向量。
            manifest.sources.pop(
                snapshot.relative_path,
                None,
            )

            try:
                self.vector_store.delete_chunks(
                    chunk_ids
                )
            except Exception:
                pass

            raise IndexManagerError(
                "新增文件已经完成向量化，"
                "但 Manifest 保存失败："
                f"{snapshot.relative_path}"
            ) from exc

        print(
            "新增文件完成："
            f"{snapshot.relative_path}，"
            f"写入 {len(chunk_ids)} 个 Chunk"
        )

    def _change_source(
        self,
        current: SourceSnapshot,
        indexed: IndexedSourceRecord,
        manifest: IndexManifest,
    ) -> None:
        """处理一个内容发生变化的文件。"""
        print(
            "\n开始更新文件："
            f"{current.relative_path}"
        )

        # 先生成并写入新 Chunk。
        # 因为 file_hash 发生变化，新旧 chunk_id 不同。
        chunks = self._build_chunks(
            current
        )

        new_chunk_ids = self._write_chunks(
            snapshot=current,
            chunks=chunks,
        )

        try:
            # 新 Chunk 成功后，再删除旧 Chunk。
            self.vector_store.delete_chunks(
                indexed.chunk_ids
            )
        except Exception as exc:
            # 旧 Chunk 删除失败时，回滚新 Chunk。
            try:
                self.vector_store.delete_chunks(
                    new_chunk_ids
                )
            except Exception:
                pass

            raise IndexManagerError(
                "新 Chunk 已生成，但旧 Chunk "
                "删除失败："
                f"{current.relative_path}"
            ) from exc

        new_record = (
            self._create_indexed_record(
                snapshot=current,
                chunk_ids=new_chunk_ids,
            )
        )

        manifest.sources[
            current.relative_path
        ] = new_record

        try:
            self.manifest_store.save(
                manifest
            )
        except Exception as exc:
            raise IndexManagerError(
                "文件向量已经更新，但 Manifest "
                "保存失败。请执行 rebuild："
                f"{current.relative_path}"
            ) from exc

        print(
            "更新文件完成："
            f"{current.relative_path}\n"
            f"删除旧 Chunk："
            f"{len(indexed.chunk_ids)} 个\n"
            f"写入新 Chunk："
            f"{len(new_chunk_ids)} 个"
        )

    def _delete_source(
        self,
        record: IndexedSourceRecord,
        manifest: IndexManifest,
    ) -> None:
        """清理一个已从知识目录删除的文件。"""
        print(
            "\n开始清理已删除文件："
            f"{record.relative_path}"
        )

        self.vector_store.delete_chunks(
            record.chunk_ids
        )

        manifest.sources.pop(
            record.relative_path,
            None,
        )

        try:
            self.manifest_store.save(
                manifest
            )
        except Exception as exc:
            raise IndexManagerError(
                "旧向量已删除，但 Manifest "
                "保存失败。请执行 rebuild："
                f"{record.relative_path}"
            ) from exc

        print(
            "删除记录完成："
            f"{record.relative_path}，"
            f"删除 {len(record.chunk_ids)} 个 Chunk"
        )

    def sync(self) -> IndexUpdateResult:
        """执行一次增量同步。

        新增文件：
            加载、切分、向量化、写入 Manifest。

        修改文件：
            生成新向量、删除旧向量、更新 Manifest。

        删除文件：
            删除对应旧向量、删除 Manifest 记录。

        未变化文件：
            完全跳过。
        """
        manifest = (
            self.manifest_store.load()
        )

        # 增量同步前必须保证两边状态一致。
        self.validate_index_state(
            manifest
        )

        current_sources = (
            self.scanner.scan()
        )

        diff = calculate_index_diff(
            current_sources=current_sources,
            manifest=manifest,
        )

        print("\n本次同步检测结果：")
        print_diff_details(diff)

        if not diff.has_changes:
            print(
                "\n知识库已经是最新状态，"
                "不需要重新向量化。"
            )

            return IndexUpdateResult(
                mode="sync",
                added_files=0,
                changed_files=0,
                deleted_files=0,
                unchanged_files=(
                    len(diff.unchanged)
                ),
                total_chunks=(
                    self.vector_store.count()
                ),
            )

        for snapshot in diff.added:
            self._add_source(
                snapshot=snapshot,
                manifest=manifest,
            )

        for changed_source in diff.changed:
            self._change_source(
                current=(
                    changed_source.current
                ),
                indexed=(
                    changed_source.indexed
                ),
                manifest=manifest,
            )

        for record in diff.deleted:
            self._delete_source(
                record=record,
                manifest=manifest,
            )

        # 所有操作结束后做最终一致性检查。
        self.validate_index_state(
            manifest
        )

        return IndexUpdateResult(
            mode="sync",
            added_files=len(diff.added),
            changed_files=len(diff.changed),
            deleted_files=len(diff.deleted),
            unchanged_files=(
                len(diff.unchanged)
            ),
            total_chunks=(
                self.vector_store.count()
            ),
        )

    def rebuild(self) -> IndexUpdateResult:
        """清空 Chroma 和 Manifest 后完整重建。"""
        current_sources = (
            self.scanner.scan()
        )

        print(
            "开始完整重建知识库，"
            f"共发现 {len(current_sources)} 个文件"
        )

        # 先完成文档读取和切分。
        # 确保文件无法读取时，不会提前删除旧知识库。
        prepared_chunks: dict[
            str,
            tuple[
                SourceSnapshot,
                list[Document],
            ],
        ] = {}

        for snapshot in (
            current_sources.values()
        ):
            print(
                "\n重建前检查文件："
                f"{snapshot.relative_path}"
            )

            chunks = self._build_chunks(
                snapshot
            )

            prepared_chunks[
                snapshot.relative_path
            ] = (
                snapshot,
                chunks,
            )

        # 所有文件都能正常读取和切分后，
        # 才真正清空旧 Collection。
        self.vector_store.reset_collection()

        manifest = IndexManifest.empty(
            self.manifest_store.expected_signature
        )

        self.manifest_store.save(
            manifest
        )

        for (
            snapshot,
            chunks,
        ) in prepared_chunks.values():
            print(
                "\n开始重建文件："
                f"{snapshot.relative_path}"
            )

            chunk_ids = self._write_chunks(
                snapshot=snapshot,
                chunks=chunks,
            )

            record = (
                self._create_indexed_record(
                    snapshot=snapshot,
                    chunk_ids=chunk_ids,
                )
            )

            manifest.sources[
                snapshot.relative_path
            ] = record

            self.manifest_store.save(
                manifest
            )

            print(
                "重建文件完成："
                f"{snapshot.relative_path}，"
                f"{len(chunk_ids)} 个 Chunk"
            )

        self.validate_index_state(
            manifest
        )

        return IndexUpdateResult(
            mode="rebuild",
            added_files=(
                len(current_sources)
            ),
            changed_files=0,
            deleted_files=0,
            unchanged_files=0,
            total_chunks=(
                self.vector_store.count()
            ),
        )


def create_default_index_manager(
) -> IndexManager:
    """根据正式项目配置创建 IndexManager。"""
    document_loader = create_default_document_loader()
    text_splitter = create_default_text_splitter()
    return IndexManager(
        scanner=(
            create_default_source_scanner()
        ),
        manifest_store=(
            create_default_manifest_store()
        ),
        document_loader=document_loader,
        text_splitter=text_splitter,
        vector_store=VectorStoreService(),
        chunk_builder=create_default_source_chunk_builder(
            document_loader=document_loader,
            text_splitter=text_splitter,
        ),
    )


def main() -> None:
    """提供命令行索引管理入口。"""
    parser = argparse.ArgumentParser(
        description="管理 RAG 知识库索引"
    )

    parser.add_argument(
        "command",
        nargs="?",
        default="status",
        choices=(
            "status",
            "sync",
            "rebuild",
        ),
        help=(
            "status：只查看状态；"
            "sync：增量同步；"
            "rebuild：完整重建"
        ),
    )

    args = parser.parse_args()

    manager = (
        create_default_index_manager()
    )

    if args.command == "status":
        manifest, diff = manager.status()

        print("\n文件变化状态：")
        print_diff_details(diff)

        print(
            "\nManifest 已记录文件："
            f"{len(manifest.sources)}"
        )

        print(
            "Chroma 当前 Chunk："
            f"{manager.vector_store.count()}"
        )

        try:
            manager.validate_index_state(
                manifest
            )
        except IndexStateError as exc:
            print(
                "\n索引状态：不一致"
            )
            print(exc)
        else:
            print(
                "\n索引状态：一致"
            )

        return

    if args.command == "sync":
        result = manager.sync()
    else:
        result = manager.rebuild()

    print("\n索引操作完成：")
    print(result.summary())


if __name__ == "__main__":
    main()
