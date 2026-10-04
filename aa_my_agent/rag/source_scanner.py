"""知识源扫描器。

它只观察 rag/data/knowledge 当前有哪些文件，并为每个文件生成状态快照。
它不会创建 Embedding、不会访问 Chroma，也不会修改任何知识文件。
"""

import hashlib
from pathlib import Path
from typing import Iterable

from .models import SourceSnapshot
from .models import IndexedSourceRecord


HASH_BLOCK_SIZE = 1024 * 1024 #定义哈希计算时的分块大小为 1MB


class SourceScanError(RuntimeError):
    """知识目录或知识文件无法被可靠扫描。"""


def normalize_extensions(extensions: Iterable[str]) -> frozenset[str]:
    """把 txt、.TXT 等写法统一转换成 .txt。"""
    normalized = set()

    for extension in extensions:
        value = str(extension).strip().lower()

        if not value:
            continue

        if not value.startswith("."):
            value = f".{value}"

        normalized.add(value)

    if not normalized:
        raise ValueError("allowed_extensions 不能为空")

    return frozenset(normalized)


def calculate_file_hash(file_path: Path) -> str:
    """分块计算文件的 SHA-256，避免一次把大文件读入内存。"""
    digest = hashlib.sha256()

    try:
        with file_path.open("rb") as file:
            while block := file.read(HASH_BLOCK_SIZE):
                digest.update(block)
    except OSError as exc:
        raise SourceScanError(
            f"无法读取知识文件：{file_path}，错误：{exc}"
        ) from exc

    return digest.hexdigest()


