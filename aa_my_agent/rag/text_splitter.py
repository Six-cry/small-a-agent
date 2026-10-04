"""知识文档切分层。

主要职责：

1. 接收 DocumentLoader 生成的 Document。
2. 将长文档切成更小的文本 Chunk。
3. 保留原始文件和页码元数据。
4. 为每个 Chunk 生成稳定的 chunk_id。
5. 记录 chunk_index、total_chunks 和 content_hash。

这个模块不负责：

- 生成 Embedding
- 访问 Chroma
- 更新 Manifest
- 调用聊天模型
"""

import hashlib
from collections import defaultdict
from typing import Iterable

from langchain_core.documents import Document
from langchain_text_splitters import (
    RecursiveCharacterTextSplitter,
)


class TextSplitError(RuntimeError):
    """文档无法被可靠切分。"""


def calculate_text_hash(
    text: str,
) -> str:
    """计算文本内容的 SHA-256。"""
    if not isinstance(text, str):
        raise TypeError(
            "计算文本 Hash 时，text 必须是字符串"
        )

    return hashlib.sha256(
        text.encode("utf-8")
    ).hexdigest()


def create_chunk_id(
    source: str,
    file_hash: str,
    page: int | None,
    chunk_index: int,
    content: str,
) -> str:
    """为一个文本 Chunk 生成稳定 ID。

    只要以下内容相同，生成的 ID 就相同：

    - 来源相对路径
    - 文件 Hash
    - 页码
    - Chunk 在文件中的序号
    - Chunk 文本内容
    """
    content_hash = calculate_text_hash(
        content
    )

    raw_data = "\0".join(
        [
            source,
            file_hash,
            (
                str(page)
                if page is not None
                else ""
            ),
            str(chunk_index),
            content_hash,
        ]
    )

    return hashlib.sha256(
        raw_data.encode("utf-8")
    ).hexdigest()


