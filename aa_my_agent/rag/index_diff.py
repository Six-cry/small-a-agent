"""比较当前知识文件和已入库 Manifest 的差异。

这一层只负责分类，不加载文档、不生成 Embedding，也不修改 Chroma。
"""

from dataclasses import dataclass

from .manifest import (
    IndexManifest,
    create_default_manifest_store,
)
from .models import (
    IndexedSourceRecord,
    SourceSnapshot,
)
from .source_scanner import (
    create_default_source_scanner,
)


class IndexDiffError(RuntimeError):
    """索引状态无法被可靠比较。"""


@dataclass(frozen=True, slots=True)
class ChangedSource:
    """一份内容发生变化的知识文件。

    current:
        当前扫描得到的新文件状态。
    indexed:
        Manifest 中保存的旧入库状态。
    """

    current: SourceSnapshot
    indexed: IndexedSourceRecord


@dataclass(frozen=True, slots=True)
class IndexDiff:
    """知识目录和 Manifest 的比较结果。"""

    added: tuple[SourceSnapshot, ...]
    changed: tuple[ChangedSource, ...]
    deleted: tuple[
        IndexedSourceRecord,
        ...,
    ]
    unchanged: tuple[
        SourceSnapshot,
        ...,
    ]

    @property
    def has_changes(self) -> bool:
        """是否存在需要修改向量库的变化。"""
        return bool(
            self.added
            or self.changed
            or self.deleted
        )

    @property
    def total_current_files(self) -> int:
        """当前知识目录中的文件总数。"""
        return (
            len(self.added)
            + len(self.changed)
            + len(self.unchanged)
        )

    def summary(self) -> str:
        """返回适合人工查看的统计摘要。"""
        return (
            f"当前文件：{self.total_current_files}\n"
            f"新增文件：{len(self.added)}\n"
            f"修改文件：{len(self.changed)}\n"
            f"删除文件：{len(self.deleted)}\n"
            f"未变化文件：{len(self.unchanged)}"
        )


def validate_current_sources(
    current_sources: dict[
        str,
        SourceSnapshot,
    ],
) -> None:
    """检查扫描结果中的键和快照路径是否一致。"""
    for relative_path, snapshot in (
        current_sources.items()
    ):
        if (
            relative_path
            != snapshot.relative_path
        ):
            raise IndexDiffError(
                "扫描结果路径键与快照不一致："
                f"key={relative_path}，"
                f"snapshot={snapshot.relative_path}"
            )


def validate_manifest_sources(
    manifest: IndexManifest,
) -> None:
    """检查 Manifest 中的键和记录路径是否一致。"""
    for relative_path, record in (
        manifest.sources.items()
    ):
        if (
            relative_path
            != record.relative_path
        ):
            raise IndexDiffError(
                "Manifest 路径键与记录不一致："
                f"key={relative_path}，"
                f"record={record.relative_path}"
            )


def calculate_index_diff(
    current_sources: dict[
        str,
        SourceSnapshot,
    ],
    manifest: IndexManifest,
) -> IndexDiff:
    """比较当前文件状态与已入库状态。

    判断规则：

    当前有，Manifest 没有：
        added

    当前有，Manifest 有，Hash 不同：
        changed

    当前没有，Manifest 有：
        deleted

    当前有，Manifest 有，Hash 相同：
        unchanged
    """
    validate_current_sources(
        current_sources
    )
    validate_manifest_sources(manifest)

    current_paths = set(
        current_sources
    )
    indexed_paths = set(
        manifest.sources
    )

    added_paths = sorted(
        current_paths - indexed_paths
    )

    deleted_paths = sorted(
        indexed_paths - current_paths
    )

    common_paths = sorted(
        current_paths & indexed_paths
    )

    added = tuple(
        current_sources[path]
        for path in added_paths
    )

    deleted = tuple(
        manifest.sources[path]
        for path in deleted_paths
    )

    changed_items = []
    unchanged_items = []

    for path in common_paths:
        current = current_sources[path]
        indexed = manifest.sources[path]

        if (
            current.file_hash
            != indexed.file_hash
        ):
            changed_items.append(
                ChangedSource(
                    current=current,
                    indexed=indexed,
                )
            )
        else:
            unchanged_items.append(
                current
            )

    return IndexDiff(
        added=added,
        changed=tuple(changed_items),
        deleted=deleted,
        unchanged=tuple(
            unchanged_items
        ),
    )


def print_diff_details(
    diff: IndexDiff,
) -> None:
    """在命令行中显示完整比较结果。"""
    print(diff.summary())

    if diff.added:
        print("\n新增文件：")

        for snapshot in diff.added:
            print(
                f"  + {snapshot.relative_path}"
            )

    if diff.changed:
        print("\n修改文件：")

        for item in diff.changed:
            print(
                f"  * "
                f"{item.current.relative_path}"
            )
            print(
                "    旧 Hash："
                f"{item.indexed.file_hash}"
            )
            print(
                "    新 Hash："
                f"{item.current.file_hash}"
            )

    if diff.deleted:
        print("\n删除文件：")

        for record in diff.deleted:
            print(
                f"  - {record.relative_path}"
            )

    if diff.unchanged:
        print("\n未变化文件：")

        for snapshot in diff.unchanged:
            print(
                f"  = {snapshot.relative_path}"
            )


def main() -> None:
    """扫描当前文件并与 Manifest 比较。"""
    scanner = (
        create_default_source_scanner()
    )
    manifest_store = (
        create_default_manifest_store()
    )

    current_sources = scanner.scan()
    manifest = manifest_store.load()

    diff = calculate_index_diff(
        current_sources=current_sources,
        manifest=manifest,
    )

    print_diff_details(diff)


if __name__ == "__main__":
    main()