class SourceScanner:
    """递归扫描知识目录，并生成以相对路径为键的文件快照。"""

    def __init__(
        self,
        knowledge_dir: Path,
        allowed_extensions: Iterable[str],
    ):
        self.knowledge_dir = Path(knowledge_dir).resolve()
        self.allowed_extensions = normalize_extensions(
            allowed_extensions
        )

    def scan(self) -> dict[str, SourceSnapshot]:
        """返回当前所有受支持知识文件的状态。

        返回值的键和 SourceSnapshot.relative_path 相同，方便后续直接与
        Manifest 中保存的路径做集合比较。
        """
        if not self.knowledge_dir.exists():
            return {}

        if not self.knowledge_dir.is_dir():
            raise SourceScanError(
                f"知识目录不是有效文件夹：{self.knowledge_dir}"
            )

        snapshots: dict[str, SourceSnapshot] = {}
        candidates = sorted(
            self.knowledge_dir.rglob("*"),
            key=lambda path: path.as_posix().casefold(),
        )

        for candidate in candidates:
            if not candidate.is_file():
                continue

            if candidate.suffix.lower() not in self.allowed_extensions:
                continue

            if candidate.is_symlink():
                raise SourceScanError(
                    f"知识目录暂不接受符号链接：{candidate}"
                )

            try:
                resolved_path = candidate.resolve(strict=True)
            except OSError as exc:
                raise SourceScanError(
                    f"无法解析知识文件路径：{candidate}，错误：{exc}"
                ) from exc

            if not resolved_path.is_relative_to(self.knowledge_dir):
                raise SourceScanError(
                    f"知识文件越过了知识目录边界：{candidate}"
                )

            relative_path = resolved_path.relative_to(
                self.knowledge_dir
            ).as_posix()

            try:
                stat_before = resolved_path.stat()# stat函数获取文件元数据
                file_hash = calculate_file_hash(resolved_path)
                stat_after = resolved_path.stat()
            except OSError as exc:
                raise SourceScanError(
                    f"无法读取知识文件状态：{candidate}，错误：{exc}"
                ) from exc

            if (
                stat_before.st_size != stat_after.st_size
                or stat_before.st_mtime_ns != stat_after.st_mtime_ns
            ):
                raise SourceScanError(
                    f"扫描期间文件发生变化，请稍后重试：{candidate}"
                )

            snapshots[relative_path] = SourceSnapshot(
                relative_path=relative_path,
                file_hash=file_hash,
                file_size=stat_after.st_size,
                modified_at_ns=stat_after.st_mtime_ns,
                file_type=resolved_path.suffix.lower(),
            )

        return snapshots

    def scan_metadata(
        self,
        indexed_sources: dict[str, IndexedSourceRecord],
    ) -> dict[str, SourceSnapshot]:
        """只读取文件名、大小和修改时间，供在线快速提醒使用。

        元数据与 Manifest 完全一致时复用已入库 Hash；不一致时放入一个
        不可能等于 SHA-256 的标记，让 IndexDiff 将其归为 changed。
        真正执行 sync 时仍调用 scan() 重新计算完整文件 Hash。
        """
        if not self.knowledge_dir.exists():
            return {}
        if not self.knowledge_dir.is_dir():
            raise SourceScanError(
                f"知识目录不是有效文件夹：{self.knowledge_dir}"
            )

        snapshots: dict[str, SourceSnapshot] = {}
        candidates = sorted(
            self.knowledge_dir.rglob("*"),
            key=lambda path: path.as_posix().casefold(),
        )
        for candidate in candidates:
            if not candidate.is_file():
                continue
            if candidate.suffix.lower() not in self.allowed_extensions:
                continue
            if candidate.is_symlink():
                raise SourceScanError(
                    f"知识目录暂不接受符号链接：{candidate}"
                )
            try:
                resolved_path = candidate.resolve(strict=True)
            except OSError as exc:
                raise SourceScanError(
                    f"无法解析知识文件路径：{candidate}，错误：{exc}"
                ) from exc
            if not resolved_path.is_relative_to(self.knowledge_dir):
                raise SourceScanError(
                    f"知识文件越过了知识目录边界：{candidate}"
                )

            relative_path = resolved_path.relative_to(
                self.knowledge_dir
            ).as_posix()
            try:
                stat = resolved_path.stat()
            except OSError as exc:
                raise SourceScanError(
                    f"无法读取知识文件状态：{candidate}，错误：{exc}"
                ) from exc

            file_type = resolved_path.suffix.lower()
            indexed = indexed_sources.get(relative_path)
            metadata_matches = (
                indexed is not None
                and indexed.file_size == stat.st_size
                and indexed.modified_at_ns == stat.st_mtime_ns
                and indexed.file_type == file_type
            )
            file_hash = (
                indexed.file_hash
                if metadata_matches
                else (
                    "metadata-change:"
                    f"{stat.st_size}:{stat.st_mtime_ns}:{file_type}"
                )
            )
            snapshots[relative_path] = SourceSnapshot(
                relative_path=relative_path,
                file_hash=file_hash,
                file_size=stat.st_size,
                modified_at_ns=stat.st_mtime_ns,
                file_type=file_type,
            )
        return snapshots


def create_default_source_scanner() -> SourceScanner:
    """使用 aa_my_agent 的正式配置创建扫描器。

    配置放在函数内部导入，使单元测试可以完全离线运行，不必准备模型和
    Embedding 的环境变量。
    """
    from ..config import KNOWLEDGE_DIR, allow_knowledge_file_type

    return SourceScanner(
        knowledge_dir=KNOWLEDGE_DIR,
        allowed_extensions=allow_knowledge_file_type,
    )


def main() -> None:
    """在命令行中展示扫描结果，仅用于人工检查。"""
    snapshots = create_default_source_scanner().scan()

    print(f"发现 {len(snapshots)} 个知识文件")

    for index, snapshot in enumerate(snapshots.values(), start=1):
        print(f"\n{index}. {snapshot.relative_path}")
        print(f"   类型：{snapshot.file_type}")
        print(f"   大小：{snapshot.file_size} bytes")
        print(f"   SHA-256：{snapshot.file_hash}")


if __name__ == "__main__":
    main()