class TextSplitter:
    """使用递归字符策略切分 Document。"""

    def __init__(
        self,
        chunk_size: int | None = None,
        chunk_overlap: int | None = None,
        split_separators: (
            Iterable[str] | None
        ) = None,
    ):
        # 没有显式传入参数时，使用正式项目配置。
        # 测试时可以传入自定义参数，不依赖 config.py。
        if (
            chunk_size is None
            or chunk_overlap is None
            or split_separators is None
        ):
            from ..config import (
                RAG_CHUNK_OVERLAP,
                RAG_CHUNK_SIZE,
                separators,
            )

            if chunk_size is None:
                chunk_size = RAG_CHUNK_SIZE

            if chunk_overlap is None:
                chunk_overlap = (
                    RAG_CHUNK_OVERLAP
                )

            if split_separators is None:
                split_separators = separators

        self.chunk_size = int(
            chunk_size
        )

        self.chunk_overlap = int(
            chunk_overlap
        )

        self.separators = tuple(
            str(separator)
            for separator
            in split_separators
        )

        self._validate_config()

        self.splitter = (
            RecursiveCharacterTextSplitter(
                chunk_size=self.chunk_size,
                chunk_overlap=(
                    self.chunk_overlap
                ),
                separators=list(
                    self.separators
                ),
                length_function=len,
            )
        )

    def _validate_config(self) -> None:
        """检查切分配置是否合法。"""
        if self.chunk_size <= 0:
            raise ValueError(
                "chunk_size 必须大于 0"
            )

        if self.chunk_overlap < 0:
            raise ValueError(
                "chunk_overlap 不能小于 0"
            )

        if (
            self.chunk_overlap
            >= self.chunk_size
        ):
            raise ValueError(
                "chunk_overlap 必须小于 "
                "chunk_size"
            )

        if not self.separators:
            raise ValueError(
                "separators 不能为空"
            )

    def _validate_document(
        self,
        document: Document,
    ) -> None:
        """检查 Document 是否具备稳定切分所需元数据。"""
        if not isinstance(
            document,
            Document,
        ):
            raise TypeError(
                "documents 中的元素必须是 "
                "Document"
            )

        source = document.metadata.get(
            "source"
        )

        file_hash = document.metadata.get(
            "file_hash"
        )

        if (
            not isinstance(source, str)
            or not source.strip()
        ):
            raise TextSplitError(
                "Document 缺少有效的 "
                "source 元数据"
            )

        if (
            not isinstance(file_hash, str)
            or not file_hash.strip()
        ):
            raise TextSplitError(
                "Document 缺少有效的 "
                "file_hash 元数据"
            )

    def _split_single_document(
        self,
        document: Document,
        document_index: int,
    ) -> list[Document]:
        """切分一页或一份原始 Document。"""
        self._validate_document(
            document
        )

        if not document.page_content.strip():
            return []

        # 创建新 Document，避免修改 Loader 返回的原对象。
        prepared_document = Document(
            page_content=(
                document.page_content
            ),
            metadata={
                **document.metadata,
                "document_index": (
                    document_index
                ),
            },
        )

        # Docling 适配层已经把表格/公式/图片及其上下文组装成
        # 一个完整语义单元。这类 Document 即使略长，也不能再次
        # 从中间切开；普通正文仍沿用原来的递归字符切分策略。
        if prepared_document.metadata.get(
            "preserve_as_unit"
        ) is True:
            return [prepared_document]

        return self.splitter.split_documents(
            [prepared_document]
        )

    def _attach_chunk_metadata(
        self,
        source: str,
        chunks: list[Document],
    ) -> None:
        """给同一知识文件的所有 Chunk 添加索引元数据。"""
        total_chunks = len(chunks)

        for chunk_index, chunk in enumerate(
            chunks
        ):
            page = chunk.metadata.get(
                "page"
            )

            if not isinstance(page, int):
                page = None

            file_hash = str(
                chunk.metadata["file_hash"]
            )

            content_hash = (
                calculate_text_hash(
                    chunk.page_content
                )
            )

            chunk_id = create_chunk_id(
                source=source,
                file_hash=file_hash,
                page=page,
                chunk_index=chunk_index,
                content=chunk.page_content,
            )

            chunk.metadata["chunk_id"] = (
                chunk_id
            )

            chunk.metadata["chunk_index"] = (
                chunk_index
            )

            chunk.metadata["total_chunks"] = (
                total_chunks
            )

            chunk.metadata["content_hash"] = (
                content_hash
            )

    def split_documents(
        self,
        documents: list[Document],
    ) -> list[Document]:
        """切分文档并生成稳定 Chunk ID。

        chunk_index 是文件级序号，而不是页内序号。
        同一文件的所有页面共同使用一套连续序号。
        """
        if not documents:
            return []

        chunks_by_source: dict[
            str,
            list[Document],
        ] = defaultdict(list)

        document_count_by_source: dict[
            str,
            int,
        ] = defaultdict(int)

        for document in documents:
            self._validate_document(
                document
            )

            source = str(
                document.metadata["source"]
            )

            document_index = (
                document_count_by_source[
                    source
                ]
            )

            document_count_by_source[
                source
            ] += 1

            document_chunks = (
                self._split_single_document(
                    document=document,
                    document_index=(
                        document_index
                    ),
                )
            )

            chunks_by_source[
                source
            ].extend(
                document_chunks
            )

        all_chunks: list[Document] = []

        for source, source_chunks in (
            chunks_by_source.items()
        ):
            self._attach_chunk_metadata(
                source=source,
                chunks=source_chunks,
            )

            all_chunks.extend(
                source_chunks
            )

            print(
                f"切分完成：{source} -> "
                f"{len(source_chunks)} 个文本块"
            )

        print(
            "全部切分完成："
            f"{len(documents)} 个 Document -> "
            f"{len(all_chunks)} 个文本块"
        )

        return all_chunks

    def split_text(
        self,
        text: str,
    ) -> list[str]:
        """只切分纯文本，不生成 Chunk ID。"""
        if not isinstance(text, str):
            raise TypeError(
                "text 必须是字符串"
            )

        if not text.strip():
            return []

        return self.splitter.split_text(
            text
        )


def create_default_text_splitter(
) -> TextSplitter:
    """使用项目正式配置创建切分器。"""
    from ..config import (
        RAG_CHUNK_OVERLAP,
        RAG_CHUNK_SIZE,
        separators,
    )

    return TextSplitter(
        chunk_size=RAG_CHUNK_SIZE,
        chunk_overlap=(
            RAG_CHUNK_OVERLAP
        ),
        split_separators=separators,
    )


def main() -> None:
    """人工检查正式知识文件的切分结果。"""
    from .document_loader import (
        create_default_document_loader,
    )

    loader = (
        create_default_document_loader()
    )

    documents = loader.load_all()

    splitter = (
        create_default_text_splitter()
    )

    chunks = splitter.split_documents(
        documents
    )

    print(
        f"\n最终得到 {len(chunks)} 个 Chunk"
    )

    for index, chunk in enumerate(
        chunks[:5],
        start=1,
    ):
        print(f"\nChunk {index}")

        print(
            "来源："
            f"{chunk.metadata.get('source')}"
        )

        print(
            "页码："
            f"{chunk.metadata.get('page', '无')}"
        )

        print(
            "文件内序号："
            f"{chunk.metadata.get('chunk_index')}"
        )

        print(
            "文件总块数："
            f"{chunk.metadata.get('total_chunks')}"
        )

        print(
            "Chunk ID："
            f"{chunk.metadata.get('chunk_id')}"
        )

        print(
            "内容预览："
            f"{chunk.page_content[:200]}"
        )


if __name__ == "__main__":
    main()
