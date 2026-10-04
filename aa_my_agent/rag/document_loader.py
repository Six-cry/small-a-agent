"""知识文档加载层。

主要职责：

1. 根据 SourceSnapshot 找到对应知识文件。
2. 根据文件类型选择 PDF、TXT 或 DOCX 加载器。
3. 验证文件在扫描后没有发生变化。
4. 将文件转换为 LangChain Document。
5. 给 Document 添加统一、可靠的来源元数据。

这个模块不负责：

- 文本切分
- Embedding
- Chroma
- Manifest 更新
- 聊天模型调用
"""

from pathlib import Path
from typing import Iterable

from langchain_community.document_loaders import (
    Docx2txtLoader,
    PyPDFLoader,
    TextLoader,
)
from langchain_core.documents import Document

from .models import SourceSnapshot
from .source_scanner import (
    SourceScanner,
    calculate_file_hash,
    normalize_extensions,
)


class DocumentLoadError(RuntimeError):
    """知识文件无法被可靠加载。"""


class DocumentLoader:
    """根据 SourceSnapshot 加载知识文件。"""

    def __init__(
        self,
        knowledge_dir: Path | None = None,
        allowed_extensions: (
            Iterable[str] | None
        ) = None,
    ):
        # 没有显式传入配置时，使用正式项目配置。
        # 测试时可以传入临时目录，避免依赖正式知识库。
        if (
            knowledge_dir is None
            or allowed_extensions is None
        ):
            from ..config import (
                KNOWLEDGE_DIR,
                allow_knowledge_file_type,
            )

            if knowledge_dir is None:
                knowledge_dir = KNOWLEDGE_DIR

            if allowed_extensions is None:
                allowed_extensions = (
                    allow_knowledge_file_type
                )

        self.knowledge_dir = Path(
            knowledge_dir
        ).resolve()

        self.allowed_extensions = (
            normalize_extensions(
                allowed_extensions
            )
        )

        self.scanner = SourceScanner(
            knowledge_dir=self.knowledge_dir,
            allowed_extensions=(
                self.allowed_extensions
            ),
        )

    def _resolve_snapshot_path(
        self,
        snapshot: SourceSnapshot,
    ) -> Path:
        """把快照中的相对路径转换成安全的绝对路径。"""
        candidate = (
            self.knowledge_dir
            / snapshot.relative_path
        )

        if candidate.is_symlink():
            raise DocumentLoadError(
                "知识文件暂不接受符号链接："
                f"{snapshot.relative_path}"
            )

        try:
            resolved_path = candidate.resolve(
                strict=True
            )
        except OSError as exc:
            raise DocumentLoadError(
                "知识文件不存在或无法访问："
                f"{snapshot.relative_path}，"
                f"错误：{exc}"
            ) from exc

        if not resolved_path.is_relative_to(
            self.knowledge_dir
        ):
            raise DocumentLoadError(
                "知识文件越过知识目录边界："
                f"{snapshot.relative_path}"
            )

        file_type = (
            resolved_path.suffix.lower()
        )

        if (
            file_type
            not in self.allowed_extensions
        ):
            raise DocumentLoadError(
                "不支持的知识文件类型："
                f"{file_type}"
            )

        if file_type != snapshot.file_type:
            raise DocumentLoadError(
                "知识文件类型与扫描快照不一致："
                f"{snapshot.relative_path}"
            )

        return resolved_path

    def _get_loader(
        self,
        file_path: Path,
    ):
        """根据扩展名创建 LangChain Loader。"""
        file_type = file_path.suffix.lower()

        if file_type == ".pdf":
            return PyPDFLoader(
                str(file_path)
            )

        if file_type == ".txt":
            return TextLoader(
                str(file_path),
                encoding="utf-8",
                autodetect_encoding=False,
            )

        if file_type == ".docx":
            return Docx2txtLoader(
                str(file_path)
            )

        raise DocumentLoadError(
            "没有可用的文档加载器："
            f"{file_type}"
        )

    def _validate_snapshot(
        self,
        file_path: Path,
        snapshot: SourceSnapshot,
    ) -> None:
        """确认文件仍然与扫描快照一致。"""
        try:
            stat = file_path.stat()
        except OSError as exc:
            raise DocumentLoadError(
                "无法读取知识文件状态："
                f"{snapshot.relative_path}，"
                f"错误：{exc}"
            ) from exc

        # 大小或修改时间变化，说明扫描后文件被改动。
        if (
            stat.st_size
            != snapshot.file_size
            or stat.st_mtime_ns
            != snapshot.modified_at_ns
        ):
            raise DocumentLoadError(
                "知识文件在扫描后发生变化，"
                "请重新扫描后再入库："
                f"{snapshot.relative_path}"
            )

        current_hash = calculate_file_hash(
            file_path
        )

        if (
            current_hash
            != snapshot.file_hash
        ):
            raise DocumentLoadError(
                "知识文件 Hash 与扫描快照不一致，"
                "请重新扫描后再入库："
                f"{snapshot.relative_path}"
            )

    def _attach_metadata(
        self,
        documents: list[Document],
        snapshot: SourceSnapshot,
    ) -> list[Document]:
        """给每个 Document 添加统一来源信息。"""
        file_name = Path(
            snapshot.relative_path
        ).name

        for document in documents:
            # 使用赋值，而不是 setdefault。
            # LangChain Loader 通常会保存绝对 source，
            # 我们需要强制改成相对路径。
            document.metadata["source"] = (
                snapshot.relative_path
            )

            document.metadata["file_name"] = (
                file_name
            )

            document.metadata["file_type"] = (
                snapshot.file_type
            )

            document.metadata["file_hash"] = (
                snapshot.file_hash
            )

            document.metadata["file_size"] = (
                snapshot.file_size
            )

        return documents

    def load_snapshot(
        self,
        snapshot: SourceSnapshot,
    ) -> list[Document]:
        """加载一份扫描快照对应的知识文件。"""
        file_path = (
            self._resolve_snapshot_path(
                snapshot
            )
        )

        self._validate_snapshot(
            file_path=file_path,
            snapshot=snapshot,
        )

        loader = self._get_loader(
            file_path
        )

        try:
            documents = loader.load()
        except Exception as exc:
            raise DocumentLoadError(
                "知识文件加载失败："
                f"{snapshot.relative_path}，"
                f"错误：{exc}"
            ) from exc

        if not documents:
            raise DocumentLoadError(
                "知识文件没有读取到有效内容："
                f"{snapshot.relative_path}"
            )

        documents = self._attach_metadata(
            documents=documents,
            snapshot=snapshot,
        )

        print(
            "成功读取："
            f"{snapshot.relative_path}，"
            f"得到 {len(documents)} 个 Document"
        )

        return documents

    def load_file(
        self,
        file_path: Path,
    ) -> list[Document]:
        """兼容原有接口：根据路径加载一个文件。

        新的同步流程应优先调用 load_snapshot()。
        """
        try:
            resolved_path = Path(
                file_path
            ).resolve(strict=True)
        except OSError as exc:
            raise DocumentLoadError(
                "文件不存在或无法访问："
                f"{file_path}，"
                f"错误：{exc}"
            ) from exc

        if not resolved_path.is_relative_to(
            self.knowledge_dir
        ):
            raise DocumentLoadError(
                "文件不在知识目录中："
                f"{file_path}"
            )

        relative_path = (
            resolved_path.relative_to(
                self.knowledge_dir
            ).as_posix()
        )

        snapshots = self.scanner.scan()
        snapshot = snapshots.get(
            relative_path
        )

        if snapshot is None:
            raise DocumentLoadError(
                "文件未被知识源扫描器识别："
                f"{relative_path}"
            )

        return self.load_snapshot(
            snapshot
        )

    def load_all(
        self,
    ) -> list[Document]:
        """递归加载当前所有知识文件。

        保留这个接口用于人工测试和旧代码兼容。
        新的增量同步流程不会调用 load_all()，
        而是根据 IndexDiff 逐个调用 load_snapshot()。
        """
        snapshots = self.scanner.scan()
        all_documents: list[Document] = []

        for snapshot in snapshots.values():
            documents = self.load_snapshot(
                snapshot
            )

            all_documents.extend(
                documents
            )

        return all_documents


def create_default_document_loader(
) -> DocumentLoader:
    """使用项目正式配置创建 Loader。"""
    from ..config import (
        KNOWLEDGE_DIR,
        allow_knowledge_file_type,
    )

    return DocumentLoader(
        knowledge_dir=KNOWLEDGE_DIR,
        allowed_extensions=(
            allow_knowledge_file_type
        ),
    )


def main() -> None:
    """人工检查所有知识文件是否能正常加载。"""
    loader = (
        create_default_document_loader()
    )

    documents = loader.load_all()

    print(
        "\n共读取到 "
        f"{len(documents)} 个 Document"
    )

    for index, document in enumerate(
        documents[:3],
        start=1,
    ):
        print(f"\nDocument {index}")
        print(
            f"来源："
            f"{document.metadata}"
        )
        print(
            "内容预览："
            f"{document.page_content[:200]}"
        )


if __name__ == "__main__":
    main()